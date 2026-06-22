# Fincept Terminal Big Implementation Plan

## Swarm Worker Project Context

This section is the quick-start context for any swarm worker pointed at this file. Read it before choosing a task. The rest of the document is the ordered implementation plan.

### Project Identity

- **Repo:** `C:\Users\nolan\CascadeProjects\fincept-terminal`
- **System name:** Fincept Terminal
- **Product shape:** Internal, one-operator, AI-assisted stock and crypto trading/research terminal.
- **Primary operator workflow:** Start the dashboard and core control plane first, then start optional modules such as OpenBB, news analysis, backtests, provider sync, Quant Foundry jobs, or RunPod workloads only when needed.
- **Important repo posture:** This is a trading-adjacent system. Safety, auditability, reproducibility, and rollback matter more than flashy model output.
- **Current plan artifact:** `AAAAAAAAA_BIG_PLAN.md` is the master implementation order. It is intentionally ordered. Do not treat later Quant Foundry, RunPod, tournament, or paper/live bridge tasks as independent starting points.

### What The System Is Trying To Become

Fincept should become a reliable operator console and quant research platform with these layers:

1. **Stable Fincept core**
   - Dashboard, API, Redis streams, Timescale/Postgres, existing agents, features, risk, OMS, backtester, and provider integrations.
2. **Safe operator workflow**
   - The operator can see readiness, start and stop optional modules, view receipts, inspect degraded states, and avoid paying for idle heavy workers.
3. **Quant Foundry intelligence layer**
   - A shadow-only quant ML platform that trains models, imports artifacts, runs live non-trading predictions, settles predictions, scores models, and recommends promotions.
4. **Governed model promotion**
   - Models can move from candidate to research-approved to shadow-approved to paper-approved only with dossiers, settlement evidence, tournament scores, receipts, and human approval.
5. **Future limited-live readiness**
   - Only after long shadow and paper evidence. RunPod must never own trading authority.

### Current Architecture Map

Use this map before changing files:

- **Dashboard app:** `apps/dashboard`
  - Next.js app router, React, TypeScript.
  - Important client/API helper files include `apps/dashboard/src/lib/api.ts`, `apps/dashboard/src/lib/auth.ts`, and `apps/dashboard/src/lib/ws.ts`.
  - Important shell/operator files include `apps/dashboard/src/components/shell/` and dashboard routes under `apps/dashboard/src/app/`.
- **Main API:** `services/api`
  - FastAPI service.
  - Existing routes live in `services/api/src/api/routes/`.
  - Model/training surfaces include `services/api/src/api/routes/models.py` and `services/api/src/api/training.py`.
  - News-impact shadow route is `services/api/src/api/routes/news_impact.py`.
- **Core library:** `libs/fincept-core`
  - Shared schemas, config, settings, event models, and prediction logging.
  - Runtime safety guard lives in `libs/fincept-core/src/fincept_core/config.py`.
- **Event bus library:** `libs/fincept-bus`
  - Redis stream names live in `libs/fincept-bus/src/fincept_bus/streams.py`.
  - Existing trading-adjacent streams include `sig.predict`, `ord.decisions`, `ord.orders`, `ord.fills`, and `ord.positions`.
- **Database library:** `libs/fincept-db`
  - Migrations and storage helpers for bars, features, provider data, and related persistence.
- **Long-running services:** `services/ingestor`, `services/features`, `services/agents`, `services/orchestrator`, `services/risk`, `services/oms`, `services/backtester`, `services/jobs`, and `services/strategy_host`.
- **Shadow/news-impact experiment:** `experiments/news-impact-model` plus API/dashboard wiring in `services/api/src/api/routes/news_impact.py` and `apps/dashboard/src/components/news-impact/`.
- **Future Quant Foundry package:** `services/quant_foundry`
  - This package is planned in this file. Do not assume it exists until the relevant task creates it.
- **Future RunPod workers:** `runpod/quant-foundry-training/` and `runpod/quant-foundry-inference/`
  - These are planned later. Do not create them before contracts, receipts, settlement, and dossiers exist.

### Non-Negotiable Safety Invariants

Every worker must preserve these:

- **No RunPod worker gets broker credentials.**
- **No RunPod worker writes to `ord.orders`, `ord.decisions`, `ord.fills`, or `ord.positions`.**
- **No RunPod worker writes directly to `sig.predict`.**
- **Shadow predictions stay in a separate Quant Foundry shadow ledger until an explicit paper bridge task is implemented and approved.**
- **The existing OMS and risk services remain authoritative for any order path.**
- **Model promotion requires a dossier, settlement evidence, tournament score, receipt, and human approval.**
- **External callbacks must be signed, schema-validated, idempotent, and stored before processing.**
- **Artifacts must be pulled and hash-verified before registration.**
- **Unsafe runtime config must fail closed before services touch Redis, streams, schedulers, or broker-adjacent clients.**
- **User-controlled paths must be allowlisted and prefix-checked before file access.**
- **Secrets never go into source files, client bundles, logs, receipts, or dashboard responses.**

### Current Highest-Risk Issues To Fix First

Do these before building new intelligence:

1. Runtime safety guard is not consistently applied across every service entrypoint.
2. Backtest file path handling is too broad and must be restricted to approved roots.
3. The repo has a broad dirty/untracked working tree and needs careful staging discipline.
4. Full-system proof is not yet captured in a durable verification receipt.
5. Dashboard auth still has v1 tradeoffs such as localStorage tokens and query-string WebSocket tokens.
6. Mock/live route readiness is not centralized.
7. Provider evidence needs redaction, freshness receipts, and clear review surfaces.

### How To Choose Work From This File

- Pick the earliest incomplete task that matches your assignment.
- If a later task depends on an earlier task, stop and implement or request the earlier task first.
- Do not start RunPod, GPU, tournament, promotion, or paper-bridge tasks until the safety and evidence foundations are complete.
- If you are a swarm worker with a narrow assignment, inspect the task's listed files plus the source evidence section before editing.
- If the task says a future file is "likely created," create it only when implementing that task.
- If current repo state differs from this plan, trust the repo and update the plan or handoff note with evidence.

### Worker Start Checklist

Run this mental checklist before changing files:

1. Confirm you are in `C:\Users\nolan\CascadeProjects\fincept-terminal`.
2. Run `git status --short` and avoid touching unrelated user changes.
3. Read the task section you are implementing, including dependencies, tests, risk, and rollback.
4. Inspect the exact files listed for that task.
5. Add or update tests first when the task changes behavior.
6. Keep changes scoped to the task.
7. Do not broad-stage the repo.
8. Do not run live provider, broker, GPU, or destructive commands unless the task explicitly requires it and the operator has provided the needed environment.

### Worker Validation Commands

Use focused commands. Run only the commands relevant to your task.

General safe dashboard checks:

```powershell
cd apps/dashboard
npm run test:shadow-news-impact
npm run test:source-health
npm run test:strategy-readiness
cd ..\..
pnpm --dir apps/dashboard exec tsc --noEmit --pretty false
```

General safe Python checks:

```powershell
uv run pytest services/api/tests/test_news_impact.py -q
uv run pytest libs/fincept-core/tests -q
uv run pytest services/api/tests -q -k "<task-specific-keyword>"
```

Markdown and patch hygiene:

```powershell
git diff --check
git status --short
```

Future Quant Foundry checks after `services/quant_foundry` exists:

```powershell
uv run pytest services/quant_foundry/tests -q
uv run pytest services/api/tests -q -k quant_foundry
pnpm --dir apps/dashboard exec tsc --noEmit --pretty false
```

### Testing Expectations

- Every behavior-changing task needs tests.
- Every cross-boundary task needs idempotency and failure-mode tests.
- Every external callback task needs bad-signature and bad-schema tests.
- Every artifact task needs bad-hash and unsupported-URI tests.
- Every dashboard task needs loading, empty, degraded, disabled, and error states where applicable.
- Every security-sensitive task needs negative tests, not only happy-path tests.
- Do not weaken or delete failing tests to get green output.

### Security Discipline For Workers

Treat every external input as hostile:

- HTTP request bodies;
- callback payloads;
- file paths;
- provider payloads;
- model artifacts;
- environment variables;
- dashboard query parameters;
- Redis stream rows;
- object storage URIs.

For every trust boundary, validate:

- authentication;
- authorization;
- schema;
- allowed values;
- path or URI allowlist;
- size limits;
- timeout behavior;
- sanitized error output;
- redacted logging.

Dangerous sinks to watch for:

- shell command execution;
- arbitrary file reads;
- SQL string construction;
- `dangerouslySetInnerHTML`;
- user-controlled fetch URLs;
- deserialization of untrusted bytes;
- model artifact loading;
- direct stream writes to trading-adjacent streams.

### Quant Foundry Target Contract

The eventual Quant Foundry connection pattern is:

1. Fincept writes a job to a durable outbox.
2. A dispatcher sends the job to local mock mode or RunPod.
3. Worker produces compact callback metadata.
4. Fincept receives signed callback.
5. Callback is stored in a durable inbox before processing.
6. Schemas and signatures are validated.
7. Artifacts are pulled by Fincept, not pushed blindly into Fincept.
8. Artifacts are hash-verified and registered into dossiers.
9. Shadow predictions are stored separately from existing trading streams.
10. Settlement later scores predictions against realized outcomes.
11. Tournament ranks models and produces recommendations.
12. Human review approves or rejects promotion.
13. Only a later paper-only bridge can publish approved predictions to existing Fincept prediction flow.

### Deployment Context For Workers

The intended hosting posture is:

- **Local first:** safest for early development and contract tests.
- **Railway for testing/staging:** good for a one-operator dashboard/API/control-plane smoke surface, especially when optional modules can sleep or stay disabled.
- **RunPod for GPUs:** training, batch model evaluation, and shadow inference. Not core trading authority.
- **AWS later for serious control plane:** ECS/App Runner style API services, S3 artifacts/receipts, Secrets Manager, managed Redis, managed Postgres/Timescale-compatible storage, CloudWatch, ALB/WAF.

Cost principle:

- Keep dashboard/API/control plane light.
- Start heavy modules only when the operator asks.
- Stop optional modules after idle time.
- Add budget guards before GPU, backtest, provider sync, and tournament replay jobs.

### Worker Completion Checklist

Before handing off:

1. Re-read the task acceptance criteria.
2. Run the focused tests listed for the task.
3. Run `git diff --check`.
4. Summarize changed files.
5. Summarize tests run and results.
6. State any tests not run and why.
7. State any pre-existing unrelated dirty files you avoided.
8. Do not claim production, hosted, live provider, broker, or RunPod proof unless you actually performed that proof.

---

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` for multi-task execution, or `superpowers:executing-plans` when implementing inline. This plan is intentionally ordered. Do not skip ahead to GPU, model, promotion, or deployment work before the safety and evidence phases are done.

**Goal:** Implement the safest, highest-leverage path from the current Fincept Terminal system to a reliable operator dashboard plus Fincept Quant Foundry, while keeping the existing system running at every step.

**Architecture:** The system should remain a safety-first, event-driven Fincept core with optional on-demand modules. Quant Foundry should connect through durable contracts, outboxes, signed callbacks, shadow ledgers, settlement evidence, model dossiers, and human approval gates before it can influence paper or live workflows.

**Tech Stack:** Python 3.12, `uv` workspace services, FastAPI API service, Redis streams, Timescale/Postgres, Next.js dashboard, TypeScript, targeted Node dashboard test scripts, Docker Compose for local infrastructure, Railway as a useful dev/staging host, AWS as the likely serious control-plane host later, and RunPod for GPU research and shadow inference only.

---

## 0. Plan Principles

This file gives the implementation order across the whole system. It combines the current repo audit, the Quant Foundry design, the connectivity/module-development design, and the deployment/cost conversation into one execution path.

The most important rule is this:

**Do not scale intelligence before the evidence loop exists.**

RunPod, GPU clusters, shadow inference, tournament scoring, and frontier model research are powerful only if Fincept can later prove which predictions were good, which were bad, why they were bad, and whether they were economically useful after costs. If we add massive training before settlement, dossiers, receipts, and guardrails, we create expensive noise. If we add the evidence loop first, every future model makes the system smarter.

The second rule:

**Every phase must leave the system usable.**

No phase should require breaking the dashboard, API, existing local startup, or current shadow/news-impact work. Each task should be small enough to validate independently and easy to roll back.

The third rule:

**RunPod never owns trading authority.**

RunPod can train models, run shadow predictions, return artifacts, generate dossiers, and recommend promotions. It cannot write order streams, call OMS, hold broker credentials, promote itself, or bypass Fincept risk controls.

---

## 1. Source Evidence Used

This plan is grounded in these repo files and prior docs:

- `docs/SYSTEM_IMPROVEMENT_REPORT.md`
- `docs/superpowers/specs/2026-06-21-fincept-quant-foundry-design.md`
- `docs/superpowers/specs/2026-06-21-fincept-quant-foundry-connectivity-module-development.md`
- `docs/ROADMAP.md`
- `docs/RISKS.md`
- `featuresmenu.md`
- `pyproject.toml`
- `apps/dashboard/package.json`
- `libs/fincept-bus/src/fincept_bus/streams.py`
- `libs/fincept-core/src/fincept_core/schemas.py`
- `libs/fincept-core/src/fincept_core/config.py`
- `libs/fincept-core/src/fincept_core/prediction_log.py`
- `services/api/src/api/main.py`
- `services/api/src/api/routes/models.py`
- `services/api/src/api/routes/news_impact.py`
- `services/api/src/api/training.py`
- `services/features/src/features/computer.py`
- `services/orchestrator/src/orchestrator/consensus.py`
- `services/risk/`
- `services/oms/`
- `experiments/news-impact-model/`
- `apps/dashboard/src/components/news-impact/shadow-news-impact-panel.tsx`

Validation commands already proven useful in this repo:

```powershell
npm run test:shadow-news-impact
npm run test:source-health
npm run test:strategy-readiness
pnpm --dir apps/dashboard exec tsc --noEmit --pretty false
uv run pytest services/api/tests/test_news_impact.py -q
```

Useful future Quant Foundry validation commands after the relevant files exist:

```powershell
uv run pytest services/quant_foundry/tests -q
uv run pytest services/api/tests -q -k quant_foundry
pnpm --dir apps/dashboard exec tsc --noEmit --pretty false
```

---

## 2. Master Implementation Order

The safest full order is:

1. **Repo and runtime stabilization.**
2. **Security boundary fixes.**
3. **Verification receipts and release hygiene.**
4. **Existing dashboard and module operator experience.**
5. **Quant Foundry contracts and mock connectivity.**
6. **Prediction settlement ledger.**
7. **Dossier registry and artifact verification.**
8. **Tournament scoring skeleton.**
9. **Feature lake builder.**
10. **RunPod Research Foundry MVP.**
11. **Shadow Inference Swarm MVP.**
12. **Tournament Governor promotion and retirement workflow.**
13. **Dashboard Quant Foundry operator pages.**
14. **Paper-only model pointer bridge.**
15. **Cost-aware deployment and on-demand module orchestration.**
16. **Frontier performance modules.**
17. **Limited live readiness only after long shadow and paper evidence.**

The tempting order is to start with RunPod GPUs. That is not the safe order. The system needs scoring, receipts, artifact integrity, and promotion governance before GPU scale.

---

## 3. Do Not Build These First

These are useful later, but dangerous or wasteful early:

- RunPod live inference that publishes to `sig.predict`.
- Any path from RunPod to `ord.orders`.
- Any broker credential access inside RunPod.
- Any active or limited-live promotion path.
- A huge model tournament before settlement evidence exists.
- A world model or Alpha Genome Lab before baseline models and leakage checks exist.
- A full AWS production deployment before local/staging verification receipts exist.
- A large multi-tenant architecture. This is currently a one-operator system.
- Always-on heavy services for OpenBB, news analysis, backtests, or GPU inference.

---

## 4. Phase 0: Freeze, Inventory, and Stabilize

### Goal

Create a safe starting point. Before building anything ambitious, make sure the current working tree, runtime safety, and API trust boundaries are understandable.

### Why This Comes First

The repo currently has many modified and untracked files. That may be normal during active work, but it means broad staging or broad refactoring is unsafe. The system also has known high-impact safety issues from the audit: runtime guards are not applied uniformly, and a backtest endpoint accepts broad file paths.

### TASK-0001: Categorize the Current Working Tree

**Order:** 1

**Objective:** Create a clear map of what is changed before implementation starts.

**Current state:** `git status --short` shows modified files across dashboard, API, core libs, services, docs, experiments, scripts, and untracked tool directories.

**Why it matters:** If we do implementation work on top of a broad dirty tree without classification, we risk mixing unrelated work, losing track of what changed, or accidentally committing tool artifacts.

**Files to inspect:**

- `.gitignore`
- `git status --short`
- `git diff --stat`
- Untracked top-level directories shown by git status

**Implementation steps:**

1. Run `git status --short`.
2. Group files into:
   - product code changes;
   - tests;
   - docs;
   - generated reports;
   - local tool state;
   - unknown files requiring human review.
3. Inspect local-only directories before ignoring them.
4. Add ignore rules only for confirmed local-only tool state.
5. Create or update a small release hygiene note if needed.

**Commands:**

```powershell
git status --short
git diff --stat
git diff --check
```

**Acceptance criteria:**

- The working tree can be explained in a short inventory.
- Local-only tool folders are not accidentally staged.
- No implementation commit uses broad staging until the inventory is clean.

**Risk:** Low implementation risk, high release-safety value.

**Rollback:** Ignore-rule changes can be reverted independently if a pattern is too broad.

---

### TASK-0002: Record the Current Baseline Verification

**Order:** 2

**Objective:** Capture what passes before new implementation starts.

**Current state:** Several focused checks are known to pass from the audit, but the proof is not yet a durable release receipt.

**Why it matters:** Every later phase needs to know whether a failure is new or pre-existing.

**Files to inspect:**

- `apps/dashboard/package.json`
- `scripts/preflight.ps1`
- `scripts/task-check.ps1`
- `pyproject.toml`
- `services/api/tests/test_news_impact.py`

**Implementation steps:**

1. Run the existing focused checks.
2. Record command, exit code, and any skipped heavy checks.
3. Do not run live provider, broker, GPU, or destructive checks by default.
4. Save output under a future `reports/verification/` receipt once the receipt runner exists.

**Commands:**

```powershell
cd apps/dashboard
npm run test:shadow-news-impact
npm run test:source-health
npm run test:strategy-readiness
cd ..\..
pnpm --dir apps/dashboard exec tsc --noEmit --pretty false
uv run pytest services/api/tests/test_news_impact.py -q
```

**Acceptance criteria:**

- Baseline pass/fail state is known.
- Any skipped checks have an explicit reason.
- Later implementation tasks can compare against this baseline.

**Risk:** Low.

**Rollback:** No code rollback needed.

---

### TASK-0003: Apply Runtime Safety Guards to All Service Entrypoints

**Order:** 3

**Objective:** Ensure every long-running service fails closed on unsafe runtime configuration.

**Current state:** `assert_safe_for_runtime(settings)` exists in `libs/fincept-core/src/fincept_core/config.py` and is used by the API startup path, but the audit found that other service entrypoints call `get_settings()` without consistently applying the guard before side effects.

**Why it matters:** The API should not be the only process that rejects unsafe runtime defaults. Ingestor, orchestrator, OMS, and strategy host can touch Redis, streams, schedulers, and broker-adjacent code. They need the same fail-closed behavior.

**Files likely touched:**

- `services/ingestor/src/ingestor/main.py`
- `services/orchestrator/src/orchestrator/main.py`
- `services/oms/src/oms/main.py`
- `services/strategy_host/src/strategy_host/main.py`
- `libs/fincept-core/tests/`
- Relevant service test folders

**Implementation steps:**

1. Import `assert_safe_for_runtime` in each service entrypoint.
2. Call it immediately after `settings = get_settings()`.
3. Ensure it runs before Redis connections, heartbeat startup, stream reads, OMS mode selection, schedulers, and broker-related clients.
4. Ensure failure logs do not include secret values.
5. Add tests that fail if the guard is removed.

**Tests:**

```powershell
uv run pytest libs/fincept-core/tests -q
uv run pytest services/ingestor/tests services/orchestrator/tests services/oms/tests services/strategy_host/tests -q
```

If some service test folders do not exist yet, create focused tests in the nearest existing service or shared test location.

**Acceptance criteria:**

- All service startup paths enforce the same runtime safety invariant.
- A prod-like environment with the default dev JWT secret fails before side effects.
- Error output is sanitized.
- Tests prove guard coverage.

**Dependencies:** TASK-0001 and TASK-0002.

**Risk:** Medium because startup paths are sensitive. The code change should be small.

**Rollback:** Revert only the affected service entrypoint changes if startup breaks in dev. Keep the tests as a signal for why the rollback happened.

---

### TASK-0004: Lock Down Backtest File Path Handling

**Order:** 4

**Objective:** Prevent the backtest API from reading arbitrary local files.

**Current state:** The audit found that `services/api/src/api/routes/backtest.py` accepts `bars_path` as absolute or relative and checks only existence.

**Why it matters:** User-controlled file paths are a trust boundary. An authenticated caller should not be able to probe arbitrary local paths or force the API to parse unexpected files.

**Files likely touched:**

- `services/api/src/api/routes/backtest.py`
- `services/api/tests/`
- `libs/fincept-core/src/fincept_core/config.py`
- `docs/RISKS.md`

**Implementation steps:**

1. Add allowed data roots for backtest inputs.
2. Resolve paths with `Path.resolve()`.
3. Enforce prefix checks against approved roots.
4. Reject traversal attempts.
5. Reject unsupported suffixes.
6. Return sanitized client errors.
7. Add positive and negative tests.

**Tests:**

```powershell
uv run pytest services/api/tests -q -k backtest
```

**Acceptance criteria:**

- Valid fixture files under approved roots still work.
- `../` traversal fails.
- Absolute paths outside approved roots fail.
- Unsupported suffixes fail.
- Error responses do not leak host-specific absolute paths.

**Dependencies:** TASK-0002.

**Risk:** Medium because some local workflows may rely on absolute paths. Preserve a local/dev-friendly approved-root setting.

**Rollback:** Temporarily widen the approved local root in dev only. Do not restore arbitrary file access in staging or prod-like modes.

---

### TASK-0005: Sanitize Authentication and Token Error Responses

**Order:** 5

**Objective:** Prevent auth failures from exposing decoder details or sensitive internals.

**Current state:** The system improvement report notes that API auth errors can return detailed token decoder text.

**Why it matters:** Token parsing failures should be useful to operators but not to attackers. Client responses should be generic. Logs should be sanitized and structured.

**Files likely touched:**

- `services/api/src/api/auth.py`
- `services/api/tests/`
- `apps/dashboard/src/lib/auth.ts`
- `apps/dashboard/src/lib/ws.ts`

**Implementation steps:**

1. Make client-facing token failures generic.
2. Keep structured internal logs without token values.
3. Add tests for malformed, expired, and missing tokens.
4. Document that moving dashboard auth from `localStorage` and WebSocket query tokens is a later high-priority task.

**Tests:**

```powershell
uv run pytest services/api/tests -q -k auth
pnpm --dir apps/dashboard exec tsc --noEmit --pretty false
```

**Acceptance criteria:**

- Malformed tokens return a generic 401.
- Expired tokens return a generic 401.
- No response includes raw decoder exception text.
- No token values appear in logs from tested paths.

**Dependencies:** TASK-0002.

**Risk:** Low to medium.

**Rollback:** Restore previous message detail only in local debug logs, never in client responses.

---

## 5. Phase 1: Verification, CI, and Release Safety

### Goal

Turn scattered proof into durable receipts and make future work safer to review.

### Why This Comes Before Feature Work

Without reliable receipts, every future feature discussion turns into "did we actually test it?" This phase creates the proof harness needed for safer implementation.

---

### TASK-0101: Create a Verification Receipt Runner

**Order:** 6

**Objective:** Add a safe default command that runs focused checks and writes a durable receipt.

**Current state:** `scripts/preflight.ps1` exists, but it can be heavy. The audit recommends a lighter receipt runner that records passed, failed, skipped, and not-run checks.

**Files likely touched:**

- `scripts/verification-receipt.ps1`
- `reports/verification/.gitkeep` or a documented ignored report directory
- `docs/ROADMAP.md`
- `docs/SYSTEM_IMPROVEMENT_REPORT.md` if updating after implementation

**Implementation steps:**

1. Create `scripts/verification-receipt.ps1`.
2. Run the safe dashboard checks:
   - `npm run test:shadow-news-impact`
   - `npm run test:source-health`
   - `npm run test:strategy-readiness`
   - `pnpm --dir apps/dashboard exec tsc --noEmit --pretty false`
3. Run the safe API slice:
   - `uv run pytest services/api/tests/test_news_impact.py -q`
4. Record command, working directory, duration, status, and exit code.
5. Record skipped heavy checks with reasons:
   - Docker Compose boot;
   - browser smoke;
   - live provider checks;
   - broker checks;
   - RunPod checks.
6. Write Markdown and JSON receipts if practical.
7. Exit non-zero if any required check fails.

**Tests:**

```powershell
pwsh ./scripts/verification-receipt.ps1
git diff --check -- scripts/verification-receipt.ps1
```

**Acceptance criteria:**

- The command creates a timestamped receipt.
- Required failures produce a non-zero exit.
- Skipped checks are explicit, not silent.
- Receipt content never includes secrets.

**Dependencies:** TASK-0002.

**Risk:** Low.

**Rollback:** Keep the script but mark it experimental if an environment-specific command needs adjustment.

---

### TASK-0102: Add Runtime Safety Matrix Tests

**Order:** 7

**Objective:** Prevent regressions in startup safety.

**Current state:** The runtime guard is a critical invariant, but without tests, a future service entrypoint can accidentally omit it.

**Why it matters:** This directly protects trading-adjacent processes.

**Files likely touched:**

- `libs/fincept-core/tests/test_config.py`
- `services/*/tests/test_startup_safety.py`
- `docs/RISKS.md`

**Implementation steps:**

1. Add tests for `assert_safe_for_runtime()` behavior.
2. Add tests or source-inspection checks proving each service startup path uses the guard.
3. Confirm the guard is called before side effects where practical.
4. Add a short receipt entry to the verification runner.

**Tests:**

```powershell
uv run pytest libs/fincept-core/tests -q
uv run pytest services -q -k startup_safety
```

**Acceptance criteria:**

- Removing a service guard fails tests.
- Dev/local/test modes remain usable.
- Staging/prod-like unsafe defaults fail closed.

**Dependencies:** TASK-0003 and TASK-0101.

**Risk:** Medium if tests are too brittle. Prefer behavior-oriented tests when possible.

**Rollback:** Adjust test strategy rather than removing the invariant.

---

### TASK-0103: Add Backtest Path Boundary Tests

**Order:** 8

**Objective:** Prove the backtest path fix cannot regress.

**Current state:** The audit identified broad path access. Once fixed, tests should lock it.

**Files likely touched:**

- `services/api/tests/test_backtest.py`
- `services/api/src/api/routes/backtest.py`
- Test fixture files under an approved fixture root

**Implementation steps:**

1. Add a valid fixture path test.
2. Add traversal rejection test.
3. Add absolute outside-root rejection test.
4. Add unsupported extension rejection test.
5. Add sanitized error body assertion.

**Tests:**

```powershell
uv run pytest services/api/tests -q -k backtest
```

**Acceptance criteria:**

- All path boundary cases are tested.
- Valid local fixtures remain usable.
- No absolute machine path leaks into client-facing errors.

**Dependencies:** TASK-0004.

**Risk:** Low.

**Rollback:** None expected after validation.

---

### TASK-0104: Harden CI and Supply Chain Defaults

**Order:** 9

**Objective:** Make automated checks more reproducible and safer.

**Current state:** The audit flags mutable workflow/action/image defaults and lockfile behavior as improvement areas.

**Files likely touched:**

- `.github/workflows/ci.yml`
- `.github/workflows/nightly.yml`
- `.github/workflows/build-images.yml`
- `apps/dashboard/package-lock.json` or `pnpm-lock.yaml` if present
- Dockerfiles if image tags are mutable

**Implementation steps:**

1. Inspect CI workflows.
2. Pin actions or document unavoidable mutable refs.
3. Use least-privilege workflow permissions.
4. Restore strict dashboard lockfile behavior once lockfile state is stable.
5. Keep gitleaks and dependency scanning in the flow.
6. Add receipt runner as a local pre-PR recommendation.

**Tests:**

```powershell
git diff --check -- .github/workflows
pnpm --dir apps/dashboard exec tsc --noEmit --pretty false
```

**Acceptance criteria:**

- Workflow permissions are explicit.
- Mutable refs are removed or justified.
- Dashboard install behavior is deterministic.
- Security scans remain active.

**Dependencies:** TASK-0101.

**Risk:** Medium because CI changes can fail only after pushing. Keep changes small.

**Rollback:** Revert workflow changes as a single commit if CI breaks unexpectedly.

---

### TASK-0105: Create an Environment Variable Reference

**Order:** 10

**Objective:** Document which env vars are required, secret, optional, local-only, staging, or production.

**Current state:** `.env.example` exists, and `libs/fincept-core/src/fincept_core/config.py` centralizes many settings, but the operator does not yet have a full env var reference.

**Files likely touched:**

- `docs/ENVIRONMENT.md`
- `.env.example`
- `apps/dashboard/.env.example`
- `libs/fincept-core/src/fincept_core/config.py`

**Implementation steps:**

1. Inventory settings from core config and dashboard env examples.
2. Classify each variable:
   - required;
   - optional;
   - secret;
   - public;
   - local-only;
   - staging/prod required.
3. Include safe example values.
4. Include "never commit real values" guidance.
5. Link the env reference from README or docs index.

**Tests:**

```powershell
git diff --check -- docs/ENVIRONMENT.md .env.example apps/dashboard/.env.example
```

**Acceptance criteria:**

- Operators can configure local/dev without guessing.
- Secret variables are clearly marked.
- Public dashboard variables are clearly separated from private server variables.

**Dependencies:** TASK-0003 and TASK-0101.

**Risk:** Low.

**Rollback:** Documentation-only rollback.

---

## 6. Phase 2: Existing Dashboard and Operator Workflow Readiness

### Goal

Make the current one-operator dashboard more honest, smoother, and cheaper to run before adding Quant Foundry.

### Why This Comes Now

The user workflow is already module-oriented: boot the dashboard, then start OpenBB, news analysis, and other modules only when needed. That architecture should become explicit because it reduces cost and keeps the operator in control.

---

### TASK-0201: Generate a Dashboard Route and Mock-Data Atlas

**Order:** 11

**Objective:** Know which screens are live, mock, hybrid, or demo-only.

**Current state:** The audit found that mock labels exist in some places, but there is no central route readiness map.

**Files likely touched:**

- `docs/dashboard-route-atlas.md`
- `apps/dashboard/src/app/`
- `apps/dashboard/src/components/`
- `apps/dashboard/src/lib/mock-data.ts`
- `featuresmenu.md`
- `docs/ROADMAP.md`

**Implementation steps:**

1. Scan all dashboard routes.
2. Search for `MockBadge`, `mock-data`, `placeholder`, `demo`, fixture imports, and hardcoded arrays.
3. For each route, record:
   - route;
   - primary source files;
   - data status;
   - backend dependency;
   - risk if mistaken for live;
   - replacement priority;
   - suggested test.
4. Pick the first mock-heavy route to convert to a service-backed read-only route later.

**Commands:**

```powershell
rg "MockBadge|mock-data|placeholder|demo|fixture" apps/dashboard/src
pnpm --dir apps/dashboard exec tsc --noEmit --pretty false
```

**Acceptance criteria:**

- Every dashboard route has a readiness status.
- Mock-heavy screens are visible in one doc.
- The next conversion target is obvious.

**Dependencies:** TASK-0101.

**Risk:** Low.

**Rollback:** Documentation-only rollback.

---

### TASK-0202: Build a Unified System Readiness Center

**Order:** 12

**Objective:** Add one dashboard place that shows whether Fincept is ready, degraded, disabled, or unsafe.

**Current state:** Readiness exists in scattered scripts, source-health checks, and status surfaces.

**Files likely touched:**

- `apps/dashboard/src/app/system/page.tsx`
- `apps/dashboard/src/components/`
- `apps/dashboard/src/lib/api.ts`
- `services/api/src/api/routes/`
- `scripts/verification-receipt.ps1`

**Implementation steps:**

1. Define readiness categories:
   - API;
   - Redis;
   - Timescale/Postgres;
   - dashboard tests;
   - verification receipt;
   - provider freshness;
   - news-impact shadow lane;
   - model/dossier status;
   - Quant Foundry status later.
2. Add API endpoint or extend existing status endpoint.
3. Display pass, warn, fail, skipped, disabled, and stale states.
4. Link latest verification receipt.
5. Avoid showing secrets or raw internal stack traces.

**Tests:**

```powershell
npm run test:source-health
npm run test:strategy-readiness
pnpm --dir apps/dashboard exec tsc --noEmit --pretty false
uv run pytest services/api/tests -q -k health
```

**Acceptance criteria:**

- Operator can see system health without reading terminal logs.
- Disabled modules are not shown as failures.
- Stale data is visibly distinct from healthy data.

**Dependencies:** TASK-0101 and TASK-0201.

**Risk:** Medium due to dashboard/API coordination.

**Rollback:** Keep old system page and hide the new panel behind a feature flag if needed.

---

### TASK-0203: Add On-Demand Module Control for Local and Staging

> **Owner:** Builder 5 (GLM-5.2) — ADOPTED 2026-06-22. IN PROGRESS.
> File-disjoint from TASK-0304 (Builder 2: quant_foundry outbox/inbox) and
> TASK-0401 (Builder 1: settlement ledger). No `schemas.py` / quant_foundry edits.

**Order:** 13

**Objective:** Make the current "start module only when needed" model an explicit first-class workflow.

**Current state:** The user described starting OpenBB, news analysis, and other modules from dashboard tabs. This can reduce hosting cost if implemented with idle timeouts and one-instance controls.

**Files likely touched:**

- `services/api/src/api/routes/modules.py`
- `services/api/src/api/main.py`
- `apps/dashboard/src/app/system/page.tsx`
- `apps/dashboard/src/components/`
- `scripts/start.ps1`
- New scripts such as `scripts/modules/start-openbb.ps1`
- New docs such as `docs/ON_DEMAND_MODULES.md`

**Implementation steps:**

1. Define a module registry:
   - module ID;
   - display name;
   - start command;
   - stop command;
   - health command or URL;
   - idle timeout;
   - estimated cost class;
   - allowed environments.
2. Add local-only or authenticated API controls.
3. Add dashboard controls:
   - start;
   - stop;
   - restart;
   - view logs or latest receipt;
   - idle countdown;
   - status badge.
4. Ensure commands cannot be arbitrary user input.
5. Add "Stop all optional modules" button.
6. Record module start/stop receipts.

**Security requirements:**

- No arbitrary shell command execution from user input.
- Module IDs must be allowlisted.
- API must require auth.
- Start commands must be predeclared server-side.
- Secrets must never be echoed into dashboard logs.

**Tests:**

```powershell
uv run pytest services/api/tests -q -k modules
pnpm --dir apps/dashboard exec tsc --noEmit --pretty false
```

**Acceptance criteria:**

- Operator can start and stop an allowlisted module.
- A disabled module does not cost resources.
- Duplicate starts do not spawn unbounded processes.
- Idle timeout stops optional modules safely.
- The core dashboard remains usable when modules are stopped.

**Dependencies:** TASK-0202.

**Risk:** High if command execution is not tightly allowlisted. Keep local-only first.

**Rollback:** Disable the module-control API and continue using manual scripts.

---

### TASK-0204: Add Dashboard Fetch Timeouts and Better Error States

**Order:** 14

**Objective:** Prevent slow modules, provider calls, or backend issues from freezing the operator experience.

**Current state:** The audit notes that dashboard API calls use no-store freshness and provider calls need explicit timeout/cancellation behavior.

**Files likely touched:**

- `apps/dashboard/src/lib/api.ts`
- `apps/dashboard/src/app/api/portfolio-report/route.ts`
- Dashboard pages that call API helpers
- Dashboard test scripts

**Implementation steps:**

1. Add a shared timeout helper around `fetch`.
2. Use `AbortController`.
3. Return typed errors:
   - unauthorized;
   - unavailable;
   - timeout;
   - stale;
   - validation failure.
4. Update UI states to show precise operator messages.
5. Add tests for timeout formatting.

**Tests:**

```powershell
pnpm --dir apps/dashboard exec tsc --noEmit --pretty false
npm run test:source-health
npm run test:strategy-readiness
```

**Acceptance criteria:**

- Slow calls fail clearly.
- Backend unavailable is not confused with "no data."
- No route hangs forever in normal operator use.

**Dependencies:** TASK-0201.

**Risk:** Medium due to many call sites.

**Rollback:** Keep helper behind a conservative default timeout and adjust per route.

---

### TASK-0205: Build Provider Evidence Redaction and Freshness Receipts

**Order:** 15

**Objective:** Make external data freshness reviewable without leaking secrets.

**Current state:** Provider data storage exists, and source-health work exists, but evidence redaction and freshness receipts need strengthening.

**Files likely touched:**

- `libs/fincept-db/src/fincept_db/provider_data.py`
- `services/api/tests/test_provider_data.py`
- `services/oms/src/oms/alpaca/news_sync.py`
- `services/oms/src/oms/alpaca/marks.py`
- `apps/dashboard/src/components/news/news-intelligence-panel.tsx`
- `docs/RISKS.md`

**Implementation steps:**

1. Define provider evidence receipt schema.
2. Redact token-like strings, account identifiers, raw private URLs, and sensitive payload fragments.
3. Record provider name, request hash, timestamp, row count, freshness, and status.
4. Add API read endpoint for summarized evidence.
5. Add dashboard freshness view.
6. Add tests with fake sensitive payloads.

**Tests:**

```powershell
uv run pytest services/api/tests/test_provider_data.py -q
uv run pytest services/oms/tests -q -k provider
pnpm --dir apps/dashboard exec tsc --noEmit --pretty false
```

**Acceptance criteria:**

- Provider evidence proves freshness without leaking secrets.
- Redaction tests catch token-shaped values.
- Dashboard can show data freshness and provider degradation.

**Dependencies:** TASK-0202.

**Risk:** Medium. Be conservative with redaction.

**Rollback:** Disable provider evidence display while retaining storage tests.

---

## 7. Phase 3: Quant Foundry Contracts and Mock Connectivity

### Goal

Create the safe bridge between Fincept and future RunPod workers without calling RunPod yet.

### Why This Comes Before RunPod

Contracts, idempotency, signed callbacks, durable outboxes, callback inboxes, and shadow-only schemas are what keep the system from becoming fragile. They must be proven locally first.

---

### TASK-0301: Create `services/quant_foundry` Package Skeleton

**Order:** 16

**Objective:** Add a dedicated Quant Foundry Python service package with no external GPU dependency.

**Current state:** The design docs propose `services/quant_foundry/`, but it does not yet exist as a package.

**Files likely created:**

- `services/quant_foundry/pyproject.toml`
- `services/quant_foundry/src/quant_foundry/__init__.py`
- `services/quant_foundry/src/quant_foundry/schemas.py`
- `services/quant_foundry/src/quant_foundry/ids.py`
- `services/quant_foundry/src/quant_foundry/signatures.py`
- `services/quant_foundry/tests/test_schemas.py`
- `services/quant_foundry/tests/test_signatures.py`

**Files likely modified:**

- `pyproject.toml`
- `uv.lock` after dependency sync

**Implementation steps:**

1. Add the service package to the `uv` workspace.
2. Keep dependencies minimal.
3. Define package layout.
4. Add initial tests.
5. Ensure package imports cleanly.

**Tests:**

```powershell
uv run pytest services/quant_foundry/tests -q
```

**Acceptance criteria:**

- Package exists in the workspace.
- Tests run without RunPod credentials.
- No trading streams are touched.

**Dependencies:** Phase 0 and Phase 1 should be complete.

**Risk:** Low.

**Rollback:** Remove the new package and workspace entry if necessary.

---

### TASK-0302: Define Quant Foundry Core Schemas

**Order:** 17

**Objective:** Create strict Pydantic schemas for all cross-boundary Quant Foundry payloads.

**Current state:** Core Fincept schemas exist in `libs/fincept-core/src/fincept_core/schemas.py`, but Quant Foundry needs its own external-worker contracts.

**Files likely touched:**

- `services/quant_foundry/src/quant_foundry/schemas.py`
- `services/quant_foundry/tests/test_schemas.py`

**Schemas to define:**

- `QuantFoundryJob`
- `RunPodTrainingRequest`
- `RunPodInferenceRequest`
- `RunPodCallbackEnvelope`
- `ArtifactManifest`
- `DatasetManifest`
- `ModelDossier`
- `ShadowPrediction`
- `PredictionOutcome`
- `TournamentScore`
- `PromotionReview`
- `WorkerHeartbeat`

**Implementation steps:**

1. Use `extra="forbid"` for external-facing payloads.
2. Add explicit schema version fields.
3. Add `authority` field for predictions, with `shadow-only` as the only early value.
4. Add forbidden order fields test:
   - quantity;
   - order side;
   - broker account;
   - order type;
   - time in force;
   - notional size.
5. Add JSON round-trip tests.
6. Reuse existing `Prediction` semantics only when bridging later.

**Tests:**

```powershell
uv run pytest services/quant_foundry/tests/test_schemas.py -q
```

**Acceptance criteria:**

- Extra fields are rejected.
- Shadow predictions cannot include order-like fields.
- Schema examples round-trip.
- Schema versions are explicit.

**Dependencies:** TASK-0301.

**Risk:** Medium if schema names churn later. Version them early.

**Rollback:** Add schema aliases rather than breaking existing receipts once used.

---

### TASK-0303: Add Idempotency Keys and HMAC Callback Signatures

**Order:** 18

**Objective:** Make cross-boundary communication retry-safe and tamper-resistant.

**Current state:** The connectivity spec requires at-least-once transport with exactly-once effects.

**Files likely touched:**

- `services/quant_foundry/src/quant_foundry/ids.py`
- `services/quant_foundry/src/quant_foundry/signatures.py`
- `services/quant_foundry/tests/test_signatures.py`
- `services/quant_foundry/tests/test_ids.py`

**Implementation steps:**

1. Define stable idempotency key format:
   - `qf:<job_type>:<dataset_id>:<model_family>:<config_hash>:<attempt_group>`
2. Add helper to hash request payloads.
3. Add HMAC signing:
   - `HMAC_SHA256(callback_secret, timestamp + "." + job_id + "." + payload_hash)`
4. Add timestamp skew validation.
5. Add tamper tests.
6. Add duplicate callback key tests.

**Tests:**

```powershell
uv run pytest services/quant_foundry/tests/test_signatures.py -q
uv run pytest services/quant_foundry/tests/test_ids.py -q
```

**Acceptance criteria:**

- Tampered payload fails verification.
- Old timestamp fails verification.
- Wrong job ID fails verification.
- Duplicate idempotency keys are deterministic.

**Dependencies:** TASK-0302.

**Risk:** Medium. Security-sensitive code must be simple and well-tested.

**Rollback:** Keep old mock-only mode disabled until signatures are correct.

---

### TASK-0304: Implement Durable Local Job Outbox and Callback Inbox

**Order:** 19

**Objective:** Store outbound jobs and inbound callbacks before any domain processing.

**Current state:** The connectivity spec recommends durable tables later and allows JSONL files under `reports/quant-foundry/` for the initial implementation.

**Files likely created:**

- `services/quant_foundry/src/quant_foundry/outbox.py`
- `services/quant_foundry/src/quant_foundry/inbox.py`
- `services/quant_foundry/tests/test_outbox.py`
- `services/quant_foundry/tests/test_inbox.py`
- `reports/quant-foundry/.gitkeep` if needed

**Implementation steps:**

1. Start with local JSONL or SQLite storage.
2. Store job before dispatch.
3. Store callback before processing.
4. Track status transitions.
5. Handle duplicate callbacks idempotently.
6. Reject same job ID with different payload hash as a security event.
7. Add migration path note to Postgres/Timescale tables.

**Tests:**

```powershell
uv run pytest services/quant_foundry/tests/test_outbox.py -q
uv run pytest services/quant_foundry/tests/test_inbox.py -q
```

**Acceptance criteria:**

- Jobs survive process restart in local storage.
- Duplicate callbacks do not duplicate domain effects.
- Different payload hash for same job is rejected.
- Receipts include job status history.

**Dependencies:** TASK-0303.

**Risk:** Medium. Local JSONL is fine for MVP but must not be mistaken for production durability.

**Rollback:** Leave the outbox disabled by config while preserving tests.

---

### TASK-0305: Add Mock Dispatcher and Mock Callback Processor

**Order:** 20

**Objective:** Prove the entire Fincept-to-worker-to-Fincept loop without RunPod.

**Files likely touched:**

- `services/quant_foundry/src/quant_foundry/mock_dispatcher.py`
- `services/quant_foundry/src/quant_foundry/callbacks.py`
- `services/quant_foundry/tests/test_mock_flow.py`

**Implementation steps:**

1. Add deterministic mock training job flow.
2. Add deterministic mock shadow inference job flow.
3. Use the same schemas, signatures, idempotency keys, outbox, and inbox as the future RunPod path.
4. Emit a local receipt.
5. Add failure cases:
   - bad signature;
   - invalid schema;
   - duplicate callback;
   - terminal job failure.

**Tests:**

```powershell
uv run pytest services/quant_foundry/tests/test_mock_flow.py -q
```

**Acceptance criteria:**

- A mock training job completes through the real contract.
- A mock shadow prediction batch stores in a shadow-only ledger stub.
- Bad callbacks fail closed.
- No existing Fincept trading stream is touched.

**Dependencies:** TASK-0304.

**Risk:** Low to medium.

**Rollback:** Disable mock dispatcher registration.

---

### TASK-0306: Add Quant Foundry API Route in Local Mock Mode

**Order:** 21

**Objective:** Expose gateway endpoints through the API without RunPod dependency.

**Files likely touched:**

- `services/api/src/api/routes/quant_foundry.py`
- `services/api/src/api/main.py`
- `services/api/tests/test_quant_foundry.py`
- `services/quant_foundry/src/quant_foundry/gateway.py`

**Endpoints:**

- `POST /quant-foundry/jobs`
- `GET /quant-foundry/jobs`
- `GET /quant-foundry/jobs/{job_id}`
- `POST /quant-foundry/callbacks/runpod`
- `GET /quant-foundry/health`
- `GET /quant-foundry/heartbeats`

**Implementation steps:**

1. Add a FastAPI router.
2. Require auth for operator endpoints.
3. Keep callback auth separate through HMAC headers.
4. Add local mock mode config:
   - `QUANT_FOUNDRY_ENABLED=false` by default;
   - `QUANT_FOUNDRY_MODE=local_mock`;
   - `QUANT_FOUNDRY_SHADOW_ONLY=true`.
5. Return safe health state when disabled.
6. Add tests for disabled, enabled mock, bad signature, duplicate callback.

**Tests:**

```powershell
uv run pytest services/api/tests/test_quant_foundry.py -q
uv run pytest services/quant_foundry/tests -q
```

**Acceptance criteria:**

- API starts with Quant Foundry disabled.
- Local mock mode can create and complete a job.
- Callback endpoint rejects bad signatures.
- No order stream or `sig.predict` writes exist in this route.

**Dependencies:** TASK-0305.

**Risk:** Medium because API registration can affect startup.

**Rollback:** Leave router unregistered behind config if needed.

---

## 8. Phase 4: Evidence Loop Foundations

### Goal

Create the scoreboard before adding more models.

### Why This Comes Before RunPod

Training thousands of models is only useful if each model can be judged later against realized outcomes, calibration, slippage, drawdown, and baseline performance.

---

### TASK-0401: Build the Prediction Settlement Ledger

**Order:** 22

**Objective:** Judge every prediction after the relevant horizon expires.

**Current state:** `libs/fincept-core/src/fincept_core/prediction_log.py` already has a `PredictionRow` with an `id` intended for future settlement joins.

**Files likely created:**

- `services/quant_foundry/src/quant_foundry/settlement.py`
- `services/quant_foundry/src/quant_foundry/outcomes.py`
- `services/quant_foundry/src/quant_foundry/metrics.py`
- `services/quant_foundry/tests/test_settlement.py`

**Files likely inspected:**

- `libs/fincept-core/src/fincept_core/prediction_log.py`
- `libs/fincept-core/src/fincept_core/schemas.py`
- `libs/fincept-db/src/fincept_db/bars.py`
- `services/backtester/src/backtester/`

**Implementation steps:**

1. Define `PredictionOutcome`.
2. Settle simple direction/confidence predictions first.
3. Add realized return by horizon.
4. Add abnormal return versus benchmark where data exists.
5. Add Brier score.
6. Add calibration bucket.
7. Add cost/slippage assumptions.
8. Add `pending_time` and `pending_data` states.
9. Make reruns idempotent.

**Tests:**

```powershell
uv run pytest services/quant_foundry/tests/test_settlement.py -q
```

**Acceptance criteria:**

- Fixture predictions settle deterministically.
- Missing market data does not crash settlement.
- Reruns do not duplicate outcomes.
- Output can feed tournament scoring.

**Dependencies:** TASK-0302.

**Risk:** Medium. Settlement math must be simple first.

**Rollback:** Keep settlement worker read-only and disabled until verified.

---

### TASK-0402: Add Shadow Prediction Ledger Storage  <!-- OWNER: Builder 1 (GLM) — ADOPTED 2026-06-22 -->

**Order:** 23

**Owner:** Builder 1 (GLM) — IN PROGRESS 2026-06-22. See `docs/AAA_GLM_SUPERTEAM_LOGS/BUILDER1_GLM.md`.
File-disjoint from TASK-0401 (Builder 1: settlement), TASK-0304 (Builder 2: outbox/inbox),
TASK-0405 (Builder 4: feature lake), TASK-0203 (Builder 5: module control), TASK-0403 (Builder 3).

**Objective:** Store Quant Foundry shadow predictions separately from existing trading prediction streams.

**Current state:** Existing `sig.predict` can feed orchestrator. Quant Foundry shadow output must not go there until paper bridge approval exists.

**Files likely touched:**

- `services/quant_foundry/src/quant_foundry/shadow_ledger.py`
- `services/quant_foundry/src/quant_foundry/schemas.py`
- `services/quant_foundry/tests/test_shadow_ledger.py`
- `libs/fincept-bus/src/fincept_bus/streams.py` later, if adding `qf.shadow.predictions`

**Implementation steps:**

1. Store shadow predictions in local storage first.
2. Include:
   - prediction ID;
   - model ID;
   - symbol;
   - timestamp;
   - horizon;
   - expected return;
   - p_up;
   - confidence;
   - feature availability;
   - latency;
   - regime metadata;
   - authority `shadow-only`.
3. Reject any payload with order-like fields.
4. Add idempotency by prediction ID and batch hash.
5. Add read APIs later.

**Tests:**

```powershell
uv run pytest services/quant_foundry/tests/test_shadow_ledger.py -q
```

**Acceptance criteria:**

- Shadow predictions store safely.
- Duplicate batches are idempotent.
- Order-like fields are rejected.
- No write path to `sig.predict` exists.

**Dependencies:** TASK-0401.

**Risk:** Medium because it defines a long-lived data contract.

**Rollback:** Keep data local and clearable during MVP.

---

### TASK-0403: Build the Dossier Registry  <!-- OWNER: Builder 3 (GLM) — COMPLETED 2026-06-22 (commit de56c38) -->

**Order:** 24

**Owner:** Builder 3 (GLM) — COMPLETED 2026-06-22 (commit `de56c38`). See `docs/AAA_GLM_SUPERTEAM_LOGS/BUILDER3.md`.
File-disjoint from TASK-0401 (Builder 1: settlement), TASK-0402 (Builder 1: shadow ledger),
TASK-0304/0305 (Builder 2: outbox/inbox/mock dispatcher), TASK-0405 (Builder 4: feature lake),
TASK-0203 (Builder 5: module control).

**Objective:** Make every model artifact understandable, reproducible, and promotable only with evidence.

**Files likely touched:**

- `services/quant_foundry/src/quant_foundry/dossier.py`
- `services/quant_foundry/src/quant_foundry/artifacts.py`
- `services/quant_foundry/src/quant_foundry/registry.py`
- `services/quant_foundry/tests/test_dossier.py`
- `services/api/src/api/routes/quant_foundry.py`

**Implementation steps:**

1. Define `ModelDossier`.
2. Define `ArtifactManifest`.
3. Add artifact hash verification.
4. Generate dossier for an existing local GBM model if available.
5. Import a mock artifact from the mock dispatcher.
6. Store dossiers immutable by version/hash.
7. Add read-only API list/detail endpoints.

**Tests:**

```powershell
uv run pytest services/quant_foundry/tests -q -k dossier
uv run pytest services/api/tests -q -k quant_foundry
```

**Acceptance criteria:**

- Existing local model can get a dossier.
- Mock artifact imports with hash verification.
- Bad hash is rejected.
- Dossier status is visible through API.

**Dependencies:** TASK-0401 and TASK-0402.

**Risk:** Medium.

**Rollback:** Reject all external artifacts and keep dossier generation local.

---

### TASK-0404: Build Tournament Scoring Skeleton

**Order:** 25

**Objective:** Rank models based on settled evidence and baseline comparisons.

**Files likely touched:**

- `services/quant_foundry/src/quant_foundry/tournament.py`
- `services/quant_foundry/src/quant_foundry/leaderboard.py`
- `services/quant_foundry/tests/test_tournament.py`

**Initial score components:**

- out-of-sample net edge;
- calibration;
- Brier score;
- realized return by confidence bucket;
- drawdown penalty;
- turnover penalty if available;
- feature availability penalty;
- latency penalty;
- decay penalty.

**Implementation steps:**

1. Define scoring input schema.
2. Implement deterministic baseline comparison.
3. Add simple weighted score.
4. Add blocking issues list.
5. Add stale evidence handling.
6. Add deterministic fixture tests.

**Tests:**

```powershell
uv run pytest services/quant_foundry/tests/test_tournament.py -q
```

**Acceptance criteria:**

- Two fixture models rank deterministically.
- A model with high ML score but poor cost-adjusted return can lose.
- Stale evidence blocks promotion recommendation.
- Tournament output can feed a promotion packet later.

**Dependencies:** TASK-0401 and TASK-0403.

**Risk:** Medium. Keep scoring explainable.

**Rollback:** Keep tournament as advisory with no promotion authority.

---

### TASK-0405: Build Feature Lake Builder MVP  <!-- OWNER: Builder 4 (GLM) — ADOPTED 2026-06-22 -->

**Order:** 26

**Owner:** Builder 4 (GLM) — IN PROGRESS 2026-06-22. See `docs/AAA_GLM_SUPERTEAM_LOGS/`.
File-disjoint from TASK-0401 (Builder 1), TASK-0304 (Builder 2), TASK-0402 (Builder 3).

**Objective:** Export point-in-time datasets and manifests for training and shadow inference.

**Files likely touched:**

- `services/quant_foundry/src/quant_foundry/feature_lake.py`
- `services/quant_foundry/src/quant_foundry/dataset_manifest.py`
- `services/quant_foundry/src/quant_foundry/feature_availability.py`
- `services/quant_foundry/tests/test_feature_lake.py`
- `services/features/src/features/computer.py`

**Implementation steps:**

1. Start with fixture-backed dataset export.
2. Generate dataset manifest.
3. Include feature schema hash.
4. Include label schema hash.
5. Include train/validation/test windows.
6. Include point-in-time proof fields.
7. Include row count and checksum.
8. Add feature availability report.
9. Write export receipt.

**Tests:**

```powershell
uv run pytest services/quant_foundry/tests/test_feature_lake.py -q
uv run pytest services/features/tests -q
```

**Acceptance criteria:**

- Fixture dataset exports with stable manifest.
- Manifest hash changes when source data changes.
- Feature availability report exists.
- Training jobs can reference manifest instead of DB credentials.

**Dependencies:** TASK-0401.

**Risk:** Medium to high if attempting real provider data too soon. Start with fixtures.

**Rollback:** Keep feature lake in fixture-only mode.

---

## 9. Phase 5: RunPod Research Foundry MVP

### Goal

Use RunPod for GPU training and evaluation while Fincept only receives artifacts, manifests, receipts, and dossiers.

### Why This Comes After Evidence Foundations

The Research Foundry should generate candidates for a scoreboard that already exists. Without settlement and dossiers, training scale creates ungoverned model sprawl.

---

### TASK-0501: Build RunPod Training Container MVP

**Order:** 27

**Objective:** Create a minimal RunPod-compatible training worker.

**Files likely created:**

- `runpod/quant-foundry-training/handler.py`
- `runpod/quant-foundry-training/Dockerfile`
- `runpod/quant-foundry-training/README.md`
- `services/quant_foundry/src/quant_foundry/runpod_training.py`
- `services/quant_foundry/tests/test_runpod_training.py`

**Implementation steps:**

1. Start with a local container-compatible handler.
2. Accept `RunPodTrainingRequest`.
3. Read dataset manifest.
4. Train a tiny baseline or fake model first.
5. Write artifact manifest.
6. Write training receipt.
7. Send signed callback.
8. Enforce time and budget limits.

**Tests:**

```powershell
uv run pytest services/quant_foundry/tests -q -k runpod_training
```

Manual container proof after Docker setup:

```powershell
docker build -t fincept-qf-training:local runpod/quant-foundry-training
```

**Acceptance criteria:**

- Local mock trainer and container handler use the same contract.
- No broker credentials are available.
- Artifact manifest is hash-verifiable.
- Training failure returns a safe terminal or retryable status.

**Dependencies:** TASK-0403 and TASK-0405.

**Risk:** Medium. Container dependency drift is likely.

**Rollback:** Keep RunPod training disabled and use mock trainer.

---

### TASK-0502: Implement RunPod Job Dispatch Client

**Order:** 28

**Objective:** Dispatch training jobs from Fincept outbox to RunPod without coupling core services to RunPod.

**Files likely touched:**

- `services/quant_foundry/src/quant_foundry/runpod_client.py`
- `services/quant_foundry/src/quant_foundry/gateway.py`
- `services/quant_foundry/tests/test_runpod_client.py`
- `docs/ENVIRONMENT.md`

**Implementation steps:**

1. Add `RunPodClient` interface.
2. Add mock implementation.
3. Add real HTTP implementation behind config.
4. Read RunPod API key only server-side.
5. Enforce dispatch rate limits.
6. Enforce per-job budget metadata.
7. Store RunPod job ID in outbox.
8. Classify transient and terminal errors.

**Tests:**

```powershell
uv run pytest services/quant_foundry/tests/test_runpod_client.py -q
```

**Acceptance criteria:**

- No RunPod call happens unless explicitly enabled.
- Failed RunPod calls leave retryable jobs.
- Rate and budget limits are enforced.
- API key is never returned to dashboard or logs.

**Dependencies:** TASK-0501.

**Risk:** Medium to high because external API behavior can drift.

**Rollback:** Switch `QUANT_FOUNDRY_MODE=local_mock`.

---

### TASK-0503: Add Artifact Import From Object Storage

**Order:** 29

**Objective:** Import model artifacts by pulling from controlled storage and verifying hash/size/content type.

**Files likely touched:**

- `services/quant_foundry/src/quant_foundry/artifacts.py`
- `services/quant_foundry/src/quant_foundry/registry.py`
- `services/quant_foundry/tests/test_artifacts.py`
- `docs/ENVIRONMENT.md`

**Implementation steps:**

1. Define allowed artifact URI schemes.
2. Add size limits.
3. Add content type validation.
4. Download to a quarantine/staging path.
5. Verify hash before registration.
6. Store immutable metadata.
7. Reject mismatches and emit security receipt.

**Tests:**

```powershell
uv run pytest services/quant_foundry/tests/test_artifacts.py -q
```

**Acceptance criteria:**

- Bad hash rejects import.
- Oversized artifact rejects import.
- Unsupported URI rejects import.
- Valid artifact gets a dossier candidate record.

**Dependencies:** TASK-0403 and TASK-0502.

**Risk:** High if URI handling is too broad. Use allowlists.

**Rollback:** Disable external artifact import and keep local mock artifacts only.

---

### TASK-0504: Train First Real Baseline Model Family

**Order:** 30

**Objective:** Produce the first RunPod-trained candidate, starting simple.

**Recommended first family:** LightGBM or CatBoost baseline, not a transformer.

**Why simple first:** A simple model makes leakage, manifests, artifacts, and tournament behavior easier to verify. If a simple baseline cannot pass the evidence loop, a frontier model will not save the system.

**Files likely touched:**

- `runpod/quant-foundry-training/handler.py`
- `services/quant_foundry/src/quant_foundry/runpod_training.py`
- `services/quant_foundry/tests/test_runpod_training.py`
- `docs/quant-foundry/` later

**Implementation steps:**

1. Select one small dataset manifest.
2. Train one baseline model family.
3. Run walk-forward validation.
4. Produce calibration report.
5. Produce feature importance report.
6. Produce economic metrics.
7. Package artifact.
8. Import dossier.
9. Keep model at `candidate` or `research-approved`.

**Tests:**

```powershell
uv run pytest services/quant_foundry/tests -q -k "feature_lake or dossier or tournament or runpod_training"
```

**Acceptance criteria:**

- One real trained artifact imports.
- Dossier includes dataset and feature schema.
- Model cannot influence predictions or orders yet.
- Costs and duration are recorded.

**Dependencies:** TASK-0503.

**Risk:** Medium to high. Training environment issues are common.

**Rollback:** Discard the candidate dossier and keep the artifact unpromoted.

---

## 10. Phase 6: Shadow Inference Swarm MVP

### Goal

Run live non-trading predictions through RunPod and measure them in Fincept.

### Why This Comes After Research MVP

Only imported and dossiered candidate models should be eligible for shadow inference. Shadow inference must be a measurement lane, not a trading lane.

---

### TASK-0601: Build RunPod Inference Container MVP

**Order:** 31

**Objective:** Run candidate model predictions on RunPod and return shadow-only prediction batches.

**Files likely created:**

- `runpod/quant-foundry-inference/handler.py`
- `runpod/quant-foundry-inference/Dockerfile`
- `runpod/quant-foundry-inference/README.md`
- `services/quant_foundry/src/quant_foundry/shadow_inference.py`
- `services/quant_foundry/tests/test_shadow_inference.py`

**Implementation steps:**

1. Accept `RunPodInferenceRequest`.
2. Load a candidate artifact from read-only cache or controlled URI.
3. Score fixture feature snapshots first.
4. Return `ShadowPrediction` batch.
5. Include latency and feature availability.
6. Include `authority: shadow-only`.
7. Send signed callback.

**Tests:**

```powershell
uv run pytest services/quant_foundry/tests -q -k shadow_inference
```

**Acceptance criteria:**

- Container returns valid shadow predictions.
- Invalid feature snapshot fails safely.
- No output contains order fields.
- Inference can be disabled without breaking Fincept.

**Dependencies:** TASK-0504.

**Risk:** Medium.

**Rollback:** Use local mock inference.

---

### TASK-0602: Add Live Feature Snapshot Export

**Order:** 32

**Objective:** Provide compact, point-in-time feature snapshots for shadow inference.

**Files likely touched:**

- `services/quant_foundry/src/quant_foundry/feature_lake.py`
- `services/quant_foundry/src/quant_foundry/feature_availability.py`
- `services/features/src/features/computer.py`
- `services/quant_foundry/tests/test_feature_snapshots.py`

**Implementation steps:**

1. Define snapshot schema.
2. Include symbol, timestamp, horizon, feature hash, and availability score.
3. Include freshness metadata.
4. Avoid raw provider secrets or raw credential-bearing payloads.
5. Add tests for missing features.
6. Add abstain behavior when availability is too low.

**Tests:**

```powershell
uv run pytest services/quant_foundry/tests/test_feature_snapshots.py -q
uv run pytest services/features/tests -q
```

**Acceptance criteria:**

- Feature snapshots are compact.
- Feature availability is measurable.
- Missing required features produce abstain or degraded state.

**Dependencies:** TASK-0405 and TASK-0601.

**Risk:** Medium to high because live feature timing is subtle.

**Rollback:** Restrict snapshots to fixture or delayed data until stable.

---

### TASK-0603: Store and Settle Shadow Predictions

**Order:** 33

**Objective:** Connect RunPod shadow predictions to the settlement ledger.

**Files likely touched:**

- `services/quant_foundry/src/quant_foundry/shadow_ledger.py`
- `services/quant_foundry/src/quant_foundry/settlement.py`
- `services/quant_foundry/tests/test_shadow_settlement.py`

**Implementation steps:**

1. Store signed prediction batches.
2. Reject bad signatures and schemas.
3. Mark predictions pending by horizon.
4. Settle after horizon expires.
5. Update model live calibration metrics.
6. Emit receipt for settled batches.

**Tests:**

```powershell
uv run pytest services/quant_foundry/tests/test_shadow_settlement.py -q
npm run test:shadow-news-impact
```

**Acceptance criteria:**

- Shadow predictions settle into outcomes.
- Settlement lag is visible.
- No prediction reaches `sig.predict`.
- Invalid callback is stored as rejected, not silently discarded.

**Dependencies:** TASK-0602.

**Risk:** Medium.

**Rollback:** Stop shadow ingestion and preserve stored rejected callback receipts.

---

### TASK-0604: Add Shadow Inference Health Dashboard

**Order:** 34

**Objective:** Let the operator see shadow model health, latency, drift, and settlement progress.

**Files likely touched:**

- `apps/dashboard/src/app/quant-foundry/shadow/page.tsx`
- `apps/dashboard/src/app/quant-foundry/page.tsx`
- `apps/dashboard/src/lib/api.ts`
- `services/api/src/api/routes/quant_foundry.py`

**Implementation steps:**

1. Add API methods for shadow status.
2. Add dashboard page.
3. Show:
   - enabled/disabled;
   - models running;
   - latest prediction;
   - latency p50/p95;
   - feature availability;
   - callback rejection rate;
   - settlement lag;
   - circuit breaker state.
4. Add loading, empty, degraded, disabled, and error states.

**Tests:**

```powershell
pnpm --dir apps/dashboard exec tsc --noEmit --pretty false
npm run test:source-health
```

**Acceptance criteria:**

- Operator can distinguish disabled from broken.
- Stale shadow predictions are visible.
- Rejected callbacks are visible without exposing secrets.

**Dependencies:** TASK-0603.

**Risk:** Medium.

**Rollback:** Hide dashboard page behind a feature flag.

---

## 11. Phase 7: Tournament Governor and Promotion Workflow

### Goal

Turn prediction evidence into ranked recommendations, not automatic trades.

---

### TASK-0701: Expand Tournament Leaderboards

**Order:** 35

**Objective:** Score models by horizon, regime, symbol cluster, event type, and live shadow evidence.

**Files likely touched:**

- `services/quant_foundry/src/quant_foundry/tournament.py`
- `services/quant_foundry/src/quant_foundry/leaderboard.py`
- `services/quant_foundry/tests/test_tournament.py`
- `services/api/src/api/routes/quant_foundry.py`

**Implementation steps:**

1. Add horizon slices.
2. Add regime slices.
3. Add symbol-cluster slices.
4. Add event/news-type slices where data exists.
5. Add baseline deltas.
6. Add confidence calibration summaries.
7. Add decay indicators.

**Tests:**

```powershell
uv run pytest services/quant_foundry/tests/test_tournament.py -q
uv run pytest services/api/tests -q -k quant_foundry
```

**Acceptance criteria:**

- A model can rank high in one horizon and low in another.
- Stale or decayed models are flagged.
- Leaderboard explains why a model ranks where it does.

**Dependencies:** TASK-0603.

**Risk:** Medium.

**Rollback:** Keep global leaderboard only until slices are stable.

---

### TASK-0702: Build Promotion Review Queue

**Order:** 36

**Objective:** Require human approval and evidence packets for model promotion.

**Files likely touched:**

- `services/quant_foundry/src/quant_foundry/promotion.py`
- `services/quant_foundry/tests/test_promotion.py`
- `services/api/src/api/routes/quant_foundry.py`
- `apps/dashboard/src/app/quant-foundry/promotion/page.tsx`

**Implementation steps:**

1. Define promotion levels:
   - `candidate`;
   - `research-approved`;
   - `shadow-approved`;
   - `paper-approved`;
   - `limited-live-approved`;
   - `active`.
2. For MVP, allow only up to `shadow-approved`.
3. Require:
   - dossier;
   - artifact hash;
   - settlement evidence;
   - tournament score;
   - blocking issues list;
   - human review note.
4. Add rejection reasons.
5. Add immutable promotion receipt.

**Tests:**

```powershell
uv run pytest services/quant_foundry/tests/test_promotion.py -q
pnpm --dir apps/dashboard exec tsc --noEmit --pretty false
```

**Acceptance criteria:**

- No model can be promoted without a dossier.
- No model can be promoted without settlement evidence.
- Human approval is stored.
- Rejection is stored with reason.

**Dependencies:** TASK-0701.

**Risk:** Medium to high because this defines governance.

**Rollback:** Freeze all promotions at `candidate`.

---

### TASK-0703: Add Retirement and Edge-Decay Flags

**Order:** 37

**Objective:** Automatically flag models that stop working.

**Files likely touched:**

- `services/quant_foundry/src/quant_foundry/tournament.py`
- `services/quant_foundry/src/quant_foundry/promotion.py`
- `services/quant_foundry/tests/test_retirement.py`
- `apps/dashboard/src/app/quant-foundry/tournament/page.tsx`

**Implementation steps:**

1. Define decay thresholds.
2. Detect calibration degradation.
3. Detect net edge below baseline.
4. Detect feature availability degradation.
5. Detect latency budget violations.
6. Detect drawdown contribution warnings.
7. Emit retirement recommendations.

**Tests:**

```powershell
uv run pytest services/quant_foundry/tests/test_retirement.py -q
```

**Acceptance criteria:**

- A decayed fixture model is flagged.
- Flag includes reason.
- Retirement recommendation cannot delete artifacts.
- Dashboard shows retire/retrain suggestion.

**Dependencies:** TASK-0701.

**Risk:** Medium.

**Rollback:** Mark retirement as advisory only.

---

### TASK-0704: Build Paper-Only Model Pointer Bridge

**Order:** 38

**Objective:** Allow approved models to influence paper workflows without live trading authority.

**Current state:** Existing orchestrator can consume `sig.predict`, so this bridge is the first dangerous connection point. It must come late.

**Files likely touched:**

- `services/quant_foundry/src/quant_foundry/paper_bridge.py`
- `services/quant_foundry/src/quant_foundry/promotion.py`
- `libs/fincept-core/src/fincept_core/schemas.py`
- `libs/fincept-bus/src/fincept_bus/streams.py`
- `services/orchestrator/`
- `services/risk/`
- `services/oms/`
- `services/quant_foundry/tests/test_paper_bridge.py`

**Implementation steps:**

1. Require `paper-approved` model status.
2. Require explicit config:
   - `QUANT_FOUNDRY_ALLOW_PAPER_BRIDGE=true`.
3. Convert shadow prediction to existing `Prediction` schema.
4. Publish only in paper mode.
5. Store rollback pointer before enabling.
6. Add circuit breaker for bad predictions or missing evidence.
7. Keep OMS and risk authoritative.

**Tests:**

```powershell
uv run pytest services/quant_foundry/tests/test_paper_bridge.py -q
uv run pytest services/orchestrator/tests services/risk/tests services/oms/tests -q
```

**Acceptance criteria:**

- Bridge is disabled by default.
- Bridge refuses non-paper runtime.
- Bridge refuses models without evidence packet.
- Rollback pointer exists.
- Risk/OMS boundaries remain unchanged.

**Dependencies:** TASK-0702 and TASK-0703.

**Risk:** High. This is the first path that can influence trading-adjacent decisioning.

**Rollback:** Disable `QUANT_FOUNDRY_ALLOW_PAPER_BRIDGE`.

---

## 12. Phase 8: Quant Foundry Dashboard and Operator Control

### Goal

Expose Quant Foundry clearly to the one-person operator without creating accidental authority.

---

### TASK-0801: Add Quant Foundry Overview Page

**Order:** 39

**Files likely touched:**

- `apps/dashboard/src/app/quant-foundry/page.tsx`
- `apps/dashboard/src/components/shell/nav-tabs.tsx`
- `apps/dashboard/src/components/shell/sidebar.tsx`
- `apps/dashboard/src/lib/api.ts`

**Implementation steps:**

1. Add route.
2. Show module status cards:
   - Gateway;
   - Outbox;
   - Callback inbox;
   - Feature Lake;
   - Settlement;
   - Dossier Registry;
   - Tournament;
   - RunPod Research;
   - Shadow Inference.
3. Show global mode:
   - disabled;
   - local mock;
   - RunPod research;
   - RunPod shadow;
   - paper bridge.
4. Show cost/budget state.
5. Show latest receipts.

**Tests:**

```powershell
pnpm --dir apps/dashboard exec tsc --noEmit --pretty false
```

**Acceptance criteria:**

- Page loads in disabled mode.
- Disabled is not shown as failure.
- No action can promote or trade from overview.

**Dependencies:** TASK-0306 and TASK-0202.

**Risk:** Medium.

**Rollback:** Hide nav entry.

---

### TASK-0802: Add Jobs, Dossiers, Tournament, and Promotion Pages

**Order:** 40

**Files likely created:**

- `apps/dashboard/src/app/quant-foundry/jobs/page.tsx`
- `apps/dashboard/src/app/quant-foundry/models/page.tsx`
- `apps/dashboard/src/app/quant-foundry/tournament/page.tsx`
- `apps/dashboard/src/app/quant-foundry/promotion/page.tsx`

**Implementation steps:**

1. Jobs page:
   - queued;
   - running;
   - retrying;
   - failed;
   - completed.
2. Models page:
   - dossier list;
   - artifact hash;
   - status;
   - evidence completeness.
3. Tournament page:
   - leaderboard;
   - baseline deltas;
   - blocking issues;
   - decay flags.
4. Promotion page:
   - review packet;
   - approve/reject;
   - confirmation;
   - rollback visibility.
5. Add empty/error/loading/stale states everywhere.

**Tests:**

```powershell
pnpm --dir apps/dashboard exec tsc --noEmit --pretty false
npm run test:source-health
npm run test:strategy-readiness
```

**Acceptance criteria:**

- Operator can see all Quant Foundry states.
- Promotion requires confirmation.
- Missing evidence blocks promotion visually and server-side.

**Dependencies:** TASK-0801 and Phase 7.

**Risk:** Medium to high due to many UI states.

**Rollback:** Keep read-only pages and disable actions.

---

## 13. Phase 9: Deployment and Cost-Optimized Runtime

### Goal

Run the system cheaply for one operator while keeping a clear path to serious production infrastructure.

### Deployment Principle

Use an always-on thin shell and on-demand workers:

- Always-on:
  - dashboard;
  - API/control plane;
  - small Postgres/Timescale;
  - small Redis;
  - module registry and status.
- On-demand:
  - OpenBB;
  - news analysis;
  - provider sync;
  - backtests;
  - Quant Foundry gateway mock jobs;
  - RunPod training;
  - RunPod shadow inference;
  - tournament replay;
  - feature lake builds.

---

### TASK-0901: Create Local/Staging Module Runtime Plan

**Order:** 41

**Objective:** Document and implement how each optional module starts, stops, idles, and reports cost.

**Files likely touched:**

- `docs/ON_DEMAND_MODULES.md`
- `scripts/start.ps1`
- `scripts/modules/`
- `services/api/src/api/routes/modules.py`
- `apps/dashboard/src/app/system/page.tsx`

**Implementation steps:**

1. Define module list.
2. Define start/stop scripts.
3. Define health checks.
4. Define idle timeout.
5. Define max instances.
6. Define estimated monthly cost if always-on versus 2 hours/day.
7. Add budget guard before heavy jobs.
8. Add "stop all optional modules."

**Acceptance criteria:**

- Optional modules have explicit lifecycle.
- Idle modules can stop.
- Operator can see which modules are costing money.
- Heavy jobs require confirmation.

**Dependencies:** TASK-0203.

**Risk:** Medium.

**Rollback:** Return to manual module startup scripts.

---

### TASK-0902: Use Railway for Test/Staging Only

**Order:** 42

**Objective:** Use Railway where it is cost-effective without mistaking it for the final production architecture.

**Recommended Railway use:**

- dashboard staging;
- API staging;
- small Postgres test DB;
- Redis test instance;
- mock Quant Foundry gateway;
- on-demand worker-lite jobs;
- route smoke and operator demos.

**Do not use Railway first for:**

- GPU model training;
- serious model artifact storage;
- broker-adjacent production OMS;
- always-on heavy backtests;
- high-frequency inference;
- long-running data ingestion.

**Implementation steps:**

1. Create Railway staging service definitions.
2. Keep optional modules disabled by default.
3. Add idle/sleep behavior where supported.
4. Add budget alerts.
5. Add health endpoints.
6. Add staging verification receipt.

**Acceptance criteria:**

- Railway staging boots core dashboard/API.
- Optional modules stay off unless started.
- Cost risk is visible.
- No production broker credentials live in Railway staging.

**Dependencies:** TASK-0101 and TASK-0901.

**Risk:** Medium because sleeping services can cold-start and return transient failures.

**Rollback:** Stop Railway services and run locally.

---

### TASK-0903: Prepare AWS Production Control Plane Later

**Order:** 43

**Objective:** Design the serious deployment path without moving too early.

**Recommended later AWS shape:**

- ECS Fargate or App Runner for API/control services;
- S3 for receipts, dossiers, artifacts;
- ECR for images;
- Secrets Manager;
- CloudWatch;
- VPC/private subnets;
- ElastiCache Redis/Valkey;
- managed Postgres or Timescale-compatible service;
- ALB plus WAF.

**Implementation steps:**

1. Write `docs/DEPLOYMENT_PRODUCTION_PLAN.md`.
2. Separate control plane from GPU plane.
3. Keep RunPod for GPUs.
4. Keep OMS/risk boundaries inside the trusted Fincept deployment.
5. Define rollback and receipt gates.
6. Define backup/restore.

**Acceptance criteria:**

- There is a concrete AWS plan.
- It does not require moving before local/staging are stable.
- It separates RunPod from core trading authority.

**Dependencies:** Phase 1 and Phase 3.

**Risk:** Low if documentation-only; high if implemented too early.

**Rollback:** Keep local/Railway staging.

---

## 14. Phase 10: Frontier Performance Modules

### Goal

Add cutting-edge accuracy improvements only after the core evidence loop works.

### Why This Comes Late

Frontier modules are powerful but overfit-prone. The system needs settlement, dossiers, tournament scoring, retirement, and shadow proof before these modules can be trusted.

---

### TASK-1001: Mixture-of-Experts Model Router

**Order:** 44

**Objective:** Learn which model to trust by regime, symbol, liquidity, volatility, news type, horizon, feature availability, and recent calibration.

**Files likely touched:**

- `services/quant_foundry/src/quant_foundry/router.py`
- `services/quant_foundry/tests/test_router.py`
- `apps/dashboard/src/app/quant-foundry/tournament/page.tsx`

**Implementation steps:**

1. Define router input features.
2. Start with rules from tournament evidence.
3. Add learned router only after enough settled data exists.
4. Add abstain output.
5. Add explainability:
   - selected expert;
   - reason;
   - confidence;
   - fallback.

**Acceptance criteria:**

- Router can choose different experts by horizon/regime.
- Router can abstain.
- Router remains shadow-only until proven.

**Dependencies:** Phase 7.

**Risk:** High overfitting risk.

**Rollback:** Disable router and use single model or simple ensemble.

---

### TASK-1002: Causal Market Memory Graph

**Order:** 45

**Objective:** Represent historical relationships between symbols, sectors, events, regimes, and outcomes.

**Files likely touched:**

- `services/quant_foundry/src/quant_foundry/market_graph.py`
- `services/quant_foundry/tests/test_market_graph.py`
- `services/quant_foundry/src/quant_foundry/feature_lake.py`

**Implementation steps:**

1. Start with offline graph build.
2. Add nodes:
   - symbol;
   - sector;
   - event;
   - regime;
   - provider;
   - horizon outcome.
3. Add edges:
   - co-movement;
   - event similarity;
   - supply-chain relationship if data exists;
   - regime co-response.
4. Use graph features in research only.
5. Add analog explanations.

**Acceptance criteria:**

- Graph build is deterministic.
- Analog explanations cite historical examples.
- Graph features are versioned in dataset manifest.

**Dependencies:** TASK-0405 and Phase 7.

**Risk:** High data-quality risk.

**Rollback:** Exclude graph features from training manifests.

---

### TASK-1003: Conformal Prediction Risk Gate

**Order:** 46

**Objective:** Produce uncertainty intervals and abstain when the model cannot make a reliable prediction.

**Files likely touched:**

- `services/quant_foundry/src/quant_foundry/conformal.py`
- `services/quant_foundry/tests/test_conformal.py`

**Implementation steps:**

1. Add conformal calibration set.
2. Produce q10/q50/q90 intervals.
3. Mark predictions abstain when uncertainty is too wide.
4. Feed uncertainty into tournament and paper bridge.

**Acceptance criteria:**

- Coverage is measured.
- Wide uncertainty blocks promotion or bridge output.
- Intervals are visible in dossiers.

**Dependencies:** Phase 6 and Phase 7.

**Risk:** Medium.

**Rollback:** Ignore conformal gate outputs and continue with standard confidence scoring.

---

### TASK-1004: Adversarial Drift Sentinel

**Order:** 47

**Objective:** Detect when the current market is hostile to the active or shadow model set.

**Files likely touched:**

- `services/quant_foundry/src/quant_foundry/drift.py`
- `services/quant_foundry/tests/test_drift.py`
- `apps/dashboard/src/app/quant-foundry/health/page.tsx`

**Implementation steps:**

1. Track feature distribution drift.
2. Track calibration drift.
3. Track provider freshness drift.
4. Track prediction disagreement spikes.
5. Track live edge decay.
6. Emit recommendations:
   - lower trust;
   - shadow-only;
   - retrain;
   - retire.

**Acceptance criteria:**

- Drift sentinel flags synthetic fixture drift.
- Drift status appears in dashboard.
- Drift can block promotion.

**Dependencies:** Phase 6 and Phase 7.

**Risk:** Medium.

**Rollback:** Treat drift as advisory only.

---

### TASK-1005: Alpha Genome Lab

**Order:** 48

**Objective:** Automate feature/model recipe generation while forcing every recipe through leakage, walk-forward, shadow, and economic gates.

**Files likely touched:**

- `services/quant_foundry/src/quant_foundry/alpha_genome.py`
- `services/quant_foundry/tests/test_alpha_genome.py`
- `runpod/quant-foundry-training/`

**Implementation steps:**

1. Represent a recipe as a versioned config.
2. Mutate features and model settings within allowlisted ranges.
3. Enforce trial budgets.
4. Kill underperforming sweeps early.
5. Register only evidence-backed candidates.

**Acceptance criteria:**

- Generated recipes are reproducible.
- Bad candidates are discarded.
- No recipe can bypass tournament gates.

**Dependencies:** Phase 5, Phase 6, and Phase 7.

**Risk:** Very high overfitting and cost risk.

**Rollback:** Disable search and keep manually defined model families.

---

## 15. Phase 11: Limited Live Readiness

### Goal

Prepare for live influence only after long shadow and paper evidence.

### Hard Gate

Do not implement limited live mode until all of these are true:

- runtime safety guards are enforced everywhere;
- backtest path handling is locked down;
- verification receipts exist;
- Quant Foundry is contract-tested;
- settlement ledger is reliable;
- dossier registry is reliable;
- tournament scoring is reliable;
- shadow inference has enough settled history;
- paper bridge has run safely;
- rollback pointer exists;
- OMS and risk are unchanged and authoritative;
- human approval workflow is working;
- deployment environment has secure secrets and monitoring;
- live provider/broker credentials are never available to RunPod.

### TASK-1101: Limited Live Readiness Review

**Order:** 49

**Objective:** Produce a go/no-go report for limited live mode.

**Files likely touched:**

- `docs/LIMITED_LIVE_READINESS.md`
- `docs/RISKS.md`
- `docs/ROADMAP.md`
- Quant Foundry receipts

**Implementation steps:**

1. Summarize all evidence.
2. List every remaining blocker.
3. Prove rollback.
4. Prove risk caps.
5. Prove no RunPod broker credential access.
6. Prove human approval.
7. Require explicit operator decision.

**Acceptance criteria:**

- Report says either "not ready" with blockers or "ready for limited paper-to-live pilot" with exact caps.
- No code path can skip risk/OMS.
- Live mode remains disabled by default.

**Dependencies:** All previous phases.

**Risk:** Very high if rushed.

**Rollback:** Keep live disabled.

---

## 16. Recommended Commit/Execution Groups

Use small commits. Do not bundle everything into one giant change.

Recommended grouping:

1. `docs: add big implementation plan`
2. `chore: classify local tool artifacts`
3. `fix(config): enforce runtime safety across services`
4. `fix(api): restrict backtest input paths`
5. `chore(test): add verification receipt runner`
6. `test: add startup safety and path boundary coverage`
7. `docs: add environment reference`
8. `feat(dashboard): add route readiness atlas`
9. `feat(api): add module control registry`
10. `feat(quant-foundry): add contracts and signatures`
11. `feat(quant-foundry): add outbox inbox and mock dispatcher`
12. `feat(api): expose quant foundry mock gateway`
13. `feat(quant-foundry): add settlement ledger`
14. `feat(quant-foundry): add dossier registry`
15. `feat(quant-foundry): add tournament skeleton`
16. `feat(quant-foundry): add feature lake manifests`
17. `feat(runpod): add research training worker`
18. `feat(runpod): add shadow inference worker`
19. `feat(dashboard): add quant foundry operator pages`
20. `feat(quant-foundry): add promotion queue`

Each commit should run the smallest relevant tests and include the verification result in the handoff.

---

## 17. Top 15 Highest-Leverage Tasks

1. **Apply runtime safety guards to all service entrypoints.**
   - Highest safety improvement for smallest code change.
2. **Restrict backtest file paths to approved roots.**
   - Removes a clear trust-boundary weakness.
3. **Create the verification receipt runner.**
   - Turns scattered proof into reusable evidence.
4. **Add startup safety matrix tests.**
   - Prevents regression of the runtime guard.
5. **Generate the dashboard route/mock atlas.**
   - Makes product readiness visible.
6. **Add on-demand module control.**
   - Matches the one-operator cost-saving workflow.
7. **Create Quant Foundry contracts.**
   - Establishes the safety shape before RunPod.
8. **Add HMAC signatures and idempotency.**
   - Makes external callbacks safe to retry and reject.
9. **Add job outbox and callback inbox.**
   - Prevents lost jobs and duplicate effects.
10. **Build prediction settlement ledger.**
    - Creates the core evidence loop.
11. **Build dossier registry.**
    - Makes model artifacts reproducible and reviewable.
12. **Build tournament scoring skeleton.**
    - Turns settled predictions into governed rankings.
13. **Build feature lake manifests.**
    - Prevents leakage and gives RunPod controlled datasets.
14. **Add RunPod Research Foundry MVP.**
    - Begins GPU training after the scoreboard exists.
15. **Add Shadow Inference Swarm MVP.**
    - Begins live non-trading measurement.

---

## 18. Final Recommended Next Move

The very next implementation should be:

1. Runtime safety guards across all service entrypoints.
2. Backtest path allowlist validation.
3. Verification receipt runner.

Those three tasks reduce immediate security and reliability risk without changing the product model. After that, build the dashboard route atlas and on-demand module control. Only then start the Quant Foundry package with schemas, signatures, outbox, inbox, and mock dispatcher.

The fastest path to a powerful Fincept Quant Foundry is not to start with the biggest GPU cluster. It is to make every future prediction become evidence, every artifact become reproducible, every promotion require proof, and every optional module turn off when it is not needed.
