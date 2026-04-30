"""
backtester.costs — transaction-cost simulator (v2).

Three components stack to produce ``(executed_price, fee_usd)``:

  1. **Spread**: half of the configured spread widens execution against
     the trader.  Buy pays half-spread up; sell receives half-spread
     down.  Per-symbol overrides; optionally widens further on
     high-range bars (proxy for current realised volatility).

  2. **Slippage / market impact**: two interchangeable parameterisations

     - **square-root** (preferred): ``impact_bps = impact_coef_sqrt *
       sqrt(participation_pct)`` where ``participation_pct = order_qty
       / bar_volume * 100``.  This is the canonical Almgren-style
       impact function used by execution shops because it captures the
       observed nonlinearity (1% of volume costs ~1 bp; 10% costs ~3
       bp; 100% costs ~10 bp).
     - **linear** (legacy): ``impact_bps = slippage_impact_coef *
       adv_pct * 100``.  Kept for back-compat with code that passes
       ``adv_pct`` only.  When ``bar_volume`` is supplied the
       square-root model wins.

  3. **Fees**: maker vs taker rate applied to executed notional.

Every parameter accepts a per-symbol override via ``per_symbol``: a
``SPY`` entry with ``spread_bps=Decimal("0.5")`` will override the
global default of 3 bp for that ticker only.  Fields left ``None`` on
the override fall back to the global value, so you can tighten just
spread for liquid names without touching impact or fees.

Holding costs:
  - **Borrow cost for shorts**: ``borrow_bps_annual`` (per-symbol or
    global default) charges any negative-quantity position a daily
    accrual prorated to the elapsed time between bars.  See
    :meth:`CostModel.accrue_borrow` for the formula.  Default is 0 so
    long-only backtests are unaffected; opt in by setting a non-zero
    rate either globally on ``CostModel`` or per-symbol on
    ``SymbolCosts``.

What's still out of scope (deferred):
  - Volume-tier rebates
  - Cross-side priority / queue-position effects
  - Rule-200 short-sale uptick filter
  - Hard-to-borrow penalty rate-bumps (would require a per-symbol
    "htb_premium_bps" overlay that activates intraday)
"""

from __future__ import annotations

import math
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from fincept_core.schemas import Side

# Calendar seconds per year used to prorate annual borrow bps to a
# per-elapsed-time charge.  365-day year matches how prime brokers quote
# annualised borrow even though the underlying market is closed for
# weekends / holidays — a borrow position accrues every calendar day.
SECONDS_PER_YEAR: Decimal = Decimal(365 * 24 * 60 * 60)


class SymbolCosts(BaseModel):
    """Per-symbol overrides for any subset of the global cost params.

    Fields left ``None`` inherit the global default.  Use this to model
    liquid names (tight spread, low impact) differently from illiquid
    ones in the same backtest.
    """

    model_config = ConfigDict(frozen=True)

    spread_bps: Decimal | None = None
    impact_coef_sqrt: Decimal | None = None
    slippage_impact_coef: Decimal | None = None
    maker_fee_bps: Decimal | None = None
    taker_fee_bps: Decimal | None = None
    # Annualized borrow rate in bps charged on negative-quantity
    # positions; ``None`` inherits :attr:`CostModel.default_borrow_bps_annual`.
    borrow_bps_annual: Decimal | None = None


class CostModel(BaseModel):
    """Parameters for realistic transaction-cost simulation."""

    model_config = ConfigDict(frozen=True)

    maker_fee_bps: Decimal = Decimal("1")
    taker_fee_bps: Decimal = Decimal("5")
    spread_bps_default: Decimal = Decimal("3")

    # Legacy linear impact: bps per 1% of ADV.  Used only when callers
    # pass ``adv_pct`` and no ``bar_volume``.
    slippage_impact_coef: Decimal = Decimal("0.1")

    # Square-root impact coefficient: bps per sqrt(participation_pct).
    # 10 bp at 1% of bar volume; 31.6 bp at 10%; 100 bp at 100%.
    impact_coef_sqrt: Decimal = Decimal("10")

    # If > 0 and bar_high/bar_low are supplied, the spread used for the
    # fill is ``max(default_spread, bar_range_bps * vol_spread_factor)``.
    # Default 0 disables the feature so existing tests keep their exact
    # round-trip arithmetic.
    vol_spread_factor: Decimal = Decimal("0")

    # Cap on participation_pct fed to the sqrt impact function — orders
    # that would exceed this fraction of the bar's volume get clamped
    # (partial fills aren't modelled in v1; clamping is the conservative
    # alternative).  Default 100% means no clamp.
    max_participation_pct: Decimal = Decimal("100")

    # Default annualized borrow rate in bps applied to negative-quantity
    # positions; per-symbol overrides on :class:`SymbolCosts` win.  Zero
    # by default so existing long-only backtests are bit-for-bit
    # identical to before this knob was introduced.
    default_borrow_bps_annual: Decimal = Decimal(0)

    per_symbol: dict[str, SymbolCosts] = Field(default_factory=dict)

    # ------------------------------------------------------------------ #
    # Per-symbol resolution                                              #
    # ------------------------------------------------------------------ #

    def _override(self, symbol: str | None, attr: str) -> Decimal | None:
        if not symbol:
            return None
        sym = self.per_symbol.get(symbol)
        if sym is None:
            return None
        value: Decimal | None = getattr(sym, attr, None)
        return value

    def spread_bps(self, symbol: str | None) -> Decimal:
        override = self._override(symbol, "spread_bps")
        return override if override is not None else self.spread_bps_default

    def impact_coef_sqrt_for(self, symbol: str | None) -> Decimal:
        override = self._override(symbol, "impact_coef_sqrt")
        return override if override is not None else self.impact_coef_sqrt

    def slippage_impact_coef_for(self, symbol: str | None) -> Decimal:
        override = self._override(symbol, "slippage_impact_coef")
        return override if override is not None else self.slippage_impact_coef

    def maker_fee_bps_for(self, symbol: str | None) -> Decimal:
        override = self._override(symbol, "maker_fee_bps")
        return override if override is not None else self.maker_fee_bps

    def taker_fee_bps_for(self, symbol: str | None) -> Decimal:
        override = self._override(symbol, "taker_fee_bps")
        return override if override is not None else self.taker_fee_bps

    def borrow_bps_annual_for(self, symbol: str | None) -> Decimal:
        override = self._override(symbol, "borrow_bps_annual")
        return override if override is not None else self.default_borrow_bps_annual

    # ------------------------------------------------------------------ #
    # Holding-cost simulation                                            #
    # ------------------------------------------------------------------ #

    def accrue_borrow(
        self,
        *,
        quantity: Decimal,
        mark_price: Decimal,
        elapsed_seconds: Decimal,
        symbol: str | None = None,
    ) -> Decimal:
        """USD borrow cost for ``quantity`` held over ``elapsed_seconds``.

        Returns ``Decimal(0)`` for any of:
          - long or flat positions (``quantity >= 0``)
          - non-positive elapsed time
          - non-positive mark price
          - effective borrow rate that resolves to <= 0

        Otherwise: ``|quantity| * mark_price * (bps_annual / 10_000) *
        (elapsed_seconds / SECONDS_PER_YEAR)``.

        The 365-day year matches prime-broker quoting convention; the
        engine accrues every bar, which is a continuous-time
        approximation of the conventional end-of-day charge.  Per-symbol
        overrides on :class:`SymbolCosts` win over the default.
        """
        if quantity >= 0 or elapsed_seconds <= 0 or mark_price <= 0:
            return Decimal(0)
        bps = self.borrow_bps_annual_for(symbol)
        if bps <= 0:
            return Decimal(0)
        notional = quantity.copy_abs() * mark_price
        return (
            notional * bps / Decimal(10000) * elapsed_seconds / SECONDS_PER_YEAR
        )

    # ------------------------------------------------------------------ #
    # Fill simulation                                                    #
    # ------------------------------------------------------------------ #

    def apply(
        self,
        *,
        side: Side,
        price: Decimal,
        quantity: Decimal,
        is_maker: bool,
        adv_pct: float = 0.0,
        symbol: str | None = None,
        bar_volume: Decimal | None = None,
        bar_high: Decimal | None = None,
        bar_low: Decimal | None = None,
    ) -> tuple[Decimal, Decimal]:
        """Return ``(executed_price, fee_usd)`` after spread + slippage + fee.

        Impact mode selection:
          - If ``bar_volume`` is supplied and > 0, the **square-root**
            model is used: ``participation_pct = qty/bar_volume * 100``
            and ``impact_bps = impact_coef_sqrt * sqrt(participation_pct)``.
          - Otherwise the **linear** legacy model is used:
            ``impact_bps = slippage_impact_coef * adv_pct * 100``.

        Volatility-scaled spread: if both ``bar_high`` and ``bar_low``
        are supplied AND ``vol_spread_factor > 0``, the effective spread
        is widened to ``max(default_spread, bar_range_bps *
        vol_spread_factor)``.  Otherwise the default spread is used.

        ``symbol``-keyed overrides apply to spread, impact coef, and
        fees — see :class:`SymbolCosts`.
        """
        # -- Spread -----------------------------------------------------
        spread_bps = self.spread_bps(symbol)
        if (
            self.vol_spread_factor > 0
            and bar_high is not None
            and bar_low is not None
            and price > 0
        ):
            bar_range_bps = (bar_high - bar_low) / price * Decimal(10000)
            vol_spread = bar_range_bps * self.vol_spread_factor
            if vol_spread > spread_bps:
                spread_bps = vol_spread
        # /10000 (bps -> fraction) then /2 (half-spread)
        half_spread = spread_bps / Decimal(20000) * price

        # -- Impact -----------------------------------------------------
        if bar_volume is not None and bar_volume > 0:
            participation_pct = quantity / bar_volume * Decimal(100)
            if participation_pct > self.max_participation_pct:
                participation_pct = self.max_participation_pct
            impact_bps = self.impact_coef_sqrt_for(symbol) * Decimal(
                str(math.sqrt(float(participation_pct)))
            )
        else:
            coef = self.slippage_impact_coef_for(symbol)
            impact_bps = coef * Decimal(str(adv_pct)) * Decimal(100)
        impact = impact_bps / Decimal(10000) * price

        # -- Direction --------------------------------------------------
        if side == Side.BUY:
            exec_price = price + half_spread + impact
        else:
            exec_price = price - half_spread - impact

        # -- Fees -------------------------------------------------------
        notional = exec_price * quantity
        fee_bps = (
            self.maker_fee_bps_for(symbol)
            if is_maker
            else self.taker_fee_bps_for(symbol)
        )
        fee = notional * fee_bps / Decimal(10000)
        return exec_price, fee
