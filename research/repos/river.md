---
title: "river — Online Machine Learning in Python"
authors: ["Bifet, Albert et al."]
affiliation: "Eindhoven University of Technology / contributors"
source: "https://github.com/online-ml/river"
date: "2021-01-01"
added: "2026-06-22"
last_reviewed: "2026-06-22"
status: "verified"
relevance: "high"
tier_mapping: ["Q1.3", "Q3"]
tags: ["online-learning", "concept-drift", "incremental-learning", "ADWIN", "lightgbm"]
license: "BSD-3-Clause"
effort_to_apply: "M"
adoption_risk: "low"
---

## TL;DR
River is the canonical Python library for online (incremental) machine learning. It includes streaming versions of common models (linear regression, logistic regression, decision trees, random forests, LightGBM via `river-forest`), feature scaling, feature selection, anomaly detection, and — most importantly for Fincept — concept-drift detectors (ADWIN, DDM, EDDM, Page-Hinkley). The API is sklearn-like: `model.learn_one(x, y)` and `model.predict_one(x)`.

## Why we care
Tier Q1.3 of the Sisyphus Quant/ML Deep Dive calls for drift detection. River ships production-grade ADWIN, DDM, and Page-Hinkley implementations that can be plugged directly into the prediction-log monitoring pipeline. Tier Q3 (online learning) calls for incremental GBM updates; River's `river-forest` package includes a streaming LightGBM variant. The library is BSD-3-licensed, actively maintained, and well-documented.

## Key ideas
- Online learning: model is updated one example at a time, no batch retraining.
- `model.learn_one(x, y)` updates model state; `model.predict_one(x)` returns a prediction. The API is uniform across model types.
- Drift detectors: `river.drift.ADWIN(delta=0.002)`, `river.drift.binary.DDM()`, `river.drift.binary.EDDM()`, `river.drift.PageHinkley()`. Each returns a tuple (drift_detected, info) when the input is updated.
- Concept drift can be detected on the *input* distribution (no labels needed) or on the *prediction error* (labels needed, more accurate but slower).
- For Fincept, the right shape: monitor rolling Brier on the active model's predictions using ADWIN with a 30-day rolling window.

## How to apply to Fincept
1. Add `river` to `services/jobs/pyproject.toml` dependencies.
2. Add `services/jobs/model_drift.py::ADWINMonitor(delta=0.002)`. Each day, push the previous day's Brier score to the detector.
3. When `ADWIN.drift_detected` is True, emit an Alert to `events.alerts` and trigger the model-promotion review flow.
4. For Tier Q3 online learning: replace the LightGBM retrain step in `services/agents/gbm_predictor/train.py` with a River `forest.AMFClassifier` or `forest.BinaryClassifier` that updates incrementally on each new bar.

## Caveats
- Online learning changes the model state continuously. Hot-reload semantics change: instead of "swap to new model on a 30s tick", the model updates in place. The agent's hot-reload loop in `gbm_predictor/main.py` would need an adapter.
- Drift detectors have a delay-vs-false-positive tradeoff. Tight delta = noisy; loose delta = misses real changes. Calibrate on the first 90 days of data.
- River's online models have different accuracy than batch models on the same data. The accuracy gap can be 5-10%. Measure before adopting.
- The library is pure Python; some operations are slow at scale. For a 50-symbol universe at 1-minute cadence, the per-cycle work is small (< 1 ms), but the *state* held per model grows linearly with feature count.

## Related entries
- `research/papers/2025/concept-drift-survey-gama.md` (the survey)
- `research/papers/2026/lopez-de-prado-deflated-sharpe.md` (DSR can be combined with ADWIN for auto-decommission)
- EDGE_ROADMAP Tier X+ Task 095 (online learning / concept drift)

## References
- https://github.com/online-ml/river
- https://riverml.xyz/latest/
- Bifet et al., *CD-MOA: A New Framework for Distributed Real-Time Monitoring of Evolving Data Streams* (2011) — concept drift in distributed systems
