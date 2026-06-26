---
title: "MLflow — Open-Source ML Lifecycle Management"
authors: ["MLflow contributors"]
affiliation: "Linux Foundation / Databricks (originator) / community"
source: "https://github.com/mlflow/mlflow"
date: "2018-06-01"
added: "2026-06-22"
last_reviewed: "2026-06-22"
status: "verified"
relevance: "medium"
tier_mapping: ["none"]
tags: ["mlflow", "experiment-tracking", "model-registry", "lifecycle", "reproducibility"]
license: "Apache-2.0"
effort_to_apply: "M"
adoption_risk: "low"
---

## TL;DR
MLflow is the de facto open-source ML lifecycle management platform. It provides four components: Tracking (log experiments, hyperparameters, metrics), Projects (package code for reproducibility), Models (standard format for model packaging), and Registry (centralized model store with versioning, stage transitions, and approval workflows). The Tracking component alone covers ~80% of what an ML team needs for experiment reproducibility.

## Why we care
The current Fincept model registry is a filesystem-based `PromotionStore` (active + shadow pointers + history JSONL). It works but does not have the standard MLflow features: model versioning, stage transitions (Staging → Production), approval workflows, and a web UI. Adopting MLflow is a Tier Q4 investment that would replace `PromotionStore` with a more standard implementation.

## Key ideas
- MLflow Tracking: log every experiment (hyperparameters, metrics, artifacts) to a tracking server. SQLite for dev, Postgres for prod.
- MLflow Models: a standard format for packaging a model. Load a model with `mlflow.sklearn.load_model("models:/name/version")`.
- MLflow Registry: a centralized model store with versioning, stage transitions, and approval workflows.
- MLflow Projects: package code in a conda/Docker environment for reproducibility.

## How to apply to Fincept
1. (Future) Replace `services/api/src/api/promotions.py::PromotionStore` with an MLflow Registry client.
2. The training CLI (`agents/gbm_predictor/train.py`) logs the model to MLflow Tracking.
3. The promotion endpoint (`POST /models/{name}/promote`) becomes an MLflow stage transition.

## Caveats
- MLflow adds a Postgres dependency (or SQLite for dev). Operational complexity.
- The in-tree `PromotionStore` is simpler and may be sufficient for the team size. Adopt MLflow when the team grows past 3 engineers.
- MLflow's model registry is overkill for Fincept's current model count (3-4 models). Reconsider when the count grows past 20.

## Related entries
- `research/architectures/qlib-design.md` (Qlib's experiment tracking is similar)
- EDGE_ROADMAP §2 (related to ML lifecycle)

## References
- https://github.com/mlflow/mlflow
- https://mlflow.org/docs/latest/index.html
- MLflow: Manage the End-to-End ML Lifecycle (2022, tutorial at KDD)
