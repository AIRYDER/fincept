from __future__ import annotations

import datetime as _dt
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


class MonotonicClock(Clock):
    def now_ns(self) -> int:
        return time.time_ns()


class FrozenClock(Clock):
    def __init__(self, now_ns: int) -> None:
        self._now_ns = now_ns

    def now_ns(self) -> int:
        return self._now_ns
