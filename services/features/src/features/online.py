"""
features.online — bars-in / FeatureFrame-out runner.

Subscribes to ``md.bars.1m`` via a Redis Streams consumer group, dispatches
each ``BarEvent`` through the per-symbol :class:`PriceFeatures`,
:class:`VolatilityFeatures`, and a single shared :class:`CrossFeatures`,
then publishes one merged :class:`FeatureFrame` per bar to
``features.online``.

Why a single ``CrossFeatures`` for all symbols?  Cross features are
benchmark-anchored — every symbol uses the same benchmark deque, so they
share the rolling state.  Per-symbol price/vol state is naturally split
out into ``self._price[sym]`` / ``self._vol[sym]``.

The runner is dependency-injectable for testability:

  - ``producer``           — anything with ``async publish(stream, Event)``;
                             real wiring is ``fincept_bus.Producer(redis)``.
  - ``benchmark_symbol``   — defaults to the first entry of
                             ``Settings.UNIVERSE`` (or ``"BTC-USD"`` if
                             the universe is empty); tests pass it
                             explicitly.

Spec landmine #1 (PIT correctness) holds for free here because bars are
delivered in monotonic ``ts_event`` order on a single stream.
"""

from __future__ import annotations

from typing import Protocol

from features.transforms.cross import CrossFeatures
from features.transforms.price import PriceFeatures
from features.transforms.volatility import VolatilityFeatures
from fincept_bus.streams import STREAM_FEATURES_ONLINE
from fincept_core.config import get_settings
from fincept_core.events import Event
from fincept_core.logging import get_logger
from fincept_core.schemas import BarEvent, FeatureFrame

log = get_logger(__name__)

DEFAULT_BENCHMARK = "BTC-USD"


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
    """Consumes BarEvents and publishes merged FeatureFrames per bar."""

    def __init__(
        self,
        producer: _FeaturePublisher,
        *,
        benchmark_symbol: str | None = None,
    ) -> None:
        self._producer = producer
        self._benchmark = benchmark_symbol or _default_benchmark()
        self._price: dict[str, PriceFeatures] = {}
        self._vol: dict[str, VolatilityFeatures] = {}
        self._cross = CrossFeatures(benchmark_symbol=self._benchmark)

    @property
    def benchmark(self) -> str:
        return self._benchmark

    async def handle_event(self, event: Event) -> None:
        """Consumer-loop entrypoint: ignore non-bar events, dispatch bars."""
        payload = event.payload
        if not isinstance(payload, BarEvent):
            return
        await self.on_bar(payload)

    async def on_bar(self, bar: BarEvent) -> None:
        """Update all per-symbol state and publish a merged FeatureFrame."""
        sym = bar.symbol

        price = self._price.setdefault(sym, PriceFeatures())
        vol = self._vol.setdefault(sym, VolatilityFeatures())

        price_vals = price.update(bar.close)
        log_ret = price_vals.get("ret_log_1")
        vol_vals = vol.update(bar.open, bar.high, bar.low, bar.close, log_ret)

        # The benchmark's own bar must update the bench deque BEFORE we
        # query cross features for it — otherwise the symbol deque grows
        # one element ahead of the bench and the windows misalign.
        if sym == self._benchmark:
            self._cross.on_benchmark_ret(log_ret)
        cross_vals = self._cross.on_symbol_ret(sym, log_ret)

        merged: dict[str, float | None] = {**price_vals, **vol_vals, **cross_vals}
        frame = FeatureFrame(
            symbol=sym,
            ts_event=bar.ts_event,
            freq=bar.freq,
            values=merged,
        )
        await self._producer.publish(
            STREAM_FEATURES_ONLINE, Event(type="feature_frame", payload=frame)
        )
        log.debug(
            "features.published",
            symbol=sym,
            ts_event=bar.ts_event,
            freq=bar.freq,
            populated=sum(1 for v in merged.values() if v is not None),
        )
