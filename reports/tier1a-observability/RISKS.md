# Risks — Observability & Cost Tracking

## Known risks

1. **GPU cost rates are hardcoded defaults.** The rates (RTX_4090: $0.40/hr, etc.) are built-in defaults. In production, these should be configurable via environment variables or a config table, as cloud provider pricing changes.

2. **No live cost integration.** The `CostTracker` records cost events but does not query RunPod's billing API. Costs are estimated from GPU type + duration, not actual billing data.

3. **`metadata` column name.** The DB column is `metadata` but the Python attribute is `extra_metadata` due to SQLAlchemy's reserved name. Any code that queries `JobCostEventRow.metadata` directly will fail — must use `extra_metadata`.

4. **Period rollup is not automatic.** `compute_period_cost()` must be called explicitly. There is no scheduled job or trigger that automatically rolls up costs.

5. **Sync engine only.** Matches the callback ingestion pattern (sync sessions). A second connection pool is the cost.

## Soft blockers
- None

## Next recommended tasks
1. Wire `CostTracker.record_job_dispatch()` into the RunPod dispatch path (call it right after `HttpRunPodClient.dispatch()`)
2. Wire `CostTracker.update_job_status()` into the callback processor (call it when a callback arrives)
3. Add a scheduled job to compute period cost rollups automatically
4. Make GPU cost rates configurable via environment variables
5. Add a cost dashboard endpoint to the API
