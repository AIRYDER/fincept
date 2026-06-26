---
title: "TimesFM — Time-Series Foundation Model (Google Research)"
authors: ["Das, Abhimanyu", "Kong, Weihao", "Sen, Rajat", "Zhou, Yang"]
affiliation: "Google Research"
source: "https://github.com/google-research/timesfm"
date: "2024-03-01"
added: "2026-06-22"
last_reviewed: "2026-06-22"
status: "verified"
relevance: "high"
tier_mapping: ["Q3"]
tags: ["foundation-model", "time-series", "zero-shot", "transformer", "forecasting"]
license: "Apache-2.0"
effort_to_apply: "L"
adoption_risk: "medium"
---

## TL;DR
TimesFM is a decoder-only transformer pre-trained on a large corpus of time-series data (mostly synthetic + public datasets) for zero-shot forecasting. The model takes a context window of past observations and outputs a quantile forecast for the requested horizon. Released checkpoints: 200M and 500M parameters. Evaluated on Monash benchmarks and others; competitive with or beating supervised baselines in zero-shot settings. Apache-2.0-licensed; weights are public on HuggingFace.

## Why we care
`spec/BUILD_ORDER.md` Task 063 (TSFMs for zero-shot forecast) is on the roadmap. TimesFM is the most credible open implementation as of 2024. Replacing or augmenting the per-symbol gbm_predictor with a foundation model is a Tier Q3 path to potentially better OOS performance without per-symbol training. The risk: TSFMs are a moving target; Chronos (Amazon), Moirai (Salesforce), and TimeGPT (Nixtla) are the competitors. The right move is to evaluate TimesFM on Fincept's captured data before committing.

## Key ideas
- Architecture: decoder-only transformer; input is a patch-tokenized time series; output is a quantile forecast.
- Pre-training: a large corpus of synthetic + public time series. The synthetic data uses Gaussian processes to produce diverse dynamics.
- Inference: feed the model a context of length L (default 512); specify a horizon H; the model returns quantile forecasts.
- Fine-tuning: optional; the published checkpoints are zero-shot.
- Compute: 200M model on a single A100 can forecast ~10k series per second.

## How to apply to Fincept
1. Add `services/agents/ts_foundation/` package (per spec Task 063).
2. Wrap TimesFM as an Agent subclass: `load()` and `predict_one(symbol, freq, horizon)` methods.
3. The agent subscribes to `features.online`, calls TimesFM, emits a `Prediction`.
4. A/B test against the gbm_predictor in shadow mode. Compare deflated Sharpe after 30 days.
5. NOT a replacement for the per-symbol GBM yet — TSFMs are not yet competitive with per-symbol models on horizon-1-bar predictions, but they're competitive on horizon-15-to-60-bar.

## Caveats
- TSFMs are a fast-moving field. TimesFM is one of several credible options; Chronos and Moirai are equally good. Pick one as the default and watch the others.
- The pre-training corpus is mostly public + synthetic; it does not contain crypto microstructure. The zero-shot performance on crypto may be weaker than on US equity or energy.
- Quantile forecasts are not the same as a directional `Prediction` event. We need to translate a quantile distribution into a (direction, confidence) pair; the right translation is "P(up) = mass above zero" or "P(up) = 1 - median quantile mass below zero" depending on the model.
- The 200M parameter model is a serious compute addition to a uv workspace. Profile before adopting.

## Related entries
- `research/models/chronos-amazon.md` (the Amazon competitor)
- `research/models/lag-llama.md` (the open-source alternative)
- EDGE_ROADMAP Tier X+ Task 063

## References
- https://github.com/google-research/timesfm
- Das et al., *A Decoder-Only Foundation Model for Time-Series Forecasting* (2024, arXiv:2310.10688)
- HuggingFace model: https://huggingface.co/google/timesfm-1.0-200m
