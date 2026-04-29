"""
features.transforms.price — log/simple returns + multi-window momentum.

Incremental and side-effect-free: every call to :meth:`PriceFeatures.update`
appends one close to the rolling deque and returns the feature dict for
that bar.  ``None`` is emitted for any feature that doesn't yet have
enough history (PIT-correct: never invent values).

Why ``float`` and not ``Decimal``?  Returns and momentum are unitless
ratios — IEEE-754 double precision is more than enough.  Money quantities
(prices, sizes, notionals) stay ``Decimal`` upstream; the conversion happens
once here at the input.  Spec landmine #2 makes this distinction explicit.
"""

from __future__ import annotations

import math
from collections import deque
from decimal import Decimal


class PriceFeatures:
    """Per-symbol rolling close history -> returns + momentum features."""

    DEFAULT_MOMENTUM_LOOKBACKS = (5, 20, 60)

    def __init__(
        self,
        *,
        max_lookback: int = 240,
        momentum_lookbacks: tuple[int, ...] = DEFAULT_MOMENTUM_LOOKBACKS,
    ) -> None:
        if max_lookback < max(momentum_lookbacks, default=1) + 1:
            raise ValueError(
                "max_lookback must be > max(momentum_lookbacks) so we can compute "
                "the longest momentum window"
            )
        self._momentum_lookbacks = momentum_lookbacks
        # +1 because momentum_k needs k bars in the past PLUS the current bar.
        self._closes: deque[float] = deque(maxlen=max_lookback + 1)

    @property
    def feature_keys(self) -> tuple[str, ...]:
        """The full set of keys this transform can emit (for None-bootstrapping)."""
        return ("ret_log_1", "ret_simple_1", *(f"mom_{k}" for k in self._momentum_lookbacks))

    def update(self, close: Decimal) -> dict[str, float | None]:
        self._closes.append(float(close))

        # Bootstrap: a single close yields no return.
        if len(self._closes) < 2:
            return dict.fromkeys(self.feature_keys)

        c0 = self._closes[-1]
        c_prev = self._closes[-2]

        out: dict[str, float | None] = {}
        if c_prev > 0:
            out["ret_log_1"] = math.log(c0 / c_prev)
            out["ret_simple_1"] = (c0 / c_prev) - 1.0
        else:
            # Non-positive previous close (corporate action artefact, junk
            # tick) — emit None rather than divide-by-zero or take log(0).
            out["ret_log_1"] = None
            out["ret_simple_1"] = None

        for k in self._momentum_lookbacks:
            # Need k+1 bars total: current + k bars in the past.
            if len(self._closes) > k:
                ck = self._closes[-(k + 1)]
                out[f"mom_{k}"] = (c0 / ck) - 1.0 if ck > 0 else None
            else:
                out[f"mom_{k}"] = None
        return out
