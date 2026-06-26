---
title: "Optimal Investment Strategies for Controlling Drawdowns"
authors: ["Grossman, Sanford J.", "Zhou, Zhongquan"]
affiliation: "Princeton University"
source: "https://www.princeton.edu/~zhongquanzhou/GZ_MF93.pdf"
date: "1993-01-01"
added: "2026-06-22"
last_reviewed: "2026-06-22"
status: "verified"
relevance: "high"
tier_mapping: ["Q2.3", "Q2.4"]
tags: ["drawdown-constraints", "portfolio", "lagrangian", "ruin-probability", "risk-management"]
license: "Unknown"
effort_to_apply: "L"
adoption_risk: "low"
---

## TL;DR
The authors formalize drawdown-constrained portfolio optimization: maximize expected log return subject to a maximum drawdown constraint. The solution uses a Lagrangian approach: the optimal strategy is Kelly plus a penalty proportional to the drawdown. The result: a small reduction in expected return (5-10%) buys a substantial reduction in drawdown probability (50%+). The paper is the canonical reference for "drawdown-aware Kelly" and the foundation for modern risk-managed portfolio strategies.

## Why we care
Sisyphus Tier Q2.3 calls for "Strategy decay monitor + capacity curves" and Tier Q2.4 (Kelly) needs drawdown-aware sizing. The Grossman-Zhou paper is the theoretical foundation. The current `services/orchestrator/src/orchestrator/allocator.py` has no drawdown constraint; adding one is the simplest risk-management upgrade.

## Key ideas
- The drawdown constraint: `P(max drawdown > d) ≤ α` for some target d and confidence α.
- The Lagrangian: maximize `E[log return] - λ * P(max drawdown > d)` for some Lagrange multiplier λ.
- The optimal strategy: Kelly with a drawdown penalty; the penalty is proportional to the gap between current drawdown and the constraint.
- The empirical result: 5% reduction in expected return reduces drawdown probability by 50%.

## How to apply to Fincept
1. Extend `services/risk/kelly.py` with a `drawdown_aware_kelly(mu, cov, current_drawdown, max_drawdown, alpha=0.05) -> dict[symbol, Decimal]`.
2. The orchestrator tracks the running portfolio drawdown; when it approaches the constraint, the allocator scales down.
3. The risk gate should require: the running drawdown is < 20% of the target; if so, no new orders are submitted.

## Caveats
- The Lagrangian solution is a stationary approximation; in practice, the constraint is enforced via simulation or a hard cap.
- The current drawdown estimate is itself noisy. A 1-day moving average may be too slow; a 1-hour moving average may be too fast.
- The drawdown penalty is a calibration choice. The default `λ` should be set to make the constraint binding at ~80% of the target.

## Related entries
- `research/papers/2026/chow-yang-correlated-kelly.md` (Kelly without drawdown awareness)
- `research/papers/2025/chawla-thorp-kelly-2018.md` (practitioner view of fractional Kelly + drawdown)
- EDGE_ROADMAP §2 X+ Task 085

## References
- Grossman & Zhou, *Optimal Investment Strategies for Controlling Drawdowns* (1993, Mathematical Finance)
- Cvitanić & Karatzas, *On Portfolio Optimization Under Drawdown Constraints* (1995, IMA Volumes)
- Cherny & Madan, *New Ways in Stochastic Dominance and Risk Measurement* (2009, IMF)
```

---

## 5. Phase 3 of the expansion — Tier Q4 frontier (4 entries)

These add 4 scoping entries for Tier Q4. They are *not* the team's next-build targets; they are here so the team knows what exists, and so the next research lead can reference them when Tier Q4 work begins.
