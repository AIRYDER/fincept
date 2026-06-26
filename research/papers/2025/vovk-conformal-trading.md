---
title: "Algorithmic Learning in a Random World: Conformal Prediction for Time Series"
authors: ["Vovk, Vladimir", "Gammerman, Alex", "Shafer, Glenn"]
affiliation: "Royal Holloway / University of London"
source: "https://link.springer.com/book/10.1007/978-3-031-06649-8"
date: "2022-08-01"
added: "2026-06-22"
last_reviewed: "2026-06-22"
status: "verified"
relevance: "high"
tier_mapping: ["Q1.1", "Q2.2", "Q1.3"]
tags: ["conformal-prediction", "distribution-free", "uncertainty", "calibration", "trading"]
license: "CC-BY"
effort_to_apply: "L"
adoption_risk: "low"
---

## TL;DR
The canonical reference on conformal prediction — a distribution-free framework for producing prediction intervals with guaranteed coverage. Conformal prediction works by computing a nonconformity score on a calibration set, then producing prediction intervals that contain the true value with probability ≥ 1-α. The book is the authoritative reference; recent extensions (2020-2024) adapt conformal prediction to time series, addressing the exchangeability violation. The result: a Kelly-sized position can be calibrated by the prediction interval width, not by the raw point estimate.

## Why we care
Sisyphus Tier Q1.1 calls for calibration. Platt scaling (already in the database) calibrates probabilities. Conformal prediction calibrates prediction intervals. For a Kelly-sized position, what matters is the *distribution* of the prediction, not the point estimate. Conformal prediction intervals are a natural fit for the Tier Q2.4 Kelly sizing problem: allocate more capital when the prediction interval is tight (high confidence in the prediction), less when the interval is wide (low confidence).

## Key ideas
- Conformal prediction: given a nonconformity score function `A(x, y)`, the prediction interval at level 1-α is the set of y values such that `A(x, y) ≤ q_(1-α)(A)`, where `q` is the empirical quantile on the calibration set.
- For time series, the exchangeability assumption is violated. Extensions like ACI (Adaptive Conformal Inference) and EnbPI use a sliding window or online recalibration.
- Coverage guarantee: under weak stationarity, the prediction interval contains the true value with probability ≥ 1-α *in the long run*.
- For trading: a 90% conformal interval on a 1-hour-ahead price prediction is "the price is between X and Y with 90% probability" — a far more useful signal than "the price is 100.5."

## How to apply to Fincept
1. Add `libs/fincept-core/src/fincept_core/conformal.py::ConformalInterval(quantile_estimator, alpha=0.1)`.
2. The gbm_predictor inference loop should output a (direction, confidence_lower, confidence_upper) tuple: the direction is the point estimate, the interval is the conformal prediction.
3. The orchestrator's allocator can use the interval width as an additional risk input: a wide interval → smaller notional.
4. The calibration dossier should include the empirical coverage rate: is the prediction interval covering the true value at the expected rate?

## Caveats
- The coverage guarantee is *marginal* (over the long run), not *per-prediction*. A 90% interval may have a 60% coverage on a specific day.
- Conformal prediction requires a calibration set that is exchangeable with the test set. For time series, this is approximately true only for stationary processes.
- The implementation cost is moderate (1-2 weeks for the conformal library, plus integration).

## Related entries
- `research/papers/2024/platt-scaling.md` (calibration for probabilities)
- `research/papers/2026/chow-yang-correlated-kelly.md` (Kelly consumes the prediction interval)
- `research/papers/2026/moreira-muir-volatility-managed.md` (vol targeting as another uncertainty input)

## References
- Vovk, Gammerman, Shafer, *Algorithmic Learning in a Random World* (2nd ed., 2022, Springer)
- Gibbs & Candès, *Adaptive Conformal Inference Under Distribution Shift* (2021, NeurIPS)
- Xu & Xie, *Conformal Prediction for Time Series* (2022, tutorial at NeurIPS)
