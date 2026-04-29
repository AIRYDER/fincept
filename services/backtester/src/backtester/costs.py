"""
backtester.costs — transaction-cost simulator.

Three components:

  1. **Spread**: half the configured ``spread_bps_default`` widens the
     execution price against the trader.  Buy pays half-spread up;
     sell receives half-spread down.
  2. **Slippage**: linear in size as a fraction of average daily volume
     (``adv_pct``).  ``slippage_impact_coef`` is bps per 1% ADV.
  3. **Fees**: maker vs taker rate applied to notional.

This is a deliberately simple v1 model.  Real markets have:

  - Volume-tier rebates (first 100M/day, etc.)
  - Cross-side priority and queue position effects
  - Borrow costs for shorts
  - Rule-200 short-sale uptick filters

These belong in TASK-021's "cost model refinement" follow-up; for now
the heuristic is good enough to detect strategies that ignore costs
entirely (a common backtest fairy-tale failure).
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from fincept_core.schemas import Side


class CostModel(BaseModel):
    """Parameters for realistic transaction-cost simulation."""

    model_config = ConfigDict(frozen=True)

    maker_fee_bps: Decimal = Decimal("1")  # 1 bp on rebated/passive fills
    taker_fee_bps: Decimal = Decimal("5")  # 5 bps on aggressive crosses
    spread_bps_default: Decimal = Decimal("3")  # used when no live spread is available
    slippage_impact_coef: Decimal = Decimal("0.1")  # bps per 1% ADV

    def apply(
        self,
        *,
        side: Side,
        price: Decimal,
        quantity: Decimal,
        is_maker: bool,
        adv_pct: float,
    ) -> tuple[Decimal, Decimal]:
        """Return ``(executed_price, fee_usd)`` after spread + slippage + fee.

        ``adv_pct`` is the order's size as a fraction of the symbol's
        average daily volume — e.g., ``0.005`` = 0.5% of ADV.  In v1 the
        engine passes a fixed value; a real ADV service is a follow-up.
        """
        # Half-spread always works against the taker direction.
        spread = self.spread_bps_default / Decimal(10000) * price
        half_spread = spread / Decimal(2)

        # Linear impact: more size -> more drift against you.
        impact_bps = self.slippage_impact_coef * Decimal(str(adv_pct)) * Decimal(100)
        impact = impact_bps / Decimal(10000) * price

        if side == Side.BUY:
            exec_price = price + half_spread + impact
        else:
            exec_price = price - half_spread - impact

        notional = exec_price * quantity
        fee_bps = self.maker_fee_bps if is_maker else self.taker_fee_bps
        fee = notional * fee_bps / Decimal(10000)
        return exec_price, fee
