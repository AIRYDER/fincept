"""
quant_foundry.modules.features.pre_event_momentum — pre-event price momentum & volatility.

Computes price-based features from OHLCV bars *strictly before* the
decision time (PIT-correct).  These capture the price regime leading
into a media event, allowing the model to condition media→price
response on prior momentum and volatility.

Features produced (per ``(symbol, decision_time)``):
    ``pre_return_1d``  — return over 1 trading day before decision_time
    ``pre_return_5d``  — return over 5 trading days before decision_time
    ``pre_volatility_20d`` — std of daily returns over 20 trading days
    ``pre_volatility_60d`` — std of daily returns over 60 trading days
    ``pre_volume_ratio_5d`` — latest day volume / 5-day average volume

PIT correctness: only bars with ``ts_ns < decision_time`` are used.
If there is insufficient history for a feature, its value is 0.0.

This module is registered as ``feature:pre-event-momentum:1.0.0``.
"""

from __future__ import annotations

import math
from typing import Any

from quant_foundry.modules.registry import (
    FeatureComputer,
    MediaItem,
    ModuleInfo,
    PriceBar,
    SentimentResult,
    register_module,
)

NS_PER_DAY = 86_400_000_000_000


@register_module(
    "feature",
    "pre-event-momentum",
    "1.0.0",
    default_config={
        "lookback_days": 60,
        "volatility_windows": [20, 60],
        "return_horizons": [1, 5],
    },
)
class PreEventMomentumFeatures:
    """Compute pre-event price momentum and volatility features.

    For each ``(symbol, decision_time)`` in the date range, uses price
    bars with ``ts_ns < decision_time`` to compute return, volatility,
    and volume-ratio features.  Requires ``price_bars`` to be passed
    via the ``compute_features`` kwarg; if absent, returns an empty
    dict (the module is a no-op without price data).
    """

    info: ModuleInfo

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}
        self.lookback_days: int = int(self.config.get("lookback_days", 60))
        self.volatility_windows: list[int] = list(
            self.config.get("volatility_windows", [20, 60]),
        )
        self.return_horizons: list[int] = list(
            self.config.get("return_horizons", [1, 5]),
        )

    # ------------------------------------------------------------------ #
    # Helpers                                                            #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _pit_bars(bars: list[PriceBar], decision_time: int) -> list[PriceBar]:
        """Return bars strictly before ``decision_time``, sorted by ts_ns."""
        pit = [b for b in bars if b.ts_ns < decision_time]
        pit.sort(key=lambda b: b.ts_ns)
        return pit

    @staticmethod
    def _daily_returns(closes: list[float]) -> list[float]:
        """Compute simple daily returns from a list of close prices."""
        if len(closes) < 2:
            return []
        returns: list[float] = []
        for i in range(1, len(closes)):
            prev = closes[i - 1]
            if prev == 0.0:
                returns.append(0.0)
            else:
                returns.append((closes[i] - prev) / prev)
        return returns

    @staticmethod
    def _std(values: list[float]) -> float:
        """Population standard deviation of a list of floats."""
        n = len(values)
        if n == 0:
            return 0.0
        mean = sum(values) / n
        var = sum((v - mean) ** 2 for v in values) / n
        return math.sqrt(var)

    def _return_over_horizon(
        self,
        pit_bars: list[PriceBar],
        horizon: int,
    ) -> float:
        """Return over the last ``horizon`` trading days before decision_time.

        ``return = close[-1] / close[-1 - horizon] - 1``.
        Returns 0.0 if insufficient history.
        """
        if len(pit_bars) < horizon + 1:
            return 0.0
        end_close = pit_bars[-1].close
        start_close = pit_bars[-1 - horizon].close
        if start_close == 0.0:
            return 0.0
        return round(end_close / start_close - 1.0, 6)

    def _volatility_over_window(
        self,
        pit_bars: list[PriceBar],
        window: int,
    ) -> float:
        """Std of daily returns over the last ``window`` trading days.

        Returns 0.0 if insufficient history.
        """
        if len(pit_bars) < window + 1:
            return 0.0
        recent = pit_bars[-(window + 1):]
        closes = [b.close for b in recent]
        returns = self._daily_returns(closes)
        # returns has len == window
        return round(self._std(returns), 6)

    def _volume_ratio_5d(self, pit_bars: list[PriceBar]) -> float:
        """Ratio of latest day volume to 5-day average volume.

        Returns 0.0 if insufficient history or zero average volume.
        """
        if len(pit_bars) < 6:
            return 0.0
        latest_volume = pit_bars[-1].volume
        avg_volume = sum(b.volume for b in pit_bars[-6:-1]) / 5.0
        if avg_volume <= 0.0:
            return 0.0
        return round(latest_volume / avg_volume, 6)

    # ------------------------------------------------------------------ #
    # Protocol interface                                                 #
    # ------------------------------------------------------------------ #

    def compute_features(
        self,
        items: list[MediaItem],
        sentiments: list[SentimentResult],
        *,
        symbols: list[str],
        start_ns: int,
        end_ns: int,
        price_bars: dict[str, list[PriceBar]] | None = None,
    ) -> dict[str, dict[int, dict[str, float]]]:
        """Compute pre-event momentum and volatility features.

        Returns ``{symbol: {decision_time: {feature_name: value}}}``.

        ``price_bars`` maps symbol → list of :class:`PriceBar`.  If
        ``None``, returns an empty dict (no-op without price data).

        Decision times are aligned to media item availability times
        (same convention as :class:`PerEventTypeFeatures`).
        """
        if price_bars is None:
            return {}

        # Group items by symbol to determine decision times
        items_by_symbol: dict[str, list[MediaItem]] = {}
        for item in items:
            for sym in item.symbols:
                if sym in symbols:
                    items_by_symbol.setdefault(sym, []).append(item)

        for sym in items_by_symbol:
            items_by_symbol[sym].sort(key=lambda i: i.available_at_ns)

        result: dict[str, dict[int, dict[str, float]]] = {}

        for sym in symbols:
            sym_items = items_by_symbol.get(sym, [])
            sym_bars = price_bars.get(sym, [])
            if not sym_items or not sym_bars:
                continue

            sym_result: dict[int, dict[str, float]] = {}
            for item in sym_items:
                dt = item.available_at_ns
                if dt < start_ns or dt >= end_ns:
                    continue

                pit_bars = self._pit_bars(sym_bars, dt)
                if not pit_bars:
                    continue

                features: dict[str, float] = {}

                # Return horizons
                for horizon in self.return_horizons:
                    features[f"pre_return_{horizon}d"] = (
                        self._return_over_horizon(pit_bars, horizon)
                    )

                # Volatility windows
                for window in self.volatility_windows:
                    features[f"pre_volatility_{window}d"] = (
                        self._volatility_over_window(pit_bars, window)
                    )

                # Volume ratio (fixed 5d)
                features["pre_volume_ratio_5d"] = self._volume_ratio_5d(pit_bars)

                sym_result[dt] = features

            if sym_result:
                result[sym] = sym_result

        return result


__all__ = ["PreEventMomentumFeatures"]
