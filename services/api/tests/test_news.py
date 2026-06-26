"""
Tests for /news — composite scoring + 3-lane classification.

The endpoint is data-heavy: each test seeds Redis with a small set of
articles (and the corresponding mark prices) via the fakeredis fixture,
then asserts the lane membership and ordering.

We intentionally exercise both the pure scoring helper (free function,
no Redis) and the integrated route, because the route adds the lane
classification and sorting on top of the score.
"""

from __future__ import annotations

import json
import math
from decimal import Decimal
from typing import Any

import fakeredis.aioredis
from httpx import AsyncClient

from api.routes.news import (
    ADVERSE_BOOST,
    ALERT_PCT_OF_BOOK,
    NS_PER_HOUR,
    RECENCY_HALF_LIFE_H,
    _book_equity_usd,
    _score,
)
from oms.alpaca.marks import write_mark
from oms.alpaca.news_sync import NEWS_INDEX_KEY, _article_key

# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


async def _seed_article(
    redis: fakeredis.aioredis.FakeRedis,
    *,
    article_id: str,
    headline: str,
    symbols: list[str],
    snapshots: dict[str, dict[str, Any]],
    ts_event_ns: int,
    source: str = "Benzinga",
) -> None:
    """Insert one article into the news index + payload store."""
    payload = {
        "id": article_id,
        "headline": headline,
        "summary": f"summary for {article_id}",
        "source": source,
        "url": f"https://example/{article_id}",
        "author": "wire",
        "created_at": "2026-04-30T12:00:00Z",
        "ts_event_ns": ts_event_ns,
        "symbols": symbols,
        "snapshots": snapshots,
    }
    await redis.set(_article_key(article_id), json.dumps(payload))
    await redis.zadd(NEWS_INDEX_KEY, {article_id: ts_event_ns})


def _snap(
    *,
    price_at_publish: str,
    bars: list[list[Any]] | None = None,
    bars_available: bool = True,
) -> dict[str, Any]:
    return {
        "price_at_publish": price_at_publish,
        "bars": bars or [],
        "bars_available": bars_available,
    }


async def _seed_position(
    redis: fakeredis.aioredis.FakeRedis,
    *,
    strategy_id: str,
    symbol: str,
    qty: str,
    avg_cost: str = "100",
) -> None:
    """Write a Position into the shared fake Redis via the real store.

    We exercise the production code path (PositionStore.put) so the
    test stays honest -- if the storage schema ever changes, the test
    breaks alongside the route.
    """
    from fincept_core.schemas import Position
    from portfolio.store import PositionStore

    store = PositionStore(redis)
    await store.put(
        Position(
            strategy_id=strategy_id,
            symbol=symbol,
            quantity=Decimal(qty),
            avg_cost=Decimal(avg_cost),
            realized_pnl=Decimal(0),
            unrealized_pnl=Decimal(0),
            updated_at=0,
        )
    )


# --------------------------------------------------------------------------- #
# Pure scoring helper                                                         #
# --------------------------------------------------------------------------- #


def test_score_zero_impact_returns_zero() -> None:
    """No impact → no score regardless of recency or direction."""
    assert _score(
        base_abs_impact=Decimal(0), age_hours=0.0, is_adverse=True
    ) == Decimal(0)


def test_score_decay_halves_at_half_life() -> None:
    """Score at age=half_life is base × 0.5 (within float epsilon)."""
    base = Decimal("1000")
    fresh = _score(base_abs_impact=base, age_hours=0.0, is_adverse=False)
    aged = _score(base_abs_impact=base, age_hours=RECENCY_HALF_LIFE_H, is_adverse=False)
    ratio = float(aged) / float(fresh)
    assert math.isclose(ratio, 0.5, rel_tol=1e-3), ratio


def test_score_adverse_boost_multiplies_by_constant() -> None:
    """Adverse boost is exactly ADVERSE_BOOST when ages match."""
    base = Decimal("500")
    favourable = _score(base_abs_impact=base, age_hours=2.0, is_adverse=False)
    adverse = _score(base_abs_impact=base, age_hours=2.0, is_adverse=True)
    ratio = float(adverse) / float(favourable)
    assert math.isclose(ratio, float(ADVERSE_BOOST), rel_tol=1e-9)


def test_score_extreme_age_does_not_overflow() -> None:
    """Far-future or far-past ages collapse cleanly, no exception."""
    score = _score(
        base_abs_impact=Decimal("1000"),
        age_hours=10_000.0,
        is_adverse=False,
    )
    # 10000h / 12h ≈ 833 half-lives; clamped to 50 → 1000 × 2^-50 ≈ 9e-13.
    # The point of the test is "doesn't blow up" -- we verify it's
    # finite, non-negative, and at least 10 orders of magnitude smaller
    # than the input (i.e., the decay actually applied).
    assert score >= 0
    assert float(score) < 1e-9
    assert float(score) < float(Decimal("1000")) * 1e-9


# --------------------------------------------------------------------------- #
# Book equity helper                                                          #
# --------------------------------------------------------------------------- #


def test_book_equity_uses_gross_notional() -> None:
    """Long $10k + short $10k = $20k gross, not net 0."""
    book = {
        "AAPL": Decimal("100"),  # long 100 shares
        "MSFT": Decimal("-50"),  # short 50 shares
    }
    marks = {"AAPL": Decimal("100"), "MSFT": Decimal("200")}
    equity = _book_equity_usd(book, marks)
    assert equity == Decimal("20000")


def test_book_equity_skips_marks_without_qty() -> None:
    """A position with qty=0 doesn't contribute (defensive)."""
    book = {"AAPL": Decimal(0), "MSFT": Decimal("10")}
    marks = {"AAPL": Decimal("100"), "MSFT": Decimal("200")}
    assert _book_equity_usd(book, marks) == Decimal("2000")


def test_book_equity_zero_when_no_marks() -> None:
    """Mark-less positions contribute 0 -- conservative."""
    book = {"AAPL": Decimal("100")}
    marks: dict[str, Decimal] = {}
    assert _book_equity_usd(book, marks) == Decimal(0)


# --------------------------------------------------------------------------- #
# Integrated route — auth + empty path                                        #
# --------------------------------------------------------------------------- #


async def test_news_requires_auth(client: AsyncClient) -> None:
    response = await client.get("/news")
    assert response.status_code == 401


async def test_news_returns_three_lanes_when_empty(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    response = await client.get("/news", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["alert"] == []
    assert body["impact"] == []
    assert body["universe"] == []
    assert body["book_equity_usd"] == "0"
    assert body["alert_pct_of_book"] == float(ALERT_PCT_OF_BOOK)
    assert body["recency_half_life_h"] == RECENCY_HALF_LIFE_H


# --------------------------------------------------------------------------- #
# Integrated route — lane classification                                      #
# --------------------------------------------------------------------------- #


async def test_news_promotes_high_impact_story_to_alert_lane(
    fake_redis: fakeredis.aioredis.FakeRedis,
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """A book-touching story above ALERT_PCT_OF_BOOK lands in alert."""
    # Book: 1000 shares of AAPL at $110 mark → $110k gross equity.
    await _seed_position(fake_redis, strategy_id="test", symbol="AAPL", qty="1000")
    await write_mark(fake_redis, "AAPL", Decimal("110"))

    # Article snapshot: published at $100, mark now $110 → +10% pct,
    # +$10k impact = 10% of equity (well above 0.5% threshold).
    await _seed_article(
        fake_redis,
        article_id="big",
        headline="AAPL beats Q2 earnings",
        symbols=["AAPL"],
        snapshots={
            "AAPL": _snap(
                price_at_publish="100",
                bars=[[1, "100"], [2, "105"], [3, "110"]],
            )
        },
        ts_event_ns=NS_PER_HOUR,  # ~1h ago relative to now()
    )

    response = await client.get("/news", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert len(body["alert"]) == 1
    assert body["alert"][0]["id"] == "big"
    assert body["alert"][0]["tier"] == "alert"
    assert body["alert"][0]["has_impact_math"] is True
    assert body["alert"][0]["is_adverse"] is False  # mark > publish, long → favourable
    assert Decimal(body["book_equity_usd"]) == Decimal("110000")
    # pct_of_book = 10000 / 110000 ≈ 0.0909
    assert float(body["alert"][0]["pct_of_book"]) > 0.05
    assert body["impact"] == []


async def test_news_keeps_small_book_story_in_impact_lane(
    fake_redis: fakeredis.aioredis.FakeRedis,
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """A story below the alert threshold stays in impact, not alert."""
    await _seed_position(fake_redis, strategy_id="test", symbol="AAPL", qty="1000")
    await write_mark(fake_redis, "AAPL", Decimal("100.10"))  # +0.1%

    await _seed_article(
        fake_redis,
        article_id="tiny",
        headline="AAPL reshuffles a logo",
        symbols=["AAPL"],
        snapshots={
            "AAPL": _snap(
                price_at_publish="100",
                bars=[[1, "100"], [2, "100.05"], [3, "100.10"]],
            )
        },
        ts_event_ns=NS_PER_HOUR,
    )
    response = await client.get("/news", headers=auth_headers)
    body = response.json()
    # +0.1% on 1000 shares = $100 impact, equity ≈ $100k → 0.1% < 0.5%.
    assert body["alert"] == []
    assert len(body["impact"]) == 1
    assert body["impact"][0]["id"] == "tiny"
    assert body["impact"][0]["tier"] == "impact"


async def test_news_classifies_non_book_story_as_universe(
    fake_redis: fakeredis.aioredis.FakeRedis,
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """A story about a symbol we don't hold belongs in universe."""
    await _seed_position(fake_redis, strategy_id="test", symbol="AAPL", qty="100")
    await write_mark(fake_redis, "AAPL", Decimal("150"))

    await _seed_article(
        fake_redis,
        article_id="msft",
        headline="MSFT acquires Y",
        symbols=["MSFT"],
        snapshots={"MSFT": _snap(price_at_publish="200")},
        ts_event_ns=NS_PER_HOUR,
    )
    response = await client.get("/news", headers=auth_headers)
    body = response.json()
    assert body["alert"] == []
    assert body["impact"] == []
    assert len(body["universe"]) == 1
    assert body["universe"][0]["tier"] == "universe"


async def test_news_only_book_filter_drops_universe(
    fake_redis: fakeredis.aioredis.FakeRedis,
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    await _seed_position(fake_redis, strategy_id="test", symbol="AAPL", qty="100")
    await write_mark(fake_redis, "AAPL", Decimal("150"))

    await _seed_article(
        fake_redis,
        article_id="msft",
        headline="MSFT acquires Y",
        symbols=["MSFT"],
        snapshots={"MSFT": _snap(price_at_publish="200")},
        ts_event_ns=NS_PER_HOUR,
    )
    response = await client.get(
        "/news", headers=auth_headers, params={"only_book": "true"}
    )
    body = response.json()
    assert body["universe"] == []


async def test_news_alert_lane_sorted_by_score_desc(
    fake_redis: fakeredis.aioredis.FakeRedis,
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """Two alert-lane stories: the higher-scoring one comes first.

    We pick a fresh adverse story (boosted) vs an older favourable
    story (no boost) and assert the ordering.
    """
    await _seed_position(fake_redis, strategy_id="test", symbol="AAPL", qty="1000")
    await write_mark(fake_redis, "AAPL", Decimal("90"))  # mark moved -10%

    # Article A: published $100, mark $90 (consistent with current mark)
    # → adverse for our long, fresh.  base_abs = 1000*0.10*100 = $10k.
    await _seed_article(
        fake_redis,
        article_id="adverse_fresh",
        headline="AAPL whistleblower lawsuit",
        symbols=["AAPL"],
        snapshots={
            "AAPL": _snap(
                price_at_publish="100", bars=[[1, "100"], [2, "95"], [3, "90"]]
            )
        },
        ts_event_ns=NS_PER_HOUR,  # 1h ago
    )
    # Article B: published $80, mark $90 → favourable, older.
    # base_abs = 1000*((90-80)/80)*80 = $10k same magnitude, but no
    # adverse boost and 24h decay → e^-2 ≈ 0.135 vs adverse fresh ≈
    # e^-(1/12)*1.3 ≈ 1.198, so adverse-fresh wins ~9x.
    await _seed_article(
        fake_redis,
        article_id="favourable_old",
        headline="AAPL old earnings beat",
        symbols=["AAPL"],
        snapshots={
            "AAPL": _snap(price_at_publish="80", bars=[[1, "80"], [2, "85"], [3, "90"]])
        },
        ts_event_ns=NS_PER_HOUR * 24,  # 24h ago
    )

    response = await client.get("/news", headers=auth_headers)
    body = response.json()
    ids = [a["id"] for a in body["alert"]]
    assert ids[0] == "adverse_fresh"
    assert ids[1] == "favourable_old"
    # And the score ordering matches.
    scores = [a["score"] for a in body["alert"]]
    assert scores[0] > scores[1]


async def test_news_marks_adverse_direction_for_long_position(
    fake_redis: fakeredis.aioredis.FakeRedis,
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    await _seed_position(fake_redis, strategy_id="test", symbol="AAPL", qty="100")
    await write_mark(fake_redis, "AAPL", Decimal("90"))

    await _seed_article(
        fake_redis,
        article_id="bad",
        headline="AAPL guidance cut",
        symbols=["AAPL"],
        snapshots={"AAPL": _snap(price_at_publish="100", bars=[[1, "100"], [2, "90"]])},
        ts_event_ns=NS_PER_HOUR,
    )
    response = await client.get("/news", headers=auth_headers)
    body = response.json()
    # Story may be alert or impact depending on equity threshold; the
    # is_adverse flag is what we're verifying.
    matches = body["alert"] + body["impact"]
    assert len(matches) == 1
    assert matches[0]["is_adverse"] is True


async def test_news_computes_impact_for_legacy_snapshot_with_bars(
    fake_redis: fakeredis.aioredis.FakeRedis,
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    await _seed_position(fake_redis, strategy_id="test", symbol="AAPL", qty="100")
    await write_mark(fake_redis, "AAPL", Decimal("110"))

    await _seed_article(
        fake_redis,
        article_id="legacy_bars",
        headline="AAPL legacy snapshot still has bars",
        symbols=["AAPL"],
        snapshots={
            "AAPL": {
                "price_at_publish": "100",
                "bars": [[1, "100"], [2, "105"]],
            }
        },
        ts_event_ns=NS_PER_HOUR,
    )

    response = await client.get("/news", headers=auth_headers)
    body = response.json()
    matches = body["alert"] + body["impact"]
    assert len(matches) == 1
    story = matches[0]
    assert story["has_impact_math"] is True
    assert Decimal(story["total_dollar_impact"]) == Decimal("1000.0")
    symbol = story["symbols"][0]
    assert symbol["pct_change"] == 0.1
    assert Decimal(symbol["dollar_impact"]) == Decimal("1000.0")


async def test_news_handles_story_without_bars_gracefully(
    fake_redis: fakeredis.aioredis.FakeRedis,
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """No bars → has_impact_math=False → tier='impact', not alert."""
    await _seed_position(fake_redis, strategy_id="test", symbol="AAPL", qty="1000")
    await write_mark(fake_redis, "AAPL", Decimal("100"))

    await _seed_article(
        fake_redis,
        article_id="no_bars",
        headline="AAPL holds press conference",
        symbols=["AAPL"],
        snapshots={
            "AAPL": _snap(price_at_publish="100", bars=[], bars_available=False)
        },
        ts_event_ns=NS_PER_HOUR,
    )
    response = await client.get("/news", headers=auth_headers)
    body = response.json()
    assert body["alert"] == []
    assert len(body["impact"]) == 1
    assert body["impact"][0]["has_impact_math"] is False
    assert body["impact"][0]["tier"] == "impact"
    assert body["impact"][0]["score"] == 0.0


async def test_news_fallback_snapshot_computes_after_mark_moves(
    fake_redis: fakeredis.aioredis.FakeRedis,
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    await _seed_position(fake_redis, strategy_id="test", symbol="AAPL", qty="100")
    await write_mark(fake_redis, "AAPL", Decimal("105"))

    await _seed_article(
        fake_redis,
        article_id="fallback_moved",
        headline="AAPL fallback anchor later moved",
        symbols=["AAPL"],
        snapshots={
            "AAPL": _snap(
                price_at_publish="100",
                bars=[],
                bars_available=False,
            )
        },
        ts_event_ns=NS_PER_HOUR,
    )

    response = await client.get("/news", headers=auth_headers)
    body = response.json()
    matches = body["alert"] + body["impact"]
    assert len(matches) == 1
    story = matches[0]
    assert story["has_impact_math"] is True
    assert Decimal(story["total_dollar_impact"]) == Decimal("500.00")
    assert story["score"] > 0
