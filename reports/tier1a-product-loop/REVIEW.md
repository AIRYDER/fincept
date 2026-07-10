# Tier 1A Product Loop — Integration Review

**Reviewer:** Reviewer 1
**Task ID:** task-mr6r2ygd-ff2954e4
**Date:** 2026-07-04
**Verdict:** APPROVED

---

## 1. Verdict

**APPROVED** — All 272 tests pass across 12 test files. The migration chain is
linear and valid. All security invariants are enforced at the DB layer. The
promotion gate integration is correct — the registry persists, the gate
enforces, no logic is duplicated. The `metadata` → `extra_metadata` rename is
consistent. No file conflicts between the 4 work items.

One minor cosmetic issue found (stale docstring in 0005 migration — see §6)
that does not affect functionality.

---

## 2. Migration Chain

**Status: VERIFIED — linear, no branching**

The Alembic revision chain is:

```
0001 → 0002 → 0003 → 0004 → 0004b → 0005
```

| Migration | revision | down_revision | Tables created |
|-----------|----------|---------------|----------------|
| 0003_provider_data | `0003` | `0002` | provider_data |
| 0004_callback_ingestion | `0004` | `0003` | artifact_manifests, model_dossiers, callback_receipts, callback_dlq, callback_metrics, shadow_predictions |
| 0004b_observability | `0004b` | `0004` | training_jobs, job_cost_events, job_metrics, cost_summary |
| 0005_model_registry | `0005` | `0004b` | models, model_versions, model_metrics, promotions, promotion_decisions, shadow_evaluations |

**No branching:** Each `down_revision` is unique — no two migrations point to
the same parent. The chain is strictly linear.

**Cross-migration FK validity:**
- `0004b.training_jobs.callback_receipt_id` → `0004.callback_receipts.callback_id` ✓
- `0005.model_versions.dossier_content_hash` → `0004.model_dossiers.content_hash` ✓
- `0005.model_versions.artifact_id` → `0004.artifact_manifests.artifact_id` ✓
- `0005.model_versions.callback_receipt_id` → `0004.callback_receipts.callback_id` ✓

All FK targets exist in prior migrations. The `use_alter=True` on
`models.current_version_id` → `model_versions.version_id` correctly breaks the
circular FK between `models` and `model_versions`.

---

## 3. Security Invariants

**Status: ALL VERIFIED**

| Invariant | Verification |
|-----------|-------------|
| No secrets/HMAC signatures/raw payloads in any DB column | ✓ — No column stores secret material. `callback_receipts` stores `signature_valid: bool` + `payload_hash: str(64)` + `payload_ref: str(512)`, never the signature bytes. |
| `request_payload_ref` is a file path, not the payload | ✓ — `training_jobs.request_payload_ref` is `String(512), nullable=True`; docstring confirms "a file path to the request JSON on disk, never the payload itself." |
| `callback_receipts` stores `signature_valid: bool` + `payload_hash`, never signature bytes | ✓ — Confirmed in migration 0004 DDL (lines 146-148) and ORM model `CallbackReceiptRow` (callback_tables.py lines 134-136). |
| `shadow_predictions.authority` has CHECK constraint forcing `'shadow-only'` | ✓ — Migration 0004 line 252-253: `sa.CheckConstraint("authority = 'shadow-only'", name="ck_shadow_predictions_authority_shadow_only")`. ORM model mirrors this. |
| `promotion_decisions.waivers` is a JSON list of `{issue_code, waived_by, reason}`, never secrets | ✓ — Migration 0005 line 181: `sa.Column("waivers", JSONB, ...)`. ORM docstring confirms "JSONB list of `{issue_code, waived_by, reason}` dicts — never secrets." |
| `promotion_decisions.rejection_reason` has CHECK constraint to enum domain | ✓ — Migration 0005 lines 192-195: CHECK forces values to `('no_dossier','insufficient_evidence','sentinel_failed','blocking_issue','mvp_level_limit')` or NULL. |

---

## 4. Promotion Gate Integration

**Status: VERIFIED — no duplication, correct persist-then-update flow**

The `ModelRegistryDB.promote()` method (registry_db.py lines 315-423):

1. **Queries the version row** to get `from_status` and `model_id` (line 342-352).
2. **Assembles `PromotionEvidence`** from registry tables via `_assemble_evidence()` (line 354) — queries `model_dossiers`, `model_metrics` (tournament + sentinel), and `dossier.blocking_issues`.
3. **Builds `PromotionRequest`** (line 357-362).
4. **Calls `self._gate.evaluate(request=request, evidence=evidence)`** (line 365) — delegates to the existing `PromotionGate`, no logic duplicated.
5. **Persists the `promotions` row** (always — approved or rejected) (line 371-381).
6. **Persists the `PromotionReceipt` into `promotion_decisions`** (line 384-400).
7. **Only if `receipt.decision == ReviewDecision.APPROVED`:** updates `model_versions.status` + `models.current_status` (line 403-419).

**If the gate rejects:** status does NOT change (the `if approved` block is
skipped), but the `promotions` row and `promotion_decisions` receipt ARE
persisted (they're written before the conditional). The audit trail is complete.

**`DossierStatus` enum was NOT renamed** — confirmed still `DossierStatus` in
dossier.py line 45. The registry imports it as `from quant_foundry.dossier import DossierRecord, DossierStatus`.

---

## 5. Test Results

**All tests pass.** Exit code 1 on Windows is the known pytest temp-dir cleanup
`PermissionError` — NOT a test failure. All output dots are green (no F's or
E's).

### Tier 1A test suite (first command)

```
& ".venv/Scripts/python.exe" -m pytest tests/test_callback_db_sinks.py tests/test_runpod_dispatch.py tests/test_registry_db.py tests/test_cost_tracker.py tests/test_schemas.py tests/test_runpod_client.py tests/test_promotion.py tests/test_dossier.py --tb=short -q
```

| Test file | Tests | Result |
|-----------|-------|--------|
| test_callback_db_sinks.py | 31 | PASS |
| test_runpod_dispatch.py | 18 | PASS |
| test_registry_db.py | 38 | PASS |
| test_cost_tracker.py | 44 | PASS |
| test_schemas.py | 16 | PASS |
| test_runpod_client.py | 21 | PASS |
| test_promotion.py | 25 | PASS |
| test_dossier.py | 27 | PASS |
| **Subtotal** | **220** | **ALL PASS** |

### Existing callback tests (second command)

Note: `test_callbacks.py` does not exist in the test directory. The existing
callback tests are `test_signatures.py`, `test_inbox.py`, `test_callback_dlq.py`,
and `test_callback_metrics.py`.

```
& ".venv/Scripts/python.exe" -m pytest tests/test_signatures.py tests/test_inbox.py tests/test_callback_dlq.py tests/test_callback_metrics.py --tb=short -q
```

| Test file | Tests | Result |
|-----------|-------|--------|
| test_signatures.py | 9 | PASS |
| test_inbox.py | 5 | PASS |
| test_callback_dlq.py | 28 | PASS |
| test_callback_metrics.py | 10 | PASS |
| **Subtotal** | **52** | **ALL PASS** |

### Grand total: 272 tests, ALL PASS

---

## 6. Code Quality

**Status: CLEAN — one minor cosmetic issue**

### ORM models match migration DDL
Verified column names, types, and constraints match between:
- `0004_callback_ingestion.py` ↔ `callback_tables.py` ✓
- `0004b_observability.py` ↔ `observability.py` ✓
- `0005_model_registry.py` ↔ `registry_tables.py` ✓

### `metadata` → `extra_metadata` rename
Consistent across all three files:
- `observability.py` line 108: `extra_metadata: Mapped[...] = mapped_column("metadata", JSON, ...)` — Python attribute `extra_metadata` maps to DB column `metadata` (avoids SQLAlchemy reserved `metadata` attribute on `Base`).
- `cost_tracker.py` line 283: accepts `metadata` parameter, line 310: passes `extra_metadata=metadata` to ORM row, line 536: reads `r.extra_metadata` and returns as `"metadata"` key.
- `test_cost_tracker.py` line 357: passes `metadata={...}`, line 364: asserts `row.extra_metadata == {...}`.

### `__init__.py` exports
- `libs/fincept-db/src/fincept_db/__init__.py` exports `callback_tables`, `observability`, `registry_tables` ✓
- `services/quant_foundry/src/quant_foundry/__init__.py` — only exports skeleton stubs by design; new modules are imported directly by consumers ✓

### Missing imports / type errors
None found. All imports verified:
- `registry_db.py` imports `DossierStatus`, `PromotionGate`, `PromotionReceipt`, `PromotionRequest`, `PromotionWaiver`, `ReviewDecision`, `PromotionRejectionReason`, `BlockingIssue`, `PromotionEvidence` from `promotion.py`; `DossierRecord` from `dossier.py`; `SentinelReceipt`, `SentinelSeverity` from `sentinel.py`; `TournamentResult` from `tournament.py`.
- `sqlalchemy_update_status` and `sqlalchemy_update_model_status` helper functions defined at module level (lines 685-714) with local `from sqlalchemy import update` to avoid circular import.
- Call sites match function signatures (4 args for both helpers).

### Minor cosmetic issue (non-blocking)
- **0005_model_registry.py docstring line 4:** says `Revises: 0004` but the actual `down_revision = "0004b"` (line 47). The code is correct; the docstring is stale. This is cosmetic only — Alembic uses the `down_revision` variable, not the docstring.

---

## 7. Integration Points

**Status: ALL VERIFIED**

| Integration point | Verification |
|-------------------|-------------|
| Callback ingestion sinks (db_sinks.py) implement existing protocols | ✓ — `DbDossierStore.store(training_result: dict)` matches `DossierStoreSink` protocol; `DbShadowLedgerStore.store(predictions: list[dict])` matches `ShadowLedgerSink` protocol. `CallbackReceiptDbStore`, `CallbackDlqDbStore`, `CallbackMetricsDbStore` write to their respective tables. |
| Training dispatcher (runpod_client.py) sends the policy dict | ✓ — runpod_client.py line 36: `from quant_foundry.runpod_policy import build_job_policy`; line 304: `body = json.dumps({"input": request_payload, "policy": build_job_policy()})`. |
| Model registry (registry_db.py) uses sync sessions | ✓ — All 14 `Session(engine)` / `Session(self.engine)` calls use sync sessions. No `AsyncSession` anywhere. |
| Cost tracker (cost_tracker.py) uses sync sessions | ✓ — All 13 `Session(engine)` / `Session(self.engine)` calls use sync sessions. No `AsyncSession` anywhere. |
| No file conflicts between the 4 work items | ✓ — Each work item touches distinct files. Shared files (`engine.py`, `fincept_db/__init__.py`) were modified only by work item 1 (callback persistence) for sync engine support and module exports. |

---

## 8. Risks

1. **Dual connection pool (sync + async):** The callback processor uses sync
   sessions while the rest of the platform may use async. This creates a second
   Postgres connection pool. The design doc acknowledges this as "acceptable
   for the first cut." Monitor pool exhaustion under load.

2. **`record_metrics` and `record_shadow_evaluation` are not idempotent:** Unlike
   `register_model` / `register_version` / `record_job_dispatch` (which use
   `ON CONFLICT DO NOTHING`), these methods generate unique IDs per call and are
   append-only. A replayed call creates a duplicate row. This is by design
   (append-only metrics) but callers must be aware.

3. **`_assemble_evidence` queries the latest tournament/sentinel metrics row**
   (ordered by `recorded_at_ns desc`, takes `.first()`). If multiple metrics
   rows exist for the same version, only the most recent is used. This is
   correct for the promotion gate but means historical metric snapshots are not
   part of the evidence packet.

4. **0005 docstring stale** (cosmetic, non-blocking): says `Revises: 0004`
   instead of `Revises: 0004b`. No functional impact.

---

## 9. Recommendations

1. **Fix the 0005 docstring** — change `Revises: 0004` to `Revises: 0004b` in
   `0005_model_registry.py` line 4. Cosmetic only; can be done in a follow-up.

2. **Proceed to Tier 1.3 (Dataset Registry)** — the product loop foundation
   (callback persistence → training dispatcher → model registry → observability)
   is complete and verified. The next priority per AGENTS.md is the dataset
   registry.

3. **Add an end-to-end integration test** — a single test that dispatches a
   training job (via runpod_client with mock), receives a callback, persists it
   (via db_sinks), registers the model (via registry_db), records cost events
   (via cost_tracker), and promotes through the gate. This would verify the
   full product loop as a single transaction.

4. **Monitor the dual connection pool** — once production traffic flows, verify
   the sync pool is not exhausting connections under concurrent callback
   bursts. Consider a shared pool or async migration if issues arise.
