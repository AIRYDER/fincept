---
title: "Correlated-Asset Kelly Criterion"
authors: ["Chow, K. C. Cliff", "Yang, Jingsan"]
affiliation: "Jane Street Capital"
source: "https://www.linkedin.com/pulse/correlated-asset-kelly-criterion-chow-yang/"
date: "2019-11-21"
added: "2026-06-22"
last_reviewed: "2026-06-22"
status: "verified"
relevance: "high"
tier_mapping: ["Q2.4"]
tags: ["kelly", "portfolio-sizing", "covariance", "fractional-kelly"]
license: "Unknown"
effort_to_apply: "M"
adoption_risk: "medium"
---

## TL;DR
A 2-page treatment of the Kelly criterion applied to a portfolio of correlated assets. The key result: with N correlated assets, the optimal Kelly fraction is `Σ⁻¹μ` where `Σ` is the covariance matrix and `μ` is the vector of expected excess returns. In practice, fractional Kelly (½ or ¼) is used to avoid the catastrophic drawdowns of full Kelly. The author's blog post gives the closed-form and discusses practical pitfalls.

## Why we care
Tier Q2.4 of the Sisyphus Quant/ML Deep Dive calls for Kelly-optimal sizing. The current `services/risk/kelly.py` is referenced in the spec (TASK-042) but not yet implemented. Correlated-asset Kelly is the correct formulation: the consensus emits per-symbol `(direction, confidence)`, the allocator must convert these into a portfolio of notionals that respects covariance. A 2-page reference is enough to design the implementation.

## Key ideas
- Single-asset Kelly: f* = (μ - r) / σ² where μ is expected return, σ² is variance, r is risk-free.
- Multi-asset Kelly: f* = Σ⁻¹(μ - r) where Σ is the covariance matrix, μ is the expected return vector, f* is the allocation vector.
- Fractional Kelly: f_actual = γ × f* for γ ∈ (0, 1]. Half-Kelly is the standard compromise between expected growth and drawdown risk.
- Inputs: rolling covariance estimate (e.g., 60-day window of returns), expected return vector (the consensus output), risk-free rate (default 0 for crypto).
- Pitfalls: the covariance estimate is itself noisy; using in-sample covariance over-allocates to historically-correlated assets; using exponential weighting helps.

## How to apply to Fincept
1. Implement `services/risk/kelly.py::correlated_kelly_weights(mu, cov, fractional=0.5, cap_per_symbol) -> dict[symbol, Decimal]`.
2. The orchestrator's `allocator.target_notional` should call this with the consensus output and a recent covariance matrix.
3. Use an exponentially-weighted covariance with a 30–60 day half-life.
4. Add a `KELLY_FRACTIONAL` setting (default 0.5, configurable per strategy).
5. The `cap_per_symbol` from the existing allocator is preserved as a hard ceiling.

## Caveats
- Kelly assumes a known, stationary return distribution. In practice, both μ and Σ are estimated and drift.
- Half-Kelly still produces a 1/3 chance of drawdown exceeding the *theoretical* loss. Use ¼-Kelly in production.
- The covariance matrix must be well-conditioned. If a symbol has very low recent variance, Kelly will allocate to it heavily; that's a bug, not a feature.
- This is one of the few research papers that gives a directly implementable formula in 2 pages. The implementation is short; the design discipline is the hard part.

## Related entries
- `research/papers/2026/moreira-muir-volatility-managed.md` (vol targeting + Kelly are complementary; vol target constrains the marginal)
- `research/architectures/qlib-design.md` (Qlib's enhanced portfolio handler)
- EDGE_ROADMAP §2 X+ Task 042

## References
- Chow & Yang, *Correlated-Asset Kelly Criterion* (Jane Street internal note, 2019; widely circulated)
- Thorp, *The Kelly Criterion in Betting* (2008, mostly mathematical foundations)
- Luenberger, *Investment Science* (2013), Ch. 6 — textbook Kelly treatment
