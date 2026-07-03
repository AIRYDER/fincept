"""quant_foundry.oof_artifacts — Out-Of-Fold prediction artifacts (T-8.3).

This module writes and validates **out-of-fold (OOF) prediction artifacts**
for stacking / ensembling. An OOF artifact is the collection of predictions
a model makes on the *validation* rows of each fold — i.e. rows the model
never trained on. These predictions are the input features for a meta-learner
(stackers).

The flow is:

1. A trainer trains a model on each fold's train window and predicts on that
   fold's validation window. For every validation prediction it calls
   :meth:`OOFWriter.add_prediction` (or constructs :class:`OOFRow` objects
   directly).
2. :func:`write_oof_artifact` (or :meth:`OOFWriter.flush`) serializes the
   collected rows to a JSON file on disk and returns an :class:`OOFArtifact`
   carrying a deterministic SHA-256 ``artifact_hash``.
3. :func:`read_oof_artifact` reads the file back and verifies the stored
   hash matches the recomputed hash (fail-closed on tampering / corruption).
4. :func:`validate_oof_artifact` checks the artifact against a
   :class:`~quant_foundry.fold_consumer.FoldAssignment`:
   - the OOF row count equals the total number of validation rows,
   - no training-fold predictions leak into the meta-learner (every OOF
     row's ``fold_id`` is the fold where that row is a *validation* row),
   - every ``row_id`` in the artifact exists in the fold assignment.
5. :func:`merge_oof_artifacts` merges several model families' OOF
   predictions into a ``dict[row_id, list[predictions]]`` suitable as
   input to a meta-learner.

Design invariants (enforced from the skeleton onward):

- All Pydantic models are ``frozen=True`` and ``extra="forbid"`` (audit
  integrity — an OOF artifact is an immutable, tamper-evident record).
- The ``artifact_hash`` is a deterministic SHA-256 over the rows sorted by
  ``row_id`` (canonical JSON). Two identical sets of rows always produce the
  same hash; reordering rows does not change the hash.
- ``row_id`` is a deterministic key formed by joining the fold assignment's
  row-key tuple elements with ``"_"`` (see :func:`canonical_row_id`). The
  caller is responsible for using the same convention when building
  :class:`OOFRow` objects so that :func:`validate_oof_artifact` can match
  them against the fold assignment.

This module is file-disjoint from all active builders. It imports only
from :mod:`quant_foundry.fold_consumer` (for :class:`FoldAssignment` and
:func:`get_fold_data`) and the standard library.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from quant_foundry.fold_consumer import FoldAssignment, get_fold_data


# ---------------------------------------------------------------------------
# Row-id canonicalization
# ---------------------------------------------------------------------------


def canonical_row_id(row_key: tuple) -> str:
    """Build a canonical ``row_id`` string from a fold-assignment row key.

    The fold assignment stores each row's key as a tuple of the
    ``row_id_columns`` values (e.g. ``("AAPL", "2024-04-15", 5)``). This
    helper joins those elements with ``"_"`` after stringifying them,
    producing a deterministic key such as ``"AAPL_2024-04-15_5"``.

    Callers building :class:`OOFRow` objects should use the same convention
    (or :func:`make_row_id`) so that :func:`validate_oof_artifact` can match
    OOF rows against the fold assignment.

    Args:
        row_key: the row-key tuple from a :class:`FoldAssignment`.

    Returns:
        A deterministic ``row_id`` string.
    """
    return "_".join(str(part) for part in row_key)


def make_row_id(*parts: Any) -> str:
    """Build a deterministic ``row_id`` string from individual key parts.

    Convenience wrapper around :func:`canonical_row_id` for callers that
    have the key parts as separate arguments rather than a tuple.

    Args:
        *parts: the row-key parts (e.g. ``make_row_id("AAPL", "2024-04-15", 5)``).

    Returns:
        A deterministic ``row_id`` string.
    """
    return canonical_row_id(tuple(parts))


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class OOFRow(BaseModel):
    """A single out-of-fold prediction row.

    One :class:`OOFRow` records the prediction a model made on a single
    validation row of a single fold. The ``row_id`` is a deterministic key
    (e.g. ``"AAPL_2024-04-15_5"``) that identifies the row across model
    families so that :func:`merge_oof_artifacts` can align predictions.

    Fields:
        row_id: deterministic key identifying the row (e.g.
            ``symbol_decisiontime_horizon``).
        fold_id: the fold whose validation window this row belongs to.
        symbol: the instrument symbol.
        timestamp: ISO-format timestamp of the row's decision time.
        label: the ground-truth label for the row.
        prediction: the model's predicted value for the row.
        horizon: the prediction horizon (in bars/days).
        weight: the sample weight (default 1.0).
        model_family: the model family that produced the prediction
            (e.g. ``"lightgbm"``, ``"catboost"``, ``"xgboost"``).

    Frozen + ``extra='forbid'`` (audit integrity).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    row_id: str
    fold_id: int
    symbol: str
    timestamp: str
    label: float
    prediction: float
    horizon: int
    weight: float = 1.0
    model_family: str

    @field_validator("row_id", "symbol", "timestamp", "model_family")
    @classmethod
    def _nonempty_str(cls, v: str, info: Any) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError(
                f"{info.field_name} must be a non-empty string"
            )
        return v

    @field_validator("fold_id")
    @classmethod
    def _fold_id_nonnegative(cls, v: int) -> int:
        if v < 0:
            raise ValueError(f"fold_id must be >= 0; got {v}")
        return v

    @field_validator("horizon")
    @classmethod
    def _horizon_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError(f"horizon must be > 0; got {v}")
        return v

    @field_validator("weight")
    @classmethod
    def _weight_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError(f"weight must be > 0; got {v}")
        return v


class OOFArtifact(BaseModel):
    """An out-of-fold prediction artifact — a tamper-evident record.

    An :class:`OOFArtifact` bundles all the OOF predictions for a single
    model family together with metadata (hash, uri, timestamps) that make
    it auditable and verifiable.

    Fields:
        rows: the list of :class:`OOFRow` predictions.
        model_family: the model family that produced the rows.
        fold_count: the number of folds represented (>= 1).
        artifact_uri: the file path / URI where the artifact is stored.
        artifact_hash: deterministic SHA-256 of the rows
            (see :func:`compute_oof_hash`).
        created_at: ISO-format timestamp of artifact creation.
        row_count: the number of rows (must equal ``len(rows)``).

    Frozen + ``extra='forbid'`` (audit integrity).

    Validators:
        - ``row_count == len(rows)``
        - ``fold_count >= 1``
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    rows: list[OOFRow]
    model_family: str
    fold_count: int
    artifact_uri: str
    artifact_hash: str
    created_at: str
    row_count: int

    @field_validator("model_family", "artifact_uri", "created_at", "artifact_hash")
    @classmethod
    def _nonempty_str(cls, v: str, info: Any) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError(
                f"{info.field_name} must be a non-empty string"
            )
        return v

    @field_validator("fold_count")
    @classmethod
    def _fold_count_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"fold_count must be >= 1; got {v}")
        return v

    @field_validator("row_count")
    @classmethod
    def _row_count_nonnegative(cls, v: int) -> int:
        if v < 0:
            raise ValueError(f"row_count must be >= 0; got {v}")
        return v

    @model_validator(mode="after")
    def _check_row_count_matches(self) -> OOFArtifact:
        """row_count must equal len(rows)."""
        if self.row_count != len(self.rows):
            raise ValueError(
                f"row_count must equal len(rows); "
                f"got row_count={self.row_count}, len(rows)={len(self.rows)}"
            )
        return self

    @model_validator(mode="after")
    def _check_model_family_consistent(self) -> OOFArtifact:
        """Every row's model_family must match the artifact's model_family."""
        for i, row in enumerate(self.rows):
            if row.model_family != self.model_family:
                raise ValueError(
                    f"rows[{i}].model_family={row.model_family!r} does not "
                    f"match artifact.model_family={self.model_family!r}"
                )
        return self


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------


def compute_oof_hash(rows: list[OOFRow]) -> str:
    """Compute a deterministic SHA-256 hash over a list of OOF rows.

    The rows are sorted by ``row_id`` (and by ``fold_id`` as a tiebreaker)
    and serialized to a canonical JSON representation (sorted keys, compact
    separators) before hashing. Two identical sets of rows therefore
    produce the same hash regardless of insertion order, and any change to
    a row alters the hash.

    The hash is computed *only* over the row payloads — it does not include
    the artifact metadata (``artifact_uri``, ``created_at``, etc.) so that
    the same predictions always hash identically regardless of where/when
    they were written.

    Args:
        rows: the list of :class:`OOFRow` to hash.

    Returns:
        A 64-character lowercase hex SHA-256 digest.
    """
    sorted_rows = sorted(rows, key=lambda r: (r.row_id, r.fold_id))
    payload = [
        {
            "row_id": r.row_id,
            "fold_id": r.fold_id,
            "symbol": r.symbol,
            "timestamp": r.timestamp,
            "label": r.label,
            "prediction": r.prediction,
            "horizon": r.horizon,
            "weight": r.weight,
            "model_family": r.model_family,
        }
        for r in sorted_rows
    ]
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Write / read
# ---------------------------------------------------------------------------


def _count_folds(rows: list[OOFRow]) -> int:
    """Count the number of distinct fold_ids in ``rows``."""
    return len({r.fold_id for r in rows})


def write_oof_artifact(
    rows: list[OOFRow],
    model_family: str,
    output_path: str,
) -> OOFArtifact:
    """Write an OOF artifact to disk and return the artifact.

    Builds an :class:`OOFArtifact` from ``rows``, computes the deterministic
    ``artifact_hash``, writes the artifact to ``output_path`` as JSON, and
    returns the artifact. The parent directory is created if it does not
    exist.

    Args:
        rows: the list of :class:`OOFRow` to write.
        model_family: the model family that produced the rows. Must match
            every row's ``model_family``.
        output_path: the file path to write the JSON artifact to.

    Returns:
        The :class:`OOFArtifact` that was written.

    Raises:
        ValueError: if ``rows`` is empty, if ``model_family`` does not match
            the rows, or if the rows contain duplicate ``row_id`` values.
    """
    if not rows:
        raise ValueError("cannot write an OOF artifact with no rows")
    for i, row in enumerate(rows):
        if row.model_family != model_family:
            raise ValueError(
                f"rows[{i}].model_family={row.model_family!r} does not "
                f"match model_family={model_family!r}"
            )
    # Detect duplicate row_ids (each validation row should appear once).
    seen: set[str] = set()
    for row in rows:
        if row.row_id in seen:
            raise ValueError(
                f"duplicate row_id {row.row_id!r} in OOF rows — each "
                "validation row must appear exactly once per model family"
            )
        seen.add(row.row_id)

    artifact_hash = compute_oof_hash(rows)
    fold_count = _count_folds(rows)
    created_at = datetime.now(timezone.utc).isoformat()

    artifact = OOFArtifact(
        rows=rows,
        model_family=model_family,
        fold_count=fold_count,
        artifact_uri=output_path,
        artifact_hash=artifact_hash,
        created_at=created_at,
        row_count=len(rows),
    )

    # Ensure the parent directory exists.
    parent = os.path.dirname(os.path.abspath(output_path))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)

    # Serialize with canonical JSON (sorted keys) for reproducibility.
    payload = artifact.model_dump(mode="json")
    with open(output_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, sort_keys=True, indent=2)

    return artifact


def read_oof_artifact(path: str) -> OOFArtifact:
    """Read and verify an OOF artifact from disk.

    Reads the JSON file at ``path``, parses it into an :class:`OOFArtifact`,
    and verifies that the stored ``artifact_hash`` matches the hash
    recomputed from the rows (fail-closed on tampering / corruption).

    Args:
        path: the file path to read the artifact from.

    Returns:
        The verified :class:`OOFArtifact`.

    Raises:
        FileNotFoundError: if ``path`` does not exist.
        ValueError: if the file is empty, the JSON is invalid, the parsed
            object fails Pydantic validation, or the stored hash does not
            match the recomputed hash.
    """
    if not os.path.isfile(path):
        raise FileNotFoundError(f"OOF artifact file not found: {path!r}")
    if os.path.getsize(path) == 0:
        raise ValueError(f"OOF artifact file is empty (0 bytes): {path!r}")

    with open(path, "r", encoding="utf-8") as fh:
        try:
            payload = json.load(fh)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"OOF artifact at {path!r} is not valid JSON: {exc}"
            ) from exc

    artifact = OOFArtifact.model_validate(payload)

    # Fail-closed: verify the stored hash matches the recomputed hash.
    recomputed = compute_oof_hash(artifact.rows)
    if recomputed != artifact.artifact_hash:
        raise ValueError(
            f"OOF artifact hash mismatch at {path!r}: "
            f"stored={artifact.artifact_hash!r}, recomputed={recomputed!r} "
            "— artifact may have been tampered with or corrupted"
        )
    return artifact


# ---------------------------------------------------------------------------
# Validation against a fold assignment
# ---------------------------------------------------------------------------


def _build_fold_membership(
    assignment: FoldAssignment,
) -> tuple[dict[str, int], dict[str, set[int]], int]:
    """Build membership maps from a fold assignment.

    Returns a tuple ``(validation_map, train_map, total_validation_count)``
    where:

    - ``validation_map`` maps ``canonical_row_id -> fold_id`` for every row
      that is a *validation* row of some fold.
    - ``train_map`` maps ``canonical_row_id -> set(fold_id)`` for every row
      that is a *train* row of some fold (a row can be a train row for
      multiple folds in walk-forward schemes).
    - ``total_validation_count`` is the sum of validation row counts across
      all folds.
    """
    validation_map: dict[str, int] = {}
    train_map: dict[str, set[int]] = {}
    total_validation_count = 0

    for fw in assignment.fold_spec.folds:
        fold_id = fw.fold_id
        train_indices, validation_indices = get_fold_data(assignment, fold_id)
        total_validation_count += len(validation_indices)
        for i in validation_indices:
            row_id = canonical_row_id(assignment.row_keys[i])
            validation_map[row_id] = fold_id
        for i in train_indices:
            row_id = canonical_row_id(assignment.row_keys[i])
            train_map.setdefault(row_id, set()).add(fold_id)

    return validation_map, train_map, total_validation_count


def validate_oof_artifact(
    artifact: OOFArtifact,
    fold_assignment: FoldAssignment,
) -> bool:
    """Validate an OOF artifact against a fold assignment.

    Checks (all fail-closed — raise ``ValueError`` on failure):

    1. **Row count matches validation rows**: the artifact's ``row_count``
       equals the total number of validation rows across all folds.
    2. **No training-fold prediction leak**: every OOF row's ``fold_id`` is
       the fold where that row is a *validation* row (not a train row). A
       row that is a train row for fold ``f`` must never appear as an OOF
       prediction with ``fold_id == f``.
    3. **All row_ids exist in the fold assignment**: every ``row_id`` in
       the artifact must correspond to a row in the fold assignment.
    4. **No duplicate row_ids**: each validation row appears at most once.

    Args:
        artifact: the :class:`OOFArtifact` to validate.
        fold_assignment: the :class:`FoldAssignment` to validate against.

    Returns:
        True if the artifact is valid.

    Raises:
        ValueError: if any check fails.
    """
    validation_map, train_map, total_validation_count = _build_fold_membership(
        fold_assignment
    )

    # Check 1: row count matches total validation rows.
    if artifact.row_count != total_validation_count:
        raise ValueError(
            f"OOF row_count={artifact.row_count} does not match total "
            f"validation rows={total_validation_count}"
        )
    if len(artifact.rows) != total_validation_count:
        raise ValueError(
            f"len(OOF rows)={len(artifact.rows)} does not match total "
            f"validation rows={total_validation_count}"
        )

    seen: set[str] = set()
    for i, row in enumerate(artifact.rows):
        # Check 4: no duplicate row_ids.
        if row.row_id in seen:
            raise ValueError(
                f"duplicate row_id {row.row_id!r} in artifact (rows[{i}])"
            )
        seen.add(row.row_id)

        # Check 3: row_id exists in the fold assignment.
        if row.row_id not in validation_map:
            # It might be a train-only row (leak) or a completely unknown row.
            if row.row_id in train_map:
                train_folds = sorted(train_map[row.row_id])
                raise ValueError(
                    f"OOF rows[{i}].row_id={row.row_id!r} with "
                    f"fold_id={row.fold_id} is a TRAIN row for fold(s) "
                    f"{train_folds} — training-fold prediction leak detected"
                )
            raise ValueError(
                f"OOF rows[{i}].row_id={row.row_id!r} does not exist in "
                "the fold assignment"
            )

        # Check 2: the row's fold_id matches its validation fold.
        expected_fold = validation_map[row.row_id]
        if row.fold_id != expected_fold:
            # If the row is a train row for the declared fold_id, that's a leak.
            if row.row_id in train_map and row.fold_id in train_map[row.row_id]:
                raise ValueError(
                    f"OOF rows[{i}].row_id={row.row_id!r} fold_id="
                    f"{row.fold_id} is a TRAIN row for that fold — "
                    "training-fold prediction leak detected (expected "
                    f"validation fold {expected_fold})"
                )
            raise ValueError(
                f"OOF rows[{i}].row_id={row.row_id!r} fold_id={row.fold_id} "
                f"does not match its validation fold {expected_fold}"
            )

    return True


# ---------------------------------------------------------------------------
# Merging for stacking
# ---------------------------------------------------------------------------


def merge_oof_artifacts(
    artifacts: list[OOFArtifact],
) -> dict[str, list[float]]:
    """Merge multiple model families' OOF predictions for stacking.

    Aligns the OOF predictions of several model families by ``row_id`` and
    returns a mapping ``row_id -> list[predictions]`` where the list has one
    prediction per model family (in the same order as ``artifacts``). This
    is the input matrix for a meta-learner (stacker).

    All artifacts must cover the *same* set of ``row_id`` values — a
    mismatch indicates that one model family is missing predictions for
    some validation rows, which would corrupt the stacker's input.

    Args:
        artifacts: the list of :class:`OOFArtifact` to merge (one per model
            family).

    Returns:
        A dict mapping ``row_id -> list[predictions]`` (one prediction per
        artifact, in input order).

    Raises:
        ValueError: if ``artifacts`` is empty, or if the artifacts do not
            all cover the same set of ``row_id`` values.
    """
    if not artifacts:
        raise ValueError("cannot merge an empty list of OOF artifacts")

    # Collect the row_id set for each artifact.
    row_id_sets: list[set[str]] = []
    for art in artifacts:
        ids = {r.row_id for r in art.rows}
        if len(ids) != len(art.rows):
            raise ValueError(
                f"OOF artifact for model_family={art.model_family!r} "
                "contains duplicate row_ids — cannot merge"
            )
        row_id_sets.append(ids)

    # All artifacts must cover the same row_ids.
    baseline_ids = row_id_sets[0]
    for i, ids in enumerate(row_id_sets[1:], start=1):
        if ids != baseline_ids:
            missing = baseline_ids - ids
            extra = ids - baseline_ids
            raise ValueError(
                f"OOF artifact {i} (model_family={artifacts[i].model_family!r}) "
                f"does not cover the same row_ids as artifact 0 "
                f"(model_family={artifacts[0].model_family!r}); "
                f"missing={sorted(missing)!r}, extra={sorted(extra)!r}"
            )

    # Build row_id -> [prediction per model].
    per_model: list[dict[str, float]] = []
    for art in artifacts:
        per_model.append({r.row_id: r.prediction for r in art.rows})

    merged: dict[str, list[float]] = {}
    for row_id in sorted(baseline_ids):
        merged[row_id] = [pm[row_id] for pm in per_model]

    return merged


# ---------------------------------------------------------------------------
# OOFWriter — convenience collector
# ---------------------------------------------------------------------------


class OOFWriter:
    """Convenience collector for OOF predictions.

    A :class:`OOFWriter` accumulates OOF predictions for a single model
    family and writes them to disk as an :class:`OOFArtifact` on
    :meth:`flush`. This is the typical interface a trainer uses:

    .. code-block:: python

        writer = OOFWriter(model_family="lightgbm", output_dir="/tmp/oof")
        for fold_id in range(n_folds):
            train_idx, val_idx = get_fold_data(assignment, fold_id)
            model = train(X.iloc[train_idx], y.iloc[train_idx])
            preds = model.predict(X.iloc[val_idx])
            for i, pred in zip(val_idx, preds):
                writer.add_prediction(
                    row_id=canonical_row_id(assignment.row_keys[i]),
                    fold_id=fold_id,
                    symbol=...,
                    timestamp=...,
                    label=float(y.iloc[i]),
                    prediction=float(pred),
                    horizon=5,
                )
        artifact = writer.flush()

    The output file is named ``oof_<model_family>.json`` inside
    ``output_dir``.
    """

    def __init__(self, model_family: str, output_dir: str) -> None:
        """Initialize the writer.

        Args:
            model_family: the model family that will produce the
                predictions (e.g. ``"lightgbm"``).
            output_dir: the directory where the artifact will be written
                on :meth:`flush`. Created if it does not exist.
        """
        if not isinstance(model_family, str) or not model_family.strip():
            raise ValueError("model_family must be a non-empty string")
        if not isinstance(output_dir, str) or not output_dir.strip():
            raise ValueError("output_dir must be a non-empty string")
        self.model_family: str = model_family
        self.output_dir: str = output_dir
        self._rows: list[OOFRow] = []
        self._seen: set[str] = set()

    def add_prediction(
        self,
        row_id: str,
        fold_id: int,
        symbol: str,
        timestamp: str,
        label: float,
        prediction: float,
        horizon: int,
        weight: float = 1.0,
    ) -> None:
        """Add an OOF prediction row.

        Args:
            row_id: deterministic key identifying the row.
            fold_id: the fold whose validation window this row belongs to.
            symbol: the instrument symbol.
            timestamp: ISO-format timestamp of the row's decision time.
            label: the ground-truth label.
            prediction: the model's predicted value.
            horizon: the prediction horizon.
            weight: the sample weight (default 1.0).

        Raises:
            ValueError: if ``row_id`` has already been added (duplicate).
        """
        if row_id in self._seen:
            raise ValueError(
                f"duplicate row_id {row_id!r} — each validation row must "
                "be added exactly once"
            )
        self._seen.add(row_id)
        self._rows.append(
            OOFRow(
                row_id=row_id,
                fold_id=fold_id,
                symbol=symbol,
                timestamp=timestamp,
                label=label,
                prediction=prediction,
                horizon=horizon,
                weight=weight,
                model_family=self.model_family,
            )
        )

    def flush(self) -> OOFArtifact:
        """Write all collected rows to disk and return the artifact.

        The artifact is written to ``<output_dir>/oof_<model_family>.json``.

        Raises:
            ValueError: if no predictions have been added.
        """
        if not self._rows:
            raise ValueError(
                f"cannot flush OOFWriter for model_family={self.model_family!r} "
                "— no predictions have been added"
            )
        os.makedirs(self.output_dir, exist_ok=True)
        output_path = os.path.join(
            self.output_dir, f"oof_{self.model_family}.json"
        )
        return write_oof_artifact(
            rows=list(self._rows),
            model_family=self.model_family,
            output_path=output_path,
        )

    def clear(self) -> None:
        """Reset the writer's internal state (drop all collected rows)."""
        self._rows = []
        self._seen = set()


__all__ = [
    "OOFRow",
    "OOFArtifact",
    "compute_oof_hash",
    "write_oof_artifact",
    "read_oof_artifact",
    "validate_oof_artifact",
    "merge_oof_artifacts",
    "OOFWriter",
    "canonical_row_id",
    "make_row_id",
]
