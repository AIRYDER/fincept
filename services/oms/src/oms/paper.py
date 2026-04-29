"""
oms.paper — paper-trading fill simulator.

Generates a ``Fill`` from an ``Order`` + a current mid price:

  - **MARKET**   pays half-spread above mid (BUY) or receives half-spread
                 below mid (SELL).  Treated as taker -> 5 bps fee by default.
  - **LIMIT**    fills at the limit price.  Treated as maker (rebated rate
                 in production; we use 1 bps default).  No range check vs
                 the bar: live OMS doesn't see bars, just the current mid.
                 If you want stricter limit semantics, gate at the
                 strategy level before submitting.

Latency is added via Gaussian noise on top of the OMS's wall-clock
``now_ns()`` so the resulting Fill ``ts_event`` reflects realistic round-
trip delay.  This matters for backtests-that-replay-paper: the latency
gives a more honest view of slippage than instant fills.

Determinism:  the random number source is **injectable** so tests can
seed it.  Default is ``random.gauss`` from the global ``random`` module;
a test passes ``rng=lambda mu, sigma: 0.0`` for zero-jitter fills.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from decimal import Decimal

from fincept_core.clock import now_ns
from fincept_core.ids import new_id
from fincept_core.schemas import Fill, Order, OrderType, Side

GaussFn = Callable[[float, float], float]
NowFn = Callable[[], int]


class PaperFiller:
    """Simulate a venue fill against a live mid price."""

    def __init__(
        self,
        *,
        mean_latency_ms: float = 50.0,
        std_latency_ms: float = 15.0,
        spread_bps: Decimal = Decimal("3"),
        maker_fee_bps: Decimal = Decimal("1"),
        taker_fee_bps: Decimal = Decimal("5"),
        rng: GaussFn = random.gauss,
        clock: NowFn = now_ns,
    ) -> None:
        self._mean_lat_ms = mean_latency_ms
        self._std_lat_ms = std_latency_ms
        self._spread_bps = spread_bps
        self._maker_fee_bps = maker_fee_bps
        self._taker_fee_bps = taker_fee_bps
        self._rng = rng
        self._clock = clock

    def latency_ns(self) -> int:
        """Sample a non-negative latency in nanoseconds."""
        ms = max(0.0, self._rng(self._mean_lat_ms, self._std_lat_ms))
        return int(ms * 1_000_000)

    def fill(self, order: Order, mid: Decimal) -> Fill:
        """Produce a Fill for ``order`` at the current ``mid`` price."""
        is_maker = order.order_type == OrderType.LIMIT
        if order.order_type == OrderType.MARKET:
            half_spread = mid * self._spread_bps / Decimal(10000) / Decimal(2)
            exec_price = mid + half_spread if order.side == Side.BUY else mid - half_spread
        else:
            # LIMIT (and STOP_LIMIT — STOP not yet supported); fill at the
            # limit price.  Caller guarantees limit_price is set; raise
            # rather than silently producing a wrong fill.
            if order.limit_price is None:
                raise ValueError(f"order {order.order_id}: {order.order_type} requires limit_price")
            exec_price = order.limit_price

        fee_bps = self._maker_fee_bps if is_maker else self._taker_fee_bps
        fee = exec_price * order.quantity * fee_bps / Decimal(10000)

        return Fill(
            fill_id=new_id(),
            order_id=order.order_id,
            ts_event=self._clock() + self.latency_ns(),
            symbol=order.symbol,
            side=order.side,
            price=exec_price,
            quantity=order.quantity,
            fee=fee,
            is_maker=is_maker,
        )
