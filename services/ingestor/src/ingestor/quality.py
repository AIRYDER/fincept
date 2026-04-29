"""
ingestor.quality — gap + latency observers.

For each canonical event we track:
  - **Sequence gaps**: when a venue assigns a monotonic ``seq``, we count
    missing IDs between consecutive observations.  Useful for detecting
    dropped frames or out-of-order WebSocket messages.
  - **Exchange-to-process latency**: ``ts_recv - ts_event`` per event;
    we track running max and a rolling p99.

The monitor is *passive* — it never raises.  Operators wire it up to a
periodic emitter (Prometheus / OTel metrics) outside this module.

Design choice: ``QualityMonitor.observe`` is sync because it's called on
the hot path and we want zero await overhead.  Snapshots are likewise
sync.
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Iterable
from typing import NamedTuple

from fincept_core.logging import get_logger

log = get_logger(__name__)


class Snapshot(NamedTuple):
    """Per-symbol metrics emitted by ``QualityMonitor.snapshot()``."""

    key: str
    last_seq: int | None
    total_gaps: int
    max_latency_ns: int
    p99_latency_ns: int
    samples: int


class QualityMonitor:
    """Tracks per-(venue, symbol) sequence gaps and arrival latency."""

    def __init__(self, latency_window: int = 1024) -> None:
        if latency_window < 16:
            raise ValueError("latency_window must be >= 16 for a meaningful p99")
        self._latency_window = latency_window
        self.last_seq: dict[str, int] = {}
        self.gaps: dict[str, int] = defaultdict(int)
        self.max_latency_ns: dict[str, int] = defaultdict(int)
        self._latency_samples: dict[str, deque[int]] = defaultdict(
            lambda: deque(maxlen=latency_window)
        )

    @staticmethod
    def _key(venue: str, symbol: str) -> str:
        return f"{venue}:{symbol}"

    def observe(
        self,
        venue: str,
        symbol: str,
        seq: int | None,
        ts_event: int,
        ts_recv: int,
    ) -> None:
        """Record a single event for the given (venue, symbol) channel."""
        key = self._key(venue, symbol)

        if seq is not None:
            last = self.last_seq.get(key)
            if last is not None and seq > last + 1:
                gap_size = seq - last - 1
                self.gaps[key] += gap_size
                log.warning("md.gap", key=key, gap=gap_size)
            # Always advance to seq even if it's a regression — the venue is
            # the source of truth and a regression is itself a quality signal
            # (out-of-order delivery), but we choose not to overwrite ``last``
            # backwards so a transient OOO doesn't fake a gap on the next msg.
            if last is None or seq > last:
                self.last_seq[key] = seq

        latency = ts_recv - ts_event
        if latency < 0:
            # Clock skew between venue and host; clamp to 0 rather than emit
            # nonsense.  Negative latency would corrupt the p99 histogram.
            latency = 0
        if latency > self.max_latency_ns[key]:
            self.max_latency_ns[key] = latency
        self._latency_samples[key].append(latency)

    def _p99(self, samples: Iterable[int]) -> int:
        ordered = sorted(samples)
        if not ordered:
            return 0
        idx = max(0, round(0.99 * (len(ordered) - 1)))
        return ordered[idx]

    def snapshot(self) -> list[Snapshot]:
        """Return a frozen view of all per-channel metrics."""
        keys = set(self.last_seq) | set(self.gaps) | set(self._latency_samples)
        return [
            Snapshot(
                key=key,
                last_seq=self.last_seq.get(key),
                total_gaps=self.gaps.get(key, 0),
                max_latency_ns=self.max_latency_ns.get(key, 0),
                p99_latency_ns=self._p99(self._latency_samples.get(key, [])),
                samples=len(self._latency_samples.get(key, [])),
            )
            for key in sorted(keys)
        ]
