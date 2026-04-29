"""
risk.checks - pure-logic pre-trade limit checks.

The single public entry point is :func:`check_intent`, which returns a
canonical :class:`fincept_core.schemas.RiskCheckResult`.  The function is
pure given a :class:`RiskContext` snapshot; the ``snapshot`` module
knows how to build one from live state.

v1 covers three checks; the daily-loss check is intentionally deferred
(needs a realized-P&L tracker that doesn't yet exist):

  1. Kill switch
        If ``ctx.kill_switch_engaged``, every intent is rejected with
        reason ``"kill_switch_engaged"``.  Short-circuits before any
        other check runs - the kill switch is a global override.

  2. Per-symbol notional cap (``MAX_NOTIONAL_USD_PER_SYMBOL``)
        Sum of |existing notional in this symbol| + |intent notional|
        must not exceed the cap.  Uses limit_price if available, else
        the last_price callback.  Intents without a reference price
        are rejected (``"no_reference_price"``) - allowing them would
        defeat the entire risk gate.

  3. Gross notional cap (``MAX_GROSS_NOTIONAL_USD``)
        Sum of |existing total notional| + |intent notional| must not
        exceed the cap.

Reduce-and-allow (``reduced_notional_usd`` field on RiskCheckResult) is
NOT implemented in v1.  When a strategy wants partial fills it should
shrink the intent itself; the gate is a binary approve / reject for
simplicity.  Phase H may add reduce-mode for risk-aware OMS rebalancing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from fincept_core.clock import now_ns
from fincept_core.config import Settings
from fincept_core.schemas import OrderIntent, RiskCheckResult


@dataclass(frozen=True)
class RiskContext:
    """Snapshot of state used by :func:`check_intent`.

    Built by :func:`risk.snapshot.build_context` before each intent.
    Notionals are unsigned (absolute values) - net position direction
    doesn't matter for cap checks; only total dollars at risk does.
    """

    notional_by_symbol: dict[str, Decimal] = field(default_factory=dict)
    gross_notional: Decimal = Decimal(0)
    kill_switch_engaged: bool = False


def check_intent(
    intent: OrderIntent,
    *,
    ctx: RiskContext,
    settings: Settings,
    last_price: Decimal | None,
) -> RiskCheckResult:
    """Approve or reject ``intent`` against the limits in ``settings``.

    ``last_price`` is the most recent observed market price for the
    symbol - typically from an in-process price cache (LivePrices).
    May be None if the price feed hasn't yet seen a trade for this
    symbol; in that case, the intent must carry a ``limit_price`` or
    it will be rejected.
    """
    # 1. Kill switch overrides everything.
    if ctx.kill_switch_engaged:
        return RiskCheckResult(
            approved=False,
            reasons=["kill_switch_engaged"],
            checked_at=now_ns(),
        )

    # 2. Determine the reference price for notional math.
    intent_price = intent.limit_price if intent.limit_price is not None else last_price
    if intent_price is None:
        return RiskCheckResult(
            approved=False,
            reasons=[f"no_reference_price:{intent.symbol}"],
            checked_at=now_ns(),
        )
    if intent_price <= 0:
        return RiskCheckResult(
            approved=False,
            reasons=[f"non_positive_reference_price:{intent_price}"],
            checked_at=now_ns(),
        )

    intent_notional = (intent_price * intent.quantity).copy_abs()
    reasons: list[str] = []

    # 3. Per-symbol notional cap.
    cap_per_symbol = Decimal(settings.MAX_NOTIONAL_USD_PER_SYMBOL)
    existing_symbol = ctx.notional_by_symbol.get(intent.symbol, Decimal(0))
    new_symbol_notional = existing_symbol + intent_notional
    if new_symbol_notional > cap_per_symbol:
        reasons.append(
            f"per_symbol_notional_breach:{intent.symbol}:"
            f"{new_symbol_notional} > {cap_per_symbol}"
        )

    # 4. Gross notional cap.
    cap_gross = Decimal(settings.MAX_GROSS_NOTIONAL_USD)
    new_gross = ctx.gross_notional + intent_notional
    if new_gross > cap_gross:
        reasons.append(f"gross_notional_breach:{new_gross} > {cap_gross}")

    return RiskCheckResult(
        approved=not reasons,
        reasons=reasons,
        checked_at=now_ns(),
    )
