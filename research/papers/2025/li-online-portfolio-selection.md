---
title: "Online Portfolio Selection: A Survey"
authors: ["Li, Bin", "Hoi, Steven C.H."]
affiliation: "SMU (Singapore Management University)"
source: "arXiv:1405.0915"
date: "2014-05-05"
added: "2026-06-22"
last_reviewed: "2026-06-22"
status: "verified"
relevance: "high"
tier_mapping: ["Q2.1", "Q2.4"]
tags: ["online-portfolio-selection", "OLPS", "passive-aggressive", "confidence-weighted", "mean-reversion"]
license: "Unknown"
effort_to_apply: "M"
adoption_risk: "low"
---

## TL;DR
A canonical survey of the online portfolio selection (OLPS) literature, which addresses the problem of sequentially allocating capital to a set of assets to maximize cumulative wealth. The survey covers the major algorithm families: follow-the-winner (Buy-and-Hold, Constant Rebalanced Portfolios), follow-the-loser (Mean Reversion, Passive Aggressive Mean Reversion), pattern-matching (Correlation-driven, Non-parametric), and meta-learning (Aggregating Algorithm, Fast Universalization). The empirical comparison: Follow-the-Loser algorithms dominate Follow-the-Winner in the 2000-2010 US equity backtests.

## Why we care
Sisyphus Tier Q2.1 calls for cross-sectional ranking. The OLPS literature is the academic foundation for cross-sectional rebalancing. Algorithms like Passive Aggressive Mean Reversion (PAMR) and Online Moving Average Reversion (OLMAR) are the precursors to the modern LSTM-based cross-section (Lim et al. 2019, already in the database). The OLPS reference gives the classical baselines; the LSTM is the deep-learning upgrade.

## Key ideas
- The OLPS framework: at each round t, observe prices, choose a portfolio `b_t ∈ Δ_n` (the simplex), realize return `b_t · x_t`, repeat.
- Follow-the-Winner algorithms increase allocation to recent winners.
- Follow-the-Loser algorithms (PAMR, OLMAR) bet on mean reversion: increase allocation to recent losers. Theoretically and empirically dominant in the 2000-2010 data.
- Pattern-matching algorithms (Non-parametric) find similar historical windows and trade accordingly.
- Meta-learning algorithms combine multiple OLPS algorithms with adaptive weights.

## How to apply to Fincept
1. Implement PAMR and OLMAR as baselines in `services/orchestrator/src/orchestrator/cross_section.py` alongside the LSTM (Lim et al.).
2. The backtester should have a `run_olps_baselines(algorithm, universe, start, end)` function to evaluate OLPS performance.
3. The candidate-gate policy should require: the LSTM cross-section must beat the OLPS baseline by ≥ 10% on Sharpe over a 1-year backtest before promotion.

## Caveats
- OLPS algorithms were developed for daily data; for 1-minute crypto data, transaction costs dominate. The paper's results may not transfer.
- The OLPS framework is a one-period problem; the Fincept OMS is multi-period. A naive port loses information.
- The crypto market may have different mean-reversion properties than US equity.

## Related entries
- `research/papers/2026/deep-momentum-lim.md` (LSTM cross-section is the deep-learning upgrade)
- `research/papers/2025/jegadeesh-titman-canonical.md` (cross-sectional momentum in equity)
- `research/papers/2025/qlib-architecture.md` (Qlib's `TopkDropoutStrategy` is an OLPS-style approach)

## References
- Li & Hoi, *Online Portfolio Selection: A Survey* (2014, CSUR)
- Li, Hoi, Gopalkrishnan, *PAMR: Passive Aggressive Mean Reversion Strategy* (2012, IJDATS)
- Li, Hoi, Zhao, *OLMAR: Online Moving Average Reversion* (2015, ICONIP)
```

---

## 4. Phase 2 of the expansion — Tier Q3 cutting edge (6 entries)
