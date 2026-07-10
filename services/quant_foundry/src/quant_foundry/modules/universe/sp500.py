"""
quant_foundry.modules.universe.sp500 — S&P 500 universe selectors.

Two modules are provided:

``universe:sp500:1.0.0`` (static)
    Returns the *current* S&P 500 constituents.  This is a static list
    that does not account for historical membership changes — it suffers
    from survivorship bias (stocks added to the index later appear in
    earlier backtests).  Kept for backward compatibility.

``universe:sp500-pit:1.0.0`` (point-in-time)
    Uses point-in-time membership loaded from
    ``data/sp500_changes.json``.  For a given ``(start_ns, end_ns)``
    range, returns only the tickers that were actually in the S&P 500
    at *any point* during that period.  This eliminates both
    survivorship bias (only current members) and exclusion bias (only
    stocks that survived the full period).

The PIT module exposes three helper functions:

- :func:`_load_sp500_changes` — load + validate the changes JSON.
- :func:`_constituents_at_time` — the member set at a single instant.
- :func:`_constituents_during_range` — all members at any point in a range.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
from typing import Any

from quant_foundry.modules.registry import (
    ModuleInfo,
    register_module,
)

#: Nanoseconds per day — used to convert ``date`` strings in the changes
#: file to integer nanosecond timestamps comparable to ``start_ns`` /
#: ``end_ns``.
_NS_PER_DAY = 86_400_000_000_000


#: Current S&P 500 constituents (as of 2025).  This is a static list;
#: a future version may load a PIT membership file.
_SP500_TICKERS: tuple[str, ...] = (
    "AAPL",
    "MSFT",
    "AMZN",
    "NVDA",
    "GOOGL",
    "GOOG",
    "META",
    "TSLA",
    "BRK.B",
    "JPM",
    "V",
    "JNJ",
    "WMT",
    "XOM",
    "MA",
    "PG",
    "UNH",
    "HD",
    "CVX",
    "ORCL",
    "ABBV",
    "MRK",
    "KO",
    "PEP",
    "AVGO",
    "COST",
    "MCD",
    "CRM",
    "ADBE",
    "BAC",
    "TMO",
    "ACN",
    "ABT",
    "NFLX",
    "DHR",
    "LIN",
    "TXN",
    "WFC",
    "PM",
    "CSCO",
    "NEE",
    "QCOM",
    "AMD",
    "INTC",
    "LOW",
    "UPS",
    "SPGI",
    "INTU",
    "AMGN",
    "IBM",
    "CAT",
    "GS",
    "RTX",
    "BLK",
    "BA",
    "AMAT",
    "GE",
    "DE",
    "ISRG",
    "AXP",
    "GS",
    "MDT",
    "SYK",
    "ADI",
    "GILD",
    "PLD",
    "BKNG",
    "TMUS",
    "ADP",
    "C",
    "CB",
    "MO",
    "BX",
    "CL",
    "AMT",
    "TJX",
    "MSCI",
    "MMC",
    "REGN",
    "SCHW",
    "LRCX",
    "ETN",
    "SLB",
    "APD",
    "COP",
    "BMY",
    "WMB",
    "FIS",
    "CI",
    "PNC",
    "DUK",
    "SO",
    "NSC",
    "ITW",
    "SHW",
    "ZTS",
    "HUM",
    "SNPS",
    "CDNS",
    "ICE",
    "KLAC",
    "EQIX",
    "WM",
    "MDLZ",
    "FDX",
    "PSX",
    "OXY",
    "EOG",
    "PXD",
    "MPC",
    "VLO",
    "TRV",
    "AON",
    "MCK",
    "MAR",
    "CTAS",
    "ORLY",
    "ECL",
    "KMB",
    "RSG",
    "WELL",
    "AJG",
    "PSA",
    "CMI",
    "APH",
    "ROST",
    "DLR",
    "YUM",
    "TT",
    "BKR",
    "FTNT",
    "ANET",
    "PANW",
    "SNPS",
    "CDNS",
    "CRWD",
    "NOW",
    "TEAM",
    "DDOG",
    "NET",
    "MDB",
    "ZS",
    "PINS",
    "RBLX",
    "U",
    "DASH",
    "ABNB",
    "COIN",
    "PLTR",
    "SOFI",
    "MSTR",
    "MARA",
    "RIOT",
    "HUT",
)

#: Deduplicated, sorted.
_SP500_TICKERS = tuple(sorted(set(_SP500_TICKERS)))


#: Path to the bundled point-in-time membership changes file.
_DEFAULT_CHANGES_PATH = pathlib.Path(__file__).resolve().parent / "data" / "sp500_changes.json"


# --------------------------------------------------------------------------- #
# Static S&P 500 universe (backward compatible)                               #
# --------------------------------------------------------------------------- #


@register_module(
    "universe",
    "sp500",
    "1.0.0",
    default_config={
        "max_symbols": None,  # None = all; set to int for a subset
    },
)
class SP500Universe:
    """S&P 500 universe selector (static, current constituents).

    Returns the S&P 500 ticker list.  Set ``max_symbols`` in the config
    to limit to the first N tickers (useful for quick tests).

    .. note::
        This module suffers from survivorship bias — it always returns
        the *current* constituents regardless of the requested date
        range.  Use :class:`SP500PointInTimeUniverse`
        (``universe:sp500-pit:1.0.0``) for bias-free backtests.
    """

    info: ModuleInfo

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}
        self.max_symbols: int | None = self.config.get("max_symbols")

    def select_symbols(
        self,
        *,
        start_ns: int,
        end_ns: int,
    ) -> list[str]:
        tickers = list(_SP500_TICKERS)
        if self.max_symbols is not None:
            tickers = tickers[: self.max_symbols]
        return tickers


# --------------------------------------------------------------------------- #
# Point-in-time S&P 500 universe                                              #
# --------------------------------------------------------------------------- #


def _date_to_ns(date_str: str) -> int:
    """Convert a ``YYYY-MM-DD`` date string to UTC nanoseconds since epoch."""
    # ``date_str`` may be ``"YYYY-MM-DD"``.  Parse as a naive UTC midnight.
    y, m, d = (int(x) for x in date_str.split("-"))
    return int(dt.datetime(y, m, d, tzinfo=dt.UTC).timestamp()) * 1_000_000_000


def _load_sp500_changes(path: pathlib.Path) -> dict[str, Any]:
    """Load and validate the S&P 500 changes JSON file.

    Args:
        path: Path to the ``sp500_changes.json`` file.

    Returns:
        Parsed dict with keys ``base_constituents_2018`` (list[str]) and
        ``changes`` (list[dict] each with ``date``, ``added``, ``removed``).

    Raises:
        FileNotFoundError: If ``path`` does not exist.
        ValueError: If the file is not valid JSON or is missing required
            keys / has a malformed ``changes`` entry.
    """
    path = pathlib.Path(path)
    if not path.exists():
        raise FileNotFoundError(f"sp500 changes file not found: {path}")

    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError(f"changes file root must be an object, got {type(data)!r}")

    base = data.get("base_constituents_2018")
    if not isinstance(base, list) or not all(isinstance(t, str) for t in base):
        raise ValueError("base_constituents_2018 must be a list of strings")

    changes = data.get("changes")
    if not isinstance(changes, list):
        raise ValueError("changes must be a list")

    for i, entry in enumerate(changes):
        if not isinstance(entry, dict):
            raise ValueError(f"changes[{i}] must be an object, got {type(entry)!r}")
        if "date" not in entry or not isinstance(entry["date"], str):
            raise ValueError(f"changes[{i}].date must be a string")
        # Validate the date parses (raises ValueError on bad format).
        _date_to_ns(entry["date"])
        for key in ("added", "removed"):
            val = entry.get(key, [])
            if not isinstance(val, list) or not all(isinstance(t, str) for t in val):
                raise ValueError(f"changes[{i}].{key} must be a list of strings")

    return data


def _constituents_at_time(changes_data: dict[str, Any], target_ns: int) -> set[str]:
    """Return the set of S&P 500 constituents at a specific point in time.

    Starts from ``base_constituents_2018`` and applies every change whose
    date is strictly before ``target_ns`` (i.e. the change has already
    taken effect by ``target_ns``).

    Args:
        changes_data: Parsed output of :func:`_load_sp500_changes`.
        target_ns: Point-in-time as nanoseconds since epoch.

    Returns:
        Set of tickers that were members at ``target_ns``.
    """
    constituents: set[str] = set(changes_data["base_constituents_2018"])

    # Sort changes by date so they apply chronologically.
    changes = sorted(changes_data["changes"], key=lambda e: e["date"])
    for entry in changes:
        change_ns = _date_to_ns(entry["date"])
        if change_ns > target_ns:
            # Changes are sorted; once we pass the target we can stop.
            break
        # Apply the change (it took effect on or before target_ns).
        constituents.difference_update(entry.get("removed", []))
        constituents.update(entry.get("added", []))

    return constituents


def _constituents_during_range(
    changes_data: dict[str, Any],
    start_ns: int,
    end_ns: int,
) -> set[str]:
    """Return all tickers that were members at any point during ``[start_ns, end_ns]``.

    A stock is included if it was a member of the S&P 500 at *any*
    instant in the closed interval ``[start_ns, end_ns]``.  This means:

    - Stocks removed before ``start_ns`` are excluded (they were not
      members during the period).
    - Stocks added after ``end_ns`` are excluded (they were not yet
      members during the period).
    - Stocks added or removed *during* the period are included (they
      were members for part of the period).

    Implementation: take the membership at ``start_ns`` (the snapshot
    just before the period begins) and union in every ticker that was
    *added* by a change occurring within ``[start_ns, end_ns]``.
    Removals during the period do *not* drop a ticker — it was a member
    for the earlier part of the range.
    """
    if end_ns < start_ns:
        raise ValueError(f"end_ns ({end_ns}) must be >= start_ns ({start_ns})")

    # Membership at the instant the period begins.  We use the snapshot
    # at ``start_ns`` (changes strictly before start_ns have applied).
    members: set[str] = _constituents_at_time(changes_data, start_ns)

    # Union in any ticker added by a change within the period.  A change
    # dated exactly ``start_ns`` is "during" the period (inclusive).
    changes = sorted(changes_data["changes"], key=lambda e: e["date"])
    for entry in changes:
        change_ns = _date_to_ns(entry["date"])
        if change_ns < start_ns:
            continue
        if change_ns > end_ns:
            break
        # Change occurs within [start_ns, end_ns] — additions join the
        # universe (they were members for the latter part of the period).
        members.update(entry.get("added", []))

    return members


@register_module(
    "universe",
    "sp500-pit",
    "1.0.0",
    default_config={
        "max_symbols": None,  # None = all; set to int for a subset
        "changes_path": None,  # None = bundled data file
    },
)
class SP500PointInTimeUniverse:
    """S&P 500 universe with point-in-time membership (no survivorship bias).

    For a given ``(start_ns, end_ns)`` range, returns only the tickers
    that were actually in the S&P 500 during that period.  Stocks added
    after the end date are excluded; stocks removed before the start
    date are also excluded.  Stocks added or removed *during* the period
    are included (they were members for part of the period).

    Uses the historical changes file at ``data/sp500_changes.json`` by
    default; override the path via the ``changes_path`` config key.
    """

    info: ModuleInfo

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}
        self.max_symbols: int | None = self.config.get("max_symbols")
        # ``None`` → use the bundled package data file.
        changes_path = self.config.get("changes_path")
        self.changes_path: pathlib.Path = (
            pathlib.Path(changes_path) if changes_path else _DEFAULT_CHANGES_PATH
        )
        # Lazily loaded on first use so module construction is cheap.
        self._changes_data: dict[str, Any] | None = None

    def _ensure_loaded(self) -> dict[str, Any]:
        if self._changes_data is None:
            self._changes_data = _load_sp500_changes(self.changes_path)
        return self._changes_data

    def select_symbols(
        self,
        *,
        start_ns: int,
        end_ns: int,
    ) -> list[str]:
        changes_data = self._ensure_loaded()
        members = _constituents_during_range(changes_data, start_ns, end_ns)
        tickers = sorted(members)
        if self.max_symbols is not None:
            tickers = tickers[: self.max_symbols]
        return tickers


__all__ = [
    "SP500PointInTimeUniverse",
    "SP500Universe",
    "_constituents_at_time",
    "_constituents_during_range",
    "_load_sp500_changes",
]
