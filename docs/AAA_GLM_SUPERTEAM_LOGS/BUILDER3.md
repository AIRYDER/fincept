# Builder 3 (GLM) — Work Log

**Agent:** Builder 3 (GLM-5.2)
**Joined:** 2026-06-22
**Track:** Quant Foundry evidence-loop foundations (dossier registry)

---

## Task Adoption Log

### TASK-0402: Add Shadow Prediction Ledger Storage — RELEASED 2026-06-22

**Status:** RELEASED (collision with Builder 1)
**Reason:** Initially adopted TASK-0402, but discovered Builder 1 had already claimed it in
`BUILDER1_GLM.md` and created `services/quant_foundry/src/quant_foundry/shadow_ledger.py`
(untracked). Builder 1's log shows TASK-0402 as IN PROGRESS. To avoid a destructive collision,
I released TASK-0402 back to Builder 1, reverted my ownership markers in
`AAAAAAAAA_BIG_PLAN.md` and `NEXT_STEPS_PLAN.md`, and deleted my `test_shadow_ledger.py`
(Builder 1 owns that file). No code from TASK-0402 was committed by me.

### TASK-0403: Build the Dossier Registry — ADOPTED 2026-06-22

**Status:** IN PROGRESS
**Order:** 24
**Depends on:** TASK-0401 (✅ DONE — Builder 1, commit 855f01b) and TASK-0402 (Builder 1, IN PROGRESS)

**Task selection rationale:**
- TASK-0405 was taken by Builder 4 (completed). TASK-0402 was taken by Builder 1 (in progress).
  TASK-0305 was taken by Builder 2 (in progress). TASK-0203 was taken by Builder 5 (in progress).
  TASK-0204 is already implemented per Builder 5. TASK-0306/0404/0406 are blocked by in-flight work.
- TASK-0403 is the earliest unblocked, unclaimed task. Its hard dependency TASK-0401 is DONE.
  The remaining dependency TASK-0402 (Builder 1, in progress) is logical, not a code import:
  the dossier registry stores dossier metadata + artifact manifests, not shadow predictions.
  The dossier can reference a shadow ledger / settlement evidence by ref/id without importing
  those modules. I build with fixtures, so no import of `shadow_ledger.py` / `settlement.py` is
  required.
- File-disjoint from ALL active builders (verified — no dossier/artifact/registry files exist).

**Files owned (file-disjoint from active tasks):**
- `services/quant_foundry/src/quant_foundry/dossier.py` (created)
- `services/quant_foundry/src/quant_foundry/artifacts.py` (created)
- `services/quant_foundry/src/quant_foundry/registry.py` (created)
- `services/quant_foundry/tests/test_dossier.py` (created)

**Files inspected (NOT modified — kept file-disjoint):**
- `services/quant_foundry/src/quant_foundry/schemas.py` — `ModelDossier` + `ArtifactManifest`
  already defined by TASK-0302 with `extra="forbid"`, frozen. Consumed read-only. The richer
  dossier (trial_count, blocking_issues, status, evidence refs) lives in `dossier.py` as a local
  model composing the base, mirroring how Builder 1 kept `SettlementRecord` local in
  `outcomes.py` instead of modifying `schemas.PredictionOutcome`.
- `services/quant_foundry/src/quant_foundry/ids.py` — `hash_payload` reused for artifact hashing.
- `services/quant_foundry/src/quant_foundry/outbox.py` — JSONL durability pattern reference.

**Files deliberately NOT touched:**
- `schemas.py` — shared contract (Builder 2's track + Builder 1's outcomes depend on it).
- `services/api/src/api/routes/quant_foundry.py` — does not exist yet; TASK-0306 owns the API
  route. The registry exposes a Python read API only for MVP (list/detail by model_id / hash).
- `settlement.py` / `outcomes.py` / `metrics.py` (Builder 1) — no import; dossier references
  settlement evidence by id/ref, not by code coupling.
- `shadow_ledger.py` (Builder 1) — no import; dossier references shadow predictions by id/ref.
- `feature_lake.py` / `dataset_manifest.py` (Builder 4) — no import; dossier references dataset
  manifest by id/ref.

**Plan (TDD):**
1. Write failing tests in `test_dossier.py` covering:
   - ModelDossier carries full reproducibility set (dataset/feature/label hashes, code SHA,
     lockfile hash, image digest, seeds, hardware class) + trial_count + blocking_issues.
   - Dossier is immutable by version/hash (same model_id + content hash → same dossier; a
     content change produces a new version).
   - Artifact hash verification: import a mock artifact, verify sha256 matches; bad hash
     rejected (security event, fail closed).
   - Unsupported URI scheme rejected (only allowlisted schemes, e.g. file:// for local MVP).
   - Generate a dossier for a fixture local model (GBM-style).
   - Import a mock artifact from a fixture (mock-dispatcher shape).
   - Dossier status visible through Python read API (list/detail by model_id, by hash).
   - Dossiers stored durable (JSONL, restart-safe).
   - blocking_issues list is append-only and visible.
   - No secrets in dossier records (no token/account fields).
   - Frozen + extra="forbid" on all record models.
2. Implement `artifacts.py`:
   - `ArtifactRecord` (richer local model composing base `ArtifactManifest`): sha256, size,
     uri (allowlisted scheme), model_family, created_at_ns, feature_schema_hash,
     label_schema_hash, code_git_sha, lockfile_hash, container_image_digest.
   - `verify_artifact_hash(data: bytes, expected_sha256: str)` — fail closed on mismatch.
   - `import_artifact(uri, expected_sha256, ...)` — pull-based, hash-verified, scheme-allowlisted.
3. Implement `dossier.py`:
   - `DossierStatus` StrEnum (candidate / research_approved / shadow_approved / paper_approved / rejected).
   - `DossierRecord` (frozen, extra="forbid"): full reproducibility set + trial_count +
     blocking_issues + status + artifact_ref + dataset_manifest_ref + evidence_refs.
   - `DossierBuilder` — assemble a dossier from an artifact + dataset manifest ref + training
     metadata.
4. Implement `registry.py`:
   - `DossierRegistry` (filesystem JSONL, immutable by version/hash, restart-safe).
   - `register(dossier)` — idempotent by (model_id, content_hash); reject different content
     for same model_id+version as a security event (mirrors outbox/inbox invariant).
   - `get(model_id)`, `get_by_hash(content_hash)`, `list(status=)`, `add_blocking_issue(...)`.
5. Run `uv run pytest services/quant_foundry/tests/test_dossier.py -q` green; ruff/mypy clean.
6. Atomic commit.

---

## Completion Log

(pending)
