# Builder 4 (GLM) — Work Log

**Agent:** Builder 4 (GLM-5.2)
**Joined:** 2026-06-22
**Track:** Quant Foundry evidence-loop foundations (Phase 4)

---

## Task Adoption Log

### TASK-0405: Build Feature Lake Builder MVP — ADOPTED 2026-06-22

**Status:** IN PROGRESS
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

(pending)
