from __future__ import annotations

from news_impact_model.schema import ImpactLabels, PricePoint


def label_event_impact(
    *,
    event_available_at_ns: int,
    asset_prices: list[PricePoint],
    benchmark_prices: list[PricePoint],
    horizons_ns: dict[str, int],
    asset_beta: float = 1.0,
) -> ImpactLabels:
    """Build abnormal-return labels for one historical news event.

    The base price is the latest observation at or before availability time.
    The horizon price is the first observation at or after ``available_at + h``.
    Abnormal return = asset_return - beta * benchmark_return.

    When ``asset_beta`` is 1.0 (default), this is a simple market-model
    abnormal return.  When beta is provided (e.g., 1.5 for a high-beta
    stock), the benchmark return is scaled by beta before subtraction,
    isolating the idiosyncratic (stock-specific) component of the move.
    """
    if not horizons_ns:
        raise ValueError("horizons_ns must not be empty")

    base_asset = _last_at_or_before(asset_prices, event_available_at_ns)
    base_benchmark = _last_at_or_before(benchmark_prices, event_available_at_ns)
    if base_asset is None:
        raise ValueError("asset_prices has no observation at or before event time")
    if base_benchmark is None:
        raise ValueError("benchmark_prices has no observation at or before event time")

    abnormal: dict[str, float] = {}
    for label, horizon_ns in horizons_ns.items():
        if horizon_ns <= 0:
            raise ValueError(f"horizon {label!r} must be positive")
        target_ts = event_available_at_ns + horizon_ns
        asset_future = _first_at_or_after(asset_prices, target_ts)
        benchmark_future = _first_at_or_after(benchmark_prices, target_ts)
        if asset_future is None or benchmark_future is None:
            continue
        asset_ret = _simple_return(base_asset.price, asset_future.price)
        benchmark_ret = _simple_return(base_benchmark.price, benchmark_future.price)
        abnormal[label] = round(asset_ret - asset_beta * benchmark_ret, 12)

    if not abnormal:
        raise ValueError("no horizon labels could be built from supplied prices")

    return ImpactLabels(
        abnormal_returns=abnormal,
        max_favorable_return=max(0.0, max(abnormal.values())),
        max_adverse_return=min(0.0, min(abnormal.values())),
    )


def _simple_return(start: float, end: float) -> float:
    if start <= 0:
        raise ValueError(f"start price must be positive, got {start}")
    return (end / start) - 1.0


def _last_at_or_before(points: list[PricePoint], ts_ns: int) -> PricePoint | None:
    candidates = [p for p in points if p.ts_ns <= ts_ns]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.ts_ns)


def _first_at_or_after(points: list[PricePoint], ts_ns: int) -> PricePoint | None:
    candidates = [p for p in points if p.ts_ns >= ts_ns]
    if not candidates:
        return None
    return min(candidates, key=lambda p: p.ts_ns)
