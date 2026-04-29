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

from datetime import datetime

from fincept_core.clock import now_ns

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
    """``BTC-USD`` → ``XBT/USD`` (Kraken v2 WS format).

    Kraken's legacy ticker for Bitcoin is ``XBT``; canonical (and every other
    venue) uses ``BTC``.  This helper applies the substitution and the
    separator change atomically so callers never juggle both.
    """
    return canonical.upper().replace("BTC", "XBT").replace("-", "/")


def from_kraken_symbol(venue_symbol: str) -> str:
    """``XBT/USD`` → ``BTC-USD`` (Kraken → canonical).

    Inverse of :func:`to_kraken_symbol`.  Used by the Kraken adapter to map
    inbound symbols back to canonical form.
    """
    return venue_symbol.upper().replace("XBT", "BTC").replace("/", "-")


def iso8601_to_ns(s: str) -> int:
    """ISO-8601 UTC timestamp → integer nanoseconds since epoch.

    Accepts ``2024-12-01T12:00:00.123Z`` (trailing ``Z``) or the explicit
    ``+00:00`` offset form.  Returns ``now_ns()`` if the input is empty or
    unparseable — callers should log separately if they need to distinguish.

    Central to Coinbase and Kraken adapters so timestamp handling is a single
    place to audit for float-precision issues.
    """
    if not s:
        return now_ns()
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return now_ns()
    # ``dt.timestamp()`` returns float seconds; convert to ns via integer math
    # on the epoch seconds and microseconds to avoid float drift for sub-second
    # precision (important for cross-venue latency arithmetic).
    epoch_s = int(dt.timestamp())
    return epoch_s * 1_000_000_000 + dt.microsecond * 1_000
