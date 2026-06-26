---
title: "Qlib Platform Architecture — Reference Design"
authors: ["Microsoft Research Asia"]
affiliation: "Microsoft"
source: "https://github.com/microsoft/qlib/tree/main/qlib/contrib"
date: "2020-09-22"
added: "2026-06-22"
last_reviewed: "2026-06-22"
status: "verified"
relevance: "high"
tier_mapping: ["Q1.1", "Q2.1", "Q3"]
tags: ["architecture", "alpha-research", "dataflow", "rolling-backtest"]
license: "MIT"
effort_to_apply: "L"
adoption_risk: "low"
---

## TL;DR
The Qlib platform's architecture is the de facto reference for an AI quant platform. The shape: data handlers → feature engineering → alpha research → portfolio construction → backtest, where each stage is a Python object with `fit()` and `predict()` and the platform orchestrates the rolling train/predict over the time series. The `qlib.contrib` directory contains the most useful reference implementations: the alpha evaluator (IC/RankIC/Dsp), the portfolio handlers (TopkDropoutStrategy, EnhancedIndexingStrategy), and the rolling cross-validation harness.

## Why we care
Several Fincept Tier Q2 tasks have direct Qlib analogs. Reading the Qlib source for the alpha evaluator and the portfolio handlers is the fastest way to see what "good" looks like. The architecture is a reference, not a target for adoption — Fincept is event-sourced; Qlib is dataflow-based — but the patterns transfer.

## Key ideas
- Dataflow architecture: each stage is a Python object with a uniform `fit()` / `predict()` API. The platform orchestrates the rolling train/predict.
- Information Coefficient (IC) and RankIC: per-bar correlation between predicted and actual returns. These are the canonical alpha evaluation metrics in Qlib.
- Dsp: deflated Sharpe p-value. The `qlib.contrib.evaluate` module has a direct port of the Bailey & López de Prado paper.
- Portfolio handlers: `TopkDropoutStrategy` (rank by signal, long top N, short bottom N, with turnover control), `EnhancedIndexingStrategy` (track a benchmark with alpha overlay).
- Rolling cross-validation: the platform's `qlib.model.train_model` and `qlib.model.pred_score` methods handle the rolling train/predict automatically.

## How to apply to Fincept
1. Borrow the alpha evaluator pattern: add `services/agents/<agent>/evaluate.py` with IC, RankIC, and Dsp metrics. Store alongside the model dossier.
2. Borrow the `TopkDropoutStrategy` pattern for the Tier Q2.1 cross-section ranking task.
3. The portfolio-handler design is a reference for Tier Q2.2 vol targeting — a vol-targeting handler would be `class VolTargetedHandler` with `fit()` and `predict()` returning target weights.
4. The rolling cross-validation harness is a reference for the Sisyphus Tier Q0.2 walk-forward trainer.

## Caveats
- Qlib's dataflow architecture is batch-oriented. Fincept's event-sourced architecture is real-time. The patterns transfer; the implementations do not.
- Qlib's `Dsp` is a port of the Bailey paper. Use it; don't reimplement.
- The platform does not have a real-time OMS. The portfolio handlers produce *target* portfolios, not orders. Fincept's OMS is the missing layer.

## Related entries
- `research/papers/2025/qlib-architecture.md`
- `research/papers/2026/lopez-de-prado-deflated-sharpe.md`
- `research/papers/2026/moreira-muir-volatility-managed.md`
- `research/papers/2026/deep-momentum-lim.md`

## References
- https://github.com/microsoft/qlib/tree/main/qlib/contrib
- https://qlib.readthedocs.io/en/latest/component/strategy.html
