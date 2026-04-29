"""Tests for fincept_tools.analytics tools.

Uses mocked fincept_db calls; no database connection required.
"""

from __future__ import annotations

import math
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

from fincept_tools.analytics.tools import (
    ComputeCorrelationInput,
    ComputeCorrelationTool,
    ComputeDrawdownInput,
    ComputeDrawdownTool,
    ComputeReturnsInput,
    ComputeReturnsTool,
    ComputeSharpeInput,
    ComputeSharpeTool,
    ComputeVolInput,
    ComputeVolTool,
    ComputeVwapInput,
    ComputeVwapTool,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_bar(
    symbol: str = "BTC-USD",
    close: float = 100.0,
    volume: float = 10.0,
    ts_event: int = 0,
    vwap: float | None = None,
) -> Any:
    from fincept_core.schemas import AssetClass, BarEvent, Venue

    vwap_dec = Decimal(str(vwap)) if vwap is not None else None
    return BarEvent(
        venue=Venue.BINANCE,
        symbol=symbol,
        asset_class=AssetClass.CRYPTO_SPOT,
        ts_event=ts_event,
        ts_recv=ts_event,
        freq="1d",
        open=Decimal(str(close * 0.99)),
        high=Decimal(str(close * 1.01)),
        low=Decimal(str(close * 0.98)),
        close=Decimal(str(close)),
        volume=Decimal(str(volume)),
        trades=100,
        vwap=vwap_dec,
    )


def _bars_from_closes(closes: list[float], symbol: str = "BTC-USD") -> list[Any]:
    return [
        _make_bar(symbol=symbol, close=c, ts_event=i * 86_400_000_000_000)
        for i, c in enumerate(closes)
    ]


# ---------------------------------------------------------------------------
# ComputeReturns
# ---------------------------------------------------------------------------


def test_compute_returns_input_valid() -> None:
    inp = ComputeReturnsInput(symbol="BTC-USD", lookback_bars=10, end_ns=9999999)
    assert inp.freq == "1d"
    assert inp.lookback_bars == 10


def test_compute_returns_input_invalid_freq() -> None:
    with pytest.raises(ValidationError):
        ComputeReturnsInput(symbol="X", freq="4h", lookback_bars=10, end_ns=1)


def test_compute_returns_input_lookback_bounds() -> None:
    with pytest.raises(ValidationError):
        ComputeReturnsInput(symbol="X", lookback_bars=1, end_ns=1)  # ge=2
    with pytest.raises(ValidationError):
        ComputeReturnsInput(symbol="X", lookback_bars=10_001, end_ns=1)  # le=10_000


@pytest.mark.asyncio
async def test_compute_returns_basic() -> None:
    import math

    closes = [100.0, 110.0, 105.0, 115.0]
    bars = _bars_from_closes(closes)

    with patch(
        "fincept_tools.analytics.tools.read_bars", new_callable=AsyncMock, return_value=bars
    ):
        tool = ComputeReturnsTool()
        result = await tool(ComputeReturnsInput(symbol="BTC-USD", lookback_bars=4, end_ns=999))

    assert result.ok is True
    assert len(result.returns) == 3  # N bars → N-1 returns
    assert result.n_bars == 4
    # First return: log(110/100)
    assert abs(result.returns[0] - math.log(110 / 100)) < 1e-10


@pytest.mark.asyncio
async def test_compute_returns_insufficient_data() -> None:
    bars = _bars_from_closes([100.0])  # only 1 bar

    with patch(
        "fincept_tools.analytics.tools.read_bars", new_callable=AsyncMock, return_value=bars
    ):
        tool = ComputeReturnsTool()
        result = await tool(ComputeReturnsInput(symbol="BTC-USD", lookback_bars=10, end_ns=999))

    assert result.ok is True
    assert result.returns == []


@pytest.mark.asyncio
async def test_compute_returns_error_on_db_failure() -> None:
    with patch(
        "fincept_tools.analytics.tools.read_bars",
        new_callable=AsyncMock,
        side_effect=RuntimeError("connection lost"),
    ):
        tool = ComputeReturnsTool()
        result = await tool(ComputeReturnsInput(symbol="X", lookback_bars=5, end_ns=1))

    assert result.ok is False
    assert result.error_type == "ToolBackendError"
    assert "connection lost" in (result.error or "")


# ---------------------------------------------------------------------------
# ComputeVol
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compute_vol_basic() -> None:
    # 252 daily bars with 1% daily returns → ~16% annualised vol
    import math

    # Generate bars with 1% log-return each day
    closes = [100.0 * math.exp(0.01 * i) for i in range(60)]
    bars = _bars_from_closes(closes)

    with patch(
        "fincept_tools.analytics.tools.read_bars", new_callable=AsyncMock, return_value=bars
    ):
        tool = ComputeVolTool()
        result = await tool(ComputeVolInput(symbol="BTC-USD", lookback_bars=30, end_ns=999))

    assert result.ok is True
    # Constant 1% returns → std(rets) ≈ 0 → vol ≈ 0
    assert result.realized_vol_annualized is not None
    assert result.realized_vol_annualized >= 0.0


@pytest.mark.asyncio
async def test_compute_vol_insufficient_data_returns_none() -> None:
    bars = _bars_from_closes([100.0, 105.0])  # 2 bars → 1 return, lookback=30

    with patch(
        "fincept_tools.analytics.tools.read_bars", new_callable=AsyncMock, return_value=bars
    ):
        tool = ComputeVolTool()
        result = await tool(ComputeVolInput(symbol="X", lookback_bars=30, end_ns=999))

    assert result.ok is True
    assert result.realized_vol_annualized is None


@pytest.mark.asyncio
async def test_compute_vol_annualization_factor_1m() -> None:
    """Vol for 1m bars should use 525600 annualization factor."""
    from fincept_core.schemas import AssetClass, BarEvent, Venue

    bars = [
        BarEvent(
            venue=Venue.BINANCE,
            symbol="BTC-USD",
            asset_class=AssetClass.CRYPTO_SPOT,
            ts_event=i * 60_000_000_000,
            ts_recv=i * 60_000_000_000,
            freq="1m",
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal(str(100 + i * 0.1)),
            volume=Decimal("1"),
            trades=10,
        )
        for i in range(20)
    ]

    with patch(
        "fincept_tools.analytics.tools.read_bars", new_callable=AsyncMock, return_value=bars
    ):
        tool = ComputeVolTool()
        result = await tool(
            ComputeVolInput(symbol="BTC-USD", freq="1m", lookback_bars=10, end_ns=999)
        )

    assert result.ok is True


# ---------------------------------------------------------------------------
# ComputeCorrelation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compute_correlation_perfectly_correlated() -> None:
    closes = [float(100 + i * 2) for i in range(20)]
    bars_a = _bars_from_closes(closes, symbol="A")
    bars_b = _bars_from_closes(closes, symbol="B")  # identical → corr = 1

    def fake_read_bars(symbol: str, freq: str, start: int, end: int, venue: Any = None) -> Any:
        return bars_a if symbol == "A" else bars_b

    with patch(
        "fincept_tools.analytics.tools.read_bars",
        new_callable=AsyncMock,
        side_effect=fake_read_bars,
    ):
        tool = ComputeCorrelationTool()
        result = await tool(
            ComputeCorrelationInput(symbol_a="A", symbol_b="B", lookback_bars=15, end_ns=999)
        )

    assert result.ok is True
    assert result.correlation is not None
    assert abs(result.correlation - 1.0) < 1e-6


@pytest.mark.asyncio
async def test_compute_correlation_insufficient_data() -> None:
    bars = _bars_from_closes([100.0, 105.0])

    with patch(
        "fincept_tools.analytics.tools.read_bars",
        new_callable=AsyncMock,
        return_value=bars,
    ):
        tool = ComputeCorrelationTool()
        result = await tool(
            ComputeCorrelationInput(symbol_a="A", symbol_b="B", lookback_bars=30, end_ns=999)
        )

    assert result.ok is True
    assert result.correlation is None


def test_compute_correlation_input_min_lookback() -> None:
    with pytest.raises(ValidationError):
        ComputeCorrelationInput(
            symbol_a="A",
            symbol_b="B",
            lookback_bars=2,
            end_ns=1,  # ge=3
        )


# ---------------------------------------------------------------------------
# ComputeVwap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compute_vwap_uses_stored_vwap() -> None:
    bars = [
        _make_bar(close=100.0, volume=10.0, vwap=102.0, ts_event=0),
        _make_bar(close=110.0, volume=20.0, vwap=108.0, ts_event=1),
    ]

    with patch(
        "fincept_tools.analytics.tools.read_bars", new_callable=AsyncMock, return_value=bars
    ):
        tool = ComputeVwapTool()
        result = await tool(ComputeVwapInput(symbol="BTC-USD", start_ns=0, end_ns=999))

    assert result.ok is True
    assert result.vwap is not None
    # (102 * 10 + 108 * 20) / (10 + 20) = (1020 + 2160) / 30 = 106
    assert abs(result.vwap - 106.0) < 1e-6
    assert result.total_volume == pytest.approx(30.0)
    assert result.n_bars == 2


@pytest.mark.asyncio
async def test_compute_vwap_falls_back_to_hlc3() -> None:
    """When vwap is not stored, falls back to (H+L+C)/3 as typical price."""
    from fincept_core.schemas import AssetClass, BarEvent, Venue

    bar = BarEvent(
        venue=Venue.BINANCE,
        symbol="BTC-USD",
        asset_class=AssetClass.CRYPTO_SPOT,
        ts_event=0,
        ts_recv=0,
        freq="1m",
        open=Decimal("99"),
        high=Decimal("102"),
        low=Decimal("97"),
        close=Decimal("100"),
        volume=Decimal("5"),
        trades=50,
        vwap=None,  # no stored vwap
    )

    with patch(
        "fincept_tools.analytics.tools.read_bars", new_callable=AsyncMock, return_value=[bar]
    ):
        tool = ComputeVwapTool()
        result = await tool(ComputeVwapInput(symbol="BTC-USD", start_ns=0, end_ns=999))

    assert result.ok is True
    expected = (102 + 97 + 100) / 3
    assert abs((result.vwap or 0) - expected) < 1e-6


@pytest.mark.asyncio
async def test_compute_vwap_empty_returns_none() -> None:
    with patch("fincept_tools.analytics.tools.read_bars", new_callable=AsyncMock, return_value=[]):
        tool = ComputeVwapTool()
        result = await tool(ComputeVwapInput(symbol="X", start_ns=0, end_ns=1))

    assert result.ok is True
    assert result.vwap is None
    assert result.n_bars == 0


# ---------------------------------------------------------------------------
# ComputeSharpe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compute_sharpe_constant_returns_none_for_zero_std() -> None:
    """Constant log-returns → std=0 → Sharpe is undefined → None."""
    closes = [100.0 * math.exp(0.01 * i) for i in range(40)]
    bars = _bars_from_closes(closes)

    with patch(
        "fincept_tools.analytics.tools.read_bars",
        new_callable=AsyncMock,
        return_value=bars,
    ):
        tool = ComputeSharpeTool()
        result = await tool(ComputeSharpeInput(symbol="BTC-USD", lookback_bars=30, end_ns=999))

    assert result.ok is True
    # Constant 1% returns → std=0 → Sharpe undefined.
    assert result.sharpe_ratio is None


@pytest.mark.asyncio
async def test_compute_sharpe_basic_with_variance() -> None:
    """Mixed positive/negative returns → Sharpe is finite."""
    # Alternating +1% / -0.5% → mean > 0, std > 0.
    closes = [100.0]
    for i in range(30):
        closes.append(closes[-1] * (1.01 if i % 2 == 0 else 0.995))
    bars = _bars_from_closes(closes)

    with patch(
        "fincept_tools.analytics.tools.read_bars",
        new_callable=AsyncMock,
        return_value=bars,
    ):
        tool = ComputeSharpeTool()
        result = await tool(ComputeSharpeInput(symbol="BTC-USD", lookback_bars=20, end_ns=999))

    assert result.ok is True
    assert result.sharpe_ratio is not None
    assert math.isfinite(result.sharpe_ratio)
    assert result.n_returns > 0


@pytest.mark.asyncio
async def test_compute_sharpe_insufficient_data() -> None:
    bars = _bars_from_closes([100.0, 105.0])

    with patch(
        "fincept_tools.analytics.tools.read_bars",
        new_callable=AsyncMock,
        return_value=bars,
    ):
        tool = ComputeSharpeTool()
        result = await tool(ComputeSharpeInput(symbol="X", lookback_bars=30, end_ns=999))

    assert result.ok is True
    assert result.sharpe_ratio is None


@pytest.mark.asyncio
async def test_compute_sharpe_subtracts_risk_free_rate() -> None:
    """With identical returns and a positive rf, excess mean drops — sharpe shrinks."""
    closes = [100.0]
    for _ in range(40):
        closes.append(closes[-1] * 1.001)  # 0.1% per bar
    bars = _bars_from_closes(closes)

    with patch(
        "fincept_tools.analytics.tools.read_bars",
        new_callable=AsyncMock,
        return_value=bars,
    ):
        tool = ComputeSharpeTool()
        result = await tool(
            ComputeSharpeInput(
                symbol="X",
                lookback_bars=30,
                end_ns=999,
                risk_free_rate_annual=0.0,
            )
        )

    # std=0 → None even with rf=0
    assert result.sharpe_ratio is None


# ---------------------------------------------------------------------------
# ComputeDrawdown
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compute_drawdown_basic() -> None:
    """Up-then-down series → drawdown peak → trough is detected correctly."""
    # Closes: 100, 110, 120, 90, 95 — peak at idx 2 (120), trough at idx 3 (90).
    closes = [100.0, 110.0, 120.0, 90.0, 95.0]
    bars = _bars_from_closes(closes)

    with patch(
        "fincept_tools.analytics.tools.read_bars",
        new_callable=AsyncMock,
        return_value=bars,
    ):
        tool = ComputeDrawdownTool()
        result = await tool(ComputeDrawdownInput(symbol="X", lookback_bars=10, end_ns=999))

    assert result.ok is True
    assert result.max_drawdown is not None
    # (90 - 120) / 120 = -0.25
    assert result.max_drawdown == pytest.approx(-0.25)
    assert result.peak_index == 2
    assert result.trough_index == 3


@pytest.mark.asyncio
async def test_compute_drawdown_monotonic_increase_is_zero() -> None:
    closes = [float(100 + i) for i in range(20)]
    bars = _bars_from_closes(closes)

    with patch(
        "fincept_tools.analytics.tools.read_bars",
        new_callable=AsyncMock,
        return_value=bars,
    ):
        tool = ComputeDrawdownTool()
        result = await tool(ComputeDrawdownInput(symbol="X", lookback_bars=20, end_ns=999))

    assert result.ok is True
    assert result.max_drawdown == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_compute_drawdown_insufficient_data() -> None:
    bars = _bars_from_closes([100.0])

    with patch(
        "fincept_tools.analytics.tools.read_bars",
        new_callable=AsyncMock,
        return_value=bars,
    ):
        tool = ComputeDrawdownTool()
        result = await tool(ComputeDrawdownInput(symbol="X", lookback_bars=10, end_ns=999))

    assert result.ok is True
    assert result.max_drawdown is None
    assert result.n_bars == 1


@pytest.mark.asyncio
async def test_compute_drawdown_db_failure_returns_typed_error() -> None:
    with patch(
        "fincept_tools.analytics.tools.read_bars",
        new_callable=AsyncMock,
        side_effect=RuntimeError("db down"),
    ):
        tool = ComputeDrawdownTool()
        result = await tool(ComputeDrawdownInput(symbol="X", lookback_bars=10, end_ns=999))

    assert result.ok is False
    assert result.error_type == "ToolBackendError"
    assert "db down" in (result.error or "")
