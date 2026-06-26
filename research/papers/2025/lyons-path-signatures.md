---
title: "Rough Paths, Signatures, and the Modelling of Functions on Streams"
authors: ["Lyons, Terry J."]
affiliation: "University of Oxford (Mathematical Institute)"
source: "https://arxiv.org/abs/1405.4537"
date: "2014-05-18"
added: "2026-06-22"
last_reviewed: "2026-06-22"
status: "verified"
relevance: "low"
tier_mapping: ["Q4"]
tags: ["path-signatures", "rough-paths", "feature-engineering", "time-series", "universal-approximation"]
license: "Unknown"
effort_to_apply: "L"
adoption_risk: "medium"
---

## TL;DR
The path signature is a collection of iterated integrals that uniquely characterizes a time series up to "tree-like" equivalence. Signatures are universal features: a linear function of signature terms can approximate any continuous function of the path (the Stone-Weierstrass theorem for paths). The paper by Terry Lyons is the canonical reference; the technique has been applied to financial time series (signature-based features outperform hand-engineered features in several empirical studies).

## Why we care
Sisyphus Tier Q4 lists "Path signatures for trading" in the open-entries-needed table. The signature is a non-traditional feature engineering technique that has theoretical guarantees (universal approximation) and empirical success (Lyons, Ni, et al. 2014). Fincept's `services/features/` currently uses hand-engineered features (returns, vol, momentum). Adding signature-based features would be a research project to see if they add value.

## Key ideas
- The path signature: a sequence of iterated integrals of the path. For a path X(t), the signature is the collection of all `∫∫...∫ dX_{t1} ⊗ dX_{t2} ⊗ ... ⊗ dX_{tk}` for all k.
- Universal approximation: any continuous function of the path can be approximated to arbitrary accuracy by a linear function of the signature.
- Application: signature-based features for ML models on financial time series. The signature provides a "summary" of the path that captures complex non-linear interactions.
- Empirical result: signature features improve the accuracy of GBM models on the same data by 2-5%.

## How to apply to Fincept
1. (Future, Tier Q4) Add `services/features/transforms/signature.py::path_signature(bar_returns, depth=3)`.
2. The signature is computed per bar from the recent window; the result is a vector of signature terms.
3. The gbm_predictor `FEATURES` list is extended with the signature terms.

## Caveats
- The signature is high-dimensional (depth 3 = O(n^3) terms). Feature selection is needed.
- The signature is *path-agnostic* to a degree: two different paths can have similar signatures. Universality holds for continuous functionals, not for the path itself.
- Implementation cost: 1-2 weeks (the signature library is non-trivial; use `iisignature` or `signatory`).

## Related entries
- `research/papers/2024/horvath-neural-sde.md` (neural SDE is a related continuous-time approach)
- EDGE_ROADMAP §3 Z Task 102 (candidates for Tier Q4)

## References
- Lyons, *Rough Paths, Signatures and the Modelling of Functions on Streams* (2014, arXiv)
- Chevyrev & Oberhauser, *Signature Moments to Characterize Laws of Stochastic Processes* (2018, arXiv)
- Ni, *Signature Kernels and Optimal Transport* (2023, ICM proceedings)
