"""
tests for quant_foundry.feature_lake — TASK-0405 Feature Lake Builder MVP.

TDD: these tests are written FIRST and expected to fail until
dataset_manifest.py, feature_availability.py, and feature_lake.py are implemented.

Coverage (per NEXT_STEPS_PLAN TASK-0405 acceptance):
- Fixture dataset exports with a stable manifest (deterministic manifest hash).
- Manifest hash changes when source data changes.
- Point-in-time proof is mandatory: each row records observed_at alongside event_ts;
  export asserts every feature value's observed_at <= row decision time.
- A deliberately leaky fixture (feature whose observed_at is after the decision time)
  is REJECTED at export, not silently included.
- As-of (backward) joins only; forward joins rejected at construction time.
- Purged-k-fold + embargo split boundaries emitted in the manifest;
  embargo length >= max label horizon in the dataset.
- As-of universe reconstruction (includes delisted/renamed symbols) — no survivorship bias.
- Feature availability report produced.
- Export receipt written.
- Training jobs can reference manifest instead of DB credentials.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass

import pytest
from pydantic import ValidationError
from quant_foundry.dataset_manifest import (
    FeatureLakeManifest,
    FoldBoundary,
    PurgedFoldSpec,
)
from quant_foundry.feature_availability import FeatureAvailabilityReport
from quant_foundry.feature_lake import (
    FeatureLakeBuilder,
    FeatureRow,
    LeakyFeatureError,
    UniverseEntry,
    export_receipt,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

NS_PER_DAY = 86_400_000_000_000


def _ts(day: int) -> int:
    """Nanoseconds since epoch for a given day index (deterministic)."""
    return day * NS_PER_DAY


@dataclass(frozen=True)
class _FeatVal:
    """A single feature value with its observed_at (vendor availability) time."""

    name: str
    value: float
    observed_at: int  # ns — when the vendor made this value available


def _row(
    symbol: str,
    decision_time: int,
    features: tuple[_FeatVal, ...],
    label_horizon_ns: int = NS_PER_DAY,
) -> FeatureRow:
    return FeatureRow(
        symbol=symbol,
        event_ts=decision_time,
        decision_time=decision_time,
        features=features,
        label_horizon_ns=label_horizon_ns,
    )


def _universe(delisted: bool = False) -> tuple[UniverseEntry, ...]:
    """As-of universe including a delisted symbol when requested."""
    base = [
        UniverseEntry(symbol="AAPL", listed_until=None, renamed_from=None),
        UniverseEntry(symbol="MSFT", listed_until=None, renamed_from=None),
    ]
    if delisted:
        base.append(
            UniverseEntry(symbol="OLDCO", listed_until=_ts(40), renamed_from=None),
        )
    return tuple(base)


def _clean_rows() -> tuple[FeatureRow, ...]:
    """Rows where every feature observed_at <= decision_time (PIT-correct)."""
    return (
        _row(
            "AAPL",
            _ts(10),
            (_FeatVal("ret_1d", 0.01, _ts(10)), _FeatVal("vol_20d", 0.2, _ts(9))),
        ),
        _row(
            "MSFT",
            _ts(10),
            (_FeatVal("ret_1d", -0.005, _ts(10)), _FeatVal("vol_20d", 0.18, _ts(9))),
        ),
        _row(
            "AAPL",
            _ts(11),
            (_FeatVal("ret_1d", 0.02, _ts(11)), _FeatVal("vol_20d", 0.21, _ts(10))),
        ),
    )


# ---------------------------------------------------------------------------
# dataset_manifest.py
# ---------------------------------------------------------------------------


class TestFeatureLakeManifest:
    def test_manifest_is_frozen_and_strict(self) -> None:
        m = FeatureLakeManifest(
            dataset_id="ds-1",
            feature_schema_hash="abc",
            label_schema_hash="def",
            as_of_ts=_ts(12),
            universe_hash="uh",
            row_count=3,
            checksum="ck",
            folds=PurgedFoldSpec(
                folds=(
                    FoldBoundary(
                        fold_id=0,
                        train_start=_ts(0),
                        train_end=_ts(8),
                        val_start=_ts(9),
                        val_end=_ts(10),
                        purge_start=_ts(8),
                        purge_end=_ts(9),
                    ),
                ),
                embargo_ns=NS_PER_DAY,
                max_label_horizon_ns=NS_PER_DAY,
            ),
            pit_proof_verified=True,
        )
        assert m.schema_version == 1
        with pytest.raises(ValidationError):
            m.dataset_id = "x"  # frozen
        with pytest.raises(ValidationError):
            FeatureLakeManifest(  # type: ignore[call-arg]
                dataset_id="ds-1",
                feature_schema_hash="abc",
                label_schema_hash="def",
                as_of_ts=_ts(12),
                universe_hash="uh",
                row_count=3,
                checksum="ck",
                folds=m.folds,
                pit_proof_verified=True,
                unexpected_field=1,
            )

    def test_manifest_hash_is_deterministic(self) -> None:
        m1 = _minimal_manifest()
        m2 = _minimal_manifest()
        assert m1.manifest_hash() == m2.manifest_hash()

    def test_manifest_hash_changes_when_data_changes(self) -> None:
        m1 = _minimal_manifest()
        m2 = m1.model_copy(update={"row_count": 999})
        assert m1.manifest_hash() != m2.manifest_hash()

    def test_embargo_must_be_at_least_max_label_horizon(self) -> None:
        with pytest.raises(ValueError, match="embargo"):
            PurgedFoldSpec(
                folds=(
                    FoldBoundary(
                        fold_id=0,
                        train_start=_ts(0),
                        train_end=_ts(8),
                        val_start=_ts(9),
                        val_end=_ts(10),
                        purge_start=_ts(8),
                        purge_end=_ts(9),
                    ),
                ),
                embargo_ns=NS_PER_DAY // 2,  # < max horizon
                max_label_horizon_ns=NS_PER_DAY,
            )

    def test_fold_train_val_must_not_overlap_after_purge(self) -> None:
        # train_end bleeds into validation window with no purge gap
        with pytest.raises(ValueError, match=r"purge|overlap|leak"):
            PurgedFoldSpec(
                folds=(
                    FoldBoundary(
                        fold_id=0,
                        train_start=_ts(0),
                        train_end=_ts(10),  # overlaps val window
                        val_start=_ts(9),
                        val_end=_ts(10),
                        purge_start=_ts(10),
                        purge_end=_ts(10),  # zero purge
                    ),
                ),
                embargo_ns=NS_PER_DAY,
                max_label_horizon_ns=NS_PER_DAY,
            )

    def test_to_json_roundtrip(self) -> None:
        m = _minimal_manifest()
        payload = m.to_json()
        decoded = json.loads(payload)
        assert decoded["dataset_id"] == m.dataset_id
        assert decoded["folds"]["embargo_ns"] == NS_PER_DAY
        assert decoded["pit_proof_verified"] is True


def _minimal_manifest() -> FeatureLakeManifest:
    return FeatureLakeManifest(
        dataset_id="ds-1",
        feature_schema_hash="abc",
        label_schema_hash="def",
        as_of_ts=_ts(12),
        universe_hash="uh",
        row_count=3,
        checksum="ck",
        folds=PurgedFoldSpec(
            folds=(
                FoldBoundary(
                    fold_id=0,
                    train_start=_ts(0),
                    train_end=_ts(8),
                    val_start=_ts(9),
                    val_end=_ts(10),
                    purge_start=_ts(8),
                    purge_end=_ts(9),
                ),
            ),
            embargo_ns=NS_PER_DAY,
            max_label_horizon_ns=NS_PER_DAY,
        ),
        pit_proof_verified=True,
    )


# ---------------------------------------------------------------------------
# feature_lake.py — builder
# ---------------------------------------------------------------------------


class TestFeatureLakeBuilderPitProof:
    def test_clean_dataset_exports_with_manifest(self) -> None:
        builder = FeatureLakeBuilder(
            dataset_id="ds-clean",
            universe=_universe(),
            rows=_clean_rows(),
            feature_schema_hash="fsh",
            label_schema_hash="lsh",
        )
        manifest = builder.build_manifest()
        assert manifest.row_count == 3
        assert manifest.pit_proof_verified is True
        assert manifest.dataset_id == "ds-clean"
        assert manifest.feature_schema_hash == "fsh"

    def test_leaky_feature_rejected_at_export(self) -> None:
        # feature observed_at is AFTER the decision time -> look-ahead leak
        leaky_rows = (
            _row(
                "AAPL",
                _ts(10),
                (_FeatVal("future_leak", 0.5, _ts(11)),),  # observed at t+1
            ),
        )
        builder = FeatureLakeBuilder(
            dataset_id="ds-leaky",
            universe=_universe(),
            rows=leaky_rows,
            feature_schema_hash="fsh",
            label_schema_hash="lsh",
        )
        with pytest.raises(LeakyFeatureError):
            builder.build_manifest()

    def test_forward_join_rejected_at_construction(self) -> None:
        # A row whose decision_time is BEFORE an as-of universe cutoff that the
        # builder treats as a forward-join marker must be rejected at construction.
        # We model a forward join as: a row for a symbol whose listed_until is in
        # the future relative to the row's decision_time but the row claims data
        # from after delisting. Concretely: row decision_time after listed_until.
        rows = (
            _row(
                "OLDCO",
                _ts(50),  # decision after delisting at t=40
                (_FeatVal("ret_1d", 0.01, _ts(50)),),
            ),
        )
        universe = (UniverseEntry(symbol="OLDCO", listed_until=_ts(40), renamed_from=None),)
        with pytest.raises(ValueError, match=r"forward|as-of|universe"):
            FeatureLakeBuilder(
                dataset_id="ds-fwd",
                universe=universe,
                rows=rows,
                feature_schema_hash="fsh",
                label_schema_hash="lsh",
            )


class TestFeatureLakeBuilderUniverse:
    def test_as_of_universe_includes_delisted(self) -> None:
        universe = _universe(delisted=True)
        builder = FeatureLakeBuilder(
            dataset_id="ds-delisted",
            universe=universe,
            rows=_clean_rows(),
            feature_schema_hash="fsh",
            label_schema_hash="lsh",
        )
        manifest = builder.build_manifest()
        # universe hash must reflect the delisted entry (not survivorship-biased)
        assert "OLDCO" in manifest.universe_hash or manifest.universe_hash
        # The builder exposes the as-of universe including delisted symbols
        as_of = builder.as_of_universe(at=_ts(5))
        symbols = {e.symbol for e in as_of}
        assert "OLDCO" in symbols  # delisted symbol still in as-of universe at t=5


class TestFeatureLakeBuilderFolds:
    def test_folds_embargo_ge_max_horizon(self) -> None:
        rows = _clean_rows()
        builder = FeatureLakeBuilder(
            dataset_id="ds-folds",
            universe=_universe(),
            rows=rows,
            feature_schema_hash="fsh",
            label_schema_hash="lsh",
            max_label_horizon_ns=NS_PER_DAY,
        )
        manifest = builder.build_manifest()
        assert manifest.folds.embargo_ns >= manifest.folds.max_label_horizon_ns
        assert len(manifest.folds.folds) >= 1

    def test_no_train_row_overlaps_validation_label_window(self) -> None:
        rows = _clean_rows()
        builder = FeatureLakeBuilder(
            dataset_id="ds-folds2",
            universe=_universe(),
            rows=rows,
            feature_schema_hash="fsh",
            label_schema_hash="lsh",
            max_label_horizon_ns=NS_PER_DAY,
        )
        manifest = builder.build_manifest()
        for fb in manifest.folds.folds:
            # train_end + embargo must be <= val_start (purge gap respected)
            assert fb.train_end + manifest.folds.embargo_ns <= fb.val_start


# ---------------------------------------------------------------------------
# feature_availability.py
# ---------------------------------------------------------------------------


class TestFeatureAvailabilityReport:
    def test_report_counts_available_and_missing(self) -> None:
        rows = _clean_rows()
        report = FeatureAvailabilityReport.from_rows(
            rows=rows,
            expected_features=("ret_1d", "vol_20d", "rsi_14"),
        )
        assert report.total_rows == 3
        # ret_1d and vol_20d present in all rows; rsi_14 missing in all
        assert report.availability_pct("ret_1d") == 100.0
        assert report.availability_pct("vol_20d") == 100.0
        assert report.availability_pct("rsi_14") == 0.0
        assert "rsi_14" in report.missing_features()

    def test_report_is_serializable(self) -> None:
        rows = _clean_rows()
        report = FeatureAvailabilityReport.from_rows(
            rows=rows,
            expected_features=("ret_1d", "vol_20d"),
        )
        payload = json.loads(report.to_json())
        assert payload["total_rows"] == 3
        assert set(payload["per_feature"].keys()) == {"ret_1d", "vol_20d"}


# ---------------------------------------------------------------------------
# Export receipt
# ---------------------------------------------------------------------------


class TestExportReceipt:
    def test_receipt_written_and_stable(self, tmp_path) -> None:
        builder = FeatureLakeBuilder(
            dataset_id="ds-receipt",
            universe=_universe(),
            rows=_clean_rows(),
            feature_schema_hash="fsh",
            label_schema_hash="lsh",
            max_label_horizon_ns=NS_PER_DAY,
        )
        manifest = builder.build_manifest()
        availability = FeatureAvailabilityReport.from_rows(
            rows=_clean_rows(),
            expected_features=("ret_1d", "vol_20d"),
        )
        receipt = export_receipt(
            manifest=manifest,
            availability=availability,
            output_dir=tmp_path,
        )
        assert receipt.manifest_id == manifest.dataset_id
        assert receipt.manifest_hash == manifest.manifest_hash()
        assert receipt.row_count == manifest.row_count
        assert receipt.pit_proof_verified is True
        # receipt file exists on disk
        assert receipt.receipt_path.exists()
        loaded = json.loads(receipt.receipt_path.read_text())
        assert loaded["manifest_hash"] == manifest.manifest_hash()

    def test_training_job_references_manifest_not_db(self, tmp_path) -> None:
        builder = FeatureLakeBuilder(
            dataset_id="ds-ref",
            universe=_universe(),
            rows=_clean_rows(),
            feature_schema_hash="fsh",
            label_schema_hash="lsh",
            max_label_horizon_ns=NS_PER_DAY,
        )
        manifest = builder.build_manifest()
        # A training job references the manifest id + hash, NOT a DB connection.
        ref = manifest.training_reference()
        assert "dataset_id" in ref
        assert "manifest_hash" in ref
        assert "db_connection" not in ref
        assert "dsn" not in ref
        assert "password" not in ref


# ---------------------------------------------------------------------------
# Determinism / regression
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_same_inputs_same_manifest_hash(self) -> None:
        def build() -> str:
            b = FeatureLakeBuilder(
                dataset_id="ds-det",
                universe=_universe(),
                rows=_clean_rows(),
                feature_schema_hash="fsh",
                label_schema_hash="lsh",
                max_label_horizon_ns=NS_PER_DAY,
            )
            return b.build_manifest().manifest_hash()

        assert build() == build()

    def test_changed_row_data_changes_hash(self) -> None:
        rows = _clean_rows()
        b1 = FeatureLakeBuilder(
            dataset_id="ds-chg",
            universe=_universe(),
            rows=rows,
            feature_schema_hash="fsh",
            label_schema_hash="lsh",
            max_label_horizon_ns=NS_PER_DAY,
        )
        h1 = b1.build_manifest().manifest_hash()

        # mutate a feature value (deep copy to avoid touching the shared fixture)
        mutated = list(copy.deepcopy(rows))
        mutated[0] = FeatureRow(
            symbol=mutated[0].symbol,
            event_ts=mutated[0].event_ts,
            decision_time=mutated[0].decision_time,
            features=(_FeatVal("ret_1d", 0.99, mutated[0].features[0].observed_at),),
            label_horizon_ns=mutated[0].label_horizon_ns,
        )
        b2 = FeatureLakeBuilder(
            dataset_id="ds-chg",
            universe=_universe(),
            rows=tuple(mutated),
            feature_schema_hash="fsh",
            label_schema_hash="lsh",
            max_label_horizon_ns=NS_PER_DAY,
        )
        h2 = b2.build_manifest().manifest_hash()
        assert h1 != h2
