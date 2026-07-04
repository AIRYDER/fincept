# Risks — Model Registry (Tier 1.2)

## Known risks

1. **Sync engine only.** The registry uses sync SQLAlchemy sessions (matching the sync CallbackProcessor). A second connection pool is the cost — acceptable for the first cut but may need migration to async if throughput becomes an issue.

2. **FK prevents NO_DOSSIER path.** The `model_versions.dossier_content_hash` FK to `model_dossiers.content_hash` makes the gate's NO_DOSSIER rejection unreachable through normal operations. This is correct — the DB enforces referential integrity — but the gate's check remains as defense-in-depth.

3. **No dataset_manifests table yet.** The hard rule's item 3 (dataset manifest hash) is partially satisfied — `model_dossiers.dataset_manifest_id` exists but the `dataset_manifests` table is Tier 1.5 (not this skill). The gate does not currently check for a matching dataset_manifests row.

4. **No tournament_results table yet.** `shadow_evaluations.tournament_result_id` is nullable and references a future `tournament_results` table. Currently the tournament result is stored as metrics JSON in `model_metrics`.

5. **MVP level cap.** The gate caps promotions at `paper_approved`. `limited_live_approved` is rejected with `MVP_LEVEL_LIMIT`. This is intentional for MVP.

## Soft blockers
- None

## Next recommended tasks
1. Wire the registry's `promote()` method into the API route (`services/api/src/api/routes/quant_foundry.py`)
2. Add a `dataset_manifests` table (Tier 1.5) so the hard rule's item 3 is fully satisfied
3. Add a `tournament_results` table so `shadow_evaluations.tournament_result_id` has a real FK target
4. Add observability hooks to the registry (track promotion attempt duration, gate evaluation time)
