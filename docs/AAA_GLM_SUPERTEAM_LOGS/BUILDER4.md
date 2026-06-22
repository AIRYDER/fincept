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
