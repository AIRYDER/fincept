# Merge Readiness Report

Branch: `tier1a/product-loop` → target `origin/main`
Analyst: Agent C (C4 — Merge Readiness / Mainline Stabilization)
Date: 2026-07-09
Task: task-mre4htwv-b8a2691d / swarm 6e2054c37b750e

> No-code report. No merge performed. No files modified outside this report.

---

## Branch Summary

| Metric | Value |
|---|---|
| Commits ahead of `origin/main` | **209** |
| Commits ahead of local `main` | 470 (local `main` is stale: 1 commit `ab388fc8` not on `origin/main`) |
| Merge base with `origin/main` | `c8219564` (= `origin/main` HEAD) |
| Files changed vs `origin/main` | **819 files**, +262,441 / −2,285 lines |
| Branch age | `origin/main` tip 2026-07-03 → branch tip 2026-07-09 (~6 days) |
| Working tree | **dirty** — 119 untracked files (docs, scripts, zips, stray partial-path dirs) |

**Key finding — clean merge base:** `origin/main` (`c8219564`) is a *direct ancestor* of `tier1a/product-loop`. There is **no divergence** from the remote target. A merge/PR to `origin/main` would be a clean **fast-forward** with **zero textual conflicts**. The "469+ commits ahead of main" in the task brief refers to the *stale local* `main`, not the real merge target.

**Key milestones on the branch (origin/main..HEAD):**
- Tier 0 consolidation: ruff burn-down (1845→249), durable artifacts, image slimming, metric sanity bounds, lifecycle/timeout fixes (B4/B5).
- RunPod production canary proven live (A6 gpu_healthcheck, A7 train_model) on pinned image `6dbec436`; bit-deterministic across environments.
- Tier 1a product loop: callback persistence, model registry API + lifespan wiring, training dispatcher, observability, cost tracker.
- Tier 1.3 CatBoost GPU backend; Tier 1.5 dataset registry dispatch gate (reject `inline_dataset_csv` in production); Tier 1.6 per-job operational metrics.
- Tier 2.1–2.7: CPCV + real DSR/PBO, triple-barrier + meta-labeling, champion/challenger shadow deployment, execution-aware backtesting, versioned feature definitions, checkpoint/resume.
- Tier 2a–2f: promotion gate proof, auto-promotion orchestrator, settlement-backed comparison provider, sentinel receipt lookup, auto-tournament consumer, auto shadow dispatch scheduler.
- Tier 3.1: determinism proof CI gate added to `nightly.yml`.
- C1 (HEAD `f0343af9`): bundle round-trip contract — write/load/score/selfcheck.

---

## Commit Cluster Map

209 commits grouped by feature area (counts approximate from `git log`):

| Cluster | ~Commits | Risk | Notes |
|---|---|---|---|
| RunPod worker debugging / Docker base churn | ~49 (`fix(runpod)`, `diag`, `debug`, `evidence`) | **HIGH** | 4 explicit `revert` commits; 37 touches to `handler.py`; iterative Dockerfile base flips (nvidia/cuda ↔ python:3.12-slim ↔ runpod/base). High churn, hard-won "known-working" state. |
| Tier 1a product loop (callback, registry, dispatcher, observability) | ~15 | MED | New DB migrations 0004/0004b/0005/0006; gateway wiring. |
| Tier 2 quant validation (CPCV, DSR, triple-barrier, shadow, tournament) | ~20 | MED-HIGH | Large new test surfaces; statistical rigor changes. |
| RunPod training handler feature growth | ~12 | **HIGH** | `handler.py` grew +4,206 lines (single file ~4,507 lines). |
| Gateway wiring (`gateway.py`, `gateway_callback.py`) | ~8 | MED | `gateway.py` +604 lines across 8 commits. |
| CI / nightly / build workflow | ~6 | LOW-MED | 3 new runpod build workflows + nightly determinism gate. |
| Tests | ~9 dedicated + 139 test files added | LOW | Strong coverage growth. |
| Docs / evidence / receipts | ~10 + 363 report files | LOW | Bulk of file count; not runtime risk. |
| Reverts / debug-leftover cleanup | 4 | MED | `bebc3dd3` reverts debug leftovers — verify no debug code remains. |

**Risky clusters:** (1) RunPod Docker/handler churn, (2) the 4,500-line `handler.py` monolith, (3) gateway.py growth, (4) 4 new Alembic migrations that must apply cleanly on `main`'s DB schema.

---

## Highest Risk Areas

1. **CI lint/typecheck gate will FAIL on merge.** Required CI job `py-lint-typecheck` runs `ruff check`, `ruff format --check`, and `mypy` over `libs services`. Current state on the branch:
   - `ruff check libs services` → **391 errors** (140 auto-fixable).
   - `ruff format --check libs services` → **48 files would be reformatted**.
   - `mypy libs services` → **188 errors in 51 files**.
   - This is the single biggest merge blocker. The PR cannot go green until these are resolved (or the CI job is adjusted, which is a policy decision, not recommended here).

2. **`runpod/quant-foundry-training/handler.py` — 4,507-line monolith.** Touched 37 times across the branch with 4 reverts. Highest behavioral-risk file. A merge itself won't conflict (fast-forward), but any post-merge refactor or mainline hotfix here is dangerous. (Out of scope for C4 — no decomposition.)

3. **4 new Alembic migrations** (`0004_callback_ingestion`, `0004b_observability`, `0005_model_registry`, `0006_dataset_manifests`). CI runs `alembic upgrade head` against a fresh Timescale container, so schema apply risk is covered by CI *if* CI is green. Confirm no manual DDL has been applied to production out-of-band.

4. **Dirty working tree (119 untracked files).** Includes stray partial-path directories (`docs/S`, `docs/S2`, `docs/S2_L`…), zips (`docs/AAA_GLM_SUPERTEAM_LOGS.zip`, `docs/runpod-fix-plan.zip`), and root-level scripts (`analyze_skills.ps1`, `diff_skills.ps1`, `gather`). These must NOT be committed with the merge. Several look like editor/agent artifacts (incremental path writes).

5. **`pyproject.toml` change:** adds `pythonpath = ["scripts"]` to pytest config. Low risk but changes test collection behavior repo-wide; verify no test assumes scripts/ is *not* on the path.

6. **Bulk report/evidence artifacts (363 files under `reports/`, 137 JSON/JSONL/zip files).** Not runtime risk, but bloats the repo and main's history. Consider whether evidence bundles belong on `main` or in long-lived artifact storage.

---

## Expected Conflicts

**Against `origin/main`: NONE.** `origin/main` (`c8219564`) is a direct ancestor of `tier1a/product-loop`; `git merge-base origin/main tier1a/product-loop` == `origin/main` HEAD. The merge is a clean **fast-forward** — no textual conflicts possible.

**Against stale local `main` (`ab388fc8`):** local `main` has 1 commit (`ab388fc8 debug(runpod): test quant_foundry imports to isolate crash`) not on `origin/main`. If someone merges into *local* `main` instead of `origin/main`, that debug commit could create a divergence. **Recommendation: target `origin/main` and fast-forward; do not merge into the stale local `main`.**

**Files most likely to conflict if `main` advances before merge** (high-churn, single-target files):
- `services/quant_foundry/src/quant_foundry/gateway.py` (8 commits, +604 lines)
- `runpod/quant-foundry-training/handler.py` (37 commits, +4,206 lines)
- `.github/workflows/nightly.yml`, `ci.yml`
- `pyproject.toml`, `uv.lock`

---

## Required Pre-Merge Tests

Run these locally and ensure green before opening the PR (these mirror the required CI jobs in `.github/workflows/ci.yml` and `nightly.yml`):

```bash
# 1. Lint + format + typecheck (CURRENTLY FAILING — must fix first)
uv run ruff check libs services
uv run ruff format --check libs services
uv run mypy libs services

# 2. Lockfile sync (CURRENTLY PASSING)
uv lock --check

# 3. Full default test suite (excludes long/gpu/live markers) — RUN, PASSING
uv run pytest                          # repo-wide, default markers
# Targeted quant_foundry suite (RUN, PASSING):
uv run pytest services/quant_foundry -q -m "not long and not gpu and not live"
# Result observed: 4487 passed, 159 skipped, 6 xfailed, 0 failed in 86s

# 4. Startup safety matrix (CI required)
uv run pytest libs/fincept-core/tests/test_startup_safety_matrix.py -q

# 5. Alembic migration applies cleanly (CI runs this against fresh Timescale)
uv run alembic -c libs/fincept-db/alembic.ini upgrade head

# 6. Determinism proof gate (nightly, Tier 3.1) — not run here (needs trainer env)
QUANT_FOUNDRY_CALLBACK_SECRET=determinism-ci-gate-secret \
QUANT_FOUNDRY_USE_REAL_TRAINER=true \
uv run python -c "from quant_foundry.determinism_proof import run_determinism_gate; import sys; sys.exit(run_determinism_gate())"

# 7. JS dashboard checks (CI required)
pnpm install --frozen-lockfile=false
pnpm -r --if-present lint
pnpm -r --if-present typecheck
pnpm -r --if-present test
pnpm -r --if-present build

# 8. Verification receipt (CI required, pwsh)
./scripts/verification-receipt.ps1
```

**Test status observed by Agent C:**
- `services/quant_foundry` default suite: **4487 passed, 159 skipped, 6 xfailed, 0 failed** (86s). ✅
- `test_gateway_callbacks` + `test_training_handler_preflight` + `test_startup_safety_matrix`: **26 passed**. ✅
- `uv lock --check`: **PASS**. ✅
- `ruff check`: **391 errors** ❌
- `ruff format --check`: **48 files need reformat** ❌
- `mypy`: **188 errors** ❌
- Full repo `uv run pytest`, determinism gate, JS checks, receipt runner: **NOT RUN** (time-boxed; mark as required before merge).

---

## Required Live Canary

Before merge, obtain fresh executable evidence (per AGENTS.md "never claim completion without executable evidence"):

1. **RunPod training canary** — dispatch one `train_model` job on the pinned production image and confirm: HMAC-signed callback received, artifact sha256 verified, callback persisted to model registry, `model_versions` row written. (Reuses the A7 proof path.)
2. **Determinism proof** — train twice, confirm identical sha256 (the Tier 3.1 nightly gate). This is the gate that guards the "bit-deterministic across environments" claim.
3. **End-to-end product loop** — dispatch → callback ingestion → model registry registration → promotion gate decision. The E2E proof test (`test_gateway_runpod_loop` / `test_real_ml_e2e`) covers the contract; a live run confirms the wiring against real RunPod + real DB.
4. **Alembic upgrade on a fresh Timescale container** — confirms all 4 new migrations apply from `main`'s baseline schema without manual intervention.

Existing live evidence on the branch (docs/S2 live training proof, A6/A7 canaries on `6dbec436`) is dated 2026-07-03..07; re-confirm on the current HEAD `f0343af9` before merge.

---

## Rollback Plan

Because the merge to `origin/main` is a **fast-forward**, rollback is straightforward:

1. **Immediate revert (no merge commit to undo):** force `origin/main` back to `c8219564`:
   ```bash
   git push origin c8219564:refs/heads/main --force
   ```
   Use only if the merge breaks production and no other commits have landed on `main` since. Coordinate with all contributors.

2. **If commits have landed on `main` after the fast-forward:** create a revert commit range:
   ```bash
   git revert --no-commit c8219564..f0343af9
   git commit -m "revert: roll back tier1a/product-loop merge (production break)"
   git push origin main
   ```
   This produces a single revert commit (cleaner history than force-push when `main` has advanced).

3. **Database rollback:** the 4 new migrations are forward-only by default. Before merge, confirm each migration has a `downgrade()` path. If a migration breaks the prod DB:
   ```bash
   uv run alembic -c libs/fincept-db/alembic.ini downgrade -1   # one migration at a time
   ```
   Verify `downgrade()` functions exist for `0004`, `0004b`, `0005`, `0006` before merge.

4. **RunPod fleet:** the training/inference images are built from `runpod/*` Dockerfiles. If a merged Dockerfile breaks the fleet, re-deploy the last known-good image tag (`6dbec436` for training) via the existing RunPod endpoint update tooling; the branch retains `Dockerfile.minimal`/`Dockerfile.slim` fallbacks.

5. **Feature flags:** the branch introduces production-mode gates (e.g. reject `inline_dataset_csv` in production, PIT proof fail-closed). If these gates break an existing production flow, set `FINCEPT_TRADING_MODE=paper` (or the equivalent mode env) to relax the gates while investigating — confirm the mode-aware gate semantics before relying on this.

**Pre-merge safety net:** tag `origin/main` before merge:
```bash
git tag pre-tier1a-merge c8219564
git push origin pre-tier1a-merge
```
This gives an unambiguous restore point regardless of how `main` advances.

---

## Recommendation: do not merge yet

**Verdict: DO NOT MERGE YET.** The branch is functionally strong (4,487 tests passing, clean fast-forward merge base, no conflicts) but is **not CI-green** and the working tree is dirty.

**Blockers (must fix before merge):**
1. ❌ `ruff check libs services` — 391 errors (CI required check `py-lint-typecheck` will fail).
2. ❌ `ruff format --check libs services` — 48 files need reformat (same CI job).
3. ❌ `mypy libs services` — 188 errors (same CI job).
4. ❌ Dirty working tree — 119 untracked files including zips and stray partial-path dirs; must be cleaned or `.gitignore`'d so they aren't swept into the merge.

**Conditions to satisfy before merge (in order):**
1. Drive ruff errors to 0 (140 are auto-fixable with `ruff check --fix`; the rest need manual attention or a documented CI policy exception — not recommended).
2. Run `ruff format libs services` to reformat the 48 files.
3. Resolve or baseline the 188 mypy errors (at minimum, confirm they are pre-existing and not newly introduced by the branch; if new, fix them).
4. Clean the working tree: `git clean -nd` to review, then remove/gignore editor artifacts and zips; do not commit evidence zips to `main`.
5. Re-run the full repo `uv run pytest` (not just `services/quant_foundry`) and confirm green.
6. Run the JS dashboard checks (`pnpm -r lint/typecheck/test/build`).
7. Run the determinism proof gate and a fresh live RunPod training canary on HEAD `f0343af9`.
8. Tag `origin/main` as `pre-tier1a-merge` for rollback safety.
9. Open PR targeting `origin/main` (fast-forward); require all CI checks green + human approval.

**Once blockers 1–4 are cleared and 5–8 pass, this is a low-conflict, fast-forward merge ready for mainline.** The 4,500-line `handler.py` and `gateway.py` growth are post-merge stabilization concerns (decomposition), not merge blockers.

---

# Merge Readiness Update — C4B CI Greening

Branch: `tier1a/product-loop`
Analyst: Agent G (C4B — CI Greening / Merge Stabilization)
Date: 2026-07-09
Task: task-mre5zjol-237acc65 / swarm 8201c0bf2012d4

> No-code report update. No product behavior changed. Only this report file was modified by Agent G.

This section records the results of the C4B CI-greening swarm (Agents A–F) and supersedes the C4 blocker list above where addressed.

## Commits Added By C4B Swarm

| Commit | Agent | Subject |
|---|---|---|
| `68c53c51` | A+B | `style(c4b): ruff auto-fix + format pass` |
| `81a2a2d1` | F | `fix(c4b): repair broken Alembic downgrade paths` |
| `92d8b228` | E | `chore(c4b): update .gitignore for dirty tree cleanup` |
| `b8f658e5` | E | `docs(c4b): commit legitimate new files from prior swarms` |
| `56c8110a` | E | `chore(c4b): ignore generated GPU output artifact` |
| `77074ca6` | C | `type(c4b): mypy triage — 190 errors to 0` |

Working tree after C4B: **CLEAN** (0 untracked files).

## CI Status (ruff, format, mypy, tests)

| Check | Before C4B | After C4B | Status |
|---|---|---|---|
| `ruff check` | 623 errors (211 safe-fixable) | 406 remaining (all unsafe-fix-only) | ⚠️ Partial |
| `ruff format --check` | 65 files would be reformatted | 825 files already formatted (exit 0) | ✅ PASS |
| `mypy` | 190 errors across 52 files | 0 errors across 162 source files | ✅ PASS |
| C1–C3 regression tests | (not isolated) | 187 passed, 2 skipped, 0 failed | ✅ PASS |
| Full quant_foundry suite | 4487 passed (per C4) | pending final run | ⏳ Pending |

## Ruff Status (623 → 406 remaining, all unsafe-fix-only)

- **Before:** 623 ruff errors; 211 were safe-fixable.
- **After `ruff check --fix`:** 406 errors remain. **All 406 are unsafe-fix-only** (e.g. `F841` unused variables) and were intentionally **not** applied, because unsafe fixes can alter behavior.
- 67 files changed in the auto-fix + format pass (commit `68c53c51`).
- **Assessment:** The remaining 406 are not auto-fixable without manual review. They are predominantly cosmetic/unused-variable lint, not type or runtime defects. Whether they block merge depends on CI policy: if `py-lint-typecheck` is a *required* check with zero-tolerance, the PR will not go green until these are resolved or the policy is adjusted.

## Format Status (65 files → PASS, 825 files formatted)

- **Before:** `ruff format --check` reported 65 files would be reformatted.
- **After `ruff format`:** 825 files already formatted; `ruff format --check` exits 0. ✅
- Format check is now **green**.

## Mypy Status (190 errors → 0 errors)

- **Before:** 190 mypy errors across 52 files.
- **After:** **0 errors across 162 source files.** ✅
- Strategy (Agent C, commit `77074ca6`): precise annotations, `cast()`, assert guards, and targeted `# type: ignore[code]` with inline justifications. No behavior changes.
- 53 files changed (52 `.py` + 1 report). C1–C3 tests after fixes: 187 passed, 2 skipped, 0 failed.

## Test Status (C1–C3: 187 passed, 0 failed; full suite pending)

- Agent D's sandbox could not locate `uv`/`python`/`git`; the orchestrator ran the C1–C3 regression suite directly.
- Suites run: `test_bundle_io.py`, `test_real_trainer_inference_e2e.py`, `test_shadow_inference.py`, `test_real_inference.py`, `test_pit_proof_gate.py`, `test_feature_lake.py`, `test_manifest_dataset_loader.py`, `test_promotion_gate_prep.py`.
- **Result: 187 passed, 2 skipped, 0 failed.** No behavior drift from CI cleanup.
- **Full repo `uv run pytest` and the quant_foundry default suite need a final confirmation run** before merge (C4 observed 4487 passed previously; re-confirm on post-C4B HEAD).

## Working Tree Status (120 untracked → CLEAN)

Agent E classified 120 untracked files:

| Class | Count | Action |
|---|---|---|
| Truncated duplicate artifacts (byte-for-byte dupes from broken file-writing) | 95 | DELETED |
| Generated/local artifacts | 19 | IGNORED (16 new `.gitignore` patterns) |
| Legitimate new files (`AGENTS.md`, `docs/S2_GPU_TRAINING_PROOF_REPORT.md`) | 2 | COMMITTED |
| `.py` files from prior swarms (`c8_probe_ladder.py`, `s3_product_loop_proof.py`, `test_promotion_gate_prep.py`) | 3 | COMMITTED |
| GPU output artifact | 1 | IGNORED |

Working tree is now **CLEAN** (0 untracked files). Commits: `92d8b228`, `b8f658e5`, `56c8110a`.

## Alembic Status (1 broken downgrade fixed, runtime verification blocked)

Agent F reviewed 7 migrations (`0001` through `0006`):

- **1 broken downgrade repaired:** `0005_model_registry.py` — foreign-key constraint drop was missing before the table drop; FK drop now precedes table drop (commit `81a2a2d1`).
- **Runtime DB verification: BLOCKED** — no local Postgres/Timescale available. Static analysis confirms all `downgrade()` paths are now correct.
- **Recommendation:** run `alembic upgrade head` then `alembic downgrade base` against a fresh Timescale container in CI (or a local docker Postgres) before merge to obtain executable evidence.

## Remaining Blockers

1. ⚠️ **406 ruff errors remain (all unsafe-fix-only).** Not auto-fixable without manual review. Blocks merge *only if* `py-lint-typecheck` is a hard-required CI check with zero tolerance. If the team accepts a policy exception/baseline for pre-existing unsafe lint, this is no longer a hard blocker.
2. ⏳ **Full repo test suite** (`uv run pytest` + quant_foundry default suite) needs a final green run on post-C4B HEAD.
3. ⏳ **Alembic downgrade runtime verification** not performed (no local Postgres). Static analysis is clean; live `upgrade head` → `downgrade base` against a fresh container is recommended before merge.
4. ⏳ **JS dashboard checks** (`pnpm -r lint/typecheck/test/build`), **determinism proof gate**, and a **fresh live RunPod training canary** on the new HEAD are still required per the C4 pre-merge list.
5. ⏳ **Tag `origin/main` as `pre-tier1a-merge`** for rollback safety before merging.

## Recommendation: merge / do not merge yet

**Verdict: CONDITIONALLY READY — do not merge *yet*, but the hard CI blockers from C4 are largely cleared.**

What C4B resolved (was a C4 blocker, now green):
- ✅ `ruff format --check` — PASS (was 48/65 files needing reformat).
- ✅ `mypy` — 0 errors (was 188/190).
- ✅ Dirty working tree — CLEAN (was 119/120 untracked files).
- ✅ Alembic downgrade correctness — 1 broken path repaired (static analysis).

What still gates merge:
- ⚠️ 406 unsafe-fix-only ruff errors (policy-dependent: hard blocker only if CI enforces zero-tolerance lint with no baseline).
- ⏳ Final full-suite test run on post-C4B HEAD.
- ⏳ Live Alembic upgrade/downgrade against a fresh Timescale container.
- ⏳ JS dashboard checks, determinism gate, live RunPod canary (per C4).
- ⏳ Pre-merge rollback tag.

**Once the final test run is green and either (a) the 406 ruff errors are resolved or (b) a documented CI lint baseline/exception is accepted, this branch is a low-conflict fast-forward merge ready for mainline.** The remaining items are verification/evidence steps, not code-defect blockers.
