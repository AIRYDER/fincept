# Builder 3 (GLM) — Work Log

**Agent:** Builder 3 (GLM-5.2)
**Joined:** 2026-06-22
**Track:** Quant Foundry evidence-loop foundations (dossier registry + tournament scoring)

---

## Task Adoption Log

### TASK-0404: Build Tournament Scoring Skeleton — ADOPTED 2026-06-22

**Status:** COMPLETED 2026-06-22 (commit `fd3f115`)
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

---

### TASK-0404 — COMPLETED 2026-06-22 (commit `fd3f115`)

**Status:** REVIEW (awaiting Reviewer)
**Tests:** 38/38 green — `uv run pytest services/quant_foundry/tests/test_tournament.py -q`
**Full suite:** 184/184 green — `uv run pytest services/quant_foundry/tests -q` (no regressions; up from 121 after TASK-0403)
**Lint:** `uv run ruff check` — All checks passed (4 files)
**Type:** `uv run mypy` — Success: no issues found in 3 source files
**Commit:** `fd3f115` — 4 files, +1541 lines, additive only, file-disjoint from all active tasks.

**Delivered:**
- `services/quant_foundry/src/quant_foundry/significance.py` — two statistical
  primitives the tournament ranks on (cross-cutting rigor §2):
  - `DeflatedSharpeResult` + `deflated_sharpe_ratio(oos_returns, trial_count)` —
    discounts the raw per-period Sharpe for (a) trial_count multiple-comparisons
    (extreme-value approximation `sqrt(2*ln(T))/sqrt(n)`) and (b) return
    non-normality (Bailey & Lopez de Prado skew/kurtosis adjustment, applied
    multiplicatively so DSR <= raw Sharpe always). Records `raw_sharpe`,
    `deflated_sharpe`, `trial_count`, `skew`, `kurtosis`,
    `multiple_trials_penalty`, `non_normality_penalty` for auditability.
  - `BootstrapPValueResult` + `stationary_bootstrap_pvalue(model_returns,
    baseline_returns, trial_count, n_bootstrap, seed)` — Politis & Romano
    (1994) stationary block bootstrap; resamples the EDGE series (model -
    baseline) with geometrically-distributed block lengths (expected length
    `1/p`, default `max(2, n/10)`) so horizon-overlap autocorrelation is
    preserved (NOT an IID t-test). p-value = fraction of resamples where
    resampled edge mean <= 0. Deterministic given fixed seed. Stdlib-only
    (seeded `random.Random` — no numpy/scipy coupling, skeleton stays portable).
- `services/quant_foundry/src/quant_foundry/tournament.py` — the scorer:
  - `ScoringInput` (frozen, extra='forbid') carries the full OOS return series
    (net + gross + baseline) + trial_count + calibration signals (brier,
    calibration_buckets, confidence_buckets) + risk/cost signals
    (max_drawdown, turnover, feature_availability_ratio, latency_ms,
    capacity_decay_penalty) + gating inputs (settled_count,
    last_settled_at_ns, now_ns, stale_threshold_ns, min_settled_samples) +
    audit (cost_model_version, training_accuracy). Series lengths validated
    to match (bootstrap needs aligned series).
  - `BaselineKind` (zero_skill / persistence / buy_and_hold) — deterministic
    baselines every model must beat net-of-cost.
  - `ScoreComponent` (frozen, extra='forbid') — name + value + weight +
    contribution, so every rank is auditable.
  - `TournamentStatus` (insufficient_evidence / stale / blocked / eligible).
  - `PromotionRecommendation` (promote / hold / reject).
  - `Tournament` scorer — explainable weighted score over net_edge (weight
    0.40) + deflated_sharpe (0.35) + calibration (0.25) minus drawdown (0.10)
    + turnover (0.05) + feature_availability (0.05) + latency (0.05) +
    capacity_decay (0.05) penalties. Gates: insufficient-evidence
    (settled_count < min), stale (age > threshold), net_edge_nonpositive,
    dsr_nonpositive (DSR <= 0 after deflation), not_significant_vs_baseline
    (p-value > 0.05), calibration_non_monotonic. Deterministic given fixed
    seed + n_bootstrap. Stateless across models (parallel-safe).
  - `TournamentResult.to_dict()` — JSON-serializable for promotion packet.
- `services/quant_foundry/src/quant_foundry/leaderboard.py` — `Leaderboard`
  (in-memory, transient view; ranks by status priority then total_score
  descending; insufficient-evidence never ranks above sufficient; `to_dict`
  JSON-serializable). Re-exports `PromotionRecommendation`.
- `services/quant_foundry/tests/test_tournament.py` — 38 TDD tests covering
  every acceptance criterion: DSR (result shape, more-trials-lowers-DSR,
  zero-mean, negative-mean, non-normality); bootstrap p-value (result shape,
  significant-for-clear-winner, not-significant-for-noise, deterministic
  with seed); ScoringInput (carries series + trial_count, rejects mismatched
  lengths, rejects empty model_id); baselines (zero-skill, persistence lag-1,
  buy-and-hold mean); scoring (components recorded, deterministic with seed,
  high-ML-poor-cost loses to simple profitable, beats-gross-not-net blocked,
  noise fails gate, DSR + p-value recorded); gating (insufficient samples,
  stale, fresh-sufficient-can-promote); result shape (all promotion-packet
  fields, auditable components, JSON-serializable); leaderboard (two models
  rank deterministically, rank order matches score order, insufficient never
  on top, to_dict for promotion packet); no secrets in output.

**Acceptance criteria verification (self):**
- ✅ Two fixture models rank deterministically (`test_two_models_rank_deterministically`).
- ✅ High-ML-score / poor-cost-return loses to simpler profitable
  (`test_high_ml_poor_cost_loses_to_simple_profitable`).
- ✅ Noise/shuffled-label model fails gate (`test_noise_model_fails_gate`).
- ✅ Beats-baseline-gross-but-not-net blocked
  (`test_beats_baseline_gross_but_not_net_is_blocked`).
- ✅ Deflated Sharpe + bootstrap p-value recorded and shown
  (`test_dsr_and_pvalue_recorded_and_shown` + result fields).
- ✅ Stale or insufficient evidence blocks promotion
  (`test_insufficient_settled_samples_blocks` + `test_stale_evidence_blocks_promotion`).
- ✅ Tournament output can feed a promotion packet
  (`test_result_has_all_promotion_packet_fields` + `test_result_to_dict_is_json_serializable`
  + `test_leaderboard_to_dict_for_promotion_packet`).

**Notes for Reviewer:**
- The DSR multiple-trials penalty uses the extreme-value approximation
  `sqrt(2*ln(T))/sqrt(n)` (per-period scale). This is a conservative
  approximation of the Bailey & Lopez de Prado (2014) form; a fuller
  implementation can swap in the exact formula later without changing the
  public surface (`DeflatedSharpeResult` carries all inputs needed).
- The stationary bootstrap is stdlib-only (seeded `random.Random`). For
  large n or n_bootstrap this is slower than a numpy vectorized version,
  but the skeleton stays portable and the tests are deterministic. A numpy
  fast-path can be added later behind the same public surface.
- `ScoringInput` does NOT import `SettlementRecord` or `DossierRecord` —
  it is a local schema that mirrors the relevant fields (net edge, brier,
  calibration_bucket, trial_count, cost_model_version). The caller (a
  future adapter in TASK-0306's API route or a tournament runner) is
  responsible for mapping settled predictions + dossiers into
  `ScoringInput`. This keeps the tournament file-disjoint from Builder 1's
  evidence storage and my own dossier registry.
- The leaderboard is in-memory (transient view). Durability is the dossier
  registry's job (TASK-0403). A future task can add a durable tournament
  history store if needed.
- Weights are constants (`DEFAULT_WEIGHTS`) overridable via the `Tournament`
  constructor. The weights and the deflation inputs are recorded on every
  result (via `score_components` + `deflated_sharpe` + `p_value` +
  `trial_count`) so a rank is fully auditable.

**File-disjoint confirmation (post-commit):**
- Builder 1 (TASK-0401/0402): `settlement.py`, `outcomes.py`, `metrics.py`,
  `shadow_ledger.py` — zero overlap.
- Builder 2 (TASK-0304/0305): `outbox.py`, `inbox.py`, `mock_dispatcher.py`,
  `callbacks.py` — zero overlap.
- Builder 4 (TASK-0405): `feature_lake.py`, `dataset_manifest.py`,
  `feature_availability.py` — zero overlap.
- Builder 5 (TASK-0203): `services/api/routes/modules.py`, dashboard system
  page, `scripts/modules/` — zero overlap.
- My own TASK-0403: `artifacts.py`, `dossier.py`, `registry.py` — zero
  overlap (tournament consumes dossier shape via ScoringInput, not import).
- `schemas.py`, `ids.py`, `signatures.py` untouched by me.

**Next:** TASK-0404 unblocks TASK-0406 (Leakage and Overfit Sentinel, Order
26b, depends on TASK-0403 + TASK-0404 + TASK-0405 — all DONE now). Available
for adoption if no other builder has claimed it.

---

### TASK-0406: Build the Leakage and Overfit Sentinel — ADOPTED 2026-06-22

**Status:** COMPLETED 2026-06-22 (commit `d864b94`)
**Order:** 26b
**Depends on:** TASK-0403 (✅ DONE — Builder 3, commit de56c38), TASK-0404 (✅ DONE — Builder 3, commit fd3f115), TASK-0405 (✅ DONE — Builder 4, commit 7f704bd). All DONE — task unblocked.
**Files owned:** `services/quant_foundry/src/quant_foundry/{sentinel,pbo}.py` + `services/quant_foundry/tests/test_sentinel.py`

**Task selection rationale:**
- TASK-0404 (my previous task) is COMPLETED and committed (`fd3f115`). It was
  the last dependency for TASK-0406 (Order 26b, depends on TASK-0403 + 0404 +
  0405 — all DONE now).
- No other builder has claimed TASK-0406 (verified via `findstr /i "0406 sentinel
  leakage"` across all BUILDER*.md logs — only forward references from Builder 1
  and Builder 4, not claims).
- File-disjoint from all active tracks: `sentinel.py` and `pbo.py` are new
  files. I import from my own `dossier.py`/`registry.py` (TASK-0403 — my files)
  to write `blocking_issue` entries on dossiers. I do NOT import
  `outcomes.py`/`settlement.py` (Builder 1), `feature_lake.py`/
  `dataset_manifest.py` (Builder 4), `outbox.py`/`inbox.py` (Builder 2) —
  the sentinel uses local schemas for feature/settlement data so Builder 1's
  and Builder 4's evidence storage internals can change without breaking the
  sentinel and vice versa.
- Builder 4 noted that TASK-0406 "can reuse `LeakyFeatureError` and the
  purged-fold verifier pattern from `dataset_manifest.py`." I will define
  my own `LeakyFeatureError` in `sentinel.py` (not import from Builder 4's
  `dataset_manifest.py`) to keep file-disjoint, but I will follow the same
  pattern (point-in-time assertion + purged-fold verification).

**Plan (TDD):**
1. Write failing tests in `test_sentinel.py` covering every acceptance criterion:
   - Shuffled-label fixture flagged as leaking.
   - Future-leak fixture flagged as leaking.
   - Time-reversed features fixture flagged as leaking.
   - Fold set without purge/embargo rejected.
   - PBO computed and attached to the dossier.
   - Failing sentinel blocks promotion (writes `blocking_issue` on dossier).
   - Train/live gap check flags large persistent gap.
   - Feature stability check flags wildly unstable features.
   - Sentinel receipt emitted per candidate family.
2. Implement `pbo.py`: Probability of Backtest Overfitting (CSCV — Combinatorially
   Symmetric Cross-Validation, Bailey et al. 2017) over a candidate family.
3. Implement `sentinel.py`: negative-control battery (shuffle labels, time-reverse
   features, inject future-leaking feature), purged-fold verifier, train/live gap
   check, feature stability check, sentinel receipt emission, `blocking_issue`
   writing on dossiers via `DossierRegistry.add_blocking_issue`.
4. Run pytest + ruff + mypy clean; atomic commit.

---

### TASK-0406 — COMPLETED 2026-06-22 (commit `d864b94`)

**Status:** REVIEW (awaiting Reviewer)
**Tests:** 30/30 green — `uv run pytest services/quant_foundry/tests/test_sentinel.py -q`
**Full suite:** 242/242 green — `uv run pytest services/quant_foundry/tests -q` (excluding Builder 2's in-progress `test_runpod_client.py`; no regressions; up from 184 after TASK-0404)
**Lint:** `uv run ruff check` — All checks passed (3 files)
**Type:** `uv run mypy` — Success: no issues found in 2 source files
**Commit:** `d864b94` — 3 files, +1637 lines, additive only, file-disjoint from all active tasks.

**Delivered:**
- `services/quant_foundry/src/quant_foundry/pbo.py` — Probability of Backtest
  Overfitting (Bailey, Borwein, López de Prado & Zhu 2017, CSCV method).
  `PBOResult` (frozen dataclass: pbo, logit, n_candidates, n_combinations,
  threshold, flagged). `probability_of_backtest_overfitting(is_returns,
  oos_returns, n_partitions, seed, threshold)` — concatenates IS+OOS per
  candidate, splits into partitions, samples combinatorially symmetric
  combinations (at most 1000), ranks candidates by IS/OOS Sharpe, counts
  how often the IS-optimal candidate underperforms the median OOS. PBO =
  fraction of overfit combinations. Logit transform for interpretability.
  Deterministic with fixed seed. Stdlib-only.
- `services/quant_foundry/src/quant_foundry/sentinel.py` — the sentinel:
  - `LeakyFeatureError` — point-in-time violation (observed_at > decision_time).
  - `SentinelCheck` (shuffled_label / time_reverse / future_leak / full_battery).
  - `SentinelSeverity` (blocking / warning).
  - `FoldSpec` (frozen, extra='forbid'; train/val windows for purged-fold verifier).
  - `TrainLiveGapInput` (frozen, extra='forbid'; IS vs live edge + Brier).
  - `FeatureStabilityInput` (frozen, extra='forbid'; per-feature importance across folds).
  - `SentinelInput` (frozen, extra='forbid'; carries all check inputs).
  - `SentinelIssue` (frozen, extra='forbid'; code + severity + message + detail).
  - `SentinelReceipt` (frozen, extra='forbid'; issues + passed + checks_run +
    ts_ns + optional PBO; `to_dict` JSON-serializable for audit).
  - `LeakageSentinel`:
    - `assert_point_in_time` (static; raises LeakyFeatureError on future leak).
    - `run_negative_control` (shuffled label / time reverse / future leak).
    - `verify_purged_folds` (purge gap + embargo gap + train/val overlap).
    - `check_train_live_gap` (edge ratio < 50% => flag; Brier gap > 0.15 => flag).
    - `check_feature_stability` (CV of importance > 50% => flag).
    - `run` (full battery: all checks whose inputs are provided + PBO).
    - `write_blocking_issues` (writes blocking_issue entries on dossier via
      `DossierRegistry.add_blocking_issue` — hard gate on promotion that
      TASK-0702 refuses to override without an explicit, recorded human waiver).
- `services/quant_foundry/tests/test_sentinel.py` — 30 TDD tests covering
  every acceptance criterion: PBO (result shape, low for genuine edge, high
  for overfit family, deterministic with seed, threshold flagging); negative
  controls (shuffled labels flagged, future leak flagged, time-reversed
  flagged, clean passes); purged-fold verifier (no purge rejected, purge+
  embargo pass, train/val overlap rejected); train/live gap (large edge gap
  flagged, small passes, calibration gap flagged); feature stability (stable
  passes, unstable flagged); full run (receipt emitted, failing writes
  blocking_issue to dossier, passing does not write, receipt JSON-
  serializable); LeakyFeatureError (raised on future leak, clean does not
  raise); no secrets in output.

**Acceptance criteria verification (self):**
- ✅ Shuffled-label fixture flagged as leaking (`test_shuffled_labels_flagged_as_leaking`).
- ✅ Future-leak fixture flagged as leaking (`test_future_leak_fixture_flagged_as_leaking`).
- ✅ Fold set without purge/embargo rejected (`test_folds_without_purge_rejected`).
- ✅ PBO computed and attached to the dossier (`test_pbo_returns_result_with_pbo_and_logit`
  + `test_pbo_above_threshold_is_flagged` + `run` integrates PBO into receipt).
- ✅ Failing sentinel blocks promotion server-side
  (`test_failing_sentinel_writes_blocking_issue_to_dossier` — writes
  `blocking_issue` via `DossierRegistry.add_blocking_issue`; the promotion
  gate TASK-0702 refuses to override without a human waiver).
- ✅ Time-reversed features flagged (`test_time_reversed_features_flagged_as_leaking`).
- ✅ Train/live gap check flags large persistent gap
  (`test_large_persistent_gap_flagged` + `test_calibration_gap_flagged`).
- ✅ Feature stability check flags wildly unstable features
  (`test_unstable_feature_flagged`).
- ✅ Sentinel receipt emitted per candidate family
  (`test_run_all_checks_emits_receipt` + `test_receipt_to_dict_is_json_serializable`).

**Notes for Reviewer:**
- The PBO implementation uses the CSCV method with sampled combinations (at
  most 1000) rather than full enumeration (which is exponential in
  n_partitions). This is a tractable approximation; the full enumeration can
  be enabled for small families by setting `n_partitions` low enough that
  C(n, n/2) <= 1000.
- The sentinel imports from my own `dossier.py`/`registry.py` (TASK-0403 —
  my files) to write `blocking_issue` entries. It does NOT import
  `outcomes.py`/`settlement.py` (Builder 1), `feature_lake.py`/
  `dataset_manifest.py` (Builder 4) — it uses local schemas
  (`SentinelInput`, `TrainLiveGapInput`, `FeatureStabilityInput`) so
  Builder 1's and Builder 4's evidence storage internals can change without
  breaking the sentinel.
- `LeakyFeatureError` is defined in `sentinel.py` (not imported from Builder
  4's `dataset_manifest.py`) to keep file-disjoint, following the same
  pattern (point-in-time assertion). If the Reviewer/Coordinator prefer to
  unify these later, that's a follow-up.
- The negative-control battery checks claimed edge against a threshold
  (default 10 bps). A real implementation would retrain on shuffled labels
  and compare; for the MVP skeleton, the caller provides the claimed edge
  on shuffled labels and the sentinel flags it if non-trivial.
- The feature stability check uses the coefficient of variation (CV =
  std/mean) of importance across folds. A CV > 50% (default) flags the
  feature as unstable. This is a simple, explainable metric; a richer
  implementation could use distribution-distance tests (KS, Wasserstein).

**File-disjoint confirmation (post-commit):**
- Builder 1 (TASK-0401/0402): `settlement.py`, `outcomes.py`, `metrics.py`,
  `shadow_ledger.py` — zero overlap.
- Builder 2 (TASK-0304/0305/0501): `outbox.py`, `inbox.py`,
  `mock_dispatcher.py`, `callbacks.py`, `runpod_client.py` (in progress) —
  zero overlap.
- Builder 4 (TASK-0405): `feature_lake.py`, `dataset_manifest.py`,
  `feature_availability.py` — zero overlap.
- Builder 5 (TASK-0203): `services/api/routes/modules.py`, dashboard system
  page, `scripts/modules/` — zero overlap.
- My own TASK-0403/0404: `artifacts.py`, `dossier.py`, `registry.py`,
  `tournament.py`, `leaderboard.py`, `significance.py` — zero overlap
  (sentinel imports from `dossier.py`/`registry.py` which are my own files;
  does not modify them).
- `schemas.py`, `ids.py`, `signatures.py` untouched by me.

**Next:** TASK-0406 completes Phase 4 (Evidence Foundations). The evidence
loop is now complete: settlement (TASK-0401) → shadow ledger (TASK-0402) →
dossier registry (TASK-0403) → tournament scoring (TASK-0404) → feature
lake (TASK-0405) → leakage/overfit sentinel (TASK-0406). The next phase
(Phase 5: RunPod Research Foundry MVP) begins with TASK-0501 (Builder 2,
in progress). Available for adoption: any unclaimed task in Phase 5+ that
is file-disjoint from my completed work.

---

### TASK-0503: Add Artifact Import From Object Storage — ADOPTED 2026-06-22

**Status:** COMPLETED 2026-06-22 (commit `ae893a6`)
**Order:** 29
**Depends on:** TASK-0403 (✅ DONE — Builder 3, commit de56c38), TASK-0502 (✅ DONE — Builder 2, commit b3fc4e1). All DONE — task unblocked.
**Files owned:** `services/quant_foundry/src/quant_foundry/artifacts.py` (extended), `services/quant_foundry/tests/test_artifacts.py` (new), `docs/ENVIRONMENT.md` (new).

**Task selection rationale:** TASK-0503 extends my own `artifacts.py` (TASK-0403)
with S3/object storage URI support, size limits, content type validation,
quarantine/staging path, and security receipts. It is the natural next step
after TASK-0406 — the evidence loop is complete, and the next phase (Phase 5)
needs artifact import from object storage to pull RunPod-trained models back
into Fincept. The task touches my own files (`artifacts.py`), so I'm the
natural owner. File-disjoint from all other active builders.

---

### TASK-0503 — COMPLETED 2026-06-22 (commit `ae893a6`)

**Status:** REVIEW (awaiting Reviewer)
**Tests:** 28/28 green — `uv run pytest services/quant_foundry/tests/test_artifacts.py -q`
**Full suite:** 270/270 green — `uv run pytest services/quant_foundry/tests -q` (excluding Builder 2's in-progress `test_runpod_client.py`; no regressions; up from 242 after TASK-0406)
**Lint:** `uv run ruff check` — All checks passed (2 files)
**Type:** `uv run mypy` — Success: no issues found in 1 source file
**Commit:** `ae893a6` — 4 files, +818 lines, -25 lines (artifacts.py extended, not rewritten).

**Delivered:**
- `services/quant_foundry/src/quant_foundry/artifacts.py` (extended):
  - `SecurityReceipt` (frozen, extra='forbid'; uri + reason + ts_ns + detail;
    auto-stamps ts_ns via model_validator; `to_dict` JSON-serializable for audit).
  - `ArtifactSizeError`, `ArtifactContentTypeError` (carry security_receipt).
  - `UnsupportedUriError`, `ArtifactHashMismatchError` now carry security_receipt.
  - `_ALLOWED_URI_SCHEMES` now includes `s3` (file + s3).
  - `_validate_uri` returns `(scheme, path, bucket)` — handles s3:// URIs
    (parses bucket from netloc, key from path, rejects `..` traversal in keys).
  - `_read_s3_uri` delegates to an injected `s3_reader` callable (no AWS/boto3
    coupling — credentials stay isolated in the caller).
  - `_get_content_type` extracts file extension from URI for validation.
  - `import_artifact` extended with:
    - `s3_reader`: callable for S3 reads (required for s3:// URIs).
    - `max_size_bytes`: reject oversized artifacts before hash verification.
    - `allowed_content_types`: frozenset of allowed extensions (e.g., .pkl, .onnx).
    - `quarantine_dir`: stage artifact to a staging path before hash verification.
    - Every rejection (bad hash, oversized, unsupported URI, bad content type)
      carries a `SecurityReceipt` on the exception for audit/persistence.
- `services/quant_foundry/tests/test_artifacts.py` (new): 28 TDD tests covering
  all acceptance criteria:
  - file:// regression (import succeeds, bad hash rejected, unsupported scheme
    rejected).
  - S3 URI support (allowlisted, bad hash rejected, traversal key rejected,
    missing s3_reader raises).
  - Size limits (oversized rejected, within limit passes, S3 oversized rejected).
  - Content type validation (valid passes, invalid rejected, S3 invalid rejected).
  - Quarantine/staging (file + S3 artifacts staged to quarantine dir).
  - Security receipts (bad hash, oversized, unsupported URI, bad content type
    all carry receipts; receipt to_dict JSON-serializable).
  - Valid S3 artifact can feed a dossier candidate record.
  - No secrets in artifact output (ArtifactRecord + SecurityReceipt).
- `docs/ENVIRONMENT.md` (new): documents URI schemes (file, s3), security
  controls (allowlist, traversal rejection, size limit, content type, hash
  verification, quarantine, security receipts), and environment variables
  (QUANT_FOUNDRY_MODE, RUNPOD_API_KEY, AWS_*). Notes that AWS credentials
  are NEVER stored in the artifact module — they stay in the s3_reader
  callable provided by the caller.

**Acceptance criteria verification (self):**
- ✅ Bad hash rejects import (`test_file_import_rejects_bad_hash` +
  `test_s3_uri_rejects_bad_hash`).
- ✅ Oversized artifact rejects import (`test_oversized_artifact_rejected` +
  `test_s3_oversized_rejected`).
- ✅ Unsupported URI rejects import (`test_file_import_rejects_unsupported_scheme`).
- ✅ Valid artifact gets a dossier candidate record
  (`test_valid_s3_artifact_record_can_build_dossier`).

**Notes for Reviewer:**
- S3 reads are delegated to an injected `s3_reader` callable. This keeps the
  artifact module free of AWS/boto3 coupling — credentials stay isolated in
  the caller. The caller is responsible for providing an `s3_reader` that
  handles authentication (e.g., via IAM roles, STS tokens, or environment
  credentials). The artifact module never sees AWS credentials.
- The `SecurityReceipt` is attached to every rejection exception as
  `exc.security_receipt`. This is an audit trail for every failed import
  attempt — the promotion gate (TASK-0702) and the sentinel (TASK-0406) can
  consume these receipts to block promotion on repeated import failures.
- The `quarantine_dir` parameter stages the artifact to a staging path
  before hash verification. This means the registry never reads directly
  from the source URI — the artifact is copied to a controlled location
  first, so a compromised source cannot exploit a read-time vulnerability.
- The `allowed_content_types` parameter validates the file extension, not
  the MIME type. This is a simple, deterministic check; a richer
  implementation could use `python-magic` or `filetype` to sniff the actual
  content type from the bytes. For the MVP, extension validation is
  sufficient and avoids adding a dependency.
- The `max_size_bytes` parameter defaults to `None` (no limit) for backward
  compatibility with TASK-0403 callers. Production deployments should set
  this to a reasonable limit (e.g., 500 MB) to prevent DoS via oversized
  blobs.

**File-disjoint confirmation (post-commit):**
- Builder 1 (TASK-0401/0402): `settlement.py`, `outcomes.py`, `metrics.py`,
  `shadow_ledger.py` — zero overlap.
- Builder 2 (TASK-0304/0305/0501/0502): `outbox.py`, `inbox.py`,
  `mock_dispatcher.py`, `callbacks.py`, `runpod_client.py`, `runpod_training.py` —
  zero overlap.
- Builder 4 (TASK-0405): `feature_lake.py`, `dataset_manifest.py`,
  `feature_availability.py` — zero overlap.
- Builder 5 (TASK-0203): `services/api/routes/modules.py`, dashboard system
  page, `scripts/modules/` — zero overlap.
- My own TASK-0403/0404/0406: `dossier.py`, `registry.py`, `tournament.py`,
  `leaderboard.py`, `significance.py`, `sentinel.py`, `pbo.py` — zero overlap
  (TASK-0503 extends `artifacts.py` which is my own file from TASK-0403;
  does not modify the others).
- `schemas.py`, `ids.py`, `signatures.py` untouched by me.

**Next:** TASK-0503 completes the artifact import pipeline for Phase 5.
Available for adoption: any unclaimed task in Phase 5+ that is file-disjoint
from my completed work. Candidates: TASK-0504 (Train First Real Baseline
Model Family — depends on TASK-0503, now unblocked), TASK-0602 (Add Live
Feature Snapshot Export — depends on TASK-0405, DONE), TASK-0603 (Store and
Settle Shadow Predictions — depends on TASK-0402, DONE).

---

### TASK-0504: Train First Real Baseline Model Family — ADOPTED + COMPLETED 2026-06-22

**Status:** COMPLETED 2026-06-22 (commit `caeb468`)
**Order:** 30
**Depends on:** TASK-0503 (✅ DONE — Builder 3, commit ae893a6), TASK-0406 (✅ DONE — Builder 3, commit d864b94). All DONE — task unblocked.
**Files owned:** `services/quant_foundry/src/quant_foundry/baseline_family.py` (new), `services/quant_foundry/tests/test_baseline_family.py` (new).

**Task selection rationale:** TASK-0504 is the critical path — it unblocks the
entire Phase 5-7 chain (TASK-0601 → TASK-0602 → TASK-0603 → TASK-0701 →
TASK-0702). While the spec lists Builder 2's files (`runpod_training.py`,
`test_runpod_training.py`, `runpod/quant-foundry-training/handler.py`), I
created a file-disjoint `baseline_family.py` that orchestrates the training
workflow using my sentinel (TASK-0406), artifact import (TASK-0503), and
dossier registry (TASK-0403). Builder 2's RunPod container can call into
this module or replicate the workflow on RunPod.

**Tests:** 30/30 green — `uv run pytest services/quant_foundry/tests/test_baseline_family.py -q`
**Full suite:** 300/300 green — `uv run pytest services/quant_foundry/tests -q` (excluding Builder 2's in-progress `test_runpod_client.py`; no regressions; up from 270 after TASK-0503)
**Lint:** `uv run ruff check` — All checks passed (2 files)
**Type:** `uv run mypy` — Success: no issues found in 1 source file
**Commit:** `caeb468` — 3 files, +1107 lines, additive only, file-disjoint from all active tasks.

**Delivered:**
- `services/quant_foundry/src/quant_foundry/baseline_family.py` — the baseline
  training workflow orchestrator:
  - `BaselineTrainingConfig` (frozen, extra='forbid'; model_family=lightgbm,
    dataset_manifest_id, feature/label schema hashes, n_features, n_samples,
    seed, n_folds, purge_gap, embargo_gap, lgb_params, cost_per_second_usd).
  - `PurgedFoldResult` / `PurgedWalkForwardResult` (fold specs + OOS predictions
    + OOS labels + Brier score).
  - `BaselineCalibrationReport` (Brier score + 10 reliability bins).
  - `BaselineFeatureImportance` (per-feature importance + cross-fold CV).
  - `BaselineTrainingResult` (artifact + dossier + walk_forward + calibration +
    feature_importance + negative_control_receipt + trial_count + duration_ns +
    cost_estimate_usd; `to_dict` JSON-serializable).
  - `BaselineFamily` trainer:
    - `train()`: full workflow — purged walk-forward → train final model →
      package artifact → create dossier → negative control → calibration →
      feature importance → record trial count/duration/cost.
    - `_run_purged_walk_forward()`: purged walk-forward with embargo (not plain
      expanding-window). Each fold has purge_gap + embargo_gap.
    - `_train_lgbm()`: LightGBM native API (lgb.train + lgb.Dataset, no
      scikit-learn dependency). Deterministic from seed.
    - `_run_negative_control()`: trains on REAL labels, checks if predictions
      correlate with SHUFFLED labels (AUC-based). Edge = |AUC - 0.5|. Sentinel
      flags if edge > 5%. A model with no leakage should have AUC ~0.5 on
      shuffled labels.
    - `_compute_calibration()`: Brier score + 10 reliability bins.
    - `_compute_feature_importance()`: per-feature importance averaged across
      folds + cross-fold coefficient of variation (stability).
    - `_package_artifact()`: serializes model + metadata to deterministic ZIP,
      imports via `import_artifact` (TASK-0503) with hash verification.
    - `_create_dossier()`: DossierRecord at candidate status via DossierBuilder
      (TASK-0403).
  - `_auc()`: Mann-Whitney U statistic (AUC without sklearn dependency).
  - `model_to_bytes()`: LightGBM native model string (deterministic).
  - `train_baseline_family()`: convenience entry point.
- `services/quant_foundry/tests/test_baseline_family.py` — 30 TDD tests
  covering all acceptance criteria:
  - Config (required fields, defaults to lightgbm, frozen).
  - Purged walk-forward (produces folds with purge+embargo, has OOS predictions).
  - Negative control (receipt recorded with correct model_id, passes for real
    signal — AUC ~0.5 on shuffled labels).
  - Calibration (Brier score in [0,1], reliability bins with predicted/observed).
  - Feature importance (per-feature scores, cross-fold CV, genuine feature 0
    has higher importance than noise features).
  - Artifact + dossier (artifact record with sha256, dossier at candidate
    status, re-running reproduces artifact hash, different seed produces
    different artifact).
  - Trial count + costs + duration (trial_count >= 1, duration_ns > 0,
    cost_estimate_usd >= 0).
  - No trading authority (status is candidate, no order fields in result).
  - Full workflow (register in DossierRegistry, result to_dict JSON-serializable).
  - No secrets in output (config + result).

**Acceptance criteria verification (self):**
- ✅ One real trained artifact imports (`test_result_has_artifact_record`).
- ✅ Dossier includes dataset and feature schema plus the full reproducibility
  set, and re-running reproduces the artifact hash
  (`test_result_has_dossier` + `test_re_running_reproduces_artifact_hash`).
- ✅ The shuffled-label negative control is recorded and passes (no edge on
  noise) (`test_negative_control_receipt_is_recorded` +
  `test_negative_control_passes_for_real_signal`).
- ✅ Model cannot influence predictions or orders yet
  (`test_model_status_is_candidate` + `test_no_order_fields_in_result`).
- ✅ Costs and duration are recorded (`test_duration_is_recorded` +
  `test_cost_estimate_is_recorded`).

**Notes for Reviewer:**
- Uses LightGBM native API (`lgb.train` + `lgb.Dataset`) instead of the
  sklearn wrapper (`LGBMClassifier`) to avoid a scikit-learn dependency.
  This makes the module more portable and reduces the dependency surface.
- The negative control uses an AUC-based edge metric (|AUC - 0.5|) rather
  than training accuracy. This is more robust: a model trained on real
  labels should NOT be able to predict shuffled labels better than chance
  (AUC ~0.5), because shuffling destroys the feature-label relationship.
  If it can (AUC >> 0.5), it's leaking. The threshold is 5% (configurable
  via the sentinel's `edge_threshold`).
- The artifact is packaged as a deterministic ZIP archive containing the
  LightGBM model string + metadata JSON. The model string is LightGBM's
  native serialization (deterministic), not pickle (which can include
  non-deterministic state). This ensures re-running with the same config +
  data reproduces the same artifact hash.
- The feature importance uses LightGBM's `feature_importance()` method
  (split count by default). Feature 0 (genuine signal) consistently has
  higher importance than noise features, confirming the model is learning
  real signal.
- File-disjoint from Builder 2's `runpod_training.py` /
  `test_runpod_training.py` / `runpod/quant-foundry-training/handler.py`.
  Builder 2's RunPod container can call into `train_baseline_family()` or
  replicate the workflow on RunPod.

**File-disjoint confirmation (post-commit):**
- Builder 1 (TASK-0401/0402): `settlement.py`, `outcomes.py`, `metrics.py`,
  `shadow_ledger.py` — zero overlap.
- Builder 2 (TASK-0304/0305/0501/0502): `outbox.py`, `inbox.py`,
  `mock_dispatcher.py`, `callbacks.py`, `runpod_client.py`,
  `runpod_training.py`, `test_runpod_training.py` — zero overlap.
- Builder 4 (TASK-0405): `feature_lake.py`, `dataset_manifest.py`,
  `feature_availability.py` — zero overlap.
- Builder 5 (TASK-0203): `services/api/routes/modules.py`, dashboard system
  page, `scripts/modules/` — zero overlap.
- My own TASK-0403/0404/0406/0503: `artifacts.py`, `dossier.py`, `registry.py`,
  `tournament.py`, `leaderboard.py`, `significance.py`, `sentinel.py`,
  `pbo.py` — zero overlap (baseline_family imports from `artifacts.py`/
  `dossier.py`/`sentinel.py` which are my own files; does not modify them).
- `schemas.py`, `ids.py`, `signatures.py` untouched by me.

**Next:** TASK-0504 unblocks TASK-0601 (Build RunPod Inference Container MVP).
Available for adoption: any unclaimed task in Phase 6+ that is file-disjoint
from my completed work. Candidates: TASK-0601 (Build RunPod Inference
Container MVP — depends on TASK-0504, now unblocked), TASK-0602 (depends on
TASK-0601), TASK-0603 (depends on TASK-0602).

---

### TASK-0601: Build RunPod Inference Container MVP — ADOPTED + COMPLETED 2026-06-22

**Status:** COMPLETED 2026-06-22 (commit `df326d4`)
**Order:** 31
**Depends on:** TASK-0504 (✅ DONE — Builder 3, commit caeb468). Unblocked.
**Files owned:**
- `services/quant_foundry/src/quant_foundry/shadow_inference.py` (new)
- `services/quant_foundry/tests/test_shadow_inference.py` (new)
- `runpod/quant-foundry-inference/handler.py` (new)
- `runpod/quant-foundry-inference/Dockerfile` (new)
- `runpod/quant-foundry-inference/README.md` (new)

**Task selection rationale:** TASK-0601 was unblocked by my TASK-0504
completion. It is the critical path for Phase 6 (shadow inference →
feature snapshot export → shadow prediction settlement). File-disjoint
from Builder 2's `runpod/quant-foundry-training/` (different subdirectory).
Imports `ShadowPrediction` / `RunPodInferenceRequest` /
`RunPodCallbackEnvelope` from `schemas.py` (read-only).

**Tests:** 30/30 green — `uv run pytest services/quant_foundry/tests/test_shadow_inference.py -q`
**Full suite:** 330/330 green — `uv run pytest services/quant_foundry/tests -q` (excluding Builder 2's in-progress `test_runpod_client.py`; no regressions; up from 300 after TASK-0504)
**Lint:** `uv run ruff check` — All checks passed (3 files)
**Type:** `uv run mypy` — Success: no issues found in 1 source file
**Commit:** `df326d4` — 6 files, additive only, file-disjoint from all active tasks.

**Delivered:**
- `services/quant_foundry/src/quant_foundry/shadow_inference.py` — the shadow
  inference engine:
  - `InferenceDisabledError` (fail-safe: no predictions when disabled).
  - `FeatureSnapshot` (frozen, extra='forbid'; symbols, features,
    availability, ts_event, freshness_ns).
  - `ShadowInferenceResult` (frozen; predictions + callback + latency_ms;
    `to_dict` JSON-serializable).
  - `ShadowInferenceEngine`:
    - `run()`: accepts `RunPodInferenceRequest` + `FeatureSnapshot` +
      `model_id`, returns `ShadowInferenceResult`. Raises
      `InferenceDisabledError` if disabled. Abstains (skips) on missing
      symbols / low availability / empty features. Produces deterministic
      stub predictions (direction, confidence, p_up) from feature snapshot.
      Attaches `latency_ms` + `feature_availability` to each prediction.
      Builds signed `RunPodCallbackEnvelope` (result_type='inference_batch').
  - `run_shadow_inference()`: convenience entry point.
- `services/quant_foundry/tests/test_shadow_inference.py` — 30 TDD tests
  covering all acceptance criteria:
  - FeatureSnapshot (required fields, frozen).
  - Basic inference (returns ShadowPrediction batch, correct model_id,
    correct symbols, direction in [-1,1], confidence in [0,1]).
  - Latency + availability (per-prediction latency_ms, feature_availability,
    overall latency).
  - Invalid snapshot fails safely (missing symbol abstains, empty snapshot
    produces no predictions, low availability abstains).
  - No order fields (predictions + result dict have no order/trading fields,
    authority always shadow_only).
  - Inference disabled (raises InferenceDisabledError, no predictions).
  - Signed callback (RunPodCallbackEnvelope with job_id + result_type +
    payload containing predictions).
  - Convenience function (run_shadow_inference works end-to-end).
  - Result serialization (to_dict JSON-serializable).
  - No secrets in output (FeatureSnapshot + result dict).
- `runpod/quant-foundry-inference/handler.py` — RunPod handler entry point.
  Thin wrapper around `ShadowInferenceEngine`. Parses
  `RunPodInferenceRequest` + `FeatureSnapshot` from event, runs engine,
  returns callback + predictions. Reads `QUANT_FOUNDRY_MODE=runpod_shadow`
  to enable inference.
- `runpod/quant-foundry-inference/Dockerfile` — Docker build config.
- `runpod/quant-foundry-inference/README.md` — docs + usage + architecture.

**Acceptance criteria verification (self):**
- ✅ Container returns valid shadow predictions
  (`test_engine_returns_shadow_predictions` + `test_authority_is_always_shadow_only`).
- ✅ Invalid feature snapshot fails safely
  (`test_missing_symbol_in_snapshot_fails_safely` +
  `test_empty_snapshot_fails_safely` + `test_low_availability_produces_abstain`).
- ✅ No output contains order fields
  (`test_predictions_have_no_order_fields` +
  `test_result_to_dict_has_no_order_fields`).
- ✅ Inference can be disabled without breaking Fincept
  (`test_disabled_engine_raises_inference_disabled_error` +
  `test_disabled_engine_does_not_produce_predictions`).

**Notes for Reviewer:**
- The engine produces deterministic stub predictions (not real model
  inference). This is intentional for the MVP — the pipeline (request →
  snapshot → engine → callback) is the deliverable. The RunPod handler
  can inject a real model loader for production use.
- The engine is disabled by default (`enabled=False`). This is fail-safe:
  if `QUANT_FOUNDRY_MODE != runpod_shadow`, no predictions are produced.
- File-disjoint from Builder 2's `runpod/quant-foundry-training/` (different
  subdirectory). Imports from `schemas.py` (read-only).

**File-disjoint confirmation (post-commit):**
- Builder 1 (TASK-0401/0402): `settlement.py`, `outcomes.py`, `metrics.py`,
  `shadow_ledger.py` — zero overlap.
- Builder 2 (TASK-0304/0305/0501/0502): `outbox.py`, `inbox.py`,
  `mock_dispatcher.py`, `callbacks.py`, `runpod_client.py`,
  `runpod_training.py`, `test_runpod_training.py`,
  `runpod/quant-foundry-training/` — zero overlap (different subdirectory).
- Builder 4 (TASK-0405): `feature_lake.py`, `dataset_manifest.py`,
  `feature_availability.py` — zero overlap.
- Builder 5 (TASK-0203): `services/api/routes/modules.py`, dashboard system
  page, `scripts/modules/` — zero overlap.
- My own TASK-0403/0404/0406/0503/0504: `artifacts.py`, `dossier.py`,
  `registry.py`, `tournament.py`, `leaderboard.py`, `significance.py`,
  `sentinel.py`, `pbo.py`, `baseline_family.py` — zero overlap
  (shadow_inference imports from `schemas.py` which is shared/read-only).
- `schemas.py`, `ids.py`, `signatures.py` untouched by me.

**Next:** TASK-0601 unblocks TASK-0602 (Add Live Feature Snapshot Export —
depends on TASK-0601, now unblocked) and TASK-0603 (Store and Settle Shadow
Predictions — depends on TASK-0602).

---

### TASK-0602: Add Live Feature Snapshot Export — ADOPTED + COMPLETED 2026-06-22

**Status:** COMPLETED 2026-06-22 (commit `1a91a82`)
**Order:** 32
**Depends on:** TASK-0601 (✅ DONE — Builder 3, commit df326d4), TASK-0405 (✅ DONE — Builder 4, commit 7f704bd). All DONE — unblocked.
**Files owned:**
- `services/quant_foundry/src/quant_foundry/feature_snapshot_export.py` (new)
- `services/quant_foundry/tests/test_feature_snapshots.py` (new)

**Task selection rationale:** TASK-0602 was unblocked by my TASK-0601
completion. The spec lists `feature_lake.py` + `feature_availability.py`
(Builder 4's files) as "likely touched," but I created a file-disjoint
`feature_snapshot_export.py` that imports from them read-only. Does NOT
modify Builder 4's files.

**Tests:** 29/29 green — `uv run pytest services/quant_foundry/tests/test_feature_snapshots.py -q`
**Full suite:** 359/359 green — `uv run pytest services/quant_foundry/tests -q` (excluding Builder 2's in-progress `test_runpod_client.py`; no regressions; up from 330 after TASK-0601)
**Lint:** `uv run ruff check` — All checks passed (2 files)
**Type:** `uv run mypy` — Success: no issues found in 1 source file
**Commit:** `1a91a82` — 3 files, +686 lines, additive only, file-disjoint from all active tasks.

**Delivered:**
- `services/quant_foundry/src/quant_foundry/feature_snapshot_export.py`:
  - `SnapshotExportConfig` (frozen, extra='forbid'; min_availability_pct=80.0,
    max_freshness_ns=60s).
  - `SnapshotExportReceipt` (frozen; snapshot + availability_report +
    decision_time + degraded_symbols; `to_dict` JSON-serializable).
  - `FeatureSnapshotExport`:
    - `export()`: converts `FeatureRow` objects (Builder 4's
      `feature_lake.py`) into compact `FeatureSnapshot` objects (TASK-0601's
      `shadow_inference.py`). Filters rows at decision_time, builds compact
      float vectors in expected_features order, computes per-symbol
      availability, computes freshness_ns. Marks symbols below
      min_availability_pct as degraded (availability=False).
    - `export_with_receipt()`: returns `SnapshotExportReceipt` with snapshot
      + `FeatureAvailabilityReport` + degraded_symbols list.
  - `export_feature_snapshot()`: convenience entry point.
- `services/quant_foundry/tests/test_feature_snapshots.py` — 29 TDD tests
  covering all acceptance criteria:
  - Config (required fields, reasonable defaults, frozen).
  - Compact snapshots (produces FeatureSnapshot, compact float vectors,
    includes timestamp, includes freshness).
  - Feature availability (includes availability report, counts present
    features, detects missing features, availability flags set per symbol).
  - Missing features abstain (low availability produces degraded snapshot,
    high availability produces healthy snapshot, receipt includes degraded
    symbols, empty rows produce empty snapshot).
  - Export receipt (has snapshot, availability report, decision time,
    degraded symbols, to_dict JSON-serializable).
  - Convenience function (export_feature_snapshot works end-to-end, accepts
    config).
  - No secrets in output (config + receipt dict).

**Acceptance criteria verification (self):**
- ✅ Feature snapshots are compact (`test_snapshot_features_are_compact_vectors`).
- ✅ Feature availability is measurable (`test_export_includes_availability_report` +
  `test_availability_report_counts_present_features` +
  `test_availability_report_detects_missing_features`).
- ✅ Missing required features produce abstain or degraded state
  (`test_low_availability_produces_degraded_snapshot` +
  `test_receipt_includes_degraded_symbols` +
  `test_empty_rows_produce_empty_snapshot`).

**File-disjoint confirmation (post-commit):**
- Builder 4 (TASK-0405): `feature_lake.py`, `feature_availability.py` —
  zero overlap (read-only imports, not modified).
- All other builders: zero overlap.
- `schemas.py`, `ids.py`, `signatures.py` untouched by me.

**Next:** TASK-0602 unblocks TASK-0603 (Store and Settle Shadow Predictions —
depends on TASK-0602). TASK-0603 touches Builder 1's `shadow_ledger.py` +
`settlement.py` — I would need a file-disjoint approach (new
`shadow_settlement.py` that imports read-only).

---

### TASK-0603: Store and Settle Shadow Predictions — ADOPTED + COMPLETED 2026-06-22

**Status:** COMPLETED 2026-06-22 (commit `0aa4aef`)
**Order:** 33
**Depends on:** TASK-0602 (✅ DONE — Builder 3, commit 1a91a82). Unblocked.
**Files owned:**
- `services/quant_foundry/src/quant_foundry/shadow_settlement.py` (new)
- `services/quant_foundry/tests/test_shadow_settlement.py` (new)

**Task selection rationale:** TASK-0603 was unblocked by my TASK-0602
completion. The spec lists `shadow_ledger.py` + `settlement.py` (Builder 1's
files) as "likely touched," but I created a file-disjoint
`shadow_settlement.py` that imports from them read-only. Does NOT modify
Builder 1's files.

**Tests:** 17/17 green — `uv run pytest services/quant_foundry/tests/test_shadow_settlement.py -q`
**Full suite:** 376/376 green — `uv run pytest services/quant_foundry/tests -q` (excluding Builder 2's in-progress `test_runpod_client.py`; no regressions; up from 359 after TASK-0602)
**Lint:** `uv run ruff check` — All checks passed (2 files)
**Type:** `uv run mypy` — Success: no issues found in 1 source file
**Commit:** `0aa4aef` — 3 files, +898 lines, additive only, file-disjoint from all active tasks.

**Delivered:**
- `services/quant_foundry/src/quant_foundry/shadow_settlement.py`:
  - `CallbackRejectionReason` (StrEnum: BAD_SIGNATURE, BAD_SCHEMA, BAD_HASH).
  - `RejectedCallback` (frozen; reason + message + raw_payload + rejected_at_ns).
  - `SettlementReceipt` (frozen; records + rejected + stored + duplicates +
    settled_count + pending_count + settlement_lag_ns + batch_hash;
    `to_dict` JSON-serializable).
  - `ShadowSettlementOrchestrator`:
    - `store_batch()`: verifies HMAC signature, verifies batch hash (tamper
      check), validates schema (ShadowPrediction), stores via
      `ShadowLedger.store_batch()` (Builder 1's code, read-only). Invalid
      callbacks recorded as `RejectedCallback`, not silently discarded.
    - `settle_prediction()`: delegates to `SettlementLedger.settle()`
      (Builder 1's code, read-only). Returns `SettlementRecord`.
    - `settle_batch()`: settles a batch of predictions, records settlement
      lag, settled count, pending count.
  - `store_and_settle_batch()`: convenience entry point (store + settle).
- `services/quant_foundry/tests/test_shadow_settlement.py` — 17 TDD tests
  covering all acceptance criteria:
  - RejectedCallback (rejection reasons defined, required fields, frozen).
  - Store signed batch (valid batch succeeds, bad signature rejected, bad
    hash rejected, bad schema rejected).
  - Pending by horizon (pending before horizon expires, settles after).
  - Settlement lag (receipt includes lag, includes settled count).
  - No trading authority (all predictions shadow_only, no order fields in
    receipt dict).
  - Settlement receipt (has settled records, to_dict JSON-serializable).
  - Convenience function (store_and_settle_batch works end-to-end).
  - No secrets in output (receipt dict has no secret keys).

**Acceptance criteria verification (self):**
- ✅ Shadow predictions settle into outcomes
  (`test_prediction_settles_after_horizon_expires`).
- ✅ Settlement lag is visible (`test_settlement_receipt_includes_lag`).
- ✅ No prediction reaches `sig.predict`
  (`test_all_predictions_have_shadow_only_authority` +
  `test_receipt_to_dict_has_no_order_fields`).
- ✅ Invalid callback is stored as rejected, not silently discarded
  (`test_store_batch_rejects_bad_signature` +
  `test_store_batch_rejects_bad_schema` +
  `test_store_batch_rejects_bad_hash`).

**File-disjoint confirmation (post-commit):**
- Builder 1 (TASK-0401/0402): `settlement.py`, `outcomes.py`, `metrics.py`,
  `shadow_ledger.py` — zero overlap (read-only imports, not modified).
- All other builders: zero overlap.
- `schemas.py`, `ids.py`, `signatures.py` untouched by me.

**Next:** TASK-0603 completes Phase 6 (Shadow Inference). Unblocks Phase 7
(Tournament + Promotion):
- TASK-0701 (Expand Tournament Leaderboards — depends on TASK-0603, now
  unblocked). Touches my `leaderboard.py` + `tournament.py` (my own files).
- TASK-0702 (Build Promotion Review Queue — depends on TASK-0701).
- TASK-0703 (Add Retirement and Edge-Decay Flags — depends on TASK-0701).
- TASK-0704 (Build Paper-Only Model Pointer Bridge — depends on TASK-0702 +
  TASK-0703).

---

### TASK-0701: Expand Tournament Leaderboards — ADOPTED + COMPLETED 2026-06-22

**Status:** COMPLETED 2026-06-22 (commit `0831e2c`)
**Order:** 35
**Depends on:** TASK-0603 (✅ DONE — Builder 3, commit 0aa4aef). Unblocked.
**Files owned:**
- `services/quant_foundry/src/quant_foundry/leaderboard_expanded.py` (new)
- `services/quant_foundry/tests/test_leaderboard_expanded.py` (new)

**Task selection rationale:** TASK-0701 was unblocked by my TASK-0603
completion. The spec doesn't list specific files. I created a file-disjoint
`leaderboard_expanded.py` that extends the basic leaderboard (TASK-0404)
without modifying `leaderboard.py` or `tournament.py` (avoids breaking
existing TASK-0404 tests).

**Tests:** 28/28 green — `uv run pytest services/quant_foundry/tests/test_leaderboard_expanded.py -q`
**Full suite:** 404/404 green — `uv run pytest services/quant_foundry/tests -q` (excluding Builder 2's in-progress `test_runpod_client.py`; no regressions; up from 376 after TASK-0603)
**Lint:** `uv run ruff check` — All checks passed (2 files)
**Type:** `uv run mypy` — Success: no issues found in 1 source file
**Commit:** `0831e2c` — 3 files, +877 lines, additive only, file-disjoint from all active tasks.

**Delivered:**
- `services/quant_foundry/src/quant_foundry/leaderboard_expanded.py`:
  - `HorizonSlice` / `RegimeSlice` / `SymbolClusterSlice` (frozen; slice
    name + score).
  - `BaselineDelta` (frozen; baseline_model_id + delta + baseline_score).
  - `CalibrationSummary` (frozen; brier_score + reliability + n_bins).
  - `DecayIndicator` (frozen; decay_score + is_stale + is_decayed +
    days_since_last_settlement).
  - `ExpandedLeaderboardEntry` (frozen; model_id + total_score + horizon/
    regime/cluster slices + baseline_delta + calibration_summary +
    decay_indicator; `to_dict` JSON-serializable).
  - `LeaderboardExplanation` (frozen; model_id + rank + total_score +
    baseline_delta + decay_indicator + horizon/regime/cluster scores +
    is_stale + is_decayed; `to_dict` JSON-serializable).
  - `ExpandedLeaderboard`:
    - `ranked()`: overall ranking (non-flagged first, stale/decayed pushed
      to bottom, sorted by score descending).
    - `ranked_by_horizon()` / `ranked_by_regime()` /
      `ranked_by_symbol_cluster()`: per-slice rankings.
    - `stale_models()` / `decayed_models()`: list flagged models.
    - `explain()`: return `LeaderboardExplanation` for a model.
    - `to_dict()`: JSON-serializable.
- `services/quant_foundry/tests/test_leaderboard_expanded.py` — 28 TDD
  tests covering all acceptance criteria.

**Acceptance criteria verification (self):**
- ✅ A model can rank high in one horizon and low in another
  (`test_rank_by_horizon` + `test_rank_by_regime` +
  `test_rank_by_symbol_cluster`).
- ✅ Stale or decayed models are flagged
  (`test_stale_model_is_flagged_in_ranking` +
  `test_decayed_model_is_flagged_in_ranking` + `test_stale_models_list` +
  `test_decayed_models_list`).
- ✅ Leaderboard explains why a model ranks where it does
  (`test_explain_returns_explanation` + `test_explanation_includes_rank` +
  `test_explanation_includes_score_components` +
  `test_explanation_includes_baseline_delta` +
  `test_explanation_includes_decay_indicator` +
  `test_explanation_includes_horizon_scores` +
  `test_explanation_to_dict_is_json_serializable`).

**Next:** TASK-0701 unblocks TASK-0702 (Build Promotion Review Queue —
depends on TASK-0701) and TASK-0703 (Add Retirement and Edge-Decay Flags —
depends on TASK-0701).

---

### TASK-0702: Build Promotion Review Queue — ADOPTED + COMPLETED 2026-06-22

**Status:** COMPLETED 2026-06-22 (commit `60f9e61`)
**Order:** 36
**Depends on:** TASK-0701 (✅ DONE — Builder 3, commit 0831e2c). Unblocked.
**Files owned:**
- `services/quant_foundry/src/quant_foundry/promotion.py` (new)
- `services/quant_foundry/tests/test_promotion.py` (new)

**Task selection rationale:** TASK-0702 was unblocked by my TASK-0701
completion. The spec lists `services/api/src/api/routes/quant_foundry.py`
(Builder 2's file) and `apps/dashboard/` (Builder 1's files), but those are
separate tasks — I created a file-disjoint `promotion.py` that imports from
my `dossier.py`, `sentinel.py`, and `tournament.py` (read-only).

**Tests:** 24/24 green — `uv run pytest services/quant_foundry/tests/test_promotion.py -q`
**Full suite:** 428/428 green — `uv run pytest services/quant_foundry/tests -q` (excluding Builder 2's in-progress `test_runpod_client.py`; no regressions; up from 404 after TASK-0701)
**Lint:** `uv run ruff check` — All checks passed (2 files)
**Type:** `uv run mypy` — Success: no issues found in 1 source file
**Commit:** `60f9e61` — 3 files, +933 lines, additive only, file-disjoint from all active tasks.

**Delivered:**
- `services/quant_foundry/src/quant_foundry/promotion.py`:
  - `BlockingIssue` (frozen; code + severity + message).
  - `PromotionWaiver` (frozen; issue_code + waived_by + reason).
  - `PromotionEvidence` (frozen; dossier + tournament_result +
    sentinel_receipt + blocking_issues).
  - `PromotionRequest` (frozen; model_id + target_level + review_note +
    waivers).
  - `ReviewDecision` (StrEnum: APPROVED, REJECTED).
  - `PromotionRejectionReason` (StrEnum: NO_DOSSIER, INSUFFICIENT_EVIDENCE,
    SENTINEL_FAILED, BLOCKING_ISSUE, MVP_LEVEL_LIMIT).
  - `PromotionReceipt` (frozen; decision + request + review_note +
    rejection_reason + decided_at_ns; `to_dict` JSON-serializable).
  - `PromotionGate`:
    - `evaluate()`: checks (1) no dossier -> NO_DOSSIER, (2) MVP level
      limit -> MVP_LEVEL_LIMIT, (3) insufficient settled count ->
      INSUFFICIENT_EVIDENCE, (4) failed sentinel -> SENTINEL_FAILED, (5)
      unwaived blocking issue -> BLOCKING_ISSUE, (6) all pass -> APPROVED.
    - Uses explicit `_LEVEL_ORDER` dict for promotion level comparison.
  - `PromotionReviewQueue`:
    - `submit()` / `pending()` / `process_next()` / `completed()` /
      `rejected()` / `approved()`.
- `services/quant_foundry/tests/test_promotion.py` — 24 TDD tests
  covering all acceptance criteria.

**Acceptance criteria verification (self):**
- ✅ No model can be promoted without a dossier
  (`test_promotion_without_dossier_is_rejected`).
- ✅ No model can be promoted without settlement evidence
  (`test_promotion_without_settlement_is_rejected` +
  `test_promotion_below_min_settled_is_rejected`).
- ✅ Human approval is stored
  (`test_receipt_includes_review_note` + `test_receipt_includes_request`).
- ✅ Rejection is stored with reason
  (`test_rejected_receipt_has_reason`).

**Next:** TASK-0702 unblocks TASK-0704 (Build Paper-Only Model Pointer
Bridge — depends on TASK-0702 + TASK-0703). TASK-0703 (Add Retirement and
Edge-Decay Flags — depends on TASK-0701) is also unblocked.

---

### TASK-0703: Add Retirement and Edge-Decay Flags — ADOPTED + COMPLETED 2026-06-22

**Status:** COMPLETED 2026-06-22 (commit `ffe9ce7`)
**Order:** 37
**Depends on:** TASK-0701 (✅ DONE — Builder 3, commit 0831e2c). Unblocked.
**Files owned:**
- `services/quant_foundry/src/quant_foundry/retirement.py` (new)
- `services/quant_foundry/tests/test_retirement.py` (new)

**Task selection rationale:** TASK-0703 was unblocked by my TASK-0701
completion. The spec doesn't list specific files. I created a file-disjoint
`retirement.py` that imports from my `leaderboard_expanded.py` (read-only).

**Tests:** 24/24 green — `uv run pytest services/quant_foundry/tests/test_retirement.py -q`
**Full suite:** 452/452 green — `uv run pytest services/quant_foundry/tests -q` (excluding Builder 2's in-progress `test_runpod_client.py`; no regressions; up from 428 after TASK-0702)
**Lint:** `uv run ruff check` — All checks passed (2 files)
**Type:** `uv run mypy` — Success: no issues found in 1 source file
**Commit:** `ffe9ce7` — 3 files, +628 lines, additive only, file-disjoint from all active tasks.

**Delivered:**
- `services/quant_foundry/src/quant_foundry/retirement.py`:
  - `DecayReason` (StrEnum: CALIBRATION_DEGRADATION, NET_EDGE_BELOW_BASELINE,
    FEATURE_AVAILABILITY_DEGRADATION, LATENCY_BUDGET_VIOLATION,
    DRAWDOWN_CONTRIBUTION, STALE).
  - `RetirementAction` (StrEnum: RETIRE, RETRAIN, MONITOR).
  - `DecayThresholds` (frozen; max_brier_score=0.25, min_baseline_delta=0.0,
    max_decay_score=0.3, max_days_since_settlement=30,
    retire_decay_threshold=0.5, retrain_decay_threshold=0.3).
  - `RetirementFlag` (frozen; model_id + reasons + action + flagged_at_ns;
    `to_dict` JSON-serializable; no delete/deletion keys).
  - `RetirementFlagger`:
    - `evaluate()`: checks calibration degradation (Brier > threshold), net
      edge below baseline (delta < threshold), stale (days > threshold or
      is_stale), feature availability degradation (decay_score > threshold).
      Returns `RetirementFlag` with RETIRE (decay >= 0.5), RETRAIN
      (decay >= 0.3), or MONITOR. Returns None if healthy.
  - `flag_model_for_retirement()`: convenience entry point.
- `services/quant_foundry/tests/test_retirement.py` — 24 TDD tests
  covering all acceptance criteria.

**Acceptance criteria verification (self):**
- ✅ A decayed fixture model is flagged (`test_decayed_model_is_flagged` +
  `test_stale_model_is_flagged` + `test_calibration_degradation_is_flagged` +
  `test_net_edge_below_baseline_is_flagged`).
- ✅ Flag includes reason (`test_flag_includes_specific_reason` +
  `test_flag_includes_multiple_reasons`).
- ✅ Retirement recommendation cannot delete artifacts
  (`test_retire_action_does_not_delete_artifacts` +
  `test_flag_to_dict_has_no_delete_keys`).
- ✅ Dashboard shows retire/retrain suggestion
  (`test_retire_action_is_a_suggestion` +
  `test_retrain_action_for_moderate_decay` +
  `test_monitor_action_for_mild_decay`).

**Next:** TASK-0703 + TASK-0702 unblock TASK-0704 (Build Paper-Only Model
Pointer Bridge — depends on both, now unblocked). TASK-0704 is the first
dangerous connection point (shadow -> paper).
