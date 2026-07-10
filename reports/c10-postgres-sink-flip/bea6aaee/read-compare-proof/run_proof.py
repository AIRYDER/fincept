#!/usr/bin/env python3
"""C10 read-compare proof — deterministic verification.

This script proves that read-compare mode correctly:
  1. Matches records that are identical after normalization.
  2. Detects missing Postgres records (read_compare_miss).
  3. Detects field-level mismatches (read_compare_mismatch).
  4. Handles Postgres read errors (read_compare_error).
  5. Always returns the legacy record to the caller.

The proof uses an in-memory SQLite database and deterministic fixtures.
No external services are required.

Run:
  uv run python reports/c10-postgres-sink-flip/bea6aaee/read-compare-proof/run_proof.py
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile

# Ensure the project is importable.
project_root = pathlib.Path(__file__).resolve().parents[4]
sys.path.insert(0, str(project_root / "services" / "quant_foundry" / "src"))
sys.path.insert(0, str(project_root / "libs" / "fincept-db" / "src"))
sys.path.insert(0, str(project_root / "libs" / "fincept-core" / "src"))

# Enable read-compare mode.
os.environ["QF_POSTGRES_SINK_ENABLED"] = "1"
os.environ["QF_DUAL_WRITE_SETTLEMENTS"] = "1"
os.environ["QF_POSTGRES_READ_COMPARE_ENABLED"] = "1"
os.environ["QF_POSTGRES_READS_ENABLED"] = "0"
os.environ["QF_LEGACY_FILE_READ_FALLBACK"] = "1"
os.environ["QF_DUAL_WRITE_FAIL_HARD"] = "0"

from sqlalchemy import create_engine  # noqa: E402

from fincept_db.models import Base  # noqa: E402
from fincept_db.settlement_tables import SettlementRecordRow  # noqa: E402
from quant_foundry.outcomes import SettlementRecord, SettlementStatus  # noqa: E402
from quant_foundry.read_compare import (  # noqa: E402
    get_counters,
    read_compare_settlement,
    reset_counters,
)
from quant_foundry.settlement import SettlementLedger  # noqa: E402
from quant_foundry.settlement_db_sink import DbSettlementStore  # noqa: E402

PROOF_DIR = pathlib.Path(__file__).resolve().parent


def _settled_record(
    *,
    prediction_id: str = "pred-001",
    realized_return_gross: float = 0.05,
) -> SettlementRecord:
    return SettlementRecord(
        prediction_id=prediction_id,
        model_id="model-alpha",
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
        cost_model_version="cm-v1",
        decision_window_start=1_700_000_000_000_000_000,
        decision_window_end=1_700_000_366_000_000_000,
    )


def _make_engine():
    eng = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(eng, tables=[SettlementRecordRow.__table__])
    return eng


def proof_match() -> dict:
    """Proof: 10 records dual-written, read-compare shows all matches."""
    reset_counters()
    engine = _make_engine()
    db_store = DbSettlementStore(engine=engine)
    with tempfile.TemporaryDirectory() as tmpdir:
        ledger = SettlementLedger(root=pathlib.Path(tmpdir), db_store=db_store)
        now_ns = 1_700_000_366_000_000_000
        for i in range(10):
            record = _settled_record(prediction_id=f"pred-{i:03d}")
            ledger._append(record)
            ledger._dual_write(record, now_ns=now_ns + i)

        records = ledger.read_all()
        c = get_counters()

        result = {
            "proof": "match",
            "records_written": 10,
            "records_read": len(records),
            "matches": c.matches,
            "misses": c.misses,
            "mismatches": c.mismatches,
            "errors": c.errors,
            "pass": c.matches == 10 and c.mismatches == 0 and c.misses == 0 and c.errors == 0,
            "legacy_returned": all(r.prediction_id.startswith("pred-") for r in records),
        }
    engine.dispose()
    return result


def proof_missing_postgres() -> dict:
    """Proof: legacy records exist but Postgres is empty → all misses."""
    reset_counters()
    engine = _make_engine()
    db_store = DbSettlementStore(engine=engine)
    with tempfile.TemporaryDirectory() as tmpdir:
        ledger = SettlementLedger(root=pathlib.Path(tmpdir), db_store=db_store)
        for i in range(5):
            record = _settled_record(prediction_id=f"pred-miss-{i:03d}")
            ledger._append(record)
            # Do NOT dual-write — Postgres is empty

        records = ledger.read_all()
        c = get_counters()

        result = {
            "proof": "missing_postgres",
            "records_written_to_jsonl": 5,
            "records_in_postgres": db_store.count(),
            "records_read": len(records),
            "matches": c.matches,
            "misses": c.misses,
            "mismatches": c.mismatches,
            "errors": c.errors,
            "pass": c.misses == 5 and c.matches == 0,
            "legacy_returned": len(records) == 5,
        }
    engine.dispose()
    return result


def proof_mismatch() -> dict:
    """Proof: legacy and Postgres differ → mismatch detected, legacy returned."""
    reset_counters()
    engine = _make_engine()
    db_store = DbSettlementStore(engine=engine)
    with tempfile.TemporaryDirectory() as tmpdir:
        ledger = SettlementLedger(root=pathlib.Path(tmpdir), db_store=db_store)
        # Write 3 legacy records
        for i in range(3):
            legacy = _settled_record(
                prediction_id=f"pred-mismatch-{i:03d}",
                realized_return_gross=0.05,
            )
            ledger._append(legacy)
            # Write a DIFFERENT record to Postgres
            postgres = _settled_record(
                prediction_id=f"pred-mismatch-{i:03d}",
                realized_return_gross=0.99,
            )
            db_store.write(postgres, now_ns=1_700_000_366_000_000_000)

        records = ledger.read_all()
        c = get_counters()

        # Verify legacy values are returned, not Postgres
        all_legacy = all(r.realized_return_gross == 0.05 for r in records)

        result = {
            "proof": "mismatch",
            "records_written": 3,
            "records_read": len(records),
            "matches": c.matches,
            "misses": c.misses,
            "mismatches": c.mismatches,
            "errors": c.errors,
            "pass": c.mismatches == 3 and c.matches == 0,
            "legacy_returned": all_legacy,
            "postgres_values_not_returned": all(r.realized_return_gross != 0.99 for r in records),
        }
    engine.dispose()
    return result


def proof_error() -> dict:
    """Proof: Postgres read errors → error evidence, legacy returned."""
    reset_counters()
    from unittest.mock import MagicMock

    broken_store = MagicMock()
    broken_store.get.side_effect = RuntimeError("Simulated DB connection refused")
    with tempfile.TemporaryDirectory() as tmpdir:
        ledger = SettlementLedger(root=pathlib.Path(tmpdir), db_store=broken_store)
        for i in range(3):
            record = _settled_record(prediction_id=f"pred-err-{i:03d}")
            ledger._append(record)

        records = ledger.read_all()
        c = get_counters()

        result = {
            "proof": "error",
            "records_written": 3,
            "records_read": len(records),
            "matches": c.matches,
            "misses": c.misses,
            "mismatches": c.mismatches,
            "errors": c.errors,
            "pass": c.errors == 3 and c.matches == 0,
            "legacy_returned": len(records) == 3,
        }
    return result


def main() -> None:
    print("=" * 70)
    print("C10 Read-Compare Mode — Deterministic Proof")
    print("=" * 70)
    print()

    results = {}

    # 1. Match proof
    print("[1/4] Match proof: 10 records, all should match...")
    r = proof_match()
    results["match"] = r
    status = "PASS" if r["pass"] else "FAIL"
    print(
        f"  -> {status}: matches={r['matches']}, misses={r['misses']}, "
        f"mismatches={r['mismatches']}, errors={r['errors']}"
    )
    print()

    # 2. Missing Postgres proof
    print("[2/4] Missing Postgres proof: 5 legacy records, Postgres empty...")
    r = proof_missing_postgres()
    results["missing_postgres"] = r
    status = "PASS" if r["pass"] else "FAIL"
    print(
        f"  -> {status}: misses={r['misses']}, matches={r['matches']}, "
        f"legacy_returned={r['legacy_returned']}"
    )
    print()

    # 3. Mismatch proof
    print("[3/4] Mismatch proof: 3 records, Postgres differs...")
    r = proof_mismatch()
    results["mismatch"] = r
    status = "PASS" if r["pass"] else "FAIL"
    print(
        f"  -> {status}: mismatches={r['mismatches']}, matches={r['matches']}, "
        f"legacy_returned={r['legacy_returned']}, "
        f"pg_values_not_returned={r['postgres_values_not_returned']}"
    )
    print()

    # 4. Error proof
    print("[4/4] Error proof: 3 records, Postgres read errors...")
    r = proof_error()
    results["error"] = r
    status = "PASS" if r["pass"] else "FAIL"
    print(
        f"  -> {status}: errors={r['errors']}, matches={r['matches']}, "
        f"legacy_returned={r['legacy_returned']}"
    )
    print()

    # Overall
    all_pass = all(r["pass"] for r in results.values())
    print("=" * 70)
    print(f"OVERALL: {'ALL PASS' if all_pass else 'FAILURES DETECTED'}")
    print("=" * 70)

    # Save JSON evidence files
    for name, data in results.items():
        path = PROOF_DIR / f"read_compare_{name}.json"
        with path.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        print(f"  Saved: {path}")

    # Save summary
    summary = {
        "overall_pass": all_pass,
        "proofs": {
            name: {
                "pass": data["pass"],
                "matches": data.get("matches", 0),
                "misses": data.get("misses", 0),
                "mismatches": data.get("mismatches", 0),
                "errors": data.get("errors", 0),
            }
            for name, data in results.items()
        },
    }
    summary_path = PROOF_DIR / "summary.json"
    with summary_path.open("w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, sort_keys=True)
    print(f"  Saved: {summary_path}")

    sys.exit(0 if all_pass else 1)


if __name__ == "__main__":
    main()
