---
title: "A Survey on Concept Drift Adaptation"
authors: ["Gama, João", "Žliobaitė, Indrė", "Bifet, Albert", "Pechenizkiy, Mykola", "Bouchachia, Abdelhamid"]
affiliation: "Univ. of Porto / Eindhoven Univ. of Technology / Univ. of Waikato"
source: "https://doi.org/10.1109/TKDE.2014.2345282"
date: "2014-04-01"
added: "2026-06-22"
last_reviewed: "2026-06-22"
status: "verified"
relevance: "high"
tier_mapping: ["Q1.3", "Q3"]
tags: ["concept-drift", "online-learning", "model-degradation", "detection", "ADWIN"]
license: "CC-BY"
effort_to_apply: "M"
adoption_risk: "low"
---

## TL;DR
A canonical survey of concept-drift detection and adaptation methods. The paper categorizes drift by type (sudden, gradual, incremental, recurring) and by detection method (statistical tests, window-based, ensembles). It covers ADWIN, DDM, EDDM, Page-Hinkley, and ensemble methods like Streaming Random Patches. The taxonomy is the canonical reference for "my model is degrading; how do I detect and respond?" questions.

## Why we care
Tier Q1.3 of the Sisyphus Quant/ML Deep Dive calls for drift detection on the live prediction log. Currently the system has no monitoring: a model trained 6 months ago may be obsolete, but the operator has no signal. The Gama survey gives the taxonomy and the canonical detectors. ADWIN (Adaptive Windowing) is the right starting point: it's well-implemented in `river`, has bounded memory, and is appropriate for online monitoring of rolling metrics.

## Key ideas
- Concept drift = the relationship between features and labels changes over time.
- Four canonical types: sudden (regime break), gradual (slow shift), incremental (stepwise), recurring (cyclical).
- Detection methods compared: ADWIN (adaptive windowing, low false-positive rate), DDM (drift detection method, sensitive to distribution changes), EDDM (early DDM), Page-Hinkley (cumulative sum).
- ADWIN in particular: maintain a variable-length window; split at each timestep; if the difference in means between the two sub-windows exceeds a threshold, drop the older one. Bounded memory, O(log W) detection delay.
- Adaptation: trigger retraining when drift is detected; or use an online learner that updates incrementally.
- In finance: regime change (2008 GFC, 2020 COVID, 2022 rate shock) is the obvious example. Crypto exchanges delisting, regulatory changes, and stablecoin depegs are less obvious but real.

## How to apply to Fincept
1. Add `services/jobs/model_drift.py::ADWINMonitor(rolling_metric, threshold)`.
2. The metric is rolling 30-day Brier score on the active model's predictions, computed from `data/predictions/<agent_id>.jsonl`.
3. The monitor runs daily via the existing job runner.
4. When drift is detected, emit an `Alert` to `events.alerts` and surface on the dashboard.
5. Combine with the existing model-promotion flow: detected drift + new candidate with better DSR-p-value → automatic shadow promotion (still requires operator approval for active).

## Caveats
- Drift detectors have a delay-vs-false-positive tradeoff. Tight thresholds flag every regime change (noisy); loose thresholds miss real changes.
- Brier score is the right metric for binary classifiers; for multi-class or regression, different metrics apply.
- Detecting drift and *responding* are different. The survey covers detection; the response (retrain, replace, retire) is a system-design problem.
- ADWIN's "Bounded memory" is an asymptotic claim; the actual constant can be configured.

## Related entries
- `research/repos/river.md` (the canonical online learning library implementing ADWIN)
- `research/papers/2026/moreira-muir-volatility-managed.md` (vol regime change is a form of drift)
- EDGE_ROADMAP §2 X+ Task 085 (strategy decay monitor)

## References
- Gama et al., *A Survey on Concept Drift Adaptation* (2014, IEEE TKDE)
- Bifet, Gavalda, *Learning from Time-Changing Data with Adaptive Windowing* (2007, SDM) — the ADWIN paper
- Lu, Liu, Wang, et al., *Learning under Concept Drift: A Review* (2018, IEEE TKDE) — more recent survey
- `river` library: https://github.com/online-ml/river
