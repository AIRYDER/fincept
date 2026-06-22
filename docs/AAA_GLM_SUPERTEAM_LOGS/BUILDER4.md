# Builder 4 (GLM) — Work Log

**Agent:** Builder 4 (GLM-5.2)
**Joined:** 2026-06-22
**Track:** Quant Foundry evidence-loop foundations (Phase 4)

---

## Task Adoption Log

### TASK-0405: Build Feature Lake Builder MVP — ADOPTED 2026-06-22

**Status:** COMPLETED 2026-06-22 (commit `7f704bd`)
**Order:** 26
**Depends on:** TASK-0401 (BUILDING — Builder 1 GLM). The plan explicitly permits
"Start with fixture-backed dataset export", so the manifest/builder scaffolding
(PIT proofs, purged-fold boundaries, embargo, feature availability, export receipt)
is being built and tested against fixtures without waiting for real settlement labels.

**Files owned (file-disjoint from active tasks):**
- `services/quant_foundry/src/quant_foundry/feature_lake.py` (created)
- `services/quant_foundry/src/quant_foundry/dataset_manifest.py` (created)
- `services/quant_foundry/src/quant_foundry/feature_availability.py` (created)
- `services/quant_foundry/tests/test_feature_lake.py` (created)

**File-disjoint check:**
- TASK-0401 (Builder 1 GLM, BUILDING) owns `settlement.py`, `outcomes.py`, `metrics.py`, `test_settlement.py` — no overlap.
- TASK-0304 (Builder 2, BUILDING) owns `outbox.py`, `inbox.py`, `test_outbox.py`, `test_inbox.py` — no overlap.
- `schemas.py` is intentionally NOT touched (Builder 2's track). The richer feature-lake
  manifest (fold boundaries, embargo, PIT proof fields, checksum) lives in
  `dataset_manifest.py` as a local model composing the base `DatasetManifest`, so
  ownership stays clean.
- `services/features/src/features/computer.py` is NOT touched — fixture-backed export
  only, per plan guidance ("Start with fixture-backed dataset export").

**Plan (TDD):**
1. Write failing tests in `test_feature_lake.py` covering:
   - Fixture dataset exports with a stable manifest (deterministic manifest hash).
   - Manifest hash changes when source data changes.
   - **Point-in-time proof is mandatory**: each row records `observed_at` alongside
     `event_ts`; export asserts every feature value's `observed_at <=` row decision time.
   - A deliberately leaky fixture (feature whose `observed_at` is after the decision
     time) is REJECTED at export, not silently included.
   - As-of (backward) joins only; forward joins rejected at construction time.
   - Purged-k-fold + embargo split boundaries emitted in the manifest; embargo length
     >= max label horizon in the dataset.
   - As-of universe reconstruction (includes delisted/renamed symbols) — no
     survivorship bias.
   - Feature availability report produced.
   - Export receipt written.
   - Training jobs can reference the manifest instead of DB credentials.
2. Implement `dataset_manifest.py` (`FeatureLakeManifest` with fold boundaries,
   embargo, PIT proof fields, checksum; composes base `DatasetManifest`).
3. Implement `feature_availability.py` (per-feature availability report).
4. Implement `feature_lake.py` (builder: as-of universe, PIT assertion, leak
   rejection, manifest emission, export receipt).
5. Run `uv run pytest services/quant_foundry/tests/test_feature_lake.py -q` green;
   ruff/mypy clean.
6. Atomic commit.

---

## Completion Log

### TASK-0405 — COMPLETED 2026-06-22 (commit `7f704bd`)

**Result:** Feature Lake Builder MVP shipped, fixture-backed, TDD.

**Files delivered:**
- `services/quant_foundry/src/quant_foundry/dataset_manifest.py` —
  `FeatureLakeManifest`, `PurgedFoldSpec`, `FoldBoundary`. Embargo >= max
  label horizon enforced at the spec level; purge gap prevents label overlap;
  stable SHA-256 `manifest_hash()` over all reproducibility fields;
  `training_reference()` returns `dataset_id` + `manifest_hash` only (no
  DSN/password/credentials ever present).
- `services/quant_foundry/src/quant_foundry/feature_availability.py` —
  `FeatureAvailabilityReport` with per-feature availability %, missing
  feature detection, JSON serialization.
- `services/quant_foundry/src/quant_foundry/feature_lake.py` —
  `FeatureLakeBuilder` asserts `observed_at <= decision_time` for every
  feature value (`LeakyFeatureError` on look-ahead); rejects forward joins
  (row `decision_time` after symbol `listed_until`); as-of universe includes
  delisted/renamed symbols (no survivorship bias); emits purged-k-fold +
  embargo boundaries; `export_receipt()` writes a verifiable receipt to disk
  with the manifest hash, PIT-proof flag, and availability summary.
- `services/quant_foundry/tests/test_feature_lake.py` — 18 tests covering
  every acceptance criterion.

**Verification:**
- `uv run pytest services/quant_foundry/tests/test_feature_lake.py -q` → 18 passed.
- `uv run pytest services/quant_foundry/tests -q` → 87 passed (no regressions).
- `uv run ruff check` → All checks passed.
- `uv run ruff format --check` → clean.
- `uv run mypy` → Success: no issues found in 3 source files.

**Acceptance criteria — all met:**
- [x] Fixture dataset exports with stable manifest.
- [x] Manifest hash changes when source data changes.
- [x] Deliberately leaky fixture (observed_at after decision time) rejected
      at export via `LeakyFeatureError`, not silently included.
- [x] As-of (backward) joins only; forward join (row after delisting) rejected
      at construction time.
- [x] Purged-fold boundaries + embargo length present in manifest;
      embargo >= max label horizon.
- [x] As-of universe reconstruction includes delisted/renamed symbols.
- [x] Feature availability report exists.
- [x] Training jobs reference manifest (dataset_id + hash) instead of DB
      credentials — `training_reference()` contains no DSN/password.

**File-disjointness preserved:**
- `schemas.py` NOT touched (Builder 2's track) — richer manifest kept local
  to `dataset_manifest.py`.
- `services/features/src/features/computer.py` NOT touched — fixtures only,
  per plan guidance.
- No overlap with TASK-0401 (settlement.py/outcomes.py/metrics.py) or
  TASK-0304 (outbox.py/inbox.py).

**Handoff note for downstream tasks:**
- TASK-0501 (RunPod training container) can now reference a
  `FeatureLakeManifest.training_reference()` instead of DB credentials.
- TASK-0406 (leakage sentinel) can reuse `LeakyFeatureError` and the
  purged-fold verifier pattern from `dataset_manifest.py`.
- When TASK-0401 (settlement) lands, the feature lake can be extended to
  pull real labels via the settlement ledger; the manifest contract is
  already stable.

---

### TASK-0404: Build Tournament Scoring Skeleton — YIELDED to Builder 1 (collision avoided)

**Status:** YIELDED 2026-06-22
**Reason:** After adopting 0404 in my log, I discovered Builder 1 (GLM-6th)
already has a complete TDD red test file on disk (`test_tournament.py`,
untracked, 638 lines) and the SWARM_BOARD marks 0404 as BUILDING by Builder 1.
Builder 1's `BUILDER1_GLM.md` log hadn't been updated yet, but the test file +
board claim are clear evidence they're actively implementing. To avoid a
destructive collision on `tournament.py`/`leaderboard.py`/`significance.py`,
I yield 0404 to Builder 1. No files created or modified by me for 0404.

### TASK-0402: Add Shadow Prediction Ledger Storage — ADOPTED 2026-06-22

**Status:** COMPLETED 2026-06-22
**Order:** 23
**Depends on:** TASK-0401 (✅ DONE — Builder 1, commit 855f01b, 27/27 green).
Verified green before adoption.

**Task selection rationale:**
- TASK-0405 (mine) DONE. TASK-0404 yielded to Builder 1 (test file on disk).
  TASK-0306 claimed by Builder 3 (`gateway.py` on disk). TASK-0406 blocked on
  0404. TASK-0402 is explicitly UNOWNED on the SWARM_BOARD (line 43: "Builder 3
  released, Builder 1 yielded; both files deleted; available for adoption").
  Builder 3 moved to 0403 (done) then 0306 — no longer pursuing 0402.
- Unblocked (0401 done) and file-disjoint from all active builders.
- I have context from Builder 2's `callbacks.py` `ShadowLedgerStub` which
  defines the interface the real ledger must match (`store(predictions)`,
  `list()`), so the mock dispatcher can swap to the real ledger cleanly.

**Files owned (file-disjoint from active tasks):**
- `services/quant_foundry/src/quant_foundry/shadow_ledger.py` (created)
- `services/quant_foundry/tests/test_shadow_ledger.py` (created)

**Files deliberately NOT touched:**
- `schemas.py` — `ShadowPrediction` + `Authority` already defined by TASK-0302;
  consumed read-only. No new schema fields needed.
- `libs/fincept-bus/src/fincept_bus/streams.py` — spec says "later, if adding
  `qf.shadow.predictions`"; MVP uses local storage first.
- `callbacks.py` (Builder 2) — the `ShadowLedgerStub` is consumed read-only as
  an interface reference; the real ledger matches its `store`/`list` surface so
  the mock dispatcher can swap stub → real ledger by injection.

**Plan (TDD):**
1. Write failing tests in `test_shadow_ledger.py` covering:
   - Shadow predictions store safely (local JSONL, restart-durable).
   - Duplicate batches are idempotent (same prediction_id + batch_hash → no
     duplicate; same batch_hash + same content → idempotent skip).
   - Order-like fields are REJECTED (quantity, side, broker, order_type —
     shadow predictions must never carry trading authority).
   - No write path to `sig.predict` exists (structural source guard: the
     module contains no `sig.predict` / `fincept_bus` producer reference).
   - Read API by `model_id` / `symbol` / time window.
   - Batch hashing reuses `ids.hash_payload` (deterministic; diff-hash
     rejection as a security event mirroring TASK-0304's inbox invariant).
   - `authority` is always `shadow-only` (enforced at store time).
   - Frozen + extra="forbid" on all record models.
2. Implement `shadow_ledger.py`:
   - `ShadowLedgerRecord` (frozen, extra="forbid"): prediction_id, model_id,
     symbol, ts_event, horizon_ns, direction, confidence, expected_return,
     p_up, feature_availability, latency_ms, regime, model_version, authority,
     batch_hash, stored_at_ns, metadata.
   - `BatchHasher` / `compute_batch_hash` reusing `ids.hash_payload`.
   - `ShadowLedger` (JSONL, restart-durable, idempotent by prediction_id +
     batch_hash; rejects diff-hash as security event; rejects order-like
     fields; enforces shadow-only authority).
   - `store_batch(predictions, batch_hash)`, `list()`, `read_by_model`,
     `read_by_symbol`, `read_by_window`.
   - Structural no-`sig.predict` / no-`fincept_bus` guard (defense-in-depth).
3. Run `uv run pytest services/quant_foundry/tests/test_shadow_ledger.py -q`
   green; ruff/mypy clean.
4. Atomic commit.

---

## Completion Log (continued)

### TASK-0402 — COMPLETED 2026-06-22

**What shipped:**
- `services/quant_foundry/src/quant_foundry/shadow_ledger.py` —
  `ShadowLedgerRecord` (frozen Pydantic, extra='forbid', authority defaults to
  shadow-only), `compute_batch_hash` (deterministic SHA-256 reusing
  `ids.hash_payload`), `ORDER_LIKE_FIELDS` frozenset (quantity/side/broker/
  order_type/etc.), `StoreReceipt` dataclass, `ShadowLedger` class:
  - JSONL durability at `<base_dir>/shadow_predictions.jsonl` with fsync.
  - Restart-safe: replays JSONL on construction (last record per prediction_id
    wins).
  - `store_batch(predictions, batch_hash)`: validates each prediction against
    `ShadowPrediction` (extra='forbid'), rejects order-like fields with a clear
    security message, enforces shadow-only authority, tamper-checks the
    caller-supplied batch_hash vs computed hash, idempotent by
    (prediction_id, batch_hash) — duplicate = skip, diff-hash = security event
    (rejected).
  - Read API: `list()`, `read_by_model(model_id)`, `read_by_symbol(symbol)`,
    `read_by_window(start_ns, end_ns)`.
  - Structural no-trading-stream / no-bus guard: no bus producer, no stream
    writer, no reference to the orchestrator's trading stream or the bus
    library (defense-in-depth + negative test).
- `services/quant_foundry/tests/test_shadow_ledger.py` — 25 TDD tests covering:
  compute_batch_hash determinism/order-sensitivity, ShadowLedgerRecord
  frozen+strict+default authority, store_batch safety/idempotency/diff-hash
  rejection, order-like field rejection (quantity/side/broker/order_type),
  shadow-only authority enforcement, read API (model/symbol/window), restart
  durability + idempotency after restart, structural no-trading-stream guard
  (source scan + no forbidden attributes + no sig.predict file), batch hash
  mismatch rejection.

**Verification:**
- `uv run pytest services/quant_foundry/tests/test_shadow_ledger.py -q` →
  25 passed.
- `uv run pytest services/quant_foundry/tests -q
  --ignore=services/quant_foundry/tests/test_tournament.py` → 146 passed
  (no regressions; TASK-0301/0302/0303/0304/0305/0401/0403/0405 all green).
  (test_tournament.py is Builder 1's TDD red WIP for TASK-0404 — expected to
  fail until tournament.py/leaderboard.py/significance.py are implemented.)
- `uv run ruff check shadow_ledger.py test_shadow_ledger.py` → All checks
  passed.
- `uv run mypy shadow_ledger.py` → Success: no issues found in 1 source file.

**Design notes for downstream tasks:**
- The real `ShadowLedger` matches the `ShadowLedgerStub` interface in
  `callbacks.py` (Builder 2, TASK-0305): `store(predictions)` / `list()`. The
  mock dispatcher can swap stub → real ledger by injection. The real ledger's
  `store_batch` is richer (batch_hash, idempotency, security guards) but the
  `list()` surface is compatible.
- The ledger stores `ShadowLedgerRecord` (enriched with batch_hash +
  stored_at_ns), not raw `ShadowPrediction`. Callers that need the original
  prediction can reconstruct it from the record's fields (all ShadowPrediction
  fields are present on the record).
- The `qf.shadow.predictions` Redis stream is deferred per spec ("later, if
  adding"). When it lands, the ledger can be extended to dual-write (JSONL +
  stream) without changing the store_batch/read API.
- The diff-hash security event mirrors TASK-0304's inbox invariant: same key
  (prediction_id) + different content hash = tamper/replay attempt, rejected.

**File-disjoint confirmation (post-commit):**
- `schemas.py` NOT modified (ShadowPrediction + Authority consumed read-only).
- `callbacks.py` (Builder 2) NOT modified (ShadowLedgerStub consumed
  read-only as interface reference; no import).
- `libs/fincept-bus/streams.py` NOT modified (local storage MVP).
- `settlement.py` / `outcomes.py` / `metrics.py` (Builder 1) — no overlap.
- `dossier.py` / `artifacts.py` / `registry.py` (Builder 3) — no overlap.
- `gateway.py` (Builder 3, TASK-0306) — no overlap.
- `tournament.py` / `leaderboard.py` / `significance.py` (Builder 1,
  TASK-0404) — no overlap.

**Next:** TASK-0402 unblocks TASK-0406 (Leakage Sentinel) and the promotion
gate wiring. Available for the next task.
