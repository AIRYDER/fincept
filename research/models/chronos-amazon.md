---
title: "Chronos — Time-Series Foundation Model (Amazon Science)"
authors: ["Ansari, Abdul Fatir et al."]
affiliation: "Amazon Science"
source: "https://github.com/amazon-science/chronos-forecasting"
date: "2024-03-01"
added: "2026-06-22"
last_reviewed: "2026-06-22"
status: "verified"
relevance: "high"
tier_mapping: ["Q3"]
tags: ["foundation-model", "time-series", "zero-shot", "transformer", "forecasting", "T5"]
license: "Apache-2.0"
effort_to_apply: "L"
adoption_risk: "medium"
---

## TL;DR
Chronos is Amazon's time-series foundation model, built on the T5 architecture. The pre-training approach: tokenize time series via scaling + quantization into a fixed vocabulary, then train a T5 encoder-decoder on next-token prediction. Released model sizes: 8M (Tiny), 20M (Mini), 200M (Small), 710M (Base). Apache-2.0-licensed; weights on HuggingFace. Evaluated on Monash benchmarks; competitive with TimesFM and Lag-Llama.

## Why we care
TimesFM and Chronos are the two most credible open TSFMs as of 2024. They are alternative implementations of the same idea. For Fincept, the right approach is to evaluate both on captured data before committing. Chronos has a larger family of model sizes (8M through 710M), which is useful for matching the model size to the compute budget.

## Key ideas
- Tokenization: time series is scaled (z-score or median) and quantized into bins; the model is trained on token sequences.
- The encoder-decoder design is more flexible than TimesFM's decoder-only: the model can be prompted with arbitrary context.
- Multiple model sizes allow a compute-quality tradeoff.
- HuggingFace integration is clean: load the model with `AutoModelForSeq2SeqLM.from_pretrained("amazon/chronos-t5-small")` and use the `ChronosPipeline` wrapper.

## How to apply to Fincept
1. Same integration as `research/models/timesfm-google.md` but with the Chronos model.
2. The smaller sizes (8M, 20M) are useful for very-low-latency predictions.
3. The pipeline is `from_pretrained(...)`; not as lightweight as TimesFM's inference path, but HuggingFace integration is well-tested.

## Caveats
- Same caveats as TimesFM: TSFMs are not yet competitive with per-symbol models on horizon-1-bar predictions; they are competitive on horizon-15-to-60-bar.
- The tokenization can lose precision for low-volatility series. The 8M and 20M models quantize more aggressively than the larger models.
- The encoder-decoder design has higher inference latency than decoder-only. For a 60s cadence, this is fine; for sub-second, it matters.

## Related entries
- `research/models/timesfm-google.md`
- `research/models/lag-llama.md`
- EDGE_ROADMAP Tier X+ Task 063

## References
- https://github.com/amazon-science/chronos-forecasting
- Ansari et al., *Chronos: Learning the Language of Time Series* (2024, arXiv:2403.07815)
- HuggingFace: https://huggingface.co/amazon/chronos-t5-small
