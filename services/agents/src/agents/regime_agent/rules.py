"""
agents.regime_agent.rules - rule-based regime classifier.

A *deliberately simple* deterministic classifier that maps a small
panel of FRED series to one of four regimes::

  - "risk_off"  Vol spike or yield curve inverted -> bearish for risk assets
  - "high_vol"  Elevated VIX without curve inversion -> de-risk a bit
  - "risk_on"   Calm vol + healthy curve -> mildly bullish for risk assets
  - "neutral"   Anything in between

This is intentionally NOT an ML classifier.  Macro regime is a noisy
domain where a 4-state hand-crafted rule wins on interpretability and
trust - operators can debug "why is regime risk_off?" with a glance,
which matters more than squeezing out a few bps of edge.

Confidence is derived from how far each input is from the threshold.
That way "VIX = 35" produces a more confident risk_off than "VIX = 30.5".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# Series IDs (FRED canonical).
SERIES_VIX = "VIXCLS"  # CBOE Volatility Index, daily close.
SERIES_T10Y2Y = "T10Y2Y"  # 10y minus 2y treasury spread.
SERIES_DFF = "DFF"  # Effective federal funds rate (daily).

# Thresholds.  Calibrated against the post-2010 US distribution; values
# above the median by ~1 stdev start tilting risk-off.  Tunable at the
# top of this file so an operator can sharpen or relax the bands without
# hunting through code.
VIX_RISK_OFF = 30.0
VIX_HIGH_VOL = 22.0
VIX_RISK_ON = 15.0
T10Y2Y_INVERTED = 0.0  # Negative spread = inversion.
T10Y2Y_HEALTHY = 0.30


Regime = Literal["risk_on", "risk_off", "high_vol", "neutral"]


@dataclass(frozen=True)
class RegimeView:
    """Classifier output: regime label + confidence + the inputs used."""

    regime: Regime
    confidence: float
    vix: float | None
    yield_spread: float | None
    fed_funds: float | None
    rationale: str


def classify(
    *,
    vix: float | None,
    yield_spread: float | None,
    fed_funds: float | None = None,
) -> RegimeView:
    """Classify the current macro regime.

    Decision tree (first match wins):

      1. VIX > VIX_RISK_OFF  OR  yield curve inverted -> risk_off
      2. VIX > VIX_HIGH_VOL                          -> high_vol
      3. VIX < VIX_RISK_ON  AND  spread > healthy    -> risk_on
      4. otherwise                                   -> neutral

    Missing inputs degrade confidence but never silence the signal -
    a stale series still produces a "neutral" with low confidence so
    the dashboard / consensus knows the agent is alive.
    """
    notes: list[str] = []

    if vix is None and yield_spread is None:
        return RegimeView(
            regime="neutral",
            confidence=0.0,
            vix=vix,
            yield_spread=yield_spread,
            fed_funds=fed_funds,
            rationale="all macro inputs unavailable",
        )

    inversion = yield_spread is not None and yield_spread < T10Y2Y_INVERTED
    if inversion:
        notes.append(f"yield curve inverted ({yield_spread:.2f})")
    if vix is not None and vix > VIX_RISK_OFF:
        notes.append(f"VIX > {VIX_RISK_OFF} (={vix:.1f})")
    if vix is not None and vix > VIX_HIGH_VOL:
        notes.append(f"VIX elevated ({vix:.1f})")
    if vix is not None and vix < VIX_RISK_ON:
        notes.append(f"VIX low ({vix:.1f})")
    if yield_spread is not None and yield_spread > T10Y2Y_HEALTHY:
        notes.append(f"yield spread healthy ({yield_spread:.2f})")

    # 1. risk_off: VIX panic OR inverted curve.
    if (vix is not None and vix > VIX_RISK_OFF) or inversion:
        # Scale confidence: more extreme = more confident.  Use the
        # max of "VIX overshoot" and "inversion magnitude" as the
        # signal strength.
        vix_overshoot = (
            (vix - VIX_RISK_OFF) / max(VIX_RISK_OFF, 1.0)
            if vix is not None and vix > VIX_RISK_OFF
            else 0.0
        )
        inv_strength = (
            -yield_spread
            if yield_spread is not None and yield_spread < 0
            else 0.0
        )
        strength = max(vix_overshoot, inv_strength)
        confidence = max(0.4, min(1.0, 0.6 + strength))
        return RegimeView(
            regime="risk_off",
            confidence=confidence,
            vix=vix,
            yield_spread=yield_spread,
            fed_funds=fed_funds,
            rationale="; ".join(notes) or "risk_off triggers fired",
        )

    # 2. high_vol: VIX elevated but not panicking.
    if vix is not None and vix > VIX_HIGH_VOL:
        # Confidence proportional to how deep into the high_vol band we are.
        depth = (vix - VIX_HIGH_VOL) / (VIX_RISK_OFF - VIX_HIGH_VOL)
        confidence = max(0.3, min(0.7, 0.3 + 0.4 * depth))
        return RegimeView(
            regime="high_vol",
            confidence=confidence,
            vix=vix,
            yield_spread=yield_spread,
            fed_funds=fed_funds,
            rationale="; ".join(notes),
        )

    # 3. risk_on: low VIX + healthy curve.
    if (
        vix is not None
        and vix < VIX_RISK_ON
        and yield_spread is not None
        and yield_spread > T10Y2Y_HEALTHY
    ):
        # Confidence rises with VIX-below-threshold and spread-above-threshold.
        vix_below = (VIX_RISK_ON - vix) / VIX_RISK_ON
        spread_above = min((yield_spread - T10Y2Y_HEALTHY) / 1.0, 1.0)
        strength = (vix_below + spread_above) / 2.0
        confidence = max(0.3, min(0.85, 0.4 + 0.45 * strength))
        return RegimeView(
            regime="risk_on",
            confidence=confidence,
            vix=vix,
            yield_spread=yield_spread,
            fed_funds=fed_funds,
            rationale="; ".join(notes),
        )

    # 4. neutral fallback.
    return RegimeView(
        regime="neutral",
        confidence=0.2,
        vix=vix,
        yield_spread=yield_spread,
        fed_funds=fed_funds,
        rationale="; ".join(notes) or "no triggers",
    )


# How regime maps to a market-wide directional bias for risk assets.
# Used by the orchestrator's regime adapter; kept here so all calibration
# numbers live in one file.
REGIME_DIRECTION: dict[str, float] = {
    "risk_on": 0.20,
    "neutral": 0.0,
    "high_vol": -0.10,
    "risk_off": -0.30,
}
