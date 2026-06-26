---
title: "Lag-Llama — Open-Source Foundation Model for Time-Series Forecasting"
authors: ["Rasul, Kashif", "Ashok, Arjun", "Williams, Andrew Robert", "Ghonia, Hena", "Bhatnagar, Rishika", "Bilos, George", "Schmidt-Nielsen, Jesper", "Scherer, Korbinian"]
affiliation: "Morgan Stanley AI Research"
source: "https://github.com/time-series-foundation-models/lag-llama"
date: "2024-02-01"
added: "2026-06-22"
last_reviewed: "2026-06-22"
status: "verified"
relevance: "high"
tier_mapping: ["Q3"]
tags: ["foundation-model", "time-series", "lag-llama", "open-source", "fine-tunable"]
license: "Apache-2.0"
effort_to_apply: "L"
adoption_risk: "low"
---

## TL;DR
Lag-Llama is a time-series foundation model based on the LLaMA architecture, designed for probabilistic forecasting (output is a distribution, not a point estimate). Pre-trained on a large corpus of public time series; designed to be fine-tuned on a target domain with limited data. The pre-trained checkpoints are released; fine-tuning recipes are documented. Apache-2.0-licensed.

## Why we care
Lag-Llama is the most fine-tunable of the open TSFMs. Where TimesFM and Chronos are zero-shot or limited fine-tuning, Lag-Llama is explicitly designed for fine-tuning on a target domain. For Fincept, the right shape is: take a Lag-Llama checkpoint, fine-tune it on the captured crypto data, and use the fine-tuned model as a Tier Q3 alpha source. This is a research project, not a weekend.

## Key ideas
- LLaMA-based architecture: decoder-only transformer, RoPE positional encoding, SwiGLU activations.
- Pre-training on a large corpus of public time series with diverse dynamics.
- Probabilistic output: the model produces a distribution (e.g., Student-t) over future values; quantile forecasts are sampled from this distribution.
- Fine-tuning recipes in the repo: continued pre-training on a target domain, then supervised fine-tuning on a forecast task.
- Compute: the 200M model fine-tunes in a few hours on a single A100.

## How to apply to Fincept
1. Same integration as `research/models/timesfm-google.md` and `research/models/chronos-amazon.md`.
2. After evaluating both zero-shot competitors, fine-tune Lag-Llama on `data/captures/*.parquet` (the prerequisite of Sisyphus Tier Q0.1).
3. Compare the fine-tuned model to the per-symbol GBM in shadow mode.

## Caveats
- Fine-tuning requires more compute than zero-shot evaluation. Plan for an A100-class GPU.
- The pre-training corpus does not include crypto microstructure; fine-tuning is necessary, not optional, for this domain.
- The "fine-tuning" path is a research project. The "zero-shot comparison" path is a weekend task. Start with the latter.

## Related entries
- `research/models/timesfm-google.md`
- `research/models/chronos-amazon.md`
- EDGE_ROADMAP Tier X+ Task 063

## References
- https://github.com/time-series-foundation-models/lag-llama
- Rasul et al., *Lag-Llama: Towards Foundation Models for Probabilistic Time Series Forecasting* (2023, arXiv:2310.08278)
