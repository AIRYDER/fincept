"""
api.symbol_search — typeahead matcher for symbol-input UI.

The dashboard's strategy-create form (and the upcoming manual-order
panel) needs an interactive ticker autocomplete: the operator types
``nvda`` and the UI should immediately suggest ``NVDA``.  Sources of
truth, in priority order:

  1. The configured universe (``read_universe()``) — canonical for
     anything the live ingestor is fanning out.  We include inactive
     rows too so an operator who paused a symbol can still find it.
  2. A curated list of ~150 well-known US equities + major crypto
     pairs (``WELL_KNOWN``) so the typeahead works *before* the
     universe is populated and gives sensible suggestions on a
     blank dashboard.

A future iteration can plug in Alpaca's ``/v2/assets`` for the full
US-equity catalog; the public API of this module won't change.

Matching algorithm
~~~~~~~~~~~~~~~~~~

Each candidate is scored against the query with the following tiers
(higher score = more relevant):

  - 1000  exact case-insensitive match on the symbol
  - 800   exact case-insensitive match on the company name
  - 500   symbol starts with the query (prefix match)
  - 300   any word in the company name starts with the query
  - 200   symbol contains the query as a substring
  - 100   company name contains the query as a substring
  -  50   one-edit Levenshtein match against the symbol (typo tolerance)

Within a tier, ties break by:
  - shorter symbol first (NVDA beats NVDAQ for "nvda")
  - alphabetical second

The function is sync, deterministic, and pure — easy to unit-test.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# --------------------------------------------------------------------------- #
# Curated well-known list                                                     #
# --------------------------------------------------------------------------- #
#
# Mega-cap US equities + commonly traded crypto pairs (Alpaca format,
# i.e. dash-separated).  Kept short on purpose: this is the fallback
# for an empty universe table.  Adding 5000 tickers would ship a
# heavy module for no real benefit -- the universe table or Alpaca
# catalog is the right home for the long tail.

_EQUITIES: list[tuple[str, str]] = [
    # FAANG+ / mega-cap tech
    ("AAPL", "Apple Inc."),
    ("MSFT", "Microsoft Corporation"),
    ("GOOGL", "Alphabet Inc. Class A"),
    ("GOOG", "Alphabet Inc. Class C"),
    ("AMZN", "Amazon.com Inc."),
    ("META", "Meta Platforms Inc."),
    ("NVDA", "NVIDIA Corporation"),
    ("TSLA", "Tesla Inc."),
    ("NFLX", "Netflix Inc."),
    ("AVGO", "Broadcom Inc."),
    ("ORCL", "Oracle Corporation"),
    ("CRM", "Salesforce Inc."),
    ("ADBE", "Adobe Inc."),
    ("AMD", "Advanced Micro Devices Inc."),
    ("INTC", "Intel Corporation"),
    ("QCOM", "QUALCOMM Incorporated"),
    ("CSCO", "Cisco Systems Inc."),
    ("IBM", "International Business Machines"),
    ("UBER", "Uber Technologies Inc."),
    ("LYFT", "Lyft Inc."),
    ("SHOP", "Shopify Inc."),
    ("SQ", "Block Inc."),
    ("PYPL", "PayPal Holdings Inc."),
    ("PLTR", "Palantir Technologies Inc."),
    ("SNOW", "Snowflake Inc."),
    ("CRWD", "CrowdStrike Holdings Inc."),
    ("NOW", "ServiceNow Inc."),
    ("INTU", "Intuit Inc."),
    ("PANW", "Palo Alto Networks Inc."),
    # Consumer
    ("WMT", "Walmart Inc."),
    ("COST", "Costco Wholesale Corporation"),
    ("HD", "Home Depot Inc."),
    ("MCD", "McDonald's Corporation"),
    ("SBUX", "Starbucks Corporation"),
    ("NKE", "Nike Inc."),
    ("DIS", "Walt Disney Company"),
    ("KO", "Coca-Cola Company"),
    ("PEP", "PepsiCo Inc."),
    ("PG", "Procter & Gamble"),
    ("TGT", "Target Corporation"),
    ("LULU", "Lululemon Athletica Inc."),
    ("CMG", "Chipotle Mexican Grill Inc."),
    # Financials
    ("JPM", "JPMorgan Chase & Co."),
    ("BAC", "Bank of America Corporation"),
    ("WFC", "Wells Fargo & Company"),
    ("GS", "Goldman Sachs Group Inc."),
    ("MS", "Morgan Stanley"),
    ("C", "Citigroup Inc."),
    ("BRK.B", "Berkshire Hathaway Class B"),
    ("V", "Visa Inc."),
    ("MA", "Mastercard Incorporated"),
    ("AXP", "American Express Company"),
    ("BLK", "BlackRock Inc."),
    ("SCHW", "Charles Schwab Corporation"),
    ("COIN", "Coinbase Global Inc."),
    ("HOOD", "Robinhood Markets Inc."),
    # Healthcare / pharma
    ("JNJ", "Johnson & Johnson"),
    ("UNH", "UnitedHealth Group Inc."),
    ("LLY", "Eli Lilly and Company"),
    ("PFE", "Pfizer Inc."),
    ("MRK", "Merck & Co. Inc."),
    ("ABBV", "AbbVie Inc."),
    ("TMO", "Thermo Fisher Scientific Inc."),
    ("MRNA", "Moderna Inc."),
    ("NVAX", "Novavax Inc."),
    ("REGN", "Regeneron Pharmaceuticals Inc."),
    # Energy
    ("XOM", "Exxon Mobil Corporation"),
    ("CVX", "Chevron Corporation"),
    ("COP", "ConocoPhillips"),
    ("SLB", "Schlumberger NV"),
    ("OXY", "Occidental Petroleum Corporation"),
    # Industrials / materials
    ("BA", "Boeing Company"),
    ("CAT", "Caterpillar Inc."),
    ("GE", "General Electric Company"),
    ("MMM", "3M Company"),
    ("HON", "Honeywell International Inc."),
    ("LMT", "Lockheed Martin Corporation"),
    ("RTX", "Raytheon Technologies Corporation"),
    ("DE", "Deere & Company"),
    ("UPS", "United Parcel Service Inc."),
    ("FDX", "FedEx Corporation"),
    # Telecom / utilities / autos
    ("T", "AT&T Inc."),
    ("VZ", "Verizon Communications Inc."),
    ("F", "Ford Motor Company"),
    ("GM", "General Motors Company"),
    ("RIVN", "Rivian Automotive Inc."),
    ("LCID", "Lucid Group Inc."),
    # ETFs (popular)
    ("SPY", "SPDR S&P 500 ETF Trust"),
    ("VOO", "Vanguard S&P 500 ETF"),
    ("QQQ", "Invesco QQQ Trust"),
    ("IWM", "iShares Russell 2000 ETF"),
    ("DIA", "SPDR Dow Jones Industrial Average ETF"),
    ("VTI", "Vanguard Total Stock Market ETF"),
    ("ARKK", "ARK Innovation ETF"),
    ("XLE", "Energy Select Sector SPDR Fund"),
    ("XLF", "Financial Select Sector SPDR Fund"),
    ("XLK", "Technology Select Sector SPDR Fund"),
    ("GLD", "SPDR Gold Shares"),
    ("SLV", "iShares Silver Trust"),
    ("TLT", "iShares 20+ Year Treasury Bond ETF"),
    ("HYG", "iShares iBoxx High Yield Corporate Bond ETF"),
]

_CRYPTO: list[tuple[str, str]] = [
    ("BTC-USD", "Bitcoin / US Dollar"),
    ("ETH-USD", "Ethereum / US Dollar"),
    ("SOL-USD", "Solana / US Dollar"),
    ("DOGE-USD", "Dogecoin / US Dollar"),
    ("AVAX-USD", "Avalanche / US Dollar"),
    ("LINK-USD", "Chainlink / US Dollar"),
    ("DOT-USD", "Polkadot / US Dollar"),
    ("MATIC-USD", "Polygon / US Dollar"),
    ("LTC-USD", "Litecoin / US Dollar"),
    ("BCH-USD", "Bitcoin Cash / US Dollar"),
    ("UNI-USD", "Uniswap / US Dollar"),
    ("AAVE-USD", "Aave / US Dollar"),
    ("XRP-USD", "Ripple / US Dollar"),
    ("ADA-USD", "Cardano / US Dollar"),
]

WELL_KNOWN: list[dict[str, str]] = [
    {"symbol": s, "name": n, "asset_class": "equity", "source": "well_known"}
    for s, n in _EQUITIES
] + [
    {"symbol": s, "name": n, "asset_class": "crypto_spot", "source": "well_known"}
    for s, n in _CRYPTO
]


# --------------------------------------------------------------------------- #
# Match scoring                                                               #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SymbolMatch:
    """One scored search candidate."""

    symbol: str
    name: str
    asset_class: str
    score: int
    source: str  # "universe" | "well_known"


def _levenshtein_le1(a: str, b: str) -> bool:
    """True if ``a`` and ``b`` differ by at most one edit.

    A 1-edit metric is a sweet spot for ticker typeahead: it catches
    ``nvdia`` -> ``NVDA`` (not really -- 2 edits) but it does catch
    ``aaple`` -> ``AAPL`` and ``msft`` -> ``MSF`` style fat-fingers.
    Full Levenshtein would slow the scoring down; this O(n) check is
    fast enough to run against the well-known list per keystroke.
    """
    if a == b:
        return True
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    # Substitution case
    if la == lb:
        diffs = sum(1 for x, y in zip(a, b, strict=True) if x != y)
        return diffs <= 1
    # Insertion / deletion: align the shorter against the longer.
    short, long_ = (a, b) if la < lb else (b, a)
    i = j = 0
    edits = 0
    while i < len(short) and j < len(long_):
        if short[i] == long_[j]:
            i += 1
            j += 1
        else:
            edits += 1
            if edits > 1:
                return False
            j += 1
    # Trailing char in long_
    return True


def _score_candidate(query_lc: str, symbol: str, name: str) -> int:
    """Return the integer match score for one candidate against the query."""
    sym_lc = symbol.lower()
    name_lc = name.lower()

    # Tier 1: exact symbol match
    if sym_lc == query_lc:
        return 1000

    # Tier 2: exact name match (rare for tickers, but happens for ETFs)
    if name_lc == query_lc:
        return 800

    # Tier 3: symbol prefix
    if sym_lc.startswith(query_lc):
        # Bonus for a short symbol -- "nvda" prefers NVDA over a 6-char
        # match like "NVDAXY".
        return 500 + max(0, 20 - len(sym_lc))

    # Tier 4: any word in name starts with query
    for word in name_lc.replace(",", " ").replace(".", " ").split():
        if word.startswith(query_lc):
            return 300

    # Tier 5: substring on symbol
    if query_lc in sym_lc:
        # Penalty for late position
        pos = sym_lc.index(query_lc)
        return 200 - min(pos, 50)

    # Tier 6: substring on name
    if query_lc in name_lc:
        return 100

    # Tier 7: 1-edit Levenshtein on symbol (typo tolerance)
    if len(query_lc) >= 2 and _levenshtein_le1(query_lc, sym_lc):
        return 50

    return 0


def search_symbols(
    query: str,
    *,
    universe_rows: list[dict[str, Any]],
    limit: int = 10,
    extras: list[dict[str, Any]] | None = None,
) -> list[SymbolMatch]:
    """Return the top ``limit`` matches for ``query``.

    ``universe_rows`` is the result of ``read_universe()``.  ``extras``
    is an optional list of additional candidates (same shape: dicts
    with ``symbol``, ``asset_class``, optionally ``name``); production
    callers don't need to pass it -- it's only used by tests.

    The merged candidate pool de-duplicates by symbol with
    ``universe_rows`` winning over ``WELL_KNOWN``, so an operator
    who marks a custom name in the universe table sees that name in
    suggestions.
    """
    q = query.strip().lower()
    if not q:
        return []

    # Build a unique-by-symbol pool with universe taking precedence.
    seen: dict[str, dict[str, Any]] = {}
    for row in universe_rows:
        sym = str(row.get("symbol") or "")
        if not sym:
            continue
        seen[sym.upper()] = {
            "symbol": sym,
            "name": str(row.get("name") or row.get("symbol") or ""),
            "asset_class": str(row.get("asset_class") or "equity"),
            "source": "universe",
        }
    for row in WELL_KNOWN + (extras or []):
        sym = str(row.get("symbol") or "")
        key = sym.upper()
        if not sym or key in seen:
            continue
        seen[key] = {
            "symbol": sym,
            "name": str(row.get("name") or sym),
            "asset_class": str(row.get("asset_class") or "equity"),
            "source": str(row.get("source") or "well_known"),
        }

    scored: list[SymbolMatch] = []
    for entry in seen.values():
        score = _score_candidate(q, entry["symbol"], entry["name"])
        if score <= 0:
            continue
        scored.append(
            SymbolMatch(
                symbol=entry["symbol"],
                name=entry["name"],
                asset_class=entry["asset_class"],
                score=score,
                source=entry["source"],
            )
        )

    # Sort: highest score, then shortest symbol, then alphabetical.
    scored.sort(key=lambda m: (-m.score, len(m.symbol), m.symbol))
    return scored[:limit]
