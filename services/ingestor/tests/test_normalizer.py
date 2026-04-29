"""Tests for ingestor.normalizer — symbol-format conversions."""

from __future__ import annotations

from ingestor.normalizer import (
    to_binance_symbol,
    to_canonical,
    to_coinbase_symbol,
    to_kraken_symbol,
)


def test_to_canonical_splits_known_quote_currencies() -> None:
    assert to_canonical("BTCUSDT") == "BTC-USDT"
    assert to_canonical("ETHUSDC") == "ETH-USDC"
    assert to_canonical("ETHBTC") == "ETH-BTC"


def test_to_canonical_prefers_longer_quote_match() -> None:
    """``USDT`` (4 chars) must win over ``USD`` (3 chars)."""
    assert to_canonical("ETHUSDT") == "ETH-USDT"
    assert to_canonical("BTCUSDC") == "BTC-USDC"


def test_to_canonical_uppercases_input() -> None:
    assert to_canonical("btcusdt") == "BTC-USDT"


def test_to_canonical_returns_input_when_no_known_quote() -> None:
    """Unknown suffix → fall back to the upper-cased input untouched."""
    assert to_canonical("WEIRDPAIR") == "WEIRDPAIR"


def test_to_binance_symbol_roundtrip() -> None:
    assert to_binance_symbol("BTC-USDT") == "btcusdt"
    assert to_binance_symbol("ETH-USDC") == "ethusdc"


def test_to_coinbase_symbol_is_canonical() -> None:
    assert to_coinbase_symbol("BTC-USD") == "BTC-USD"
    assert to_coinbase_symbol("eth-usd") == "ETH-USD"


def test_to_kraken_symbol_uses_slash() -> None:
    assert to_kraken_symbol("BTC-USD") == "BTC/USD"
    assert to_kraken_symbol("eth-usd") == "ETH/USD"
