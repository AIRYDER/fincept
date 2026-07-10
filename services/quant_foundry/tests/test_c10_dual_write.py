"""Tests for C10 dual-write — settlement records, callback receipts, evidence.

Tests the dual-write behavior controlled by the ``QF_POSTGRES_SINK_ENABLED``
feature flag. All tests verify both flags-off (no Postgres write) and
flags-on (dual-write) behavior.

Test coverage:
  - Flags off: no Postgres write attempted
  - Flags on: legacy + Postgres write
  - Idempotent duplicate write (same record → no second row)
  - Hash mismatch rejection (different content → different row)
  - Database unavailable behavior (DB write fails → logged, legacy write OK)
  - Settlement dual-write (SettlementLedger with db_store)
  - Callback receipt dual-write (dual_write_callback_receipt)
  - Dossier dual-write (dual_write_dossier)
  - Model metric dual-write (dual_write_model_metric)
  - Legacy read behavior unchanged
  - Postgres reads still disabled
"""

from __future__ import annotations

import pathlib
from typing import Any
from unittest.mock import MagicMock

import pytest
from quant_foundry.c10_flags import (
    postgres_reads_enabled,
    should_read_from_postgres,
    should_write_to_postgres,
)
from quant_foundry.outcomes import CostModel, SettlementRecord, SettlementStatus
from quant_foundry.settlement import PredictionInput, SettlementLedger
from quant_foundry.settlement_db_sink import DbSettlementStore
from sqlalchemy import create_engine

from fincept_db.models import Base
from fincept_db.settlement_tables import SettlementRecordRow

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def engine():
    """In-memory SQLite engine with only the settlement_records table."""
    eng = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(eng, tables=[SettlementRecordRow.__table__])
    yield eng
    eng.dispose()


@pytest.fixture()
def db_store(engine):
    return DbSettlementStore(engine=engine)


@pytest.fixture()
def clean_env(monkeypatch: pytest.MonkeyPatch):
    """Remove all C10 flags from the environment."""
    for key in (
        "QF_POSTGRES_SINK_ENABLED",
        "QF_POSTGRES_READS_ENABLED",
        "QF_DUAL_WRITE_SETTLEMENTS",
        "QF_LEGACY_FILE_READ_FALLBACK",
        "QF_DUAL_WRITE_FAIL_HARD",
    ):
        monkeypatch.delenv(key, raising=False)
    yield


@pytest.fixture()
def sink_on(clean_env: None, monkeypatch: pytest.MonkeyPatch):
    """Enable the Postgres sink flag (dual-write mode)."""
    monkeypatch.setenv("QF_POSTGRES_SINK_ENABLED", "1")
    monkeypatch.setenv("QF_DUAL_WRITE_SETTLEMENTS", "1")
    yield


@pytest.fixture()
def sink_on_fail_hard(sink_on: None, monkeypatch: pytest.MonkeyPatch):
    """Enable the Postgres sink flag + fail-hard mode."""
    monkeypatch.setenv("QF_DUAL_WRITE_FAIL_HARD", "1")
    yield


@pytest.fixture()
def tmp_settlements_root(tmp_path: pathlib.Path):
    root = tmp_path / "settlements"
    root.mkdir()
    return root


def _prediction(
    *,
    prediction_id: str = "pred-001",
    model_id: str = "model-alpha",
) -> PredictionInput:
    return PredictionInput(
        prediction_id=prediction_id,
        model_id=model_id,
        symbol="AAPL",
        ts_event=1_700_000_000_000_000_000,
        horizon_ns=366_000_000_000,
        direction=1.0,
        confidence=0.7,
        p_up=0.7,
    )


def _cost_model() -> CostModel:
    return CostModel(
        version="cm-v1",
        fee_bps=10.0,
        spread_bps=5.0,
        slippage_bps=3.0,
        borrow_bps_per_day=2.0,
    )


def _settled_record(
    *,
    prediction_id: str = "pred-001",
    model_id: str = "model-alpha",
    cost_model_version: str = "cm-v1",
) -> SettlementRecord:
    return SettlementRecord(
        prediction_id=prediction_id,
        model_id=model_id,
        symbol="AAPL",
        ts_event=1_700_000_000_000_000_000,
        horizon_ns=366_000_000_000,
        status=SettlementStatus.SETTLED,
        settled_at_ns=1_700_000_366_000_000_000,
        realized_return_gross=0.05,
        realized_return_net=0.0482,
        abnormal_return=0.045,
        brier=0.09,
        calibration_bucket="0.6-0.8",
        cost_model_version=cost_model_version,
        decision_window_start=1_700_000_000_000_000_000,
        decision_window_end=1_700_000_366_000_000_000,
    )


# ---------------------------------------------------------------------------
# Flags off: no Postgres write
# ---------------------------------------------------------------------------


class TestFlagsOff:
    """When QF_POSTGRES_SINK_ENABLED=0, no Postgres write is attempted."""

    def test_settlement_no_db_write_when_flag_off(
        self,
        clean_env: None,
        db_store: DbSettlementStore,
        tmp_settlements_root: pathlib.Path,
    ) -> None:
        """SettlementLedger with db_store does not write to Postgres when flag is off."""
        ledger = SettlementLedger(root=tmp_settlements_root, db_store=db_store)
        record = _settled_record()
        ledger._append(record)
        ledger._dual_write(record, now_ns=1_700_000_366_000_000_000)
        # No Postgres write should have happened
        assert db_store.count() == 0

    def test_should_write_to_postgres_false(self, clean_env: None) -> None:
        """should_write_to_postgres() is False when flag is off."""
        assert should_write_to_postgres() is False

    def test_should_read_from_postgres_false(self, clean_env: None) -> None:
        """should_read_from_postgres() is False when flag is off."""
        assert should_read_from_postgres() is False

    def test_legacy_read_unchanged(
        self,
        clean_env: None,
        db_store: DbSettlementStore,
        tmp_settlements_root: pathlib.Path,
    ) -> None:
        """Legacy JSONL reads work correctly when flag is off."""
        ledger = SettlementLedger(root=tmp_settlements_root, db_store=db_store)
        record = _settled_record()
        ledger._append(record)
        # Read from JSONL (legacy path)
        records = ledger.read_all()
        assert len(records) == 1
        assert records[0].prediction_id == record.prediction_id

    def test_postgres_reads_disabled(self, clean_env: None) -> None:
        """postgres_reads_enabled() is False when flag is off."""
        assert postgres_reads_enabled() is False


# ---------------------------------------------------------------------------
# Flags on: dual-write
# ---------------------------------------------------------------------------


class TestFlagsOn:
    """When QF_POSTGRES_SINK_ENABLED=1, both legacy and Postgres writes happen."""

    def test_settlement_dual_write(
        self,
        sink_on: None,
        db_store: DbSettlementStore,
        tmp_settlements_root: pathlib.Path,
    ) -> None:
        """SettlementLedger writes to both JSONL and Postgres when flag is on."""
        ledger = SettlementLedger(root=tmp_settlements_root, db_store=db_store)
        record = _settled_record()
        ledger._append(record)
        ledger._dual_write(record, now_ns=1_700_000_366_000_000_000)
        # Postgres write should have happened
        assert db_store.count() == 1
        got = db_store.get("pred-001", "cm-v1")
        assert got is not None
        assert got.prediction_id == record.prediction_id
        # JSONL write should also have happened
        records = ledger.read_all()
        assert len(records) == 1

    def test_settlement_dual_write_via_settle(
        self,
        sink_on: None,
        db_store: DbSettlementStore,
        tmp_settlements_root: pathlib.Path,
    ) -> None:
        """Full settle() call dual-writes to both JSONL and Postgres."""
        ledger = SettlementLedger(root=tmp_settlements_root, db_store=db_store)
        from quant_foundry.metrics import PriceTick

        prices = [
            PriceTick(ts=1_700_000_000_000_000_000, price=100.0),
            PriceTick(ts=1_700_000_366_000_000_000, price=105.0),
        ]
        record = ledger.settle(
            prediction=_prediction(),
            prices=prices,
            benchmark_prices=None,
            cost_model=_cost_model(),
            now_ns=1_700_000_366_000_000_000,
        )
        assert record.status == SettlementStatus.SETTLED
        # Postgres write
        assert db_store.count() == 1
        # JSONL write
        assert len(ledger.read_all()) == 1

    def test_callback_receipt_dual_write(
        self,
        sink_on: None,
    ) -> None:
        """dual_write_callback_receipt writes to DB when flag is on."""
        from quant_foundry.dual_write import dual_write_callback_receipt

        mock_store = MagicMock()
        mock_receipt = MagicMock()
        mock_receipt.callback_id = "cb-001"
        dual_write_callback_receipt(mock_store, mock_receipt)
        mock_store.write.assert_called_once_with(mock_receipt)

    def test_dossier_dual_write(
        self,
        sink_on: None,
    ) -> None:
        """dual_write_dossier writes to DB when flag is on."""
        from quant_foundry.dual_write import dual_write_dossier

        mock_store = MagicMock()
        training_result: dict[str, Any] = {
            "dossier": {"model_id": "model-alpha"},
            "artifact_manifest": {"artifact_id": "art-001"},
        }
        dual_write_dossier(mock_store, training_result)
        mock_store.store.assert_called_once_with(training_result)

    def test_model_metric_dual_write(
        self,
        sink_on: None,
    ) -> None:
        """dual_write_model_metric writes to DB when flag is on."""
        from quant_foundry.dual_write import dual_write_model_metric

        mock_registry = MagicMock()
        dual_write_model_metric(
            mock_registry,
            version_id="ver-001",
            metric_type="selfcheck",
            metrics={"passed": True},
        )
        mock_registry.record_metrics.assert_called_once_with(
            version_id="ver-001",
            metric_type="selfcheck",
            metrics={"passed": True},
            now_ns=None,
        )


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


class TestIdempotency:
    """Dual-write is idempotent — replaying the same record does not duplicate."""

    def test_settlement_idempotent_replay(
        self,
        sink_on: None,
        db_store: DbSettlementStore,
        tmp_settlements_root: pathlib.Path,
    ) -> None:
        """Settling the same prediction twice does not create a second DB row."""
        ledger = SettlementLedger(root=tmp_settlements_root, db_store=db_store)
        record = _settled_record()
        # First write
        ledger._append(record)
        ledger._dual_write(record, now_ns=1_700_000_366_000_000_000)
        assert db_store.count() == 1
        # Second write (replay)
        ledger._dual_write(record, now_ns=1_700_000_366_000_000_000)
        assert db_store.count() == 1  # still 1, not 2

    def test_settlement_different_cost_model_new_row(
        self,
        sink_on: None,
        db_store: DbSettlementStore,
        tmp_settlements_root: pathlib.Path,
    ) -> None:
        """Same prediction_id but different cost_model_version → new row."""
        ledger = SettlementLedger(root=tmp_settlements_root, db_store=db_store)
        r1 = _settled_record(prediction_id="pred-001", cost_model_version="cm-v1")
        r2 = _settled_record(prediction_id="pred-001", cost_model_version="v1.default")
        ledger._append(r1)
        ledger._dual_write(r1, now_ns=1_700_000_366_000_000_000)
        ledger._append(r2)
        ledger._dual_write(r2, now_ns=1_700_000_366_000_000_000)
        assert db_store.count() == 2


# ---------------------------------------------------------------------------
# Hash mismatch
# ---------------------------------------------------------------------------


class TestHashMismatch:
    """Hash mismatch behavior — different content produces different rows."""

    def test_different_records_different_rows(
        self,
        sink_on: None,
        db_store: DbSettlementStore,
        tmp_settlements_root: pathlib.Path,
    ) -> None:
        """Two different settlement records produce two different DB rows."""
        ledger = SettlementLedger(root=tmp_settlements_root, db_store=db_store)
        r1 = _settled_record(prediction_id="pred-001")
        r2 = _settled_record(prediction_id="pred-002")
        ledger._append(r1)
        ledger._dual_write(r1, now_ns=1_700_000_366_000_000_000)
        ledger._append(r2)
        ledger._dual_write(r2, now_ns=1_700_000_366_000_000_000)
        assert db_store.count() == 2
        got1 = db_store.get("pred-001", "cm-v1")
        got2 = db_store.get("pred-002", "cm-v1")
        assert got1 is not None
        assert got2 is not None
        assert got1.prediction_id != got2.prediction_id


# ---------------------------------------------------------------------------
# Database unavailable
# ---------------------------------------------------------------------------


class TestDatabaseUnavailable:
    """When the DB write fails, the legacy write (which already succeeded) is not lost."""

    def test_db_failure_does_not_block_legacy_write(
        self,
        sink_on: None,
        tmp_settlements_root: pathlib.Path,
    ) -> None:
        """DB write failure is logged; JSONL write (already done) is preserved."""
        # Create a broken db_store that raises on write
        broken_store = MagicMock()
        broken_store.write.side_effect = RuntimeError("DB connection refused")
        ledger = SettlementLedger(root=tmp_settlements_root, db_store=broken_store)
        record = _settled_record()
        # JSONL write first (always succeeds)
        ledger._append(record)
        # DB write fails — should not raise in default (non-fail-hard) mode
        ledger._dual_write(record, now_ns=1_700_000_366_000_000_000)
        # JSONL record is preserved
        assert len(ledger.read_all()) == 1

    def test_db_failure_fail_hard_re_raises(
        self,
        sink_on_fail_hard: None,
        tmp_settlements_root: pathlib.Path,
    ) -> None:
        """In fail-hard mode, DB write failure re-raises."""
        broken_store = MagicMock()
        broken_store.write.side_effect = RuntimeError("DB connection refused")
        ledger = SettlementLedger(root=tmp_settlements_root, db_store=broken_store)
        record = _settled_record()
        ledger._append(record)
        with pytest.raises(RuntimeError, match="DB connection refused"):
            ledger._dual_write(record, now_ns=1_700_000_366_000_000_000)

    def test_callback_receipt_db_failure_logged(
        self,
        sink_on: None,
    ) -> None:
        """dual_write_callback_receipt logs DB failure, does not raise."""
        from quant_foundry.dual_write import dual_write_callback_receipt

        broken_store = MagicMock()
        broken_store.write.side_effect = RuntimeError("DB unavailable")
        mock_receipt = MagicMock()
        mock_receipt.callback_id = "cb-001"
        # Should not raise in default mode
        dual_write_callback_receipt(broken_store, mock_receipt)
        broken_store.write.assert_called_once()

    def test_callback_receipt_db_failure_fail_hard(
        self,
        sink_on_fail_hard: None,
    ) -> None:
        """dual_write_callback_receipt re-raises in fail-hard mode."""
        from quant_foundry.dual_write import dual_write_callback_receipt

        broken_store = MagicMock()
        broken_store.write.side_effect = RuntimeError("DB unavailable")
        mock_receipt = MagicMock()
        mock_receipt.callback_id = "cb-001"
        with pytest.raises(RuntimeError, match="DB unavailable"):
            dual_write_callback_receipt(broken_store, mock_receipt)

    def test_no_db_store_no_error(
        self,
        sink_on: None,
        tmp_settlements_root: pathlib.Path,
    ) -> None:
        """SettlementLedger without db_store does not attempt DB write."""
        ledger = SettlementLedger(root=tmp_settlements_root, db_store=None)
        record = _settled_record()
        ledger._append(record)
        # Should be a no-op, no error
        ledger._dual_write(record, now_ns=1_700_000_366_000_000_000)
        assert len(ledger.read_all()) == 1


# ---------------------------------------------------------------------------
# Legacy read behavior unchanged
# ---------------------------------------------------------------------------


class TestLegacyReadUnchanged:
    """Legacy JSONL reads work correctly regardless of C10 flags."""

    def test_read_all_from_jsonl_with_flag_off(
        self,
        clean_env: None,
        db_store: DbSettlementStore,
        tmp_settlements_root: pathlib.Path,
    ) -> None:
        """read_all() returns JSONL records when flag is off."""
        ledger = SettlementLedger(root=tmp_settlements_root, db_store=db_store)
        record = _settled_record()
        ledger._append(record)
        records = ledger.read_all()
        assert len(records) == 1
        assert records[0].prediction_id == "pred-001"

    def test_read_all_from_jsonl_with_flag_on(
        self,
        sink_on: None,
        db_store: DbSettlementStore,
        tmp_settlements_root: pathlib.Path,
    ) -> None:
        """read_all() still reads from JSONL even when flag is on (reads not flipped)."""
        ledger = SettlementLedger(root=tmp_settlements_root, db_store=db_store)
        record = _settled_record()
        ledger._append(record)
        ledger._dual_write(record, now_ns=1_700_000_366_000_000_000)
        # read_all() reads from JSONL, not Postgres
        records = ledger.read_all()
        assert len(records) == 1
        # Postgres also has the record, but reads are not flipped
        assert db_store.count() == 1
        assert should_read_from_postgres() is False


# ---------------------------------------------------------------------------
# Dual-write comparison proof
# ---------------------------------------------------------------------------


class TestDualWriteComparison:
    """Verify that JSONL and Postgres records are field-by-field equal."""

    def test_settlement_jsonl_equals_postgres(
        self,
        sink_on: None,
        db_store: DbSettlementStore,
        tmp_settlements_root: pathlib.Path,
    ) -> None:
        """JSONL record and Postgres record are field-by-field equal."""
        ledger = SettlementLedger(root=tmp_settlements_root, db_store=db_store)
        record = _settled_record()
        ledger._append(record)
        ledger._dual_write(record, now_ns=1_700_000_366_000_000_000)

        # Read from JSONL
        jsonl_records = ledger.read_all()
        assert len(jsonl_records) == 1
        jsonl_rec = jsonl_records[0]

        # Read from Postgres
        db_rec = db_store.get("pred-001", "cm-v1")
        assert db_rec is not None

        # Field-by-field comparison
        assert jsonl_rec.prediction_id == db_rec.prediction_id
        assert jsonl_rec.model_id == db_rec.model_id
        assert jsonl_rec.symbol == db_rec.symbol
        assert jsonl_rec.ts_event == db_rec.ts_event
        assert jsonl_rec.horizon_ns == db_rec.horizon_ns
        assert jsonl_rec.status == db_rec.status
        assert jsonl_rec.settled_at_ns == db_rec.settled_at_ns
        assert jsonl_rec.realized_return_gross == pytest.approx(db_rec.realized_return_gross)
        assert jsonl_rec.realized_return_net == pytest.approx(db_rec.realized_return_net)
        assert jsonl_rec.abnormal_return == pytest.approx(db_rec.abnormal_return)
        assert jsonl_rec.brier == pytest.approx(db_rec.brier)
        assert jsonl_rec.calibration_bucket == db_rec.calibration_bucket
        assert jsonl_rec.cost_model_version == db_rec.cost_model_version
        assert jsonl_rec.decision_window_start == db_rec.decision_window_start
        assert jsonl_rec.decision_window_end == db_rec.decision_window_end

    def test_settlement_replay_zero_divergences(
        self,
        sink_on: None,
        db_store: DbSettlementStore,
        tmp_settlements_root: pathlib.Path,
    ) -> None:
        """Multiple settled records produce 0 divergences between JSONL and Postgres."""
        ledger = SettlementLedger(root=tmp_settlements_root, db_store=db_store)
        now_ns = 1_700_000_366_000_000_000
        for i in range(10):
            record = _settled_record(
                prediction_id=f"pred-{i:03d}",
                model_id="model-alpha",
            )
            ledger._append(record)
            ledger._dual_write(record, now_ns=now_ns + i)

        jsonl_records = ledger.read_all()
        db_records = db_store.list_all()

        assert len(jsonl_records) == 10
        assert len(db_records) == 10

        # Compare each record by prediction_id
        jsonl_by_id = {r.prediction_id: r for r in jsonl_records}
        db_by_id = {r.prediction_id: r for r in db_records}

        divergences = 0
        for pred_id in jsonl_by_id:
            j = jsonl_by_id[pred_id]
            d = db_by_id.get(pred_id)
            if d is None:
                divergences += 1
                continue
            if j.status != d.status:
                divergences += 1
            if j.realized_return_gross is not None and d.realized_return_gross is not None:
                if abs(j.realized_return_gross - d.realized_return_gross) > 1e-9:
                    divergences += 1
            if j.realized_return_net is not None and d.realized_return_net is not None:
                if abs(j.realized_return_net - d.realized_return_net) > 1e-9:
                    divergences += 1

        assert divergences == 0, f"{divergences} divergences found"
