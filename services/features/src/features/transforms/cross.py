"""
features.transforms.cross — rolling beta + correlation vs a benchmark.

Maintains two parallel deques per symbol-pair:

  - ``self._bench_rets``           rolling benchmark log-returns.
  - ``self._sym_rets[symbol]``     rolling symbol log-returns.

For each window ``w``, on every symbol update we recompute beta and
Pearson correlation over the last ``w`` aligned-by-position elements of
both deques.  The "by-position" alignment assumes 1-min bars across
venues co-arrive at roughly the same wall-clock minute; for tighter
alignment (e.g., to the nanosecond) a future revision can switch to a
ts_event-keyed dict.  Spec landmine #5 documents this trade-off.

Until both deques have ``w`` samples, beta and corr are ``None`` (no
defaulting to zero — that would lie about the data).
"""

from __future__ import annotations

import math
from collections import deque


class CrossFeatures:
    """Per-symbol rolling beta + correlation against one benchmark symbol."""

    DEFAULT_WINDOWS = (60, 240)

    def __init__(
        self,
        *,
        benchmark_symbol: str = "BTC-USD",
        windows: tuple[int, ...] = DEFAULT_WINDOWS,
    ) -> None:
        if not windows:
            raise ValueError("windows must contain at least one positive int")
        if any(w < 2 for w in windows):
            # w < 2 makes covariance/variance undefined.
            raise ValueError("all windows must be >= 2 for a meaningful covariance")
        self._bench = benchmark_symbol
        self._windows = windows
        cap = max(windows)
        self._bench_rets: deque[float] = deque(maxlen=cap)
        self._sym_rets: dict[str, deque[float]] = {}

    @property
    def benchmark(self) -> str:
        return self._bench

    @property
    def feature_keys(self) -> tuple[str, ...]:
        out: list[str] = []
        for w in self._windows:
            out.append(f"beta_{self._bench}_{w}")
            out.append(f"corr_{self._bench}_{w}")
        return tuple(out)

    def on_benchmark_ret(self, r: float | None) -> None:
        """Append a benchmark return; ``None`` is dropped silently."""
        if r is not None:
            self._bench_rets.append(r)

    def on_symbol_ret(self, symbol: str, r: float | None) -> dict[str, float | None]:
        """Append a symbol return and return all (beta, corr) for that symbol."""
        cap = self._bench_rets.maxlen
        d = self._sym_rets.setdefault(symbol, deque(maxlen=cap))
        if r is not None:
            d.append(r)

        out: dict[str, float | None] = {}
        for w in self._windows:
            beta, corr = self._beta_and_corr(d, w)
            out[f"beta_{self._bench}_{w}"] = beta
            out[f"corr_{self._bench}_{w}"] = corr
        return out

    def _beta_and_corr(self, sym_rets: deque[float], w: int) -> tuple[float | None, float | None]:
        if len(sym_rets) < w or len(self._bench_rets) < w:
            return None, None
        xs = list(sym_rets)[-w:]
        ys = list(self._bench_rets)[-w:]
        mx = sum(xs) / w
        my = sum(ys) / w
        cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
        var_y = sum((y - my) ** 2 for y in ys)
        var_x = sum((x - mx) ** 2 for x in xs)
        beta = cov / var_y if var_y > 0 else None
        corr = cov / math.sqrt(var_x * var_y) if var_x > 0 and var_y > 0 else None
        return beta, corr
