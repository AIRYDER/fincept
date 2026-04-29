"""
orchestrator.decisions - target rebalance -> (Decision, OrderIntent).

Two pieces:

  ``TargetState``               In-memory dict of last-emitted signed
                                target notional per symbol.  The
                                router asks for ``delta(symbol, new)``
                                to get the rebalance amount; if it
                                exceeds the deadband, the router emits
                                an order and calls ``update`` to record
                                the new high-water mark.

  ``build_decision_and_intent`` Pure function: given a delta in USD
                                plus a reference price, builds the
                                canonical Decision (audit) + OrderIntent
                                (routable) pair.  Both share the same
                                ``decision_id`` so audit / blotter /
                                attribution can join across streams.

Decimal precision: quantity is computed as ``|delta| / price`` and
quantized to 8 decimals - finer than any spot/perp tick we care
about, coarser than Decimal's full precision (which would make Redis
payloads huge).  Real per-symbol tick sizes are a Phase H concern
(TASK-074 venue catalog).

Side determination: ``side = BUY`` when delta > 0 (we want to ADD
long exposure or REDUCE short), ``SELL`` when delta < 0.  Combined
with the OMS net-position math (TASK-044), this naturally handles
position flips: a target of -5000 from a current of +3000 yields a
SELL of 8000-worth, taking us from +3000 to -5000.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal

from fincept_core.ids import new_id
from fincept_core.schemas import (
    Decision,
    OrderIntent,
    OrderType,
    Side,
    TimeInForce,
    Venue,
)

DEFAULT_QUANTITY_QUANTUM = Decimal("0.00000001")  # 1 satoshi-equivalent


@dataclass
class TargetState:
    """In-memory last-emitted target notional per symbol."""

    targets: dict[str, Decimal] = field(default_factory=dict)

    def delta(self, symbol: str, new_target: Decimal) -> Decimal:
        return new_target - self.targets.get(symbol, Decimal(0))

    def update(self, symbol: str, new_target: Decimal) -> None:
        self.targets[symbol] = new_target

    def clear(self, symbol: str) -> None:
        self.targets.pop(symbol, None)

    def known_symbols(self) -> set[str]:
        return set(self.targets)


def build_decision_and_intent(
    *,
    symbol: str,
    delta_notional: Decimal,
    last_price: Decimal,
    strategy_id: str,
    ts_event: int,
    rationale: str,
    source_signals: Iterable[str],
    venue: Venue = Venue.ALPACA,
    urgency: float = 0.5,
    quantity_quantum: Decimal = DEFAULT_QUANTITY_QUANTUM,
) -> tuple[Decision, OrderIntent]:
    """Build (Decision, OrderIntent) pair sharing a fresh decision_id.

    ``delta_notional`` is signed: positive for net BUY, negative for
    net SELL.  Returns a market-order intent with GTC TIF; refining
    to limit + smart routing is the job of a future execution agent.
    """
    if last_price <= 0:
        raise ValueError(f"last_price must be positive; got {last_price}")
    if delta_notional == 0:
        raise ValueError("delta_notional must be non-zero")

    side = Side.BUY if delta_notional > 0 else Side.SELL
    quantity_raw = delta_notional.copy_abs() / last_price
    quantity = quantity_raw.quantize(quantity_quantum)
    if quantity <= 0:
        raise ValueError(
            f"computed quantity {quantity} <= 0 for delta={delta_notional} @ {last_price}"
        )

    decision_id = new_id()
    order_id = new_id()
    sources = list(source_signals)
    decision = Decision(
        decision_id=decision_id,
        ts_event=ts_event,
        strategy_id=strategy_id,
        symbol=symbol,
        side=side,
        target_notional_usd=delta_notional.copy_abs(),
        urgency=urgency,
        rationale=rationale,
        source_signals=sources,
    )
    intent = OrderIntent(
        order_id=order_id,
        decision_id=decision_id,
        ts_event=ts_event,
        strategy_id=strategy_id,
        symbol=symbol,
        venue=venue,
        side=side,
        order_type=OrderType.MARKET,
        quantity=quantity,
        time_in_force=TimeInForce.GTC,
        tags={"orchestrator": strategy_id},
    )
    return decision, intent
