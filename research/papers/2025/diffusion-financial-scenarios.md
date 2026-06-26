---
title: "Score-based Diffusion Models for Synthetic Financial Time Series"
authors: ["Tashiro, Yusuke", "Song, Jiaming", "Ermon, Stefano"]
affiliation: "Stanford University"
source: "arXiv:2107.03518"
date: "2021-07-08"
added: "2026-06-22"
last_reviewed: "2026-06-22"
status: "verified"
relevance: "low"
tier_mapping: ["Q4"]
tags: ["diffusion", "score-based", "scenario-generation", "synthetic-data", "stress-test"]
license: "CC-BY"
effort_to_apply: "XL"
adoption_risk: "high"
---

## TL;DR
The authors show that score-based diffusion models — the same architecture behind Stable Diffusion for images — can be adapted to generate synthetic financial time series. The trained model produces realistic synthetic scenarios that preserve the statistical properties of the historical distribution (return moments, volatility clustering, fat tails). The model is the modern successor to GANs for synthetic financial data and is more stable to train.

## Why we care
Sisyphus Tier Q4.2 calls for "Generative scenario simulation (GAN/diffusion adversarial scenarios)." The diffusion approach is the modern improvement over GANs (more stable training, better sample quality). This is a Tier Q4 entry — not in the next-build queue, but a research-tracking entry for the future.

## Key ideas
- Score-based diffusion: learn the score function (gradient of the log-density) of the data distribution; sample by reversing a stochastic differential equation.
- Conditional generation: condition the diffusion on a regime label.
- Validation: the generated scenarios should preserve key statistics (return distribution, volatility clustering, cross-asset correlations).
- Application: stress testing, paper-trading training data, what-if analysis.

## How to apply to Fincept
1. NOT recommended for current implementation. This is a Tier Q4 entry.
2. The Tier Q4 plan: train a diffusion model on Fincept's captured data; generate synthetic 1-year scenarios for stress testing.
3. Use the generated scenarios to compute worst-case Kelly, worst-case Sharpe, and worst-case drawdown.

## Caveats
- The synthetic scenarios are *similar* to the historical, not *novel*. A model trained on 2015-2023 data will not produce a 2008-style crash.
- Validation is hard: how do you know the synthetic scenarios are realistic?
- Implementation cost: 3-6 months. The model is research-grade, not production.

## Related entries
- `research/papers/2024/kiyavash-generative-scenarios.md` (GAN alternative)
- EDGE_ROADMAP §3 Z Task 101

## References
- Tashiro, Song, Ermon, *SDG: Score-based Diffusion for Generative Modeling of Financial Time Series* (2021, NeurIPS workshop)
- Ho, Jain, Abbeel, *Denoising Diffusion Probabilistic Models* (2020, NeurIPS)
