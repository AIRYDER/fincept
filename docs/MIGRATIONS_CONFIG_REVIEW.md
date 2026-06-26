# Migrations & Configuration Review — `codex/portfolio-optimizer-core`

**Branch:** `codex/portfolio-optimizer-core`
**Base:** `main`
**Reviewer:** Devin (read-only review; no code changed)
**Date:** 2026-06-23
**Scope:** Database migrations, configuration files, environment examples, lockfiles, CI/CD workflow definitions, and runtime launch scripts changed between `main` and `HEAD` (`751d212`).

This review covers **compatibility, rollback, and deployment risks** only. It does not assess feature correctness, test coverage, or business logic.

---

## 1. Summary of changes in scope

| Area | Files | Nature |
|---|---|---|
| DB migration | `libs/fincept-db/.../migrations/versions/0003_provider_data.py` | **New** additive migration (new table + hypertable + compression) |
| Core config | `libs/fincept-core/src/fincept_core/config.py` | New `ENV` setting + `assert_safe_for_runtime()` guard |
| Core schemas | `libs/fincept-core/src/fincept_core/schemas.py` | New `InformationEvent` model (additive) |
| Strategy config store | `libs/fincept-core/src/fincept_core/strategy_config.py` (+ tests) | **New** filesystem-backed config store |
| Quant Foundry schemas | `services/quant_foundry/src/quant_foundry/schemas.py` (+ tests) | **New** package — strict cross-boundary contracts |
| Workspace manifests | `pyproject.toml`, `services/api/pyproject.toml`, `services/quant_foundry/pyproject.toml`, `services/strategy_host/pyproject.toml` | New workspace members + new deps on `api` |
| Lockfiles | `uv.lock`, `pnpm-lock.yaml` | Workspace membership + dep additions |
| Env examples | `.env.example`, `apps/dashboard/.env.example` | New keys: `EXA_API_KEY`, `OPENBB_API_URL`, dashboard LLM keys; **API port 8000 → 8010** |
| Launch scripts | `start.bat`, `scripts/start.ps1` | New service lanes, OpenBB bootstrap, **default API port 8010**, Memurai elevation |
| Strategy instance files | `strategies/alpaca.live.json`, `strategies/alpaca.live.history.jsonl` | **New** committed runtime state file (enabled strategy) |
| CI workflows | `.github/workflows/ci.yml`, `build-images.yml`, `nightly.yml` | Pinned action SHAs, least-privilege `permissions`, new required jobs (receipt, startup-safety, lockfile-sync) |

---

## 2. Database migration — `0003_provider_data`

**File:** <ref_file file="C:\Users\nolan\CascadeProjects\fincept-terminal\libs\fincept-db\src\fincept_db\migrations\versions\0003_provider_data.py" />

### 2.1 Compatibility
- **Additive only.** Creates a new `provider_data` table; does **not** alter `bars` or `features` (from `0001`/`0002`). Existing tables and their data are untouched.
- **Revision chain is linear and consistent:** `0002` → `0003`, `down_revision = "0002"`. No branching/merge heads.
- Uses TimescaleDB-specific DDL: `create_hypertable`, `timescaledb.compress`, `add_compression_policy`. **Requires the TimescaleDB extension** to be installed on the target Postgres instance — this is already a precondition from `0002` (the `features` hypertable), so no new platform requirement is introduced.
- Composite primary key `(record_id, ts_event)` — both `NOT NULL`. Inserters must supply both.
- `JSONB` columns `request`, `normalized`, `raw` are `NOT NULL` with no `server_default`. Any inserter that omits them will fail at write time. This is intentional (capture-everything semantics) but is a **contract change for any code path that writes to this table** — confirm all writers populate all three JSONB columns.

### 2.2 Rollback
- `downgrade()` is **complete and symmetric**: removes the compression policy, drops all three indexes, then drops the table. Order is correct (policy → indexes → table).
- `remove_compression_policy(..., if_exists => TRUE)` and `if_not_exists => TRUE` on `create_hypertable` are idempotent — re-running `upgrade` on a partially-migrated DB will not crash on the hypertable/compression steps.
- **Rollback is destructive:** dropping `provider_data` loses all captured provider responses. There is no data-preserving downgrade. Acceptable for a capture/audit table, but operators must be warned that `alembic downgrade -1` is not reversible without re-ingest.

### 2.3 Deployment risks
- **TimescaleDB version compatibility:** `add_compression_policy` and `compress_segmentby` syntax is stable across TimescaleDB ≥ 2.0; no risk for any currently-supported version.
- **Chunk interval `86400000000000` (ns)** = 1 day, matching `0002`'s `features` table. Consistent.
- **Compression policy at 14 days** — once compressed, rows are immutable. Any backfill job that writes rows older than 14 days will hit compressed chunks and fail unless `decompress_chunk` is run first. Document this for operators.
- **No retention policy** is added (matches `0002`'s stated decision). `provider_data` will grow unbounded — monitor disk. This is a known/accepted posture but worth flagging for capacity planning.
- **Index count:** 3 indexes on a high-write capture table. `ix_provider_data_request_hash` is a plain btree on a 64-char hash — fine. The `(provider, dataset, ts_event)` and `(symbol, ts_event)` indexes are appropriate for the documented query patterns but add write overhead; expected for a capture table.

---

## 3. Core configuration — `fincept_core.config`

**File:** <ref_file file="C:\Users\nolan\CascadeProjects\fincept-terminal\libs\fincept-core\src\fincept_core\config.py" />

### 3.1 New `ENV` setting
- `ENV: str = Field(default="dev")` — **new required-ish setting**. Default `"dev"` keeps current behavior, so existing deployments that do not set `FINCEPT_ENV` continue to work.
- **Risk:** `.env.example` was **not** updated to document `FINCEPT_ENV`. Operators copying `.env.example` to `.env` for a staging/production deploy will silently run with `ENV=dev`, which **disables** the new JWT-secret guard (see below). **Action recommended:** add `FINCEPT_ENV=production` guidance to `.env.example`.

### 3.2 `assert_safe_for_runtime()` guard
- Adds a fail-closed check: in non-dev environments (`staging`/`production`/anything not in `{dev, local, test}`), starting a service with the default JWT secret `dev-only-change-me` (or an empty secret) raises `ConfigError` and the process refuses to start.
- **Compatibility:** purely additive; only fires when explicitly called by an entrypoint. Services that do not call it are unaffected.
- **Rollback:** N/A (no persistent state). Removing the call sites reverts behavior.
- **Deployment risk — HIGH:** `.env.example` still ships `FINCEPT_JWT_SECRET=dev-only-change-me`. If an operator copies it verbatim into a production `.env` **and** sets `FINCEPT_ENV=production`, services will **refuse to start**. This is the intended fail-closed behavior, but it will surface as a hard outage on first prod deploy if the secret is not rotated. Mitigation: update `.env.example` to leave `FINCEPT_JWT_SECRET=` blank with a comment, and document the `ENV` interaction in deployment runbooks.
- The guard is case-insensitive and trims `ENV`, which is good. The allow-list `{dev, local, test}` is hardcoded — adding a new non-dev env name (e.g. `preview`) would bypass the guard **silently** (treated as production-like, which is safe-by-default since it's not in the allow-list). Behavior is correct, but the inverse risk exists: a typo like `FINCEPT_ENV=producation` would also fail-closed. Acceptable.

### 3.3 `InformationEvent` schema
- New frozen, `extra="forbid"` Pydantic model in `schemas.py`. **Additive** — does not modify existing models. No migration/rollback concern.
- `schema_version: int = 1` is set; future breaking changes should bump this. No version-gate enforcement exists yet — flag for when consumers span versions.

---

## 4. Strategy config store — `fincept_core.strategy_config`

**File:** <ref_file file="C:\Users\nolan\CascadeProjects\fincept-terminal\libs\fincept-core\src\fincept_core\strategy_config.py" />

### 4.1 Compatibility
- **New module.** Filesystem-backed persistence under `$STRATEGIES_DIR` (default `strategies/`).
- `from_dict` is **tolerant of missing optional keys** (`symbols`, `params`, `model_binding`, `enabled`, timestamps) — explicitly designed to read older on-disk shapes. Good forward/backward compatibility.
- Empty-string `model_binding` is normalized to `None` (handles dashboard forms submitting `""`). Good.
- `strategy_id` validation rejects path-traversal characters and leading-dot names — matches the existing `agent_id`/`model_name` policy in `api.promotions`. The module docstring notes these three must be kept in sync.

### 4.2 Rollback
- Removing the module leaves orphan files under `strategies/`. They are inert JSON/JSONL and can be deleted manually. No DB state to roll back.
- The store is append-only for history (`<id>.history.jsonl`); there is no compaction. Long-running deployments will accumulate history lines. Not a rollback issue, but a **growth** concern.

### 4.3 Deployment risks
- **Committed runtime state:** `strategies/alpaca.live.json` and `strategies/alpaca.live.history.jsonl` are **checked into git** with `enabled: true` and a real symbol list (AI, AMD, AMZN, ...). This means:
  - Any clone + `start.bat` will **start a live position-tracking strategy by default** (once the strategy host runs). Confirm this is intended for a paper-trading repo. If `position_tracker` is read-only (no orders), the risk is low; if it can emit `OrderIntent`s, this is a **live-trading footgun** on first run.
  - The history JSONL records a toggle `false → true` with wall-clock timestamps `1778277232` / `1778277256`. These are **future-dated** (Unix ~2026-05) and consistent with the repo's synthetic timeline, but operators should be aware the file ships pre-seeded.
- **`STRATEGIES_DIR` override** is respected via env var. Containers/tests should set it to an ephemeral path; otherwise the host will read the repo's `strategies/` dir, which is version-controlled and may surprise operators who hand-edit a file and find it reverted by `git pull`.

---

## 5. Quant Foundry schemas — `services/quant_foundry/src/quant_foundry/schemas.py`

**File:** <ref_file file="C:\Users\nolan\CascadeProjects\fincept-terminal\services\quant_foundry\src\quant_foundry\schemas.py" />

### 5.1 Compatibility
- **New package.** All models are `frozen=True, extra="forbid"` with `schema_version: int = 1`. Strict-by-default is good for cross-boundary contracts to untrusted (RunPod) workers.
- `Authority` enum is a `StrEnum` with a single member `SHADOW_ONLY` — enforces shadow-only predictions at the type level. Adding new authorities later is a **non-breaking** enum extension, but consumers that pattern-match exhaustively on `Authority` will need updating.

### 5.2 Rollback / Deployment
- No persistent state of its own; contracts only. Rollback = remove package + dependents.
- **Deployment risk:** `extra="forbid"` means any field added by a worker that isn't in the schema will **reject the callback**. This is the intended security posture (untrusted workers), but it makes forward compatibility strict: deploying a new worker version that adds a payload field requires deploying the schema update **first**. Document this ordering in the RunPod release runbook.

---

## 6. Workspace manifests & lockfiles

### 6.1 `pyproject.toml` (root)
- Adds `services/strategy_host` and `services/quant_foundry` to `members`.
- Adds `strategy_host = { workspace = true }` to `[tool.uv.sources]`.
- **Note:** `quant_foundry` is added to `members` but **not** to root `[tool.uv.sources]` (only `strategy_host` is). This is fine — `quant_foundry` is sourced transitively via `services/api`. No inconsistency, but worth verifying `uv lock` resolves it as intended (the lockfile diff confirms `strategy-host` is locked; **`quant-foundry` does not appear in the `uv.lock` diff** — see risk below).

### 6.2 `services/api/pyproject.toml`
- Adds `fincept-tools` and `quant-foundry` as runtime deps. This means **the API service now imports quant_foundry at runtime**. Confirm the API does not gate this import behind a try/except, otherwise a quant_foundry packaging error becomes an API startup failure.

### 6.3 `uv.lock`
- Adds the `strategy-host` package entry. **`quant-foundry` is NOT present in the `uv.lock` diff** despite being added to root `members` and to `services/api` deps. This is a **lockfile-drift risk**: `uv lock --check` (now a required CI job, see §8) may fail, or the lock may be incomplete. **Action: verify `uv lock --check` passes locally and that `quant-foundry` resolves.** The new `lockfile-sync` CI job will catch this on PR, but it should be confirmed before merge.
- No third-party version bumps in the diff — only workspace-internal additions. Low supply-chain risk.

### 6.4 `pnpm-lock.yaml`
- Changed but not inspected in detail here; the new `lockfile-sync` CI job enforces `pnpm install --frozen-lockfile` parity. No manual action needed beyond ensuring CI is green.

### 6.5 Rollback
- Removing the workspace members and re-running `uv lock` regenerates the prior lockfile state. No persistent data.

---

## 7. Environment examples & launch scripts

### 7.1 `.env.example` (root)
- Adds `EXA_API_KEY=` (empty) and `OPENBB_API_URL=http://127.0.0.1:6900`.
- **Does NOT add `FINCEPT_ENV=`** — see §3.1 risk.
- **Still ships `FINCEPT_JWT_SECRET=dev-only-change-me`** — see §3.2 risk. Combined with the new fail-closed guard, this is the single highest-impact deployment trap in this branch.

### 7.2 `apps/dashboard/.env.example`
- **API URL changed from `:8000` to `:8010`** (both `NEXT_PUBLIC_API_URL` and `NEXT_PUBLIC_WS_URL`). This is a **breaking change for any existing dashboard deployment** that sourced env from the prior example. Operators must update their `.env` (or runtime env) to `:8010` or the dashboard will point at a dead port.
- Adds server-only LLM keys: `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, model selectors, and `PORTFOLIO_REPORT_MAX_OUTPUT_TOKENS`. The example correctly notes these must NOT be `NEXT_PUBLIC_`-prefixed. **Deployment risk:** any operator who accidentally prefixes them with `NEXT_PUBLIC_` will ship API keys to the browser bundle. The example is correct; add a CI/build-time grep guard if not already present.
- Model names `gpt-5.5` and `claude-opus-4-7` are hardcoded defaults — verify these are valid model identifiers in the target provider accounts before deploying the portfolio report feature.

### 7.3 `start.bat` / `scripts/start.ps1`
- **Default API port changed 8000 → 8010** (`$ApiPort = 8010`). This is the second half of the port migration. Any external integrations, firewalls, or docs referencing `:8000` must be updated. The change is consistent between `start.bat`, `start.ps1`, and `apps/dashboard/.env.example`.
- `start.ps1` gains many new opt-in lanes (`-WithMarketData`, `-WithGbm`, `-WithOpenBB`, `-Full`, etc.) and a `-NoServices`/`-NoOpenBB` escape hatch. Default (no flags) starts a "lean core" set: Redis, API, Dashboard, strategy host, orchestrator, OMS, portfolio. **This is a behavior change from the prior default** (which started only Redis + API + Dashboard). Operators running `start.bat` unattended will now get **more processes** than before. Confirm this is intended and that the lean set does not require credentials absent from a fresh clone.
- New `Start-MemuraiWithElevation` triggers a UAC prompt via `Start-Process -Verb RunAs`. On unattended/headless startup this will **block waiting for UAC consent**. The script handles cancellation with a warning, but a scheduled-task deployment will hang here. Document the headless path (pre-start the Memurai service out-of-band).
- `start.bat` now skips the "Press any key to close" pause when arguments are passed (`if not "%~1"=="" goto end`). Minor UX change; correct for scripted invocations.

### 7.4 Rollback (env/scripts)
- Env example changes are documentation only; revert by restoring the prior file.
- Port change has no persistent state but **external consumers must be updated in lockstep**. Rolling back the code without rolling back operator-managed `.env` files / reverse proxies will break the dashboard.

---

## 8. CI workflows

### 8.1 Action pinning
- All third-party actions are pinned to **commit SHAs** with a `# pin: <name>@vN` comment preserving the human-readable reference. This is a security best practice (protects against tag re-pointing). **Maintenance cost:** future updates require finding the new SHA; the `# pin:` comment makes this discoverable.

### 8.2 Least-privilege `permissions`
- `ci.yml`, `build-images.yml`, `nightly.yml` all add explicit `permissions: contents: read` (plus `packages: write` only on `build-images`). This **tightens** the default GITHUB_TOKEN scope. **Compatibility risk:** any existing job that relied on the default `contents: write` (e.g., a commit-back step) will now fail. Inspected diffs show no such job, so this is safe — but verify no other workflow in the repo does an implicit write.

### 8.3 New required CI jobs (in `ci.yml`)
- **`receipt-runner`** — runs `./scripts/verification-receipt.ps1` via `pwsh` and uploads `reports/verification/`. Requires `pwsh` on the runner (ubuntu-latest has it via PowerShell Core). Confirm the script path and `reports/verification/` output exist or the job's `if-no-files-found: warn` will only warn (not fail) — acceptable.
- **`startup-safety-matrix`** — runs a specific pytest file. If that file is missing or renamed, the job fails hard. Pin the test path or make it tolerant.
- **`lockfile-sync`** — runs `uv lock --check` and `pnpm install --frozen-lockfile`. **This is the job most likely to fail on merge** if §6.3's `quant-foundry` lockfile drift is real. Verify locally before merge.

### 8.4 Rollback
- Workflow changes are repo-state only; revert via git. No external side effects. Note that **making these jobs required** on the branch-protection ruleset means reverting them will block future PRs until branch protection is also updated.

---

## 9. Consolidated risk register

| # | Risk | Severity | Area | Recommended action |
|---|---|---|---|---|
| R1 | `.env.example` ships `FINCEPT_JWT_SECRET=dev-only-change-me` alongside a new fail-closed guard | **High** | config | Blank the value in `.env.example`; add a comment; document in deploy runbook |
| R2 | `.env.example` does not document `FINCEPT_ENV` | Medium | config | Add `FINCEPT_ENV=dev` with staging/prod guidance |
| R3 | `quant-foundry` missing from `uv.lock` diff despite being a workspace member + api dep | Medium | lockfile | Run `uv lock --check` locally; regenerate if needed before merge |
| R4 | API port changed 8000 → 8010; external consumers/docs may break | Medium | deploy | Update all references; communicate to operators |
| R5 | `strategies/alpaca.live.json` committed with `enabled: true` | Medium | runtime state | Confirm `position_tracker` is read-only; otherwise default to `enabled: false` in the committed file |
| R6 | `start.ps1` default now launches more services (strategy host, orchestrator, OMS, portfolio) | Low–Medium | deploy | Document the new default lean set; ensure no credentials required for lean set |
| R7 | UAC elevation for Memurai blocks headless/scheduled startup | Low | deploy | Document headless pre-start procedure |
| R8 | `provider_data` has no retention policy; unbounded growth | Low | db | Add monitoring; consider a future retention migration |
| R9 | Backfills into `provider_data` older than 14d hit compressed chunks | Low | db | Document `decompress_chunk` procedure for backfills |
| R10 | Quant Foundry `extra="forbid"` requires schema-first deploy ordering vs RunPod workers | Low | contracts | Document deploy ordering in RunPod runbook |
| R11 | New CI jobs (receipt, startup-safety, lockfile-sync) become required → branch protection must stay in sync | Low | ci | Coordinate with branch-protection ruleset on revert |

---

## 10. Overall assessment

- **Migrations:** Safe and additive. The `0003` migration is well-formed with a symmetric downgrade and idempotent TimescaleDB calls. No data-migration or backfill step is required.
- **Configuration:** The new `ENV`/JWT guard is a strong safety improvement, but its **interaction with the unchanged `.env.example` creates a deployment trap** (R1/R2) that should be fixed before this branch ships to a staging/production environment.
- **Lockfiles:** Verify `uv.lock` parity for `quant-foundry` (R3) — the new `lockfile-sync` CI job will enforce this, but it should pass on the merge commit, not fail.
- **Launch scripts:** The port migration (8000→8010) and the expanded default service set are the operator-facing breaking changes; both are low-risk if communicated.
- **CI:** Pinning and least-privilege `permissions` are strict improvements. The new required jobs are appropriately scoped.

**Recommendation:** Address R1, R2, and R3 before merging to `main`. The remaining items are documentation/operational and can land with runbook updates.
