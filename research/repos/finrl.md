---
title: "FinRL — Deep Reinforcement Learning for Quantitative Finance"
authors: ["Yang, Xiao-Yang et al."]
affiliation: "Columbia University / AI4Finance Foundation"
source: "https://github.com/AI4Finance-Foundation/FinRL"
date: "2020-07-26"
added: "2026-06-22"
last_reviewed: "2026-06-22"
status: "verified"
relevance: "medium"
tier_mapping: ["Q3"]
tags: ["reinforcement-learning", "DQN", "PPO", "A2C", "trading-environments"]
license: "MIT"
effort_to_apply: "L"
adoption_risk: "medium"
---

## TL;DR
FinRL is a deep RL framework for quantitative finance. It provides trading environments (gym-style) wrapping common data sources, baseline agents (DQN, PPO, A2C, DDPG, SAC, TD3), and a series of increasingly ambitious "FinRL-Meta" agent designs (single-stock, multi-stock, portfolio allocation, cryptocurrency, high-frequency). The repo is well-documented with tutorials, has >10k stars, and is actively maintained.

## Why we care
EDGE_ROADMAP §3 explicitly forbids pure RL for portfolio allocation: "sample-inefficient and unstable." However, the research direction is important to understand because (a) `spec/BUILD_ORDER.md` Task 065 (RL execution agent) is still on the roadmap, (b) `services/agents/execution_rl/` is referenced in the spec, and (c) understanding what *doesn't* work in RL-for-finance is as important as understanding what does. The FinRL repo is the most thorough public reference for the state of the art in this corner of the literature.

## Key ideas
- Trading environments wrap data in a gym-like API: state = features, action = position size, reward = realized P&L.
- Baseline agents: DQN (discrete actions), PPO (continuous actions), A2C (advantage actor-critic), DDPG/SAC/TD3 (off-policy continuous).
- Common failure mode: PPO with a Sharpe-like reward overfits the training window. The Sharpe is non-stationary.
- The FinRL-Meta paper (`arXiv:2111.09337`) introduces a market simulator and benchmarks several agents. The conclusion: PPO and A2C beat DQN on Sharpe; none beat a simple momentum baseline on out-of-sample.
- Important lesson: RL is sample-hungry. A 1-minute crypto universe at 1 month is too small to train anything but the simplest agents.

## How to apply to Fincept
1. NOT recommended for portfolio allocation (per EDGE_ROADMAP). The system is too small to support RL training.
2. POSSIBLY useful for execution (TASK-065). An RL agent that takes (parent order, market state) → (child slice schedule) is a bounded problem with a clear reward. The FinRL execution-environment design is a starting point.
3. The repo's environment design (state space, reward shaping, transaction cost modeling) is informative regardless of adoption.

## Caveats
- FinRL's default reward is realized P&L, which is non-stationary and short-termist. Most academic RL-for-finance papers use a Sharpe-like reward that is *also* non-stationary but better.
- The repo's agents are not production-ready; they are research baselines. Tuning a PPO agent to a specific market is a multi-month effort.
- The repo's installation includes a heavy ML stack (PyTorch, stable-baselines3, gym). Adding it to a uv workspace would significantly increase the dependency footprint.
- The repo is research-focused, not production-focused. The data handlers, the agents, the backtester — none are designed for live trading.

## Related entries
- `research/papers/2026/moreira-muir-volatility-managed.md` (vol targeting is a non-RL alternative)
- `research/papers/2026/chow-yang-correlated-kelly.md` (Kelly is the analytical alternative to RL)
- EDGE_ROADMAP §2 Tier Y Task 094 (multi-arm bandit, a *bounded* RL approach)

## References
- https://github.com/AI4Finance-Foundation/FinRL
- Yang, Liu, Zhong, et al., *FinRL: A Deep Reinforcement Learning Library for Automated Stock Trading in Quantitative Finance* (2020, NeurIPS workshop)
- Yang, Zhang, Zhang, et al., *FinRL-Meta: A Universe of Near-Real-Market Environments for Advancing Reinforcement Learning in Quantitative Finance* (2021, NeurIPS workshop)
