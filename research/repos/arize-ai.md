---
title: "Arize AI — ML Observability and Model Monitoring"
authors: ["Arize AI contributors"]
affiliation: "Arize AI"
source: "https://github.com/Arize-ai/phoenix"
date: "2022-01-01"
added: "2026-06-22"
last_reviewed: "2026-06-22"
status: "verified"
relevance: "medium"
tier_mapping: ["none"]
tags: ["ml-monitoring", "drift-detection", "model-observability", "performance-tracking"]
license: "Apache-2.0"
effort_to_apply: "M"
adoption_risk: "low"
---

## TL;DR
Arize AI is the de facto open-source ML observability platform. It provides drift detection (feature drift, prediction drift, label drift), performance monitoring (accuracy, precision, recall, custom metrics), and root-cause analysis (which features are drifting, which cohorts are degrading). The open-source Phoenix library (now part of Arize) is the implementation; the commercial Arize product adds the dashboard, alerts, and integrations.

## Why we care
Sisyphus Tier Q1.3 calls for drift detection. The current Fincept implementation is a roll-your-own ADWIN on the prediction log. Arize Phoenix is a more complete solution: feature drift, prediction drift, label drift, and performance monitoring, with an API designed for streaming data. For a production system, this is the right level of investment.

## Key ideas
- Drift detection: PSI (Population Stability Index), KS test, or Chi-squared test on the feature distribution over time.
- Performance monitoring: track accuracy, precision, recall, F1, AUC on a rolling window.
- Root-cause analysis: correlate drift in features with drift in performance. The dashboard shows which cohorts are degrading.
- Streaming support: Phoenix is designed for streaming data; the API supports async updates.

## How to apply to Fincept
1. (Future) Add Arize Phoenix to `services/jobs/`.
2. The Fincept prediction log is the input; the dashboard surfaces drift on the per-symbol, per-agent level.
3. Alerts: when drift exceeds a threshold, emit `events.alerts` to the operator.

## Caveats
- Phoenix requires a database backend (SQLite for dev, Postgres for prod). Adds operational complexity.
- The Fincept "drift" definition may differ from Phoenix's. Phoenix's PSI is general-purpose; Fincept's is Brier-score-based. The integration needs a custom metric.
- The commercial Arize product is a paid tier. The open-source Phoenix is sufficient for Fincept.

## Related entries
- `research/papers/2025/concept-drift-survey-gama.md` (the underlying drift algorithms)
- `research/repos/river.md` (online learning library with ADWIN)

## References
- https://github.com/Arize-ai/phoenix
- https://docs.arize.com/phoenix
- Arize AI blog: *Detecting Drift in ML Models* (2022, online)
