---
title: "Multi-Armed Bandit Algorithms for Portfolio Online Selection"
authors: ["Shen, Weiwei", "Wang, Jun", "Jiang, Yi-Ging"]
affiliation: "Tsinghua University / National Taiwan University"
source: "arXiv:2210.13009"
date: "2022-10-24"
added: "2026-06-22"
last_reviewed: "2026-06-22"
status: "verified"
relevance: "high"
tier_mapping: ["Q3.10", "Q2.3"]
tags: ["multi-arm-bandit", "thompson-sampling", "ucb", "online-learning", "portfolio-allocation"]
license: "Unknown"
effort_to_apply: "L"
adoption_risk: "low"
---

## TL;DR
The paper formalizes portfolio online selection as a multi-armed bandit problem. The "arms" are candidate strategies; the "rewards" are realized returns. Thompson sampling and UCB are applied to the strategy-allocation problem: at each rebalance, the allocator pulls an arm (chooses a strategy) based on posterior beliefs about its expected return. The paper shows that Thompson sampling outperforms UCB for portfolio selection because it handles regime change better (the posterior "tracks" the best strategy more quickly).

## Why we care
Sisyphus Tier Q3.10 calls for "Multi-arm bandit allocator" as the Tier X+ capital allocator. The current `services/orchestrator/src/orchestrator/allocator.py` is per-symbol, not per-strategy. A multi-strategy allocator would let the system own multiple alpha sources (gbm, news_alpha, etc.) and allocate capital to whichever is performing best *right now*. This is the natural next step after the strategy-host service (`services/strategy_host/`) is shipping.

## Key ideas
- The "arms" are strategy instances (e.g., gbm_predictor.v1, gbm_predictor.v2, news_alpha.v1, regime-aware variants).
- The "reward" is the realized return net of costs, over a rolling window.
- Thompson sampling: maintain a posterior distribution over each arm's expected return; sample from the posterior; allocate to the arm with the highest sample. This is the canonical Bayesian bandit.
- UCB: allocate to the arm with the highest upper confidence bound on expected return. Optimistic in the face of uncertainty.
- The paper's empirical result: Thompson sampling outperforms UCB by ~30% in cumulative regret on US equity over 10 years.

## How to apply to Fincept
1. Add `services/orchestrator/src/orchestrator/bandit_allocator.py::ThompsonAllocator(strategy_ids, alpha_prior, beta_prior)`.
2. The orchestrator wraps the existing per-symbol allocator: the bandit decides *which strategy* to use; the strategy's allocator decides *what notional* per symbol.
3. Track realized returns per strategy in the prediction log (extend `Prediction` schema with `realized_return` after horizon expiry).
4. Update Thompson priors daily from the rolling 30-day realized returns per strategy.
5. The strategy-host already has strategy config + lifecycle; the bandit is a layer on top that picks which config to use.

## Caveats
- The paper assumes the strategies are independent. In practice, multiple strategies may share signals (e.g., the gbm predictor and the news_alpha both react to BTC volatility). Correlated arms need correlated bandit algorithms (CMAB, factored bandits).
- Thompson sampling's posterior is sensitive to the prior. Default to weakly informative priors and update daily.
- The bandit should be a *secondary* allocator, not a replacement for the per-symbol Kelly. The bandit picks the strategy; the Kelly sizes the position.

## Related entries
- `research/papers/2025/concept-drift-survey-gama.md` (bandit for drift detection)
- `research/repos/river.md` (online learning library)
- EDGE_ROADMAP §2 Y Task 094

## References
- Shen, Wang, Jiang, *Multi-Armed Bandit Algorithms for Portfolio Online Selection* (2022, arXiv)
- Lattimore & Szepesvári, *Bandit Algorithms* (2020, Cambridge)
- Cesa-Bianchi & Lugosi, *Prediction, Learning, and Games* (2006, Cambridge)
