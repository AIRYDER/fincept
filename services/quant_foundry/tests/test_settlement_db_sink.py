"""Tests for quant_foundry.settlement_db_sink — DB-backed settlement store.

Tests the ``DbSettlementStore`` against an in-memory SQLite database (no
Postgres required). The store uses ``INSERT ... ON CONFLICT DO NOTHING`` for
idempotency, which works on both SQLite and Postgres (the sink code picks the
right dialect-specific insert at runtime).

Test coverage:
  - Insert a settlement record, read it back, verify field-by-field equality.
  - Idempotent insert (same prediction_id + cost_model_version → no-op).
  - List records for a model, filtered by status.
  - Count records, filtered by model_id and/or status.
  - Pending records (status=pending_time, settled_at_ns=None, return fields None).
  - Batch write with mixed new + existing records.
  - CHECK constraints reject invalid status / cost_model_version.
  - No secrets / raw payloads in any column.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from quant_foundry.outcomes import SettlementRecord, SettlementStatus
from quant_foundry.settlement_db_sink import DbSettlementStore
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from fincept_db.models import Base
from fincept_db.settlement_tables import SettlementRecordRow

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def engine():
    """In-memory SQLite engine with only the settlement_records table created."""
    eng = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(eng, tables=[SettlementRecordRow.__table__])
    yield eng
    eng.dispose()


@pytest.fixture()
def store(engine):
    return DbSettlementStore(engine=engine)


@pytest.fixture()
def now_ns():
    return 1_700_000_000_000_000_000


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _settled_record(
    *,
    prediction_id: str = "pred-001",
    model_id: str = "model-alpha",
    symbol: str = "AAPL",
    cost_model_version: str = "cm-v1",
    settled_at_ns: int = 1_700_000_366_000_000_000,
) -> SettlementRecord:
    return SettlementRecord(
        prediction_id=prediction_id,
        model_id=model_id,
        symbol=symbol,
        ts_event=1_700_000_000_000_000_000,
        horizon_ns=366_000_000_000,
        status=SettlementStatus.SETTLED,
        settled_at_ns=settled_at_ns,
        realized_return_gross=0.05,
        realized_return_net=0.0482,
        abnormal_return=0.045,
        brier=0.09,
        calibration_bucket="0.6-0.8",
        cost_model_version=cost_model_version,
        decision_window_start=1_700_000_000_000_000_000,
        decision_window_end=1_700_000_366_000_000_000,
    )


def _pending_record(
    *,
    prediction_id: str = "pred-002",
    model_id: str = "model-alpha",
) -> SettlementRecord:
    return SettlementRecord(
        prediction_id=prediction_id,
        model_id=model_id,
        symbol="MSFT",
        ts_event=1_700_000_100_000_000_000,
        horizon_ns=366_000_000_000,
        status=SettlementStatus.PENDING_TIME,
        settled_at_ns=None,
        realized_return_gross=None,
        realized_return_net=None,
        abnormal_return=None,
        brier=None,
        calibration_bucket=None,
        cost_model_version="cm-v1",
        decision_window_start=1_700_000_100_000_000_000,
        decision_window_end=1_700_000_466_000_000_000,
    )


# ---------------------------------------------------------------------------
# Insert + read back
# ---------------------------------------------------------------------------


def test_write_and_get(store: DbSettlementStore, now_ns: int) -> None:
    """Write a record, read it back, verify field-by-field equality."""
    record = _settled_record()
    inserted = store.write(record, now_ns=now_ns)
    assert inserted is True

    got = store.get("pred-001", "cm-v1")
    assert got is not None
    assert got.prediction_id == record.prediction_id
    assert got.model_id == record.model_id
    assert got.symbol == record.symbol
    assert got.ts_event == record.ts_event
    assert got.horizon_ns == record.horizon_ns
    assert got.status == record.status
    assert got.settled_at_ns == record.settled_at_ns
    assert got.realized_return_gross == pytest.approx(record.realized_return_gross)
    assert got.realized_return_net == pytest.approx(record.realized_return_net)
    assert got.abnormal_return == pytest.approx(record.abnormal_return)
    assert got.brier == pytest.approx(record.brier)
    assert got.calibration_bucket == record.calibration_bucket
    assert got.cost_model_version == record.cost_model_version
    assert got.decision_window_start == record.decision_window_start
    assert got.decision_window_end == record.decision_window_end


def test_write_returns_true_for_new_record(store: DbSettlementStore, now_ns: int) -> None:
    """First write of a record returns True (new row inserted)."""
    record = _settled_record()
    assert store.write(record, now_ns=now_ns) is True


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_idempotent_write_same_record(store: DbSettlementStore, now_ns: int) -> None:
    """Writing the same record twice does not create a second row."""
    record = _settled_record()
    assert store.write(record, now_ns=now_ns) is True
    # Second write — same settlement_id → ON CONFLICT DO NOTHING
    assert store.write(record, now_ns=now_ns + 1) is False
    assert store.count() == 1


def test_idempotent_write_different_cost_model(store: DbSettlementStore, now_ns: int) -> None:
    """Same prediction_id but different cost_model_version → new row."""
    r1 = _settled_record(prediction_id="pred-001", cost_model_version="cm-v1")
    r2 = _settled_record(prediction_id="pred-001", cost_model_version="v1.default")
    assert store.write(r1, now_ns=now_ns) is True
    assert store.write(r2, now_ns=now_ns) is True
    assert store.count() == 2


# ---------------------------------------------------------------------------
# Pending records
# ---------------------------------------------------------------------------


def test_pending_record_round_trips(store: DbSettlementStore, now_ns: int) -> None:
    """Pending records (null returns, null settled_at_ns) round-trip correctly."""
    record = _pending_record()
    assert store.write(record, now_ns=now_ns) is True

    got = store.get("pred-002", "cm-v1")
    assert got is not None
    assert got.status == SettlementStatus.PENDING_TIME
    assert got.settled_at_ns is None
    assert got.realized_return_gross is None
    assert got.realized_return_net is None
    assert got.abnormal_return is None
    assert got.brier is None
    assert got.calibration_bucket is None


# ---------------------------------------------------------------------------
# List + count
# ---------------------------------------------------------------------------


def test_list_for_model(store: DbSettlementStore, now_ns: int) -> None:
    """list_for_model returns records for the specified model, newest-first."""
    # pred-001: settled_at_ns = 1_700_000_366_000_000_000 (default)
    store.write(_settled_record(prediction_id="pred-001"), now_ns=now_ns)
    # pred-003: settled_at_ns = later than pred-001 → should be first
    store.write(
        _settled_record(prediction_id="pred-003", settled_at_ns=1_700_000_400_000_000_000),
        now_ns=now_ns,
    )
    # pred-002: pending (settled_at_ns = None) → should be last
    store.write(
        _pending_record(prediction_id="pred-002"),
        now_ns=now_ns,
    )
    records = store.list_for_model("model-alpha")
    assert len(records) == 3
    # Newest settled_at_ns first; pending (null) last
    assert records[0].prediction_id == "pred-003"
    assert records[1].prediction_id == "pred-001"
    # Pending record is last (nullslast)
    assert records[2].prediction_id == "pred-002"


def test_list_for_model_filtered_by_status(store: DbSettlementStore, now_ns: int) -> None:
    """list_for_model with status filter returns only matching records."""
    store.write(_settled_record(prediction_id="pred-001"), now_ns=now_ns)
    store.write(_pending_record(prediction_id="pred-002"), now_ns=now_ns)

    settled = store.list_for_model("model-alpha", status="settled")
    assert len(settled) == 1
    assert settled[0].prediction_id == "pred-001"

    pending = store.list_for_model("model-alpha", status="pending_time")
    assert len(pending) == 1
    assert pending[0].prediction_id == "pred-002"


def test_list_all(store: DbSettlementStore, now_ns: int) -> None:
    """list_all returns records across all models."""
    store.write(
        _settled_record(prediction_id="pred-001", model_id="model-a"),
        now_ns=now_ns,
    )
    store.write(
        _settled_record(prediction_id="pred-002", model_id="model-b"),
        now_ns=now_ns,
    )
    records = store.list_all()
    assert len(records) == 2


def test_count(store: DbSettlementStore, now_ns: int) -> None:
    """count returns total, filtered by model_id and/or status."""
    store.write(_settled_record(prediction_id="pred-001"), now_ns=now_ns)
    store.write(_pending_record(prediction_id="pred-002"), now_ns=now_ns)

    assert store.count() == 2
    assert store.count(model_id="model-alpha") == 2
    assert store.count(status="settled") == 1
    assert store.count(status="pending_time") == 1
    assert store.count(model_id="model-alpha", status="settled") == 1
    assert store.count(model_id="nonexistent") == 0


# ---------------------------------------------------------------------------
# Batch write
# ---------------------------------------------------------------------------


def test_write_batch(store: DbSettlementStore, now_ns: int) -> None:
    """write_batch inserts new records and skips existing ones."""
    r1 = _settled_record(prediction_id="pred-001")
    r2 = _settled_record(prediction_id="pred-002")
    r3 = _pending_record(prediction_id="pred-003")

    inserted = store.write_batch([r1, r2, r3], now_ns=now_ns)
    assert inserted == 3
    assert store.count() == 3

    # Replay — all 3 are existing → 0 inserted
    inserted_again = store.write_batch([r1, r2, r3], now_ns=now_ns)
    assert inserted_again == 0
    assert store.count() == 3


# ---------------------------------------------------------------------------
# CHECK constraints
# ---------------------------------------------------------------------------


def test_invalid_status_rejected(store: DbSettlementStore, now_ns: int) -> None:
    """CHECK constraint rejects invalid status."""
    # Bypass the dataclass validation by directly inserting a bad row
    bad_values = {
        "schema_version": 1,
        "settlement_id": "pred-001:cm-v1",
        "prediction_id": "pred-001",
        "model_id": "model-alpha",
        "symbol": "AAPL",
        "ts_event": 1_700_000_000_000_000_000,
        "horizon_ns": 366_000_000_000,
        "status": "invalid_status",
        "settled_at_ns": now_ns,
        "realized_return_gross": Decimal("0.05"),
        "realized_return_net": Decimal("0.0482"),
        "abnormal_return": Decimal("0.045"),
        "brier": Decimal("0.09"),
        "calibration_bucket": "0.6-0.8",
        "cost_model_version": "cm-v1",
        "decision_window_start": 1_700_000_000_000_000_000,
        "decision_window_end": 1_700_000_366_000_000_000,
        "created_at_ns": now_ns,
    }
    with Session(store.engine) as session:
        session.add(SettlementRecordRow(**bad_values))
        with pytest.raises(Exception):
            session.commit()


def test_invalid_cost_model_version_rejected(store: DbSettlementStore, now_ns: int) -> None:
    """CHECK constraint rejects invalid cost_model_version."""
    bad_values = {
        "schema_version": 1,
        "settlement_id": "pred-001:bad-version",
        "prediction_id": "pred-001",
        "model_id": "model-alpha",
        "symbol": "AAPL",
        "ts_event": 1_700_000_000_000_000_000,
        "horizon_ns": 366_000_000_000,
        "status": "settled",
        "settled_at_ns": now_ns,
        "realized_return_gross": Decimal("0.05"),
        "realized_return_net": Decimal("0.0482"),
        "abnormal_return": Decimal("0.045"),
        "brier": Decimal("0.09"),
        "calibration_bucket": "0.6-0.8",
        "cost_model_version": "bad-version",
        "decision_window_start": 1_700_000_000_000_000_000,
        "decision_window_end": 1_700_000_366_000_000_000,
        "created_at_ns": now_ns,
    }
    with Session(store.engine) as session:
        session.add(SettlementRecordRow(**bad_values))
        with pytest.raises(Exception):
            session.commit()


# ---------------------------------------------------------------------------
# No secrets / raw payloads
# ---------------------------------------------------------------------------


def test_no_secrets_in_columns(store: DbSettlementStore, now_ns: int) -> None:
    """No column stores secrets, signatures, or raw payloads."""
    record = _settled_record()
    store.write(record, now_ns=now_ns)

    with Session(store.engine) as session:
        row = session.get(SettlementRecordRow, "pred-001:cm-v1")
        assert row is not None
        # Verify no secret-like columns exist
        col_names = set(row.__table__.columns.keys())
        for forbidden in ("secret", "signature", "payload", "api_key", "password"):
            for col in col_names:
                assert forbidden not in col.lower(), (
                    f"column {col} contains forbidden substring: {forbidden}"
                )
