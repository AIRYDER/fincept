"""
backtester.report - performance metrics from a Blotter.

Compute the standard report a quant uses to triage a strategy: total
return, sharpe, max drawdown, win rate, fills count, fees paid.  All
inputs come from a :class:`backtester.blotter.Blotter`; the function is
pure and synchronous so it can be called from the CLI, the API, and
tests with no setup.

Equity-curve metrics use bar-level returns (close-to-close on the
equity curve, not per-trade) because the blotter samples equity once
per bar.  Sharpe is annualized assuming ``bar_seconds=60`` (1m bars,
525,600 bars/year) by default; override ``bars_per_year`` for other
frequencies.

The ``BacktestReport`` model is the contract that the API and dashboard
consume - keep it stable, only add new optional fields.
"""

from __future__ import annotations

import math
from decimal import Decimal
from itertools import pairwise
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from backtester.blotter import Blotter

# Default annualization assumes 1-minute bars (60s).  Override per run.
DEFAULT_BARS_PER_YEAR = 365 * 24 * 60  # 525,600 minutes


class TradeRow(BaseModel):
    """One fill, in JSON-friendly form for the API + dashboard."""

    model_config = ConfigDict(frozen=True)

    fill_id: str
    order_id: str
    ts_event: int
    symbol: str
    side: str
    price: float
    quantity: float
    fee: float
    is_maker: bool | None = None


class EquityPoint(BaseModel):
    """One row of the equity curve."""

    model_config = ConfigDict(frozen=True)

    ts_event: int
    equity_usd: float


class PerSymbolStats(BaseModel):
    """Aggregate per-symbol activity + cost summary."""

    model_config = ConfigDict(frozen=True)

    symbol: str
    fills: int
    bought_qty: float
    sold_qty: float
    notional_traded: float
    fees_paid: float


class BacktestReport(BaseModel):
    """Top-level report payload returned by :func:`compute_metrics`."""

    model_config = ConfigDict(frozen=True)

    starting_cash: float
    final_equity: float
    total_return_pct: float
    n_bars: int = Field(default=0)
    n_fills: int = Field(default=0)
    fees_paid_total: float = Field(default=0.0)
    # USD borrow charges accrued on short positions over the run.
    # Always 0 for long-only or default-borrow=0 configurations.
    borrow_paid_total: float = Field(default=0.0)
    sharpe: float | None = None
    max_drawdown_pct: float | None = None
    longest_drawdown_bars: int | None = None
    bars_per_year: int = DEFAULT_BARS_PER_YEAR
    n_rejections: int = Field(default=0)
    rejection_reasons: dict[str, int] = Field(default_factory=dict)
    per_symbol: list[PerSymbolStats] = Field(default_factory=list)
    equity_curve: list[EquityPoint] = Field(default_factory=list)
    trades: list[TradeRow] = Field(default_factory=list)


def _equity_floats(blotter: Blotter) -> list[tuple[int, float]]:
    return [(ts, float(eq)) for ts, eq in blotter.equity_curve]


def _bar_returns(equity: list[tuple[int, float]]) -> list[float]:
    """Bar-to-bar equity returns; returns [] if fewer than 2 points."""
    rets: list[float] = []
    for prev, cur in pairwise(equity):
        prev_eq = prev[1]
        cur_eq = cur[1]
        if prev_eq <= 0:
            rets.append(0.0)
            continue
        rets.append((cur_eq - prev_eq) / prev_eq)
    return rets


def _sharpe(returns: list[float], *, bars_per_year: int) -> float | None:
    """Annualized Sharpe from per-bar returns; ``None`` if too few or zero-vol."""
    if len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    std = math.sqrt(var)
    if std == 0:
        return None
    annualized_mean = mean * bars_per_year
    annualized_std = std * math.sqrt(bars_per_year)
    return annualized_mean / annualized_std


def _max_drawdown(equity: list[tuple[int, float]]) -> tuple[float, int]:
    """Return ``(max_dd_pct, longest_dd_bars)``.

    ``max_dd_pct`` is the deepest peak-to-trough decline as a positive
    number (0.20 = 20% drawdown).  ``longest_dd_bars`` is the longest
    consecutive run of bars where the curve is below its running peak.
    """
    if not equity:
        return 0.0, 0
    peak = equity[0][1]
    max_dd = 0.0
    cur_run = 0
    longest = 0
    for _, eq in equity:
        if eq > peak:
            peak = eq
            cur_run = 0
            continue
        cur_run += 1
        longest = max(longest, cur_run)
        if peak <= 0:
            continue
        dd = (peak - eq) / peak
        if dd > max_dd:
            max_dd = dd
    return max_dd, longest


def _per_symbol_stats(blotter: Blotter) -> list[PerSymbolStats]:
    by_sym: dict[str, dict[str, Decimal | int]] = {}
    for fill in blotter.fills:
        slot = by_sym.setdefault(
            fill.symbol,
            {
                "fills": 0,
                "bought_qty": Decimal(0),
                "sold_qty": Decimal(0),
                "notional_traded": Decimal(0),
                "fees_paid": Decimal(0),
            },
        )
        slot["fills"] = int(slot["fills"]) + 1
        if str(fill.side).lower() == "buy":
            slot["bought_qty"] = Decimal(slot["bought_qty"]) + fill.quantity
        else:
            slot["sold_qty"] = Decimal(slot["sold_qty"]) + fill.quantity
        slot["notional_traded"] = (
            Decimal(slot["notional_traded"]) + fill.price * fill.quantity
        )
        slot["fees_paid"] = Decimal(slot["fees_paid"]) + fill.fee
    rows: list[PerSymbolStats] = []
    for sym, slot in sorted(by_sym.items()):
        rows.append(
            PerSymbolStats(
                symbol=sym,
                fills=int(slot["fills"]),
                bought_qty=float(Decimal(slot["bought_qty"])),
                sold_qty=float(Decimal(slot["sold_qty"])),
                notional_traded=float(Decimal(slot["notional_traded"])),
                fees_paid=float(Decimal(slot["fees_paid"])),
            )
        )
    return rows


def _trade_rows(blotter: Blotter) -> list[TradeRow]:
    return [
        TradeRow(
            fill_id=f.fill_id,
            order_id=f.order_id,
            ts_event=f.ts_event,
            symbol=f.symbol,
            side=str(f.side).lower(),
            price=float(f.price),
            quantity=float(f.quantity),
            fee=float(f.fee),
            is_maker=f.is_maker,
        )
        for f in blotter.fills
    ]


def compute_metrics(
    blotter: Blotter,
    *,
    bars_per_year: int = DEFAULT_BARS_PER_YEAR,
    include_equity_curve: bool = True,
    include_trades: bool = True,
) -> BacktestReport:
    """Pure function: blotter -> typed performance report.

    ``include_equity_curve`` and ``include_trades`` toggle whether the
    detailed series are embedded in the report.  The API includes both
    by default for the dashboard chart; tooling that just wants headline
    numbers can set them to False to keep payloads small.
    """
    equity = _equity_floats(blotter)
    starting_cash = float(blotter.starting_cash)
    final_equity = (
        equity[-1][1] if equity else float(blotter.starting_cash)
    )
    total_return = (
        (final_equity - starting_cash) / starting_cash * 100.0
        if starting_cash > 0
        else 0.0
    )
    returns = _bar_returns(equity)
    sharpe = _sharpe(returns, bars_per_year=bars_per_year)
    max_dd, longest_dd = _max_drawdown(equity)
    fees_total = float(sum(f.fee for f in blotter.fills))
    borrow_total = float(blotter.borrow_paid)

    return BacktestReport(
        starting_cash=starting_cash,
        final_equity=float(final_equity),
        total_return_pct=total_return,
        n_bars=len(equity),
        n_fills=len(blotter.fills),
        fees_paid_total=fees_total,
        borrow_paid_total=borrow_total,
        sharpe=sharpe,
        max_drawdown_pct=max_dd * 100.0 if max_dd > 0 else 0.0,
        longest_drawdown_bars=longest_dd,
        bars_per_year=bars_per_year,
        n_rejections=len(blotter.rejections),
        rejection_reasons=_rejection_breakdown(blotter),
        per_symbol=_per_symbol_stats(blotter),
        equity_curve=(
            [EquityPoint(ts_event=ts, equity_usd=eq) for ts, eq in equity]
            if include_equity_curve
            else []
        ),
        trades=_trade_rows(blotter) if include_trades else [],
    )


def _rejection_breakdown(blotter: Blotter) -> dict[str, int]:
    """Count rejections by their reason *prefix* (the part before the
    first ``:``).  ``"per_symbol_notional_breach:BTC:9000>5000"`` and
    ``"per_symbol_notional_breach:ETH:7000>5000"`` both increment
    ``"per_symbol_notional_breach"`` so the breakdown is a useful
    dashboard summary instead of a per-message-id histogram.
    """
    counts: dict[str, int] = {}
    for r in blotter.rejections:
        for reason in r.reasons:
            prefix = reason.split(":", 1)[0]
            counts[prefix] = counts.get(prefix, 0) + 1
    return counts


def report_to_dict(report: BacktestReport) -> dict[str, Any]:
    """Serialize a report to a JSON-safe dict (float-only, no Decimals)."""
    return report.model_dump(mode="json")
