"""
features.transforms.volatility — realized vol + Parkinson + Garman-Klass.

Three families of per-bar volatility estimators over configurable windows:

  - ``vol_rs_w``    Rolling-sample standard deviation of 1-bar log returns
                    over the last ``w`` bars (sample stdev, ``ddof=1``).
  - ``vol_park_w``  Parkinson estimator from per-bar high/low ranges:
                        sqrt( (1 / (4 ln 2)) * mean( ln(H/L)^2 ) )
                    Roughly 5x more efficient than close-to-close vol.
  - ``vol_gk_w``    Garman-Klass estimator using OHLC:
                        sqrt( mean( 0.5 * ln(H/L)^2
                                    - (2 ln 2 - 1) * ln(C/O)^2 ) )
                    Even more efficient when bars are well-behaved; can
                    go negative (returns ``None``) when the close-to-open
                    move dominates the high-low range.

All three are PIT-correct: each value uses only bars up to and including
the one being updated.  ``None`` is emitted until the requested window
has been filled.
"""

from __future__ import annotations

import math
from collections import deque
from decimal import Decimal


class VolatilityFeatures:
    """Per-symbol OHLC + log-return history -> vol estimators."""

    DEFAULT_WINDOWS = (5, 20, 30, 60, 240)
    _GK_K = 2.0 * math.log(2.0) - 1.0
    _PARK_NORM = 4.0 * math.log(2.0)

    def __init__(self, *, windows: tuple[int, ...] = DEFAULT_WINDOWS) -> None:
        if not windows:
            raise ValueError("windows must contain at least one positive int")
        if any(w < 1 for w in windows):
            raise ValueError("all windows must be >= 1")
        self._windows = windows
        cap = max(windows) + 1
        # Log returns deque drives realized vol; OHLC deque drives Park / GK.
        self._log_rets: deque[float] = deque(maxlen=cap)
        self._bars: deque[tuple[float, float, float, float]] = deque(maxlen=cap)

    @property
    def feature_keys(self) -> tuple[str, ...]:
        out: list[str] = []
        for w in self._windows:
            out.extend([f"vol_rs_{w}", f"vol_park_{w}", f"vol_gk_{w}"])
        return tuple(out)

    def update(
        self,
        o: Decimal,
        h: Decimal,
        low: Decimal,
        c: Decimal,
        log_ret: float | None,
    ) -> dict[str, float | None]:
        # log_ret is computed externally (in PriceFeatures) so we don't
        # duplicate state; pass-through None on the first bar.
        if log_ret is not None:
            self._log_rets.append(log_ret)
        self._bars.append((float(o), float(h), float(low), float(c)))

        out: dict[str, float | None] = {}
        for w in self._windows:
            out[f"vol_rs_{w}"] = self._realized_vol(w)
            out[f"vol_park_{w}"] = self._parkinson(w)
            out[f"vol_gk_{w}"] = self._garman_klass(w)
        return out

    def _realized_vol(self, w: int) -> float | None:
        if len(self._log_rets) < w:
            return None
        xs = list(self._log_rets)[-w:]
        mean = sum(xs) / w
        # Sample stdev (ddof=1) — matches numpy.std(rets, ddof=1).
        # max(w-1, 1) avoids divide-by-zero when w == 1 (degenerate but supported).
        var = sum((x - mean) ** 2 for x in xs) / max(w - 1, 1)
        return math.sqrt(var)

    def _parkinson(self, w: int) -> float | None:
        if len(self._bars) < w:
            return None
        sub = list(self._bars)[-w:]
        accumulator = 0.0
        for _o, h, lo, _c in sub:
            if lo > 0:
                accumulator += math.log(h / lo) ** 2
        # Per the canonical Parkinson estimator, the ln(H/L)^2 mean is
        # normalized by 4 ln 2.  Keeping the divisor as `w` (not "valid
        # bars") matches the spec; in practice every well-formed bar has
        # L > 0 so the masked-out case is vanishingly rare.
        return math.sqrt(accumulator / (w * self._PARK_NORM))

    def _garman_klass(self, w: int) -> float | None:
        if len(self._bars) < w:
            return None
        sub = list(self._bars)[-w:]
        accumulator = 0.0
        for o, h, lo, c in sub:
            if lo > 0 and o > 0:
                accumulator += 0.5 * math.log(h / lo) ** 2 - self._GK_K * math.log(c / o) ** 2
        # GK is theoretically non-negative for typical bars; in pathological
        # cases (tiny H-L, large C-O) the sample sum can dip below zero —
        # we emit None rather than sqrt(-x) which would raise.
        if accumulator <= 0:
            return None
        return math.sqrt(accumulator / w)
