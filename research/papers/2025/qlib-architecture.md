---
title: "Qlib: An AI-oriented Quantitative Investment Platform"
authors: ["Yang, Xiao", "Liu, Weiqing", "Zhou, Dan", "Bian, Jiangwei", "Yin, Liyang"]
affiliation: "Microsoft Research Asia"
source: "arXiv:2009.11189"
date: "2020-09-22"
added: "2026-06-22"
last_reviewed: "2026-06-22"
status: "verified"
relevance: "high"
tier_mapping: ["Q1.1", "Q2.1", "Q3"]
tags: ["alpha-research", "platform", "architecture", "backtest", "feature-store", "deflated-sharpe"]
license: "MIT"
effort_to_apply: "L"
adoption_risk: "low"
---

## TL;DR
Qlib is Microsoft's open-source AI-oriented quantitative investment platform. The paper and the GitHub repo together describe an end-to-end pipeline: data handlers → feature engineering → alpha research (LightGBM, MLP, Transformer, etc.) → portfolio construction → backtest with realistic execution. The platform emphasizes alpha-factor construction and IC/RankIC evaluation, with first-class support for the DSR-style alpha evaluator and rolling cross-validation.

## Why we care
Qlib is the de facto open-source reference for an AI quant platform. It is *adjacent* to Fincept — same problems (alpha research, backtest, portfolio construction), different choices (Qlib is research-tool focused, Fincept is a running system). Reading the Qlib architecture and the corresponding `qlib/contrib` examples is the fastest way to see what "good" looks like for an alpha research stack. Several Fincept Tier Q2 tasks have direct Qlib analogs (cross-section ranking, alpha evaluator, portfolio handler with vol target).

## Key ideas
- Information Coefficient (IC) and Rank IC are the canonical alpha evaluation metrics in Qlib; they are the per-bar correlation between predicted and actual returns.
- DSR-style alpha evaluator: tests whether an alpha's IC is statistically significant after multiple-testing correction.
- Portfolio handlers: TopkDropoutStrategy, EnhancedIndexingStrategy, etc. Each takes an alpha signal and produces a portfolio, with built-in turnover and execution-cost models.
- The platform ships with `qlib.contrib.data` for common data sources (US equity, China A-share, crypto via custom adapters) and `qlib.contrib.model` for common model architectures.
- The architecture is fundamentally dataflow-based: each component is a Python object with a `fit()` and `predict()` method, and Qlib provides the orchestration (rolling train/predict over the time series).

## How to apply to Fincept
1. Borrow Qlib's alpha-evaluator pattern: add `services/agents/<agent>/evaluate.py::alpha_evaluator(predictions, ground_truth) -> {ic, rank_ic, dsp_pvalue}`.
2. Adopt the Information Coefficient as a per-cycle metric in the prediction log.
3. The cross-section ranking task (Tier Q2.1) can be modeled after Qlib's `TopkDropoutStrategy`.
4. The vol-targeting task (Tier Q2.2) can be modeled after Qlib's portfolio handlers.
5. NOT recommended: wholesale Qlib adoption. Qlib is a Python library for notebook-driven research; Fincept is a long-running event-sourced system. The two have different design centers.

## Caveats
- Qlib is data-source-coupled. The Chinese A-share data is licensed; using Qlib for crypto or US equity requires writing custom data handlers.
- The Qlib paper is 5+ years old; the repo has moved on. Read both.
- Qlib's portfolio handlers assume a *target* portfolio rebalanced at each cycle. Fincept's OMS submits *incremental* orders. The mapping is non-trivial.
- Qlib's `Dsp` (deflated Sharpe p-value) is a direct port of the Bailey & López de Prado paper. Use that, don't reimplement.

## Related entries
- `research/papers/2026/lopez-de-prado-deflated-sharpe.md` (Qlib's DSR port)
- `research/papers/2026/moreira-muir-volatility-managed.md` (Qlib's portfolio handlers with vol target)
- `research/repos/qlib-microsoft.md` (the repo itself)

## References
- Yang et al., *Qlib: An AI-oriented Quantitative Investment Platform* (2020, arXiv:2009.11189)
- https://github.com/microsoft/qlib
- Qlib documentation: https://qlib.readthedocs.io/
