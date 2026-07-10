"""Tests for migration 0008_settlement_records — revision chain and ORM metadata.

Verifies:
  - Migration 0008 imports cleanly.
  - Revision chain: 0008.down_revision == '0007'.
  - ORM metadata includes the settlement_records table.
  - SettlementRecordRow is registered on Base.metadata.
"""

from __future__ import annotations

import importlib


def test_migration_0008_imports() -> None:
    """Migration 0008 module imports without error."""
    mod = importlib.import_module("fincept_db.migrations.versions.0008_settlement_records")
    assert mod is not None


def test_migration_0008_revision_chain() -> None:
    """Migration 0008 revises 0007 (the current head)."""
    mod = importlib.import_module("fincept_db.migrations.versions.0008_settlement_records")
    assert mod.revision == "0008"
    assert mod.down_revision == "0007"


def test_settlement_records_table_in_metadata() -> None:
    """Base.metadata includes the settlement_records table."""
    from fincept_db.models import Base
    from fincept_db.settlement_tables import SettlementRecordRow

    assert "settlement_records" in Base.metadata.tables
    table = Base.metadata.tables["settlement_records"]
    # Primary key
    assert "settlement_id" in table.columns
    assert table.columns["settlement_id"].primary_key
    # Required columns
    for col in (
        "prediction_id",
        "model_id",
        "symbol",
        "ts_event",
        "horizon_ns",
        "status",
        "cost_model_version",
        "decision_window_start",
        "decision_window_end",
        "created_at_ns",
    ):
        assert col in table.columns, f"missing column: {col}"
    # Nullable columns
    for col in (
        "settled_at_ns",
        "realized_return_gross",
        "realized_return_net",
        "abnormal_return",
        "brier",
        "calibration_bucket",
    ):
        assert col in table.columns, f"missing nullable column: {col}"
    # Unique constraint on (prediction_id, cost_model_version)
    unique_cols = {
        tuple(c.name for c in uc.columns)
        for uc in table.constraints
        if uc.__class__.__name__ == "UniqueConstraint"
    }
    assert ("prediction_id", "cost_model_version") in unique_cols
    # SettlementRecordRow is a mapped class
    assert SettlementRecordRow.__tablename__ == "settlement_records"


def test_settlement_records_indexes_in_metadata() -> None:
    """Base.metadata includes the settlement_records indexes."""
    from fincept_db.models import Base

    table = Base.metadata.tables["settlement_records"]
    index_names = {idx.name for idx in table.indexes}
    assert "ix_settlement_records_model_id_ts" in index_names
    assert "ix_settlement_records_symbol_ts" in index_names
    assert "ix_settlement_records_status" in index_names
    assert "ix_settlement_records_prediction_id" in index_names
    assert "ix_settlement_records_cost_model_version" in index_names


def test_settlement_records_check_constraints_in_metadata() -> None:
    """Base.metadata includes the settlement_records CHECK constraints."""
    from fincept_db.models import Base

    table = Base.metadata.tables["settlement_records"]
    check_names = {c.name for c in table.constraints if c.__class__.__name__ == "CheckConstraint"}
    assert "ck_settlement_records_status_domain" in check_names
    assert "ck_settlement_records_cost_model_version_domain" in check_names
    assert "ck_settlement_records_calibration_bucket_domain" in check_names
