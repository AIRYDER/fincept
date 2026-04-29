"""
oms.alpaca.symbols - canonical <-> Alpaca symbol mapping.

Alpaca's symbol formats:

  - Equities: ``AAPL``, ``MSFT``, ``SPY`` - identical to our canonical form.
  - Crypto:   ``BTC/USD``, ``ETH/USD`` - slash separator instead of dash.

Our canonical form (from CONTRACTS.md §1) uses dashes for both, so we
swap on the way out (submission) and on the way back (fill events).

A symbol is considered crypto if it has the dash and a known stable-coin
quote on the right.  This is intentionally narrow: misclassifying an
equity as crypto would corrupt the slash conversion for symbols like
``BRK-B`` (Berkshire Hathaway Class B) which contain a hyphen.
"""

from __future__ import annotations

# Stable-coin / fiat quote currencies that mark a symbol as crypto.  This
# is the canonical right-hand side after ``-`` in our universe (see
# CONTRACTS.md §1 + ingestor.normalizer.to_canonical).
_CRYPTO_QUOTES = frozenset({"USD", "USDT", "USDC", "EUR", "BTC", "ETH"})


def is_crypto_symbol(canonical: str) -> bool:
    """Return True if ``canonical`` is a crypto pair like ``BTC-USD``.

    A canonical symbol is crypto if it contains exactly one ``-`` and the
    right-hand side is a known crypto quote currency.  This excludes
    equity symbols that happen to contain a hyphen (e.g., ``BRK-B``).
    """
    if "-" not in canonical:
        return False
    base, sep, quote = canonical.rpartition("-")
    if not base or sep != "-":
        return False
    return quote in _CRYPTO_QUOTES


def to_alpaca_symbol(canonical: str) -> str:
    """Convert our canonical symbol to Alpaca's wire format.

    >>> to_alpaca_symbol("BTC-USD")
    'BTC/USD'
    >>> to_alpaca_symbol("AAPL")
    'AAPL'
    >>> to_alpaca_symbol("BRK-B")
    'BRK-B'
    """
    if is_crypto_symbol(canonical):
        return canonical.replace("-", "/", 1)
    return canonical


def from_alpaca_symbol(alpaca: str) -> str:
    """Convert Alpaca's wire format back to our canonical form.

    >>> from_alpaca_symbol("BTC/USD")
    'BTC-USD'
    >>> from_alpaca_symbol("AAPL")
    'AAPL'
    """
    if "/" in alpaca:
        return alpaca.replace("/", "-", 1)
    return alpaca
