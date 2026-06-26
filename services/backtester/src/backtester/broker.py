"""
backtester.broker — fill-against-bar simulator.

Order lifecycle:

  ``submit(intent)``  -> creates an Order in NEW state, stored open
  ``cancel(id)``      -> drops the order from the open book
  ``on_bar(bar)``     -> for each open order on this symbol, decide if
                         the bar's range triggers a fill, compute the
                         executed price + fee via the cost model, and
                         emit a Fill.  Fully-filled orders are removed;
                         partially-filled orders stay open with their
                         ``filled_qty`` / ``avg_fill_price`` / ``status``
                         updated, subject to the order's TimeInForce.

Fill rules:

  - **MARKET**: fills at the bar's ``open`` price.  Backtester
    convention: orders submitted on bar T-1 are visible at the open of
    bar T (no instant-fill cheating).  The engine enforces this by
    calling ``on_bar`` after ``strategy.on_bar``.
  - **LIMIT**: fills if and only if the bar's ``[low, high]`` range
    contains the limit price.  Buy fills at ``min(limit, open)``; sell
    fills at ``max(limit, open)``.  Treated as a maker fill.
  - **STOP**: triggers when the bar's range crosses through the stop
    (high >= stop for a BUY, low <= stop for a SELL).  Once triggered,
    behaves like a MARKET on a hypothetical sub-bar at the stop level
    — fill price is ``max(stop, open)`` (BUY) or ``min(stop, open)``
    (SELL) so gap-throughs fill at the worse open price.  Treated as a
    taker fill.
  - **STOP_LIMIT**: triggers like a STOP but additionally requires the
    bar's range to cross the limit price (both stop and limit must be
    touched in the same bar).  Fill price is the STOP-fill clamped to
    the limit ceiling/floor.  Stop-with-marketable-limit configurations
    that arithmetically yield a fill outside the bar's range emit no
    fill (matches venue rejection semantics).  Taker fill.

Partial fills:

  When ``cost_model.max_participation_pct < 100`` and the bar carries a
  positive ``volume``, no single order can consume more than
  ``bar.volume * max_participation_pct/100`` shares against that bar.
  Larger orders fill the capped portion at the cost-model's executed
  price (impact computed at the *actual* clamped participation, not the
  raw order participation), and the remainder stays open for the next
  bar.  The cost model's internal participation clamp still acts as a
  defensive ceiling on the impact calc, but the broker is the
  authoritative source for fill *quantity*.

  With the default ``max_participation_pct = 100`` (no participation
  cap configured) the broker preserves the original full-fill behavior
  even when the order would consume >100% of the bar's volume.  Opt in
  to realistic partial fills by setting a tighter cap (e.g.
  ``max_participation_pct=Decimal("10")`` for a conservative 10% cap).

  TimeInForce semantics on the leftover quantity:
    - **GTC** / **DAY** (default): partial-filled orders stay open
      across bars until fully filled or canceled.  Status flips to
      ``PARTIALLY_FILLED`` until the final fill flips it to ``FILLED``.
    - **IOC**: any leftover after the first fill attempt is canceled
      (the order is dropped from the open book regardless of how much
      filled).
    - **FOK**: if the participation cap would prevent a *full* fill in
      one bar, the order is canceled with NO fill emitted.  Otherwise
      it fills atomically like before.

  Strategy-side caveat: a strategy that treats every submit() as a
  one-shot "position is now flat" intent (e.g., the legacy GBM
  strategy) needs to be aware partial fills can leave residual
  positions — the strategy is responsible for re-submitting closes
  until ``ctx.positions[symbol].quantity == 0``.

Out of scope (deferred): queue-position effects, hidden liquidity,
trailing stops (would require per-bar stop-price recomputation rather
than a static trigger level).
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
    TimeInForce,
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
        """Try to fill every open order matching ``bar.symbol`` against this bar.

        Returns the list of new ``Fill`` events produced this bar.  May
        emit zero or one fill per order (no over-the-bar fragmentation).
        Orders that fully fill are removed from the open book; orders
        that partially fill remain open with their ``filled_qty`` /
        ``avg_fill_price`` / ``status`` updated, unless their
        ``TimeInForce`` says otherwise (IOC drops leftover, FOK cancels
        without filling when full fill isn't possible).
        """
        fills: list[Fill] = []
        # Collect ids first so we can safely pop while iterating.
        for order_id in list(self.open_orders):
            order = self.open_orders[order_id]
            if order.symbol != bar.symbol:
                continue
            trigger_price = self._trigger_price(order, bar)
            if trigger_price is None:
                continue

            remaining = order.quantity - order.filled_qty
            if remaining <= 0:
                # Defensive: shouldn't happen because fully-filled orders
                # are removed below, but a manual mutation could leave
                # one in the book.  Drop it cleanly.
                self.open_orders.pop(order_id)
                continue

            fillable_qty = self._fillable_qty(remaining=remaining, bar=bar)
            if fillable_qty <= 0:
                # Cap is configured but bar gives us no liquidity (e.g.,
                # bar.volume is 0 even though the cap is < 100%).  Hold
                # the order open and try again on the next bar.
                continue

            # FOK: must fully fill in one shot, else cancel without filling.
            if order.time_in_force == TimeInForce.FOK and fillable_qty < remaining:
                self._mark_canceled(order_id, order)
                continue

            is_maker = order.order_type == OrderType.LIMIT
            exec_price, fee = self._costs.apply(
                side=order.side,
                price=trigger_price,
                quantity=fillable_qty,
                is_maker=is_maker,
                adv_pct=self._adv_pct,
                symbol=order.symbol,
                bar_volume=bar.volume if bar.volume > 0 else None,
                bar_high=bar.high,
                bar_low=bar.low,
            )
            fills.append(
                Fill(
                    fill_id=new_id(),
                    order_id=order.order_id,
                    ts_event=bar.ts_event,
                    symbol=order.symbol,
                    side=order.side,
                    price=exec_price,
                    quantity=fillable_qty,
                    fee=fee,
                    is_maker=is_maker,
                )
            )

            new_filled = order.filled_qty + fillable_qty
            new_avg = self._weighted_avg_price(
                old_avg=order.avg_fill_price,
                old_qty=order.filled_qty,
                fill_price=exec_price,
                fill_qty=fillable_qty,
            )
            fully_filled = new_filled >= order.quantity

            if fully_filled:
                self.open_orders.pop(order_id)
            elif order.time_in_force == TimeInForce.IOC:
                # IOC cancels any remainder after the first fill window.
                # We DO emit the partial fill (above), then drop the rest.
                self.open_orders.pop(order_id)
            else:
                # GTC / DAY: leave remainder open with PARTIALLY_FILLED.
                self.open_orders[order_id] = order.model_copy(
                    update={
                        "filled_qty": new_filled,
                        "avg_fill_price": new_avg,
                        "status": OrderStatus.PARTIALLY_FILLED,
                        "updated_at": now_ns(),
                    }
                )
        return fills

    def _fillable_qty(self, *, remaining: Decimal, bar: BarEvent) -> Decimal:
        """Return how much of *remaining* can fill against *bar*.

        With the default ``max_participation_pct = 100`` we preserve the
        original behavior: fill the entire remaining quantity regardless
        of bar volume.  Tighter caps clamp to ``bar.volume * cap/100``.
        Bars with ``volume == 0`` are treated as "no participation
        signal" — we fall back to full fill so a strategy doesn't get
        starved on a halt-bar with non-zero OHLC but zero traded volume.
        """
        cap_pct = self._costs.max_participation_pct
        if cap_pct >= Decimal(100):
            return remaining
        if bar.volume <= 0:
            return remaining
        max_against_bar = bar.volume * cap_pct / Decimal(100)
        return min(remaining, max_against_bar)

    def _mark_canceled(self, order_id: str, order: Order) -> None:
        """Drop *order* from the open book (used for FOK rejects).

        We don't currently track canceled orders — the engine logs
        rejections at the risk-gate layer, and FOK cancels here are
        rare enough that emitting them would just add noise.  Consumers
        that need the count can subclass and override.
        """
        del order  # unused; kept for future hook
        self.open_orders.pop(order_id, None)

    @staticmethod
    def _weighted_avg_price(
        *,
        old_avg: Decimal | None,
        old_qty: Decimal,
        fill_price: Decimal,
        fill_qty: Decimal,
    ) -> Decimal:
        """Quantity-weighted average across the order's fill history.

        First fill: ``avg = fill_price``.  Subsequent fills: classic
        ``(old_qty*old_avg + fill_qty*fill_price) / (old_qty + fill_qty)``.
        """
        if old_avg is None or old_qty <= 0:
            return fill_price
        new_qty = old_qty + fill_qty
        return (old_qty * old_avg + fill_qty * fill_price) / new_qty

    @staticmethod
    def _trigger_price(order: Order, bar: BarEvent) -> Decimal | None:
        """Return the reference price at which *order* fills on *bar*, or None.

        Order-type semantics:

          * **MARKET** -- always fills at ``bar.open``.

          * **LIMIT BUY** -- fills if ``bar.low <= limit_price``.  Fill
            price is ``min(limit, bar.open)`` so a gap-down opens us
            inside the limit zone at the better price.

          * **LIMIT SELL** -- mirror image: fills if
            ``bar.high >= limit_price`` at ``max(limit, bar.open)``.

          * **STOP BUY** -- fires when ``bar.high >= stop_price`` (price
            broke up through our trigger).  Once triggered, behaves like
            a MARKET BUY on a hypothetical sub-bar at the stop level.
            Fill price is ``max(stop_price, bar.open)`` so a gap-up
            (open already past the stop) fills at ``bar.open`` rather
            than rewarding us with the better stop level we never saw.

          * **STOP SELL** -- mirror: fires when ``bar.low <= stop_price``;
            fill price is ``min(stop_price, bar.open)`` (gap-down fills
            at the worse open price).

          * **STOP_LIMIT BUY** -- fires when ``bar.high >= stop_price``
            *and* ``bar.low <= limit_price`` (price both hit the trigger
            and traded inside the limit zone during the bar).  Fill
            price is ``min(limit_price, max(stop_price, bar.open))`` --
            the STOP-fill price clamped down to the limit ceiling.

          * **STOP_LIMIT SELL** -- mirror: fires when
            ``bar.low <= stop_price`` *and* ``bar.high >= limit_price``;
            fill price is ``max(limit_price, min(stop_price, bar.open))``.

        Returns ``None`` when none of the trigger conditions were
        satisfied this bar (the order simply stays in the open book and
        is re-evaluated on the next bar -- same as any unfilled order).

        Caveats:
          * Intra-bar timing is not modelled.  We can't tell whether
            ``bar.low`` happened before or after ``bar.high`` from OHLC
            alone, so STOP_LIMIT triggering uses both extremes within
            the same bar.  This is the standard backtest approximation
            and tends to be slightly too generous on rare two-sided
            wicks; tighten the cap via ``CostModel.max_participation_pct``
            if you want partial-fill realism layered on top.
          * "Stop with marketable limit" cases (e.g.\\ STOP BUY whose
            limit is below the stop) are honoured arithmetically -- if
            the clamp yields a price < the limit, no fill (returned as
            ``None``).  This matches how real venues reject the order.
        """
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
        if order.order_type == OrderType.STOP and order.stop_price is not None:
            stop = order.stop_price
            if order.side == Side.BUY and bar.high >= stop:
                # Triggered.  Gap-ups (open already > stop) fill at
                # the worse open price; otherwise we assume the trigger
                # was hit on the way up and we fill at the stop level.
                return max(stop, bar.open)
            if order.side == Side.SELL and bar.low <= stop:
                return min(stop, bar.open)
        if (
            order.order_type == OrderType.STOP_LIMIT
            and order.stop_price is not None
            and order.limit_price is not None
        ):
            stop = order.stop_price
            limit = order.limit_price
            if order.side == Side.BUY and bar.high >= stop and bar.low <= limit:
                # Triggered and price entered the limit zone in the same
                # bar.  Fill at the STOP price (or open if a gap got us
                # past stop), clamped to the limit ceiling.
                fill = min(limit, max(stop, bar.open))
                # Defensive: with stop > limit (unusual config) the
                # arithmetic could fall below bar.low which would imply
                # we got a phantom fill below the bar's range.  Reject.
                if fill < bar.low:
                    return None
                return fill
            if order.side == Side.SELL and bar.low <= stop and bar.high >= limit:
                fill = max(limit, min(stop, bar.open))
                if fill > bar.high:
                    return None
                return fill
        return None
