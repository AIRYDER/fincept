"""
ingestor.quality — two complementary observers.

``LatencyTracker``
    Sync, hot-path counters used by the in-process ingestor for local
    diagnostics: per-(venue, symbol) sequence-gap totals, max latency,
    rolling p99 latency.  Fire-and-forget; never raises.

``QualityMonitor``
    Async, event-driven monitor designed to run as a **separate process**
    consuming ``md.trades`` / ``md.books`` and emitting ``AlertEvent``s on
    the ``events.alerts`` stream.  Detects sequence gaps, clock skew,
    cross-venue spread anomalies, and staleness.

Why two classes?  Different observation windows.  ``LatencyTracker`` is
in-process only — its counters live and die with the ingestor.  The
``QualityMonitor`` is a long-lived watchdog that survives ingestor
restarts, dedupes alerts across the fleet, and emits operationally
actionable signals to the alerts stream.  Keeping them in one module
makes it obvious they observe the same domain.

Design rules (per spec/CONTRACTS.md and TASK-014):

  - Top-of-book is tracked **only on snapshots**.  Computing top-of-book
    from deltas requires maintaining the full book in this process,
    which is intentionally out of scope.  Binance never emits snapshots,
    so cross-spread is structurally Coinbase ↔ Kraken in the default
    setup; that's documented and correct.
  - Cross-venue comparison is by **canonical symbol**.  ``BTC-USDT``
    (Binance) and ``BTC-USD`` (Coinbase) are different markets and
    will not be compared.  Tether-stable groupings are deferred to a
    later config-driven enhancement.
  - Alert dedup keys on ``(code, frozenset(tags.items()))`` with a
    30 s TTL — identical alerts within the window are suppressed.
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Callable, Iterable
from decimal import Decimal
from typing import NamedTuple, Protocol

from fincept_bus.streams import STREAM_ALERTS
from fincept_core.clock import now_ns
from fincept_core.events import Event
from fincept_core.ids import new_id
from fincept_core.logging import get_logger
from fincept_core.schemas import (
    AlertEvent,
    BookDeltaEvent,
    BookSnapshotEvent,
    TradeEvent,
)

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Defaults — tunable via constructor; spec-aligned values live here so a
# future settings module can override them without changing call sites.
# ---------------------------------------------------------------------------

STALENESS_BUDGET_NS = 30_000_000_000  # 30 s
CLOCK_SKEW_BUDGET_NS = 1_000_000_000  # 1 s
CROSS_SPREAD_THRESHOLD_BPS = Decimal(50)  # 0.5 %
DEDUP_WINDOW_NS = 30_000_000_000  # 30 s
STALENESS_LOOP_INTERVAL_S = 5.0


# ---------------------------------------------------------------------------
# LatencyTracker — sync, in-process counters (was QualityMonitor in TASK-010)
# ---------------------------------------------------------------------------


class Snapshot(NamedTuple):
    """Per-symbol metrics emitted by ``LatencyTracker.snapshot()``."""

    key: str
    last_seq: int | None
    total_gaps: int
    max_latency_ns: int
    p99_latency_ns: int
    samples: int


class LatencyTracker:
    """Sync per-(venue, symbol) gap + latency observer for the ingestor process."""

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
            if last is None or seq > last:
                self.last_seq[key] = seq

        latency = ts_recv - ts_event
        if latency < 0:
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


# ---------------------------------------------------------------------------
# QualityMonitor — async, event-driven, emits AlertEvents
# ---------------------------------------------------------------------------


class _AlertProducer(Protocol):
    """The minimum surface ``QualityMonitor`` needs from a producer.

    Both ``fincept_bus.Producer`` and the ``_FakeProducer`` used in tests
    satisfy this — explicit Protocol means tests don't need to subclass
    ``Producer`` and pull in a real Redis client.
    """

    async def publish(self, stream: str, event: Event) -> str: ...


class QualityMonitor:
    """Event-driven quality watchdog.

    Consumes ``TradeEvent`` / ``BookSnapshotEvent`` / ``BookDeltaEvent``
    via :meth:`on_trade` and :meth:`on_book`, emits ``AlertEvent``s through
    the injected ``producer`` for sequence gaps, clock skew, cross-venue
    spread anomalies, and staleness.  ``staleness_loop`` is the periodic
    coroutine that fires the staleness check.
    """

    def __init__(
        self,
        producer: _AlertProducer,
        *,
        staleness_budget_ns: int = STALENESS_BUDGET_NS,
        clock_skew_budget_ns: int = CLOCK_SKEW_BUDGET_NS,
        cross_spread_threshold_bps: Decimal = CROSS_SPREAD_THRESHOLD_BPS,
        dedup_window_ns: int = DEDUP_WINDOW_NS,
        clock: Callable[[], int] = now_ns,
        id_factory: Callable[[], str] = new_id,
    ) -> None:
        self._producer = producer
        self._staleness_budget_ns = staleness_budget_ns
        self._clock_skew_budget_ns = clock_skew_budget_ns
        self._cross_spread_threshold_bps = cross_spread_threshold_bps
        self._dedup_window_ns = dedup_window_ns
        self._clock = clock
        self._new_id = id_factory

        # Last activity timestamp per (venue, symbol) — drives staleness.
        self._last_ts: dict[tuple[str, str], int] = {}
        # Last venue-assigned seq seen on trades — drives gap detection.
        self._last_seq: dict[tuple[str, str], int] = {}
        # Last (best_bid, best_ask) per (venue, symbol) — drives cross-spread.
        self._last_top: dict[tuple[str, str], tuple[Decimal, Decimal]] = {}
        # Dedup table: alert key → emit-time-ns.  Pruned lazily on hit.
        self._recent_alerts: dict[tuple[str, frozenset[tuple[str, str]]], int] = {}

    # ------------------------------------------------------------------
    # Public hooks
    # ------------------------------------------------------------------

    async def on_trade(self, ev: TradeEvent) -> None:
        key = (ev.venue.value, ev.symbol)

        if ev.seq is not None:
            prev = self._last_seq.get(key)
            if prev is not None and ev.seq != prev + 1:
                # Out-of-order delivery (seq <= prev) is also a gap signal —
                # operators want to know about both directions.
                await self._emit(
                    code="seq_gap",
                    severity="warning",
                    message=f"seq gap on {ev.venue.value}:{ev.symbol}: {prev} -> {ev.seq}",
                    tags={
                        "venue": ev.venue.value,
                        "symbol": ev.symbol,
                        "prev": str(prev),
                        "current": str(ev.seq),
                    },
                )
            # Advance forward only — out-of-order shouldn't fake a gap on the
            # next monotonic message (mirrors LatencyTracker semantics).
            if prev is None or ev.seq > prev:
                self._last_seq[key] = ev.seq

        self._last_ts[key] = ev.ts_recv

        skew = ev.ts_recv - ev.ts_event
        if skew > self._clock_skew_budget_ns:
            await self._emit(
                code="clock_skew",
                severity="warning",
                message=(f"clock skew on {ev.venue.value}:{ev.symbol}: {skew / 1e9:.2f}s"),
                tags={
                    "venue": ev.venue.value,
                    "symbol": ev.symbol,
                    "skew_ns": str(skew),
                },
            )

    async def on_book(self, ev: BookSnapshotEvent | BookDeltaEvent) -> None:
        key = (ev.venue.value, ev.symbol)
        self._last_ts[key] = ev.ts_recv
        if isinstance(ev, BookSnapshotEvent) and ev.bids and ev.asks:
            best_bid = max(b.price for b in ev.bids)
            best_ask = min(a.price for a in ev.asks)
            self._last_top[key] = (best_bid, best_ask)
            await self._check_cross_venue(ev.symbol)

    async def staleness_check(self) -> None:
        """One-shot staleness sweep — exposed for tests + the loop."""
        now = self._clock()
        for (venue, symbol), ts in list(self._last_ts.items()):
            silent = now - ts
            if silent > self._staleness_budget_ns:
                await self._emit(
                    code="stale",
                    severity="warning",
                    message=(f"no events on {venue}:{symbol} for {silent / 1e9:.1f}s"),
                    tags={
                        "venue": venue,
                        "symbol": symbol,
                        "silent_ns": str(silent),
                    },
                )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _check_cross_venue(self, symbol: str) -> None:
        """Compare best mid across all venues currently tracking *symbol*."""
        tops = {
            venue: (bid, ask)
            for (venue, sym), (bid, ask) in self._last_top.items()
            if sym == symbol
        }
        if len(tops) < 2:
            return
        mids = {v: (b + a) / Decimal(2) for v, (b, a) in tops.items()}
        max_mid = max(mids.values())
        min_mid = min(mids.values())
        if min_mid <= 0:
            return  # degenerate input — never alert on garbage
        spread_bps = (max_mid - min_mid) / min_mid * Decimal(10000)
        if spread_bps > self._cross_spread_threshold_bps:
            tags: dict[str, str] = {
                "symbol": symbol,
                "spread_bps": f"{spread_bps:.2f}",
            }
            for venue, mid in mids.items():
                tags[f"mid_{venue}"] = str(mid)
            await self._emit(
                code="cross_spread",
                severity="critical",
                message=(f"cross-venue mid divergence {spread_bps:.1f} bps on {symbol}"),
                tags=tags,
            )

    async def _emit(
        self,
        *,
        code: str,
        severity: str,
        message: str,
        tags: dict[str, str],
    ) -> None:
        if self._is_duplicate(code, tags):
            return
        alert = AlertEvent(
            alert_id=self._new_id(),
            ts_event=self._clock(),
            severity=severity,
            source="ingestor.quality",
            code=code,
            message=message,
            tags=tags,
        )
        await self._producer.publish(STREAM_ALERTS, Event(type="alert", payload=alert))
        log.warning(
            "quality.alert",
            code=code,
            severity=severity,
            message=message,
            **tags,
        )

    def _is_duplicate(self, code: str, tags: dict[str, str]) -> bool:
        """Return True if (code, tags) was emitted within ``dedup_window_ns``."""
        key = (code, frozenset(tags.items()))
        now = self._clock()
        prev = self._recent_alerts.get(key)
        if prev is not None and now - prev < self._dedup_window_ns:
            return True
        self._recent_alerts[key] = now
        # Cheap GC: if the table grows beyond ~1024 keys, drop everything older
        # than the dedup window so it doesn't grow without bound.
        if len(self._recent_alerts) > 1024:
            cutoff = now - self._dedup_window_ns
            self._recent_alerts = {k: t for k, t in self._recent_alerts.items() if t >= cutoff}
        return False
