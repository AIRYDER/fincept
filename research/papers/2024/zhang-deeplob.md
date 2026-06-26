---
title: "DeepLOB: Deep Convolutional Neural Networks for Limit Order Books"
authors: ["Zhang, Zihao", "Zohren, Stefan", "Roberts, Stephen"]
affiliation: "Oxford-Man Institute of Quantitative Finance"
source: "arXiv:1912.10622"
date: "2019-12-23"
added: "2026-06-22"
last_reviewed: "2026-06-22"
status: "verified"
relevance: "high"
tier_mapping: ["Q3.7"]
tags: ["limit-order-book", "deep-learning", "cnn", "microstructure", "high-frequency"]
license: "CC-BY"
effort_to_apply: "L"
adoption_risk: "medium"
---

## TL;DR
The authors show that a deep convolutional neural network (CNN) trained on limit order book (LOB) data outperforms classical feature-engineered models at mid-price movement prediction. The architecture: a sequence of convolutional and pooling layers operating on the LOB's bid/ask price-volume grid; the output is a 3-class prediction (up, down, stationary). On a large FI-2010 benchmark dataset, DeepLOB achieves 73% accuracy on a 50-step-ahead prediction — a ~10% improvement over the best classical baseline.

## Why we care
Sisyphus Tier Q3.7 calls for "L2 microstructure features." Fincept is currently ingesting level-1 data (best bid/ask) but the crypto exchanges provide full L2 (and even L3) data. The DeepLOB reference is the canonical architecture for extracting predictive signal from LOB data. Implementing it would give Fincept a per-exchange microstructure agent.

## Key ideas
- The LOB input: a 2-channel image (bid side and ask side) of the order book at time t, with rows being price levels and columns being volume. Typically 10-40 levels.
- The CNN architecture: 3-4 convolutional blocks (Conv → ReLU → BatchNorm → MaxPool) followed by 1-2 fully connected layers.
- The output: a softmax over {down, stationary, up} at horizon h.
- Training: cross-entropy loss with class-weighted loss to handle the imbalanced (most bars are stationary) training set.
- Empirical result: 73% accuracy on FI-2010, 10% improvement over best classical baseline.

## How to apply to Fincept
1. (Future) Add `services/agents/l2_microstructure/` package per spec Task 096.
2. The agent subscribes to L2 data from a crypto exchange (Binance, Coinbase), constructs the LOB image at each tick, runs DeepLOB, and emits a `Prediction` with horizon 1-5 minutes.
3. The orchestrator consensus combines this with the per-symbol agents.

## Caveats
- L2 data volume is high: 10+ MB/minute per exchange. The agent will need significant compute.
- L2 microstructure is a fast-moving field; the DeepLOB architecture (2019) is being superseded by transformer-based models (e.g., `research/papers/2025/transformer-lob.md` to be added).
- The signal decays faster than the GBM signal. The horizon is short (1-5 minutes).
- Crypto L2 data is *not* the same as equity L2; the architecture may need re-training on crypto data.

## Related entries
- `research/papers/2025/transformer-lob.md` (future, when added — successor architecture)
- `research/benchmarks/kaggle-optiver-trading-at-the-close.md` (realized vol from L2)
- EDGE_ROADMAP §2 Y Task 096

## References
- Zhang, Zohren, Roberts, *DeepLOB: Deep Convolutional Neural Networks for Limit Order Books* (2019, IEEE TSE)
- Tsantekidis et al., *Using Deep Learning to Detect Price Change Indications in Financial Markets* (2017, IJCNN)
- Kolm, Turiel, Westray, *Deep Order Book Tracking* (2023, JFM)
