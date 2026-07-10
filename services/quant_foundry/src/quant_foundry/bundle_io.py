"""
quant_foundry.bundle_io — Bundle round-trip contract (C1).

Implements the minimal bundle format that closes the write-only gap
identified in the architecture review: a trained artifact that cannot be
loaded and scored must never produce a signed success callback.

Public contracts:
    ModelBundle          — in-memory loaded bundle (manifest + models)
    BundleManifest       — frozen Pydantic v2 schema listing every member + sha256
    BundleScorer         — scores feature rows → Decision
    Decision             — frozen Pydantic v2 scoring output
    TrainingSelfCheck    — frozen Pydantic v2 selfcheck result
    BundleLoadError      — raised when a bundle cannot be loaded
    SchemaMismatchError  — raised when feature schema does not match

Bundle format (v1):
    A zip archive containing:
      - bundle_manifest.json   (BundleManifest serialized as JSON)
      - primary.pkl            (primary model, pickled)
      - meta.pkl               (meta model, pickled — only for meta_labeled)

    The bundle_sha256 is the SHA-256 of the entire zip archive bytes.
    load_bundle() verifies every member's sha256 before returning.

Legacy compatibility:
    A bare LightGBM pickle (not a zip) is accepted by load_bundle() as a
    read-only legacy single bundle. New training writes only ModelBundle v1.

Invariants enforced:
    1. New training writes only ModelBundle v1.
    2. Legacy bare LightGBM pickle is load-only compatibility.
    3. bundle_manifest.json lists every member and sha256.
    4. load_bundle() verifies member hashes before scoring.
    5. bundle_kind is one of: single, meta_labeled.
    6. meta_labeled requires both primary and meta members.
    7. Missing meta member fails closed.
    8. Unknown bundle kind fails closed.
    9. Feature schema mismatch fails before scoring.
    10. Selfcheck runs against the final serialized artifact bytes.
    11. A selfcheck crash is a selfcheck failure.
    12. Selfcheck failure → error_code="bundle_selfcheck_failed".
    13. Selfcheck success records passed/n_rows_scored/output_sha256/
        bundle_sha256/loader_version/duration_ms.
"""

from __future__ import annotations

import hashlib
import io
import json
import pickle
import time
import zipfile
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Loader version stamp. Embedded in every manifest and selfcheck so the
#: trusted side knows which loader code path produced/consumed the bundle.
LOADER_VERSION: str = "bundle-v1"

#: The manifest filename inside the zip archive.
_MANIFEST_FILENAME: str = "bundle_manifest.json"

#: The primary model member filename inside the zip archive.
_PRIMARY_FILENAME: str = "primary.pkl"

#: The meta model member filename inside the zip archive.
_META_FILENAME: str = "meta.pkl"

#: Default abstention threshold for meta-labeled bundles. If the meta
#: model's probability (meta_p) is below this, the decision abstains.
_DEFAULT_META_THRESHOLD: float = 0.5

#: Default policy version stamp for decisions.
_DEFAULT_POLICY_VERSION: str = "meta-abstain-v1"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class BundleLoadError(Exception):
    """Raised when a bundle cannot be loaded (corrupt, missing member, etc.)."""


class SchemaMismatchError(BundleLoadError):
    """Raised when the feature schema does not match the bundle manifest."""


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class BundleKind(StrEnum):
    """Allowed bundle kinds.

    - ``single``: one primary model.
    - ``meta_labeled``: primary model + meta model (abstention policy).
    """

    SINGLE = "single"
    META_LABELED = "meta_labeled"


# ---------------------------------------------------------------------------
# Pydantic v2 schemas (frozen, extra="forbid")
# ---------------------------------------------------------------------------


class BundleMember(BaseModel):
    """A single member file inside the bundle archive."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    filename: str
    sha256: str
    size_bytes: int
    role: str  # "primary" or "meta"


class BundleManifest(BaseModel):
    """Manifest listing every member and its sha256.

    Serialized as ``bundle_manifest.json`` inside the archive.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    bundle_kind: BundleKind
    loader_version: str
    model_family: str
    feature_names: list[str]
    feature_schema_hash: str
    label_schema_hash: str
    members: dict[str, BundleMember]
    created_at_ns: int
    # Meta-labeling fields (only for meta_labeled bundles).
    label_map: dict[str, int] | None = None
    meta_label_config: dict[str, Any] | None = None
    # Policy version for the abstention policy.
    policy_version: str = _DEFAULT_POLICY_VERSION


class Decision(BaseModel):
    """Scoring output from BundleScorer.score().

    Invariant: ``abstained=True`` ⇒ ``act=False``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    p: float = Field(ge=0.0, le=1.0)
    direction: int = Field(ge=-1, le=1)
    act: bool
    abstained: bool
    meta_p: float | None = Field(default=None, ge=0.0, le=1.0)
    bundle_sha256: str
    policy_version: str


class TrainingSelfCheck(BaseModel):
    """Result of a training selfcheck.

    On success: ``passed=True`` with all fields populated.
    On failure: ``passed=False`` (the handler emits error_code=
    "bundle_selfcheck_failed").
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    passed: bool
    n_rows_scored: int = 0
    output_sha256: str = ""
    bundle_sha256: str = ""
    loader_version: str = LOADER_VERSION
    duration_ms: float = 0.0
    error_detail: str | None = None


# ---------------------------------------------------------------------------
# In-memory loaded bundle
# ---------------------------------------------------------------------------


class ModelBundle:
    """In-memory representation of a loaded bundle.

    Holds the manifest, the loaded primary model, and (for meta_labeled)
    the loaded meta model. The ``bundle_sha256`` is the SHA-256 of the
    archive bytes the bundle was loaded from.
    """

    __slots__ = ("bundle_sha256", "manifest", "meta_model", "primary_model")

    def __init__(
        self,
        manifest: BundleManifest,
        primary_model: Any,
        meta_model: Any | None,
        bundle_sha256: str,
    ) -> None:
        self.manifest = manifest
        self.primary_model = primary_model
        self.meta_model = meta_model
        self.bundle_sha256 = bundle_sha256

    @property
    def bundle_kind(self) -> BundleKind:
        return self.manifest.bundle_kind

    @property
    def is_meta_labeled(self) -> bool:
        return self.manifest.bundle_kind == BundleKind.META_LABELED


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_bundle(
    *,
    primary_model: Any,
    meta_model: Any | None = None,
    feature_names: list[str],
    feature_schema_hash: str,
    label_schema_hash: str,
    model_family: str,
    label_map: dict[str, int] | None = None,
    meta_label_config: dict[str, Any] | None = None,
    created_at_ns: int | None = None,
) -> bytes:
    """Serialize a ModelBundle v1 to zip archive bytes.

    Returns the raw zip bytes. The caller should register
    ``sha256(return_value)`` as the artifact sha256.

    Args:
        primary_model: the primary model object (pickled).
        meta_model: the meta model object (pickled). Required for
            meta_labeled bundles; must be None for single bundles.
        feature_names: ordered feature column names.
        feature_schema_hash: sha256 hex binding the feature schema.
        label_schema_hash: sha256 hex binding the label schema.
        model_family: model family string (e.g. "gbm").
        label_map: optional label remap dict (for meta_labeled).
        meta_label_config: optional meta-labeling config dict.
        created_at_ns: nanosecond epoch timestamp. Defaults to now.

    Raises:
        ValueError: if meta_model is provided but meta_label_config is
            None, or vice versa partial (inconsistent bundle kind).
    """
    if created_at_ns is None:
        created_at_ns = time.time_ns()

    # Determine bundle kind from the presence of meta_model.
    if meta_model is not None:
        bundle_kind = BundleKind.META_LABELED
    else:
        bundle_kind = BundleKind.SINGLE

    # Validate consistency: meta_labeled requires meta_model.
    if bundle_kind == BundleKind.META_LABELED and meta_model is None:
        raise ValueError("meta_labeled bundle requires meta_model")
    if bundle_kind == BundleKind.SINGLE and meta_model is not None:
        raise ValueError("single bundle must not have meta_model")

    # Pickle members.
    primary_bytes = pickle.dumps(primary_model, protocol=pickle.HIGHEST_PROTOCOL)
    members: dict[str, BundleMember] = {
        "primary": BundleMember(
            filename=_PRIMARY_FILENAME,
            sha256=_sha256_bytes(primary_bytes),
            size_bytes=len(primary_bytes),
            role="primary",
        ),
    }

    meta_bytes: bytes | None = None
    if bundle_kind == BundleKind.META_LABELED:
        meta_bytes = pickle.dumps(meta_model, protocol=pickle.HIGHEST_PROTOCOL)
        members["meta"] = BundleMember(
            filename=_META_FILENAME,
            sha256=_sha256_bytes(meta_bytes),
            size_bytes=len(meta_bytes),
            role="meta",
        )

    manifest = BundleManifest(
        bundle_kind=bundle_kind,
        loader_version=LOADER_VERSION,
        model_family=model_family,
        feature_names=list(feature_names),
        feature_schema_hash=feature_schema_hash,
        label_schema_hash=label_schema_hash,
        members=members,
        created_at_ns=created_at_ns,
        label_map={str(k): int(v) for k, v in label_map.items()} if label_map else None,
        meta_label_config=dict(meta_label_config) if meta_label_config else None,
    )

    manifest_json = manifest.model_dump_json(indent=2).encode("utf-8")

    # Build the zip archive in memory.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(_MANIFEST_FILENAME, manifest_json)
        zf.writestr(_PRIMARY_FILENAME, primary_bytes)
        if meta_bytes is not None:
            zf.writestr(_META_FILENAME, meta_bytes)

    return buf.getvalue()


# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------


def _is_zip(data: bytes) -> bool:
    """Check if data starts with a zip magic number."""
    return len(data) >= 4 and data[:4] == b"PK\x03\x04"


def load_bundle(data: bytes | str) -> ModelBundle:
    """Load a ModelBundle from bytes or a file path.

    Supports:
    - ModelBundle v1 (zip archive with bundle_manifest.json).
    - Legacy bare LightGBM pickle (load-only compatibility, treated as
      a single bundle with no manifest).

    Verifies every member's sha256 before returning. Fails closed on:
    - Corrupt or missing member hash.
    - Missing meta member for meta_labeled bundles.
    - Unknown bundle kind.
    - Feature schema mismatch (checked at score time, not load time).

    Args:
        data: raw bundle bytes, or a filesystem path (str).

    Raises:
        BundleLoadError: if the bundle cannot be loaded.
        SchemaMismatchError: (at score time) if features don't match.
    """
    # Resolve to bytes.
    if isinstance(data, str):
        with open(data, "rb") as fh:
            raw = fh.read()
    elif isinstance(data, (bytes, bytearray)):
        raw = bytes(data)
    else:
        raise BundleLoadError(f"unsupported data type for load_bundle: {type(data)!r}")

    bundle_sha256 = _sha256_bytes(raw)

    # Legacy bare pickle (not a zip) → load-only compatibility.
    if not _is_zip(raw):
        return _load_legacy_pickle(raw, bundle_sha256)

    # ModelBundle v1 zip archive.
    return _load_zip_bundle(raw, bundle_sha256)


def _load_legacy_pickle(raw: bytes, bundle_sha256: str) -> ModelBundle:
    """Load a legacy bare LightGBM pickle as a read-only single bundle.

    The pickle may contain:
    - A bare lightgbm.Booster (legacy single).
    - A dict with "primary" + "meta" (legacy meta-labeled, written by
      the pre-C1 trainer). We load it but wrap it in a synthetic manifest.
    """
    try:
        obj = pickle.loads(raw)  # noqa: S301 — trusted trainer artifact
    except Exception as exc:
        raise BundleLoadError(f"legacy pickle load failed: {exc}") from exc

    if isinstance(obj, dict) and "primary" in obj:
        # Legacy meta-labeled dict (pre-C1 format).
        primary = obj["primary"]
        meta = obj.get("meta")
        label_map = obj.get("label_map")
        meta_cfg = obj.get("meta_label_config")
        if meta is not None:
            kind = BundleKind.META_LABELED
            members = {
                "primary": BundleMember(
                    filename=_PRIMARY_FILENAME,
                    sha256=_sha256_bytes(pickle.dumps(primary, protocol=pickle.HIGHEST_PROTOCOL)),
                    size_bytes=len(raw),
                    role="primary",
                ),
                "meta": BundleMember(
                    filename=_META_FILENAME,
                    sha256=_sha256_bytes(pickle.dumps(meta, protocol=pickle.HIGHEST_PROTOCOL)),
                    size_bytes=len(raw),
                    role="meta",
                ),
            }
        else:
            kind = BundleKind.SINGLE
            members = {
                "primary": BundleMember(
                    filename=_PRIMARY_FILENAME,
                    sha256=bundle_sha256,
                    size_bytes=len(raw),
                    role="primary",
                ),
            }
        manifest = BundleManifest(
            bundle_kind=kind,
            loader_version="legacy-pickle",
            model_family="unknown",
            feature_names=[],
            feature_schema_hash="",
            label_schema_hash="",
            members=members,
            created_at_ns=0,
            label_map=label_map if isinstance(label_map, dict) else None,
            meta_label_config=meta_cfg if isinstance(meta_cfg, dict) else None,
        )
        return ModelBundle(manifest, primary, meta, bundle_sha256)

    # Bare single model.
    manifest = BundleManifest(
        bundle_kind=BundleKind.SINGLE,
        loader_version="legacy-pickle",
        model_family="unknown",
        feature_names=[],
        feature_schema_hash="",
        label_schema_hash="",
        members={
            "primary": BundleMember(
                filename=_PRIMARY_FILENAME,
                sha256=bundle_sha256,
                size_bytes=len(raw),
                role="primary",
            ),
        },
        created_at_ns=0,
    )
    return ModelBundle(manifest, obj, None, bundle_sha256)


def _load_zip_bundle(raw: bytes, bundle_sha256: str) -> ModelBundle:
    """Load a ModelBundle v1 zip archive."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise BundleLoadError(f"not a valid zip archive: {exc}") from exc

    with zf:
        names = set(zf.namelist())
        if _MANIFEST_FILENAME not in names:
            raise BundleLoadError(
                f"bundle missing {_MANIFEST_FILENAME!r} (not a ModelBundle v1 archive)"
            )

        manifest_bytes = zf.read(_MANIFEST_FILENAME)
        try:
            manifest = BundleManifest.model_validate_json(manifest_bytes)
        except Exception as exc:
            raise BundleLoadError(f"invalid bundle_manifest.json: {exc}") from exc

        # Validate bundle_kind.
        if manifest.bundle_kind not in (BundleKind.SINGLE, BundleKind.META_LABELED):
            raise BundleLoadError(
                f"unknown bundle_kind: {manifest.bundle_kind!r} (allowed: single, meta_labeled)"
            )

        # Verify every declared member exists and its sha256 matches.
        loaded_members: dict[str, Any] = {}
        for member_name, member in manifest.members.items():
            if member.filename not in names:
                raise BundleLoadError(
                    f"bundle member {member_name!r} ({member.filename!r}) "
                    "is declared in manifest but missing from archive"
                )
            member_bytes = zf.read(member.filename)
            actual_sha = _sha256_bytes(member_bytes)
            if actual_sha != member.sha256:
                raise BundleLoadError(
                    f"bundle member {member_name!r} sha256 mismatch: "
                    f"manifest declares {member.sha256} but actual is {actual_sha}"
                )
            if len(member_bytes) != member.size_bytes:
                raise BundleLoadError(
                    f"bundle member {member_name!r} size mismatch: "
                    f"manifest declares {member.size_bytes} but actual is {len(member_bytes)}"
                )
            # Unpickle the member.
            try:
                loaded_members[member_name] = pickle.loads(member_bytes)  # noqa: S301
            except Exception as exc:
                raise BundleLoadError(f"failed to unpickle member {member_name!r}: {exc}") from exc

        # Validate member presence per bundle_kind.
        if "primary" not in loaded_members:
            raise BundleLoadError("bundle has no primary member")
        primary_model = loaded_members["primary"]

        meta_model: Any | None = None
        if manifest.bundle_kind == BundleKind.META_LABELED:
            if "meta" not in loaded_members:
                raise BundleLoadError(
                    "meta_labeled bundle is missing the meta member (fail closed)"
                )
            meta_model = loaded_members["meta"]
        else:
            # single bundle must not have a meta member.
            if "meta" in loaded_members:
                raise BundleLoadError("single bundle has an unexpected meta member")

    return ModelBundle(manifest, primary_model, meta_model, bundle_sha256)


# ---------------------------------------------------------------------------
# Score
# ---------------------------------------------------------------------------


def _coerce_probability(raw: Any) -> float:
    """Coerce a raw model output into a scalar probability [0, 1].

    Handles:
    - scalar → float(raw)
    - 1-element sequence → float(seq[0])
    - 2-element sequence (binary proba) → p(class=1) = float(seq[1])
    - N-element sequence (multiclass proba) → max probability
    """
    if isinstance(raw, (int, float)):
        return float(raw)
    try:
        seq = list(raw)
    except TypeError:
        return 0.0
    if not seq:
        return 0.0
    if len(seq) == 2:
        return float(seq[1])
    if len(seq) > 2:
        return float(max(seq))
    return float(seq[0])


def _primary_direction(raw: Any, label_map: dict[str, int] | None) -> int:
    """Compute the directional signal from primary model output.

    For binary: +1 if p > 0.5, else -1.
    For multiclass: argmax → reverse label_map → original label value
        (-1, 0, +1 for triple-barrier).
    """
    if isinstance(raw, (int, float)):
        return 1 if float(raw) > 0.5 else -1
    try:
        seq = list(raw)
    except TypeError:
        return 0
    if not seq:
        return 0
    if len(seq) <= 2:
        # Binary: p(class=1) > 0.5 → +1, else -1.
        p = float(seq[-1]) if len(seq) == 2 else float(seq[0])
        return 1 if p > 0.5 else -1
    # Multiclass: argmax → reverse label map.
    pred_class = max(range(len(seq)), key=lambda i: seq[i])
    if label_map:
        inv = {v: k for k, v in label_map.items()}
        val = inv.get(int(pred_class), 0)
        try:
            return max(-1, min(1, int(val)))
        except (TypeError, ValueError):
            return 0
    # No label_map: clamp the raw class index to [-1, 1].
    return max(-1, min(1, int(pred_class)))


class BundleScorer:
    """Scores feature rows using a loaded ModelBundle.

    For ``single`` bundles:
        primary model → probability → Decision

    For ``meta_labeled`` bundles:
        primary model → primary probability/features → meta model →
        meta_p → abstention policy → Decision

    Invariant: ``abstained=True`` ⇒ ``act=False``.
    """

    def __init__(self, bundle: ModelBundle) -> None:
        self.bundle = bundle
        self._meta_threshold = _DEFAULT_META_THRESHOLD
        # Extract threshold from meta_label_config if available.
        if (
            bundle.manifest.meta_label_config
            and "abstain_threshold" in bundle.manifest.meta_label_config
        ):
            try:
                self._meta_threshold = float(bundle.manifest.meta_label_config["abstain_threshold"])
            except (TypeError, ValueError):
                pass

    @property
    def n_features(self) -> int:
        return len(self.bundle.manifest.feature_names)

    def verify_schema(self, n_features: int) -> None:
        """Verify the feature count matches the manifest.

        Raises SchemaMismatchError if the number of features does not
        match the manifest's feature_names length (when feature_names
        is non-empty — legacy bundles have empty feature_names and
        skip this check).
        """
        expected = self.n_features
        if expected > 0 and n_features != expected:
            raise SchemaMismatchError(
                f"feature schema mismatch: bundle expects {expected} features "
                f"but received {n_features}"
            )

    def predict(self, features: list[list[float]]) -> list[Any]:
        """Return raw primary model outputs (backward-compat with _Scorer).

        This does NOT apply the meta-model or abstention policy. Use
        ``score()`` for full Decision objects.
        """
        import numpy as np

        arr = np.asarray(features, dtype=np.float32)
        if arr.ndim == 1:
            arr = arr.reshape(1, -1)
        return list(self._primary_predict(arr))

    def _primary_predict(self, arr: Any) -> Any:
        """Run the primary model's predict, handling XGBoost DMatrix."""
        # XGBoost boosters require DMatrix input; LightGBM accepts numpy.
        # Detect by module path to avoid importing xgboost unconditionally.
        cls = type(self.bundle.primary_model)
        if cls.__module__.startswith("xgboost"):
            import xgboost as xgb

            return self.bundle.primary_model.predict(xgb.DMatrix(arr))
        return self.bundle.primary_model.predict(arr)

    def _meta_predict(self, arr: Any) -> Any:
        """Run the meta model's predict, handling XGBoost DMatrix."""
        assert self.bundle.meta_model is not None  # only called for META_LABELED bundles
        cls = type(self.bundle.meta_model)
        if cls.__module__.startswith("xgboost"):
            import xgboost as xgb

            return self.bundle.meta_model.predict(xgb.DMatrix(arr))
        return self.bundle.meta_model.predict(arr)

    def score(self, features: list[list[float]]) -> list[Decision]:
        """Score feature rows and return full Decision objects.

        Raises SchemaMismatchError if the feature count does not match
        the manifest (when feature_names is non-empty).
        """
        import numpy as np

        if not features:
            return []

        arr = np.asarray(features, dtype=np.float32)
        if arr.ndim == 1:
            arr = arr.reshape(1, -1)

        # Feature schema check (fails before scoring).
        self.verify_schema(arr.shape[1])

        # Primary model predictions.
        primary_out = self._primary_predict(arr)
        primary_list = list(primary_out)

        label_map = self.bundle.manifest.label_map

        if not self.bundle.is_meta_labeled:
            # single: primary → probability → Decision
            decisions: list[Decision] = []
            for raw in primary_list:
                p = _coerce_probability(raw)
                direction = _primary_direction(raw, label_map)
                decisions.append(
                    Decision(
                        p=p,
                        direction=direction,
                        act=True,
                        abstained=False,
                        meta_p=None,
                        bundle_sha256=self.bundle.bundle_sha256,
                        policy_version=self.bundle.manifest.policy_version,
                    )
                )
            return decisions

        # meta_labeled: primary → side → meta → abstention → Decision
        if self.bundle.meta_model is None:
            raise BundleLoadError("meta_labeled bundle has no meta model (fail closed)")

        # Compute sides from primary predictions.
        sides = np.array(
            [_primary_direction(raw, label_map) for raw in primary_list],
            dtype=np.float64,
        )

        # Augment features with the side signal.
        X_meta = np.column_stack([arr.astype(np.float64), sides.reshape(-1, 1)])

        meta_out = self._meta_predict(X_meta)
        meta_list = list(meta_out)

        decisions = []
        for raw_p, raw_m in zip(primary_list, meta_list, strict=True):
            p = _coerce_probability(raw_p)
            meta_p = _coerce_probability(raw_m)
            direction = _primary_direction(raw_p, label_map)
            abstained = meta_p < self._meta_threshold
            act = not abstained  # abstained=True ⇒ act=False
            decisions.append(
                Decision(
                    p=p,
                    direction=direction,
                    act=act,
                    abstained=abstained,
                    meta_p=meta_p,
                    bundle_sha256=self.bundle.bundle_sha256,
                    policy_version=self.bundle.manifest.policy_version,
                )
            )
        return decisions


# ---------------------------------------------------------------------------
# Selfcheck
# ---------------------------------------------------------------------------


def run_selfcheck(
    bundle_bytes: bytes,
    sample_features: list[list[float]],
) -> TrainingSelfCheck:
    """Run a selfcheck on the final serialized bundle bytes.

    Loads the bundle from bytes, scores the sample, and returns a
    TrainingSelfCheck. A selfcheck crash is caught and returned as a
    failed result (passed=False) — the caller should emit
    error_code="bundle_selfcheck_failed".

    The bundle_sha256 in the result is sha256(bundle_bytes), which must
    match the registered artifact sha256.

    Args:
        bundle_bytes: the final serialized artifact bytes.
        sample_features: a small sample of feature rows to score.

    Returns:
        TrainingSelfCheck with passed=True on success, passed=False on
        any failure (load error, scoring crash, schema mismatch).
    """
    start_ns = time.time_ns()
    try:
        bundle = load_bundle(bundle_bytes)
        scorer = BundleScorer(bundle)
        decisions = scorer.score(sample_features)
        n_rows = len(decisions)

        # Compute output sha256 from the serialized decisions.
        output_json = json.dumps(
            [d.model_dump() for d in decisions],
            sort_keys=True,
        ).encode("utf-8")
        output_sha = _sha256_bytes(output_json)

        duration_ms = (time.time_ns() - start_ns) / 1_000_000

        return TrainingSelfCheck(
            passed=True,
            n_rows_scored=n_rows,
            output_sha256=output_sha,
            bundle_sha256=bundle.bundle_sha256,
            loader_version=LOADER_VERSION,
            duration_ms=duration_ms,
        )
    except Exception as exc:
        duration_ms = (time.time_ns() - start_ns) / 1_000_000
        # A selfcheck crash is a selfcheck failure.
        bundle_sha = _sha256_bytes(bundle_bytes) if bundle_bytes else ""
        return TrainingSelfCheck(
            passed=False,
            n_rows_scored=0,
            output_sha256="",
            bundle_sha256=bundle_sha,
            loader_version=LOADER_VERSION,
            duration_ms=duration_ms,
            error_detail=str(exc),
        )
