from __future__ import annotations

import datetime as _dt
import re
import time
from abc import ABC, abstractmethod


class Clock(ABC):
    @abstractmethod
    def now_ns(self) -> int:
        raise NotImplementedError


def now_ns() -> int:
    return time.time_ns()


def ns_to_iso(ns: int) -> str:
    return _dt.datetime.fromtimestamp(ns / 1_000_000_000, tz=_dt.UTC).isoformat()


def iso_to_ns(iso: str) -> int:
    """Convert an ISO-8601 string to integer nanoseconds.

    Uses integer arithmetic throughout to avoid float precision loss.
    ``datetime.timestamp()`` returns a float whose mantissa (~15-16 significant
    digits) is too narrow for nanosecond timestamps (~19 digits), causing silent
    truncation.  Instead we compute seconds * 1e9 + microseconds * 1e3 as ints.
    """
    dt = _dt.datetime.fromisoformat(iso)
    # Normalize to UTC if naive (treat as UTC, matching timestamp() semantics
    # for naive datetimes on most platforms).
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.UTC)
    # Epoch seconds as integer (fromtimestamp floor would lose sub-second
    # precision, so we split into seconds + microseconds).
    epoch = _dt.datetime(1970, 1, 1, tzinfo=_dt.UTC)
    delta = dt - epoch
    total_seconds = delta.days * 86_400 + delta.seconds
    return total_seconds * 1_000_000_000 + delta.microseconds * 1_000


# ---------------------------------------------------------------------------
# Frequency → bars/year mapping (shared by backtester, fincept-tools, etc.)
# ---------------------------------------------------------------------------

# Common frequencies with precomputed annualization factors.
_COMMON_BARS_PER_YEAR: dict[str, int] = {
    "1m": 365 * 24 * 60,      # 525,600
    "5m": 365 * 24 * 12,      # 105,120
    "15m": 365 * 24 * 4,      # 35,040
    "1h": 365 * 24,           # 8,760
    "1d": 252,                # 252 trading days
}

_MINUTES_PER_YEAR = 525_600
_UNIT_TO_MINUTES: dict[str, float] = {
    "s": 1 / 60,
    "m": 1,
    "h": 60,
    "d": 60 * 24,
    "w": 60 * 24 * 7,
}


def bars_per_year_for_freq(freq: str) -> int:
    """Map a bar frequency string to the number of bars per year.

    Supports common shorthand frequencies (1m, 5m, 15m, 1h, 1d) and
    arbitrary ``<N><unit>`` patterns (e.g., 30m, 4h, 1w) via parsing.
    Falls back to 1-minute bars (525,600/year) for unknown formats.

    For daily and weekly frequencies, uses trading-day conventions
    (252 days/year, 52 weeks/year) rather than calendar days.
    """
    if freq in _COMMON_BARS_PER_YEAR:
        return _COMMON_BARS_PER_YEAR[freq]

    match = re.match(r"^(\d+)([smhdw])$", freq)
    if match is None:
        return _COMMON_BARS_PER_YEAR["1m"]

    n = int(match.group(1))
    unit = match.group(2)
    if unit == "d":
        return max(1, 252 // n) if n <= 252 else 1
    if unit == "w":
        return max(1, 52 // n) if n <= 52 else 1
    minutes_per_bar = n * _UNIT_TO_MINUTES[unit]
    return max(1, int(_MINUTES_PER_YEAR / minutes_per_bar))


class MonotonicClock(Clock):
    def now_ns(self) -> int:
        return time.time_ns()


class FrozenClock(Clock):
    def __init__(self, now_ns: int) -> None:
        self._now_ns = now_ns

    def now_ns(self) -> int:
        return self._now_ns
