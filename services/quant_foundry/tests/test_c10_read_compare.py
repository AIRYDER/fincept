"""Tests for C10 read-compare mode.

Tests the read-compare behavior controlled by the
``QF_POSTGRES_READ_COMPARE_ENABLED`` feature flag. All tests verify both
flags-off (no Postgres read) and flags-on (read-compare) behavior.

Test coverage:
  - Flags off: legacy-only read, no Postgres read attempted
  - Read-compare on: legacy and Postgres match
  - Read-compare on: Postgres missing (read_compare_miss)
  - Read-compare on: field mismatch (read_compare_mismatch)
  - Read-compare on: hash mismatch (read_compare_mismatch)
  - Read-compare on: Postgres read error (read_compare_error)
  - Legacy result is returned even when comparison fails
  - Comparison emits structured evidence
  - No Postgres result is returned while QF_POSTGRES_READS_ENABLED=0
  - Settlement replay remains 0 divergences
  - Counters are updated correctly
  - Dict-based read-compare (callback receipts, dossiers, metrics)
"""

from __future__ import annotations

import pathlib
from unittest.mock import MagicMock

import pytest
from quant_foundry.c10_flags import (
    postgres_read_compare_enabled,
    should_read_compare,
)
from quant_foundry.outcomes import SettlementRecord, SettlementStatus
from quant_foundry.read_compare import (
    ReadCompareEvidence,
    compare_settlement_records,
    get_counters,
    normalize_settlement_record,
    read_compare_dict,
    read_compare_settlement,
    read_compare_settlement_batch,
    reset_counters,
)
from quant_foundry.settlement import SettlementLedger
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
        "QF_POSTGRES_READ_COMPARE_ENABLED",
    ):
        monkeypatch.delenv(key, raising=False)
    yield


@pytest.fixture()
def read_compare_on(clean_env: None, monkeypatch: pytest.MonkeyPatch):
    """Enable read-compare mode (sink + dual-write + read-compare)."""
    monkeypatch.setenv("QF_POSTGRES_SINK_ENABLED", "1")
    monkeypatch.setenv("QF_DUAL_WRITE_SETTLEMENTS", "1")
    monkeypatch.setenv("QF_POSTGRES_READ_COMPARE_ENABLED", "1")
    reset_counters()
    yield


@pytest.fixture()
def read_compare_on_fail_hard(read_compare_on: None, monkeypatch: pytest.MonkeyPatch):
    """Enable read-compare mode + fail-hard."""
    monkeypatch.setenv("QF_DUAL_WRITE_FAIL_HARD", "1")
    yield


@pytest.fixture()
def tmp_settlements_root(tmp_path: pathlib.Path):
    root = tmp_path / "settlements"
    root.mkdir()
    return root


def _settled_record(
    *,
    prediction_id: str = "pred-001",
    model_id: str = "model-alpha",
    cost_model_version: str = "cm-v1",
    realized_return_gross: float = 0.05,
) -> SettlementRecord:
    return SettlementRecord(
        prediction_id=prediction_id,
        model_id=model_id,
        symbol="AAPL",
        ts_event=1_700_000_000_000_000_000,
        horizon_ns=366_000_000_000,
        status=SettlementStatus.SETTLED,
        settled_at_ns=1_700_000_366_000_000_000,
        realized_return_gross=realized_return_gross,
        realized_return_net=0.0482,
        abnormal_return=0.045,
        brier=0.09,
        calibration_bucket="0.6-0.8",
        cost_model_version=cost_model_version,
        decision_window_start=1_700_000_000_000_000_000,
        decision_window_end=1_700_000_366_000_000_000,
    )


# ---------------------------------------------------------------------------
# Flags off: no Postgres read
# ---------------------------------------------------------------------------


class TestFlagsOff:
    """When QF_POSTGRES_READ_COMPARE_ENABLED=0, no Postgres read is attempted."""

    def test_read_compare_defaults_off(self, clean_env: None) -> None:
        """postgres_read_compare_enabled() is False by default."""
        assert postgres_read_compare_enabled() is False
        assert should_read_compare() is False

    def test_no_postgres_read_when_flag_off(
        self,
        clean_env: None,
        db_store: DbSettlementStore,
        tmp_settlements_root: pathlib.Path,
    ) -> None:
        """read_all() does not read from Postgres when flag is off."""
        ledger = SettlementLedger(root=tmp_settlements_root, db_store=db_store)
        record = _settled_record()
        ledger._append(record)
        # Mock the db_store.get to detect if it's called
        db_store.get = MagicMock(side_effect=AssertionError("Postgres read should not be called"))
        records = ledger.read_all()
        assert len(records) == 1
        assert records[0].prediction_id == "pred-001"
        db_store.get.assert_not_called()

    def test_legacy_read_unchanged_with_db_store(
        self,
        clean_env: None,
        db_store: DbSettlementStore,
        tmp_settlements_root: pathlib.Path,
    ) -> None:
        """Legacy JSONL reads work correctly even with db_store injected."""
        ledger = SettlementLedger(root=tmp_settlements_root, db_store=db_store)
        record = _settled_record()
        ledger._append(record)
        records = ledger.read_all()
        assert len(records) == 1
        assert records[0].prediction_id == record.prediction_id

    def test_counters_zero_when_flag_off(
        self,
        clean_env: None,
        db_store: DbSettlementStore,
        tmp_settlements_root: pathlib.Path,
    ) -> None:
        """Counters remain at zero when flag is off."""
        reset_counters()
        ledger = SettlementLedger(root=tmp_settlements_root, db_store=db_store)
        record = _settled_record()
        ledger._append(record)
        ledger.read_all()
        c = get_counters()
        assert c.total() == 0


# ---------------------------------------------------------------------------
# Read-compare on: match
# ---------------------------------------------------------------------------


class TestReadCompareMatch:
    """When read-compare is on and records match, evidence is 'match'."""

    def test_settlement_match(
        self,
        read_compare_on: None,
        db_store: DbSettlementStore,
        tmp_settlements_root: pathlib.Path,
    ) -> None:
        """Legacy and Postgres records match → 'match' evidence."""
        ledger = SettlementLedger(root=tmp_settlements_root, db_store=db_store)
        record = _settled_record()
        ledger._append(record)
        ledger._dual_write(record, now_ns=1_700_000_366_000_000_000)
        # read_all() triggers read-compare
        records = ledger.read_all()
        assert len(records) == 1
        # Legacy record is returned
        assert records[0].prediction_id == "pred-001"
        # Counter shows a match
        c = get_counters()
        assert c.matches == 1
        assert c.mismatches == 0
        assert c.misses == 0
        assert c.errors == 0

    def test_settlement_match_batch(
        self,
        read_compare_on: None,
        db_store: DbSettlementStore,
        tmp_settlements_root: pathlib.Path,
    ) -> None:
        """Multiple records all match → all 'match' evidence."""
        ledger = SettlementLedger(root=tmp_settlements_root, db_store=db_store)
        now_ns = 1_700_000_366_000_000_000
        for i in range(5):
            record = _settled_record(prediction_id=f"pred-{i:03d}")
            ledger._append(record)
            ledger._dual_write(record, now_ns=now_ns + i)
        records = ledger.read_all()
        assert len(records) == 5
        c = get_counters()
        assert c.matches == 5
        assert c.mismatches == 0
        assert c.misses == 0
        assert c.errors == 0

    def test_settlement_match_direct(
        self,
        read_compare_on: None,
        db_store: DbSettlementStore,
    ) -> None:
        """Direct read_compare_settlement() returns match evidence."""
        record = _settled_record()
        db_store.write(record, now_ns=1_700_000_366_000_000_000)
        evidence = read_compare_settlement(record, db_store)
        assert evidence.outcome == "match"
        assert evidence.record_type == "settlement_record"
        assert evidence.record_key == "pred-001:cm-v1"
        assert evidence.legacy_hash is not None
        assert evidence.postgres_hash is not None
        assert evidence.legacy_hash == evidence.postgres_hash


# ---------------------------------------------------------------------------
# Read-compare on: missing Postgres
# ---------------------------------------------------------------------------


class TestReadCompareMissing:
    """When legacy exists but Postgres is missing, evidence is 'read_compare_miss'."""

    def test_settlement_missing_postgres(
        self,
        read_compare_on: None,
        db_store: DbSettlementStore,
        tmp_settlements_root: pathlib.Path,
    ) -> None:
        """Legacy record exists but Postgres is empty → 'read_compare_miss'."""
        ledger = SettlementLedger(root=tmp_settlements_root, db_store=db_store)
        record = _settled_record()
        ledger._append(record)
        # Do NOT dual-write — Postgres is empty
        records = ledger.read_all()
        assert len(records) == 1
        # Legacy record is still returned
        assert records[0].prediction_id == "pred-001"
        c = get_counters()
        assert c.misses == 1
        assert c.matches == 0
        assert c.mismatches == 0

    def test_settlement_missing_postgres_direct(
        self,
        read_compare_on: None,
        db_store: DbSettlementStore,
    ) -> None:
        """Direct read_compare_settlement() returns miss evidence."""
        record = _settled_record()
        # Don't write to DB
        evidence = read_compare_settlement(record, db_store)
        assert evidence.outcome == "read_compare_miss"
        assert evidence.record_key == "pred-001:cm-v1"
        assert evidence.legacy_hash is not None
        assert evidence.postgres_hash is None

    def test_missing_postgres_returns_legacy(
        self,
        read_compare_on: None,
        db_store: DbSettlementStore,
        tmp_settlements_root: pathlib.Path,
    ) -> None:
        """Legacy record is returned even when Postgres is missing."""
        ledger = SettlementLedger(root=tmp_settlements_root, db_store=db_store)
        record = _settled_record()
        ledger._append(record)
        records = ledger.read_all()
        assert len(records) == 1
        assert records[0].prediction_id == record.prediction_id
        assert records[0].realized_return_gross == record.realized_return_gross


# ---------------------------------------------------------------------------
# Read-compare on: mismatch
# ---------------------------------------------------------------------------


class TestReadCompareMismatch:
    """When both exist but differ, evidence is 'read_compare_mismatch'."""

    def test_settlement_field_mismatch_direct(
        self,
        read_compare_on: None,
        db_store: DbSettlementStore,
    ) -> None:
        """Field mismatch is detected and evidence includes field diffs."""
        legacy = _settled_record(realized_return_gross=0.05)
        postgres = _settled_record(realized_return_gross=0.06)
        db_store.write(postgres, now_ns=1_700_000_366_000_000_000)
        evidence = read_compare_settlement(legacy, db_store)
        assert evidence.outcome == "read_compare_mismatch"
        assert evidence.legacy_hash != evidence.postgres_hash
        assert "realized_return_gross" in evidence.field_diffs

    def test_settlement_hash_mismatch_direct(
        self,
        read_compare_on: None,
        db_store: DbSettlementStore,
    ) -> None:
        """Different status produces different hash → mismatch."""
        legacy = _settled_record()
        postgres = SettlementRecord(
            prediction_id="pred-001",
            model_id="model-alpha",
            symbol="AAPL",
            ts_event=1_700_000_000_000_000_000,
            horizon_ns=366_000_000_000,
            status=SettlementStatus.PENDING_DATA,
            settled_at_ns=None,
            realized_return_gross=None,
            realized_return_net=None,
            abnormal_return=None,
            brier=None,
            calibration_bucket=None,
            cost_model_version="cm-v1",
            decision_window_start=1_700_000_000_000_000_000,
            decision_window_end=1_700_000_366_000_000_000,
        )
        db_store.write(postgres, now_ns=1_700_000_366_000_000_000)
        evidence = read_compare_settlement(legacy, db_store)
        assert evidence.outcome == "read_compare_mismatch"
        assert "status" in evidence.field_diffs

    def test_mismatch_returns_legacy(
        self,
        read_compare_on: None,
        db_store: DbSettlementStore,
        tmp_settlements_root: pathlib.Path,
    ) -> None:
        """Legacy record is returned even when Postgres differs."""
        ledger = SettlementLedger(root=tmp_settlements_root, db_store=db_store)
        legacy_record = _settled_record(realized_return_gross=0.05)
        postgres_record = _settled_record(realized_return_gross=0.06)
        ledger._append(legacy_record)
        # Write a DIFFERENT record to Postgres
        db_store.write(postgres_record, now_ns=1_700_000_366_000_000_000)
        records = ledger.read_all()
        assert len(records) == 1
        # Legacy value is returned, not Postgres
        assert records[0].realized_return_gross == 0.05
        c = get_counters()
        assert c.mismatches == 1
        assert c.matches == 0

    def test_mismatch_counters(
        self,
        read_compare_on: None,
        db_store: DbSettlementStore,
    ) -> None:
        """Mismatch counter is incremented."""
        legacy = _settled_record(realized_return_gross=0.05)
        postgres = _settled_record(realized_return_gross=0.10)
        db_store.write(postgres, now_ns=1_700_000_366_000_000_000)
        read_compare_settlement(legacy, db_store)
        c = get_counters()
        assert c.mismatches == 1
        assert c.matches == 0


# ---------------------------------------------------------------------------
# Read-compare on: Postgres read error
# ---------------------------------------------------------------------------


class TestReadCompareError:
    """When Postgres read errors, evidence is 'read_compare_error'."""

    def test_postgres_read_error_direct(
        self,
        read_compare_on: None,
    ) -> None:
        """Postgres read error → 'read_compare_error' evidence."""
        broken_store = MagicMock()
        broken_store.get.side_effect = RuntimeError("DB connection refused")
        record = _settled_record()
        evidence = read_compare_settlement(record, broken_store)
        assert evidence.outcome == "read_compare_error"
        assert evidence.error_class == "RuntimeError"
        assert "DB connection refused" in (evidence.error_message or "")
        c = get_counters()
        assert c.errors == 1

    def test_postgres_read_error_returns_legacy(
        self,
        read_compare_on: None,
        tmp_settlements_root: pathlib.Path,
    ) -> None:
        """Legacy record is returned even when Postgres read errors."""
        broken_store = MagicMock()
        broken_store.get.side_effect = RuntimeError("DB connection refused")
        ledger = SettlementLedger(root=tmp_settlements_root, db_store=broken_store)
        record = _settled_record()
        ledger._append(record)
        records = ledger.read_all()
        assert len(records) == 1
        assert records[0].prediction_id == "pred-001"
        c = get_counters()
        assert c.errors == 1

    def test_postgres_read_error_fail_hard(
        self,
        read_compare_on_fail_hard: None,
    ) -> None:
        """In fail-hard mode, Postgres read error re-raises."""
        broken_store = MagicMock()
        broken_store.get.side_effect = RuntimeError("DB connection refused")
        record = _settled_record()
        with pytest.raises(RuntimeError, match="DB connection refused"):
            read_compare_settlement(record, broken_store)


# ---------------------------------------------------------------------------
# Legacy result always returned
# ---------------------------------------------------------------------------


class TestLegacyAlwaysReturned:
    """Legacy result is returned even when comparison fails."""

    def test_legacy_returned_on_match(
        self,
        read_compare_on: None,
        db_store: DbSettlementStore,
        tmp_settlements_root: pathlib.Path,
    ) -> None:
        """Legacy record returned on match."""
        ledger = SettlementLedger(root=tmp_settlements_root, db_store=db_store)
        record = _settled_record()
        ledger._append(record)
        ledger._dual_write(record, now_ns=1_700_000_366_000_000_000)
        records = ledger.read_all()
        assert records[0].realized_return_gross == record.realized_return_gross

    def test_legacy_returned_on_miss(
        self,
        read_compare_on: None,
        db_store: DbSettlementStore,
        tmp_settlements_root: pathlib.Path,
    ) -> None:
        """Legacy record returned on miss."""
        ledger = SettlementLedger(root=tmp_settlements_root, db_store=db_store)
        record = _settled_record()
        ledger._append(record)
        records = ledger.read_all()
        assert records[0].realized_return_gross == record.realized_return_gross

    def test_legacy_returned_on_mismatch(
        self,
        read_compare_on: None,
        db_store: DbSettlementStore,
        tmp_settlements_root: pathlib.Path,
    ) -> None:
        """Legacy record returned on mismatch."""
        ledger = SettlementLedger(root=tmp_settlements_root, db_store=db_store)
        legacy = _settled_record(realized_return_gross=0.05)
        postgres = _settled_record(realized_return_gross=0.99)
        ledger._append(legacy)
        db_store.write(postgres, now_ns=1_700_000_366_000_000_000)
        records = ledger.read_all()
        assert records[0].realized_return_gross == 0.05  # legacy, not 0.99

    def test_legacy_returned_on_error(
        self,
        read_compare_on: None,
        tmp_settlements_root: pathlib.Path,
    ) -> None:
        """Legacy record returned on error."""
        broken_store = MagicMock()
        broken_store.get.side_effect = RuntimeError("DB down")
        ledger = SettlementLedger(root=tmp_settlements_root, db_store=broken_store)
        record = _settled_record()
        ledger._append(record)
        records = ledger.read_all()
        assert records[0].realized_return_gross == record.realized_return_gross


# ---------------------------------------------------------------------------
# No Postgres result returned while reads flag is off
# ---------------------------------------------------------------------------


class TestNoPostgresResultReturned:
    """No Postgres result is returned while QF_POSTGRES_READS_ENABLED=0."""

    def test_reads_flag_off_during_read_compare(
        self,
        read_compare_on: None,
    ) -> None:
        """QF_POSTGRES_READS_ENABLED is off during read-compare."""
        from quant_foundry.c10_flags import should_read_from_postgres

        assert should_read_from_postgres() is False

    def test_read_all_returns_jsonl_not_postgres(
        self,
        read_compare_on: None,
        db_store: DbSettlementStore,
        tmp_settlements_root: pathlib.Path,
    ) -> None:
        """read_all() returns JSONL records, not Postgres records."""
        ledger = SettlementLedger(root=tmp_settlements_root, db_store=db_store)
        legacy = _settled_record(realized_return_gross=0.05)
        postgres = _settled_record(realized_return_gross=0.99)
        ledger._append(legacy)
        db_store.write(postgres, now_ns=1_700_000_366_000_000_000)
        records = ledger.read_all()
        # Must return legacy (0.05), not Postgres (0.99)
        assert records[0].realized_return_gross == 0.05


# ---------------------------------------------------------------------------
# Structured evidence
# ---------------------------------------------------------------------------


class TestStructuredEvidence:
    """Comparison emits structured evidence."""

    def test_evidence_to_dict(self) -> None:
        """ReadCompareEvidence.to_dict() produces a valid dict."""
        ev = ReadCompareEvidence(
            outcome="match",
            record_type="settlement_record",
            record_key="pred-001:cm-v1",
            legacy_hash="abc123",
            postgres_hash="abc123",
        )
        d = ev.to_dict()
        assert d["outcome"] == "match"
        assert d["record_type"] == "settlement_record"
        assert d["record_key"] == "pred-001:cm-v1"
        assert d["legacy_hash"] == "abc123"
        assert d["postgres_hash"] == "abc123"

    def test_evidence_to_json(self) -> None:
        """ReadCompareEvidence.to_json() produces valid JSON."""
        import json

        ev = ReadCompareEvidence(
            outcome="read_compare_mismatch",
            record_type="settlement_record",
            record_key="pred-001:cm-v1",
            legacy_hash="abc",
            postgres_hash="def",
            field_diffs={"realized_return_gross": (0.05, 0.06)},
        )
        d = json.loads(ev.to_json())
        assert d["outcome"] == "read_compare_mismatch"
        assert d["field_diffs"]["realized_return_gross"] == [0.05, 0.06]

    def test_evidence_miss_to_dict(self) -> None:
        """Miss evidence to_dict has postgres_hash=None."""
        ev = ReadCompareEvidence(
            outcome="read_compare_miss",
            record_type="settlement_record",
            record_key="pred-001:cm-v1",
            legacy_hash="abc",
        )
        d = ev.to_dict()
        assert d["postgres_hash"] is None

    def test_evidence_error_to_dict(self) -> None:
        """Error evidence to_dict has error_class and error_message."""
        ev = ReadCompareEvidence(
            outcome="read_compare_error",
            record_type="settlement_record",
            record_key="pred-001:cm-v1",
            error_class="RuntimeError",
            error_message="DB down",
        )
        d = ev.to_dict()
        assert d["error_class"] == "RuntimeError"
        assert d["error_message"] == "DB down"


# ---------------------------------------------------------------------------
# Settlement replay: 0 divergences
# ---------------------------------------------------------------------------


class TestSettlementReplayZeroDivergences:
    """Settlement replay with read-compare produces 0 divergences."""

    def test_replay_zero_divergences(
        self,
        read_compare_on: None,
        db_store: DbSettlementStore,
        tmp_settlements_root: pathlib.Path,
    ) -> None:
        """10 records dual-written, read-compare shows 0 mismatches."""
        ledger = SettlementLedger(root=tmp_settlements_root, db_store=db_store)
        now_ns = 1_700_000_366_000_000_000
        for i in range(10):
            record = _settled_record(prediction_id=f"pred-{i:03d}")
            ledger._append(record)
            ledger._dual_write(record, now_ns=now_ns + i)
        records = ledger.read_all()
        assert len(records) == 10
        c = get_counters()
        assert c.matches == 10
        assert c.mismatches == 0
        assert c.misses == 0
        assert c.errors == 0


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


class TestNormalization:
    """Normalization rules produce fair comparisons."""

    def test_normalize_settlement_record(self) -> None:
        """normalize_settlement_record produces a normalized dict."""
        record = _settled_record()
        norm = normalize_settlement_record(record)
        assert norm["prediction_id"] == "pred-001"
        assert norm["status"] == "settled"
        assert norm["realized_return_gross"] == 0.05
        assert norm["ts_event"] == 1_700_000_000_000_000_000

    def test_float_precision_normalization(self) -> None:
        """Float precision differences within 12 decimal places are normalized away."""
        r1 = _settled_record(realized_return_gross=0.050000000000001)
        r2 = _settled_record(realized_return_gross=0.05)
        ev = compare_settlement_records(r1, r2)
        # After rounding to 12 places, they should match
        assert ev.outcome == "match"

    def test_status_string_normalization(self) -> None:
        """Status strings are compared as string values."""
        r1 = _settled_record()
        r2 = _settled_record()
        ev = compare_settlement_records(r1, r2)
        assert ev.outcome == "match"

    def test_null_fields_preserved(self) -> None:
        """None fields are preserved in normalization."""
        record = SettlementRecord(
            prediction_id="pred-pending",
            model_id="model-alpha",
            symbol="AAPL",
            ts_event=1_700_000_000_000_000_000,
            horizon_ns=366_000_000_000,
            status=SettlementStatus.PENDING_TIME,
            settled_at_ns=None,
            realized_return_gross=None,
            realized_return_net=None,
            abnormal_return=None,
            brier=None,
            calibration_bucket=None,
            cost_model_version="cm-v1",
            decision_window_start=1_700_000_000_000_000_000,
            decision_window_end=1_700_000_366_000_000_000,
        )
        norm = normalize_settlement_record(record)
        assert norm["settled_at_ns"] is None
        assert norm["realized_return_gross"] is None
        assert norm["brier"] is None


# ---------------------------------------------------------------------------
# Dict-based read-compare (callback receipts, dossiers, metrics)
# ---------------------------------------------------------------------------


class TestDictReadCompare:
    """Dict-based read-compare for callback receipts, dossiers, model metrics."""

    def test_dict_match(self, read_compare_on: None) -> None:
        """Dict read-compare match."""
        legacy = {"callback_id": "cb-001", "status": "completed", "job_id": "job-001"}
        db_getter = MagicMock(
            return_value={"callback_id": "cb-001", "status": "completed", "job_id": "job-001"}
        )
        ev = read_compare_dict(
            legacy,
            db_getter,
            record_type="callback_receipt",
            record_key="cb-001",
        )
        assert ev.outcome == "match"

    def test_dict_mismatch(self, read_compare_on: None) -> None:
        """Dict read-compare mismatch."""
        legacy = {"callback_id": "cb-001", "status": "completed"}
        db_getter = MagicMock(return_value={"callback_id": "cb-001", "status": "failed"})
        ev = read_compare_dict(
            legacy,
            db_getter,
            record_type="callback_receipt",
            record_key="cb-001",
        )
        assert ev.outcome == "read_compare_mismatch"
        assert "status" in ev.field_diffs

    def test_dict_missing(self, read_compare_on: None) -> None:
        """Dict read-compare miss."""
        legacy = {"callback_id": "cb-001", "status": "completed"}
        db_getter = MagicMock(return_value=None)
        ev = read_compare_dict(
            legacy,
            db_getter,
            record_type="callback_receipt",
            record_key="cb-001",
        )
        assert ev.outcome == "read_compare_miss"

    def test_dict_error(self, read_compare_on: None) -> None:
        """Dict read-compare error."""
        legacy = {"callback_id": "cb-001", "status": "completed"}
        db_getter = MagicMock(side_effect=RuntimeError("DB down"))
        ev = read_compare_dict(
            legacy,
            db_getter,
            record_type="callback_receipt",
            record_key="cb-001",
        )
        assert ev.outcome == "read_compare_error"
        assert ev.error_class == "RuntimeError"

    def test_dict_float_normalization(self, read_compare_on: None) -> None:
        """Float fields are normalized in dict read-compare."""
        legacy = {"metric_id": "m-001", "sharpe": 1.5000000000001}
        db_getter = MagicMock(return_value={"metric_id": "m-001", "sharpe": 1.5})
        ev = read_compare_dict(
            legacy,
            db_getter,
            record_type="model_metric",
            record_key="m-001",
            float_fields={"sharpe"},
        )
        assert ev.outcome == "match"

    def test_dict_error_fail_hard(self, read_compare_on_fail_hard: None) -> None:
        """Dict read-compare error re-raises in fail-hard mode."""
        legacy = {"callback_id": "cb-001", "status": "completed"}
        db_getter = MagicMock(side_effect=RuntimeError("DB down"))
        with pytest.raises(RuntimeError, match="DB down"):
            read_compare_dict(
                legacy,
                db_getter,
                record_type="callback_receipt",
                record_key="cb-001",
            )


# ---------------------------------------------------------------------------
# Batch read-compare
# ---------------------------------------------------------------------------


class TestBatchReadCompare:
    """Batch read-compare for multiple records."""

    def test_batch_mixed_outcomes(
        self,
        read_compare_on: None,
        db_store: DbSettlementStore,
    ) -> None:
        """Batch with match, miss, and mismatch."""
        r1 = _settled_record(prediction_id="pred-001")  # will match
        r2 = _settled_record(prediction_id="pred-002")  # will miss (not in DB)
        r3 = _settled_record(prediction_id="pred-003", realized_return_gross=0.05)  # legacy
        r3_pg = _settled_record(prediction_id="pred-003", realized_return_gross=0.99)  # pg differs

        db_store.write(r1, now_ns=1_700_000_366_000_000_000)
        db_store.write(r3_pg, now_ns=1_700_000_366_000_000_000)

        results = read_compare_settlement_batch([r1, r2, r3], db_store)
        assert len(results) == 3
        outcomes = [r.outcome for r in results]
        assert "match" in outcomes
        assert "read_compare_miss" in outcomes
        assert "read_compare_mismatch" in outcomes

        c = get_counters()
        assert c.matches == 1
        assert c.misses == 1
        assert c.mismatches == 1
