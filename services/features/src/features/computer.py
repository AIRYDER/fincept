"""
features.computer — bar -> FeatureFrame, the shared compute kernel.

Both ``OnlineRunner`` (live, per-bar) and ``offline.backfill`` (batch
replay over historical bars) use this exact class so the bit-identical
guarantee holds: same inputs always produce the same FeatureFrame.

State is per-instance:

  - one ``PriceFeatures`` per symbol
  - one ``VolatilityFeatures`` per symbol
  - one shared ``CrossFeatures`` keyed on the configured benchmark

The class is **stateful by design** — feature transforms are
incremental (deques + rolling means).  Callers must feed bars in
chronological order per symbol.  ``offline.backfill`` enforces this
ordering explicitly; the online runner gets it for free from the bus.
"""

from __future__ import annotations

from features.transforms.cross import CrossFeatures
from features.transforms.price import PriceFeatures
from features.transforms.volatility import VolatilityFeatures
from fincept_core.schemas import BarEvent, FeatureFrame

DEFAULT_BENCHMARK = "BTC-USD"


class FeatureComputer:
    """Stateful per-symbol feature computer; deterministic given bar order."""

    def __init__(self, *, benchmark_symbol: str = DEFAULT_BENCHMARK) -> None:
        self._benchmark = benchmark_symbol
        self._price: dict[str, PriceFeatures] = {}
        self._vol: dict[str, VolatilityFeatures] = {}
        self._cross = CrossFeatures(benchmark_symbol=benchmark_symbol)

    @property
    def benchmark(self) -> str:
        return self._benchmark

    def compute(self, bar: BarEvent) -> FeatureFrame:
        """Update internal state with *bar* and return the merged FeatureFrame."""
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
        return FeatureFrame(
            symbol=sym,
            ts_event=bar.ts_event,
            freq=bar.freq,
            values=merged,
        )
