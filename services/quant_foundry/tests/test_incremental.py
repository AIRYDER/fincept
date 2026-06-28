"""
Tests for quant_foundry.modules.composer incremental / streaming mode.

Verifies:
- ``IncrementalState`` save/load round-trip.
- ``build_or_update`` does a full build on first run (no state file).
- ``build_or_update`` does an incremental build on subsequent runs and
  appends rows to the existing parquet.
- A changed module-config hash triggers a full rebuild instead of an
  incremental append.
- ``build_incremental`` appends new rows while preserving existing rows.
- ``build_incremental`` deduplicates items already present in the
  existing parquet (by ``(symbol, decision_time)``).

Heavy dependencies (numpy, polars) use ``pytest.importorskip`` so tests
are skipped in environments without those deps.  The mocking pattern
mirrors ``test_composer_end_to_end`` in ``test_modules.py`` — a mock
source adapter + mock price joiner registered into the module registry
so no real API keys are needed.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import sys

import pytest

# Path setup (mirror test_modules.py)
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


NS_PER_DAY = 86_400_000_000_000
_BASE_NS = int(dt.datetime(2023, 1, 1, tzinfo=dt.timezone.utc).timestamp()) * 1_000_000_000


# --------------------------------------------------------------------------- #
# Mock module factories                                                        #
# --------------------------------------------------------------------------- #


def _register_mock_modules(registry, *, source_id="mock-inc", price_id="mock-inc"):
    """Register a mock source + price joiner into the registry.

    The mock source emits media items for days [280, 330] relative to a
    fixed base, filtered by the requested ``[start_ns, end_ns)`` window.
    The mock price joiner emits 450 days of bars relative to the same
    base, filtered by the requested window — so incremental builds that
    ask for extra β-estimation history still get bars back.
    """
    from quant_foundry.modules.registry import (
        MediaItem,
        ModuleInfo,
        PriceBar,
        register_module,
    )

    @register_module("source", source_id, "1.0.0")
    class MockSource:
        info = ModuleInfo(source_id, "source", "1.0.0")

        def __init__(self, config=None) -> None:
            self.config = config or {}

        async def fetch(self, *, symbols, start_ns, end_ns):
            items = []
            for sym in symbols[:3]:
                for day in range(280, 331):
                    t = _BASE_NS + day * NS_PER_DAY
                    if t < start_ns or t >= end_ns:
                        continue
                    items.append(MediaItem(
                        item_id=f"mock-{sym}-{day}",
                        source="mock",
                        headline="Company beats earnings" if day % 2 == 0 else "Stock drops on weak outlook",
                        body="",
                        available_at_ns=t,
                        symbols=(sym,),
                        event_type="earnings" if day % 2 == 0 else "guidance",
                    ))
            return items

    @register_module("price_join", price_id, "1.0.0")
    class MockPriceJoin:
        info = ModuleInfo(price_id, "price_join", "1.0.0")

        def __init__(self, config=None) -> None:
            self.config = config or {}

        def load_bars(self, *, symbols, start_ns, end_ns):
            import random

            rng = random.Random(42)
            asset_bars = {}
            for sym in symbols:
                bars = []
                price = 100.0
                for day in range(0, 451):
                    t = _BASE_NS + day * NS_PER_DAY
                    if t < start_ns or t >= end_ns:
                        # still advance the price so the sequence is stable
                        ret = 0.0005 + rng.gauss(0, 0.01)
                        price *= (1.0 + ret)
                        continue
                    ret = 0.0005 + rng.gauss(0, 0.01)
                    price *= (1.0 + ret)
                    bars.append(PriceBar(
                        symbol=sym,
                        ts_ns=t,
                        open=price * 0.999,
                        high=price * 1.005,
                        low=price * 0.995,
                        close=price,
                        volume=1e6,
                    ))
                asset_bars[sym] = bars
            # Benchmark bars (different seed for non-zero β variance)
            rng_bench = random.Random(99)
            bench_bars = []
            price = 400.0
            for day in range(0, 451):
                t = _BASE_NS + day * NS_PER_DAY
                ret = 0.0003 + rng_bench.gauss(0, 0.01)
                price *= (1.0 + ret)
                if t < start_ns or t >= end_ns:
                    continue
                bench_bars.append(PriceBar(
                    symbol="SPY",
                    ts_ns=t,
                    open=price * 0.999,
                    high=price * 1.005,
                    low=price * 0.995,
                    close=price,
                    volume=1e7,
                ))
            return asset_bars, bench_bars

    return MockSource, MockPriceJoin


def _make_composer(*, source_id="mock-inc", price_id="mock-inc", config=None):
    from quant_foundry.modules import DatasetComposer

    return DatasetComposer(
        universe="universe:sp500:1.0.0",
        source=f"source:{source_id}:1.0.0",
        sentiment="sentiment:naive-wordlist:1.0.0",
        features=["feature:per-event-type:1.0.0", "feature:per-year:1.0.0"],
        label="label:abnormal-return-v1:1.0.0",
        price_join=f"price_join:{price_id}:1.0.0",
        config=config or {"universe:sp500:1.0.0": {"max_symbols": 3}},
    )


# --------------------------------------------------------------------------- #
# IncrementalState save/load                                                   #
# --------------------------------------------------------------------------- #


def test_incremental_state_save_load(tmp_path: pathlib.Path) -> None:
    """save_incremental_state / load_incremental_state round-trip."""
    from quant_foundry.modules.composer import (
        IncrementalState,
        load_incremental_state,
        save_incremental_state,
    )

    state = IncrementalState(
        dataset_id="ds-1",
        last_build_ns=1_700_000_000_000_000_000,
        row_count=42,
        parquet_path=str(tmp_path / "ds-1.parquet"),
        manifest_path=str(tmp_path / "ds-1.manifest.json"),
        module_config_hash="abc123",
    )
    path = save_incremental_state(state, tmp_path)
    assert path.exists()
    assert path.name == "incremental_state.json"

    loaded = load_incremental_state(tmp_path)
    assert loaded == state
    assert loaded.dataset_id == "ds-1"
    assert loaded.last_build_ns == 1_700_000_000_000_000_000
    assert loaded.row_count == 42
    assert loaded.module_config_hash == "abc123"


# --------------------------------------------------------------------------- #
# build_or_update                                                              #
# --------------------------------------------------------------------------- #


def test_build_or_update_first_run(tmp_path: pathlib.Path) -> None:
    """No existing state → build_or_update does a full build."""
    pytest.importorskip("polars")
    pytest.importorskip("numpy")

    from quant_foundry.modules import ModuleRegistry, load_all_modules

    load_all_modules()
    registry = ModuleRegistry.instance()
    _register_mock_modules(registry)
    try:
        import polars as pl

        composer = _make_composer()
        end_ns = _BASE_NS + 315 * NS_PER_DAY  # items days 280-314

        result = composer.build_or_update(
            output_dir=tmp_path,
            dataset_id="inc-ds",
            start_ns=_BASE_NS,
            end_ns=end_ns,
            n_folds=3,
        )

        # Full build happened — state file written.
        state_path = tmp_path / "incremental_state.json"
        assert state_path.exists()

        df = pl.read_parquet(str(result.parquet_path))
        assert df.height > 0

        from quant_foundry.modules.composer import load_incremental_state

        state = load_incremental_state(tmp_path)
        assert state.dataset_id == "inc-ds"
        assert state.row_count == df.height
        assert state.last_build_ns == int(df["decision_time"].max())
        assert state.module_config_hash == composer.module_config_hash()
    finally:
        registry._modules.pop("source:mock-inc:1.0.0", None)
        registry._modules.pop("price_join:mock-inc:1.0.0", None)


def test_build_or_update_incremental(tmp_path: pathlib.Path) -> None:
    """Existing state → build_or_update does an incremental build + appends."""
    pytest.importorskip("polars")
    pytest.importorskip("numpy")

    from quant_foundry.modules import ModuleRegistry, load_all_modules

    load_all_modules()
    registry = ModuleRegistry.instance()
    _register_mock_modules(registry)
    try:
        import polars as pl

        composer = _make_composer()

        # First run: full build covering days 280-314.
        result1 = composer.build_or_update(
            output_dir=tmp_path,
            dataset_id="inc-ds",
            start_ns=_BASE_NS,
            end_ns=_BASE_NS + 315 * NS_PER_DAY,
            n_folds=3,
        )
        df1 = pl.read_parquet(str(result1.parquet_path))
        count1 = df1.height
        max_dt1 = int(df1["decision_time"].max())

        # Second run: extend the window so days 315-330 are now in range.
        result2 = composer.build_or_update(
            output_dir=tmp_path,
            dataset_id="inc-ds",
            start_ns=_BASE_NS,
            end_ns=_BASE_NS + 400 * NS_PER_DAY,
            n_folds=3,
        )
        df2 = pl.read_parquet(str(result2.parquet_path))

        # Rows were appended (new days 315-330 across 3 symbols).
        assert df2.height > count1
        # Existing rows preserved — the old max decision_time is still present.
        assert max_dt1 in df2["decision_time"].to_list()
        # New max is greater than the old max.
        assert int(df2["decision_time"].max()) > max_dt1

        # State updated.
        from quant_foundry.modules.composer import load_incremental_state

        state = load_incremental_state(tmp_path)
        assert state.row_count == df2.height
        assert state.last_build_ns == int(df2["decision_time"].max())
    finally:
        registry._modules.pop("source:mock-inc:1.0.0", None)
        registry._modules.pop("price_join:mock-inc:1.0.0", None)


def test_build_or_update_config_change_triggers_rebuild(tmp_path: pathlib.Path) -> None:
    """A different module-config hash triggers a full rebuild."""
    pytest.importorskip("polars")
    pytest.importorskip("numpy")

    from quant_foundry.modules import ModuleRegistry, load_all_modules

    load_all_modules()
    registry = ModuleRegistry.instance()
    _register_mock_modules(registry)
    try:
        import polars as pl

        composer_a = _make_composer()
        end_ns = _BASE_NS + 315 * NS_PER_DAY

        # First run with config A.
        result1 = composer_a.build_or_update(
            output_dir=tmp_path,
            dataset_id="inc-ds",
            start_ns=_BASE_NS,
            end_ns=end_ns,
            n_folds=3,
        )
        df1 = pl.read_parquet(str(result1.parquet_path))
        checksum1 = df1["decision_time"].to_list()

        # Tamper with the state file's hash to simulate a config change.
        state_path = tmp_path / "incremental_state.json"
        body = json.loads(state_path.read_text())
        body["module_config_hash"] = "different-config-hash"
        state_path.write_text(json.dumps(body, sort_keys=True, indent=2))

        # Second run: config hash mismatch → full rebuild (same window).
        composer_b = _make_composer()
        result2 = composer_b.build_or_update(
            output_dir=tmp_path,
            dataset_id="inc-ds",
            start_ns=_BASE_NS,
            end_ns=end_ns,
            n_folds=3,
        )
        df2 = pl.read_parquet(str(result2.parquet_path))

        # Full rebuild produced the same rows (same window) but the state
        # hash was reset to the current composer's hash.
        from quant_foundry.modules.composer import load_incremental_state

        state = load_incremental_state(tmp_path)
        assert state.module_config_hash == composer_b.module_config_hash()
        # Row content is unchanged (full rebuild of the same window).
        assert sorted(df2["decision_time"].to_list()) == sorted(checksum1)
    finally:
        registry._modules.pop("source:mock-inc:1.0.0", None)
        registry._modules.pop("price_join:mock-inc:1.0.0", None)


# --------------------------------------------------------------------------- #
# build_incremental                                                            #
# --------------------------------------------------------------------------- #


def test_build_incremental_appends_to_existing(tmp_path: pathlib.Path) -> None:
    """build_incremental appends new rows and preserves existing rows."""
    pytest.importorskip("polars")
    pytest.importorskip("numpy")

    from quant_foundry.modules import ModuleRegistry, load_all_modules

    load_all_modules()
    registry = ModuleRegistry.instance()
    _register_mock_modules(registry)
    try:
        import polars as pl

        composer = _make_composer()

        # Full build: days 280-314.
        result1 = composer.build(
            output_dir=tmp_path,
            dataset_id="inc-ds",
            start_ns=_BASE_NS,
            end_ns=_BASE_NS + 315 * NS_PER_DAY,
            n_folds=3,
        )
        df1 = pl.read_parquet(str(result1.parquet_path))
        count1 = df1.height
        existing_dts = set(df1["decision_time"].to_list())

        # Incremental: fetch items > max decision_time, extend window.
        since_ns = int(df1["decision_time"].max())
        result2 = composer.build_incremental(
            output_dir=tmp_path,
            dataset_id="inc-ds",
            since_ns=since_ns,
            end_ns=_BASE_NS + 400 * NS_PER_DAY,
            existing_parquet_path=result1.parquet_path,
            n_folds=3,
        )
        df2 = pl.read_parquet(str(result2.parquet_path))

        # New rows appended.
        assert df2.height > count1
        # All existing decision times preserved.
        assert existing_dts.issubset(set(df2["decision_time"].to_list()))
        # Artifacts refreshed.
        assert result2.parquet_path.exists()
        assert result2.manifest_path.exists()
        assert result2.receipt_path.exists()
        assert result2.quality_path.exists()
        # Manifest row_count reflects the combined dataset.
        manifest_body = json.loads(result2.manifest_path.read_text())
        assert manifest_body["row_count"] == df2.height
    finally:
        registry._modules.pop("source:mock-inc:1.0.0", None)
        registry._modules.pop("price_join:mock-inc:1.0.0", None)


def test_build_incremental_deduplication(tmp_path: pathlib.Path) -> None:
    """Items already in the dataset are not re-added (dedup by (symbol, dt))."""
    pytest.importorskip("polars")
    pytest.importorskip("numpy")

    from quant_foundry.modules import ModuleRegistry, load_all_modules

    load_all_modules()
    registry = ModuleRegistry.instance()
    _register_mock_modules(registry)
    try:
        import polars as pl

        composer = _make_composer()

        # Full build: days 280-314 (end_ns excludes day 315).
        result1 = composer.build(
            output_dir=tmp_path,
            dataset_id="inc-ds",
            start_ns=_BASE_NS,
            end_ns=_BASE_NS + 315 * NS_PER_DAY,
            n_folds=3,
        )
        df1 = pl.read_parquet(str(result1.parquet_path))
        count1 = df1.height

        # Incremental with a since_ns BEFORE the max — so the source returns
        # items that overlap the existing dataset (days 291-314) plus new
        # ones (days 315-330).  Dedup must drop the overlapping rows.
        since_ns = _BASE_NS + 290 * NS_PER_DAY
        result2 = composer.build_incremental(
            output_dir=tmp_path,
            dataset_id="inc-ds",
            since_ns=since_ns,
            end_ns=_BASE_NS + 400 * NS_PER_DAY,
            existing_parquet_path=result1.parquet_path,
            n_folds=3,
        )
        df2 = pl.read_parquet(str(result2.parquet_path))

        pairs1 = set(zip(
            df1["symbol"].to_list(),
            df1["decision_time"].to_list(),
        ))
        pairs2 = list(zip(
            df2["symbol"].to_list(),
            df2["decision_time"].to_list(),
        ))

        # No duplicate (symbol, decision_time) pairs — dedup worked.
        assert len(pairs2) == len(set(pairs2))

        # All existing rows are preserved.
        assert pairs1.issubset(set(pairs2))

        # The only rows added are ones NOT already in the existing dataset.
        new_pairs = set(pairs2) - pairs1
        assert df2.height == count1 + len(new_pairs)

        # Every newly-added row has a decision_time strictly after since_ns
        # (the overlapping days 291-309 that the source returned were dropped
        # because they already existed in the parquet).
        for _sym, dtime in new_pairs:
            assert dtime > since_ns

        # And no row from the overlap window (since_ns, max_existing] was
        # added twice — the count of rows in that window equals the original.
        max_existing = max(dt for _s, dt in pairs1)
        overlap_rows = [
            p for p in pairs2 if since_ns < p[1] <= max_existing
        ]
        assert len(overlap_rows) == len(
            [p for p in pairs1 if since_ns < p[1] <= max_existing],
        )
    finally:
        registry._modules.pop("source:mock-inc:1.0.0", None)
        registry._modules.pop("price_join:mock-inc:1.0.0", None)
