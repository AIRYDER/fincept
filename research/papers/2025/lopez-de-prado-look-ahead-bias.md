---
title: "The Probability of Backtest Overfitting (and the Deflated Sharpe Ratio)"
authors: ["Bailey, David H.", "LÃ³pez de Prado, Marcos"]
affiliation: "Lawrence Berkeley National Lab / ADIA Lab"
source: "https://www.davidhbailey.com/dhbpapers/backtest-probability.pdf"
date: "2014-12-01"
added: "2026-06-22"
last_reviewed: "2026-06-22"
status: "verified"
relevance: "high"
tier_mapping: ["Q0.1", "Q0.3", "Q1.1"]
tags: ["look-ahead-bias", "backtest", "overfitting", "deflated-sharpe", "validation"]
license: "CC-BY"
effort_to_apply: "S"
adoption_risk: "low"
---

## TL;DR
The companion paper to the Deflated Sharpe Ratio. Bailey & LÃ³pez de Prado (2014) formalize the probability that a backtested strategy is overfit to noise as a function of the number of trials tried, the number of backtest observations, and the observed Sharpe. They show that, in practice, "p-hacking" the backtest by trying many variants is the dominant source of false-positive Sharpe ratios. The paper also proposes a haircut on the Sharpe ratio to correct for overfit.

## Why we care
Sisyphus Tier Q0.1 calls for "Reconcile `gbm_predictor` features with live feature vocabulary" and "verify with look-ahead audit." The current `services/agents/gbm_predictor/train.py::build_dataset` uses `df.with_columns(forward.alias("__forward__")).drop_nulls(...)` to drop rows where the forward return is null. The forward return uses `pl.col(close_column).shift(-horizon_bars) / pl.col(close_column)`. This is *correct* for the label, but the *features* (`ret_1m`, `ret_5m`, etc.) are computed in the *live* feature service, where the FeatureComputer uses deques and may include look-back periods that don't perfectly align with the bar timestamps in the training parquet. A look-ahead audit (per the methodology in this paper) would catch any feature whose `ts_event` is from a bar *after* the label's `ts_event + horizon_bars`.

## Key ideas
- The "Probability of Backtest Overfitting" (PBO) is a function of trials (variations of the strategy) and the number of backtest bars.
- The haircut on Sharpe: `deflated_SR = observed_SR Ã— (1 - PBO)`.
- The methodology for detecting look-ahead bias: for each feature, audit its `ts_event` provenance. Any feature computed from a bar whose `ts_event` is after the label's `ts_event` is a look-ahead.
- A practical implementation: for each feature, compute `feature_at_t_minus_h` and check the correlation with `forward_return_h`; if the correlation drops sharply, the feature is probably a look-ahead.

## How to apply to Fincept
1. Add `services/agents/gbm_predictor/lookahead_audit.py` with a function `audit_features_for_lookahead(df: pl.DataFrame, feature_names: list[str], horizon_bars: int) -> list[LookaheadFinding]`.
2. The function checks each feature's value at `ts_event` vs `ts_event - horizon_bars * bar_seconds`. If the values are perfectly correlated, the feature is a look-ahead.
3. Run this audit on every `models/<agent>/meta.json` before promotion is allowed. The candidate-gate policy should add a `lookahead_audit: pass` requirement.
4. In the calibration dossier, include a "feature provenance" section: for each feature, when is it computed? Is it PIT-correct per the label?

## Caveats
- The PBO formula assumes trials are independent. Correlated trials (e.g., 10 GBM variants with overlapping features) reduce the effective trial count.
- The haircut is conservative; in practice, PBO underestimates the true overfit risk because trials share a common factor (the data distribution).
- The methodology is for backtested strategies, not for live models. The audit is a one-time check, not a continuous monitor.

## Related entries
- `research/papers/2026/lopez-de-prado-deflated-sharpe.md` (companion paper)
- `research/papers/2025/qlib-architecture.md` (Qlib uses DSR + PBO together)
- `research/repos/qlib-microsoft.md` (Qlib's `Dsp` class)

## References
- Bailey & LÃ³pez de Prado, *The Probability of Backtest Overfitting* (2014, Journal of Computational Finance)
- LÃ³pez de Prado, *Advances in Financial Machine Learning* (2018), Ch. 11
- LÃ³pez de Prado, *The 7 Reasons Most Machine Learning Funds Fail* (2018, SSRN)
