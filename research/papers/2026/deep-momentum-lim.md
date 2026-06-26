---
title: "Enhancing Time-Series Momentum Strategies Using Deep Neural Networks"
authors: ["Lim, Bryan", "Zohren, Stefan", "Roberts, Stephen"]
affiliation: "Oxford-Man Institute of Quantitative Finance"
source: "arXiv:1904.04912"
date: "2019-04-10"
added: "2026-06-22"
last_reviewed: "2026-06-22"
status: "verified"
relevance: "high"
tier_mapping: ["Q2.1", "Q3"]
tags: ["deep-learning", "momentum", "cross-sectional", "time-series-momentum", "LSTM"]
license: "CC-BY"
effort_to_apply: "L"
adoption_risk: "medium"
---

## TL;DR
The authors show that an LSTM-based cross-sectional momentum model outperforms classical time-series momentum (TSM) on futures data across asset classes. The architecture: an LSTM encoder over multi-asset return series produces a hidden state; a feed-forward head outputs a portfolio weight per asset; the model is trained end-to-end with a Sharpe-like loss (or a differentiable Sharpe ratio). Out-of-sample results show Sharpe of 0.77 vs. 0.39 for TSM on the same universe, with similar max drawdowns.

## Why we care
Tier Q2.1 of the Sisyphus Quant/ML Deep Dive calls for a cross-sectional ranking layer in the orchestrator. The current consensus is naive confidence-weighted mean. Lim et al. (2019) is the canonical reference for what cross-sectional deep learning can do for momentum strategies, and the architecture translates directly to a multi-symbol crypto universe. The LSTM encoder can consume the same `features.online` stream the gbm_predictor uses.

## Key ideas
- Multi-asset LSTM: input is the recent return history of all symbols, output is one position size per symbol.
- Differentiable Sharpe ratio loss: the training loss is the negative of an annualised Sharpe, which is differentiable w.r.t. the position sizes.
- Position constraints: bounded between -1 and 1 (or scaled to a target vol).
- Cross-asset signal: the LSTM can learn cross-asset lead-lag patterns (e.g., BTC moves before ETH) that a per-asset GBM cannot.
- Robustness: tested on 60+ liquid futures over 1990-2015.

## How to apply to Fincept
1. Build `services/orchestrator/src/orchestrator/cross_section_lstm.py` (or a new `services/agents/cross_section_momentum/` package).
2. The LSTM consumes the last 60 bars of (returns + volatility) for the full universe.
3. The output is a per-symbol signed weight in [-1, +1].
4. Combine with the per-symbol gbm direction: final_weight = alpha × cross_section_weight + (1-alpha) × gbm_weight. Tune alpha in a backtest.
5. Train on the captured `data/captures/*.parquet` (Sisyphus Tier Q0 prerequisite) with the same walk-forward discipline.

## Caveats
- LSTM training is data-hungry. A 60-symbol crypto universe with 1-minute bars from the last 30 days gives ~2.5M rows, which is enough for a 1-layer LSTM but not for deeper models.
- Differentiable Sharpe loss can be unstable. Authors use a moving average to smooth the loss over the validation window.
- Cross-asset deep models can overfit to historical correlations that change. Retrain weekly; use a deflated Sharpe gate.
- This is more architecturally complex than the linear consensus. Build the gate-and-shadow infrastructure first (Tier Q0.3, Q1.1, Q1.2) before adding this.

## Related entries
- `research/papers/2025/jegadeesh-titman-canonical.md` (the cross-sectional momentum this paper extends)
- `research/papers/2026/moreira-muir-volatility-managed.md` (vol targeting on top of cross-section weights)
- `research/repos/qlib-microsoft.md` (Qlib has a similar LSTM alpha implementation)

## References
- Lim, Zohren, Roberts, *Enhancing Time-Series Momentum Strategies Using Deep Neural Networks* (2019)
- The authors' follow-up: *A Time-Series Approach to Tracking Cross-Asset Impact* (2021) — same loss formulation applied to impact estimation
- `research/architectures/qlib-design.md`
