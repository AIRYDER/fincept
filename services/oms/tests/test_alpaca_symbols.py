"""Tests for oms.alpaca.symbols — canonical <-> Alpaca symbol mapping."""

from __future__ import annotations

import pytest

from oms.alpaca.symbols import from_alpaca_symbol, is_crypto_symbol, to_alpaca_symbol

# ---------------------------------------------------------------------------
# is_crypto_symbol
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("symbol", "expected"),
    [
        # Crypto pairs.
        ("BTC-USD", True),
        ("ETH-USD", True),
        ("SOL-USD", True),
        ("BTC-USDT", True),
        ("ETH-USDC", True),
        # Equities (no dash).
        ("AAPL", False),
        ("MSFT", False),
        ("SPY", False),
        # Equity classes WITH dash but non-crypto quote.
        ("BRK-B", False),
        ("BF-A", False),
        # Empty / malformed.
        ("", False),
        ("-", False),
        ("-USD", False),
    ],
)
def test_is_crypto_symbol(symbol: str, expected: bool) -> None:
    assert is_crypto_symbol(symbol) is expected


# ---------------------------------------------------------------------------
# to_alpaca_symbol
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("canonical", "alpaca"),
    [
        ("BTC-USD", "BTC/USD"),
        ("ETH-USD", "ETH/USD"),
        ("BTC-USDT", "BTC/USDT"),
        ("AAPL", "AAPL"),
        ("BRK-B", "BRK-B"),  # equity class - no swap
    ],
)
def test_to_alpaca_symbol(canonical: str, alpaca: str) -> None:
    assert to_alpaca_symbol(canonical) == alpaca


# ---------------------------------------------------------------------------
# from_alpaca_symbol
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("alpaca", "canonical"),
    [
        ("BTC/USD", "BTC-USD"),
        ("ETH/USD", "ETH-USD"),
        ("AAPL", "AAPL"),
        ("BRK-B", "BRK-B"),
    ],
)
def test_from_alpaca_symbol(alpaca: str, canonical: str) -> None:
    assert from_alpaca_symbol(alpaca) == canonical


# ---------------------------------------------------------------------------
# Round-trips
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("canonical", ["BTC-USD", "ETH-USDC", "AAPL", "BRK-B", "SPY"])
def test_canonical_to_alpaca_round_trip(canonical: str) -> None:
    assert from_alpaca_symbol(to_alpaca_symbol(canonical)) == canonical
