"""
quant_foundry.modules.universe.sp500 — S&P 500 universe selector.

Returns the S&P 500 ticker list.  This is a static list (the current
S&P 500 constituents) — it does not account for historical membership
changes.  A future version could load a point-in-time S&P 500
membership file for survivorship-bias-free backtesting.

This module is registered as ``universe:sp500:1.0.0``.
"""

from __future__ import annotations

from typing import Any

from quant_foundry.modules.registry import (
    ModuleInfo,
    register_module,
)

#: Current S&P 500 constituents (as of 2025).  This is a static list;
#: a future version may load a PIT membership file.
_SP500_TICKERS: tuple[str, ...] = (
    "AAPL", "MSFT", "AMZN", "NVDA", "GOOGL", "GOOG", "META", "TSLA",
    "BRK.B", "JPM", "V", "JNJ", "WMT", "XOM", "MA", "PG", "UNH", "HD",
    "CVX", "ORCL", "ABBV", "MRK", "KO", "PEP", "AVGO", "COST", "MCD",
    "CRM", "ADBE", "BAC", "TMO", "ACN", "ABT", "NFLX", "DHR", "LIN",
    "TXN", "WFC", "PM", "CSCO", "NEE", "QCOM", "AMD", "INTC", "LOW",
    "UPS", "SPGI", "INTU", "AMGN", "IBM", "CAT", "GS", "RTX", "BLK",
    "BA", "AMAT", "GE", "DE", "ISRG", "AXP", "GS", "MDT", "SYK", "ADI",
    "GILD", "PLD", "BKNG", "TMUS", "ADP", "C", "CB", "MO", "BX", "CL",
    "AMT", "TJX", "MSCI", "MMC", "REGN", "SCHW", "LRCX", "ETN", "SLB",
    "APD", "COP", "BMY", "WMB", "FIS", "CI", "PNC", "DUK", "SO", "NSC",
    "ITW", "SHW", "ZTS", "HUM", "SNPS", "CDNS", "ICE", "KLAC", "EQIX",
    "WM", "MDLZ", "FDX", "PSX", "OXY", "EOG", "PXD", "MPC", "VLO",
    "TRV", "AON", "MCK", "MAR", "CTAS", "ORLY", "ECL", "KMB", "RSG",
    "WELL", "AJG", "PSA", "CMI", "APH", "ROST", "DLR", "YUM", "TT",
    "BKR", "FTNT", "ANET", "PANW", "SNPS", "CDNS", "CRWD", "NOW",
    "TEAM", "DDOG", "NET", "MDB", "ZS", "PINS", "RBLX", "U", "DASH",
    "ABNB", "COIN", "PLTR", "SOFI", "MSTR", "MARA", "RIOT", "HUT",
)

#: Deduplicated, sorted.
_SP500_TICKERS = tuple(sorted(set(_SP500_TICKERS)))


@register_module(
    "universe",
    "sp500",
    "1.0.0",
    default_config={
        "max_symbols": None,  # None = all; set to int for a subset
    },
)
class SP500Universe:
    """S&P 500 universe selector.

    Returns the S&P 500 ticker list.  Set ``max_symbols`` in the config
    to limit to the first N tickers (useful for quick tests).
    """

    info: ModuleInfo

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}
        self.max_symbols: int | None = self.config.get("max_symbols")

    def select_symbols(
        self,
        *,
        start_ns: int,
        end_ns: int,
    ) -> list[str]:
        tickers = list(_SP500_TICKERS)
        if self.max_symbols is not None:
            tickers = tickers[: self.max_symbols]
        return tickers


__all__ = ["SP500Universe"]
