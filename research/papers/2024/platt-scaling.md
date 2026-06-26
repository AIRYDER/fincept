---
title: "Probabilistic Outputs for Support Vector Machines and Comparisons to Regularized Likelihood Methods"
authors: ["Platt, John C."]
affiliation: "Microsoft Research"
source: "https://www.researchgate.net/publication/2623819"
date: "1999-01-01"
added: "2026-06-22"
last_reviewed: "2026-06-22"
status: "verified"
relevance: "high"
tier_mapping: ["Q1.1", "Q1.2"]
tags: ["calibration", "platt-scaling", "isotonic-regression", "probability"]
license: "Unknown"
effort_to_apply: "S"
adoption_risk: "low"
---

## TL;DR
The original Platt scaling paper. The method: train a one-parameter logistic regression on the SVM's raw scores to produce calibrated probabilities. A 6-line implementation. The technique generalizes to any binary classifier with a continuous output (LightGBM probabilities, neural network logits, etc.) — fit a logistic on (raw_score, true_label) on a held-out set, store the two parameters, and apply at inference.

## Why we care
LightGBM optimizes log-loss, not probability accuracy. The probability output of a LightGBM classifier is *not* calibrated: the predicted "0.7 up" is often closer to 0.55 or 0.85 in reality. This matters for Fincept because: (a) the consensus weights by `|direction|` = `|2*prob-1|`, so uncalibrated probabilities distort the consensus; (b) any future Kelly sizing will be misled by miscalibrated probabilities. Tier Q1.1 of the Sisyphus Quant/ML Deep Dive calls for a calibration dossier; Tier Q1.1 also calls for a Platt scaling step. The implementation is ~30 lines including tests.

## Key ideas
- Platt scaling: P(y=1|s) = 1 / (1 + exp(A·s + B)) where s is the raw score (here, the LightGBM probability itself, in logit space) and (A, B) are fit by maximum likelihood on a held-out set.
- For LightGBM, s = logit(prob) and we fit a single parameter A (the slope) and a single parameter B (the intercept). The output is a rescaled probability.
- Isotonic regression (Zadrozny & Elkan 2002) is a more flexible alternative when the data is plentiful; Platt is more robust when data is sparse.
- Brier score and reliability buckets (10 quantile bins of predicted vs actual) are the standard eval methods.
- Save the calibration parameters alongside the model: `models/gbm_predictor/calibration.json` with `{"A": ..., "B": ...}`.

## How to apply to Fincept
1. Add `services/agents/gbm_predictor/calibration.py::platt_fit(probs, labels) -> (A, B)`.
2. Modify `services/agents/gbm_predictor/train.py::main`: after the final fit, run the model on the held-out 20% of training data, fit Platt, save `calibration.json` next to `model.txt`.
3. Modify `services/agents/gbm_predictor/infer.py::GBMPredictor._predict`: after `prob_up = ...`, transform via Platt before computing `direction` and `confidence`.
4. In the dossier, include reliability buckets and Brier before and after calibration. The before/after delta is the calibration gain.
5. The news_alpha_predictor should get the same treatment.

## Caveats
- Platt scaling assumes the raw scores are well-behaved. With extreme overconfidence (LightGBM output very near 0 or 1), the logit space can blow up. Clamp probabilities to [ε, 1-ε] before fitting.
- Calibrating on the same data you trained on is overfit. Calibrate on a *held-out* fold.
- A single Platt fit assumes the calibration is stable. As the data distribution shifts, the calibration drifts. Re-calibrate on each retrain.

## Related entries
- `research/papers/2026/lopez-de-prado-deflated-sharpe.md` (the gate policy should require a Brier check)
- `research/papers/2026/qlib-architecture.md` (Qlib does the same)
- Tier Q1.1 in `Sisyphus_Quant_ML_Deep_Dive.md`

## References
- Platt, *Probabilistic Outputs for Support Vector Machines* (1999)
- Zadrozny & Elkan, *Transforming Classifier Scores into Accurate Multiclass Probability Estimates* (2002, KDD) — isotonic regression alternative
- Niculescu-Mizil & Caruana, *Predicting Good Probabilities With Supervised Learning* (2005, ICML) — empirical comparison
- Guo et al., *On Calibration of Modern Neural Networks* (2017, ICML) — the warning that deep networks are systematically miscalibrated
