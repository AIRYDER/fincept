"""
ingestor.normalizer — symbol-format conversion utilities.

The system uses canonical symbols ``BASE-QUOTE`` (e.g. ``BTC-USDT``).  Each
venue uses its own format (Binance: ``btcusdt``; Coinbase: ``BTC-USD``;
Kraken: ``XBT/USD``).  These helpers live in their own module so future
venue adapters reuse them without importing each other.

Contract: ``to_canonical`` is a *splitter* that picks the longest matching
known quote suffix; ``to_venue`` is the inverse for a specific venue's
casing+separator convention.
"""

from __future__ import annotations

# Quote currencies are tried longest-first so e.g. "USDT" wins over "USD".
_KNOWN_QUOTES: tuple[str, ...] = ("USDT", "USDC", "USD", "BUSD", "BTC", "ETH", "EUR", "GBP")


def to_canonical(venue_symbol: str) -> str:
    """Split a venue symbol like ``BTCUSDT`` into canonical ``BTC-USDT``.

    Falls back to returning the input unchanged when no known quote suffix
    matches (so callers still see something they can log).
    """
    upper = venue_symbol.upper()
    for quote in _KNOWN_QUOTES:
        if upper.endswith(quote) and len(upper) > len(quote):
            base = upper[: -len(quote)]
            return f"{base}-{quote}"
    return upper


def to_binance_symbol(canonical: str) -> str:
    """``BTC-USDT`` → ``btcusdt`` (Binance spot WS format)."""
    return canonical.replace("-", "").lower()


def to_coinbase_symbol(canonical: str) -> str:
    """``BTC-USD`` → ``BTC-USD`` (Coinbase Advanced Trade format).

    Coinbase uses the canonical form directly; this helper exists for
    symmetry with the other venues so callers can stay venue-agnostic.
    """
    return canonical.upper()


def to_kraken_symbol(canonical: str) -> str:
    """``BTC-USD`` → ``BTC/USD`` (Kraken WS format)."""
    return canonical.upper().replace("-", "/")
