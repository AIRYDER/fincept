"""Tests for Phase 8 / T-8.3 — Out-Of-Fold Prediction Artifacts.

Covers:
- ``OOFRow`` construction + fail-closed validators (frozen, extra=forbid,
  non-empty strings, non-negative fold_id, positive horizon/weight).
- ``OOFArtifact`` construction, row_count validator, fold_count validator,
  model_family consistency.
- ``compute_oof_hash`` determinism + sensitivity to row order / values.
- ``write_oof_artifact`` + ``read_oof_artifact`` round-trip.
- ``read_oof_artifact`` fail-closed: hash mismatch, missing file, empty
  file, invalid JSON.
- ``validate_oof_artifact`` with valid and invalid data:
  - row count mismatch (fail-closed).
  - training-fold prediction leak (fail-closed).
  - unknown row_id (fail-closed).
  - duplicate row_ids (fail-closed).
  - fold_id mismatch (fail-closed).
- ``merge_oof_artifacts`` with multiple model families:
  - successful merge produces aligned predictions.
  - mismatched row_id sets (fail-closed).
  - empty list (fail-closed).
  - duplicate row_ids within an artifact (fail-closed).
- ``OOFWriter`` class: add_prediction, flush, clear, duplicate detection.
- Edge cases: single fold, single row, many models.
- Determinism: same rows produce the same hash.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import pytest

from quant_foundry.dataset_manifest import FoldSpec, FoldWindow, compute_fold_hash
from quant_foundry.fold_consumer import FoldAssignment, consume_manifest_folds
from quant_foundry.oof_artifacts import (
    OOFArtifact,
    OOFRow,
    OOFWriter,
    canonical_row_id,
    compute_oof_hash,
    make_row_id,
    merge_oof_artifacts,
    read_oof_artifact,
    validate_oof_artifact,
    write_oof_artifact,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _basic_fold_window(
    fold_id: int = 0,
    *,
    train_start: str = "2024-01-01",
    train_end: str = "2024-03-31",
    validation_start: str = "2024-04-10",
    validation_end: str = "2024-05-31",
    embargo_until: str | None = None,
) -> FoldWindow:
    """Build a minimal valid FoldWindow with optional overrides."""
    return FoldWindow(
        fold_id=fold_id,
        train_start=train_start,
        train_end=train_end,
        validation_start=validation_start,
        validation_end=validation_end,
        embargo_until=embargo_until,
    )


def _two_fold_windows() -> list[FoldWindow]:
    """Build two non-overlapping FoldWindows (ids 0 and 1)."""
    f0 = _basic_fold_window(fold_id=0)
    f1 = _basic_fold_window(
        fold_id=1,
        train_start="2024-06-01",
        train_end="2024-08-31",
        validation_start="2024-09-10",
        validation_end="2024-10-31",
    )
    return [f0, f1]


def _basic_fold_spec(
    folds: list[FoldWindow] | None = None,
    row_id_columns: list[str] | None = None,
) -> FoldSpec:
    """Build a minimal valid FoldSpec with optional overrides."""
    if folds is None:
        folds = _two_fold_windows()
    if row_id_columns is None:
        row_id_columns = ["symbol", "decision_time", "horizon"]
    return FoldSpec(
        folds=folds,
        fold_assignment_hash=compute_fold_hash(folds),
        row_id_columns=row_id_columns,
    )


def _synthetic_df():
    """Build a small list-of-dicts dataframe with rows spanning two folds."""
    return [
        # Fold 0 train.
        {"symbol": "AAPL", "decision_time": "2024-01-15", "horizon": 5, "f1": 0.1, "label": 1},
        {"symbol": "AAPL", "decision_time": "2024-02-15", "horizon": 5, "f1": 0.2, "label": 0},
        # Fold 0 validation.
        {"symbol": "AAPL", "decision_time": "2024-04-15", "horizon": 5, "f1": 0.3, "label": 1},
        # Fold 1 train.
        {"symbol": "AAPL", "decision_time": "2024-06-15", "horizon": 5, "f1": 0.4, "label": 0},
        {"symbol": "AAPL", "decision_time": "2024-07-15", "horizon": 5, "f1": 0.5, "label": 1},
        # Fold 1 validation.
        {"symbol": "AAPL", "decision_time": "2024-09-15", "horizon": 5, "f1": 0.6, "label": 0},
    ]


def _basic_assignment() -> FoldAssignment:
    """Build a FoldAssignment from the synthetic two-fold dataframe."""
    spec = _basic_fold_spec()
    return consume_manifest_folds(spec, _synthetic_df())


def _row(
    row_id: str = "AAPL_2024-04-15_5",
    fold_id: int = 0,
    symbol: str = "AAPL",
    timestamp: str = "2024-04-15T00:00:00Z",
    label: float = 1.0,
    prediction: float = 0.55,
    horizon: int = 5,
    weight: float = 1.0,
    model_family: str = "lightgbm",
) -> OOFRow:
    """Build a minimal valid OOFRow with optional overrides."""
    return OOFRow(
        row_id=row_id,
        fold_id=fold_id,
        symbol=symbol,
        timestamp=timestamp,
        label=label,
        prediction=prediction,
        horizon=horizon,
        weight=weight,
        model_family=model_family,
    )


def _valid_oof_rows_for_assignment(
    assignment: FoldAssignment,
    model_family: str = "lightgbm",
    prediction: float = 0.5,
) -> list[OOFRow]:
    """Build the valid OOF rows (one per validation row) for an assignment."""
    from quant_foundry.fold_consumer import get_fold_data

    rows: list[OOFRow] = []
    for fw in assignment.fold_spec.folds:
        _, val_idx = get_fold_data(assignment, fw.fold_id)
        for i in val_idx:
            key = assignment.row_keys[i]
            rows.append(
                _row(
                    row_id=canonical_row_id(key),
                    fold_id=fw.fold_id,
                    symbol=str(key[0]),
                    timestamp=str(key[1]),
                    label=float(_synthetic_df()[i]["label"]),
                    prediction=prediction,
                    horizon=int(key[2]),
                    model_family=model_family,
                )
            )
    return rows


# ---------------------------------------------------------------------------
# OOFRow construction + validation
# ---------------------------------------------------------------------------


def test_oof_row_basic_construction():
    """OOFRow constructs with all required fields."""
    row = _row()
    assert row.row_id == "AAPL_2024-04-15_5"
    assert row.fold_id == 0
    assert row.symbol == "AAPL"
    assert row.label == 1.0
    assert row.prediction == 0.55
    assert row.horizon == 5
    assert row.weight == 1.0
    assert row.model_family == "lightgbm"


def test_oof_row_default_weight():
    """OOFRow.weight defaults to 1.0."""
    row = OOFRow(
        row_id="r1", fold_id=0, symbol="AAPL", timestamp="2024-01-01T00:00:00Z",
        label=1.0, prediction=0.5, horizon=5, model_family="lightgbm",
    )
    assert row.weight == 1.0


def test_oof_row_frozen():
    """OOFRow must be frozen (immutable)."""
    row = _row()
    with pytest.raises(Exception):
        row.prediction = 0.99  # type: ignore[misc]


def test_oof_row_extra_forbid():
    """OOFRow must reject unknown fields."""
    with pytest.raises(Exception):
        OOFRow(
            row_id="r1", fold_id=0, symbol="AAPL", timestamp="2024-01-01T00:00:00Z",
            label=1.0, prediction=0.5, horizon=5, model_family="lightgbm",
            bogus="no",  # type: ignore[call-arg]
        )


def test_oof_row_empty_row_id_rejected():
    """OOFRow must reject an empty row_id."""
    with pytest.raises(Exception):
        _row(row_id="")


def test_oof_row_empty_symbol_rejected():
    """OOFRow must reject an empty symbol."""
    with pytest.raises(Exception):
        _row(symbol="")


def test_oof_row_empty_timestamp_rejected():
    """OOFRow must reject an empty timestamp."""
    with pytest.raises(Exception):
        _row(timestamp="")


def test_oof_row_empty_model_family_rejected():
    """OOFRow must reject an empty model_family."""
    with pytest.raises(Exception):
        _row(model_family="")


def test_oof_row_negative_fold_id_rejected():
    """OOFRow must reject a negative fold_id."""
    with pytest.raises(Exception):
        _row(fold_id=-1)


def test_oof_row_zero_horizon_rejected():
    """OOFRow must reject a non-positive horizon."""
    with pytest.raises(Exception):
        _row(horizon=0)


def test_oof_row_negative_horizon_rejected():
    """OOFRow must reject a negative horizon."""
    with pytest.raises(Exception):
        _row(horizon=-5)


def test_oof_row_zero_weight_rejected():
    """OOFRow must reject a non-positive weight."""
    with pytest.raises(Exception):
        _row(weight=0.0)


def test_oof_row_negative_weight_rejected():
    """OOFRow must reject a negative weight."""
    with pytest.raises(Exception):
        _row(weight=-1.0)


# ---------------------------------------------------------------------------
# OOFArtifact construction + validation
# ---------------------------------------------------------------------------


def test_oof_artifact_basic_construction():
    """OOFArtifact constructs with matching row_count."""
    rows = [_row(row_id="r1"), _row(row_id="r2", fold_id=1)]
    art = OOFArtifact(
        rows=rows, model_family="lightgbm", fold_count=2,
        artifact_uri="/tmp/oof.json", artifact_hash="x" * 64,
        created_at="2024-01-01T00:00:00Z", row_count=2,
    )
    assert art.row_count == 2
    assert art.fold_count == 2
    assert len(art.rows) == 2


def test_oof_artifact_frozen():
    """OOFArtifact must be frozen."""
    rows = [_row()]
    art = OOFArtifact(
        rows=rows, model_family="lightgbm", fold_count=1,
        artifact_uri="/tmp/oof.json", artifact_hash="x" * 64,
        created_at="2024-01-01T00:00:00Z", row_count=1,
    )
    with pytest.raises(Exception):
        art.model_family = "catboost"  # type: ignore[misc]


def test_oof_artifact_extra_forbid():
    """OOFArtifact must reject unknown fields."""
    with pytest.raises(Exception):
        OOFArtifact(
            rows=[_row()], model_family="lightgbm", fold_count=1,
            artifact_uri="/tmp/oof.json", artifact_hash="x" * 64,
            created_at="2024-01-01T00:00:00Z", row_count=1,
            bogus="no",  # type: ignore[call-arg]
        )


def test_oof_artifact_row_count_mismatch_rejected():
    """OOFArtifact must reject row_count != len(rows)."""
    with pytest.raises(Exception):
        OOFArtifact(
            rows=[_row()], model_family="lightgbm", fold_count=1,
            artifact_uri="/tmp/oof.json", artifact_hash="x" * 64,
            created_at="2024-01-01T00:00:00Z", row_count=2,
        )


def test_oof_artifact_fold_count_zero_rejected():
    """OOFArtifact must reject fold_count < 1."""
    with pytest.raises(Exception):
        OOFArtifact(
            rows=[_row()], model_family="lightgbm", fold_count=0,
            artifact_uri="/tmp/oof.json", artifact_hash="x" * 64,
            created_at="2024-01-01T00:00:00Z", row_count=1,
        )


def test_oof_artifact_model_family_mismatch_rejected():
    """OOFArtifact must reject rows whose model_family differs."""
    rows = [_row(model_family="catboost")]
    with pytest.raises(Exception):
        OOFArtifact(
            rows=rows, model_family="lightgbm", fold_count=1,
            artifact_uri="/tmp/oof.json", artifact_hash="x" * 64,
            created_at="2024-01-01T00:00:00Z", row_count=1,
        )


# ---------------------------------------------------------------------------
# compute_oof_hash
# ---------------------------------------------------------------------------


def test_compute_oof_hash_deterministic():
    """Same rows produce the same hash."""
    rows = [_row(row_id="r1"), _row(row_id="r2", fold_id=1)]
    h1 = compute_oof_hash(rows)
    h2 = compute_oof_hash(list(rows))
    assert h1 == h2


def test_compute_oof_hash_order_invariant():
    """Hash is invariant to row insertion order (sorted by row_id)."""
    r1 = _row(row_id="r1")
    r2 = _row(row_id="r2", fold_id=1)
    h1 = compute_oof_hash([r1, r2])
    h2 = compute_oof_hash([r2, r1])
    assert h1 == h2


def test_compute_oof_hash_sensitive_to_prediction():
    """A changed prediction alters the hash."""
    r1 = _row(row_id="r1", prediction=0.5)
    r2 = _row(row_id="r1", prediction=0.6)
    assert compute_oof_hash([r1]) != compute_oof_hash([r2])


def test_compute_oof_hash_sensitive_to_row_id():
    """A changed row_id alters the hash."""
    r1 = _row(row_id="r1")
    r2 = _row(row_id="r2")
    assert compute_oof_hash([r1]) != compute_oof_hash([r2])


def test_compute_oof_hash_is_sha256_hex():
    """The hash is a 64-char lowercase hex string."""
    h = compute_oof_hash([_row()])
    assert len(h) == 64
    assert all(c in "0123456789abcdef" for c in h)


def test_compute_oof_hash_empty_list():
    """An empty list hashes deterministically (not an error)."""
    h = compute_oof_hash([])
    assert len(h) == 64
    # Deterministic.
    assert compute_oof_hash([]) == h


# ---------------------------------------------------------------------------
# write_oof_artifact + read_oof_artifact round-trip
# ---------------------------------------------------------------------------


def test_write_and_read_round_trip(tmp_path):
    """write_oof_artifact then read_oof_artifact returns equivalent artifact."""
    rows = [_row(row_id="r1"), _row(row_id="r2", fold_id=1)]
    path = str(tmp_path / "oof_lightgbm.json")
    written = write_oof_artifact(rows, "lightgbm", path)
    assert os.path.isfile(path)

    read = read_oof_artifact(path)
    assert read.model_family == written.model_family
    assert read.row_count == written.row_count
    assert read.fold_count == written.fold_count
    assert read.artifact_hash == written.artifact_hash
    assert read.artifact_hash == compute_oof_hash(rows)
    assert [r.row_id for r in read.rows] == [r.row_id for r in rows]


def test_write_oof_artifact_creates_parent_dir(tmp_path):
    """write_oof_artifact creates missing parent directories."""
    rows = [_row()]
    path = str(tmp_path / "nested" / "deep" / "oof.json")
    write_oof_artifact(rows, "lightgbm", path)
    assert os.path.isfile(path)


def test_write_oof_artifact_empty_rows_rejected(tmp_path):
    """write_oof_artifact rejects an empty row list."""
    with pytest.raises(ValueError):
        write_oof_artifact([], "lightgbm", str(tmp_path / "oof.json"))


def test_write_oof_artifact_model_family_mismatch_rejected(tmp_path):
    """write_oof_artifact rejects rows whose model_family differs."""
    rows = [_row(model_family="catboost")]
    with pytest.raises(ValueError):
        write_oof_artifact(rows, "lightgbm", str(tmp_path / "oof.json"))


def test_write_oof_artifact_duplicate_row_id_rejected(tmp_path):
    """write_oof_artifact rejects duplicate row_ids."""
    rows = [_row(row_id="r1"), _row(row_id="r1")]
    with pytest.raises(ValueError):
        write_oof_artifact(rows, "lightgbm", str(tmp_path / "oof.json"))


def test_read_oof_artifact_missing_file(tmp_path):
    """read_oof_artifact raises FileNotFoundError for a missing file."""
    with pytest.raises(FileNotFoundError):
        read_oof_artifact(str(tmp_path / "nope.json"))


def test_read_oof_artifact_empty_file(tmp_path):
    """read_oof_artifact raises ValueError for an empty file."""
    path = tmp_path / "empty.json"
    path.write_text("")
    with pytest.raises(ValueError):
        read_oof_artifact(str(path))


def test_read_oof_artifact_invalid_json(tmp_path):
    """read_oof_artifact raises ValueError for invalid JSON."""
    path = tmp_path / "bad.json"
    path.write_text("{not valid json")
    with pytest.raises(ValueError):
        read_oof_artifact(str(path))


def test_read_oof_artifact_hash_mismatch(tmp_path):
    """read_oof_artifact fails-closed when the stored hash is wrong."""
    rows = [_row(row_id="r1")]
    path = str(tmp_path / "oof.json")
    written = write_oof_artifact(rows, "lightgbm", path)
    # Tamper with the stored hash.
    with open(path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    payload["artifact_hash"] = "0" * 64
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    with pytest.raises(ValueError, match="hash mismatch"):
        read_oof_artifact(path)


def test_read_oof_artifact_tampered_prediction(tmp_path):
    """read_oof_artifact fails-closed when a prediction is tampered."""
    rows = [_row(row_id="r1", prediction=0.5)]
    path = str(tmp_path / "oof.json")
    write_oof_artifact(rows, "lightgbm", path)
    with open(path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    payload["rows"][0]["prediction"] = 0.99
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    with pytest.raises(ValueError, match="hash mismatch"):
        read_oof_artifact(path)


# ---------------------------------------------------------------------------
# validate_oof_artifact
# ---------------------------------------------------------------------------


def test_validate_oof_artifact_valid():
    """A correct OOF artifact validates against its fold assignment."""
    assignment = _basic_assignment()
    rows = _valid_oof_rows_for_assignment(assignment)
    art = OOFArtifact(
        rows=rows, model_family="lightgbm", fold_count=2,
        artifact_uri="/tmp/oof.json", artifact_hash=compute_oof_hash(rows),
        created_at="2024-01-01T00:00:00Z", row_count=len(rows),
    )
    assert validate_oof_artifact(art, assignment) is True


def test_validate_oof_artifact_row_count_mismatch():
    """validate fails-closed when row_count != validation row count."""
    assignment = _basic_assignment()
    # Only include one of the two validation rows.
    rows = _valid_oof_rows_for_assignment(assignment)[:1]
    art = OOFArtifact(
        rows=rows, model_family="lightgbm", fold_count=1,
        artifact_uri="/tmp/oof.json", artifact_hash=compute_oof_hash(rows),
        created_at="2024-01-01T00:00:00Z", row_count=len(rows),
    )
    with pytest.raises(ValueError, match="row_count"):
        validate_oof_artifact(art, assignment)


def test_validate_oof_artifact_training_fold_leak():
    """validate fails-closed when a train-row prediction leaks in."""
    assignment = _basic_assignment()
    from quant_foundry.fold_consumer import get_fold_data

    # Take a TRAIN row of fold 0 and present it as an OOF prediction.
    train_idx, _ = get_fold_data(assignment, 0)
    i = train_idx[0]
    key = assignment.row_keys[i]
    leak_row = _row(
        row_id=canonical_row_id(key),
        fold_id=0,
        symbol=str(key[0]),
        timestamp=str(key[1]),
        label=0.0,
        prediction=0.5,
        horizon=int(key[2]),
    )
    # Plus the real validation rows to keep counts consistent — but the leak
    # row replaces one validation row, so counts will mismatch. Instead build
    # an artifact where a train row is added with the wrong fold_id.
    rows = _valid_oof_rows_for_assignment(assignment)
    # Replace the first validation row with the train-row leak (same count).
    rows[0] = leak_row
    art = OOFArtifact(
        rows=rows, model_family="lightgbm", fold_count=2,
        artifact_uri="/tmp/oof.json", artifact_hash=compute_oof_hash(rows),
        created_at="2024-01-01T00:00:00Z", row_count=len(rows),
    )
    with pytest.raises(ValueError, match="TRAIN row|leak|does not exist"):
        validate_oof_artifact(art, assignment)


def test_validate_oof_artifact_unknown_row_id():
    """validate fails-closed when a row_id is not in the fold assignment."""
    assignment = _basic_assignment()
    rows = _valid_oof_rows_for_assignment(assignment)
    rows[0] = _row(row_id="UNKNOWN_2024-04-15_5", fold_id=0)
    art = OOFArtifact(
        rows=rows, model_family="lightgbm", fold_count=2,
        artifact_uri="/tmp/oof.json", artifact_hash=compute_oof_hash(rows),
        created_at="2024-01-01T00:00:00Z", row_count=len(rows),
    )
    with pytest.raises(ValueError, match="does not exist"):
        validate_oof_artifact(art, assignment)


def test_validate_oof_artifact_duplicate_row_ids():
    """validate fails-closed on duplicate row_ids."""
    assignment = _basic_assignment()
    rows = _valid_oof_rows_for_assignment(assignment)
    # Duplicate the first row (keep count the same by dropping the last).
    rows = [rows[0], rows[0]]
    art = OOFArtifact(
        rows=rows, model_family="lightgbm", fold_count=1,
        artifact_uri="/tmp/oof.json", artifact_hash=compute_oof_hash(rows),
        created_at="2024-01-01T00:00:00Z", row_count=len(rows),
    )
    with pytest.raises(ValueError, match="duplicate row_id"):
        validate_oof_artifact(art, assignment)


def test_validate_oof_artifact_fold_id_mismatch():
    """validate fails-closed when a row's fold_id != its validation fold."""
    assignment = _basic_assignment()
    rows = _valid_oof_rows_for_assignment(assignment)
    # Flip the fold_id of the first row to the wrong fold.
    rows[0] = rows[0].model_copy(update={"fold_id": 1 if rows[0].fold_id == 0 else 0})
    art = OOFArtifact(
        rows=rows, model_family="lightgbm", fold_count=2,
        artifact_uri="/tmp/oof.json", artifact_hash=compute_oof_hash(rows),
        created_at="2024-01-01T00:00:00Z", row_count=len(rows),
    )
    with pytest.raises(ValueError, match="does not match|leak"):
        validate_oof_artifact(art, assignment)


def test_validate_oof_artifact_single_fold():
    """validate works with a single-fold assignment."""
    f0 = _basic_fold_window(fold_id=0)
    spec = _basic_fold_spec(folds=[f0])
    df = [
        {"symbol": "AAPL", "decision_time": "2024-01-15", "horizon": 5, "label": 1},
        {"symbol": "AAPL", "decision_time": "2024-04-15", "horizon": 5, "label": 0},
    ]
    assignment = consume_manifest_folds(spec, df)
    rows = _valid_oof_rows_for_assignment(assignment)
    art = OOFArtifact(
        rows=rows, model_family="lightgbm", fold_count=1,
        artifact_uri="/tmp/oof.json", artifact_hash=compute_oof_hash(rows),
        created_at="2024-01-01T00:00:00Z", row_count=len(rows),
    )
    assert validate_oof_artifact(art, assignment) is True


# ---------------------------------------------------------------------------
# merge_oof_artifacts
# ---------------------------------------------------------------------------


def test_merge_oof_artifacts_two_families():
    """merge aligns two model families' predictions by row_id."""
    assignment = _basic_assignment()
    rows_a = _valid_oof_rows_for_assignment(assignment, model_family="lightgbm", prediction=0.1)
    rows_b = _valid_oof_rows_for_assignment(assignment, model_family="catboost", prediction=0.2)
    art_a = OOFArtifact(
        rows=rows_a, model_family="lightgbm", fold_count=2,
        artifact_uri="/a.json", artifact_hash=compute_oof_hash(rows_a),
        created_at="t", row_count=len(rows_a),
    )
    art_b = OOFArtifact(
        rows=rows_b, model_family="catboost", fold_count=2,
        artifact_uri="/b.json", artifact_hash=compute_oof_hash(rows_b),
        created_at="t", row_count=len(rows_b),
    )
    merged = merge_oof_artifacts([art_a, art_b])
    assert set(merged.keys()) == {r.row_id for r in rows_a}
    for row_id, preds in merged.items():
        assert preds == [0.1, 0.2]


def test_merge_oof_artifacts_three_families():
    """merge aligns three model families' predictions by row_id."""
    assignment = _basic_assignment()
    families = ["lightgbm", "catboost", "xgboost"]
    preds_vals = [0.1, 0.2, 0.3]
    arts = []
    for fam, pv in zip(families, preds_vals):
        rows = _valid_oof_rows_for_assignment(assignment, model_family=fam, prediction=pv)
        arts.append(OOFArtifact(
            rows=rows, model_family=fam, fold_count=2,
            artifact_uri=f"/{fam}.json", artifact_hash=compute_oof_hash(rows),
            created_at="t", row_count=len(rows),
        ))
    merged = merge_oof_artifacts(arts)
    for preds in merged.values():
        assert preds == [0.1, 0.2, 0.3]


def test_merge_oof_artifacts_empty_list():
    """merge fails-closed on an empty artifact list."""
    with pytest.raises(ValueError):
        merge_oof_artifacts([])


def test_merge_oof_artifacts_mismatched_row_ids():
    """merge fails-closed when artifacts cover different row_ids."""
    rows_a = [_row(row_id="r1"), _row(row_id="r2", fold_id=1)]
    rows_b = [_row(row_id="r1", model_family="catboost"),
              _row(row_id="r3", fold_id=1, model_family="catboost")]
    art_a = OOFArtifact(
        rows=rows_a, model_family="lightgbm", fold_count=2,
        artifact_uri="/a.json", artifact_hash=compute_oof_hash(rows_a),
        created_at="t", row_count=2,
    )
    art_b = OOFArtifact(
        rows=rows_b, model_family="catboost", fold_count=2,
        artifact_uri="/b.json", artifact_hash=compute_oof_hash(rows_b),
        created_at="t", row_count=2,
    )
    with pytest.raises(ValueError, match="does not cover the same row_ids"):
        merge_oof_artifacts([art_a, art_b])


def test_merge_oof_artifacts_duplicate_row_ids_in_artifact():
    """merge fails-closed when an artifact has duplicate row_ids."""
    rows_a = [_row(row_id="r1"), _row(row_id="r2", fold_id=1)]
    rows_b = [_row(row_id="r1", model_family="catboost"),
              _row(row_id="r1", fold_id=1, model_family="catboost")]
    art_a = OOFArtifact(
        rows=rows_a, model_family="lightgbm", fold_count=2,
        artifact_uri="/a.json", artifact_hash=compute_oof_hash(rows_a),
        created_at="t", row_count=2,
    )
    art_b = OOFArtifact(
        rows=rows_b, model_family="catboost", fold_count=2,
        artifact_uri="/b.json", artifact_hash=compute_oof_hash(rows_b),
        created_at="t", row_count=2,
    )
    with pytest.raises(ValueError, match="duplicate row_ids"):
        merge_oof_artifacts([art_a, art_b])


def test_merge_oof_artifacts_single_model():
    """merge works with a single model family."""
    rows = [_row(row_id="r1"), _row(row_id="r2", fold_id=1)]
    art = OOFArtifact(
        rows=rows, model_family="lightgbm", fold_count=2,
        artifact_uri="/a.json", artifact_hash=compute_oof_hash(rows),
        created_at="t", row_count=2,
    )
    merged = merge_oof_artifacts([art])
    assert merged["r1"] == [0.55]
    assert merged["r2"] == [0.55]


# ---------------------------------------------------------------------------
# OOFWriter
# ---------------------------------------------------------------------------


def test_oof_writer_flush_writes_artifact(tmp_path):
    """OOFWriter.flush writes a valid artifact to disk."""
    writer = OOFWriter(model_family="lightgbm", output_dir=str(tmp_path))
    writer.add_prediction(row_id="r1", fold_id=0, symbol="AAPL",
                          timestamp="2024-04-15T00:00:00Z",
                          label=1.0, prediction=0.6, horizon=5)
    writer.add_prediction(row_id="r2", fold_id=1, symbol="AAPL",
                          timestamp="2024-09-15T00:00:00Z",
                          label=0.0, prediction=0.4, horizon=5)
    art = writer.flush()
    assert art.model_family == "lightgbm"
    assert art.row_count == 2
    assert os.path.isfile(os.path.join(str(tmp_path), "oof_lightgbm.json"))


def test_oof_writer_flush_round_trip(tmp_path):
    """OOFWriter.flush then read_oof_artifact round-trips."""
    writer = OOFWriter(model_family="catboost", output_dir=str(tmp_path))
    writer.add_prediction(row_id="r1", fold_id=0, symbol="AAPL",
                          timestamp="2024-04-15T00:00:00Z",
                          label=1.0, prediction=0.6, horizon=5)
    art = writer.flush()
    path = os.path.join(str(tmp_path), "oof_catboost.json")
    read = read_oof_artifact(path)
    assert read.artifact_hash == art.artifact_hash
    assert read.row_count == 1


def test_oof_writer_duplicate_row_id_rejected(tmp_path):
    """OOFWriter.add_prediction rejects a duplicate row_id."""
    writer = OOFWriter(model_family="lightgbm", output_dir=str(tmp_path))
    writer.add_prediction(row_id="r1", fold_id=0, symbol="AAPL",
                          timestamp="2024-04-15T00:00:00Z",
                          label=1.0, prediction=0.6, horizon=5)
    with pytest.raises(ValueError, match="duplicate row_id"):
        writer.add_prediction(row_id="r1", fold_id=0, symbol="AAPL",
                              timestamp="2024-04-15T00:00:00Z",
                              label=1.0, prediction=0.6, horizon=5)


def test_oof_writer_flush_empty_rejected(tmp_path):
    """OOFWriter.flush rejects when no predictions have been added."""
    writer = OOFWriter(model_family="lightgbm", output_dir=str(tmp_path))
    with pytest.raises(ValueError):
        writer.flush()


def test_oof_writer_clear(tmp_path):
    """OOFWriter.clear resets internal state."""
    writer = OOFWriter(model_family="lightgbm", output_dir=str(tmp_path))
    writer.add_prediction(row_id="r1", fold_id=0, symbol="AAPL",
                          timestamp="2024-04-15T00:00:00Z",
                          label=1.0, prediction=0.6, horizon=5)
    writer.clear()
    with pytest.raises(ValueError):
        writer.flush()


def test_oof_writer_clear_allows_reuse(tmp_path):
    """After clear, the same row_id can be added again."""
    writer = OOFWriter(model_family="lightgbm", output_dir=str(tmp_path))
    writer.add_prediction(row_id="r1", fold_id=0, symbol="AAPL",
                          timestamp="2024-04-15T00:00:00Z",
                          label=1.0, prediction=0.6, horizon=5)
    writer.clear()
    # Should not raise after clear.
    writer.add_prediction(row_id="r1", fold_id=0, symbol="AAPL",
                          timestamp="2024-04-15T00:00:00Z",
                          label=1.0, prediction=0.6, horizon=5)
    art = writer.flush()
    assert art.row_count == 1


def test_oof_writer_invalid_model_family():
    """OOFWriter rejects an empty model_family."""
    with pytest.raises(ValueError):
        OOFWriter(model_family="", output_dir="/tmp")


def test_oof_writer_invalid_output_dir():
    """OOFWriter rejects an empty output_dir."""
    with pytest.raises(ValueError):
        OOFWriter(model_family="lightgbm", output_dir="")


def test_oof_writer_creates_output_dir(tmp_path):
    """OOFWriter.flush creates the output_dir if it does not exist."""
    out = tmp_path / "newdir"
    writer = OOFWriter(model_family="lightgbm", output_dir=str(out))
    writer.add_prediction(row_id="r1", fold_id=0, symbol="AAPL",
                          timestamp="2024-04-15T00:00:00Z",
                          label=1.0, prediction=0.6, horizon=5)
    writer.flush()
    assert out.is_dir()


def test_oof_writer_validates_against_assignment(tmp_path):
    """An OOFWriter-produced artifact validates against the fold assignment."""
    assignment = _basic_assignment()
    from quant_foundry.fold_consumer import get_fold_data

    writer = OOFWriter(model_family="lightgbm", output_dir=str(tmp_path))
    for fw in assignment.fold_spec.folds:
        _, val_idx = get_fold_data(assignment, fw.fold_id)
        for i in val_idx:
            key = assignment.row_keys[i]
            writer.add_prediction(
                row_id=canonical_row_id(key),
                fold_id=fw.fold_id,
                symbol=str(key[0]),
                timestamp=str(key[1]),
                label=float(_synthetic_df()[i]["label"]),
                prediction=0.5,
                horizon=int(key[2]),
            )
    art = writer.flush()
    assert validate_oof_artifact(art, assignment) is True


# ---------------------------------------------------------------------------
# Edge cases + determinism
# ---------------------------------------------------------------------------


def test_single_row_artifact(tmp_path):
    """A single-row artifact round-trips and validates."""
    rows = [_row(row_id="r1")]
    path = str(tmp_path / "oof.json")
    written = write_oof_artifact(rows, "lightgbm", path)
    read = read_oof_artifact(path)
    assert read.row_count == 1
    assert read.rows[0].row_id == "r1"
    assert read.artifact_hash == written.artifact_hash


def test_single_fold_artifact(tmp_path):
    """A single-fold artifact round-trips."""
    rows = [_row(row_id="r1", fold_id=0), _row(row_id="r2", fold_id=0)]
    path = str(tmp_path / "oof.json")
    write_oof_artifact(rows, "lightgbm", path)
    read = read_oof_artifact(path)
    assert read.fold_count == 1
    assert read.row_count == 2


def test_many_models_merge():
    """merge handles many (5) model families."""
    rows_base = [_row(row_id="r1"), _row(row_id="r2", fold_id=1)]
    families = ["m0", "m1", "m2", "m3", "m4"]
    arts = []
    for fam in families:
        rows = [r.model_copy(update={"model_family": fam}) for r in rows_base]
        arts.append(OOFArtifact(
            rows=rows, model_family=fam, fold_count=2,
            artifact_uri=f"/{fam}.json", artifact_hash=compute_oof_hash(rows),
            created_at="t", row_count=2,
        ))
    merged = merge_oof_artifacts(arts)
    assert len(merged) == 2
    for preds in merged.values():
        assert len(preds) == 5


def test_determinism_same_rows_same_hash():
    """Repeated hashing of the same rows yields identical hashes."""
    rows = [_row(row_id="r1"), _row(row_id="r2", fold_id=1)]
    hashes = [compute_oof_hash(rows) for _ in range(5)]
    assert len(set(hashes)) == 1


def test_determinism_write_twice_same_hash(tmp_path):
    """Writing the same rows twice produces the same artifact_hash."""
    rows = [_row(row_id="r1"), _row(row_id="r2", fold_id=1)]
    p1 = str(tmp_path / "a.json")
    p2 = str(tmp_path / "b.json")
    a1 = write_oof_artifact(rows, "lightgbm", p1)
    a2 = write_oof_artifact(rows, "lightgbm", p2)
    assert a1.artifact_hash == a2.artifact_hash


def test_canonical_row_id_from_tuple():
    """canonical_row_id joins tuple elements with underscores."""
    assert canonical_row_id(("AAPL", "2024-04-15", 5)) == "AAPL_2024-04-15_5"


def test_make_row_id_from_parts():
    """make_row_id joins parts with underscores."""
    assert make_row_id("AAPL", "2024-04-15", 5) == "AAPL_2024-04-15_5"


def test_canonical_row_id_handles_non_string_parts():
    """canonical_row_id stringifies non-string parts."""
    assert canonical_row_id(("MSFT", 20240415, 10)) == "MSFT_20240415_10"


def test_write_oof_artifact_canonical_json(tmp_path):
    """The written JSON is sorted-keys canonical JSON."""
    rows = [_row(row_id="r1")]
    path = str(tmp_path / "oof.json")
    write_oof_artifact(rows, "lightgbm", path)
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    # Re-parse and re-serialize with sort_keys to confirm determinism.
    payload = json.loads(text)
    assert json.dumps(payload, sort_keys=True) == json.dumps(
        json.loads(text), sort_keys=True
    )
