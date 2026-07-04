"""
Tests for the depth feature modules — time-decay, engagement-weighted,
interactions, and pre-event momentum.

Tests verify:
- All 4 feature modules register correctly.
- TimeDecayFeatures: exponential weighting, half-life correctness,
  lookback window exclusion.
- EngagementWeightedFeatures: Reddit score weighting, NewsAPI equal
  weighting, log-scale normalization.
- InteractionsFeatures: pairwise products, only-nonzero sparsity,
  max_interactions limit.
- PreEventMomentumFeatures: basic feature computation, PIT correctness,
  insufficient-history handling.
"""

from __future__ import annotations

import datetime as dt
import math
import pathlib
import sys

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

NS_PER_DAY = 86_400_000_000_000


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
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
    """Generate n_days of synthetic daily bars with drift + noise."""
    import random

    from quant_foundry.modules.registry import PriceBar

    rng = random.Random(seed)
    bars = []
    price = base_price
    for i in range(n_days):
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


# --------------------------------------------------------------------------- #
# TimeDecayFeatures tests                                                      #
# --------------------------------------------------------------------------- #


def test_time_decay_registered() -> None:
    """feature:time-decay:1.0.0 is in the registry."""
    from quant_foundry.modules import ModuleRegistry, load_all_modules

    load_all_modules()
    registry = ModuleRegistry.instance()
    assert "feature:time-decay:1.0.0" in registry.list_by_category("feature")


def test_time_decay_exponential_weighting() -> None:
    """The most recent item should have the highest weight in the feature value."""
    from quant_foundry.modules import load_all_modules
    from quant_foundry.modules.registry import (
        MediaItem,
        ModuleRegistry,
        SentimentResult,
    )

    load_all_modules()
    mod = ModuleRegistry.instance().create(
        "feature:time-decay:1.0.0",
        config={"half_life_days": 2.0, "lookback_days": 30},
    )

    base_ns = int(dt.datetime(2023, 6, 1, tzinfo=dt.UTC).timestamp()) * 1_000_000_000

    # Three items at 0d, 5d, 10d before the decision time.
    # The decision time is the latest item's available_at_ns.
    # With half_life=2.0, the 10d item has weight ~0.03, the 5d item ~0.18,
    # and the 0d item weight 1.0 — so the recent item dominates.
    items = [
        MediaItem(
            item_id="old",
            source="newsapi",
            headline="Old news",
            body="",
            available_at_ns=base_ns,  # 10 days before decision
            symbols=("AAPL",),
            event_type="earnings",
        ),
        MediaItem(
            item_id="mid",
            source="newsapi",
            headline="Mid news",
            body="",
            available_at_ns=base_ns + 5 * NS_PER_DAY,  # 5 days before
            symbols=("AAPL",),
            event_type="earnings",
        ),
        MediaItem(
            item_id="recent",
            source="newsapi",
            headline="Recent news",
            body="",
            available_at_ns=base_ns + 10 * NS_PER_DAY,  # 0 days before (decision time)
            symbols=("AAPL",),
            event_type="earnings",
        ),
    ]
    # Sentiments: old=+0.9, mid=-0.9, recent=+0.9
    # With decay, recent dominates, so the weighted mean should be positive
    # and closer to +0.9 than a flat mean (which would be +0.3).
    sentiments = [
        SentimentResult(item_id="old", provider="naive", score=0.9, confidence=0.5),
        SentimentResult(item_id="mid", provider="naive", score=-0.9, confidence=0.5),
        SentimentResult(item_id="recent", provider="naive", score=0.9, confidence=0.5),
    ]

    result = mod.compute_features(
        items,
        sentiments,
        symbols=["AAPL"],
        start_ns=base_ns,
        end_ns=base_ns + 20 * NS_PER_DAY,
    )

    assert "AAPL" in result
    # The decision time is the latest item's available_at_ns
    dt_latest = base_ns + 10 * NS_PER_DAY
    assert dt_latest in result["AAPL"]
    feats = result["AAPL"][dt_latest]
    decay_mean = feats["sent_decay_earnings"]

    # Flat mean would be (0.9 - 0.9 + 0.9) / 3 = 0.3
    # Decay-weighted should be higher (closer to 0.9) because recent dominates
    assert decay_mean > 0.3, f"decay-weighted mean {decay_mean} should exceed flat mean 0.3"


def test_time_decay_half_life() -> None:
    """With half_life=2.0, an item 2 days old has half the weight of a 0-day item."""
    from quant_foundry.modules import load_all_modules
    from quant_foundry.modules.registry import (
        MediaItem,
        ModuleRegistry,
        SentimentResult,
    )

    load_all_modules()
    mod = ModuleRegistry.instance().create(
        "feature:time-decay:1.0.0",
        config={"half_life_days": 2.0, "lookback_days": 30},
    )

    base_ns = int(dt.datetime(2023, 6, 1, tzinfo=dt.UTC).timestamp()) * 1_000_000_000

    # Two items: one fresh (age 0), one 2 days old.
    # Fresh has sentiment -1.0, old has sentiment +1.0.
    # Weight of fresh = 1.0, weight of old = 0.5.
    # Weighted mean = (-1.0*1.0 + 1.0*0.5) / (1.0 + 0.5) = -0.5/1.5 = -0.333...
    items = [
        MediaItem(
            item_id="fresh",
            source="newsapi",
            headline="Fresh",
            body="",
            available_at_ns=base_ns + 2 * NS_PER_DAY,
            symbols=("AAPL",),
            event_type="earnings",
        ),
        MediaItem(
            item_id="old2d",
            source="newsapi",
            headline="Old 2d",
            body="",
            available_at_ns=base_ns,
            symbols=("AAPL",),
            event_type="earnings",
        ),
    ]
    sentiments = [
        SentimentResult(item_id="fresh", provider="naive", score=-1.0, confidence=0.5),
        SentimentResult(item_id="old2d", provider="naive", score=1.0, confidence=0.5),
    ]

    result = mod.compute_features(
        items,
        sentiments,
        symbols=["AAPL"],
        start_ns=base_ns,
        end_ns=base_ns + 20 * NS_PER_DAY,
    )

    dt_latest = base_ns + 2 * NS_PER_DAY
    feats = result["AAPL"][dt_latest]
    decay_mean = feats["sent_decay_earnings"]

    expected = (-1.0 * 1.0 + 1.0 * 0.5) / (1.0 + 0.5)
    assert abs(decay_mean - round(expected, 6)) < 1e-5, (
        f"decay mean {decay_mean} != expected {expected:.6f}"
    )


def test_time_decay_lookback_window() -> None:
    """Items older than lookback_days should be excluded from the window."""
    from quant_foundry.modules import load_all_modules
    from quant_foundry.modules.registry import (
        MediaItem,
        ModuleRegistry,
        SentimentResult,
    )

    load_all_modules()
    mod = ModuleRegistry.instance().create(
        "feature:time-decay:1.0.0",
        config={"half_life_days": 3.0, "lookback_days": 5},
    )

    base_ns = int(dt.datetime(2023, 6, 1, tzinfo=dt.UTC).timestamp()) * 1_000_000_000

    # An item 10 days old (outside 5-day lookback) and a fresh item.
    items = [
        MediaItem(
            item_id="too_old",
            source="newsapi",
            headline="Too old",
            body="",
            available_at_ns=base_ns,
            symbols=("AAPL",),
            event_type="earnings",
        ),
        MediaItem(
            item_id="fresh",
            source="newsapi",
            headline="Fresh",
            body="",
            available_at_ns=base_ns + 10 * NS_PER_DAY,
            symbols=("AAPL",),
            event_type="earnings",
        ),
    ]
    sentiments = [
        SentimentResult(item_id="too_old", provider="naive", score=1.0, confidence=0.5),
        SentimentResult(item_id="fresh", provider="naive", score=-0.5, confidence=0.5),
    ]

    result = mod.compute_features(
        items,
        sentiments,
        symbols=["AAPL"],
        start_ns=base_ns,
        end_ns=base_ns + 20 * NS_PER_DAY,
    )

    dt_latest = base_ns + 10 * NS_PER_DAY
    feats = result["AAPL"][dt_latest]
    # Only the fresh item is in the 5-day window
    assert feats["sent_decay_earnings"] == -0.5


# --------------------------------------------------------------------------- #
# EngagementWeightedFeatures tests                                             #
# --------------------------------------------------------------------------- #


def test_engagement_weighted_registered() -> None:
    """feature:engagement-weighted:1.0.0 is in the registry."""
    from quant_foundry.modules import ModuleRegistry, load_all_modules

    load_all_modules()
    registry = ModuleRegistry.instance()
    assert "feature:engagement-weighted:1.0.0" in registry.list_by_category("feature")


def test_engagement_weighted_reddit() -> None:
    """Reddit items with higher reddit_score should have more weight."""
    from quant_foundry.modules import load_all_modules
    from quant_foundry.modules.registry import (
        MediaItem,
        ModuleRegistry,
        SentimentResult,
    )

    load_all_modules()
    mod = ModuleRegistry.instance().create(
        "feature:engagement-weighted:1.0.0",
        config={"lookback_days": 30},
    )

    base_ns = int(dt.datetime(2023, 6, 1, tzinfo=dt.UTC).timestamp()) * 1_000_000_000

    # Two Reddit items at the same time: low-score with +1.0 sentiment,
    # high-score with -1.0 sentiment.  High-score item should dominate.
    items = [
        MediaItem(
            item_id="low_eng",
            source="reddit",
            headline="Low engagement post",
            body="",
            available_at_ns=base_ns,
            symbols=("AAPL",),
            event_type="social",
            metadata={"reddit_score": "10", "reddit_num_comments": "0"},
        ),
        MediaItem(
            item_id="high_eng",
            source="reddit",
            headline="High engagement post",
            body="",
            available_at_ns=base_ns,
            symbols=("AAPL",),
            event_type="social",
            metadata={"reddit_score": "5000", "reddit_num_comments": "1000"},
        ),
    ]
    sentiments = [
        SentimentResult(item_id="low_eng", provider="naive", score=1.0, confidence=0.5),
        SentimentResult(item_id="high_eng", provider="naive", score=-1.0, confidence=0.5),
    ]

    result = mod.compute_features(
        items,
        sentiments,
        symbols=["AAPL"],
        start_ns=base_ns,
        end_ns=base_ns + 20 * NS_PER_DAY,
    )

    dt_key = base_ns
    feats = result["AAPL"][dt_key]
    eng_mean = feats["sent_eng_social"]

    # Flat mean would be 0.0.  High-engagement item dominates → negative.
    assert eng_mean < 0.0, (
        f"engagement-weighted mean {eng_mean} should be negative (high-eng item dominates)"
    )


def test_engagement_weighted_newsapi() -> None:
    """NewsAPI items have no engagement metric, so weight=1.0 (equal weighting)."""
    from quant_foundry.modules import load_all_modules
    from quant_foundry.modules.registry import (
        MediaItem,
        ModuleRegistry,
        SentimentResult,
    )

    load_all_modules()
    mod = ModuleRegistry.instance().create(
        "feature:engagement-weighted:1.0.0",
        config={"lookback_days": 30},
    )

    base_ns = int(dt.datetime(2023, 6, 1, tzinfo=dt.UTC).timestamp()) * 1_000_000_000

    items = [
        MediaItem(
            item_id="news1",
            source="newsapi",
            headline="Bullish news",
            body="",
            available_at_ns=base_ns,
            symbols=("AAPL",),
            event_type="earnings",
        ),
        MediaItem(
            item_id="news2",
            source="newsapi",
            headline="Bearish news",
            body="",
            available_at_ns=base_ns,
            symbols=("AAPL",),
            event_type="earnings",
        ),
    ]
    sentiments = [
        SentimentResult(item_id="news1", provider="naive", score=0.8, confidence=0.5),
        SentimentResult(item_id="news2", provider="naive", score=0.4, confidence=0.5),
    ]

    result = mod.compute_features(
        items,
        sentiments,
        symbols=["AAPL"],
        start_ns=base_ns,
        end_ns=base_ns + 20 * NS_PER_DAY,
    )

    feats = result["AAPL"][base_ns]
    eng_mean = feats["sent_eng_earnings"]

    # Equal weighting → simple mean = (0.8 + 0.4) / 2 = 0.6
    assert abs(eng_mean - 0.6) < 1e-5, f"newsapi equal-weighted mean {eng_mean} != 0.6"


def test_engagement_weighted_log_scale() -> None:
    """Log-scale normalization prevents viral posts from dominating completely."""
    from quant_foundry.modules.features.engagement_weighted import (
        EngagementWeightedFeatures,
    )
    from quant_foundry.modules.registry import MediaItem

    mod = EngagementWeightedFeatures(config={"lookback_days": 30})

    base_ns = int(dt.datetime(2023, 6, 1, tzinfo=dt.UTC).timestamp()) * 1_000_000_000

    # A viral post (score=1_000_000) vs a normal post (score=10).
    viral = MediaItem(
        item_id="viral",
        source="reddit",
        headline="Viral",
        body="",
        available_at_ns=base_ns,
        symbols=("AAPL",),
        event_type="social",
        metadata={"reddit_score": "1000000", "reddit_num_comments": "0"},
    )
    normal = MediaItem(
        item_id="normal",
        source="reddit",
        headline="Normal",
        body="",
        available_at_ns=base_ns,
        symbols=("AAPL",),
        event_type="social",
        metadata={"reddit_score": "10", "reddit_num_comments": "0"},
    )

    w_viral = mod._engagement_score(viral)
    w_normal = mod._engagement_score(normal)

    # log1p(1000000) ≈ 13.8, log1p(10) ≈ 2.4
    # The ratio is ~5.7x, NOT 100000x — log scale tames the viral post.
    assert w_viral > w_normal
    ratio = w_viral / w_normal
    assert ratio < 20.0, f"log-scale ratio {ratio} should be modest (< 20), not 100000x"


# --------------------------------------------------------------------------- #
# InteractionsFeatures tests                                                   #
# --------------------------------------------------------------------------- #


def test_interactions_registered() -> None:
    """feature:interactions:1.0.0 is in the registry."""
    from quant_foundry.modules import ModuleRegistry, load_all_modules

    load_all_modules()
    registry = ModuleRegistry.instance()
    assert "feature:interactions:1.0.0" in registry.list_by_category("feature")


def test_interactions_pairwise_products() -> None:
    """Pairwise product sent_earnings_x_macro = sent_earnings * sent_macro."""
    from quant_foundry.modules import load_all_modules
    from quant_foundry.modules.registry import ModuleRegistry

    load_all_modules()
    mod = ModuleRegistry.instance().create(
        "feature:interactions:1.0.0",
        config={"event_types": ["earnings", "macro"], "max_interactions": 20},
    )

    row = {"sent_earnings": 0.8, "sent_macro": 0.4}
    annotated = mod.annotate_row(row)

    # earnings < macro alphabetically, so the feature name is sent_earnings_x_macro
    assert "sent_earnings_x_macro" in annotated
    assert abs(annotated["sent_earnings_x_macro"] - 0.32) < 1e-5, (
        f"interaction product {annotated['sent_earnings_x_macro']} != 0.32"
    )


def test_interactions_only_nonzero() -> None:
    """If sent_earnings=0.0, no interaction features for earnings pairs."""
    from quant_foundry.modules import load_all_modules
    from quant_foundry.modules.registry import ModuleRegistry

    load_all_modules()
    mod = ModuleRegistry.instance().create(
        "feature:interactions:1.0.0",
        config={"event_types": ["earnings", "macro", "regulatory"], "max_interactions": 20},
    )

    row = {"sent_earnings": 0.0, "sent_macro": 0.5, "sent_regulatory": 0.3}
    annotated = mod.annotate_row(row)

    # No interaction feature should involve earnings
    interaction_keys = [k for k in annotated if "_x_" in k]
    for key in interaction_keys:
        assert "earnings" not in key, (
            f"interaction feature {key} should not involve earnings (sent_earnings=0)"
        )
    # macro_x_regulatory should still be present
    assert "sent_macro_x_regulatory" in annotated


def test_interactions_max_limit() -> None:
    """With max_interactions=3, only 3 interaction features should be produced."""
    from quant_foundry.modules import load_all_modules
    from quant_foundry.modules.registry import ModuleRegistry

    load_all_modules()
    # Use many event types to generate many candidate pairs
    event_types = [
        "regulatory",
        "earnings",
        "guidance",
        "macro",
        "product",
        "security",
        "litigation",
        "partnership",
    ]
    mod = ModuleRegistry.instance().create(
        "feature:interactions:1.0.0",
        config={"event_types": event_types, "max_interactions": 3},
    )

    # All non-zero → many candidate pairs (8 choose 2 = 28)
    row = {f"sent_{et}": 0.5 + i * 0.01 for i, et in enumerate(event_types)}
    annotated = mod.annotate_row(row)

    interaction_keys = [k for k in annotated if "_x_" in k]
    assert len(interaction_keys) <= 3, (
        f"expected <= 3 interaction features, got {len(interaction_keys)}"
    )
    assert len(interaction_keys) == 3, (
        f"expected exactly 3 interaction features, got {len(interaction_keys)}"
    )


# --------------------------------------------------------------------------- #
# PreEventMomentumFeatures tests                                               #
# --------------------------------------------------------------------------- #


def test_pre_event_momentum_registered() -> None:
    """feature:pre-event-momentum:1.0.0 is in the registry."""
    from quant_foundry.modules import ModuleRegistry, load_all_modules

    load_all_modules()
    registry = ModuleRegistry.instance()
    assert "feature:pre-event-momentum:1.0.0" in registry.list_by_category("feature")


def test_pre_event_momentum_basic() -> None:
    """PreEventMomentumFeatures computes return, volatility, volume features."""
    from quant_foundry.modules import load_all_modules
    from quant_foundry.modules.registry import (
        MediaItem,
        ModuleRegistry,
        SentimentResult,
    )

    load_all_modules()
    mod = ModuleRegistry.instance().create("feature:pre-event-momentum:1.0.0")

    base_ns = int(dt.datetime(2023, 1, 1, tzinfo=dt.UTC).timestamp()) * 1_000_000_000

    # 100 days of bars, decision at day 80
    bars = _make_bars("AAPL", base_ns, 100, seed=42)
    decision_ns = base_ns + 80 * NS_PER_DAY

    items = [
        MediaItem(
            item_id="evt1",
            source="newsapi",
            headline="Earnings event",
            body="",
            available_at_ns=decision_ns,
            symbols=("AAPL",),
            event_type="earnings",
        ),
    ]
    sentiments = [
        SentimentResult(item_id="evt1", provider="naive", score=0.5, confidence=0.5),
    ]

    result = mod.compute_features(
        items,
        sentiments,
        symbols=["AAPL"],
        start_ns=base_ns,
        end_ns=base_ns + 200 * NS_PER_DAY,
        price_bars={"AAPL": bars},
    )

    assert "AAPL" in result
    assert decision_ns in result["AAPL"]
    feats = result["AAPL"][decision_ns]

    assert "pre_return_1d" in feats
    assert "pre_return_5d" in feats
    assert "pre_volatility_20d" in feats
    assert "pre_volatility_60d" in feats
    assert "pre_volume_ratio_5d" in feats

    # Return features should be finite numbers
    assert math.isfinite(feats["pre_return_1d"])
    assert math.isfinite(feats["pre_return_5d"])
    # Volatility should be non-negative
    assert feats["pre_volatility_20d"] >= 0.0
    assert feats["pre_volatility_60d"] >= 0.0


def test_pre_event_momentum_pit_correctness() -> None:
    """Only bars with ts_ns < decision_time are used (no look-ahead)."""
    from quant_foundry.modules import load_all_modules
    from quant_foundry.modules.registry import (
        MediaItem,
        ModuleRegistry,
        PriceBar,
        SentimentResult,
    )

    load_all_modules()
    mod = ModuleRegistry.instance().create("feature:pre-event-momentum:1.0.0")

    base_ns = int(dt.datetime(2023, 1, 1, tzinfo=dt.UTC).timestamp()) * 1_000_000_000
    decision_ns = base_ns + 10 * NS_PER_DAY

    # Create bars: some before, some after the decision time.
    # The bar AT decision_ns should NOT be used (strict <).
    bars = []
    for i in range(20):
        bars.append(
            PriceBar(
                symbol="AAPL",
                ts_ns=base_ns + i * NS_PER_DAY,
                open=100.0 + i,
                high=101.0 + i,
                low=99.0 + i,
                close=100.0 + i,  # close increases by 1 each day
                volume=1_000_000.0,
            )
        )

    items = [
        MediaItem(
            item_id="evt1",
            source="newsapi",
            headline="Event",
            body="",
            available_at_ns=decision_ns,
            symbols=("AAPL",),
            event_type="earnings",
        ),
    ]
    sentiments = [
        SentimentResult(item_id="evt1", provider="naive", score=0.5, confidence=0.5),
    ]

    result = mod.compute_features(
        items,
        sentiments,
        symbols=["AAPL"],
        start_ns=base_ns,
        end_ns=base_ns + 200 * NS_PER_DAY,
        price_bars={"AAPL": bars},
    )

    feats = result["AAPL"][decision_ns]

    # PIT bars: indices 0..9 (ts_ns < decision_ns = base_ns + 10*NS_PER_DAY)
    # Last PIT close = 100.0 + 9 = 109.0
    # pre_return_1d = close[-1] / close[-2] - 1 = 109.0 / 108.0 - 1
    expected_1d = 109.0 / 108.0 - 1.0
    assert abs(feats["pre_return_1d"] - round(expected_1d, 6)) < 1e-5, (
        f"pre_return_1d {feats['pre_return_1d']} != {expected_1d:.6f} "
        f"(look-ahead detected: bar at decision_ns was used)"
    )


def test_pre_event_momentum_insufficient_history() -> None:
    """With too few bars, features should be 0.0."""
    from quant_foundry.modules import load_all_modules
    from quant_foundry.modules.registry import (
        MediaItem,
        ModuleRegistry,
        SentimentResult,
    )

    load_all_modules()
    mod = ModuleRegistry.instance().create("feature:pre-event-momentum:1.0.0")

    base_ns = int(dt.datetime(2023, 1, 1, tzinfo=dt.UTC).timestamp()) * 1_000_000_000
    decision_ns = base_ns + 5 * NS_PER_DAY

    # Only 3 bars — not enough for 20d/60d volatility or 5d return
    bars = _make_bars("AAPL", base_ns, 3, seed=42)

    items = [
        MediaItem(
            item_id="evt1",
            source="newsapi",
            headline="Event",
            body="",
            available_at_ns=decision_ns,
            symbols=("AAPL",),
            event_type="earnings",
        ),
    ]
    sentiments = [
        SentimentResult(item_id="evt1", provider="naive", score=0.5, confidence=0.5),
    ]

    result = mod.compute_features(
        items,
        sentiments,
        symbols=["AAPL"],
        start_ns=base_ns,
        end_ns=base_ns + 200 * NS_PER_DAY,
        price_bars={"AAPL": bars},
    )

    # With only 3 bars before decision (indices 0,1,2,3,4 — but only 3 bars total
    # at ts_ns 0,1,2; decision at day 5, so all 3 bars are PIT).
    # pre_return_1d needs >= 2 bars → OK (3 bars)
    # pre_return_5d needs >= 6 bars → 0.0
    # pre_volatility_20d needs >= 21 bars → 0.0
    # pre_volatility_60d needs >= 61 bars → 0.0
    if "AAPL" in result and decision_ns in result["AAPL"]:
        feats = result["AAPL"][decision_ns]
        assert feats["pre_return_5d"] == 0.0
        assert feats["pre_volatility_20d"] == 0.0
        assert feats["pre_volatility_60d"] == 0.0
