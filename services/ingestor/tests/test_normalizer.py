"""Tests for ingestor.normalizer — symbol-format conversions."""

from __future__ import annotations

from ingestor.normalizer import (
    from_kraken_symbol,
    iso8601_to_ns,
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


def test_to_kraken_symbol_maps_btc_to_xbt() -> None:
    """Kraken's legacy Bitcoin ticker is ``XBT``; the helper applies the swap."""
    assert to_kraken_symbol("BTC-USD") == "XBT/USD"
    assert to_kraken_symbol("eth-usd") == "ETH/USD"


def test_from_kraken_symbol_inverse_of_to_kraken_symbol() -> None:
    """Round-trip canonical → Kraken → canonical must be lossless."""
    assert from_kraken_symbol("XBT/USD") == "BTC-USD"
    assert from_kraken_symbol("ETH/USD") == "ETH-USD"
    assert from_kraken_symbol(to_kraken_symbol("BTC-USD")) == "BTC-USD"
    assert from_kraken_symbol(to_kraken_symbol("ETH-USD")) == "ETH-USD"


def test_iso8601_to_ns_handles_z_suffix() -> None:
    """Accept the trailing ``Z`` Coinbase / Kraken emit."""
    # 2024-12-01T12:00:00Z = 1733054400 epoch s = 1_733_054_400_000_000_000 ns.
    assert iso8601_to_ns("2024-12-01T12:00:00Z") == 1_733_054_400_000_000_000


def test_iso8601_to_ns_preserves_microseconds() -> None:
    """Sub-second precision must round-trip via integer math, not float seconds."""
    assert iso8601_to_ns("2024-12-01T12:00:00.123456Z") == 1_733_054_400_123_456_000


def test_iso8601_to_ns_falls_back_on_garbage_input() -> None:
    """Unparseable / empty input returns now_ns(); we just check it's > 0."""
    assert iso8601_to_ns("") > 0
    assert iso8601_to_ns("not a timestamp") > 0
