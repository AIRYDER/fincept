from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
import time


class Clock(ABC):
    @abstractmethod
    def now_ns(self) -> int:
        raise NotImplementedError


class MonotonicClock(Clock):
    def now_ns(self) -> int:
        return time.time_ns()


@dataclass(frozen=True)
class FrozenClock(Clock):
    value_ns: int

    def now_ns(self) -> int:
        return self.value_ns
