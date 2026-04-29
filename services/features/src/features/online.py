"""
features.online — bars-in / FeatureFrame-out runner.

Subscribes to ``md.bars.1m`` via a Redis Streams consumer group, dispatches
each ``BarEvent`` through a shared :class:`FeatureComputer`, then publishes
one merged :class:`FeatureFrame` per bar to ``features.online`` and
optionally writes it to an :class:`OnlineStore` (Redis cache) for fast
agent inference reads.

The compute kernel lives in ``features.computer``; this module is just
the consume/dispatch/publish/cache plumbing.  Sharing the kernel with
``features.offline.backfill`` is what guarantees bit-identical online vs
offline values for the same input bars.

Spec landmine #1 (PIT correctness) holds for free here because bars
arrive in monotonic ``ts_event`` order on a single stream.
"""

from __future__ import annotations

from typing import Protocol

from features.computer import DEFAULT_BENCHMARK, FeatureComputer
from features.store import OnlineStore
from fincept_bus.streams import STREAM_FEATURES_ONLINE
from fincept_core.config import get_settings
from fincept_core.events import Event
from fincept_core.logging import get_logger
from fincept_core.schemas import BarEvent

log = get_logger(__name__)


class _FeaturePublisher(Protocol):
    """Producer surface used by ``OnlineRunner``.  Real impl: ``fincept_bus.Producer``."""

    async def publish(self, stream: str, event: Event) -> str: ...


def _default_benchmark() -> str:
    """First UNIVERSE symbol, or DEFAULT_BENCHMARK if config is empty."""
    universe = get_settings().UNIVERSE
    if universe:
        return universe[0]
    return DEFAULT_BENCHMARK


class OnlineRunner:
    """Consumes BarEvents and publishes merged FeatureFrames per bar.

    ``online_store`` is optional: when wired in (typically by ``main.py``
    against a live Redis), every published FeatureFrame is also cached
    under ``features:{symbol}:{freq}`` for cheap agent reads.  Tests omit
    it so the FakeProducer-only path stays simple.
    """

    def __init__(
        self,
        producer: _FeaturePublisher,
        *,
        benchmark_symbol: str | None = None,
        online_store: OnlineStore | None = None,
    ) -> None:
        self._producer = producer
        self._online_store = online_store
        self._computer = FeatureComputer(benchmark_symbol=benchmark_symbol or _default_benchmark())

    @property
    def benchmark(self) -> str:
        return self._computer.benchmark

    async def handle_event(self, event: Event) -> None:
        """Consumer-loop entrypoint: ignore non-bar events, dispatch bars."""
        payload = event.payload
        if not isinstance(payload, BarEvent):
            return
        await self.on_bar(payload)

    async def on_bar(self, bar: BarEvent) -> None:
        """Compute, publish, and (optionally) cache the FeatureFrame."""
        frame = self._computer.compute(bar)
        await self._producer.publish(
            STREAM_FEATURES_ONLINE, Event(type="feature_frame", payload=frame)
        )
        if self._online_store is not None:
            await self._online_store.put(frame)
        log.debug(
            "features.published",
            symbol=bar.symbol,
            ts_event=bar.ts_event,
            freq=bar.freq,
            populated=sum(1 for v in frame.values.values() if v is not None),
        )
