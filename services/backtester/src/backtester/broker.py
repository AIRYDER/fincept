"""
backtester.broker — fill-against-bar simulator.

Order lifecycle:

  ``submit(intent)``  -> creates an Order in NEW state, stored open
  ``cancel(id)``      -> drops the order from the open book
  ``on_bar(bar)``     -> for each open order on this symbol, decide if
                         the bar's range triggers a fill, compute the
                         executed price + fee via the cost model, and
                         emit a Fill.  Fully-filled orders are removed.

Fill rules (v1):

  - **MARKET**: fills at the bar's ``open`` price.  Backtester
    convention: orders submitted on bar T-1 are visible at the open of
    bar T (no instant-fill cheating).  The engine enforces this by
    calling ``on_bar`` after ``strategy.on_bar``.
  - **LIMIT**: fills if and only if the bar's ``[low, high]`` range
    contains the limit price.  Buy fills at ``min(limit, open)``; sell
    fills at ``max(limit, open)``.  Treated as a maker fill.

Out of scope (deferred): partial fills, stop / stop-limit, IOC/FOK
expiry, queue-position effects, hidden liquidity.  These are
TASK-022-refinement items.
"""

from __future__ import annotations

from decimal import Decimal

from backtester.costs import CostModel
from fincept_core.clock import now_ns
from fincept_core.ids import new_id
from fincept_core.schemas import (
    BarEvent,
    Fill,
    Order,
    OrderIntent,
    OrderStatus,
    OrderType,
    Side,
)

# Default ADV ratio used when the engine has no real volume service.
# 0.5% of average daily volume is small enough to keep slippage bounded
# but non-zero, which is what we want for a "costs are real" baseline.
DEFAULT_ADV_PCT = 0.005


class SimBroker:
    """Stateful in-memory broker that fills orders against the bar stream."""

    def __init__(
        self,
        cost_model: CostModel | None = None,
        *,
        adv_pct: float = DEFAULT_ADV_PCT,
    ) -> None:
        self._costs = cost_model if cost_model is not None else CostModel()
        self._adv_pct = adv_pct
        self.open_orders: dict[str, Order] = {}

    @property
    def cost_model(self) -> CostModel:
        return self._costs

    def submit(self, intent: OrderIntent) -> Order:
        """Move an OrderIntent into the open book as a NEW Order."""
        ts = now_ns()
        order = Order(
            **intent.model_dump(),
            status=OrderStatus.NEW,
            created_at=ts,
            updated_at=ts,
        )
        self.open_orders[order.order_id] = order
        return order

    def cancel(self, order_id: str) -> bool:
        """Drop an open order; returns True if found, False if already gone."""
        return self.open_orders.pop(order_id, None) is not None

    def on_bar(self, bar: BarEvent) -> list[Fill]:
        """Try to fill every open order matching ``bar.symbol`` against this bar."""
        fills: list[Fill] = []
        # Collect ids first so we can safely pop while iterating.
        for order_id in list(self.open_orders):
            order = self.open_orders[order_id]
            if order.symbol != bar.symbol:
                continue
            trigger_price = self._trigger_price(order, bar)
            if trigger_price is None:
                continue
            is_maker = order.order_type == OrderType.LIMIT
            exec_price, fee = self._costs.apply(
                side=order.side,
                price=trigger_price,
                quantity=order.quantity,
                is_maker=is_maker,
                adv_pct=self._adv_pct,
            )
            fills.append(
                Fill(
                    fill_id=new_id(),
                    order_id=order.order_id,
                    ts_event=bar.ts_event,
                    symbol=order.symbol,
                    side=order.side,
                    price=exec_price,
                    quantity=order.quantity,
                    fee=fee,
                    is_maker=is_maker,
                )
            )
            self.open_orders.pop(order_id)
        return fills

    @staticmethod
    def _trigger_price(order: Order, bar: BarEvent) -> Decimal | None:
        """Return the reference price at which *order* fills on *bar*, or None."""
        if order.order_type == OrderType.MARKET:
            return bar.open
        if order.order_type == OrderType.LIMIT and order.limit_price is not None:
            limit = order.limit_price
            if order.side == Side.BUY and bar.low <= limit:
                # Best price for a buyer is min(limit, open) — if the bar
                # opened below the limit, we got the better open price.
                return min(limit, bar.open)
            if order.side == Side.SELL and bar.high >= limit:
                return max(limit, bar.open)
        # STOP / STOP_LIMIT not implemented in v1; emit no fill.
        return None
