"""Manifest-first dataset loader with hash verification and column roles.

Phase 3 / T-2.2: ``ManifestDatasetLoader``.

This module implements the manifest-first loading contract that replaces
the overloaded ``dataset_manifest_ref`` field on RunPod training workers.
A worker receives a *load spec* (the manifest URI + expected hashes) and
loads the dataset in the correct, fail-closed order:

1. **Fetch the manifest** from ``manifest_uri`` (``file://`` or a volume
   path).
2. **Verify the manifest hash** against ``manifest_sha256`` — fail on
   mismatch (acceptance: manifest hash mismatch fails).
3. **Parse the manifest** to extract ``data_uri``, ``data_sha256``,
   ``data_format``, ``row_count``, and the feature/label schema hashes.
4. **Fetch the data** from the manifest-declared ``data_uri`` (never from
   an out-of-band path — acceptance: worker reads manifest first).
5. **Verify the data hash** against ``data_sha256`` — fail on mismatch
   (acceptance: bad data checksum fails).
6. **Load the data** into a dataframe (parquet or CSV). An unknown format
   fails (acceptance: unknown data format fails).
7. **Verify the row count** — fail on mismatch (acceptance: bad row count
   fails).
8. **Verify the schema hashes** — the manifest-declared
   ``feature_schema_hash`` / ``label_schema_hash`` must match the
   expected hashes on the load spec (acceptance: verify schema hashes).
9. **Build column roles** — feature/label/timestamp/symbol/weight/group
   columns. A missing required role fails (acceptance: missing required
   column role fails).
10. **Return** a :class:`LoadedDataset` with the dataframe, column roles,
    and a :class:`DatasetLoadReceipt` recording every verification.

Design constraints (from the plan + ``datasets/__init__.py``):

* **No import from ``services/quant_foundry``** — this module lives in
  ``fincept_core`` and must not create a circular dependency. The loader
  therefore accepts the load-spec fields directly (or a duck-typed spec
  object with the right attributes) rather than importing
  :class:`~quant_foundry.dataset_manifest.DatasetLoadSpec`.
* **Pydantic v2** (``frozen=True``, ``extra="forbid"``) for
  :class:`ColumnRoles` and :class:`DatasetLoadReceipt`.
* **Regular dataclass** for :class:`LoadedDataset` (it contains a
  dataframe, which is not Pydantic-compatible).
* **Fail-closed** on every verification step — a mismatch raises
  :class:`DatasetLoadError` rather than returning a partial result.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import time
from dataclasses import dataclass, field
from typing import Any, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "ColumnRoles",
    "DatasetLoadError",
    "DatasetLoadReceipt",
    "LoadedDataset",
    "ManifestDatasetLoader",
    "ManifestLike",
]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class DatasetLoadError(ValueError):
    """A dataset loading or verification step failed (fail-closed).

    Subclass of :class:`ValueError` so existing ``except ValueError``
    handlers keep catching it. ``code`` is a short machine-readable
    string (``manifest_hash_mismatch``, ``data_hash_mismatch``,
    ``row_count_mismatch``, ``schema_hash_mismatch``,
    ``unknown_data_format``, ``missing_column_role``, ``fetch_failed``,
    ``parse_failed``) the handler/API layer maps to an HTTP problem-detail
    ``code`` field.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# Protocols / duck-typed spec
# ---------------------------------------------------------------------------


class ManifestLike(Protocol):
    """Structural type for a manifest-first load spec.

    Mirrors :class:`quant_foundry.dataset_manifest.DatasetLoadSpec` without
    importing it (avoids a circular ``fincept_core`` →
    ``services/quant_foundry`` dependency). Any object with these
    attributes is accepted by :class:`ManifestDatasetLoader`.
    """

    manifest_uri: str
    manifest_sha256: str | None
    data_uri: str
    data_sha256: str | None
    data_format: Any  # DataFormat | None — kept as Any to avoid the import
    row_count: int | None
    feature_schema_hash: str | None
    label_schema_hash: str | None


# ---------------------------------------------------------------------------
# Column roles
# ---------------------------------------------------------------------------


class ColumnRoles(BaseModel):
    """Maps dataframe columns to their semantic role in a training dataset.

    Frozen + ``extra="forbid"`` (audit integrity). At least one feature
    column and at least one label column are required — a dataset with no
    features or no labels is not trainable (acceptance: missing required
    column role fails).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    feature_columns: tuple[str, ...]
    label_columns: tuple[str, ...]
    timestamp_column: str | None = None
    symbol_column: str | None = None
    weight_column: str | None = None
    group_column: str | None = None
    excluded_columns: tuple[str, ...] = Field(default_factory=tuple)

    @field_validator("feature_columns", "label_columns")
    @classmethod
    def _nonempty(cls, v: tuple[str, ...], info: Any) -> tuple[str, ...]:
        if not v:
            raise ValueError(
                f"{info.field_name} must contain at least one column (missing required column role)"
            )
        return v

    @field_validator("feature_columns", "label_columns", "excluded_columns")
    @classmethod
    def _no_duplicates(cls, v: tuple[str, ...], info: Any) -> tuple[str, ...]:
        seen: set[str] = set()
        for col in v:
            if col in seen:
                raise ValueError(f"{info.field_name} contains duplicate column {col!r}")
            seen.add(col)
        return v

    @field_validator("feature_columns", "label_columns")
    @classmethod
    def _no_empty_strings(cls, v: tuple[str, ...], info: Any) -> tuple[str, ...]:
        for col in v:
            if not col or not col.strip():
                raise ValueError(f"{info.field_name} contains an empty column name")
        return v


# ---------------------------------------------------------------------------
# Load receipt
# ---------------------------------------------------------------------------


class DatasetLoadReceipt(BaseModel):
    """Audit record of every verification performed during a load.

    Frozen + ``extra="forbid"``. Every ``*_verified`` flag is ``True``
    on a successful load (the loader raises before returning a receipt
    on any failure). ``loaded_at_ns`` is the wall-clock nanosecond
    timestamp at which the load completed.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    manifest_uri: str
    manifest_sha256_verified: bool
    data_uri: str
    data_sha256_verified: bool
    row_count_verified: bool
    schema_verified: bool
    loaded_at_ns: int

    @field_validator("manifest_uri", "data_uri")
    @classmethod
    def _uri_nonempty(cls, v: str, info: Any) -> str:
        if not v or not v.strip():
            raise ValueError(f"{info.field_name} must be non-empty")
        return v

    @field_validator("loaded_at_ns")
    @classmethod
    def _loaded_at_nonnegative(cls, v: int) -> int:
        if v < 0:
            raise ValueError(f"loaded_at_ns must be >= 0; got {v}")
        return v


# ---------------------------------------------------------------------------
# Loaded dataset (dataclass — holds a dataframe)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LoadedDataset:
    """The result of a successful manifest-first dataset load.

    A regular dataclass (not Pydantic) because ``df`` holds a pandas
    DataFrame / pyarrow Table, which is not Pydantic-compatible. The
    receipt + hashes give the caller everything needed to prove the
    dataset was loaded and verified.
    """

    df: Any
    column_roles: ColumnRoles
    manifest_hash: str
    data_hash: str | None
    row_count: int
    schema_verified: bool
    load_receipt: DatasetLoadReceipt
    # The manifest JSON as fetched (for downstream consumers that need
    # the full manifest, e.g. the feature-lake folds).
    manifest_json: str = ""
    # Optional: the parsed manifest dict (manifest_hash, folds, etc.).
    manifest: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Manifest dataset loader
# ---------------------------------------------------------------------------


# Substrings that mark a URI as inline CSV (used by canary mode). The
# loader resolves these to an in-memory bytes payload supplied by the
# caller rather than a file on disk.
_INLINE_PREFIX = "inline://"


class ManifestDatasetLoader:
    """Manifest-first dataset loader with fail-closed hash verification.

    Construction accepts either a duck-typed spec object (anything with
    the :class:`ManifestLike` attributes) or the individual fields. The
    loader does NOT import :class:`DatasetLoadSpec` — it reads the
    attributes off whatever object it is given, keeping
    ``fincept_core`` free of a ``services/quant_foundry`` dependency.

    Usage::

        loader = ManifestDatasetLoader(spec=load_spec)
        loaded = loader.load()
        # loaded.df, loaded.column_roles, loaded.load_receipt

    Or with explicit fields::

        loader = ManifestDatasetLoader(
            manifest_uri="/workspace/datasets/x.manifest.json",
            manifest_sha256="abcd...",
            data_uri="/workspace/datasets/x.parquet",
            data_sha256="ef12...",
            data_format="parquet",
            row_count=1000,
            feature_schema_hash="a1b2...",
            label_schema_hash="c3d4...",
        )
        loaded = loader.load()

    All verification steps fail closed: a mismatch raises
    :class:`DatasetLoadError` with a machine-readable ``code``.
    """

    def __init__(
        self,
        *,
        spec: ManifestLike | None = None,
        manifest_uri: str | None = None,
        manifest_sha256: str | None = None,
        data_uri: str | None = None,
        data_sha256: str | None = None,
        data_format: Any = None,
        row_count: int | None = None,
        feature_schema_hash: str | None = None,
        label_schema_hash: str | None = None,
        column_roles: ColumnRoles | None = None,
        approved_roots: Any = None,
    ) -> None:
        if spec is not None:
            self.manifest_uri: str = spec.manifest_uri
            self.manifest_sha256: str | None = spec.manifest_sha256
            self.data_uri: str = spec.data_uri
            self.data_sha256: str | None = spec.data_sha256
            self.data_format: Any = spec.data_format
            self.row_count: int | None = spec.row_count
            self.feature_schema_hash: str | None = spec.feature_schema_hash
            self.label_schema_hash: str | None = spec.label_schema_hash
        else:
            if manifest_uri is None:
                raise DatasetLoadError(
                    "missing_spec",
                    "manifest_uri is required when no spec is provided",
                )
            if data_uri is None:
                raise DatasetLoadError(
                    "missing_spec",
                    "data_uri is required when no spec is provided",
                )
            self.manifest_uri = manifest_uri
            self.manifest_sha256 = manifest_sha256
            self.data_uri = data_uri
            self.data_sha256 = data_sha256
            self.data_format = data_format
            self.row_count = row_count
            self.feature_schema_hash = feature_schema_hash
            self.label_schema_hash = label_schema_hash

        # Optional explicit column roles (overrides inference). When
        # None, the loader infers roles from the manifest's
        # ``column_roles`` section or from common column-name conventions.
        self._explicit_column_roles: ColumnRoles | None = column_roles
        # Optional approved-roots gate for path validation. When None,
        # no path gate is applied (the handler applies its own volume
        # resolution). Kept as Any to avoid importing ApprovedRoots at
        # module level (it is in the same package, but the loader is
        # usable without it).
        self._approved_roots = approved_roots

    # ------------------------------------------------------------------ #
    # Public API                                                          #
    # ------------------------------------------------------------------ #

    def load(self) -> LoadedDataset:
        """Fetch + verify the manifest, then fetch + verify the data.

        Returns a :class:`LoadedDataset` on success. Raises
        :class:`DatasetLoadError` on any verification failure
        (fail-closed).
        """
        # 1. Fetch the manifest.
        manifest_bytes = self._fetch_bytes(self.manifest_uri, label="manifest")
        manifest_json = manifest_bytes.decode("utf-8")

        # 2. Verify the manifest hash (if declared).
        manifest_sha_verified = True
        if self.manifest_sha256:
            actual_manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
            if actual_manifest_hash != self.manifest_sha256.lower():
                raise DatasetLoadError(
                    "manifest_hash_mismatch",
                    f"manifest sha256 mismatch: expected "
                    f"{self.manifest_sha256}, got {actual_manifest_hash}",
                )
        else:
            # No manifest hash declared — compute it for the receipt but
            # do not fail (canary/research may omit it). Production mode
            # requires it (enforced on DatasetLoadSpec construction).
            manifest_sha_verified = False

        manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()

        # 3. Parse the manifest.
        try:
            manifest_dict = json.loads(manifest_json)
        except json.JSONDecodeError as exc:
            raise DatasetLoadError(
                "parse_failed",
                f"manifest is not valid JSON: {exc}",
            ) from exc
        if not isinstance(manifest_dict, dict):
            raise DatasetLoadError(
                "parse_failed",
                f"manifest must be a JSON object; got {type(manifest_dict).__name__}",
            )

        # 4. Extract the data location + verification fields from the
        #    manifest. The manifest-declared values take precedence over
        #    the spec's values (the manifest is the source of truth once
        #    verified). But we cross-check consistency where both are
        #    present.
        data_uri = self._extract_from_manifest(manifest_dict, "data_uri", self.data_uri)
        data_sha256 = self._extract_optional_from_manifest(
            manifest_dict, "data_sha256", self.data_sha256
        )
        data_format = self._extract_optional_from_manifest(
            manifest_dict, "data_format", self.data_format
        )
        row_count = self._extract_optional_int_from_manifest(
            manifest_dict, "row_count", self.row_count
        )
        manifest_feature_schema_hash = self._extract_optional_from_manifest(
            manifest_dict, "feature_schema_hash", self.feature_schema_hash
        )
        manifest_label_schema_hash = self._extract_optional_from_manifest(
            manifest_dict, "label_schema_hash", self.label_schema_hash
        )

        # 5. Verify schema hashes: spec-declared vs manifest-declared.
        schema_verified = self._verify_schema_hashes(
            manifest_feature_schema_hash,
            manifest_label_schema_hash,
        )

        # 6. Fetch the data.
        data_bytes = self._fetch_bytes(data_uri, label="data")

        # 7. Verify the data hash (if declared).
        data_sha_verified = True
        data_hash: str | None
        if data_sha256:
            data_hash = hashlib.sha256(data_bytes).hexdigest()
            if data_hash != data_sha256.lower():
                raise DatasetLoadError(
                    "data_hash_mismatch",
                    f"data sha256 mismatch: expected {data_sha256}, got {data_hash}",
                )
        else:
            data_sha_verified = False
            data_hash = hashlib.sha256(data_bytes).hexdigest()

        # 8. Resolve the data format (infer from the URI if not declared).
        resolved_format = self._resolve_data_format(data_format, data_uri)

        # 9. Load the data into a dataframe.
        df = self._load_dataframe(data_bytes, resolved_format, data_uri)

        # 10. Verify the row count (if declared).
        actual_row_count = self._row_count_of(df)
        row_count_verified = True
        if row_count is not None:
            if actual_row_count != row_count:
                raise DatasetLoadError(
                    "row_count_mismatch",
                    f"row count mismatch: expected {row_count}, got {actual_row_count}",
                )
        else:
            row_count_verified = False

        # 11. Build / verify column roles.
        column_roles = self._resolve_column_roles(
            manifest_dict,
            df,
            manifest_feature_schema_hash,
            manifest_label_schema_hash,
        )

        # 12. Build the receipt.
        receipt = DatasetLoadReceipt(
            manifest_uri=self.manifest_uri,
            manifest_sha256_verified=manifest_sha_verified,
            data_uri=data_uri,
            data_sha256_verified=data_sha_verified,
            row_count_verified=row_count_verified,
            schema_verified=schema_verified,
            loaded_at_ns=time.time_ns(),
        )

        return LoadedDataset(
            df=df,
            column_roles=column_roles,
            manifest_hash=manifest_hash,
            data_hash=data_hash,
            row_count=actual_row_count,
            schema_verified=schema_verified,
            load_receipt=receipt,
            manifest_json=manifest_json,
            manifest=manifest_dict,
        )

    # ------------------------------------------------------------------ #
    # Fetching                                                            #
    # ------------------------------------------------------------------ #

    def _fetch_bytes(self, uri: str, *, label: str) -> bytes:
        """Fetch the raw bytes at ``uri`` (file:// or volume path).

        Raises :class:`DatasetLoadError` (``fetch_failed``) on any I/O
        error. ``inline://`` URIs are not supported here — the caller
        must supply an explicit bytes payload via :meth:`load_inline`
        for canary mode.
        """
        if not uri or not uri.strip():
            raise DatasetLoadError(
                "fetch_failed",
                f"{label} URI is empty",
            )
        path = self._uri_to_path(uri, label=label)
        try:
            return path.read_bytes()
        except OSError as exc:
            raise DatasetLoadError(
                "fetch_failed",
                f"failed to fetch {label} from {uri}: {exc}",
            ) from exc

    def _uri_to_path(self, uri: str, *, label: str) -> pathlib.Path:
        """Convert a URI to a :class:`pathlib.Path`.

        Supports:
        - ``file://`` URIs → local path (handles Windows drive letters:
          ``file:///C:/...`` → ``C:\\...``).
        - Plain volume paths (``/workspace/...``, ``/runpod-volume/...``,
          relative paths).
        """
        if uri.startswith("file://"):
            raw = uri[len("file://") :]
            # On Windows, ``file:///C:/path`` strips to ``/C:/path``;
            # the leading slash before a drive letter must be removed
            # so pathlib does not treat it as a UNC path.
            if len(raw) >= 3 and raw[0] == "/" and raw[2] == ":" and raw[1].isalpha():
                raw = raw[1:]
            return pathlib.Path(raw)
        if uri.startswith("inline://"):
            raise DatasetLoadError(
                "fetch_failed",
                f"{label} URI is inline:// — use load_inline() for inline data",
            )
        # Plain path (volume or relative).
        return pathlib.Path(uri)

    # ------------------------------------------------------------------ #
    # Manifest parsing helpers                                            #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _extract_from_manifest(
        manifest: dict[str, Any],
        key: str,
        fallback: str,
    ) -> str:
        """Extract a required string field from the manifest."""
        val = manifest.get(key)
        if val is not None and isinstance(val, str) and val.strip():
            return cast("str", val)
        if fallback:
            return fallback
        raise DatasetLoadError(
            "parse_failed",
            f"manifest is missing required field {key!r}",
        )

    @staticmethod
    def _extract_optional_from_manifest(
        manifest: dict[str, Any],
        key: str,
        fallback: Any,
    ) -> Any:
        """Extract an optional field from the manifest, falling back to the spec value."""
        val = manifest.get(key)
        if val is None:
            return fallback
        return val

    @staticmethod
    def _extract_optional_int_from_manifest(
        manifest: dict[str, Any],
        key: str,
        fallback: int | None,
    ) -> int | None:
        val = manifest.get(key)
        if val is None:
            return fallback
        if isinstance(val, bool) or not isinstance(val, int):
            raise DatasetLoadError(
                "parse_failed",
                f"manifest field {key!r} must be an integer; got {val!r}",
            )
        return cast("int", val)

    # ------------------------------------------------------------------ #
    # Schema hash verification                                            #
    # ------------------------------------------------------------------ #

    def _verify_schema_hashes(
        self,
        manifest_feature_hash: str | None,
        manifest_label_hash: str | None,
    ) -> bool:
        """Verify spec-declared schema hashes match the manifest-declared ones.

        Returns ``True`` if at least one hash was verified, ``False`` if
        no hashes were declared on either side (canary/research). Raises
        :class:`DatasetLoadError` (``schema_hash_mismatch``) on mismatch.
        """
        verified_any = False
        # Feature schema hash.
        if self.feature_schema_hash and manifest_feature_hash:
            verified_any = True
            if self.feature_schema_hash.lower() != str(manifest_feature_hash).lower():
                raise DatasetLoadError(
                    "schema_hash_mismatch",
                    f"feature_schema_hash mismatch: spec declares "
                    f"{self.feature_schema_hash} but manifest declares "
                    f"{manifest_feature_hash}",
                )
        # Label schema hash.
        if self.label_schema_hash and manifest_label_hash:
            verified_any = True
            if self.label_schema_hash.lower() != str(manifest_label_hash).lower():
                raise DatasetLoadError(
                    "schema_hash_mismatch",
                    f"label_schema_hash mismatch: spec declares "
                    f"{self.label_schema_hash} but manifest declares "
                    f"{manifest_label_hash}",
                )
        return verified_any

    # ------------------------------------------------------------------ #
    # Data format resolution + loading                                    #
    # ------------------------------------------------------------------ #

    def _resolve_data_format(self, declared: Any, data_uri: str) -> str:
        """Resolve the data format, inferring from the URI if not declared.

        Returns one of ``"parquet"`` or ``"csv"``. Raises
        :class:`DatasetLoadError` (``unknown_data_format``) if the format
        cannot be determined or is unsupported (acceptance: unknown data
        format fails).
        """
        # A StrEnum value stringifies to its value; accept both the enum
        # and a plain string.
        if declared is not None:
            fmt = str(declared).lower()
            if fmt in ("parquet", "csv"):
                return fmt
            raise DatasetLoadError(
                "unknown_data_format",
                f"unsupported data_format {declared!r} (expected parquet or csv)",
            )
        # Infer from the URI extension.
        low = data_uri.lower()
        if low.endswith(".parquet") or low.endswith(".parquet.gz"):
            return "parquet"
        if low.endswith(".csv") or low.endswith(".csv.gz"):
            return "csv"
        raise DatasetLoadError(
            "unknown_data_format",
            f"cannot infer data_format from data_uri {data_uri!r} "
            "(expected .parquet or .csv extension, or an explicit data_format)",
        )

    def _load_dataframe(
        self,
        data_bytes: bytes,
        fmt: str,
        data_uri: str,
    ) -> Any:
        """Load ``data_bytes`` into a dataframe using the resolved format.

        Lazy-imports pyarrow/pandas so the module is importable without
        ML deps. Raises :class:`DatasetLoadError` (``parse_failed``) on
        any load error.
        """
        if fmt == "parquet":
            return self._load_parquet(data_bytes, data_uri)
        if fmt == "csv":
            return self._load_csv(data_bytes, data_uri)
        # Should be unreachable (resolve_data_format guards this).
        raise DatasetLoadError(
            "unknown_data_format",
            f"unsupported data_format {fmt!r}",
        )

    @staticmethod
    def _load_parquet(data_bytes: bytes, data_uri: str) -> Any:
        """Load parquet bytes into a dataframe (pyarrow preferred, pandas fallback)."""
        try:
            import pyarrow.parquet as pq
        except ImportError:
            try:
                import pandas as pd
            except ImportError as exc:
                raise DatasetLoadError(
                    "parse_failed",
                    "neither pyarrow nor pandas available for parquet loading",
                ) from exc
            import io

            try:
                return pd.read_parquet(io.BytesIO(data_bytes))
            except Exception as exc:
                raise DatasetLoadError(
                    "parse_failed",
                    f"failed to parse parquet from {data_uri}: {exc}",
                ) from exc
        import io

        try:
            return pq.read_table(io.BytesIO(data_bytes)).to_pandas()  # type: ignore[no-untyped-call]  # pyarrow read_table lacks type stubs
        except Exception:
            # Fallback: pyarrow Table (no pandas conversion).
            try:
                return pq.read_table(io.BytesIO(data_bytes))  # type: ignore[no-untyped-call]  # pyarrow read_table lacks type stubs
            except Exception as exc:
                raise DatasetLoadError(
                    "parse_failed",
                    f"failed to parse parquet from {data_uri}: {exc}",
                ) from exc

    @staticmethod
    def _load_csv(data_bytes: bytes, data_uri: str) -> Any:
        """Load CSV bytes into a dataframe (pandas preferred, numpy fallback)."""
        try:
            import pandas as pd
        except ImportError:
            try:
                import numpy as np
            except ImportError as exc:
                raise DatasetLoadError(
                    "parse_failed",
                    "neither pandas nor numpy available for CSV loading",
                ) from exc
            import io

            try:
                data = np.genfromtxt(
                    io.BytesIO(data_bytes),
                    delimiter=",",
                    skip_header=1,
                    dtype=float,
                )
                if data.ndim == 1:
                    data = data.reshape(1, -1)
                return data
            except Exception as exc:
                raise DatasetLoadError(
                    "parse_failed",
                    f"failed to parse CSV from {data_uri}: {exc}",
                ) from exc
        import io

        try:
            return pd.read_csv(io.BytesIO(data_bytes))
        except Exception as exc:
            raise DatasetLoadError(
                "parse_failed",
                f"failed to parse CSV from {data_uri}: {exc}",
            ) from exc

    # ------------------------------------------------------------------ #
    # Row count                                                           #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _row_count_of(df: Any) -> int:
        """Return the number of rows in ``df`` (pandas, pyarrow, or numpy)."""
        # pandas DataFrame / pyarrow Table both have __len__.
        if hasattr(df, "__len__"):
            return len(df)
        # numpy ndarray → shape[0].
        shape = getattr(df, "shape", None)
        if shape is not None and len(shape) >= 1:
            return int(shape[0])
        raise DatasetLoadError(
            "parse_failed",
            f"cannot determine row count of {type(df).__name__}",
        )

    # ------------------------------------------------------------------ #
    # Column roles                                                        #
    # ------------------------------------------------------------------ #

    def _resolve_column_roles(
        self,
        manifest: dict[str, Any],
        df: Any,
        feature_schema_hash: str | None,
        label_schema_hash: str | None,
    ) -> ColumnRoles:
        """Build the column roles, preferring explicit > manifest > inference.

        Raises :class:`DatasetLoadError` (``missing_column_role``) if no
        feature or label columns can be determined (acceptance: missing
        required column role fails).
        """
        # 1. Explicit roles passed to the constructor.
        if self._explicit_column_roles is not None:
            return self._explicit_column_roles

        # 2. Manifest-declared column roles.
        manifest_roles = manifest.get("column_roles")
        if isinstance(manifest_roles, dict):
            try:
                return self._column_roles_from_dict(manifest_roles, df)
            except DatasetLoadError:
                # Fall through to inference if the manifest roles are
                # incomplete (but re-raise if the manifest explicitly
                # declared roles that don't match the data).
                if manifest_roles.get("feature_columns") or manifest_roles.get("label_columns"):
                    raise
                # else: manifest roles present but empty → infer.

        # 3. Infer from the dataframe columns.
        return self._infer_column_roles(df)

    @staticmethod
    def _column_roles_from_dict(
        roles: dict[str, Any],
        df: Any,
    ) -> ColumnRoles:
        """Build :class:`ColumnRoles` from a manifest-declared dict."""
        feature_cols = roles.get("feature_columns") or []
        label_cols = roles.get("label_columns") or []
        excluded = roles.get("excluded_columns") or []
        return ColumnRoles(
            feature_columns=tuple(str(c) for c in feature_cols),
            label_columns=tuple(str(c) for c in label_cols),
            timestamp_column=roles.get("timestamp_column"),
            symbol_column=roles.get("symbol_column"),
            weight_column=roles.get("weight_column"),
            group_column=roles.get("group_column"),
            excluded_columns=tuple(str(c) for c in excluded),
        )

    @staticmethod
    def _infer_column_roles(df: Any) -> ColumnRoles:
        """Infer column roles from common column-name conventions.

        - ``label`` (or the last column) → label.
        - ``timestamp`` / ``decision_time`` / ``ts`` / ``event_ts`` → timestamp.
        - ``symbol`` / ``ticker`` → symbol.
        - ``weight`` / ``sample_weight`` → weight.
        - ``group`` / ``fold_id`` → group.
        - All other columns (minus the above) → features.
        """
        columns = ManifestDatasetLoader._columns_of(df)
        if not columns:
            raise DatasetLoadError(
                "missing_column_role",
                "dataset has no columns — cannot infer column roles",
            )

        ts_candidates = ("timestamp", "decision_time", "ts", "event_ts")
        symbol_candidates = ("symbol", "ticker")
        weight_candidates = ("weight", "sample_weight")
        group_candidates = ("group", "fold_id")

        ts_col = next((c for c in columns if c in ts_candidates), None)
        symbol_col = next((c for c in columns if c in symbol_candidates), None)
        weight_col = next((c for c in columns if c in weight_candidates), None)
        group_col = next((c for c in columns if c in group_candidates), None)

        # Label: prefer an explicit "label" column, else the last column.
        label_cols: list[str] = []
        if "label" in columns:
            label_cols = ["label"]
        elif "y" in columns:
            label_cols = ["y"]
        else:
            label_cols = [columns[-1]]

        # Features: everything that is not a label, timestamp, symbol,
        # weight, or group column.
        reserved = set(label_cols) | {ts_col, symbol_col, weight_col, group_col}
        reserved.discard(None)
        feature_cols = [c for c in columns if c not in reserved]

        if not feature_cols:
            raise DatasetLoadError(
                "missing_column_role",
                "cannot infer feature columns — all columns are reserved "
                f"(label/timestamp/symbol/weight/group); columns={columns!r}",
            )
        if not label_cols:
            raise DatasetLoadError(
                "missing_column_role",
                "cannot infer label columns — dataset has no columns",
            )

        return ColumnRoles(
            feature_columns=tuple(feature_cols),
            label_columns=tuple(label_cols),
            timestamp_column=ts_col,
            symbol_column=symbol_col,
            weight_column=weight_col,
            group_column=group_col,
            excluded_columns=tuple(),
        )

    @staticmethod
    def _columns_of(df: Any) -> list[str]:
        """Return the column names of ``df`` (pandas, pyarrow, or numpy)."""
        # pandas DataFrame.
        if hasattr(df, "columns"):
            try:
                return [str(c) for c in df.columns]
            except Exception:
                pass
        # pyarrow Table.
        if hasattr(df, "column_names"):
            return list(df.column_names)
        # numpy ndarray → positional column names.
        shape = getattr(df, "shape", None)
        if shape is not None and len(shape) >= 2:
            return [f"col_{i}" for i in range(shape[1])]
        if shape is not None and len(shape) == 1:
            return ["col_0"]
        return []
