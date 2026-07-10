"""Tests for quant_foundry.c10_flags — feature flags default to safe legacy mode.

Verifies:
  - All flags default to safe legacy mode (no Postgres, JSONL only).
  - should_write_to_postgres() is False by default.
  - should_read_from_postgres() is False by default.
  - should_write_to_jsonl() is True by default.
  - legacy_file_read_fallback() is True by default (rollback available).
  - Flag combinations behave correctly.
  - Legacy behavior is unchanged when all flags are off.
"""

from __future__ import annotations

import pytest
from quant_foundry import c10_flags

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Default values — safe legacy mode
# ---------------------------------------------------------------------------


def test_postgres_sink_enabled_defaults_off(clean_env: None) -> None:
    """QF_POSTGRES_SINK_ENABLED defaults to 0 (off)."""
    assert c10_flags.postgres_sink_enabled() is False


def test_postgres_reads_enabled_defaults_off(clean_env: None) -> None:
    """QF_POSTGRES_READS_ENABLED defaults to 0 (off)."""
    assert c10_flags.postgres_reads_enabled() is False


def test_dual_write_settlements_defaults_off(clean_env: None) -> None:
    """QF_DUAL_WRITE_SETTLEMENTS defaults to 0 (off)."""
    assert c10_flags.dual_write_settlements() is False


def test_legacy_file_read_fallback_defaults_on(clean_env: None) -> None:
    """QF_LEGACY_FILE_READ_FALLBACK defaults to 1 (on — safe rollback)."""
    assert c10_flags.legacy_file_read_fallback() is True


def test_postgres_read_compare_enabled_defaults_off(clean_env: None) -> None:
    """QF_POSTGRES_READ_COMPARE_ENABLED defaults to 0 (off)."""
    assert c10_flags.postgres_read_compare_enabled() is False


def test_dual_write_fail_hard_defaults_off(clean_env: None) -> None:
    """QF_DUAL_WRITE_FAIL_HARD defaults to 0 (off)."""
    assert c10_flags.dual_write_fail_hard() is False


def test_should_read_compare_defaults_false(clean_env: None) -> None:
    """should_read_compare() is False by default."""
    assert c10_flags.should_read_compare() is False


# ---------------------------------------------------------------------------
# Derived helpers
# ---------------------------------------------------------------------------


def test_should_write_to_postgres_defaults_false(clean_env: None) -> None:
    """should_write_to_postgres() is False by default."""
    assert c10_flags.should_write_to_postgres() is False


def test_should_read_from_postgres_defaults_false(clean_env: None) -> None:
    """should_read_from_postgres() is False by default."""
    assert c10_flags.should_read_from_postgres() is False


def test_should_write_to_jsonl_defaults_true(clean_env: None) -> None:
    """should_write_to_jsonl() is True by default (JSONL is the only writer)."""
    assert c10_flags.should_write_to_jsonl() is True


# ---------------------------------------------------------------------------
# Flag combinations
# ---------------------------------------------------------------------------


def test_sink_on_reads_off(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Sink on, reads off → write to Postgres, read from JSONL, write to JSONL (dual-write off means JSONL only? No — dual-write defaults off but sink is on).

    When sink is on and dual_write is off (default), should_write_to_jsonl()
    returns False — Postgres is the only writer. This is Phase 7 (retire
    legacy writes), which is NOT the default.
    """
    monkeypatch.setenv("QF_POSTGRES_SINK_ENABLED", "1")
    # dual_write defaults to 0
    assert c10_flags.should_write_to_postgres() is True
    assert c10_flags.should_read_from_postgres() is False  # reads still off
    # When sink is on but dual_write is off, JSONL writes stop
    assert c10_flags.should_write_to_jsonl() is False


def test_sink_on_dual_write_on(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Sink on, dual-write on → write to both Postgres and JSONL, read from JSONL."""
    monkeypatch.setenv("QF_POSTGRES_SINK_ENABLED", "1")
    monkeypatch.setenv("QF_DUAL_WRITE_SETTLEMENTS", "1")
    assert c10_flags.should_write_to_postgres() is True
    assert c10_flags.should_write_to_jsonl() is True
    assert c10_flags.should_read_from_postgres() is False  # reads still off


def test_sink_on_reads_on_fallback_on(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Sink on, reads on, fallback on → reads from JSONL (rollback)."""
    monkeypatch.setenv("QF_POSTGRES_SINK_ENABLED", "1")
    monkeypatch.setenv("QF_POSTGRES_READS_ENABLED", "1")
    # fallback defaults to 1
    assert c10_flags.postgres_reads_enabled() is True
    assert c10_flags.legacy_file_read_fallback() is True
    assert c10_flags.should_read_from_postgres() is False  # fallback wins


def test_sink_on_reads_on_fallback_off(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Sink on, reads on, fallback off → reads from Postgres."""
    monkeypatch.setenv("QF_POSTGRES_SINK_ENABLED", "1")
    monkeypatch.setenv("QF_POSTGRES_READS_ENABLED", "1")
    monkeypatch.setenv("QF_LEGACY_FILE_READ_FALLBACK", "0")
    assert c10_flags.should_read_from_postgres() is True
    assert c10_flags.should_write_to_postgres() is True


def test_full_postgres_mode(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """All flags set for full Postgres mode (Phase 7)."""
    monkeypatch.setenv("QF_POSTGRES_SINK_ENABLED", "1")
    monkeypatch.setenv("QF_POSTGRES_READS_ENABLED", "1")
    monkeypatch.setenv("QF_DUAL_WRITE_SETTLEMENTS", "0")
    monkeypatch.setenv("QF_LEGACY_FILE_READ_FALLBACK", "0")
    assert c10_flags.should_write_to_postgres() is True
    assert c10_flags.should_read_from_postgres() is True
    assert c10_flags.should_write_to_jsonl() is False


def test_read_compare_on_reads_off(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Read-compare on, reads off → compare but don't serve Postgres."""
    monkeypatch.setenv("QF_POSTGRES_READ_COMPARE_ENABLED", "1")
    assert c10_flags.should_read_compare() is True
    assert c10_flags.should_read_from_postgres() is False  # reads still off


def test_read_compare_on_reads_on(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Read-compare on + reads on → read-compare is moot (reads already flipped)."""
    monkeypatch.setenv("QF_POSTGRES_READ_COMPARE_ENABLED", "1")
    monkeypatch.setenv("QF_POSTGRES_READS_ENABLED", "1")
    monkeypatch.setenv("QF_LEGACY_FILE_READ_FALLBACK", "0")
    assert c10_flags.should_read_compare() is True
    assert c10_flags.should_read_from_postgres() is True


# ---------------------------------------------------------------------------
# Truthy values
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "TRUE", "Yes"])
def test_truthy_values(clean_env: None, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """Various truthy strings enable the flag."""
    monkeypatch.setenv("QF_POSTGRES_SINK_ENABLED", value)
    assert c10_flags.postgres_sink_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "", "random"])
def test_falsy_values(clean_env: None, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """Various falsy strings keep the flag off."""
    monkeypatch.setenv("QF_POSTGRES_SINK_ENABLED", value)
    assert c10_flags.postgres_sink_enabled() is False
