"""
features.pit — point-in-time joins of bars with their as-of features.

The join's only job is to enforce one invariant:

    For each bar at time T, the feature returned must satisfy
    ``feature.ts_event <= T``.  No exceptions, no off-by-one.

Violating this is the most common form of leakage in backtests and the
reason walk-forward studies need PIT-correct stores.  ``PITJoiner.join_bars``
groups bars by ``(symbol, freq)``, fetches a single feature range per
group, and walks both lists in lock-step — O(N) over both — using a
two-pointer scan rather than per-bar queries.

If the join would ever return a feature whose ``ts_event > bar.ts_event``,
we raise ``RuntimeError`` rather than continue.  A test pins this.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence

from features.store import OfflineStore
from fincept_core.schemas import BarEvent, FeatureFrame

# How far back to look for an as-of feature when none has been recorded
# in the bar's day.  One year covers any realistic gap between the start
# of a backtest and the first feature emission.
_LOOKBACK_NS = 365 * 86_400 * 1_000_000_000


class PITJoiner:
    """Join each bar with the latest FeatureFrame whose ``ts_event <= bar.ts_event``."""

    def __init__(self, store: OfflineStore, *, lookback_ns: int = _LOOKBACK_NS) -> None:
        self._store = store
        self._lookback_ns = lookback_ns

    async def join_bars(
        self, bars: Sequence[BarEvent]
    ) -> list[tuple[BarEvent, FeatureFrame | None]]:
        """Return ``[(bar, latest_feature_or_None), ...]`` in input order.

        Bars without any preceding feature on the same ``(symbol, freq)``
        return ``None`` for the feature slot — consumers must handle that.
        """
        if not bars:
            return []

        # Group by (symbol, freq) so we can fetch a single range per group
        # rather than one query per bar.
        groups: dict[tuple[str, str], list[tuple[int, BarEvent]]] = defaultdict(list)
        for idx, bar in enumerate(bars):
            groups[(bar.symbol, bar.freq)].append((idx, bar))

        # Output preserves input order; we slot results in by index.
        out: list[tuple[BarEvent, FeatureFrame | None]] = [(b, None) for b in bars]

        for (symbol, freq), idx_bars in groups.items():
            # idx_bars are in input order, which (for sane callers) is also
            # ts_event order, but we don't rely on that — sort defensively.
            sorted_idx_bars = sorted(idx_bars, key=lambda pair: pair[1].ts_event)
            start = sorted_idx_bars[0][1].ts_event - self._lookback_ns
            end = sorted_idx_bars[-1][1].ts_event + 1  # half-open: include the latest bar's ts
            features = await self._store.read_range(symbol, freq, start, end)

            # Two-pointer walk: features are ascending by ts_event,
            # bars are ascending by ts_event, so we sweep both forward.
            cursor = 0
            n = len(features)
            for original_idx, bar in sorted_idx_bars:
                while cursor < n and features[cursor].ts_event <= bar.ts_event:
                    cursor += 1
                latest = features[cursor - 1] if cursor > 0 else None
                if latest is not None and latest.ts_event > bar.ts_event:
                    # Cannot happen given the loop above, but the assertion
                    # is the entire point of this class — if it ever fires,
                    # something deeper has corrupted the invariant and we
                    # must abort the join rather than emit a leaky pair.
                    raise RuntimeError(
                        f"PIT violation: feature ts {latest.ts_event} > "
                        f"bar ts {bar.ts_event} for {symbol}:{freq}"
                    )
                out[original_idx] = (bar, latest)
        return out
