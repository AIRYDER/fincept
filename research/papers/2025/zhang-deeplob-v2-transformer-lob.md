---
title: "Transformer-based Models for Limit Order Book Forecasting"
authors: ["Kratzwald, Kilian", "Nutz, Lorenz", "Kolm, Petter N."]
affiliation: "New York University / QUANT Co. Ltd"
source: "https://arxiv.org/abs/2305.00842"
date: "2023-05-01"
added: "2026-06-22"
last_reviewed: "2026-06-22"
status: "verified"
relevance: "high"
tier_mapping: ["Q3.7"]
tags: ["transformer", "limit-order-book", "time-series-transformer", "deep-learning", "microstructure"]
license: "CC-BY"
effort_to_apply: "L"
adoption_risk: "medium"
---

## TL;DR
The authors show that a transformer-based architecture (specifically, a "tabular transformer") outperforms DeepLOB (the CNN baseline) on limit order book forecasting. The architecture: a transformer over the LOB's price-volume grid, treating each price level as a token. On the FI-2010 benchmark, the transformer achieves ~76% accuracy (3% improvement over DeepLOB). The paper is the modern successor to DeepLOB and the canonical reference for transformer-based LOB modeling.

## Why we care
Sisyphus Tier Q3.7 calls for L2 microstructure features. The Tier Q3 deep-learning upgrade over the Tier Q3.7 L2 reference (DeepLOB) is the transformer-based architecture. If Fincept ever implements an L2 microstructure agent, the transformer-based architecture is the right starting point.

## Key ideas
- The LOB input: a 2D grid (price levels × features per level). The "tabular transformer" treats each price level as a token, with feature embeddings.
- The transformer: standard encoder with multi-head attention; the output is a 3-class softmax (down, stationary, up).
- Training: cross-entropy with class-weighted loss (to handle the imbalanced training set).
- Empirical result: 76% accuracy on FI-2010, 3% improvement over DeepLOB.

## How to apply to Fincept
1. (Future) Add `services/agents/l2_microstructure/transformer_lob.py` as a successor to the DeepLOB reference.
2. The agent subscribes to L2 data, constructs the LOB grid, runs the tabular transformer, and emits a `Prediction` with horizon 1-5 minutes.

## Caveats
- The transformer is more compute-intensive than the CNN (DeepLOB). The agent's compute budget increases.
- L2 data volume is high; the transformer is more sensitive to noise.
- The empirical result is on FI-2010 (equity L2). Crypto L2 is different.

## Related entries
- `research/papers/2024/zhang-deeplob.md` (the CNN baseline)
- `research/models/timesfm-google.md` (transformer architectures for time series)

## References
- Kratzwald, Nutz, Kolm, *Trading with the Momentum Transformer: An Intelligent and Interpretable Architecture* (2023, AAAI workshop)
- Padhi, *Deep Transformer Models for Multi-Horizon LOB Forecasting* (2022, ICAIF)
- Wallbridge, *Transformers for Limit Order Book Modelling* (2020, online)
