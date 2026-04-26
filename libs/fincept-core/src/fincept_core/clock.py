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
    return int(_dt.datetime.fromisoformat(iso).timestamp() * 1_000_000_000)


class MonotonicClock(Clock):
    def now_ns(self) -> int:
        return time.time_ns()


class FrozenClock(Clock):
    def __init__(self, now_ns: int) -> None:
        self._now_ns = now_ns

    def now_ns(self) -> int:
        return self._now_ns
