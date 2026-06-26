---
title: "Deep Learning Volatility: Neural SDEs for Hedging and Option Pricing"
authors: ["Horvath, Blanka", "Mukhopadhyay, Anirban", "Teichmann, Josef"]
affiliation: "ETH Zurich"
source: "https://arxiv.org/abs/2102.03949"
date: "2021-02-08"
added: "2026-06-22"
last_reviewed: "2026-06-22"
status: "verified"
relevance: "low"
tier_mapping: ["Q4"]
tags: ["neural-sde", "volatility-modeling", "hedging", "option-pricing", "deep-learning"]
license: "CC-BY"
effort_to_apply: "XL"
adoption_risk: "high"
---

## TL;DR
The authors show that neural stochastic differential equations (neural SDEs) — SDEs where the drift and diffusion functions are neural networks — can be calibrated to option price data and used for hedging. The neural SDE produces both a price (the model-implied expectation) and a hedge (the model-implied delta). The paper shows that neural SDEs match classical stochastic vol models (Heston, SABR) on calibration data and beat them on out-of-sample data.

## Why we care
Sisyphus Tier Q4 lists "Neural SDE for hedging" in the open-entries-needed table. Fincept is paper-trading only; options and hedging are Tier Q4 (frontier). But if Fincept ever adds options, neural SDEs are the modern approach to dynamic hedging. This is a *scoping* entry — we are not building it now, but we are tracking it.

## Key ideas
- A neural SDE: `dS_t = μ_θ(S_t, t) dt + σ_θ(S_t, t) dW_t` where μ_θ and σ_θ are neural networks.
- Training: the SDE is calibrated to option price data by minimizing the calibration error.
- Inference: simulate the SDE to compute expectations (prices) and use automatic differentiation to compute deltas (hedges).
- Empirical result: neural SDEs match Heston on SPX option data and beat Heston on out-of-sample months.

## How to apply to Fincept
1. NOT recommended for current implementation. This is a Tier Q4 entry.
2. If Fincept ever adds options, the neural SDE is the modern hedging architecture.
3. The `services/portfolio/` service could include a hedging module that consumes the neural SDE's delta.

## Caveats
- Implementation cost: months. The training is unstable; the calibration requires careful regularization.
- The neural SDE is a black box; the hedge has no closed-form interpretation.
- Options are out of MVP scope per `ROADMAP.md`. This entry is for future-tracking only.

## Related entries
- `research/papers/2025/lyons-path-signatures.md` (related continuous-time technique)
- EDGE_ROADMAP §3 Z Tier 4 (candidates)

## References
- Horvath, Mukhopadhyay, Teichmann, *Deep Learning Volatility* (2021, SSRN)
- Gierjatowicz, Sabate-Vidales, Šiška, *Robust Pricing and Hedging via Neural SDEs* (2022, JCP)
- Cuchiero, Jentzen, et al., *Neural SDEs as Infinite-Dimensional GANs* (2020, NeurIPS)
