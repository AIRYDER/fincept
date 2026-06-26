# Phase 0 + Phase 1 Implementation Handoff

> **Repo:** `C:\Users\nolan\CascadeProjects\fincept-terminal`
> **Branch:** `codex/portfolio-optimizer-core`
> **Date:** 2026-06-22
> **Source plan:** `AAAAAAAAA_BIG_PLAN.md`
> **Scope:** Phase 0 (Freeze / Inventory / Stabilize) + Phase 1 (Verification / CI / Release Safety)
> **Status:** All 9 tasks complete; all safe focused checks green.

---

## 0. How to use this handoff

This document is the durable record of the safety + evidence foundation
work. It is ordered to match `AAAAAAAAA_BIG_PLAN.md`. Each task section
lists:

- the objective and why it came first;
- every file created or modified (absolute path);
- the exact change made;
- the tests added and their results;
- the rollback note;
- any pre-existing issues observed but left alone.

Validation commands are listed in §11. Run them before building on top
of this work.

The next valid tasks (in plan order) are TASK-0201 onwards (Phase 2:
dashboard route atlas, system readiness center, on-demand module
control). Per the plan's non-negotiable rule, RunPod / GPU / tournament
/ promotion / paper-bridge work remains off-limits until the evidence
loop (settlement, dossiers, receipts) exists.

---

## 1. Tasks completed (in plan order)

| Task | Title | Status |
|---|---|---|
| TASK-0001 | Categorize the current working tree | DONE |
| TASK-0002 | Record the current baseline verification | DONE |
| TASK-0003 | Apply runtime safety guards to all service entrypoints | DONE |
| TASK-0004 | Lock down backtest file path handling | DONE |
| TASK-0005 | Sanitize auth and token error responses | DONE |
| TASK-0101 | Create a verification receipt runner | DONE |
| TASK-0102 | Add runtime safety matrix tests | DONE |
| TASK-0103 | Add backtest path boundary tests | DONE |
| TASK-0105 | Create an environment variable reference | DONE |

---

## 2. TASK-0001 — Categorize the current working tree

### Objective
Create a clear map of what is changed before implementation starts, so
implementation commits never accidentally stage unrelated work or local
tool state.

### Findings
`git status --short` showed modified files across dashboard, API, core
libs, services, docs, experiments, scripts, plus untracked tool
directories. Grouped into:

- **Product code (modified):** dashboard shell/components/features/lib,
  core libs (config/events/schemas), API (main/routes), ingestor, jobs,
  oms, scripts/start.ps1.
- **Tests (modified):** core test_schemas, api test_data/test_news_impact.
- **Docs (modified):** RISKS, ROADMAP, featuresmenu, news-impact
  IMPLEMENTATION_STATUS.
- **New product code (untracked):** dashboard symbol/watchlist routes,
  widgets, mock-data, core http.py, news_impact_agent, news-impact-model
  eval/features/events.
- **New docs (untracked):** SYSTEM_IMPROVEMENT_REPORT, audits,
  superpowers specs, dashboard docs, DESIGN.md, AAAAAAAAA_BIG_PLAN.md.
- **Local tool state (untracked, must ignore):** `.devin/dialectic-repo/`,
  `.opencode/`, `.playwright-cli/`, `.worktrees/`.

### Files changed

**`C:\Users\nolan\CascadeProjects\fincept-terminal\.gitignore`** (modified)
- Added a "Local agent/tool working state" block ignoring
  `.devin/dialectic-repo/`, `.devin/thinking-logs/`, `.opencode/`,
  `.playwright-cli/`, `.worktrees/`.
- Added `/reports/verification/receipt-*.md` and `receipt-*.json`
  ignore rules (machine-generated receipts; the directory itself stays
  tracked via `.gitkeep`).

**`C:\Users\nolan\CascadeProjects\fincept-terminal\docs\RELEASE_HYGIENE.md`** (created)
- Staging-discipline rules (never `git add -A`; stage explicitly per
  task; run `git status --short` + `git diff --check` before commit).
- Working-tree category definitions.
- Confirmed local-only tool state list with reasons.
- Note that `.devin/` is intentionally partially tracked (workflows +
  skills committed; runtime-state subdirs ignored).

### Acceptance criteria met
- Working tree can be explained in a short inventory.
- Local-only tool folders are not accidentally staged (verified:
  `git status --short` no longer lists them).
- Hygiene note exists for future workers.

### Rollback
Ignore-rule changes can be reverted independently if a pattern is too
broad.

---

## 3. TASK-0002 — Record the current baseline verification

### Objective
Capture what passes before new implementation starts, so later
failures can be classified as new vs. pre-existing.

### Checks run (all PASS)

| Check | Command | Result |
|---|---|---|
| Shadow news-impact panel | `npm run test:shadow-news-impact` | PASS (3 tests) |
| Source health | `npm run test:source-health` | PASS (5 tests) |
| Strategy readiness | `npm run test:strategy-readiness` | PASS (4 tests) |
| Dashboard typecheck | `pnpm --dir apps/dashboard exec tsc --noEmit --pretty false` | PASS (exit 0) |
| News-impact API | `uv run pytest services/api/tests/test_news_impact.py -q` | PASS (6 tests) |
| Core lib | `uv run pytest libs/fincept-core/tests -q` | PASS (119 tests) |

### Skipped heavy checks (with reason)
- Docker Compose boot — needs operator environment.
- Browser smoke — needs Playwright session + running dashboard.
- Live provider checks — need provider API keys.
- Broker checks — need broker credentials.
- RunPod checks — not yet implemented (later phases).

### File created
**`C:\Users\nolan\CascadeProjects\fincept-terminal\reports\verification\baseline-2026-06-22.md`**
- Timestamped baseline receipt (manually curated; not gitignored like
  the machine-generated `receipt-*.md` files).

### Acceptance criteria met
- Baseline pass/fail state is known.
- Skipped checks have explicit reasons.
- Later tasks can compare against this baseline.

---

## 4. TASK-0003 — Apply runtime safety guards to all service entrypoints

### Objective
Ensure every long-running service fails closed on unsafe runtime
configuration (the dev-default `JWT_SECRET` in a non-dev env).

### Why
`assert_safe_for_runtime(settings)` existed in
`libs/fincept-core/src/fincept_core/config.py` and was used by the API
startup path only. The audit found that all other service entrypoints
called `get_settings()` without the guard before opening Redis
connections, stream consumers, schedulers, and broker-adjacent clients.

### Change pattern
In every entrypoint, immediately after `settings = get_settings()` and
before any Redis connection / heartbeat / stream read / scheduler start
/ broker client, add:

```python
# Fail closed on the dev JWT secret in non-dev envs before any Redis
# connection or <side-effect> side effect.  See audit R4 / P3.
assert_safe_for_runtime(settings)
```

The import line was updated from
`from fincept_core.config import get_settings` to
`from fincept_core.config import assert_safe_for_runtime, get_settings`.

### Files modified (16 entrypoints)

1. `C:\Users\nolan\CascadeProjects\fincept-terminal\services\api\src\api\main.py` (already had the guard; left as-is)
2. `C:\Users\nolan\CascadeProjects\fincept-terminal\services\ingestor\src\ingestor\main.py`
3. `C:\Users\nolan\CascadeProjects\fincept-terminal\services\orchestrator\src\orchestrator\main.py`
4. `C:\Users\nolan\CascadeProjects\fincept-terminal\services\oms\src\oms\main.py`
5. `C:\Users\nolan\CascadeProjects\fincept-terminal\services\strategy_host\src\strategy_host\main.py`
6. `C:\Users\nolan\CascadeProjects\fincept-terminal\services\features\src\features\main.py`
7. `C:\Users\nolan\CascadeProjects\fincept-terminal\services\jobs\src\jobs\main.py`
8. `C:\Users\nolan\CascadeProjects\fincept-terminal\services\portfolio\src\portfolio\main.py`
9. `C:\Users\nolan\CascadeProjects\fincept-terminal\services\agents\src\agents\gbm_predictor\main.py`
10. `C:\Users\nolan\CascadeProjects\fincept-terminal\services\agents\src\agents\sentiment_agent\main.py`
11. `C:\Users\nolan\CascadeProjects\fincept-terminal\services\agents\src\agents\regime_agent\main.py`
12. `C:\Users\nolan\CascadeProjects\fincept-terminal\services\agents\src\agents\information_enricher\main.py`
13. `C:\Users\nolan\CascadeProjects\fincept-terminal\services\agents\src\agents\sentiment_features\main.py`
14. `C:\Users\nolan\CascadeProjects\fincept-terminal\services\agents\src\agents\news_alpha_predictor\main.py`
15. `C:\Users\nolan\CascadeProjects\fincept-terminal\services\agents\src\agents\news_outcome_labeler\main.py`
16. `C:\Users\nolan\CascadeProjects\fincept-terminal\services\agents\src\agents\news_impact_agent\main.py`

### Test added
**`C:\Users\nolan\CascadeProjects\fincept-terminal\libs\fincept-core\tests\test_config.py`** (modified)
- `test_assert_safe_for_runtime_passes_in_dev_with_dev_secret`
- `test_assert_safe_for_runtime_fails_closed_in_staging` (also asserts
  the secret value is not echoed in the error)
- `test_assert_safe_for_runtime_passes_with_real_secret`

Result: 4 passed.

### Acceptance criteria met
- All service startup paths enforce the same runtime safety invariant.
- A prod-like env with the dev JWT secret fails before side effects.
- Error output is sanitized (no secret value in the message).
- Tests prove guard coverage (behaviour tests here; matrix tests in
  TASK-0102).

### Rollback
Revert only the affected service entrypoint changes if startup breaks
in dev. Keep the tests as a signal for why the rollback happened.

---

## 5. TASK-0004 — Lock down backtest file path handling

### Objective
Prevent the backtest API from reading arbitrary local files.

### Why
The audit found that `services/api/src/api/routes/backtest.py` accepted
`bars_path` as absolute or relative and checked only existence. An
authenticated caller could probe arbitrary local paths or force the API
to parse unexpected files.

### Files changed

**`C:\Users\nolan\CascadeProjects\fincept-terminal\libs\fincept-core\src\fincept_core\config.py`** (modified)
- New setting `BACKTEST_DATA_ROOTS: str = Field(default="data")`.
- Comma-separated approved roots for `/backtest/run` `bars_path`
  inputs. Resolved relative to the process CWD (repo root in normal
  operation). Never include `/` or a drive root.

**`C:\Users\nolan\CascadeProjects\fincept-terminal\services\api\src\api\routes\backtest.py`** (modified)
- New `_ALLOWED_BARS_SUFFIXES = frozenset({".parquet"})`.
- New `_approved_data_roots()` helper that resolves
  `BACKTEST_DATA_ROOTS` to absolute `Path` objects.
- New `resolve_bars_path(bars_path)` validator:
  - rejects empty / blank paths;
  - rejects `..` in path parts (traversal);
  - resolves with `Path.resolve()` so symlinks collapse;
  - rejects unsupported suffixes;
  - rejects paths outside all approved roots (uses
    `Path.relative_to` against each root);
  - rejects non-existent files;
  - **error messages never echo the host-specific absolute path** —
    they use the operator-supplied relative form only.
- `post_run` now calls `resolve_bars_path(body.bars_path)` instead of
  the old open `pathlib.Path(...).exists()` check.

**`C:\Users\nolan\CascadeProjects\fincept-terminal\.env.example`** (modified)
- Documented `FINCEPT_BACKTEST_DATA_ROOTS=data`.

**`C:\Users\nolan\CascadeProjects\fincept-terminal\docs\RISKS.md`** (modified)
- R-14 marked **MITIGATED (backtest)** with the fix description and
  test location. Training routes still TBD.

### Tests added
**`C:\Users\nolan\CascadeProjects\fincept-terminal\services\api\tests\test_backtest.py`** (modified)
- Updated the autouse `_patch_reports_root` fixture to also set
  `FINCEPT_BACKTEST_DATA_ROOTS` to `tmp_path` so synth fixtures pass
  the boundary check (production keeps the narrow `data` default).
- New `TestBarsPathBoundary` class:
  - `test_traversal_rejected` — `tmp_path / ".." / ".." / "secret.parquet"`
    returns 400 with "traversal".
  - `test_absolute_outside_root_rejected` — a sibling-of-tmp_path
    parquet returns 400 with "approved data roots"; asserts the host
    absolute path does not leak.
  - `test_unsupported_suffix_rejected` — a `.csv` returns 400 with
    "suffix" and ".csv".
  - `test_empty_path_rejected` — `"  "` returns 400 with "required".

Result: 13 passed (9 existing + 4 new boundary).

### Acceptance criteria met
- Valid fixture files under approved roots still work.
- `../` traversal fails.
- Absolute paths outside approved roots fail.
- Unsupported suffixes fail.
- Error responses do not leak host-specific absolute paths.

### Rollback
Temporarily widen the approved local root in dev only. Do not restore
arbitrary file access in staging or prod-like modes.

---

## 6. TASK-0005 — Sanitize auth and token error responses

### Objective
Prevent auth failures from exposing decoder details or sensitive
internals.

### Why
The system improvement report noted that API auth errors returned
detailed token decoder text (`f"invalid token: {exc}"`), which leaks
decoder internals useful to attackers.

### Files changed

**`C:\Users\nolan\CascadeProjects\fincept-terminal\services\api\src\api\auth.py`** (modified)
- New module docstring section documenting the sanitization policy
  (audit R3 / P3).
- New `_INVALID_TOKEN_DETAIL = "invalid or expired token"` constant —
  the generic, attacker-safe 401 detail reused for every decode
  failure (missing / malformed / expired / bad-signature all look the
  same to the client).
- The `except jwt.PyJWTError` branch now:
  - logs `auth.token_rejected` at debug level with `reason=<exception
    class name>` only (never the token value);
  - returns the generic detail;
  - includes `WWW-Authenticate: Bearer` header.
- Added `from fincept_core.logging import get_logger` and a module
  logger.

**`C:\Users\nolan\CascadeProjects\fincept-terminal\apps\dashboard\src\lib\ws.ts`** (modified)
- Added a SECURITY TRADEOFF docblock noting that the `?token=` query
  string is a v1 tradeoff (browsers can't set headers on WS upgrade)
  and that Phase H replaces it with a short-lived single-use WS ticket
  exchanged for an httpOnly-cookie-authenticated REST call.

### Tests added
**`C:\Users\nolan\CascadeProjects\fincept-terminal\services\api\tests\test_auth.py`** (modified)
- `test_malformed_token_returns_generic_401` — `"Bearer not.a.real.jwt"`
  returns the generic detail; asserts "DecodeError" and "Not enough
  segments" do not leak.
- `test_expired_token_returns_generic_401` — an `exp` in the past
  returns the generic detail; asserts "Signature has expired" does not
  leak.
- `test_wrong_key_token_returns_generic_401` — token signed with a
  different key returns the generic detail; asserts "Signature
  verification failed" does not leak.
- `test_token_value_not_in_response_body` — the raw token string never
  appears in the 401 response body.

Result: 9 passed (5 original + 4 new sanitization).

### Acceptance criteria met
- Malformed tokens return a generic 401.
- Expired tokens return a generic 401.
- No response includes raw decoder exception text.
- No token values appear in logs from tested paths (only the exception
  class name is logged, at debug level).

### Rollback
Restore previous message detail only in local debug logs, never in
client responses.

---

## 7. TASK-0101 — Create a verification receipt runner

### Objective
Add a safe default command that runs focused checks and writes a
durable receipt.

### File created
**`C:\Users\nolan\CascadeProjects\fincept-terminal\scripts\verification-receipt.ps1`**

A PowerShell script that:
- runs the 6 safe focused checks (3 dashboard test scripts, dashboard
  tsc, news-impact API pytest, core lib pytest);
- records 7 explicitly-skipped heavy checks with reasons (Docker
  Compose, browser smoke, live provider, broker, RunPod, full mypy,
  full pytest+cov);
- captures command, working directory, duration, status, and exit code
  per check;
- writes a timestamped Markdown receipt
  (`reports/verification/receipt-<stamp>.md`) and a JSON receipt
  (`receipt-<stamp>.json`) with schema `fincept.verification-receipt/v1`;
- exits non-zero if any **required** check fails (skipped checks do
  not fail the run);
- supports `-SkipDashboard`, `-SkipPython`, and `-OutDir` parameters;
- never includes secrets, tokens, or credentials in receipt content.

### Supporting files
**`C:\Users\nolan\CascadeProjects\fincept-terminal\reports\verification\.gitkeep`** (created)
- Keeps the directory tracked; timestamped receipt files are gitignored.

**`C:\Users\nolan\CascadeProjects\fincept-terminal\.gitignore`** (modified in TASK-0001)
- `/reports/verification/receipt-*.md` and `receipt-*.json` ignored.
- Manually-curated `baseline-*.md` receipts remain trackable.

### Test run
```
pwsh ./scripts/verification-receipt.ps1
```
Result: **Overall: PASS** (pass=6 fail=0 skipped=7), exit 0.

### Acceptance criteria met
- The command creates a timestamped receipt.
- Required failures produce a non-zero exit.
- Skipped checks are explicit, not silent.
- Receipt content never includes secrets.

### Rollback
Keep the script but mark it experimental if an environment-specific
command needs adjustment.

---

## 8. TASK-0102 — Add runtime safety matrix tests

### Objective
Prevent regressions in startup safety. A future service entrypoint can
accidentally omit the guard; these tests catch that.

### File created
**`C:\Users\nolan\CascadeProjects\fincept-terminal\libs\fincept-core\tests\test_startup_safety_matrix.py`**

- `SERVICE_ENTRYPOINTS` — explicit list of all 16 entrypoint paths
  that must enforce the guard. New entrypoints must be added here when
  created.
- `test_entrypoint_applies_runtime_guard` (parametrized over the 16
  paths) — AST source-inspection test that parses each entrypoint,
  finds the `from fincept_core.config import ... assert_safe_for_runtime
  ...` import, and confirms a call to that name exists. Fails the
  moment a commit drops the guard from any entrypoint, even one with
  no other unit test.
- `test_guard_fails_closed_on_dev_secret_in_non_dev_env` — behaviour
  check that staging + dev secret raises `ConfigError`.
- `test_guard_allows_dev_secret_in_dev_env` — behaviour check that dev
  + dev secret does not raise.

### Regression net verified
Temporarily replaced `assert_safe_for_runtime(settings)` with
`# GUARD_REMOVED` in `services/features/src/features/main.py`; the
matrix test failed for that entrypoint and passed for the other 15.
Restored the guard afterward.

Result: 18 passed (16 source-inspection + 2 behaviour).

### Acceptance criteria met
- Removing a service guard fails tests.
- Dev/local/test modes remain usable.
- Staging/prod-like unsafe defaults fail closed.

### Rollback
Adjust test strategy rather than removing the invariant.

---

## 9. TASK-0103 — Add backtest path boundary tests

### Objective
Prove the backtest path fix cannot regress.

### File changed
**`C:\Users\nolan\CascadeProjects\fincept-terminal\services\api\tests\test_backtest.py`** (modified — see TASK-0004 for the full detail)

The `TestBarsPathBoundary` class locks all path boundary cases:
- valid fixture path (covered by existing tests);
- traversal rejection;
- absolute outside-root rejection;
- unsupported extension rejection;
- sanitized error body assertion (host absolute path does not leak).

Result: 13 passed.

### Acceptance criteria met
- All path boundary cases are tested.
- Valid local fixtures remain usable.
- No absolute machine path leaks into client-facing errors.

---

## 10. TASK-0105 — Create an environment variable reference

### Objective
Document which env vars are required, secret, optional, local-only,
staging, or production.

### File created
**`C:\Users\nolan\CascadeProjects\fincept-terminal\docs\ENVIRONMENT.md`**

A full classified reference covering:
- conventions (env prefix, `NEXT_PUBLIC_` rule, never-commit-secrets);
- backend runtime mode & safety (`ENV`, `TRADING_MODE`, `JWT_SECRET`);
- backend storage & observability (`DB_URL`, `REDIS_URL`, OTLP, log
  level);
- backend exchange / data secrets (Binance, Polygon, FRED, NewsAPI,
  Finnhub, Tiingo);
- backend LLM providers (OpenAI, Anthropic, Tinker, `LLM_PROVIDER`);
- backend Alpaca (brokerage);
- backend trading behavior & risk (OMS_ROUTER, UNIVERSE, risk caps);
- backend HTTP & marks;
- backend backtest path boundary (`BACKTEST_DATA_ROOTS`);
- backend CORS;
- dashboard public vars (`NEXT_PUBLIC_API_URL`, `NEXT_PUBLIC_WS_URL`,
  `NEXT_PUBLIC_DEFAULT_STRATEGY`);
- dashboard server-only secrets (portfolio report provider keys);
- local dev quick start;
- staging / production checklist.

Each variable is tagged R/O/S/P (required / optional / secret /
public).

### Acceptance criteria met
- Operators can configure local/dev without guessing.
- Secret variables are clearly marked.
- Public dashboard variables are clearly separated from private server
  variables.

### Rollback
Documentation-only rollback.

---

## 11. Validation commands and final results

### Safe focused checks (all PASS)
```powershell
cd apps/dashboard
npm run test:shadow-news-impact      # 3 passed
npm run test:source-health           # 5 passed
npm run test:strategy-readiness      # 4 passed
cd ..\..
pnpm --dir apps/dashboard exec tsc --noEmit --pretty false   # exit 0
uv run pytest services/api/tests/test_news_impact.py -q       # 6 passed
uv run pytest libs/fincept-core/tests -q                      # 123 passed (119 + 4 new)
```

### New / updated test suites
```powershell
uv run pytest libs/fincept-core/tests/test_config.py -q                # 4 passed
uv run pytest libs/fincept-core/tests/test_startup_safety_matrix.py -q # 18 passed
uv run pytest services/api/tests/test_backtest.py -q                   # 13 passed
uv run pytest services/api/tests/test_auth.py -q                       # 9 passed
```

### Combined core + API run
```powershell
uv run pytest libs/fincept-core/tests services/api/tests -q            # 498 passed
```

### Receipt runner
```powershell
pwsh ./scripts/verification-receipt.ps1
# Overall: PASS (pass=6 fail=0 skipped=7), exit 0
# Receipts at reports/verification/receipt-<stamp>.md + .json
```

### Hygiene
```powershell
git diff --check    # clean (exit 0; CRLF notice is a warning, not an error)
git status --short  # local tool dirs no longer appear as untracked
```

### Tests not run (and why)
- Docker Compose boot — needs operator environment + Docker daemon.
- Browser smoke — needs Playwright session + running dashboard.
- Live provider checks — need provider API keys; never run by default.
- Broker checks — need broker credentials; never run by default.
- RunPod checks — not yet implemented (later phases).
- Full mypy + full pytest with coverage — heavy; run via
  `scripts/preflight.ps1` or CI, not the light receipt.

---

## 12. Pre-existing issues observed but left alone

- **`services/features/tests/test_price.py::test_first_bar_emits_all_none`**
  fails on the clean tree (before any of my changes) with a
  feature-name mismatch (`mom_15` vs `mom_20`/`mom_60`). This is a
  pre-existing bug in the feature computer, unrelated to the runtime
  guard. Confirmed by `git stash` + run + `git stash pop`. Do not
  attribute this failure to Phase 0/1 work.
- **Dashboard auth v1 tradeoffs** (localStorage tokens, WS query-string
  tokens) are documented in `ws.ts` as a later high-priority task. Not
  fixed in this phase per the plan's ordering.
- **Training route path handling** (the other half of R-14) is still
  TBD; only the backtest route was in scope for TASK-0004.
- **CI workflow hardening** (TASK-0104) was not implemented in this
  pass; it is the next Phase 1 task if you want to complete Phase 1
  fully before Phase 2.

---

## 13. All files created or modified (absolute paths)

### Created
- `C:\Users\nolan\CascadeProjects\fincept-terminal\docs\RELEASE_HYGIENE.md`
- `C:\Users\nolan\CascadeProjects\fincept-terminal\docs\ENVIRONMENT.md`
- `C:\Users\nolan\CascadeProjects\fincept-terminal\reports\verification\baseline-2026-06-22.md`
- `C:\Users\nolan\CascadeProjects\fincept-terminal\reports\verification\.gitkeep`
- `C:\Users\nolan\CascadeProjects\fincept-terminal\scripts\verification-receipt.ps1`
- `C:\Users\nolan\CascadeProjects\fincept-terminal\libs\fincept-core\tests\test_startup_safety_matrix.py`

### Modified
- `C:\Users\nolan\CascadeProjects\fincept-terminal\.gitignore`
- `C:\Users\nolan\CascadeProjects\fincept-terminal\.env.example`
- `C:\Users\nolan\CascadeProjects\fincept-terminal\docs\RISKS.md`
- `C:\Users\nolan\CascadeProjects\fincept-terminal\libs\fincept-core\src\fincept_core\config.py`
- `C:\Users\nolan\CascadeProjects\fincept-terminal\libs\fincept-core\tests\test_config.py`
- `C:\Users\nolan\CascadeProjects\fincept-terminal\services\api\src\api\auth.py`
- `C:\Users\nolan\CascadeProjects\fincept-terminal\services\api\src\api\routes\backtest.py`
- `C:\Users\nolan\CascadeProjects\fincept-terminal\services\api\tests\test_auth.py`
- `C:\Users\nolan\CascadeProjects\fincept-terminal\services\api\tests\test_backtest.py`
- `C:\Users\nolan\CascadeProjects\fincept-terminal\services\ingestor\src\ingestor\main.py`
- `C:\Users\nolan\CascadeProjects\fincept-terminal\services\orchestrator\src\orchestrator\main.py`
- `C:\Users\nolan\CascadeProjects\fincept-terminal\services\oms\src\oms\main.py`
- `C:\Users\nolan\CascadeProjects\fincept-terminal\services\strategy_host\src\strategy_host\main.py`
- `C:\Users\nolan\CascadeProjects\fincept-terminal\services\features\src\features\main.py`
- `C:\Users\nolan\CascadeProjects\fincept-terminal\services\jobs\src\jobs\main.py`
- `C:\Users\nolan\CascadeProjects\fincept-terminal\services\portfolio\src\portfolio\main.py`
- `C:\Users\nolan\CascadeProjects\fincept-terminal\services\agents\src\agents\gbm_predictor\main.py`
- `C:\Users\nolan\CascadeProjects\fincept-terminal\services\agents\src\agents\sentiment_agent\main.py`
- `C:\Users\nolan\CascadeProjects\fincept-terminal\services\agents\src\agents\regime_agent\main.py`
- `C:\Users\nolan\CascadeProjects\fincept-terminal\services\agents\src\agents\information_enricher\main.py`
- `C:\Users\nolan\CascadeProjects\fincept-terminal\services\agents\src\agents\sentiment_features\main.py`
- `C:\Users\nolan\CascadeProjects\fincept-terminal\services\agents\src\agents\news_alpha_predictor\main.py`
- `C:\Users\nolan\CascadeProjects\fincept-terminal\services\agents\src\agents\news_outcome_labeler\main.py`
- `C:\Users\nolan\CascadeProjects\fincept-terminal\services\agents\src\agents\news_impact_agent\main.py`
- `C:\Users\nolan\CascadeProjects\fincept-terminal\apps\dashboard\src\lib\ws.ts`

### Pre-existing dirty files NOT touched
All other modified/untracked files in the working tree (dashboard
shell/components, core events/schemas, API data/news_impact routes,
ingestor binance/eod_equity, jobs daily_eod_load, oms alpaca marks/
news_sync, news-impact-model experiment, etc.) were left alone per the
plan's staging discipline. Stage only the files listed above when
committing this phase.

---

## 14. What comes next (in plan order)

1. **TASK-0104** — Harden CI and supply chain defaults (pin actions,
   least-privilege permissions, lockfile discipline). Optional now to
   finish Phase 1 fully.
2. **TASK-0201** — Generate a dashboard route and mock-data atlas.
3. **TASK-0202** — Build a unified system readiness center.
4. **TASK-0203** — Add on-demand module control for local and staging.
5. **TASK-0204** — Add dashboard fetch timeouts and better error states.
6. **TASK-0205** — Build provider evidence redaction and freshness
   receipts.
7. Phase 3 onward: Quant Foundry contracts, settlement ledger, dossier
   registry, tournament scoring, feature lake, then (and only then)
   RunPod / GPU / shadow inference / promotion / paper bridge.

Per the plan's non-negotiable rule: **do not start RunPod, GPU,
tournament, promotion, or paper-bridge tasks until the safety and
evidence foundations are complete.** Phase 0 + Phase 1 (less
TASK-0104) are now complete.
