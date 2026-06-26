---
title: "A Time Series is Worth 64 Words: Long-term Forecasting with Transformers"
authors: ["Nie, Yuqi", "Nguyen, Nam H.", "Sinthong, Pattarawat", "Kalagnanam, Jayant"]
affiliation: "Google Research / CMU"
source: "arXiv:2211.14730"
date: "2022-11-27"
added: "2026-06-22"
last_reviewed: "2026-06-22"
status: "verified"
relevance: "high"
tier_mapping: ["Q3.9", "Q3.7"]
tags: ["time-series-transformer", "patching", "long-term-forecasting", "patchtst"]
license: "Apache-2.0"
effort_to_apply: "M"
adoption_risk: "low"
---

## TL;DR
The authors show that a simple patch-based transformer — where the input time series is split into patches (sub-sequences) and each patch is treated as a token — significantly outperforms other transformer architectures for long-term time series forecasting. The intuition: by treating patches as tokens (rather than individual time steps), the attention is over *time intervals* rather than *time points*, which captures local semantics better. On standard benchmarks (ETT, Weather, Electricity), PatchTST achieves ~20% improvement over the prior state-of-the-art.

## Why we care
Sisyphus Tier Q3.9 (TSFMs) lists `research/models/timesfm-google.md`, `chronos-amazon.md`, `lag-llama.md`. PatchTST is a different paradigm: a *single-series* forecasting transformer, not a foundation model. The two are complementary: foundation models for zero-shot forecasting, PatchTST for fine-tuning on a target series. For Fincept, PatchTST is the right model if the team wants to fine-tune on crypto data (as Lim et al. 2019 does for the LSTM).

## Key ideas
- The patch-based tokenization: split the input time series into patches of length P (e.g., 16); each patch is a token.
- The transformer: standard encoder over the patch tokens.
- The output: a forecast for the next H time steps.
- Channel-independent: each time series is processed independently (no cross-series attention). For multi-series forecasting, the model is applied per series.
- Empirical result: 20% improvement over Fedformer, Autoformer, Informer on standard benchmarks.

## How to apply to Fincept
1. (Future) Add `services/agents/ts_foundation/patchtst.py` as a complement to the foundation models.
2. The gbm_predictor can be replaced (in shadow) by a PatchTST model fine-tuned on the per-symbol historical data.
3. The candidate-gate policy compares PatchTST's deflated Sharpe to the GBM's; if PatchTST wins, the active model is updated.

## Caveats
- PatchTST is a fine-tuning model, not zero-shot. Requires ~1 hour of training per symbol on 30k rows.
- PatchTST is single-series. For multi-symbol cross-section, use the LSTM (Lim et al.) instead.
- Implementation cost: 1-2 weeks (the model is moderately complex).

## Related entries
- `research/models/timesfm-google.md` (zero-shot alternative)
- `research/papers/2026/deep-momentum-lim.md` (multi-series alternative)

## References
- Nie, Nguyen, Sinthong, Kalagnanam, *A Time Series is Worth 64 Words* (2023, ICLR)
- https://github.com/yuqinie98/PatchTST
