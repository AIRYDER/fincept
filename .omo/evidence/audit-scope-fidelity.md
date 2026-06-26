# Scope Fidelity & Plan Compliance Audit — ml-dataset-evidence-spine

**Auditor:** devin-subagent (F4 Scope Fidelity)
**Date:** 2026-06-26
**Range:** `7dc5fc1..HEAD` (20 commits)
**Plan:** `.omo/plans/ml-dataset-evidence-spine.md`

---

## Summary

The implementation respects **all "Must NOT have" guardrails** — no scope
violations were found. The prediction-row schema is untouched, no forbidden
imports (sklearn/optuna/hyperopt) were introduced, no Dockerfiles/RunPod
handlers were added, no foundation-model/diffusion/debate/conformal pieces
leaked in, and the dashboard receipt surface beyond `gateway.py` was not
touched.

However, **three plan deviations** were identified:

1. **CRITICAL — Todo 17's commit is missing; `cv.py` and `test_cv.py` are
   untracked.** The plan specifies `Commit: Y` for todo 17, but no commit
   with message `feat(fincept-core): extract shared purged+embargoed
   walk-forward CV utility` exists in the log. The files exist on disk but
   are untracked (`git status` confirms). The task-17 report explicitly
   says "Commit: deferred to parent agent" — the parent never followed
   through. In a fresh clone or CI, `from fincept_core.datasets.cv import
   make_folds` (used by `backtester.walk_forward` and
   `quant_foundry.training_manifest`) would raise `ImportError`. The
   facade's `try/except ImportError` would silently set `make_folds = None`,
   causing `TypeError` at call time.

2. **Todo 8's commit is missing — bundled into todo 6's commit.** The plan
   specifies a separate commit `feat(api): add /models/{name}/outcomes
   joined route`, but the outcomes route and its 11-test suite were
   included in commit `74ed884` (todo 6: "enforce approved-root allowlist
   on training input path"). Two todos in one commit violates the plan's
   one-commit-per-todo structure.

3. **Two of three final receipts are missing.** The plan's verification
   strategy (lines 58-61) specifies three receipt files:
   - `reports/paper-spine/latest.json` — **EXISTS** ✓ (values verified)
   - `reports/quant-foundry/callback-rejection-receipt.json` — **MISSING** ✗
   - `reports/cv-convergence-receipt.json` — **MISSING** ✗

**Verdict: PASS with deviations.** No scope violations. The guardrails
held. The deviations are commit-hygiene and receipt-generation gaps, not
architectural or scope breaches. The untracked `cv.py` is the most
urgent item — it must be committed before push/CI or the backtester and
quant_foundry imports will break in any clean checkout.

---

## Findings

### F1. SCOPE VIOLATION — None found

All eleven "Must NOT have" guardrails (plan lines 40-51) were verified:

| # | Guardrail | Status | Evidence |
|---|-----------|--------|----------|
| 1 | No changes to `PredictionRow` schema; `schema_version` stays at 1 | **PASS** | `git diff 7dc5fc1..HEAD -- libs/fincept-core/src/fincept_core/prediction_log.py` → empty (0 lines changed) |
| 2 | No schema-version-2 unified prediction row | **PASS** | `git diff 7dc5fc1..HEAD \| Select-String "schema_version.*=.*2"` → empty |
| 3 | No DuckDB / Parquet migration as first storage layer | **PASS** | No `duckdb`/`DuckDB`/`import pyarrow` in diff; `parquet` mentions are test-fixture path strings only |
| 4 | No live trading unlock; no `limited_live_approved`; no `paper_bridge` | **PASS** | `git diff \| Select-String "limited_live_approved\|paper_bridge"` → empty |
| 5 | No full RunPod serverless deployment; no Dockerfile + handler.py pair | **PASS** | `git diff --stat -- "*/Dockerfile*" "*/handler.py"` → empty; no `runpod.serverless` or `serverless` in diff |
| 6 | No Cloud spend | **PASS** | No cloud-spend code or config in diff |
| 7 | No foundation-model agents / diffusion / LLM debate / allocator redesign | **PASS** | `git diff \| Select-String "TimesFM\|Chronos\|Moirai\|TabPFN\|diffusion\|debate_committee"` → empty; `allocator.py` not modified; no `foundation_*`/`debate_*`/`immune_*` agent paths modified |
| 8 | No Optuna / Hyperband / triple-barrier / meta-labeling / fractional diff / conformal gate | **PASS** | `git diff \| Select-String "Optuna\|Hyperband\|optuna\|triple_barrier\|meta_labeling\|fractional_diff\|conformal_gate"` → empty |
| 9 | No touching dashboard `/quant-foundry/*` receipts beyond `gateway.py` | **PASS** | `gateway.py` modified (B0 security — explicitly in scope); `quant_foundry.py` route, `quant_foundry_alpha.py`, `dossier.py`, `outcomes.py`, `alpha_genome*` all unmodified |
| 10 | No vague "improve model" todos | **PASS** | All 21 todos have exact acceptance commands and verification artifacts |
| 11 | No forbidden imports (sklearn / optuna / hyperopt) | **PASS** | `git diff 7dc5fc1..HEAD \| Select-String "import sklearn\|from sklearn\|import optuna\|from optuna\|import hyperopt\|from hyperopt"` → empty |

### F2. PLAN DEVIATION — Todo 17 commit missing; `cv.py` + `test_cv.py` untracked

**Severity: CRITICAL (CI breakage risk)**

The plan (todo 17, line 344) specifies:
```
Commit: Y | feat(fincept-core): extract shared purged+embargoed walk-forward CV utility
```

**Findings:**
- `git log --oneline 7dc5fc1..HEAD` — no commit with this message exists.
- `git log --all --oneline -- libs/fincept-core/src/fincept_core/datasets/cv.py` — empty (no commit ever touched cv.py).
- `git ls-tree HEAD libs/fincept-core/src/fincept_core/datasets/` — lists 6 files; `cv.py` is NOT among them.
- `git status -- libs/fincept-core/src/fincept_core/datasets/cv.py` — "Untracked files: cv.py".
- `git status -- libs/fincept-core/tests/test_cv.py` — "Untracked files: test_cv.py".
- Task-17 report (line 6): `**Commit:** deferred to parent agent`.

**Impact:**
- `services/backtester/src/backtester/walk_forward.py` imports `from fincept_core.datasets.cv import Fold as _SharedFold, make_folds as _shared_make_folds` — **would raise ImportError in fresh clone**.
- `services/quant_foundry/src/quant_foundry/training_manifest.py` imports `from fincept_core.datasets.cv import (...)` — **would raise ImportError in fresh clone**.
- `services/agents/src/agents/gbm_predictor/train.py` imports `from fincept_core.datasets import make_folds` (facade) — facade's `try/except ImportError` would silently set `make_folds = None`, causing `TypeError` at call time.
- Tests for todos 18, 19, 20 would all fail in CI.

**Root cause:** The subagent created the files and ran tests locally (all
passed — 26 tests), but deferred the git commit to the parent agent. The
parent agent never executed the commit. The files exist only in the
working tree.

### F3. PLAN DEVIATION — Todo 8 commit missing (bundled into todo 6)

**Severity: LOW (commit hygiene)**

The plan (todo 8, line 213) specifies:
```
Commit: Y | feat(api): add /models/{name}/outcomes joined route
```

**Findings:**
- `git log --oneline 7dc5fc1..HEAD -- services/api/src/api/routes/models.py` shows only commit `74ed884` (todo 6).
- `git show 74ed884 --stat` shows 4 files: `models.py` (+152), `test_models_outcomes.py` (+314), `test_models_train.py` (+200), `test_training.py` (+20/-2).
- The outcomes route and its 11-test suite were included in todo 6's commit.
- No separate commit with message `feat(api): add /models/{name}/outcomes joined route` exists.

**Impact:** Minor — the work is complete and correct (11 tests passing),
but two todos share one commit, violating the plan's one-commit-per-todo
structure. The commit message only describes the approved-root work, not
the outcomes route.

### F4. PLAN DEVIATION — Two final receipts missing

**Severity: MEDIUM (verification strategy not fully satisfied)**

The plan's verification strategy (lines 58-61) specifies three final
receipts after all todos:

| Receipt | Status | Values |
|---------|--------|--------|
| `reports/paper-spine/latest.json` | **EXISTS** ✓ | `settlement_hit_rate=1.0`, `pending_count=0`, `brier=0.0` — verified via `uv run python -c "..."` |
| `reports/quant-foundry/callback-rejection-receipt.json` | **MISSING** ✗ | Should list rejection-rate tests + counter sample |
| `reports/cv-convergence-receipt.json` | **MISSING** ✗ | Should show three call sites importing `fincept_core.datasets.cv` |

**Impact:** The evidence for todos 14, 17, 18, 19, 20 exists in
`.omo/evidence/` (test results, reports), but the plan-specified
aggregated receipt files at the `reports/` paths were never generated.
An auditor checking the verification strategy's receipt list would find
two of three missing.

### F5. COMPLIANCE NOTE — Commit message format deviation

**Severity: MINOR**

The plan specifies commit message format `<type>(<scope>): <summary>`.
19 of 20 commits follow this format. One commit deviates:

- `d1a40a5 fix: resolve mypy type-ignore + outcomes test clock-granularity flakiness`
  — missing `(scope)` part. Uses `fix:` instead of `fix(scope):`.

This is a cleanup commit not tied to a specific todo. The `fix` type is
correct; only the scope is omitted. Conventional commits allow scope to
be optional, so this is a minor format deviation, not a violation.

### F6. PASS — Todo completion verification

All 21 implementation todos are marked `[x]` with WORKER/FINISHED/Report
comments. The 4 unchecked items (`[ ]`) are F1-F4 (the final verification
wave — including this audit). Count verified: `Select-String -Pattern
"^\- \[x\] "` → 21 matches; `^\- \[ \] ` → 4 matches (F1-F4 only).

### F7. PASS — Evidence files

All 21 evidence file sets exist in `.omo/evidence/` (task-1 through
task-21). Each task has at least a `.report.md` file; most also have a
`.json` or `.txt` artifact. No `*deviation*`, `*FAIL*`, or `*fail*`
files found.

### F8. PASS — Final receipt values

`reports/paper-spine/latest.json` verified:
```
settlement_hit_rate: 1.0
pending_count: 0
brier: 0.0
```
All three canonical assertion values match the plan spec (line 59).

### F9. PASS — cost_model_version consistency

`DEFAULT_COST_MODEL_VERSION = "v1.default"` is used consistently:
- **Defined** in `libs/fincept-core/src/fincept_core/datasets/settlement.py:56`
- **Re-exported** from `libs/fincept-core/src/fincept_core/datasets/__init__.py:50,141`
- **Imported and used** in `services/settlements/src/settlements/worker.py:46` (4 call sites: lines 146, 169, 209, 260)
- **Tested** in `libs/fincept-core/tests/test_settlement_ledger.py:127` (`assert got.cost_model_version == DEFAULT_COST_MODEL_VERSION == "v1.default"`)
- **paper-spine replay** delegates to `settlements.worker.tick_sync` which uses `DEFAULT_COST_MODEL_VERSION` internally (no hardcoded override).

Cost model constants: fee_bps=5.0, spread_bps=3.0, slippage_bps=0.0 —
matches plan spec (line 18, line 139).

### F10. PASS — Commit types correct

All 20 commits use valid types from the plan's allowed set
(feat, refactor, security, chore, test, fix):
- feat: 11 commits
- refactor: 3 commits
- security: 1 commit
- chore: 1 commit
- test: 2 commits
- fix: 1 commit (missing scope — see F5)

### F11. PASS — No deviation/FAIL files

No `*deviation*`, `*FAIL*`, or `*fail*` files exist in `.omo/evidence/`.
The task-17 report documents the deferred commit (line 6: "Commit:
deferred to parent agent") but does not flag it as a deviation or
failure — it was intended as a handoff that was never completed.

---

## Todo Completion Checklist

| # | Todo | Checked | Evidence File | Commit | Notes |
|---|------|---------|---------------|--------|-------|
| 1 | ApprovedRoots module | [x] ✓ | task-1 .report.md + .txt | a6d36cb | PASS |
| 2 | Manifest schemas | [x] ✓ | task-2 .report.md + .json | 571733a | PASS |
| 3 | Settlement schema + side-store | [x] ✓ | task-3 .report.md + .json | 5dd41ed | PASS |
| 4 | FeatureSnapshotStore | [x] ✓ | task-4 .report.md + .txt | 248530c | PASS |
| 5 | datasets __init__ facade | [x] ✓ | task-5 .report.md + .txt | 6b14d3f | PASS |
| 6 | Approved-root on TrainBody | [x] ✓ | task-6 .report.md + .json | 74ed884 | PASS (also contains todo 8 — see F3) |
| 7 | Approved-root on backtest | [x] ✓ | task-7 .report.md + .json | b4a3b20 | PASS |
| 8 | /models/{name}/outcomes route | [x] ✓ | task-8 .report.md + .json | 74ed884 (bundled) | DEVIATION — no separate commit (F3) |
| 9 | Feature-availability sidecar | [x] ✓ | task-9 .report.md + .json | bd17e80 | PASS |
| 10 | Dossier + calibration helpers | [x] ✓ | task-10 .report.md + .json | 902b7ef | PASS |
| 11 | Settlement worker MVP | [x] ✓ | task-11 .report.md + .json | 6fe5e1b | PASS |
| 12 | Settlement side-store tests | [x] ✓ | task-12 .report.md + .json | 4ebd318 | PASS (test-only commit) |
| 13 | Remove _compat_sign_callback | [x] ✓ | task-13 .report.md + .json | 65be033 | PASS |
| 14 | Durable callback_rejection_rate | [x] ✓ | task-14 .report.md + .json | 52d99be | PASS (receipt file missing — see F4) |
| 15 | Scheduler polling health test | [x] ✓ | task-15 .report.md + .json | 4790795 | PASS (test-only commit) |
| 16 | Logreg baseline scaffold | [x] ✓ | task-16 .report.md + .txt | 515ccae | PASS (implemented, not deferred — under 80 LOC) |
| 17 | Extract make_folds → cv.py | [x] ✓ | task-17 .report.md + .txt | **MISSING** | DEVIATION — cv.py + test_cv.py untracked (F2) |
| 18 | Migrate gbm_predictor walk_forward | [x] ✓ | task-18 .report.md + .json | 38d8d9c | PASS (depends on untracked cv.py) |
| 19 | Migrate backtester make_folds | [x] ✓ | task-19 .report.md + .txt | 3360709 | PASS (depends on untracked cv.py) |
| 20 | Migrate quant_foundry window | [x] ✓ | task-20 .report.md + .txt | f038cc2 | PASS (depends on untracked cv.py) |
| 21 | paper_spine_replay --with-settlement | [x] ✓ | task-21 .report.md + .json | 2a8b16b | PASS |

---

## Verdict

**PASS with deviations.**

No scope violations. All "Must NOT have" guardrails held. The
implementation stayed within the plan's architectural boundaries
(sidecar settlement, schema-version preservation, no cloud spend, no
speculative research stack).

Three deviations require remediation before push:

1. **CRITICAL:** Commit `cv.py` and `test_cv.py` (todo 17's deferred
   commit). Without this, CI and fresh clones will break on
   `backtester.walk_forward` and `quant_foundry.training_manifest`
   imports. Run:
   ```
   git add libs/fincept-core/src/fincept_core/datasets/cv.py libs/fincept-core/tests/test_cv.py
   git commit -m "feat(fincept-core): extract shared purged+embargoed walk-forward CV utility"
   ```

2. **MEDIUM:** Generate the two missing final receipts:
   - `reports/quant-foundry/callback-rejection-receipt.json`
   - `reports/cv-convergence-receipt.json`

3. **LOW:** The todo 8 commit bundling is a hygiene issue that cannot be
   retroactively split without history rewriting. Document and accept.
