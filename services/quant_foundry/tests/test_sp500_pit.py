"""
Tests for the point-in-time S&P 500 universe selector
(``universe:sp500-pit:1.0.0``).

These tests verify:
- The PIT module is registered in the central registry.
- Base (2018) constituents are returned for a 2018 date range.
- Stocks added during/after the range are handled correctly (no
  survivorship bias: future additions excluded; in-period additions
  included).
- Stocks removed during the range are still included (they were members
  for part of the period — no exclusion bias).
- The changes JSON file loads and has the expected structure.
- The helper functions ``_constituents_at_time`` and
  ``_constituents_during_range`` behave correctly.
- The original static ``universe:sp500:1.0.0`` still works.
- ``max_symbols`` config limits the output.
"""

from __future__ import annotations

import datetime as dt
import pathlib

# Path setup matching test_modules.py
import sys

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #

NS_PER_DAY = 86_400_000_000_000


def _ns(year: int, month: int, day: int) -> int:
    """Convert a calendar date to UTC nanoseconds since epoch."""
    return int(dt.datetime(year, month, day, tzinfo=dt.UTC).timestamp()) * 1_000_000_000


@pytest.fixture(scope="module")
def registry():
    """Load all modules once and return the singleton registry."""
    from quant_foundry.modules import ModuleRegistry, load_all_modules

    load_all_modules()
    return ModuleRegistry.instance()


@pytest.fixture(scope="module")
def changes_data():
    """Load the bundled sp500_changes.json once for helper-function tests."""
    from quant_foundry.modules.universe.sp500 import (
        _DEFAULT_CHANGES_PATH,
        _load_sp500_changes,
    )

    return _load_sp500_changes(_DEFAULT_CHANGES_PATH)


# --------------------------------------------------------------------------- #
# Registration + backward-compat tests                                         #
# --------------------------------------------------------------------------- #


def test_sp500_pit_registered(registry) -> None:
    """``universe:sp500-pit:1.0.0`` is registered in the universe category."""
    universe_modules = registry.list_by_category("universe")
    assert "universe:sp500-pit:1.0.0" in universe_modules


def test_sp500_static_still_works(registry) -> None:
    """The original static ``universe:sp500:1.0.0`` still works (backward compat)."""
    mod = registry.create("universe:sp500:1.0.0")
    symbols = mod.select_symbols(start_ns=0, end_ns=1)
    assert len(symbols) > 0
    assert "AAPL" in symbols
    # The static list always includes TSLA (current constituent) regardless
    # of the date range — this is the survivorship bias the PIT module fixes.
    assert "TSLA" in symbols


# --------------------------------------------------------------------------- #
# Changes-file structure test                                                  #
# --------------------------------------------------------------------------- #


def test_sp500_pit_changes_file_loads(changes_data) -> None:
    """The bundled changes JSON loads and has the expected structure."""
    assert isinstance(changes_data, dict)
    assert "base_constituents_2018" in changes_data
    assert isinstance(changes_data["base_constituents_2018"], list)
    assert len(changes_data["base_constituents_2018"]) > 0
    assert all(isinstance(t, str) for t in changes_data["base_constituents_2018"])

    assert "changes" in changes_data
    assert isinstance(changes_data["changes"], list)
    assert len(changes_data["changes"]) >= 20  # at least 20-30 real changes

    for entry in changes_data["changes"]:
        assert "date" in entry and isinstance(entry["date"], str)
        assert "added" in entry and isinstance(entry["added"], list)
        assert "removed" in entry and isinstance(entry["removed"], list)

    # Spot-check a known change: TSLA added in 2020.
    tsla_additions = [e for e in changes_data["changes"] if "TSLA" in e.get("added", [])]
    assert len(tsla_additions) >= 1
    assert any(e["date"].startswith("2020") for e in tsla_additions)


# --------------------------------------------------------------------------- #
# Module-level select_symbols tests                                            #
# --------------------------------------------------------------------------- #


def test_sp500_pit_base_constituents(registry) -> None:
    """For a 2018 date range, returns the base constituents (no future additions)."""
    mod = registry.create("universe:sp500-pit:1.0.0")
    start = _ns(2018, 1, 2)
    end = _ns(2018, 12, 31)
    symbols = set(mod.select_symbols(start_ns=start, end_ns=end))

    # Core base tickers present.
    assert "AAPL" in symbols
    assert "MSFT" in symbols
    assert "SNAP" in symbols  # SNAP was a member in 2018 (removed May 2019)
    assert "X" in symbols  # US Steel was a member in 2018 (removed Sep 2020)

    # Future additions NOT present (survivorship bias eliminated).
    assert "TSLA" not in symbols  # added Sep 2020
    assert "ZM" not in symbols  # added May 2019
    assert "ABNB" not in symbols  # added 2022
    assert "PLTR" not in symbols  # added 2024


def test_sp500_pit_includes_added_stocks(registry) -> None:
    """For a range after TSLA was added (2020), TSLA is included."""
    mod = registry.create("universe:sp500-pit:1.0.0")
    # Range entirely after the TSLA addition (2020-09-01).
    start = _ns(2021, 1, 1)
    end = _ns(2021, 6, 30)
    symbols = set(mod.select_symbols(start_ns=start, end_ns=end))
    assert "TSLA" in symbols

    # Range that starts before and ends after the addition — TSLA still
    # included because it was a member for part of the period.
    start = _ns(2020, 1, 1)
    end = _ns(2020, 12, 31)
    symbols = set(mod.select_symbols(start_ns=start, end_ns=end))
    assert "TSLA" in symbols


def test_sp500_pit_includes_removed_stocks(registry) -> None:
    """For a range that spans a removal, the removed stock is still included.

    SNAP was removed on 2019-05-01.  A range Jan–Jun 2019 should still
    include SNAP (it was a member Jan–Apr).  This avoids exclusion bias.
    """
    mod = registry.create("universe:sp500-pit:1.0.0")
    start = _ns(2019, 1, 1)
    end = _ns(2019, 6, 30)
    symbols = set(mod.select_symbols(start_ns=start, end_ns=end))
    assert "SNAP" in symbols  # removed mid-range but was a member earlier

    # X (US Steel) removed 2020-09-01; a range spanning that date includes X.
    start = _ns(2020, 1, 1)
    end = _ns(2020, 12, 31)
    symbols = set(mod.select_symbols(start_ns=start, end_ns=end))
    assert "X" in symbols


def test_sp500_pit_excludes_future_additions(registry) -> None:
    """For a 2019 range, stocks added in 2020+ are NOT included."""
    mod = registry.create("universe:sp500-pit:1.0.0")
    start = _ns(2019, 1, 1)
    end = _ns(2019, 12, 31)
    symbols = set(mod.select_symbols(start_ns=start, end_ns=end))

    # TSLA added 2020-09-01 — after the 2019 range ends.
    assert "TSLA" not in symbols
    # ABNB added 2022 — after the range.
    assert "ABNB" not in symbols
    # PLTR added 2024 — after the range.
    assert "PLTR" not in symbols
    # DASH added 2024 — after the range.
    assert "DASH" not in symbols

    # But ZM (added 2019-05-01, within the range) IS included.
    assert "ZM" in symbols


def test_sp500_pit_excludes_pre_range_removals(registry) -> None:
    """A stock removed before the range starts is NOT included."""
    mod = registry.create("universe:sp500-pit:1.0.0")
    # SNAP removed 2019-05-01.  A range starting in 2020 should exclude it.
    start = _ns(2020, 1, 1)
    end = _ns(2020, 6, 30)
    symbols = set(mod.select_symbols(start_ns=start, end_ns=end))
    assert "SNAP" not in symbols


def test_sp500_pit_max_symbols_config(registry) -> None:
    """``max_symbols`` config limits the number of returned symbols."""
    mod = registry.create("universe:sp500-pit:1.0.0", config={"max_symbols": 10})
    start = _ns(2018, 1, 2)
    end = _ns(2018, 12, 31)
    symbols = mod.select_symbols(start_ns=start, end_ns=end)
    assert len(symbols) == 10
    assert all(isinstance(s, str) for s in symbols)


def test_sp500_pit_returns_sorted_unique(registry) -> None:
    """The returned list is sorted and contains no duplicates."""
    mod = registry.create("universe:sp500-pit:1.0.0")
    start = _ns(2020, 1, 1)
    end = _ns(2020, 12, 31)
    symbols = mod.select_symbols(start_ns=start, end_ns=end)
    assert symbols == sorted(set(symbols))


# --------------------------------------------------------------------------- #
# Helper-function tests                                                        #
# --------------------------------------------------------------------------- #


def test_constituents_at_time(changes_data) -> None:
    """``_constituents_at_time`` returns the member set at a single instant."""
    from quant_foundry.modules.universe.sp500 import _constituents_at_time

    base = set(changes_data["base_constituents_2018"])

    # At 2018-01-01 (before any change takes effect) → base set.
    members_2018 = _constituents_at_time(changes_data, _ns(2018, 1, 1))
    assert members_2018 == base

    # SNAP removed 2019-05-01 → present just before, absent just after.
    assert "SNAP" in _constituents_at_time(changes_data, _ns(2019, 4, 30))
    assert "SNAP" not in _constituents_at_time(changes_data, _ns(2019, 5, 2))

    # ZM added 2019-05-01 → absent before, present after.
    assert "ZM" not in _constituents_at_time(changes_data, _ns(2019, 4, 30))
    assert "ZM" in _constituents_at_time(changes_data, _ns(2019, 5, 2))

    # TSLA added 2020-09-01 → absent in 2019, present in 2021.
    assert "TSLA" not in _constituents_at_time(changes_data, _ns(2019, 12, 31))
    assert "TSLA" in _constituents_at_time(changes_data, _ns(2021, 1, 1))

    # X removed 2020-09-01 → present before, absent after.
    assert "X" in _constituents_at_time(changes_data, _ns(2020, 8, 31))
    assert "X" not in _constituents_at_time(changes_data, _ns(2020, 9, 2))


def test_constituents_during_range(changes_data) -> None:
    """``_constituents_during_range`` returns all members at any point in a range."""
    from quant_foundry.modules.universe.sp500 import _constituents_during_range

    base = set(changes_data["base_constituents_2018"])

    # A 2018 range → base set plus any in-range additions (INFO on 2018-06-26).
    members_2018 = _constituents_during_range(changes_data, _ns(2018, 1, 2), _ns(2018, 12, 31))
    assert base.issubset(members_2018)
    assert "INFO" in members_2018  # added during 2018
    # No future additions leak in.
    assert "TSLA" not in members_2018
    assert "ZM" not in members_2018

    # Range spanning the SNAP removal (2019-05-01) → SNAP still included.
    members_2019_h1 = _constituents_during_range(changes_data, _ns(2019, 1, 1), _ns(2019, 6, 30))
    assert "SNAP" in members_2019_h1  # was a member Jan–Apr
    assert "ZM" in members_2019_h1  # added May 1 (within range)

    # Range entirely after SNAP removal → SNAP excluded.
    members_2020 = _constituents_during_range(changes_data, _ns(2020, 1, 1), _ns(2020, 6, 30))
    assert "SNAP" not in members_2020

    # Range spanning TSLA addition (2020-09-01) → TSLA included.
    members_2020_full = _constituents_during_range(changes_data, _ns(2020, 1, 1), _ns(2020, 12, 31))
    assert "TSLA" in members_2020_full
    # X removed 2020-09-01 → still included (was a member Jan–Aug).
    assert "X" in members_2020_full

    # Range entirely before TSLA addition → TSLA excluded.
    members_2019_full = _constituents_during_range(changes_data, _ns(2019, 1, 1), _ns(2019, 12, 31))
    assert "TSLA" not in members_2019_full

    # Range entirely after X removal → X excluded.
    members_2021 = _constituents_during_range(changes_data, _ns(2021, 1, 1), _ns(2021, 6, 30))
    assert "X" not in members_2021

    # Reversed range raises.
    with pytest.raises(ValueError, match="end_ns"):
        _constituents_during_range(changes_data, _ns(2020, 6, 1), _ns(2020, 1, 1))


def test_load_sp500_changes_validates_structure(tmp_path: pathlib.Path) -> None:
    """``_load_sp500_changes`` rejects malformed files."""
    from quant_foundry.modules.universe.sp500 import _load_sp500_changes

    # Missing file.
    with pytest.raises(FileNotFoundError):
        _load_sp500_changes(tmp_path / "nope.json")

    # Invalid JSON.
    bad = tmp_path / "bad.json"
    bad.write_text("{not valid json")
    with pytest.raises(ValueError, match="invalid JSON"):
        _load_sp500_changes(bad)

    # Missing base_constituents_2018.
    bad2 = tmp_path / "bad2.json"
    bad2.write_text('{"changes": []}')
    with pytest.raises(ValueError, match="base_constituents_2018"):
        _load_sp500_changes(bad2)

    # Malformed change entry (missing date).
    bad3 = tmp_path / "bad3.json"
    bad3.write_text(
        '{"base_constituents_2018": ["AAPL"], "changes": [{"added": [], "removed": []}]}'
    )
    with pytest.raises(ValueError, match="date"):
        _load_sp500_changes(bad3)
