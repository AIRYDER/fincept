---
title: "Feast — Open-Source Feature Store for ML"
authors: ["Feast contributors"]
affiliation: "Linux Foundation / Tecton (originator) / community"
source: "https://github.com/feast-dev/feast"
date: "2019-09-01"
added: "2026-06-22"
last_reviewed: "2026-06-22"
status: "verified"
relevance: "medium"
tier_mapping: ["none"]
tags: ["feature-store", "mlops", "online-features", "offline-features", "feature-registry"]
license: "Apache-2.0"
effort_to_apply: "L"
adoption_risk: "low"
---

## TL;DR
Feast is the de facto open-source feature store. It provides a unified API for defining features, computing them offline (for training), and serving them online (for inference). Feast integrates with Redis, BigQuery, Snowflake, Kafka, and most data infrastructure. The "feature registry" gives a single source of truth: every feature has a version, owner, and online/offline status. The architecture pattern: define features once, serve them consistently in both training and inference.

## Why we care
Fincept has an in-tree feature service (`services/features/`) that is well-designed but does not have a "feature registry" — there's no canonical list of all features, their owners, their versions, their online/offline status. Feast is the reference architecture for that registry. Adopting Feast is a Tier Q4 investment, but understanding the pattern (feature registry, online/offline parity) is Tier Q1.

## Key ideas
- Feature definition: a Python file declares the feature (name, type, source, transformation).
- Offline store: BigQuery, Snowflake, Redshift, parquet files. The historical features for training.
- Online store: Redis, DynamoDB. The latest features for inference, served with < 10 ms latency.
- Feature registry: a single source of truth for all features. Used by both training and inference.
- Online/offline parity: the same feature transformation runs in both contexts, ensuring no train/serving skew.

## How to apply to Fincept
1. (Future) Adopt Feast as the feature registry; integrate with the in-tree `services/features/`.
2. The in-tree `OnlineStore` (Redis hash) maps to Feast's online store.
3. The in-tree `OfflineStore` (Timescale) maps to Feast's offline store.
4. The Tier Q1 fix (Sisyphus Phase 1, Q0.1) becomes a *first-class* feature-registry change.

## Caveats
- Feast is a relatively heavy dependency. Adding it to a uv workspace adds ~50 MB and a PostgreSQL metadata store.
- The in-tree `FeatureComputer` is more performant than Feast's default transformations for low-latency crypto data. A hybrid approach (in-tree compute, Feast registry) may be best.
- Feast is actively maintained but the API has changed significantly between versions. Pin to a specific version.

## Related entries
- `research/papers/2025/qlib-architecture.md` (Qlib has a similar feature registry pattern)
- `research/architectures/qlib-design.md` (Qlib's alpha evaluator as a feature-store pattern)

## References
- https://github.com/feast-dev/feast
- https://docs.feast.dev/
- Feature Store for ML (2022, O'Reilly)
