---
title: "Kaggle: Optiver — Trading at the Close"
authors: ["Optiver (host)"]
affiliation: "Optiver"
source: "https://www.kaggle.com/competitions/optiver-trading-at-the-close"
date: "2024-02-01"
added: "2026-06-22"
last_reviewed: "2026-06-22"
status: "verified"
relevance: "medium"
tier_mapping: ["Q1.1"]
tags: ["benchmark", "kaggle", "market-microstructure", "closing-auction", "regression"]
license: "Unknown"
effort_to_apply: "M"
adoption_risk: "low"
---

## TL;DR
A Kaggle competition hosted by Optiver in 2024. The task: predict the volatility of the closing auction (a 10-minute window before market close) for US equity, given 10 minutes of order book and trade data. The dataset is realistic, the eval metric is RMSE on the realized volatility. The competition attracted ~3,700 teams; the top solutions use gradient-boosted trees (LightGBM, XGBoost) with careful feature engineering on the order book.

## Why we care
The competition is the closest public benchmark to Fincept's market-microstructure feature work. The top solutions document which features matter (order book imbalance, trade flow, time-of-day, realized vol at multiple windows) and which don't. Tier Q1.1 of the Sisyphus Quant/ML Deep Dive calls for feature importance; this benchmark is a free source of "what features matter for microstructure prediction."

## Key ideas
- Data: 10 minutes of order book snapshots (bid/ask prices, sizes) and trade prints, per US equity per day, with realized vol as the target.
- The top solutions use: order book imbalance at multiple levels, weighted mid-price, time-decayed trade flow, realized vol at 30s/60s/300s windows, and time-of-day.
- The eval is RMSE on a 10-minute realized vol forecast, not a directional prediction. Direct translation to Fincept is partial: the *features* transfer; the *target* needs to be re-framed.
- Several top solutions are public on GitHub with detailed writeups. The lessons are concrete and actionable.

## How to apply to Fincept
1. Borrow the feature set: order book imbalance (already in `gbm_predictor/features.py::book_imbalance_1` — generalize to multiple levels), time-decayed trade flow (not yet in Fincept), realized vol at multiple windows (already in `VolatilityFeatures`).
2. Borrow the *evaluation discipline*: the competition's leaderboard shows the gap between public and private test sets. Always validate on a held-out set.
3. Tier Q1.1 dossier can include "feature importance vs Optiver benchmark" as a sanity check.

## Caveats
- The data is US equity, not crypto. The features transfer; the relative importances do not.
- The eval is regression on realized vol, not binary classification on direction. The training recipe is different.
- The competition's data is 10-minute windows; Fincept's hot-reload cadence is 30-60s. The features need to be re-computed at the right frequency.
- The competition is closed; the data is only available to participants with a Kaggle account.

## Related entries
- `research/papers/2026/deep-momentum-lim.md` (the LSTM model class also shows up here)
- `research/repos/qlib-microsoft.md` (Qlib's US equity data handlers are similar)
- `research/architectures/qlib-design.md`

## References
- https://www.kaggle.com/competitions/optiver-trading-at-the-close
- Top solutions: see the competition's "Discussion" and "Code" tabs
