# Builder 3 (GLM) — Work Log

**Agent:** Builder 3 (GLM-5.2)
**Joined:** 2026-06-22
**Track:** Quant Foundry evidence-loop foundations (dossier registry + tournament scoring)

---

## Task Adoption Log

### TASK-0404: Build Tournament Scoring Skeleton — ADOPTED 2026-06-22

**Status:** IN PROGRESS (TDD, fixture-backed)
**Order:** 25
**Depends on:** TASK-0401 (✅ DONE — Builder 1, commit 855f01b) and TASK-0403 (✅ DONE — Builder 3, commit de56c38). Both DONE — task unblocked.
**Files owned:** `services/quant_foundry/src/quant_foundry/{tournament,leaderboard,significance}.py` + `services/quant_foundry/tests/test_tournament.py`

**Task selection rationale:**
- TASK-0403 (my previous task) is COMPLETED and committed (`de56c38`). It unblocks TASK-0404
  (Order 25, depends on TASK-0401 + TASK-0403 — both DONE) and TASK-0406 (Order 26b,
  depends on TASK-0403 + TASK-0404 + TASK-0405 — TASK-0404 not yet done, so 0406 still blocked).
- No other builder has claimed TASK-0404 (verified via `findstr /i "0404 tournament"` across
  all BUILDER*.md logs — only Builder 1's note that settlement output "can feed tournament
  scoring" appears, which is a forward reference, not a claim).
- File-disjoint from all active tracks: tournament/leaderboard/significance are new files;
  I will NOT import `outcomes.py`/`settlement.py`/`dossier.py`/`shadow_ledger.py`/
  `feature_lake.py`/`outbox.py`/`inbox.py` — the tournament consumes settled predictions and
  dossier metadata via a local `ScoringInput` schema (plain dataclass/pydantic model), so
  Builder 1's evidence storage internals can change without breaking the tournament and
  vice versa. This mirrors how Builder 1 kept `SettlementRecord` local in `outcomes.py`
  instead of modifying `schemas.PredictionOutcome`, and how I kept `DossierRecord` local
  in `dossier.py` instead of modifying `schemas.ModelDossier`.
- The tournament is the scoreboard that prevents overfit models from being promoted: a
  model with high ML score but poor cost-adjusted return must lose to a simpler profitable
  one. This is the second half of the promotion decision (the dossier is the first half).

**Plan (TDD):**
1. Write failing tests in `test_tournament.py` covering every acceptance criterion:
   - Two fixture models rank deterministically.
   - High-ML-score / poor-cost-return model loses to simpler profitable one.
   - Noise/shuffled-label model fails the gate (negative control).
   - Beats-baseline-gross-but-not-net model is blocked.
   - Deflated Sharpe + bootstrap p-value recorded and shown.
   - Stale or insufficient evidence blocks promotion.
   - Tournament output can feed a promotion packet (structured `TournamentResult`).
2. Implement `significance.py`: stationary/block bootstrap p-value vs. baseline
   (respecting horizon-overlap autocorrelation — NOT an IID t-test), Deflated Sharpe
   Ratio (discounts for trial count + return non-normality).
3. Implement `tournament.py`: `ScoringInput` schema (carries trial count + OOS return
   series, not just summary stats — bootstrap needs the series), deterministic baseline
   comparison (zero-skill / naive persistence / buy-and-hold), explainable weighted score
   over the components, blocking-issues list, stale-evidence handling, minimum-settled-
   sample gate (`insufficient-evidence` status).
4. Implement `leaderboard.py`: `Leaderboard` ranking models by tournament score, with
   `TournamentResult` per model (score components, p-value, DSR, blocking issues,
   promotion recommendation).
5. Run pytest + ruff + mypy clean; atomic commit.

### TASK-0402: Add Shadow Prediction Ledger Storage — RELEASED 2026-06-22

**Status:** RELEASED (collision with Builder 1)
**Reason:** Initially adopted TASK-0402, but discovered Builder 1 had already claimed it in
`BUILDER1_GLM.md` and created `services/quant_foundry/src/quant_foundry/shadow_ledger.py`
(untracked). Builder 1's log shows TASK-0402 as IN PROGRESS. To avoid a destructive collision,
I released TASK-0402 back to Builder 1, reverted my ownership markers in
`AAAAAAAAA_BIG_PLAN.md` and `NEXT_STEPS_PLAN.md`, and deleted my `test_shadow_ledger.py`
(Builder 1 owns that file). No code from TASK-0402 was committed by me.

### TASK-0403: Build the Dossier Registry — ADOPTED 2026-06-22

**Status:** COMPLETED 2026-06-22 (commit `de56c38`)
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

### TASK-0403 — COMPLETED 2026-06-22 (commit `de56c38`)

**Status:** REVIEW (awaiting Reviewer)
**Tests:** 25/25 green — `uv run pytest services/quant_foundry/tests/test_dossier.py -q`
**Full suite:** 121/121 green — `uv run pytest services/quant_foundry/tests -q` (no regressions)
**Lint:** `uv run ruff check` — All checks passed (4 files)
**Type:** `uv run mypy` — Success: no issues found in 3 source files
**Commit:** `de56c38` — 7 files, +4259 lines, additive only, file-disjoint from all active tasks.

**Delivered:**
- `services/quant_foundry/src/quant_foundry/artifacts.py` — pull-based, hash-verified
  artifact import. `ArtifactRecord` (frozen, extra='forbid'). `verify_artifact_hash`
  (fail closed on mismatch — security event). `import_artifact` (URI scheme allowlist:
  file:// only for MVP; path traversal rejected via `..` segment check; Windows
  drive-letter path handling). `UnsupportedUriError` / `ArtifactHashMismatchError`
  distinct error types. `artifact_content_hash` helper for dossier immutability.
- `services/quant_foundry/src/quant_foundry/dossier.py` — `DossierStatus` StrEnum
  (candidate / research_approved / shadow_approved / paper_approved / rejected).
  `DossierRecord` (frozen, extra='forbid') carrying the full reproducibility set
  (dataset/feature/label hashes, code SHA, lockfile hash, image digest, seeds,
  hardware class) + `trial_count` (for Deflated Sharpe — cross-cutting rigor §2) +
  `blocking_issues` list (sentinel/tournament write into) + evidence refs (by id,
  no code coupling) + `content_hash` immutability key (always recomputed in
  `model_post_init` AND overridden `model_copy` so a copy that changes content
  fields produces a new hash — Pydantic v2's default `model_copy` does NOT re-run
  `model_post_init`). `DossierBuilder` assembles a dossier from an artifact +
  dataset manifest ref + training metadata, pulling reproducibility fields from
  the `ArtifactRecord` so dossier and artifact cannot drift.
- `services/quant_foundry/src/quant_foundry/registry.py` — `DossierRegistry`
  (filesystem JSONL at `<base_dir>/dossier_registry.jsonl`, append-only, fsync,
  restart-safe via JSONL replay, last record per model_id wins). `register`
  idempotent by `(model_id, content_hash)`; rejects same model_id + different
  content as a security event (mirrors outbox/inbox diff-hash invariant from
  TASK-0304). `get(model_id)`, `get_by_hash(content_hash)`, `list(status=)`.
  `add_blocking_issue` append-only (sentinel/tournament write into; hard gate on
  promotion). `registered_at_ns` stamped on first registration.
- `services/quant_foundry/tests/test_dossier.py` — 25 TDD tests covering every
  acceptance criterion: hash verification (match + bad hash + malformed hash),
  artifact import (file scheme success + bad hash rejection + unsupported URI
  scheme rejection + path traversal rejection), ArtifactRecord contract (frozen +
  extra='forbid'), DossierRecord reproducibility set + trial_count + blocking_issues,
  DossierStatus lifecycle enum, DossierBuilder assembly + empty model_id rejection,
  registry register/get/list/get_by_hash, idempotent register, same model_id +
  different content rejection (security event), restart durability, list filter by
  status, add_blocking_issue (append-only + unknown model_id), no secrets in
  records, end-to-end local model gets a dossier.

**Acceptance criteria verification (self):**
- ✅ Existing local model can get a dossier (`test_end_to_end_local_model_gets_dossier`).
- ✅ Mock artifact imports with hash verification (`test_import_artifact_file_scheme_succeeds`).
- ✅ Bad hash is rejected (`test_verify_artifact_hash_rejects_bad_hash` +
  `test_import_artifact_rejects_bad_hash`).
- ✅ Dossier status is visible through read API (`test_registry_list_filters_by_status` +
  `test_registry_get_by_hash` + `test_registry_register_and_get`).
- ✅ Full reproducibility set on every dossier (cross-cutting rigor §3).
- ✅ trial_count for Deflated Sharpe (cross-cutting rigor §2).
- ✅ blocking_issues append-only (sentinel/tournament write into).
- ✅ Immutable by version/hash (content_hash always recomputed).
- ✅ URI scheme allowlist (file:// only; http/https/arbitrary rejected).
- ✅ Path traversal rejected (defense-in-depth).
- ✅ Restart-durable (JSONL replay).
- ✅ No secrets in records (negative tests for token/api_key/secret/password/broker_account/credential).

**Notes for Reviewer:**
- `schemas.ModelDossier` + `schemas.ArtifactManifest` (TASK-0302) intentionally NOT
  modified — shared contract track. The richer `DossierRecord` lives in `dossier.py`
  as a local model, mirroring how Builder 1 kept `SettlementRecord` local in
  `outcomes.py` instead of modifying `schemas.PredictionOutcome`. If the
  Reviewer/Coordinator prefer to unify these later, that's a follow-up that can be
  done without re-registering dossiers (the JSONL records carry all fields needed
  to reconstruct either shape).
- `services/api/src/api/routes/quant_foundry.py` intentionally NOT created —
  TASK-0306 owns the API route. The registry exposes a Python read API only for MVP.
- Dossiers reference settlement evidence and shadow predictions by id/ref, NOT by
  importing `settlement.py` / `shadow_ledger.py` — keeps file-disjoint from
  Builder 1's tracks and avoids coupling the dossier to evidence storage internals.
- `content_hash` is always recomputed (in `model_post_init` and in the overridden
  `model_copy`) so the immutability invariant holds even after Pydantic v2's
  `model_copy` (which does NOT re-run `model_post_init` by default). This is
  pinned by `test_registry_rejects_same_model_id_different_content`.

**File-disjoint confirmation (post-commit):**
- Builder 1 (TASK-0401/0402): `settlement.py`, `outcomes.py`, `metrics.py`,
  `shadow_ledger.py` — zero overlap.
- Builder 2 (TASK-0304/0305): `outbox.py`, `inbox.py`, `mock_dispatcher.py`,
  `callbacks.py` — zero overlap.
- Builder 4 (TASK-0405): `feature_lake.py`, `dataset_manifest.py`,
  `feature_availability.py` — zero overlap.
- Builder 5 (TASK-0203): `services/api/routes/modules.py`, dashboard system page,
  `scripts/modules/` — zero overlap.
- `schemas.py`, `ids.py`, `signatures.py` untouched by me (consumed
  `hash_payload` only from `ids.py`).

**Next:** TASK-0403 unblocks TASK-0404 (Tournament Scoring Skeleton, Order 25,
depends on TASK-0401 + TASK-0403 — both DONE) and TASK-0406 (Leakage and Overfit
Sentinel, Order 26b, depends on TASK-0403 + TASK-0404 + TASK-0405). Available
for adoption if no other builder has claimed them.
