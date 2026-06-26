---
title: "The Deflated Sharpe Ratio: Adjusting for Non-Normal Returns, Selection Bias, and Multiple Testing"
authors: ["Bailey, David H.", "Borwein, Jonathan", "López de Prado, Marcos"]
affiliation: "Lawrence Berkeley National Lab / Dalhousie Univ. / ADIA Lab"
source: "https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf"
date: "2014-12-01"
added: "2026-06-22"
last_reviewed: "2026-06-22"
status: "verified"
relevance: "high"
tier_mapping: ["Q0.3", "Q1.1", "Q2.4"]
tags: ["statistical-significance", "backtest", "sharpe-ratio", "multiple-testing", "calibration"]
license: "CC-BY"
effort_to_apply: "S"
adoption_risk: "low"
---

## TL;DR
The Deflated Sharpe Ratio (DSR) extends the standard Sharpe ratio by correcting for three biases common in quantitative finance: non-normal returns, the number of trials (strategy variations) tried, and the length of the backtest. It produces a p-value for whether the observed Sharpe exceeds what would be expected by chance under the null hypothesis of random selection. It is a foundational reference for any system that backtests many strategies and promotes one based on Sharpe.

## Why we care
The current `gbm_predictor/train.py` has no statistical-significance test for a candidate's `best_auc`. `news_alpha_predictor/evaluate.py::CandidateGatePolicy` checks `best_auc >= 0.52` and `best_auc_delta >= 0.0` — both are arbitrary thresholds. Replacing these with a DSR-style test gives the operator a defensible p-value and a more honest "is this model actually better than chance?" answer. The Sisyphus Quant/ML Deep Dive flagged the asymmetry between news_alpha (gated) and gbm (ungated) as Tier Q0.3.

## Key ideas
- Standard Sharpe overstates true performance when returns are non-normal (heavy tails, skew).
- Multiple-testing bias: if you try 100 strategies, the best one will have a high Sharpe by chance alone. The DSR adjusts by estimating the expected maximum Sharpe under the null and the variance of the maximum.
- The deflated Sharpe gives a p-value for the null hypothesis that the true Sharpe is zero. A p-value < 0.05 means the observed Sharpe is statistically significant given the number of trials and the return distribution.
- Formula: DSR ≈ (observed_SR - E[max_SR_under_null]) / std(max_SR_under_null) × z-factor for non-normality.

## How to apply to Fincept
1. Add a `min_p_value` field to `CandidateGatePolicy` (default 0.05).
2. In `services/agents/gbm_predictor/train.py::main`, after the walk-forward summary, compute DSR using the per-fold returns and the number of trials (variants of the same model, e.g., different feature sets, different horizons). Store in `meta.json` as `dsr_p_value`.
3. In `services/agents/news_alpha_predictor/evaluate.py::evaluate_candidate`, add the same DSR check.
4. The promotion endpoint should require `dsr_p_value < 0.05` *or* an explicit operator override logged in promotion history.

## Caveats
- DSR assumes the trials are independent. Correlated trials (e.g., 10 GBM variants with overlapping features) reduce the effective trial count. We should estimate effective trials, not raw trials.
- DSR requires at least 30 return observations to be stable. For very short backtests, use the haircut factor in Bailey & López de Prado's follow-up papers.
- DSR does not address capacity, decay, or regime change. It is a statistical-significance test, not a model-quality test.

## Related entries
- `research/papers/2024/platt-scaling.md` (calibration for the same gate)
- `research/papers/2026/moreira-muir-volatility-managed.md` (vol-targeting shares the multiple-testing concern)
- `research/architectures/qlib-design.md` (Qlib uses DSR as part of its alpha evaluator)

## References
- https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf
- Bailey & López de Prado, *The Sharpe Ratio Efficient Frontier*, Journal of Portfolio Management (2014)
- López de Prado, *Advances in Financial Machine Learning* (2018), Ch. 13
