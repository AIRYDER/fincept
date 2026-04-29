"""
fincept_core.portfolio — shared position math.

One function: ``apply_fill_to_position(prev, fill, *, strategy_id) -> Position``.

Both the backtester engine (``services/backtester/engine.py``) and the
live portfolio service (``services/portfolio/``) call this function so
position state evolves identically in offline simulation and live paper
trading.  The same problem TASK-017 solved for features with a shared
``FeatureComputer`` — keep one source of truth, kill drift risk.

The four cases are stated explicitly because cross-zero realized PnL
formulas are notoriously easy to get wrong (the original TASK-020 spec
snippet had a bug there).  Each case is pinned by tests in both
``services/backtester/tests/test_engine.py`` and
``services/portfolio/tests/test_positions.py``.
"""

from __future__ import annotations

from decimal import Decimal

from .clock import now_ns
from .schemas import Fill, Position, Side


def apply_fill_to_position(
    prev: Position | None,
    fill: Fill,
    *,
    strategy_id: str,
) -> Position:
    """Apply *fill* to *prev* and return the resulting Position.

    *prev* is None on the first fill for a (strategy_id, symbol) pair.
    *strategy_id* is taken from the calling service rather than the fill
    because Fill doesn't carry it (Fills can be aggregated across
    strategies in the OMS audit log).
    """
    signed_qty = fill.quantity if fill.side == Side.BUY else -fill.quantity

    # Case 1: open a fresh position.
    if prev is None or prev.quantity == 0:
        return Position(
            strategy_id=strategy_id,
            symbol=fill.symbol,
            quantity=signed_qty,
            avg_cost=fill.price,
            realized_pnl=prev.realized_pnl if prev is not None else Decimal(0),
            unrealized_pnl=Decimal(0),
            updated_at=fill.ts_event,
        )

    new_qty = prev.quantity + signed_qty
    same_direction = (prev.quantity > 0) == (signed_qty > 0)

    # Case 2: open more in the same direction -> weighted average cost.
    if same_direction:
        total_cost = prev.avg_cost * abs(prev.quantity) + fill.price * abs(signed_qty)
        new_cost = total_cost / abs(new_qty)
        return prev.model_copy(
            update={
                "quantity": new_qty,
                "avg_cost": new_cost,
                "updated_at": fill.ts_event,
            }
        )

    # Cases 3 & 4: opposite direction.  Realize PnL on the closed portion.
    closed_qty = min(abs(prev.quantity), abs(signed_qty))
    direction = Decimal(1) if prev.quantity > 0 else Decimal(-1)
    realized = (fill.price - prev.avg_cost) * closed_qty * direction

    if new_qty == 0:
        # Case 3: exact close - flat position, retain realized PnL accumulator.
        return prev.model_copy(
            update={
                "quantity": Decimal(0),
                "realized_pnl": prev.realized_pnl + realized,
                "updated_at": fill.ts_event,
            }
        )

    # Case 4: cross-flip - close prior side, open opposite at fill price.
    return prev.model_copy(
        update={
            "quantity": new_qty,
            "avg_cost": fill.price,
            "realized_pnl": prev.realized_pnl + realized,
            "updated_at": fill.ts_event,
        }
    )


def empty_position(*, strategy_id: str, symbol: str) -> Position:
    """Construct a zeroed Position - useful for fresh-start initialisation."""
    return Position(
        strategy_id=strategy_id,
        symbol=symbol,
        quantity=Decimal(0),
        avg_cost=Decimal(0),
        realized_pnl=Decimal(0),
        unrealized_pnl=Decimal(0),
        updated_at=now_ns(),
    )
