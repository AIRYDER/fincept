---
title: "Qlib — Microsoft"
authors: ["Microsoft Research Asia"]
affiliation: "Microsoft"
source: "https://github.com/microsoft/qlib"
date: "2020-09-22"
added: "2026-06-22"
last_reviewed: "2026-06-22"
status: "verified"
relevance: "high"
tier_mapping: ["Q1.1", "Q2.1", "Q3"]
tags: ["alpha-research", "platform", "backtest", "feature-store", "portfolio-handler"]
license: "MIT"
effort_to_apply: "L"
adoption_risk: "low"
---

## TL;DR
Qlib is the de facto open-source AI quant platform. Python library, MIT-licensed, with first-class support for data handlers, feature engineering, alpha research, portfolio construction, and backtest. The repo has >15k stars, active maintenance, and a substantial contributor base. The `qlib.contrib` directory contains ready-to-use alpha implementations (LightGBM, MLP, Transformer, GRU, TabNet) and `qlib.contrib.data` contains data handlers for US equity, China A-share, and crypto (via custom adapters).

## Why we care
Qlib is the closest open-source reference to what Fincept is building. Several Fincept Tier Q2 tasks have direct Qlib analogs: cross-section ranking (Qlib's `TopkDropoutStrategy`), DSR-style alpha evaluation (Qlib's `Dsp`), portfolio construction with vol target (Qlib's enhanced index strategy). Studying Qlib is the fastest way to identify what Fincept is doing differently, and where.

## Key ideas
- Architecture: data handlers → feature engineering → alpha research → portfolio construction → backtest. Each is a Python object with `fit()` and `predict()`. Qlib orchestrates the rolling train/predict over time.
- The default data model is OHLCV bars on a calendar (US equity, China A-share). Crypto requires a 24/7 calendar handler.
- The platform does *not* ship a real-time OMS; it is batch/backtest-oriented. Fincept's event-sourced OMS is a different design center.
- The most useful components to borrow: alpha evaluator (IC/RankIC/Dsp), portfolio handlers, the rolling cross-validation harness.
- The least useful components: the data handlers (Fincept has its own ingestor), the experiment tracking (Fincept has its own receipts).

## How to apply to Fincept
1. Borrow the alpha evaluator pattern: add `services/agents/<agent>/evaluate.py` with IC, RankIC, and Dsp metrics.
2. Borrow the `TopkDropoutStrategy` pattern: every cycle, rank symbols by signal, drop the bottom decile, equal-weight the top.
3. NOT a wholesale adoption. Qlib's data model is calendar-based; Fincept's is event-based. The two have different concurrency models.
4. The contrib MLP and LightGBM alpha implementations are good reference implementations when Fincept implements Tier Q2.1 / Q2.4.

## Caveats
- Qlib is China-centric by origin; the data handlers and example notebooks lean A-share. US equity and crypto are well-supported but the second-class citizens.
- The Qlib API is not stable across versions. Pin to a specific version and read the migration guide when upgrading.
- The platform does not have built-in risk management; the user is expected to write their own. (Fincept has the same gap; the orchestrator's risk gate is the analog.)
- Qlib's documentation is dense but assumes familiarity with the alpha-research idiom. Newcomers should start with the tutorials in `examples/`.

## Related entries
- `research/papers/2025/qlib-architecture.md` (the paper)
- `research/papers/2026/lopez-de-prado-deflated-sharpe.md` (Qlib ports the DSR)
- EDGE_ROADMAP Tier X+ Tasks 083, 085

## References
- https://github.com/microsoft/qlib
- https://qlib.readthedocs.io/
- The Qlib paper (arXiv:2009.11189) — see `research/papers/2025/qlib-architecture.md`
