"""
risk.snapshot - build a RiskContext from live state.

The risk gate runs hot on every OrderIntent.  This module collapses the
async I/O (Redis HGETALL across strategy hashes) into a single helper
the OMS calls right before :func:`risk.checks.check_intent`.

The price source is injected as a callable so the risk package stays
free of OMS-specific imports.  In production it's wired to
``LivePrices.get``; tests pass a ``dict.get`` or a stub.

Symbols with no reference price are dropped from the gross / per-symbol
notional totals (they're effectively unobservable).  This is a
conservative choice - the alternative would be to use stale or stub
prices, which could under-report exposure.  ``check_intent`` separately
rejects intents whose own price reference is unavailable, so dropping
unobservable holdings from the context only ever produces a softer
limit (smaller measured gross), never a tighter one.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

from portfolio.store import PositionStore

from risk.checks import RiskContext
from risk.state import KillSwitchState

PriceLookup = Callable[[str], Decimal | None]


async def build_context(
    *,
    store: PositionStore,
    get_price: PriceLookup,
    kill_switch: KillSwitchState,
    strategies: list[str] | None = None,
) -> RiskContext:
    """Read positions from ``store``, multiply by current prices, build context.

    If ``strategies`` is None, every known strategy in the index is
    included.  Pass an explicit list to scope the snapshot (e.g., a
    single strategy view for the future scoped-risk-gate variant).
    """
    if strategies is None:
        strategies = sorted(await store.known_strategies())

    notional_by_symbol: dict[str, Decimal] = {}
    gross = Decimal(0)

    for strategy_id in strategies:
        positions = await store.get_all(strategy_id)
        for symbol, position in positions.items():
            if position.quantity == 0:
                continue
            price = get_price(symbol)
            if price is None:
                # Unobservable; skip to avoid stale-price exposure inflation.
                continue
            notional = (Decimal(position.quantity) * price).copy_abs()
            notional_by_symbol[symbol] = (
                notional_by_symbol.get(symbol, Decimal(0)) + notional
            )
            gross += notional

    return RiskContext(
        notional_by_symbol=notional_by_symbol,
        gross_notional=gross,
        kill_switch_engaged=kill_switch.engaged,
    )
