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

Eviction: ``evict_stale()`` removes per-symbol state for symbols that
haven't been seen within the retention period, preventing unbounded
dict growth when symbols are removed from the universe.
"""

from __future__ import annotations

from features.transforms.cross import CrossFeatures
from features.transforms.price import PriceFeatures
from features.transforms.volatility import VolatilityFeatures
from fincept_core.schemas import BarEvent, FeatureFrame

DEFAULT_BENCHMARK = "BTC-USD"
# Default retention for inactive symbol state (1 hour in nanoseconds).
DEFAULT_STATE_RETENTION_NS = 3_600_000_000_000


class FeatureComputer:
    """Stateful per-symbol feature computer; deterministic given bar order."""

    def __init__(
        self,
        *,
        benchmark_symbol: str = DEFAULT_BENCHMARK,
        state_retention_ns: int = DEFAULT_STATE_RETENTION_NS,
    ) -> None:
        self._benchmark = benchmark_symbol
        self._price: dict[str, PriceFeatures] = {}
        self._vol: dict[str, VolatilityFeatures] = {}
        self._last_seen: dict[str, int] = {}
        self._state_retention_ns = state_retention_ns
        self._cross = CrossFeatures(benchmark_symbol=benchmark_symbol)
        self._evicted_count = 0

    @property
    def benchmark(self) -> str:
        return self._benchmark

    @property
    def total_evicted(self) -> int:
        """Total symbol entries evicted since construction."""
        return self._evicted_count

    @property
    def cached_symbols(self) -> int:
        """Number of symbols currently in the price/vol caches."""
        return len(self._price)

    def compute(self, bar: BarEvent) -> FeatureFrame:
        """Update internal state with *bar* and return the merged FeatureFrame."""
        sym = bar.symbol
        price = self._price.setdefault(sym, PriceFeatures())
        vol = self._vol.setdefault(sym, VolatilityFeatures())
        self._last_seen[sym] = bar.ts_event

        price_vals = price.update(bar.close)
        log_ret = price_vals.get("ret_log_1")
        vol_vals = vol.update(bar.open, bar.high, bar.low, bar.close, log_ret)

        # The benchmark's own bar must update the bench deque BEFORE we
        # query cross features for it — otherwise the symbol deque grows
        # one element ahead of the bench and the windows misalign.
        if sym == self._benchmark:
            self._cross.on_benchmark_ret(log_ret)
        cross_vals = self._cross.on_symbol_ret(sym, log_ret, ts_event=bar.ts_event)

        merged: dict[str, float | None] = {**price_vals, **vol_vals, **cross_vals}
        return FeatureFrame(
            symbol=sym,
            ts_event=bar.ts_event,
            freq=bar.freq,
            values=merged,
        )

    def evict_stale(self, *, now_ns: int | None = None) -> int:
        """Remove per-symbol state for symbols inactive beyond retention.

        Returns the number of symbols evicted.  If ``now_ns`` is None,
        uses the maximum ts_event across all tracked symbols as the
        reference time (suitable for offline backfill where wall-clock
        time doesn't apply).

        Evicted symbols are re-initialized if a bar for them arrives
        later — no data loss for active symbols.
        """
        if not self._last_seen:
            return 0
        ref = now_ns if now_ns is not None else max(self._last_seen.values())
        evict: list[str] = []
        for sym, ts in self._last_seen.items():
            if ref - ts > self._state_retention_ns:
                evict.append(sym)
        for sym in evict:
            self._price.pop(sym, None)
            self._vol.pop(sym, None)
            self._last_seen.pop(sym, None)
        # Also evict from cross features.
        self._cross.evict_stale(now_ns=ref, retention_ns=self._state_retention_ns)
        self._evicted_count += len(evict)
        return len(evict)
