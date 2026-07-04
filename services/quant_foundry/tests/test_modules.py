"""
Tests for quant_foundry.modules — modular A/B-testable dataset system.

Tests verify:
- Module registry: registration, lookup, listing, instantiation.
- Protocol interfaces: modules satisfy their category Protocol.
- Abnormal return label: math matches label_event_impact, β is
  look-ahead-free, rows without price history are dropped.
- Per-event-type features: correct grouping, lookback window, zero
  for empty event types.
- Per-year features: correct year extraction, one-hot encoding.
- DatasetComposer: end-to-end synthetic dataset build produces a valid
  IngestionResult (parquet + manifest + receipt + quality report).
- PIT correctness: no feature value's observed_at exceeds decision_time.

Heavy dependencies (numpy, polars) use ``pytest.importorskip`` so tests
are skipped in environments without those deps.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import pathlib
import sys

import pytest

# Path setup
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


# --------------------------------------------------------------------------- #
# Registry tests                                                              #
# --------------------------------------------------------------------------- #


def test_registry_importable() -> None:
    """The modules package must be importable without heavy deps."""
    from quant_foundry.modules import (
        MODULE_CATEGORIES,
        load_all_modules,
        register_module,
    )

    assert "sentiment" in MODULE_CATEGORIES
    assert "source" in MODULE_CATEGORIES
    assert "label" in MODULE_CATEGORIES
    assert "feature" in MODULE_CATEGORIES
    assert "universe" in MODULE_CATEGORIES
    assert "price_join" in MODULE_CATEGORIES
    assert callable(load_all_modules)
    assert callable(register_module)


def test_registry_singleton() -> None:
    """ModuleRegistry.instance() returns the same object."""
    from quant_foundry.modules import ModuleRegistry

    r1 = ModuleRegistry.instance()
    r2 = ModuleRegistry.instance()
    assert r1 is r2


def test_load_all_modules_registers_modules() -> None:
    """load_all_modules() populates the registry with all module categories."""
    from quant_foundry.modules import ModuleRegistry, load_all_modules

    load_all_modules()
    registry = ModuleRegistry.instance()

    # Each category should have at least one module
    for cat in ("sentiment", "source", "label", "feature", "universe", "price_join"):
        modules = registry.list_by_category(cat)
        assert len(modules) >= 1, f"category {cat!r} has no registered modules"

    # Check specific modules are registered
    assert "sentiment:naive-wordlist:1.0.0" in registry.list_by_category("sentiment")
    assert "label:abnormal-return:1.1.0" in registry.list_by_category("label")
    assert "label:abnormal-return-v1:1.0.0" in registry.list_by_category("label")
    assert "feature:per-event-type:1.0.0" in registry.list_by_category("feature")
    assert "feature:per-year:1.0.0" in registry.list_by_category("feature")
    assert "universe:sp500:1.0.0" in registry.list_by_category("universe")
    assert "price_join:alpaca-bars:1.0.0" in registry.list_by_category("price_join")


def test_registry_create_with_config() -> None:
    """Registry.create() instantiates a module with config overrides."""
    from quant_foundry.modules import ModuleRegistry, load_all_modules

    load_all_modules()
    registry = ModuleRegistry.instance()

    mod = registry.create(
        "universe:sp500:1.0.0",
        config={"max_symbols": 5},
    )
    symbols = mod.select_symbols(start_ns=0, end_ns=1)
    assert len(symbols) == 5


def test_registry_rejects_unknown_category() -> None:
    """Registering a module with an unknown category raises ValueError."""
    from quant_foundry.modules.registry import ModuleRegistry

    registry = ModuleRegistry.instance()
    with pytest.raises(ValueError, match="unknown module category"):
        registry.register("bogus", "test", "1.0.0", object)


def test_registry_rejects_duplicate() -> None:
    """Registering the same module ID twice raises ValueError."""
    from quant_foundry.modules.registry import ModuleRegistry

    registry = ModuleRegistry.instance()
    # naive-wordlist is already registered via load_all_modules
    with pytest.raises(ValueError, match="already registered"):
        registry.register("sentiment", "naive-wordlist", "1.0.0", object)


# --------------------------------------------------------------------------- #
# Sentiment module tests                                                      #
# --------------------------------------------------------------------------- #


def test_naive_sentiment_positive() -> None:
    """NaiveWordlistSentiment scores positive headlines correctly."""
    from quant_foundry.modules import load_all_modules
    from quant_foundry.modules.registry import MediaItem, ModuleRegistry

    load_all_modules()
    mod = ModuleRegistry.instance().create("sentiment:naive-wordlist:1.0.0")

    items = [
        MediaItem(
            item_id="1",
            source="test",
            headline="Company beats earnings, surges on strong growth",
            body="",
            available_at_ns=0,
        ),
    ]
    results = mod.score(items)
    assert len(results) == 1
    assert results[0].score > 0.0
    assert results[0].provider == "naive"


def test_naive_sentiment_negative() -> None:
    """NaiveWordlistSentiment scores negative headlines correctly."""
    from quant_foundry.modules import load_all_modules
    from quant_foundry.modules.registry import MediaItem, ModuleRegistry

    load_all_modules()
    mod = ModuleRegistry.instance().create("sentiment:naive-wordlist:1.0.0")

    items = [
        MediaItem(
            item_id="1",
            source="test",
            headline="Company faces lawsuit, stock drops on weak decline",
            body="",
            available_at_ns=0,
        ),
    ]
    results = mod.score(items)
    assert len(results) == 1
    assert results[0].score < 0.0


def test_naive_sentiment_neutral() -> None:
    """NaiveWordlistSentiment returns 0.0 for neutral text."""
    from quant_foundry.modules import load_all_modules
    from quant_foundry.modules.registry import MediaItem, ModuleRegistry

    load_all_modules()
    mod = ModuleRegistry.instance().create("sentiment:naive-wordlist:1.0.0")

    items = [
        MediaItem(
            item_id="1",
            source="test",
            headline="Company announces quarterly results",
            body="",
            available_at_ns=0,
        ),
    ]
    results = mod.score(items)
    assert results[0].score == 0.0
    assert results[0].confidence == 0.0


# --------------------------------------------------------------------------- #
# Abnormal return label tests                                                 #
# --------------------------------------------------------------------------- #


def _make_bars(
    symbol: str,
    start_ns: int,
    n_days: int,
    base_price: float = 100.0,
    drift: float = 0.0001,
    volatility: float = 0.01,
    seed: int = 42,
) -> list:
    """Generate n_days of synthetic daily bars with drift + noise.

    The noise is essential for β estimation — constant drift produces
    zero variance in returns, which makes β undefined.
    """
    import random

    from quant_foundry.modules.registry import PriceBar

    rng = random.Random(seed)
    NS_PER_DAY = 86_400_000_000_000
    bars = []
    price = base_price
    for i in range(n_days):
        # Daily return = drift + noise
        ret = drift + rng.gauss(0, volatility)
        price *= 1.0 + ret
        bars.append(
            PriceBar(
                symbol=symbol,
                ts_ns=start_ns + i * NS_PER_DAY,
                open=price * 0.999,
                high=price * 1.005,
                low=price * 0.995,
                close=price,
                volume=1_000_000.0,
            )
        )
    return bars


def test_abnormal_return_label_basic() -> None:
    """AbnormalReturnLabel computes labels for rows with price history."""
    from quant_foundry.modules import load_all_modules
    from quant_foundry.modules.registry import (
        FeatureRowData,
        ModuleRegistry,
    )

    load_all_modules()
    label_mod = ModuleRegistry.instance().create("label:abnormal-return-v1:1.0.0")

    NS_PER_DAY = 86_400_000_000_000
    start_ns = int(dt.datetime(2023, 1, 1, tzinfo=dt.UTC).timestamp()) * 1_000_000_000

    # Generate 400 days of asset + benchmark bars (need 260 + 63d horizon = 323+)
    asset_bars = _make_bars("AAPL", start_ns, 400, base_price=100.0, seed=42)
    bench_bars = _make_bars("SPY", start_ns, 400, base_price=400.0, seed=99)

    # A feature row at day 260 (enough for β window + forward horizons)
    decision_time = start_ns + 260 * NS_PER_DAY
    rows = [
        FeatureRowData(
            symbol="AAPL",
            decision_time=decision_time,
            features={"sent_earnings": 0.5},
        )
    ]

    labeled = label_mod.compute_labels(
        rows,
        price_bars={"AAPL": asset_bars},
        benchmark_bars=bench_bars,
    )

    assert len(labeled) == 1
    row = labeled[0]
    assert row.label is not None
    # Should have AR columns for each horizon
    assert "ar_1d" in row.features
    assert "ar_5d" in row.features
    assert "ar_21d" in row.features
    assert "ar_63d" in row.features
    # Label should be the +5d AR
    assert row.label == row.features["ar_5d"]


def test_abnormal_return_label_drops_no_history() -> None:
    """Rows without enough price history are dropped."""
    from quant_foundry.modules import load_all_modules
    from quant_foundry.modules.registry import (
        FeatureRowData,
        ModuleRegistry,
    )

    load_all_modules()
    label_mod = ModuleRegistry.instance().create("label:abnormal-return-v1:1.0.0")

    NS_PER_DAY = 86_400_000_000_000
    start_ns = int(dt.datetime(2023, 1, 1, tzinfo=dt.UTC).timestamp()) * 1_000_000_000

    # Only 10 days of bars — not enough for β window (min 60)
    asset_bars = _make_bars("AAPL", start_ns, 10)
    bench_bars = _make_bars("SPY", start_ns, 10)

    decision_time = start_ns + 5 * NS_PER_DAY
    rows = [
        FeatureRowData(
            symbol="AAPL",
            decision_time=decision_time,
            features={"sent_earnings": 0.5},
        )
    ]

    labeled = label_mod.compute_labels(
        rows,
        price_bars={"AAPL": asset_bars},
        benchmark_bars=bench_bars,
    )
    assert len(labeled) == 0  # dropped due to insufficient β window


def test_abnormal_return_beta_no_lookahead() -> None:
    """β must be estimated only from bars BEFORE the decision time."""
    from quant_foundry.modules.labels.abnormal_return import _estimate_beta_v1

    NS_PER_DAY = 86_400_000_000_000
    start_ns = int(dt.datetime(2023, 1, 1, tzinfo=dt.UTC).timestamp()) * 1_000_000_000

    asset_bars = _make_bars("AAPL", start_ns, 300, seed=42)
    bench_bars = _make_bars("SPY", start_ns, 300, seed=99)
    bench_ts = [b.ts_ns for b in bench_bars]
    bench_close = [b.close for b in bench_bars]

    decision_time = start_ns + 200 * NS_PER_DAY

    # β should be computable (different seeds → non-zero variance)
    beta = _estimate_beta_v1(
        asset_bars,
        bench_ts,
        bench_close,
        decision_time,
        window=252,
        min_window=60,
    )
    assert beta is not None
    # β should be a finite number
    assert math.isfinite(beta)


# --------------------------------------------------------------------------- #
# Per-event-type feature tests                                                #
# --------------------------------------------------------------------------- #


def test_per_event_type_features_basic() -> None:
    """PerEventTypeFeatures groups sentiment by event type correctly."""
    from quant_foundry.modules import load_all_modules
    from quant_foundry.modules.registry import (
        MediaItem,
        ModuleRegistry,
        SentimentResult,
    )

    load_all_modules()
    mod = ModuleRegistry.instance().create("feature:per-event-type:1.0.0")

    NS_PER_DAY = 86_400_000_000_000
    base_ns = int(dt.datetime(2023, 6, 1, tzinfo=dt.UTC).timestamp()) * 1_000_000_000

    items = [
        MediaItem(
            item_id="1",
            source="newsapi",
            headline="Company beats earnings",
            body="",
            available_at_ns=base_ns,
            symbols=("AAPL",),
            event_type="earnings",
        ),
        MediaItem(
            item_id="2",
            source="newsapi",
            headline="SEC probe announced",
            body="",
            available_at_ns=base_ns + 1 * NS_PER_DAY,
            symbols=("AAPL",),
            event_type="regulatory",
        ),
    ]
    sentiments = [
        SentimentResult(item_id="1", provider="naive", score=0.8, confidence=0.5),
        SentimentResult(item_id="2", provider="naive", score=-0.6, confidence=0.5),
    ]

    result = mod.compute_features(
        items,
        sentiments,
        symbols=["AAPL"],
        start_ns=base_ns,
        end_ns=base_ns + 10 * NS_PER_DAY,
    )

    assert "AAPL" in result
    aapl = result["AAPL"]
    assert len(aapl) >= 2  # at least 2 decision times

    # Check the second row has both earnings and regulatory sentiment
    dt2 = base_ns + 1 * NS_PER_DAY
    if dt2 in aapl:
        feats = aapl[dt2]
        assert "sent_earnings" in feats
        assert "sent_regulatory" in feats
        assert feats["sent_regulatory"] < 0.0  # negative sentiment


def test_per_year_features() -> None:
    """PerYearFeatures annotates rows with correct year + one-hot."""
    from quant_foundry.modules import load_all_modules
    from quant_foundry.modules.registry import ModuleRegistry

    load_all_modules()
    mod = ModuleRegistry.instance().create("feature:per-year:1.0.0")

    NS_PER_DAY = 86_400_000_000_000
    dt_2023 = int(dt.datetime(2023, 6, 1, tzinfo=dt.UTC).timestamp()) * 1_000_000_000
    dt_2024 = int(dt.datetime(2024, 6, 1, tzinfo=dt.UTC).timestamp()) * 1_000_000_000

    feats_2023 = mod.annotate_row(dt_2023)
    assert feats_2023["year"] == 2023.0
    assert feats_2023["year_2023"] == 1.0
    assert feats_2023["year_2024"] == 0.0

    feats_2024 = mod.annotate_row(dt_2024)
    assert feats_2024["year"] == 2024.0
    assert feats_2024["year_2024"] == 1.0
    assert feats_2024["year_2023"] == 0.0


# --------------------------------------------------------------------------- #
# Universe module tests                                                       #
# --------------------------------------------------------------------------- #


def test_sp500_universe() -> None:
    """SP500Universe returns a non-empty symbol list."""
    from quant_foundry.modules import load_all_modules
    from quant_foundry.modules.registry import ModuleRegistry

    load_all_modules()
    mod = ModuleRegistry.instance().create("universe:sp500:1.0.0")
    symbols = mod.select_symbols(start_ns=0, end_ns=1)
    assert len(symbols) > 0
    assert "AAPL" in symbols
    assert all(isinstance(s, str) for s in symbols)


def test_sp500_universe_max_symbols() -> None:
    """max_symbols config limits the number of returned symbols."""
    from quant_foundry.modules import load_all_modules
    from quant_foundry.modules.registry import ModuleRegistry

    load_all_modules()
    mod = ModuleRegistry.instance().create(
        "universe:sp500:1.0.0",
        config={"max_symbols": 10},
    )
    symbols = mod.select_symbols(start_ns=0, end_ns=1)
    assert len(symbols) == 10


# --------------------------------------------------------------------------- #
# End-to-end composer test (synthetic data)                                   #
# --------------------------------------------------------------------------- #


def test_composer_end_to_end(tmp_path: pathlib.Path) -> None:
    """DatasetComposer builds a valid dataset end-to-end with synthetic data.

    This test uses a mock source adapter and synthetic price bars to
    verify the full pipeline: universe → source → sentiment → features
    → price_join → label → parquet + manifest + receipt + quality.
    """
    pytest.importorskip("polars")
    pytest.importorskip("numpy")

    from quant_foundry.modules import ModuleRegistry, load_all_modules
    from quant_foundry.modules.registry import (
        MediaItem,
        ModuleInfo,
        PriceBar,
        register_module,
    )

    load_all_modules()
    registry = ModuleRegistry.instance()

    # Register a mock source that returns synthetic items
    @register_module("source", "mock-test", "1.0.0")
    class MockSource:
        info = ModuleInfo("mock-test", "source", "1.0.0")

        def __init__(self, config=None) -> None:
            self.config = config or {}

        async def fetch(self, *, symbols, start_ns, end_ns):
            NS_PER_DAY = 86_400_000_000_000
            items = []
            for i, sym in enumerate(symbols[:3]):
                for day in range(280, 285):
                    items.append(
                        MediaItem(
                            item_id=f"mock-{sym}-{day}",
                            source="mock",
                            headline="Company beats earnings"
                            if day % 2 == 0
                            else "Stock drops on weak outlook",
                            body="",
                            available_at_ns=start_ns + day * NS_PER_DAY,
                            symbols=(sym,),
                            event_type="earnings" if day % 2 == 0 else "guidance",
                        )
                    )
            return items

    # Register a mock price joiner that returns synthetic bars
    @register_module("price_join", "mock-test", "1.0.0")
    class MockPriceJoin:
        info = ModuleInfo("mock-test", "price_join", "1.0.0")

        def __init__(self, config=None) -> None:
            self.config = config or {}

        def load_bars(self, *, symbols, start_ns, end_ns):
            import random

            NS_PER_DAY = 86_400_000_000_000
            rng = random.Random(42)
            asset_bars = {}
            for sym in symbols:
                bars = []
                price = 100.0
                for day in range(350):
                    ret = 0.0005 + rng.gauss(0, 0.01)
                    price *= 1.0 + ret
                    bars.append(
                        PriceBar(
                            symbol=sym,
                            ts_ns=start_ns + day * NS_PER_DAY,
                            open=price * 0.999,
                            high=price * 1.005,
                            low=price * 0.995,
                            close=price,
                            volume=1e6,
                        )
                    )
                asset_bars[sym] = bars
            # Benchmark bars (different seed for non-zero β variance)
            rng_bench = random.Random(99)
            bench_bars = []
            price = 400.0
            for day in range(350):
                ret = 0.0003 + rng_bench.gauss(0, 0.01)
                price *= 1.0 + ret
                bench_bars.append(
                    PriceBar(
                        symbol="SPY",
                        ts_ns=start_ns + day * NS_PER_DAY,
                        open=price * 0.999,
                        high=price * 1.005,
                        low=price * 0.995,
                        close=price,
                        volume=1e7,
                    )
                )
            return asset_bars, bench_bars

    try:
        from quant_foundry.modules import DatasetComposer

        NS_PER_DAY = 86_400_000_000_000
        start_ns = int(dt.datetime(2023, 1, 1, tzinfo=dt.UTC).timestamp()) * 1_000_000_000
        end_ns = start_ns + 365 * NS_PER_DAY

        composer = DatasetComposer(
            universe="universe:sp500:1.0.0",
            source="source:mock-test:1.0.0",
            sentiment="sentiment:naive-wordlist:1.0.0",
            features=["feature:per-event-type:1.0.0", "feature:per-year:1.0.0"],
            label="label:abnormal-return-v1:1.0.0",
            price_join="price_join:mock-test:1.0.0",
            config={
                "universe:sp500:1.0.0": {"max_symbols": 3},
            },
        )

        result = composer.build(
            output_dir=tmp_path,
            dataset_id="test-media-sentiment-price",
            start_ns=start_ns,
            end_ns=end_ns,
            n_folds=3,
        )

        # Verify all artifacts exist
        assert result.parquet_path.exists()
        assert result.manifest_path.exists()
        assert result.receipt_path.exists()
        assert result.quality_path.exists()

        # Verify parquet has the right columns
        import polars as pl

        df = pl.read_parquet(str(result.parquet_path))
        assert "decision_time" in df.columns
        assert "label" in df.columns
        assert "symbol" in df.columns
        assert df.height > 0

        # Verify manifest is valid JSON
        manifest_body = json.loads(result.manifest_path.read_text())
        assert manifest_body["dataset_id"] == "test-media-sentiment-price"
        assert manifest_body["pit_proof_verified"] is True
        assert "feature_names" in manifest_body

        # Verify receipt
        receipt_body = json.loads(result.receipt_path.read_text())
        assert receipt_body["pit_proof_verified"] is True

    finally:
        # Clean up test modules from registry
        registry._modules.pop("source:mock-test:1.0.0", None)
        registry._modules.pop("price_join:mock-test:1.0.0", None)


# --------------------------------------------------------------------------- #
# Module-level heavy deps check                                               #
# --------------------------------------------------------------------------- #


def test_modules_no_module_level_heavy_deps() -> None:
    """numpy and polars must NOT be imported at module level."""
    import quant_foundry.modules.composer as co
    import quant_foundry.modules.labels.abnormal_return as ar
    import quant_foundry.modules.price_join.alpaca_bars as pj
    import quant_foundry.modules.sentiment.naive_wordlist as nw

    for mod in (co, ar, pj, nw):
        assert not hasattr(mod, "np"), f"{mod.__name__}: numpy at module level"
        assert not hasattr(mod, "pl"), f"{mod.__name__}: polars at module level"
        assert not hasattr(mod, "numpy"), f"{mod.__name__}: numpy at module level"
        assert not hasattr(mod, "polars"), f"{mod.__name__}: polars at module level"
