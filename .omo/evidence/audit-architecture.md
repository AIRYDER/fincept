# Architecture & Dependency Audit — ml-dataset-evidence-spine

**Scope:** `git diff 7dc5fc1..HEAD` (18 commits, 44 files, +7149/-194 lines)
**Reviewer focus:** circular imports, facade design, module boundaries, CV convergence, workspace registration, API routes, settlement worker design.
**Mode:** READ-ONLY — no files were modified.

---

## Summary

The evidence-spine work lands a new `fincept_core.datasets` package as the shared home for the ML dataset evidence primitives (approved-roots gate, manifest schemas, settlement ledger, feature snapshots, CV fold math, dossier/calibration helpers) plus a new `services/settlements` worker and new API routes (`/models/{name}/outcomes`, approved-roots gating on `/models/train` and `/backtest/run`).

The **#1 plan risk — a circular import between `services/quant_foundry` and `libs/fincept-core`** — is **fully mitigated**: `fincept_core.datasets` has zero imports from any `services/*` package, and the runtime import test passes. The facade is clean (explicit re-exports, no star-imports, auditable `__all__`).

The most significant architectural issue is **two parallel settlement systems that do not interoperate**: the new `fincept_core.datasets.SettlementStore` (keyed by `agent_id`, cost model `v1.default` = 5/3/0 bps) and the pre-existing `quant_foundry.settlement.SettlementLedger` (keyed by `model_id`, cost model `cm-v1` = 10/5/3 bps). The new `/models/{name}/outcomes` route reads from the new store, but the only production settlement writer today is the quant_foundry gateway sweep, which writes to the **old** store. The new `settlements.worker` is only exercised by a replay script and tests — it has no production wiring. The same gap affects `FeatureSnapshotStore` (defined + tested, but no production writer).

CV utility convergence is **complete and correct**: all three call sites delegate to `fincept_core.datasets.cv`, with the backtester retaining a deprecated shim for back-compat and the quant_foundry manifest retaining a thin re-wrapper that preserves its public dataclass return type.

---

## Findings

### 1. Circular import analysis — **PASS**

- `grep` for `from services|import services` in `libs/fincept-core/src/fincept_core/datasets/` → **0 matches**.
- `grep` for `import quant_foundry|from quant_foundry` in `libs/fincept-core/src/fincept_core/datasets/` → **0 matches**.
- `services/settlements/` imports only from `fincept_core.datasets` + `fincept_core.prediction_log`; **no** `quant_foundry` import (`grep` → 0 matches).
- Runtime test: `uv run python -c "import fincept_core.datasets; import quant_foundry.training_manifest; import settlements.worker; print('no circular imports')"` → **prints `no circular imports`**.
- The layering rule (core ← services, never services → core re-exported back) is respected. `dossier.py` explicitly documents the "parity only, not imported" relationship with `quant_foundry.dossier`.

### 2. Facade design (`datasets/__init__.py`) — **OBSERVATION** (with one minor concern)

- **Clean surface:** every re-export is explicit; no star-imports; `__all__` is alphabetical and auditable (lines 140-161).
- **`try/except ImportError` for `cv.py`** (lines 64-77): the docstring frames this as a placeholder for "todo 17 hasn't landed yet", but `cv.py` *has* landed. The guard now only fires on a genuine Pydantic/version mismatch. This is a defensible safety net, but the `# pragma: no cover` and the `None`-assignment branches are now effectively dead code in normal operation. **Minor concern:** binding public names to `None` on an ImportError means a caller doing `from fincept_core.datasets import make_folds` gets `None` rather than an `ImportError` — a silent failure mode that could surface as a confusing `TypeError: 'NoneType' is not callable` far from the root cause. Consider letting the ImportError propagate now that `cv.py` is real, or at minimum raising a loud `RuntimeError` in the fallback.
- **`build_evidence_receipt` shape** (lines 85-137): correct and well-designed. Flat JSON-safe dict; `pending_time` sentinel when settlement is `None`; optional `feature_schema_hash` + `feature_health` passthrough. The shape matches what `GET /models/{name}/outcomes` renders. Keyword-only arguments (`*`) prevent positional-arg drift. Tested in `test_datasets_init.py`.

### 3. Module boundaries — **CONCERN** (parallel settlement systems)

There are **two divergent settlement ledgers**:

| Aspect | `fincept_core.datasets.settlement` (NEW) | `quant_foundry.settlement` (PRE-EXISTING) |
|---|---|---|
| File layout | `data/settlements/<agent_id>.jsonl` | `<base_dir>/settlements/<model_id>.settlements.jsonl` |
| Join key | `agent_id` + `prediction_id` | `model_id` + `prediction_id` |
| Cost model | `v1.default` (fee 5, spread 3, slippage 0 bps) | `cm-v1` (fee 10, spread 5, slippage 3, borrow 25 bps/day) |
| Record type | `fincept_core.datasets.SettlementRecord` (Pydantic) | `quant_foundry.outcomes.SettlementRecord` (Pydantic, different fields) |
| Writer | `settlements.worker.tick` (only in replay script + tests) | `quant_foundry.settlement_sweep.SettlementSweep` (wired into gateway) |
| Reader | `GET /models/{name}/outcomes` API route | `quant_foundry.gateway.settlement_status` |

**Impact:** The `/models/{name}/outcomes` route reads from `SettlementStore` (`$SETTLEMENTS_DIR`), but the only production settlement writer (the quant_foundry gateway sweep, polled every 60s from `api.main` lifespan) writes to the **other** ledger. So in production, `/outcomes` will return `pending_time` for every prediction until/unless the new `settlements.worker` is wired into a production poller. The two cost models also disagree (5/3/0 vs 10/5/3), so even if both ran, realized returns would diverge by ~10 bps per prediction.

**Schema duplication** between `fincept_core.datasets.schemas` and `quant_foundry.schemas`: `DatasetManifest` and `ArtifactManifest` are defined in **both** packages with identical field sets. The `fincept_core` versions add hex-shape validators; the `quant_foundry` versions do not. This is acknowledged in the `datasets.schemas` docstring ("intentionally separate") but creates a drift risk — a manifest built with one cannot be validated by the other without potential field-shape mismatch. No code today translates between them.

**`_validate_agent_id` duplication:** the identical forbidden-character set + logic is copied verbatim into three modules (`prediction_log`, `datasets.settlement`, `datasets.feature_snapshot`). Each copy carries a comment pointing at the others for "audit grep" symmetry. This is a deliberate symmetry-for-auditability tradeoff, but it is still copy-paste — a shared `fincept_core.names` helper would be cleaner. **Observation, not a flaw.**

### 4. CV utility convergence — **PASS** (with one observation)

All three call sites delegate to `fincept_core.datasets.cv`:

- **`services/agents/gbm_predictor/train.py:55,190`** — imports `make_folds` from `fincept_core.datasets` and delegates fold-position math. The local `walk_forward_splits` still applies `purge_bars` per-fold during slice translation (lines 199-214) and special-cases the last fold to absorb the remainder; `embargo_bars` is intentionally a no-op for the anchored expansion (documented). No duplicated fold-math remains.
- **`services/backtester/walk_forward.py:65-66,115-142`** — delegates via `_make_folds_local` → `_shared_make_folds`. The public `make_folds` (line 145) is a **deprecated shim** that emits a `DeprecationWarning` and re-wraps the Pydantic `Fold` into the local frozen-dataclass `Fold` to preserve `isinstance`/`dataclasses.fields` expectations. Internal callers (`walk_forward_backtest`) use the quiet `_make_folds_local`. No duplicated fold-math remains.
- **`services/quant_foundry/training_manifest.py:63-65,350-389`** — imports `derive_walk_forward_window as _canonical_derive_walk_forward_window` and re-wraps into the local `WalkForwardWindow` dataclass to preserve the public `quant_foundry.training_manifest` surface. `local_training_dispatch.py` imports the wrapper from `training_manifest` (not directly from `fincept_core`), which is the right layering.

**Observation:** the backtester and quant_foundry both retain a **local dataclass mirror** of the shared Pydantic model (`Fold` / `WalkForwardWindow`) purely for back-compat with their public import surface. This is acceptable transitional debt, but the duplication means a future field addition to the shared model must be mirrored in two dataclasses or the translation helpers will silently drop the new field.

### 5. Workspace package registration — **PASS**

- `pyproject.toml` line 26: `services/settlements` is in `[tool.uv.workspace].members`.
- Line 44: `settlements = { workspace = true }` in `[tool.uv.sources]`.
- `services/settlements/pyproject.toml` follows the same pattern as sibling services: `hatchling` build backend, `packages = ["src/settlements"]`, `requires-python = ">=3.12"`, deps on `fincept-core` + `pydantic>=2.7`, dev group with `mypy/pytest/pytest-asyncio/ruff`. Consistent with `services/agents`, `services/backtester`, etc.
- The runtime import (`import settlements.worker`) succeeds, confirming the package is installed in the workspace.

### 6. API route structure — **PASS** (with one minor concern)

- **`/models/{name}/outcomes`** (models.py:902-992): follows the existing per-model route pattern (`/{name}/predictions`, `/{name}/prediction-stats`). Same `limit` (1..1000) + `since_ns` query contract. Uses `_get_settlement_store()` (fresh per request, env-rooted) consistent with `_get_prediction_log()`. Left-joins via `build_evidence_receipt` — clean separation of transport from join logic. Registered before the catch-all `/{name}` route? **No** — it is registered *after* `/{name}` (line 750) and `/{name}/predictions` (805). This works because FastAPI matches `/models/{name}/outcomes` against `/{name}/outcomes`? Actually no — `/{name}` is a single-segment path; `/outcomes` is a sub-path. FastAPI route matching is by full path, so `/{name}/outcomes` (multi-segment) is distinct from `/{name}` (single-segment). Order does not matter here. **PASS.**
- **Approved-roots gate on `/models/train`** (models.py:508-527): layered — empty-string check (422) then `ApprovedRoots.resolve` (422 with `code: "approved_roots_violation"`). Returns a `JSONResponse` directly rather than raising, which is a slight inconsistency with the backtest route (which lets `ApprovedRootsError` propagate to the shared handler in `api.main`). Both produce the same 422 body, but via different code paths. **Minor concern:** two different patterns for the same gate (manual `try/except` + `JSONResponse` in `models.py` vs. shared exception handler in `backtest.py`). The shared handler in `api/approved_roots.py` + `api.main:272` was built specifically so routes don't need per-route try/except — `models.py` doesn't use it. Functionally equivalent today, but a future maintainer might miss the `models.py` inline handler when changing the response shape.
- **Approved-roots gate on `/backtest/run`** (backtest.py:117-128): uses the `Depends(get_approved_roots)` pattern and lets `ApprovedRootsError` propagate to the shared handler. This is the cleaner pattern.
- `api/approved_roots.py` is well-designed: single dependency, idempotent handler registration, `X-Approved-Roots-Code` header preserves the fine-grained reason.

### 7. Settlement worker design — **CONCERN** (correct logic, but unwired + divergent contract)

- **`tick` / `tick_sync`** (worker.py:186-281): the async/sync pair is sound. `tick_sync` is a faithful inline re-implementation (not `asyncio.run`) so the replay fixture stays deterministic. The state machine is correct:
  - `prior == "settled"` → skip (idempotent).
  - `prior == "pending_data"` + still no data → skip (no duplicate pending rows).
  - `prior == "pending_data"` + data now available → append `settled` (supersedes; ledger is append-only so the old pending row is retained as history).
  - no prior + data available → append `settled`.
  - no prior + no data → append `pending_data`.
  This matches the `SettlementStore.append` idempotency contract (a `settled`/`failed` row for the same `(prediction_id, cost_model_version)` raises `duplicate`, while `pending_*` rows may be superseded).
- **`market_data_source` contract** (worker.py:24-37): `Callable[[str, int, int], Awaitable[float | None]]` — returns the close at `ts2` (the later timestamp), or `None`. The worker calls it twice per prediction (entry + exit). The contract is documented but **not typed as a Protocol** — it's an inline `Callable` alias. A `Protocol` would make the contract more discoverable and let mypy verify the fixture source conforms. **Observation.**
- **`pending_data → settled` transition:** correct. The look-ahead guard in `SettlementStore.append` (line 262: `decision_window_end_ns > now_ns` raises `look_ahead`) is satisfied because `_load_due_predictions` only returns rows where `ts_event + horizon_ns <= now_ns`, so `decision_window_end_ns = ts_event + horizon_ns <= now_ns`.
- **CONCERN — no production wiring:** `settlements.worker.tick` is called only from `scripts/paper_spine_replay.py` and `services/settlements/tests/test_worker.py`. The `api.main` lifespan polls `gateway.run_settlement_sweep` (the **quant_foundry** sweep), not the new worker. So the new evidence-spine settlement store has no production writer. See finding #3.
- **Cost-model constants mirrored** (worker.py:54-57): `_FEE_BPS=5.0`, `_SPREAD_BPS=3.0`, `_SLIPPAGE_BPS=0.0` are copied from `settlement.DEFAULT_COST_MODEL` "so the worker does not depend on the dict shape". This is a third copy of the cost numbers (the dict in `settlement.py`, these constants, and the `cm-v1` values in `quant_foundry.settlement_sweep`). A single `fincept_core.datasets.settlement.DEFAULT_COST_MODEL` dataclass consumed directly would be less drift-prone.

### 8. `FeatureSnapshotStore` — **CONCERN** (defined but no production writer)

`FeatureSnapshotStore` is implemented, tested (13 references in `test_feature_snapshots.py`), and re-exported from the facade — but **no production code writes to it**. `grep` for `FeatureSnapshotStore` outside `libs/fincept-core` returns zero matches. The `gbm_predictor.main` agent writes a *separate* `FeatureHealthLog` sidecar (feature-availability counters: missing/defaulted/aliased lists) to `data/feature_health/<agent_id>.jsonl`, not feature-row snapshots. So the "what the agent saw" leg of the evidence spine is defined but not yet capturing production data. The `build_evidence_receipt` helper accepts a `feature_snapshot` and the `/outcomes` route passes `feature_snapshot=None` — so the schema-hash field is never populated in production today.

### 9. `build_dossier` / `build_calibration_sidecar` — **OBSERVATION** (parity helpers, not yet consumed)

`dossier.py` provides pure helpers matching the `quant_foundry.dossier.DossierRecord` and `real_trainer._compute_metrics` shapes "for parity only — not imported". `grep` confirms no service code imports `build_dossier` or `build_calibration_sidecar` (only one incidental match in `test_artifacts.py` for an unrelated method name). These are forward-looking shared utilities that have not yet displaced the quant_foundry-internal implementations. No harm, but the duplication they are meant to eliminate still exists.

---

## Verdict

**The evidence spine is architecturally sound at the layering boundary** — the circular-import risk is eliminated, the facade is clean, CV math is converged, and the new service is correctly registered. The settlement worker's internal state machine is correct.

**The work is not yet production-wired end-to-end.** Two concrete gaps should be closed before this spine is relied upon for real outcome reporting:

1. **Wire `settlements.worker` into a production poller** (or consolidate the two settlement ledgers into one). Today `/models/{name}/outcomes` reads from a store that nothing writes to in production, while the quant_foundry gateway writes to a parallel ledger with a different cost model and key. At minimum, the two cost models must be reconciled (5/3/0 vs 10/5/3 bps) and the read/write paths must agree on `agent_id` vs `model_id` keying.
2. **Wire `FeatureSnapshotStore` into the agent publish loop** (or document that it is intentionally deferred) so the `feature_schema_hash` leg of the evidence receipt is populated.

**Severity tally:** 0 ARCHITECTURAL FLAW · 4 CONCERN · 4 OBSERVATION · 3 PASS. No blocking flaws; the concerns are all "incomplete wiring / divergent duplicates" rather than design errors, and each has a clear remediation path that does not require re-architecting the facade or the layering.
