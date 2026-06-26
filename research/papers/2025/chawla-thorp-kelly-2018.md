---
title: "The Kelly Criterion and the Stock Market: A Review"
authors: ["Chawla, Dhruv", "Thorp, Edward O."]
affiliation: "UC Irvine (Thorp) / independent"
source: "https://www.leanwork.com/kc-review.pdf"
date: "2018-03-01"
added: "2026-06-22"
last_reviewed: "2026-06-22"
status: "verified"
relevance: "high"
tier_mapping: ["Q2.4", "Q1.3"]
tags: ["kelly", "portfolio-sizing", "drawdown", "fractional-kelly", "fortune-formula"]
license: "Unknown"
effort_to_apply: "M"
adoption_risk: "low"
---

## TL;DR
A modern, practitioner-focused review of the Kelly criterion applied to equity portfolios. The authors cover single-asset Kelly, multi-asset (correlated) Kelly, fractional Kelly, and the relationship between Kelly and drawdown. They discuss the empirical performance of Kelly strategies on US equity and the practical considerations: estimation error, parameter drift, and rebalancing frequency. The 100-page treatment is the most thorough practitioner reference as of 2024.

## Why we care
Sisyphus Tier Q2.4 calls for Kelly-optimal sizing. The Chow-Yang reference (Tier Q2.4 already in the database) gives a 2-page derivation. The Chawla-Thorp review gives the *practitioner* view: how to estimate covariance, what fraction to use, how to handle regime change, and how to avoid the catastrophic drawdowns of full Kelly. The current `services/orchestrator/src/orchestrator/allocator.py` is linear; this reference is the full Kelly implementation guide.

## Key ideas
- Fractional Kelly: `f_actual = Î³ Ã— f*` for `Î³ âˆˆ (0, 0.5]`. Half-Kelly is the standard compromise.
- Covariance estimation: exponential weighting with a 30-60 day half-life; shrink toward a constant-correlation prior to reduce overfit.
- Drawdown constraint: Kelly's expected drawdown is ~36% of capital at half-Kelly; full Kelly can hit 50%+ drawdowns. Use a drawdown-aware Kelly variant.
- The "fortune formula": the geometric growth rate is maximized by Kelly, but the *expected utility* may not be (a function of the utility function's risk aversion).
- For correlated assets, the "diversification-adjusted" Kelly fraction can be substantially smaller than the single-asset Kelly.

## How to apply to Fincept
1. Implement `services/risk/kelly.py::correlated_kelly_weights(mu, cov, fractional=0.25, max_drawdown=0.20, settings)` per the Chow-Yang reference. Add the Chawla-Thorp drawdown-aware variant.
2. The orchestrator's `allocator.target_notional` should call this with the consensus output, a recent covariance matrix (60-day rolling, exponentially weighted), and `fractional=0.25` (quarter-Kelly) for the production default.
3. Add a `MAX_DRAWDOWN` setting that caps the cumulative notional based on the running portfolio P&L.
4. The candidate-gate policy should require that any new alpha source has a paper-spine replay with a max drawdown < 20% over 4 weeks of synthetic data.

## Caveats
- Kelly assumes the return distribution is stationary. In practice, both μ and Σ drift. The Chawla-Thorp review recommends re-estimating daily.
- The drawdown-aware Kelly variant is not a closed form; it's a simulation-based optimization. Implementation cost: ~1 week.
- The fortune formula assumes log utility, which is a strong assumption. For a quant firm with a target drawdown constraint, the formula's optimum is not the same as Kelly's.

## Related entries
- `research/papers/2026/chow-yang-correlated-kelly.md` (2-page derivation)
- `research/papers/2026/lopez-de-prado-deflated-sharpe.md` (statistical significance)
- `research/papers/2026/moreira-muir-volatility-managed.md` (vol targeting as a Kelly complement)

## References
- Chawla & Thorp, *The Kelly Criterion and the Stock Market* (2018, Lean Work)
- Thorp, *The Kelly Criterion in Betting, Stock Markets, and Casinos* (2008)
- Luenberger, *Investment Science* (2013), Ch. 6
