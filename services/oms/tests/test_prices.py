"""Tests for oms.prices.LivePrices."""

from __future__ import annotations

from decimal import Decimal

from oms.prices import LivePrices


def test_get_returns_none_for_unknown_symbol() -> None:
    cache = LivePrices()
    assert cache.get("BTC-USD") is None


def test_update_stores_and_get_returns_latest() -> None:
    cache = LivePrices()
    cache.update("BTC-USD", Decimal("100"))
    assert cache.get("BTC-USD") == Decimal("100")


def test_update_overwrites_prior_value() -> None:
    cache = LivePrices()
    cache.update("BTC-USD", Decimal("100"))
    cache.update("BTC-USD", Decimal("105"))
    assert cache.get("BTC-USD") == Decimal("105")


def test_len_reflects_distinct_symbols() -> None:
    cache = LivePrices()
    cache.update("BTC-USD", Decimal("100"))
    cache.update("ETH-USD", Decimal("3000"))
    assert len(cache) == 2


def test_contains_returns_true_for_known_symbol() -> None:
    cache = LivePrices()
    cache.update("BTC-USD", Decimal("100"))
    assert "BTC-USD" in cache
    assert "ETH-USD" not in cache
