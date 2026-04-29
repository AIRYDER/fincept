"""
features.offline — batch backfill of historical bars into the feature store.

Single entry point: :func:`backfill`.  It reads bars from Timescale
(via ``fincept_db.bars.read_bars`` by default, injectable for tests),
drives them through a single ``FeatureComputer`` instance, and writes
the resulting ``FeatureFrame``s to the offline store.

Ordering rules:

  - Process the **benchmark symbol first** so its returns populate the
    cross-feature deque before any other symbol asks for ``beta_*`` or
    ``corr_*``.  Non-benchmark symbols processed before the benchmark
    would emit ``None`` for cross features even on bars where the live
    runner had data — breaking bit-identical.
  - Within a symbol, bars are processed in ``ts_event`` ascending order
    (``read_bars`` returns sorted results).

Idempotency: the offline store uses ``ON CONFLICT DO UPDATE`` so re-running
the same range overwrites prior values.  This is the right behavior for
a "fix a transform bug, re-run" workflow.  The trade-off is documented
in TASK-017's spec landmines.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence

from features.computer import DEFAULT_BENCHMARK, FeatureComputer
from features.store import OfflineStore
from fincept_core.logging import get_logger
from fincept_core.schemas import BarEvent
from fincept_db.bars import read_bars

log = get_logger(__name__)

BarReader = Callable[[str, str, int, int], Awaitable[list[BarEvent]]]


async def backfill(
    symbols: Sequence[str],
    freq: str,
    start_ns: int,
    end_ns: int,
    *,
    benchmark: str = DEFAULT_BENCHMARK,
    store: OfflineStore | None = None,
    bar_reader: BarReader | None = None,
) -> int:
    """Re-compute features over ``[start_ns, end_ns)`` and persist them.

    Returns the total number of feature rows written.  Bench-first ordering
    is enforced internally even if the caller passes symbols in a different
    order.
    """
    if not symbols:
        log.info("features.backfill.empty_symbols")
        return 0
    if start_ns >= end_ns:
        raise ValueError(f"start_ns {start_ns} must be < end_ns {end_ns}")

    offline = store if store is not None else OfflineStore()
    reader: BarReader = bar_reader if bar_reader is not None else read_bars

    # Bench-first: see module docstring for why.
    sym_order: list[str] = []
    if benchmark in symbols:
        sym_order.append(benchmark)
    sym_order.extend(s for s in symbols if s != benchmark)

    computer = FeatureComputer(benchmark_symbol=benchmark)
    written_total = 0
    for sym in sym_order:
        bars = await reader(sym, freq, start_ns, end_ns)
        if not bars:
            log.warning("features.backfill.no_bars", symbol=sym, freq=freq)
            continue
        frames = [computer.compute(bar) for bar in bars]
        n = await offline.put_many(frames)
        written_total += n
        log.info(
            "features.backfill.symbol",
            symbol=sym,
            freq=freq,
            bars=len(bars),
            rows_written=n,
        )
    log.info("features.backfill.complete", symbols=len(sym_order), rows=written_total)
    return written_total
