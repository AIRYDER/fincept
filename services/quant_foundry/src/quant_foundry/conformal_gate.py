"""
quant_foundry.conformal_gate — conformal prediction risk gate (TASK-1003).

Produces uncertainty intervals (q10/q50/q90) and abstains when the model
cannot make a reliable prediction. The uncertainty intervals can be fed
into the tournament and paper bridge.

Key invariants:
- **Produce uncertainty intervals (q10/q50/q90).** The calibrator fits on
  residuals (predictions - outcomes) and produces quantile intervals for
  new predictions.
- **Abstain when the model cannot make a reliable prediction.** The gate
  abstains when:
  - The interval is too wide (INTERVAL_TOO_WIDE).
  - There's insufficient calibration data (INSUFFICIENT_CALIBRATION_DATA).
  - The point estimate confidence is too low (LOW_CONFIDENCE).
- **Feed uncertainty into tournament and paper bridge.** The prediction
  includes the interval and is JSON-serializable for consumption by the
  tournament and paper bridge.

File-disjoint from my ``shadow_inference.py`` (read-only imports).
Does NOT modify it.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class ConformalGateConfig(BaseModel):
    """Configuration for the conformal gate.

    Frozen + extra='forbid'. Carries the maximum interval width, minimum
    calibration samples, and minimum confidence thresholds.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_interval_width: float = 0.5
    min_calibration_samples: int = 10
    min_confidence: float = 0.5


# ---------------------------------------------------------------------------
# Abstain reason
# ---------------------------------------------------------------------------


class AbstainReason(StrEnum):
    """Reason why the conformal gate abstained."""

    INTERVAL_TOO_WIDE = "interval_too_wide"
    INSUFFICIENT_CALIBRATION_DATA = "insufficient_calibration_data"
    LOW_CONFIDENCE = "low_confidence"


# ---------------------------------------------------------------------------
# Conformal interval
# ---------------------------------------------------------------------------


class ConformalInterval(BaseModel):
    """An uncertainty interval with q10/q50/q90 quantiles.

    Frozen + extra='forbid'. The width is q90 - q10.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    q10: float
    q50: float
    q90: float

    @property
    def width(self) -> float:
        """The interval width (q90 - q10)."""
        return self.q90 - self.q10


# ---------------------------------------------------------------------------
# Conformal prediction
# ---------------------------------------------------------------------------


class ConformalPrediction(BaseModel):
    """A conformal prediction with interval + abstain flag.

    Frozen + extra='forbid'. Carries the interval (if not abstaining),
    whether the gate abstained, and the abstain reason (if any).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    interval: ConformalInterval | None = None
    is_abstain: bool = False
    abstain_reason: AbstainReason | None = None

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable dict for tournament/paper bridge consumption."""
        return {
            "interval": (
                {
                    "q10": self.interval.q10,
                    "q50": self.interval.q50,
                    "q90": self.interval.q90,
                    "width": self.interval.width,
                }
                if self.interval
                else None
            ),
            "is_abstain": self.is_abstain,
            "abstain_reason": (
                self.abstain_reason.value if self.abstain_reason else None
            ),
        }


# ---------------------------------------------------------------------------
# Calibrator
# ===========================================================================


class ConformalCalibrator:
    """Conformal calibrator that builds intervals from residuals.

    Fits on residuals (predictions - outcomes) and produces quantile
    intervals for new predictions. Uses the empirical quantile method:
    the interval for a point estimate is [point + q10(residuals),
    point + q90(residuals)], with q50 = point + median(residuals).
    """

    def __init__(self) -> None:
        self._residuals: list[float] = []
        self._fitted = False

    @property
    def is_fitted(self) -> bool:
        """Return True if the calibrator has been fitted."""
        return self._fitted

    @property
    def n_samples(self) -> int:
        """Return the number of calibration samples."""
        return len(self._residuals)

    def fit(self, residuals: list[float]) -> None:
        """Fit the calibrator on residuals (predictions - outcomes)."""
        if len(residuals) < 2:
            self._fitted = False
            self._residuals = []
            return
        self._residuals = sorted(residuals)
        self._fitted = True

    def _quantile(self, q: float) -> float:
        """Compute the q-th quantile of the residuals (q in [0, 1])."""
        if not self._fitted or not self._residuals:
            raise RuntimeError("calibrator not fitted")
        n = len(self._residuals)
        # Linear interpolation between closest ranks.
        pos = q * (n - 1)
        lo = int(pos)
        hi = min(lo + 1, n - 1)
        frac = pos - lo
        return self._residuals[lo] * (1 - frac) + self._residuals[hi] * frac

    def predict_interval(self, point_estimate: float) -> ConformalInterval:
        """Produce a conformal interval for a point estimate.

        The interval is [point + q10(residuals), point + q90(residuals)],
        with q50 = point + median(residuals).
        """
        if not self._fitted:
            raise RuntimeError("calibrator not fitted")
        if self.n_samples < 2:
            raise ValueError(
                f"insufficient calibration data: {self.n_samples} samples "
                f"(need at least 2)"
            )
        q10_resid = self._quantile(0.10)
        q50_resid = self._quantile(0.50)
        q90_resid = self._quantile(0.90)
        return ConformalInterval(
            q10=point_estimate + q10_resid,
            q50=point_estimate + q50_resid,
            q90=point_estimate + q90_resid,
        )


# ---------------------------------------------------------------------------
# The gate
# ===========================================================================


class ConformalGate:
    """The conformal prediction risk gate.

    Wraps a ``ConformalCalibrator`` and applies abstain rules:
    1. Insufficient calibration data -> INSUFFICIENT_CALIBRATION_DATA.
    2. Interval too wide -> INTERVAL_TOO_WIDE.
    3. Low confidence -> LOW_CONFIDENCE.

    If all checks pass, returns a ``ConformalPrediction`` with the interval.
    """

    def __init__(
        self,
        calibrator: ConformalCalibrator,
        config: ConformalGateConfig | None = None,
    ) -> None:
        self.calibrator = calibrator
        self.config = config or ConformalGateConfig()

    def predict(
        self,
        point_estimate: float,
        confidence: float = 1.0,
    ) -> ConformalPrediction:
        """Produce a conformal prediction with abstain checks.

        Args:
        - ``point_estimate``: the point prediction.
        - ``confidence``: the model's confidence in the point estimate
          (0-1). If below ``min_confidence``, the gate abstains.

        Returns a ``ConformalPrediction`` with the interval, or an abstain
        decision if any check fails.
        """
        # 1. Check calibration data sufficiency.
        if (
            not self.calibrator.is_fitted
            or self.calibrator.n_samples < self.config.min_calibration_samples
        ):
            return ConformalPrediction(
                interval=None,
                is_abstain=True,
                abstain_reason=AbstainReason.INSUFFICIENT_CALIBRATION_DATA,
            )

        # 2. Check confidence.
        if confidence < self.config.min_confidence:
            return ConformalPrediction(
                interval=None,
                is_abstain=True,
                abstain_reason=AbstainReason.LOW_CONFIDENCE,
            )

        # 3. Produce the interval.
        interval = self.calibrator.predict_interval(point_estimate)

        # 4. Check interval width.
        if interval.width > self.config.max_interval_width:
            return ConformalPrediction(
                interval=None,
                is_abstain=True,
                abstain_reason=AbstainReason.INTERVAL_TOO_WIDE,
            )

        # 5. All checks passed.
        return ConformalPrediction(
            interval=interval,
            is_abstain=False,
        )


# ---------------------------------------------------------------------------
# Convenience function
# ---------------------------------------------------------------------------


def calibrate_and_predict(
    residuals: list[float],
    point_estimate: float,
    config: ConformalGateConfig | None = None,
    confidence: float = 1.0,
) -> ConformalPrediction:
    """Calibrate on residuals and produce a conformal prediction.

    Convenience entry point for TASK-1003. Creates a ``ConformalCalibrator``,
    fits it on the residuals, and runs the gate.
    """
    calibrator = ConformalCalibrator()
    calibrator.fit(residuals)
    gate = ConformalGate(calibrator=calibrator, config=config)
    return gate.predict(point_estimate=point_estimate, confidence=confidence)
