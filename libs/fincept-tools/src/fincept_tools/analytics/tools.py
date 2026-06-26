"""
fincept_tools.analytics.tools — pure-compute analytics tool implementations.

Each tool subclasses ``BaseTool`` and overrides ``_run``; ``BaseTool.__call__``
provides OTel tracing + typed-error handling.

All tools are PIT-safe: they accept an ``end_ns`` timestamp and never read
data beyond that point, making them safe for use in backtests.

Tools in this module:
  - analytics.compute_returns     — log-return series from OHLCV bars
  - analytics.compute_vol         — realised annualised volatility
  - analytics.compute_correlation — Pearson correlation of two return series
  - analytics.compute_vwap        — VWAP from bars
  - analytics.compute_sharpe      — annualised Sharpe ratio over a lookback
  - analytics.compute_drawdown    — max drawdown over a lookback (peak-to-trough)
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
from pydantic import Field

from fincept_db.bars import read_bars
from fincept_tools.errors import ToolBackendError
from fincept_tools.protocol import BaseTool, ToolInput, ToolOutput
from fincept_tools.registry import register

# Annualisation factors by frequency — delegates to the shared
# fincept_core.clock.bars_per_year_for_freq so all services use the
# same mapping (supports 5m, 15m, and arbitrary N+unit frequencies).
def _bars_per_year(freq: str) -> float:
    from fincept_core.clock import bars_per_year_for_freq

    return float(bars_per_year_for_freq(freq))


async def _safe_read_bars(
    symbol: str,
    freq: str,
    start_ns: int,
    end_ns: int,
    venue: str | None = None,
) -> list[Any]:
    """Wrap fincept_db.bars.read_bars and raise ToolBackendError on failure."""
    try:
        bars: list[Any] = list(await read_bars(symbol, freq, start_ns, end_ns, venue=venue))
    except Exception as exc:
        raise ToolBackendError(f"read_bars failed for {symbol!r}: {exc}") from exc
    return bars


# ---------------------------------------------------------------------------
# analytics.compute_returns
# ---------------------------------------------------------------------------


class ComputeReturnsInput(ToolInput):
    """Input for analytics.compute_returns."""

    symbol: str = Field(description="Canonical symbol, e.g. BTC-USD.")
    freq: str = Field(
        default="1d",
        pattern=r"^(1m|1h|1d)$",
        description="Bar frequency: '1m', '1h', or '1d'.",
    )
    lookback_bars: int = Field(
        ge=2,
        le=10_000,
        description="Number of bars to fetch ending at end_ns.",
    )
    end_ns: int = Field(description="PIT cutoff — no data beyond this timestamp is read.")
    venue: str | None = Field(default=None, description="Optional venue filter.")


class ComputeReturnsOutput(ToolOutput):
    """Output for analytics.compute_returns."""

    returns: list[float] = Field(
        default_factory=list,
        description="Log-return series, length = min(n_bars, lookback_bars+1) - 1.",
    )
    n_bars: int = Field(default=0, description="Number of bars actually used.")


class ComputeReturnsTool(BaseTool):
    name = "analytics.compute_returns"
    description = (
        "Compute the log-return series from the last N bars ending at end_ns "
        "(PIT-safe). Returns an empty list if fewer than 2 bars are available."
    )
    input_model = ComputeReturnsInput
    output_model = ComputeReturnsOutput

    async def _run(self, payload: ToolInput) -> ToolOutput:
        assert isinstance(payload, ComputeReturnsInput)
        bars = await _safe_read_bars(payload.symbol, payload.freq, 0, payload.end_ns, payload.venue)
        if len(bars) < 2:
            return ComputeReturnsOutput(returns=[], n_bars=len(bars))

        bars = bars[-(payload.lookback_bars + 1) :]
        closes = np.array([float(b.close) for b in bars], dtype=np.float64)
        log_returns: list[float] = np.diff(np.log(closes)).tolist()
        return ComputeReturnsOutput(returns=log_returns, n_bars=len(bars))


register(ComputeReturnsTool())


# ---------------------------------------------------------------------------
# analytics.compute_vol
# ---------------------------------------------------------------------------


class ComputeVolInput(ToolInput):
    """Input for analytics.compute_vol."""

    symbol: str = Field(description="Canonical symbol, e.g. BTC-USD.")
    lookback_bars: int = Field(
        ge=2,
        le=10_000,
        description="Number of bars to include in the vol estimate.",
    )
    freq: str = Field(default="1d", pattern=r"^(1m|1h|1d)$", description="Bar frequency.")
    end_ns: int = Field(description="PIT cutoff.")
    venue: str | None = Field(default=None, description="Optional venue filter.")


class ComputeVolOutput(ToolOutput):
    """Output for analytics.compute_vol."""

    realized_vol_annualized: float | None = Field(
        default=None,
        description=(
            "Realised volatility annualised by sqrt(bars_per_year) scaling. "
            "None if there is insufficient data."
        ),
    )
    n_returns: int = Field(default=0, description="Number of log-returns used in the estimate.")


class ComputeVolTool(BaseTool):
    name = "analytics.compute_vol"
    description = (
        "Realised volatility over the last N bars ending at end_ns (PIT-safe). "
        "Annualised by sqrt(bars_per_year) (252 for 1d, 8760 for 1h, 525600 for 1m)."
    )
    input_model = ComputeVolInput
    output_model = ComputeVolOutput

    async def _run(self, payload: ToolInput) -> ToolOutput:
        assert isinstance(payload, ComputeVolInput)
        bars = await _safe_read_bars(payload.symbol, payload.freq, 0, payload.end_ns, payload.venue)
        if len(bars) < payload.lookback_bars + 1:
            return ComputeVolOutput(realized_vol_annualized=None, n_returns=max(0, len(bars) - 1))

        bars = bars[-(payload.lookback_bars + 1) :]
        closes = np.array([float(b.close) for b in bars], dtype=np.float64)
        log_rets = np.diff(np.log(closes))
        per_year = _bars_per_year(payload.freq)
        vol = float(np.std(log_rets, ddof=1) * math.sqrt(per_year))
        return ComputeVolOutput(realized_vol_annualized=vol, n_returns=len(log_rets))


register(ComputeVolTool())


# ---------------------------------------------------------------------------
# analytics.compute_correlation
# ---------------------------------------------------------------------------


class ComputeCorrelationInput(ToolInput):
    """Input for analytics.compute_correlation."""

    symbol_a: str = Field(description="First canonical symbol.")
    symbol_b: str = Field(description="Second canonical symbol.")
    lookback_bars: int = Field(
        ge=3,
        le=10_000,
        description="Number of bars to include in the correlation estimate.",
    )
    freq: str = Field(default="1d", pattern=r"^(1m|1h|1d)$", description="Bar frequency.")
    end_ns: int = Field(description="PIT cutoff.")
    venue: str | None = Field(default=None, description="Optional venue filter.")


class ComputeCorrelationOutput(ToolOutput):
    """Output for analytics.compute_correlation."""

    correlation: float | None = Field(
        default=None,
        description="Pearson correlation of log-returns in [-1, 1]; None if insufficient data.",
    )
    n_overlapping: int = Field(
        default=0,
        description="Number of overlapping return observations used.",
    )


class ComputeCorrelationTool(BaseTool):
    name = "analytics.compute_correlation"
    description = (
        "Compute Pearson correlation of log-returns between two symbols over the last N bars "
        "ending at end_ns (PIT-safe).  Requires at least 3 overlapping return observations."
    )
    input_model = ComputeCorrelationInput
    output_model = ComputeCorrelationOutput

    async def _run(self, payload: ToolInput) -> ToolOutput:
        assert isinstance(payload, ComputeCorrelationInput)
        bars_a = await _safe_read_bars(
            payload.symbol_a, payload.freq, 0, payload.end_ns, payload.venue
        )
        bars_b = await _safe_read_bars(
            payload.symbol_b, payload.freq, 0, payload.end_ns, payload.venue
        )
        bars_a = bars_a[-(payload.lookback_bars + 1) :] if bars_a else []
        bars_b = bars_b[-(payload.lookback_bars + 1) :] if bars_b else []
        n = min(len(bars_a), len(bars_b))
        if n < 3:
            return ComputeCorrelationOutput(correlation=None, n_overlapping=max(0, n - 1))

        closes_a = np.array([float(b.close) for b in bars_a[-n:]], dtype=np.float64)
        closes_b = np.array([float(b.close) for b in bars_b[-n:]], dtype=np.float64)
        rets_a = np.diff(np.log(closes_a))
        rets_b = np.diff(np.log(closes_b))
        corr_matrix: Any = np.corrcoef(rets_a, rets_b)
        corr = float(corr_matrix[0, 1])
        return ComputeCorrelationOutput(correlation=corr, n_overlapping=len(rets_a))


register(ComputeCorrelationTool())


# ---------------------------------------------------------------------------
# analytics.compute_vwap
# ---------------------------------------------------------------------------


class ComputeVwapInput(ToolInput):
    """Input for analytics.compute_vwap."""

    symbol: str = Field(description="Canonical symbol, e.g. BTC-USD.")
    start_ns: int = Field(description="Window start, inclusive, nanoseconds since epoch.")
    end_ns: int = Field(description="Window end (PIT cutoff), exclusive.")
    freq: str = Field(
        default="1m",
        pattern=r"^(1m|1h|1d)$",
        description="Bar frequency used to approximate VWAP.",
    )
    venue: str | None = Field(default=None, description="Optional venue filter.")


class ComputeVwapOutput(ToolOutput):
    """Output for analytics.compute_vwap."""

    vwap: float | None = Field(
        default=None,
        description="Volume-weighted average price over the window.  None if no data.",
    )
    total_volume: float = Field(default=0.0, description="Total volume across all bars.")
    n_bars: int = Field(default=0, description="Number of bars used.")


class ComputeVwapTool(BaseTool):
    name = "analytics.compute_vwap"
    description = (
        "Compute VWAP (volume-weighted average price) for a symbol over [start_ns, end_ns) "
        "using bar data.  Each bar is weighted by its volume.  PIT-safe."
    )
    input_model = ComputeVwapInput
    output_model = ComputeVwapOutput

    async def _run(self, payload: ToolInput) -> ToolOutput:
        assert isinstance(payload, ComputeVwapInput)
        bars = await _safe_read_bars(
            payload.symbol, payload.freq, payload.start_ns, payload.end_ns, payload.venue
        )
        if not bars:
            return ComputeVwapOutput(vwap=None, total_volume=0.0, n_bars=0)

        pv_sum = 0.0
        vol_sum = 0.0
        for b in bars:
            vol = float(b.volume)
            price = float(b.vwap) if b.vwap is not None else float(b.high + b.low + b.close) / 3.0
            pv_sum += price * vol
            vol_sum += vol

        if vol_sum == 0.0:
            return ComputeVwapOutput(vwap=None, total_volume=0.0, n_bars=len(bars))

        return ComputeVwapOutput(
            vwap=pv_sum / vol_sum,
            total_volume=vol_sum,
            n_bars=len(bars),
        )


register(ComputeVwapTool())


# ---------------------------------------------------------------------------
# analytics.compute_sharpe
# ---------------------------------------------------------------------------


class ComputeSharpeInput(ToolInput):
    """Input for analytics.compute_sharpe."""

    symbol: str = Field(description="Canonical symbol.")
    lookback_bars: int = Field(
        ge=3, le=10_000, description="Number of bars for the estimate (≥3 returns)."
    )
    freq: str = Field(default="1d", pattern=r"^(1m|1h|1d)$", description="Bar frequency.")
    end_ns: int = Field(description="PIT cutoff.")
    risk_free_rate_annual: float = Field(
        default=0.0,
        description="Annual risk-free rate as a decimal (e.g. 0.045 for 4.5%).",
    )
    venue: str | None = Field(default=None, description="Optional venue filter.")


class ComputeSharpeOutput(ToolOutput):
    """Output for analytics.compute_sharpe."""

    sharpe_ratio: float | None = Field(
        default=None,
        description=(
            "Annualised Sharpe ratio computed from log-returns. "
            "None if insufficient data or zero std."
        ),
    )
    n_returns: int = Field(default=0)


class ComputeSharpeTool(BaseTool):
    name = "analytics.compute_sharpe"
    description = (
        "Compute annualised Sharpe ratio from log-returns over the last N bars ending at "
        "end_ns (PIT-safe).  Risk-free rate is converted to per-bar units before subtraction. "
        "Returns None if std=0 or fewer than 2 returns."
    )
    input_model = ComputeSharpeInput
    output_model = ComputeSharpeOutput

    async def _run(self, payload: ToolInput) -> ToolOutput:
        assert isinstance(payload, ComputeSharpeInput)
        bars = await _safe_read_bars(payload.symbol, payload.freq, 0, payload.end_ns, payload.venue)
        if len(bars) < payload.lookback_bars + 1:
            return ComputeSharpeOutput(sharpe_ratio=None, n_returns=max(0, len(bars) - 1))

        bars = bars[-(payload.lookback_bars + 1) :]
        closes = np.array([float(b.close) for b in bars], dtype=np.float64)
        log_rets = np.diff(np.log(closes))
        if len(log_rets) < 2:
            return ComputeSharpeOutput(sharpe_ratio=None, n_returns=len(log_rets))

        per_year = _bars_per_year(payload.freq)
        rf_per_bar = payload.risk_free_rate_annual / per_year
        excess = log_rets - rf_per_bar
        std = float(np.std(excess, ddof=1))
        # Tolerance for FP noise — math.exp(x*i) returns floats that produce
        # log-returns within ~1e-16 of each other even when "constant".
        if std < 1e-12:
            return ComputeSharpeOutput(sharpe_ratio=None, n_returns=len(log_rets))

        sharpe = float(np.mean(excess) / std * math.sqrt(per_year))
        return ComputeSharpeOutput(sharpe_ratio=sharpe, n_returns=len(log_rets))


register(ComputeSharpeTool())


# ---------------------------------------------------------------------------
# analytics.compute_drawdown
# ---------------------------------------------------------------------------


class ComputeDrawdownInput(ToolInput):
    """Input for analytics.compute_drawdown."""

    symbol: str = Field(description="Canonical symbol.")
    lookback_bars: int = Field(ge=2, le=10_000, description="Number of bars to inspect.")
    freq: str = Field(default="1d", pattern=r"^(1m|1h|1d)$", description="Bar frequency.")
    end_ns: int = Field(description="PIT cutoff.")
    venue: str | None = Field(default=None, description="Optional venue filter.")


class ComputeDrawdownOutput(ToolOutput):
    """Output for analytics.compute_drawdown."""

    max_drawdown: float | None = Field(
        default=None,
        description=(
            "Maximum peak-to-trough drawdown as a non-positive fraction "
            "(e.g. -0.18 = 18% loss).  None if insufficient data."
        ),
    )
    peak_index: int | None = Field(
        default=None, description="Bar index (0-based) of the peak preceding the trough."
    )
    trough_index: int | None = Field(default=None, description="Bar index of the trough.")
    n_bars: int = Field(default=0)


class ComputeDrawdownTool(BaseTool):
    name = "analytics.compute_drawdown"
    description = (
        "Compute maximum peak-to-trough drawdown over the last N close prices ending at "
        "end_ns (PIT-safe).  Returned as a non-positive fraction (e.g. -0.18 = 18% drawdown)."
    )
    input_model = ComputeDrawdownInput
    output_model = ComputeDrawdownOutput

    async def _run(self, payload: ToolInput) -> ToolOutput:
        assert isinstance(payload, ComputeDrawdownInput)
        bars = await _safe_read_bars(payload.symbol, payload.freq, 0, payload.end_ns, payload.venue)
        if len(bars) < 2:
            return ComputeDrawdownOutput(
                max_drawdown=None, peak_index=None, trough_index=None, n_bars=len(bars)
            )

        bars = bars[-payload.lookback_bars :]
        closes = np.array([float(b.close) for b in bars], dtype=np.float64)
        running_max = np.maximum.accumulate(closes)
        drawdowns = (closes - running_max) / running_max
        trough = int(np.argmin(drawdowns))
        # Peak index = where running_max equalled closes[trough]'s running_max.
        peak = int(np.argmax(closes[: trough + 1]))
        return ComputeDrawdownOutput(
            max_drawdown=float(drawdowns[trough]),
            peak_index=peak,
            trough_index=trough,
            n_bars=len(bars),
        )


register(ComputeDrawdownTool())
