# Fincept Terminal — Next Steps Plan

> **Source plan:** `AAAAAAAAA_BIG_PLAN.md`
> **Current state:** Phase 0 (Freeze/Inventory/Stabilize) + Phase 1 (Verification/CI/Release Safety) complete, except TASK-0104 (CI hardening).
> **Date:** 2026-06-22
> **Scope:** Everything after the safety foundation, in plan order, with context for each phase and task.

---

## How to read this document

This plan picks up exactly where `docs/PHASE_0_1_HANDOFF.md` left off.
Each section explains:

- **what** the phase accomplishes;
- **why** it comes in this order (the plan's ordering is non-negotiable);
- **what each task does**, with files, acceptance criteria, and dependencies;
- **what "done" looks like** so you know when to move on.

The plan's master ordering rule is simple: **build the scoreboard before
adding more players.** Every phase before Phase 5 (RunPod) exists to
make sure that when GPU workers finally run, Fincept already has
contracts, signatures, durable storage, settlement, dossiers, and a
tournament gate. RunPod is the last thing added, not the first.

---

## Cross-cutting quant rigor requirements (apply to every phase)

These four disciplines are not a phase — they are invariants that every
task touching data, training, settlement, or scoring must satisfy. They
are listed once here and referenced by the tasks that enforce them.
Skipping them does not make the system faster; it makes the scoreboard
**lie**, which is worse than having no scoreboard. A pipeline that trains
thousands of models on leaky data and ranks them without multiple-testing
control is a machine for manufacturing false confidence.

### 1. Point-in-time correctness (no look-ahead, no survivorship)

The single largest source of fake alpha is leakage. Enforce it
mechanically, not by good intentions:

- Every feature used for a decision at time `t` must be derivable only
  from data with `observed_at <= t` (the *decision* time, not the event
  time — vendors revise and backfill). Use as-of (backward) joins only;
  forbid forward joins in code.
- Label horizons start strictly after the feature cutoff: a prediction
  made at `t` is settled on the window `(t, t+h]`.
- Reconstruct the tradable universe as-of each date, including delisted /
  renamed symbols, or survivorship silently inflates every backtest.
- Fit all transforms (scalers, winsorization bounds, target/category
  encoders) on the training fold only — never on statistics computed over
  the full sample.
- Prefer first-print / vintage data over restated data for fundamentals
  and macro; restatements are themselves look-ahead.
- Enforced by TASK-0405 (point-in-time manifest proofs) and TASK-0406
  (leakage & overfit sentinel).

### 2. Multiple-testing and overfit control

The best in-sample result of N random strategies looks excellent by luck
alone. Ranking thousands of candidates without correcting for this
guarantees promoting noise that decays the moment it is trusted.

- Validate with **purged k-fold + embargo** (López de Prado), not plain
  k-fold — overlapping labels leak across naive fold boundaries.
- Report the **Deflated Sharpe Ratio** (Bailey/López de Prado): discount
  observed Sharpe by the number of trials and the non-normality of
  returns. This requires honestly tracking trial count per model family
  in the dossier.
- Estimate the **Probability of Backtest Overfitting (PBO)** via
  combinatorially-symmetric cross-validation (CSCV) over the candidate
  set.
- Significance vs. baseline must use a test that respects autocorrelation
  from overlapping horizons (stationary/block bootstrap or
  Diebold-Mariano), never an IID t-test.
- Enforced by TASK-0404 (tournament) and TASK-0406 (sentinel).

### 3. Reproducibility and determinism

A dossier that cannot be reproduced cannot be trusted or rolled back.
Every training/inference artifact pins: dataset snapshot hash, feature
schema hash, label schema hash, code git SHA, dependency lockfile hash,
container image digest, random seed(s), and hardware class. Record any
known nondeterminism source (GPU kernel selection, thread count) rather
than pretending it does not exist. Enforced by TASK-0403 (dossier) and
TASK-0501 (training container).

### 4. Cost governance with a hard ceiling

GPU spend must fail closed, exactly like the JWT runtime guard. A global
monthly budget ceiling with a hard kill switch sits *above* per-sweep and
per-job budgets. Prefer interruptible/spot capacity with checkpoint +
resume. Track **cost-per-validated-edge** — GPU dollars spent per model
that actually clears the tournament gate — as the metric that decides
whether scaling training is worth it. Enforced by TASK-0502 (dispatch
client) and TASK-0901 (runtime plan).

---

## Current position in the plan

```
Phase 0  [DONE]  Safety guards, path boundaries, baseline receipt
Phase 1  [~~ ]   Verification receipt runner, matrix tests, env docs
                 (TASK-0104 CI hardening still pending — optional)
Phase 2  [    ]  Dashboard route atlas, readiness center, module control  ← START HERE
Phase 3  [    ]  Quant Foundry contracts + mock connectivity
Phase 4  [    ]  Evidence loop: settlement, shadow ledger, dossier, tournament, feature lake, leakage sentinel
Phase 5  [    ]  RunPod training MVP (first GPU touch)
Phase 6  [    ]  Shadow inference swarm
Phase 7  [    ]  Tournament governor + promotion workflow
Phase 8  [    ]  Quant Foundry dashboard pages
Phase 9  [    ]  Deployment + cost-optimized runtime
Phase 10 [    ]  Frontier performance modules
Phase 11 [    ]  Limited live readiness review
```

---

## Phase 1 remainder (optional but recommended)

### TASK-0104: Harden CI and supply chain defaults

**Order:** 10 (last Phase 1 task)

**Objective:** Make CI catch the same safety invariants the receipt
runner checks, plus supply-chain basics.

**Context:** Phase 0/1 built the local safety net (guards, path
boundary, auth sanitization, receipt runner, matrix tests). CI is the
remote safety net — it prevents a contributor from merging code that
drops a guard or widens a path. Without CI hardening, the matrix tests
only run on machines that remember to run them.

**What to do:**

- Pin GitHub Actions to commit SHAs (not `@v4` floating tags).
- Set least-privilege permissions on workflow files (`contents: read`
  by default, write only on release).
- Add a CI job that runs `pwsh ./scripts/verification-receipt.ps1`
  and fails the build on required-check failures.
- Add a CI job that runs the startup safety matrix tests
  (`uv run pytest libs/fincept-core/tests/test_startup_safety_matrix.py`).
- Add lockfile discipline (fail if `uv.lock` or `pnpm-lock.yaml` is
  out of sync).
- Add gitleaks secret scan as a required check.

**Acceptance criteria:**

- CI runs the receipt runner and matrix tests on every PR.
- Floating action versions are pinned.
- A PR that drops a runtime guard fails CI.

**Dependencies:** TASK-0101 (receipt runner — done).

**Risk:** Low. CI-only; no runtime impact.

**Rollback:** Revert workflow files.

---

## Phase 2: Dashboard Readiness and Module Control

### Goal

Make the operator able to see what is live, what is mock, what is
degraded, and to start/stop optional modules from one place.

### Why this comes before Quant Foundry

The operator runs Fincept as a one-person shop. Before adding a new
subsystem (Quant Foundry), the existing dashboard needs to honestly
report its own readiness. Otherwise the operator cannot tell whether
a new Quant Foundry panel is broken or the underlying system was
already degraded.

---

### TASK-0201: Generate a Dashboard Route and Mock-Data Atlas

**Order:** 11

**Objective:** Know which screens are live, mock, hybrid, or demo-only.

**Context:** The audit found mock labels scattered across some
components, but no central map. The operator currently has to read
source code to know whether a panel is backed by real API data or by
`mock-data.ts`. This is a safety issue: a mock panel mistaken for live
data could drive a wrong operator decision.

**What to do:**

- Scan every route under `apps/dashboard/src/app/`.
- Search for `MockBadge`, `mock-data`, `placeholder`, `demo`, fixture
  imports, and hardcoded arrays.
- For each route, record in `docs/dashboard-route-atlas.md`:
  - route path;
  - primary source files;
  - data status (live / mock / hybrid / demo);
  - backend dependency;
  - risk if mistaken for live;
  - replacement priority;
  - suggested test.
- Pick the first mock-heavy route to convert to a service-backed
  read-only route later.

**Files likely touched:**

- `docs/dashboard-route-atlas.md` (created)
- `apps/dashboard/src/app/` (inspected)
- `apps/dashboard/src/components/` (inspected)
- `apps/dashboard/src/lib/mock-data.ts` (inspected)
- `featuresmenu.md` (cross-referenced)

**Commands:**

```powershell
rg "MockBadge|mock-data|placeholder|demo|fixture" apps/dashboard/src
pnpm --dir apps/dashboard exec tsc --noEmit --pretty false
```

**Acceptance criteria:**

- Every dashboard route has a readiness status in the atlas.
- Mock-heavy screens are visible in one doc.
- The next conversion target is obvious.

**Dependencies:** TASK-0101 (done).

**Risk:** Low. Documentation-only.

---

### TASK-0202: Build a Unified System Readiness Center

**Order:** 12

**Objective:** One dashboard place that shows whether Fincept is ready,
degraded, disabled, or unsafe.

**Context:** Readiness information currently lives in scattered
scripts, source-health checks, and status surfaces. The operator has
to run `npm run test:source-health` in a terminal to know if the
system is healthy. This task brings that into the dashboard itself.

**What to do:**

- Define readiness categories: API, Redis, Timescale/Postgres,
  dashboard tests, verification receipt, provider freshness,
  news-impact shadow lane, model/dossier status (later), Quant Foundry
  status (later).
- Add or extend an API endpoint that returns readiness state.
- Display pass / warn / fail / skipped / disabled / stale states on
  the dashboard system page.
- Link the latest verification receipt.
- Never show secrets or raw stack traces.

**Files likely touched:**

- `apps/dashboard/src/app/system/page.tsx`
- `apps/dashboard/src/components/` (new readiness components)
- `apps/dashboard/src/lib/api.ts`
- `services/api/src/api/routes/` (readiness endpoint)
- `scripts/verification-receipt.ps1` (already exists)

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

**Dependencies:** TASK-0101 (done) and TASK-0201.

**Risk:** Medium (dashboard/API coordination).

---

### TASK-0203: Add On-Demand Module Control for Local and Staging

> **Owner:** Builder 5 (GLM-5.2) — ADOPTED 2026-06-22. IN PROGRESS.
> File-disjoint from TASK-0304 (Builder 2: quant_foundry outbox/inbox) and
> TASK-0401 (Builder 1: settlement ledger).

**Order:** 13

**Objective:** Make the "start module only when needed" workflow
explicit and first-class.

**Context:** The operator already boots the dashboard, then starts
OpenBB, news analysis, and other modules only when needed. This is a
cost optimization: modules that run constantly cost money. This task
makes that workflow explicit with idle timeouts, one-instance
controls, and a dashboard button.

**What to do:**

- Define a module registry: module ID, display name, start command,
  stop command, health command/URL, idle timeout, estimated cost
  class, allowed environments.
- Add API controls (local-only or authenticated): start, stop,
  restart, view logs, idle countdown, status badge.
- Add dashboard controls for the above.
- Add "Stop all optional modules" button.
- Record module start/stop receipts.

**Security requirements (non-negotiable):**

- No arbitrary shell command execution from user input.
- Module IDs must be allowlisted.
- API must require auth.
- Start commands must be predeclared server-side.
- Secrets must never be echoed into dashboard logs.

**Files likely touched:**

- `services/api/src/api/routes/modules.py` (created)
- `services/api/src/api/main.py`
- `apps/dashboard/src/app/system/page.tsx`
- `scripts/start.ps1`
- `scripts/modules/` (new per-module start scripts)
- `docs/ON_DEMAND_MODULES.md` (created)

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

**Risk:** High if command execution is not tightly allowlisted. Keep
local-only first.

---

### TASK-0204: Add Dashboard Fetch Timeouts and Better Error States

**Order:** 14

**Objective:** Prevent slow modules, provider calls, or backend issues
from freezing the operator experience.

**Context:** The audit found that dashboard API calls use `no-store`
freshness but lack explicit timeout/cancellation. A slow provider call
can hang a dashboard page indefinitely, which the operator mistakes
for "no data."

**What to do:**

- Add a shared timeout helper around `fetch` using `AbortController`.
- Return typed errors: unauthorized, unavailable, timeout, stale,
  validation failure.
- Update UI states to show precise operator messages.
- Add tests for timeout formatting.

**Files likely touched:**

- `apps/dashboard/src/lib/api.ts`
- `apps/dashboard/src/app/api/portfolio-report/route.ts`
- Dashboard pages that call API helpers
- Dashboard test scripts

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

**Risk:** Medium (many call sites).

---

### TASK-0205: Build Provider Evidence Redaction and Freshness Receipts

**Order:** 15

**Objective:** Make external data freshness reviewable without leaking
secrets.

**Context:** Provider data storage exists and source-health work
exists, but the audit found that evidence redaction and freshness
receipts need strengthening. The operator needs to see "Binance data
is 2 seconds stale, Polygon is 30 seconds stale" without the receipt
containing API keys or raw private URLs.

**What to do:**

- Define a provider evidence receipt schema.
- Redact token-like strings, account identifiers, raw private URLs,
  and sensitive payload fragments.
- Record provider name, request hash, timestamp, row count, freshness,
  and status.
- Add an API read endpoint for summarized evidence.
- Add a dashboard freshness view.
- Add tests with fake sensitive payloads.

**Files likely touched:**

- `libs/fincept-db/src/fincept_db/provider_data.py`
- `services/api/tests/test_provider_data.py`
- `services/oms/src/oms/alpaca/news_sync.py`
- `services/oms/src/oms/alpaca/marks.py`
- `apps/dashboard/src/components/news/news-intelligence-panel.tsx`
- `docs/RISKS.md`

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

---

## Phase 3: Quant Foundry Contracts and Mock Connectivity

### Goal

Create the safe bridge between Fincept and future RunPod workers
**without calling RunPod yet.**

### Why this comes before RunPod

Contracts, idempotency, signed callbacks, durable outboxes, callback
inboxes, and shadow-only schemas are what keep the system from
becoming fragile. They must be proven locally first. If you build the
RunPod worker first, you discover the contract bugs while spending GPU
money.

This phase proves the entire Fincept → worker → Fincept loop using a
mock dispatcher that uses the exact same schemas, signatures, and
storage as the future RunPod path. When you flip `QUANT_FOUNDRY_MODE`
from `local_mock` to `runpod`, only the dispatcher target changes.

---

### TASK-0301: Create `services/quant_foundry` Package Skeleton

**Order:** 16

**Objective:** Add a dedicated Quant Foundry Python service package
with no external GPU dependency.

**Context:** The design docs propose `services/quant_foundry/`, but it
does not yet exist as a package. This task creates the empty package so
all subsequent Quant Foundry work has a home.

**What to do:**

- Add `services/quant_foundry/` to the `uv` workspace.
- Create `pyproject.toml`, `src/quant_foundry/__init__.py`,
  `schemas.py`, `ids.py`, `signatures.py` stubs.
- Add initial tests.
- Ensure the package imports cleanly with `uv sync`.

**Files likely created:**

- `services/quant_foundry/pyproject.toml`
- `services/quant_foundry/src/quant_foundry/__init__.py`
- `services/quant_foundry/src/quant_foundry/schemas.py`
- `services/quant_foundry/src/quant_foundry/ids.py`
- `services/quant_foundry/src/quant_foundry/signatures.py`
- `services/quant_foundry/tests/test_schemas.py`
- `services/quant_foundry/tests/test_signatures.py`

**Tests:**

```powershell
uv run pytest services/quant_foundry/tests -q
```

**Acceptance criteria:**

- Package exists in the workspace.
- Tests run without RunPod credentials.
- No trading streams are touched.

**Dependencies:** Phase 0 and Phase 1 complete (done).

**Risk:** Low.

---

### TASK-0302: Define Quant Foundry Core Schemas

**Order:** 17

**Objective:** Create strict Pydantic schemas for all cross-boundary
Quant Foundry payloads.

**Context:** Core Fincept schemas exist in
`libs/fincept-core/src/fincept_core/schemas.py`, but Quant Foundry
needs its own external-worker contracts. These schemas are the
boundary between trusted Fincept and untrusted RunPod workers. They
must be strict (`extra="forbid"`) so a worker cannot inject unexpected
fields.

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

**Critical rule:** Shadow predictions must NOT be able to contain
order-like fields. Add an explicit test that these are rejected:

- `quantity`
- `order side`
- `broker account`
- `order type`
- `time in force`
- `notional size`

This is the schema-level enforcement of the "RunPod never owns trading
authority" invariant.

**What to do:**

- Use `extra="forbid"` for all external-facing payloads.
- Add explicit `schema_version` fields.
- Add `authority` field for predictions, with `shadow-only` as the
  only early value.
- Add the forbidden-order-fields test.
- Add JSON round-trip tests.

**Files likely touched:**

- `services/quant_foundry/src/quant_foundry/schemas.py`
- `services/quant_foundry/tests/test_schemas.py`

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

---

### TASK-0303: Add Idempotency Keys and HMAC Callback Signatures

**Order:** 18

**Objective:** Make cross-boundary communication retry-safe and
tamper-resistant.

**Context:** The connectivity spec requires at-least-once transport
with exactly-once effects. That means: jobs and callbacks may be
retried, but every side effect must happen exactly once. This is
achieved through (a) stable idempotency keys and (b) HMAC-signed
callbacks.

**Idempotency key format:**

```
qf:<job_type>:<dataset_id>:<model_family>:<config_hash>:<attempt_group>
```

**HMAC signature:**

```
HMAC_SHA256(callback_secret, timestamp + "." + job_id + "." + payload_hash)
```

**What to do:**

- Define the idempotency key format and a helper to hash request
  payloads.
- Add HMAC signing with timestamp skew validation.
- Add tamper tests (wrong signature, old timestamp, wrong job ID).
- Add duplicate callback key tests.

**Files likely touched:**

- `services/quant_foundry/src/quant_foundry/ids.py`
- `services/quant_foundry/src/quant_foundry/signatures.py`
- `services/quant_foundry/tests/test_signatures.py`
- `services/quant_foundry/tests/test_ids.py`

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

**Risk:** Medium. Security-sensitive code must be simple and
well-tested.

---

### TASK-0304: Implement Durable Local Job Outbox and Callback Inbox

**Order:** 19
**Owner:** Builder 2 (GLM-5.2) — COMPLETED 2026-06-22 (commit `48c0c27`)
  Files owned: `services/quant_foundry/src/quant_foundry/outbox.py`,
  `services/quant_foundry/src/quant_foundry/inbox.py`,
  `reports/quant-foundry/.gitkeep`. File-disjoint from TASK-0401
  (Builder 1) and TASK-0204. See
  `docs/AAA_GLM_SUPERTEAM_LOGS/BUILDER2.md`.

**Objective:** Store outbound jobs and inbound callbacks before any
domain processing.

**Context:** This is the durability layer. The outbox stores a job
record *before* dispatch, so if the process crashes, the job is not
lost. The inbox stores a callback *before* processing, so if
processing crashes, the callback can be reprocessed without calling
the worker again.

**What to do:**

- Start with local JSONL or SQLite storage (migration path to
  Postgres/Timescale later).
- Outbox fields: `job_id`, `job_type`, `idempotency_key`, `status`,
  `request_payload_hash`, `request_payload_ref`, timestamps,
  `attempt_count`, `next_retry_at`, `runpod_endpoint_id`,
  `runpod_job_id`, `timeout_seconds`, `priority`, `budget_cents`,
  `error_code`, `error_summary`.
- Inbox fields: `callback_id`, `job_id`, `idempotency_key`,
  `signature_valid`, `payload_hash`, `payload_ref`, `received_at`,
  `processed_at`, `status`, `schema_version`, `error_code`,
  `error_summary`.
- Status transitions for both (queued → dispatching → dispatched →
  running → callback_received → validating → completed / failed).
- Handle duplicate callbacks idempotently.
- **Reject same job ID with different payload hash as a security
  event.**

**Files likely created:**

- `services/quant_foundry/src/quant_foundry/outbox.py`
- `services/quant_foundry/src/quant_foundry/inbox.py`
- `services/quant_foundry/tests/test_outbox.py`
- `services/quant_foundry/tests/test_inbox.py`
- `reports/quant-foundry/.gitkeep`

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

**Risk:** Medium. Local JSONL is fine for MVP but must not be mistaken
for production durability.

---

### TASK-0305: Add Mock Dispatcher and Mock Callback Processor

**Order:** 20
**Owner:** Builder 2 (GLM-5.2) — COMPLETED 2026-06-22 (commit `26183c8`)
  Files owned: `services/quant_foundry/src/quant_foundry/mock_dispatcher.py`,
  `services/quant_foundry/src/quant_foundry/callbacks.py`,
  `services/quant_foundry/tests/test_mock_flow.py`. File-disjoint from
  TASK-0402 (Builder 3: shadow_ledger.py — I use a stub, not the real
  ledger), TASK-0405 (Builder 4: feature_lake.py), TASK-0203 (Builder 5:
  modules route). See `docs/AAA_GLM_SUPERTEAM_LOGS/BUILDER2.md`.

**Objective:** Prove the entire Fincept-to-worker-to-Fincept loop
without RunPod.

**Context:** This is the moment the contract becomes real. The mock
dispatcher uses the same schemas, signatures, idempotency keys,
outbox, and inbox as the future RunPod path. It just does the work
locally instead of on a GPU. If this loop works, flipping to RunPod
later is a dispatcher-only change.

**What to do:**

- Add a deterministic mock training job flow.
- Add a deterministic mock shadow inference job flow.
- Use the same schemas, signatures, idempotency keys, outbox, and
  inbox as the future RunPod path.
- Emit a local receipt.
- Add failure cases: bad signature, invalid schema, duplicate
  callback, terminal job failure.

**Files likely touched:**

- `services/quant_foundry/src/quant_foundry/mock_dispatcher.py`
- `services/quant_foundry/src/quant_foundry/callbacks.py`
- `services/quant_foundry/tests/test_mock_flow.py`

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

---

### TASK-0306: Add Quant Foundry API Route in Local Mock Mode

**Order:** 21
**Owner:** Builder 2 (GLM-5.2) — COMPLETED 2026-06-22 (commit `3ec6c06`)
  Files owned: `services/quant_foundry/src/quant_foundry/gateway.py`,
  `services/api/src/api/routes/quant_foundry.py`,
  `services/api/tests/test_quant_foundry.py`. Additive-only edit to
  `services/api/src/api/main.py`. Config read from env (no edit to shared
  `fincept_core/config.py`). See `docs/AAA_GLM_SUPERTEAM_LOGS/BUILDER2.md`.

**Objective:** Expose gateway endpoints through the API without RunPod
dependency.

**Context:** The operator needs to create jobs, check job status, and
receive callbacks through HTTP. This task adds the FastAPI router that
becomes the Quant Foundry gateway. It is disabled by default and
starts in `local_mock` mode.

**Endpoints:**

- `POST /quant-foundry/jobs` — create a job (auth required)
- `GET /quant-foundry/jobs` — list jobs (auth required)
- `GET /quant-foundry/jobs/{job_id}` — job detail (auth required)
- `POST /quant-foundry/callbacks/runpod` — callback endpoint (HMAC
  auth, NOT bearer auth)
- `GET /quant-foundry/health` — health state
- `GET /quant-foundry/heartbeats` — worker heartbeats

**Config:**

- `QUANT_FOUNDRY_ENABLED=false` by default
- `QUANT_FOUNDRY_MODE=local_mock`
- `QUANT_FOUNDRY_SHADOW_ONLY=true`

**What to do:**

- Add a FastAPI router.
- Require auth for operator endpoints.
- Keep callback auth separate through HMAC headers (not bearer).
- Return safe health state when disabled.
- Add tests for disabled, enabled mock, bad signature, duplicate
  callback.

**Files likely touched:**

- `services/api/src/api/routes/quant_foundry.py` (created)
- `services/api/src/api/main.py`
- `services/api/tests/test_quant_foundry.py` (created)
- `services/quant_foundry/src/quant_foundry/gateway.py`

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

**Risk:** Medium (API registration can affect startup).

---

## Phase 4: Evidence Loop Foundations

### Goal

Create the scoreboard before adding more models.

### Why this comes before RunPod

Training thousands of models is only useful if each model can be
judged later against realized outcomes, calibration, slippage,
drawdown, and baseline performance. Without settlement, dossiers, and
tournament scoring, trained models are ungoverned sprawl. This phase
builds the judging infrastructure first.

---

### TASK-0401: Build the Prediction Settlement Ledger

**Order:** 22

**Objective:** Judge every prediction after its horizon expires.

**Context:** `libs/fincept-core/src/fincept_core/prediction_log.py`
already has a `PredictionRow` with an `id` intended for future
settlement joins. This task creates the settlement worker that
matches predictions to realized outcomes and computes metrics.

**What to do:**

- Define `PredictionOutcome`.
- Settle simple direction/confidence predictions first.
- Settle strictly on the post-decision window `(t, t+h]` — realized
  return must use only prices observed after the prediction's decision
  time. This is the settlement-side guard against look-ahead; a
  prediction whose horizon has not fully elapsed stays `pending_time`.
- Add realized return by horizon.
- Add abnormal return versus benchmark where data exists.
- Add Brier score.
- Add calibration bucket.
- Add **explicit, versioned cost/slippage assumptions** (fee bps, modeled
  spread, slippage model, borrow cost). Store the cost-model version on
  each outcome so a later cost-model change does not silently rewrite
  history; settle both gross and net so the tournament can rank on net.
- Add `pending_time` (horizon not elapsed) and `pending_data` (market
  data missing) states, kept distinct so a stuck provider is not confused
  with a not-yet-due prediction.
- Make reruns idempotent (re-settling a prediction with the same inputs
  and cost-model version yields the identical outcome row).

**Files likely created:**

- `services/quant_foundry/src/quant_foundry/settlement.py`
- `services/quant_foundry/src/quant_foundry/outcomes.py`
- `services/quant_foundry/src/quant_foundry/metrics.py`
- `services/quant_foundry/tests/test_settlement.py`

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

---

### TASK-0402: Add Shadow Prediction Ledger Storage

> **Owner:** Builder 4 (GLM) — ADOPTED 2026-06-22 (BUILDING, TDD, local storage first).
> Previously Builder 1 yielded → Builder 3 released → UNOWNED → Builder 4 adopted.
> Files owned: `services/quant_foundry/shadow_ledger.py` + `tests/test_shadow_ledger.py`.
> File-disjoint from TASK-0401 (Builder 1: settlement), TASK-0304 (Builder 2: outbox/inbox),
> TASK-0405 (Builder 4: feature lake — DONE), TASK-0203 (Builder 5: module control), TASK-0403 (Builder 3: dossier — DONE),
> TASK-0306 (Builder 3: gateway), TASK-0404 (Builder 1: tournament).
> `schemas.py` is NOT modified (ShadowPrediction already defined by TASK-0302; consumed read-only).
> `libs/fincept-bus/streams.py` is NOT modified for MVP (local storage first; `qf.shadow.predictions`
> stream deferred to a later task per spec "later, if adding").

**Order:** 23

**Objective:** Store Quant Foundry shadow predictions separately from
existing trading prediction streams.

**Context:** Existing `sig.predict` can feed the orchestrator. Quant
Foundry shadow output must NOT go there until a paper bridge is
explicitly approved (TASK-0704). This task creates a separate,
isolated storage for shadow predictions.

**What to do:**

- Store shadow predictions in local storage first.
- Include: prediction ID, model ID, symbol, timestamp, horizon,
  expected return, p_up, confidence, feature availability, latency,
  regime metadata, `authority: shadow-only`.
- Reject any payload with order-like fields.
- Add idempotency by prediction ID and batch hash.
- Add read APIs later.

**Files likely touched:**

- `services/quant_foundry/src/quant_foundry/shadow_ledger.py`
- `services/quant_foundry/src/quant_foundry/schemas.py`
- `services/quant_foundry/tests/test_shadow_ledger.py`
- `libs/fincept-bus/src/fincept_bus/streams.py` (later, if adding
  `qf.shadow.predictions`)

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

**Risk:** Medium (defines a long-lived data contract).

---

### TASK-0403: Build the Dossier Registry

> **Owner:** Builder 3 (GLM-5.2) — COMPLETED 2026-06-22 (commit `de56c38`). REVIEW.
> Files owned: `services/quant_foundry/{dossier,artifacts,registry}.py` + `tests/test_dossier.py`.
> File-disjoint from TASK-0401/0402 (Builder 1), TASK-0304/0305 (Builder 2),
> TASK-0405 (Builder 4), TASK-0203 (Builder 5).
> `schemas.py` is NOT modified (ModelDossier + ArtifactManifest already defined by TASK-0302;
> consumed read-only). `services/api/routes/quant_foundry.py` is NOT created here (TASK-0306 owns
> the API route); the registry exposes a Python read API only for MVP.
> Tests: 25/25 green; full suite 121/121 green; ruff + mypy clean.

**Order:** 24

**Objective:** Make every model artifact understandable, reproducible,
and promotable only with evidence.

**Context:** A "dossier" is the complete record of a model: what
dataset it was trained on, what features it used, what artifact hash
it produced, what its settlement evidence looks like, and what its
tournament score is. Without dossiers, model artifacts are opaque
files. With dossiers, every model is reviewable and promotable only
with evidence.

**What to do:**

- Define `ModelDossier` and `ArtifactManifest`.
- The dossier must carry the full reproducibility set (cross-cutting rigor
  §3: dataset/feature/label hashes, code SHA, lockfile hash, image digest,
  seeds, hardware class), the **trial count** for the model family (so the
  tournament can deflate Sharpe), and a `blocking_issues` list that the
  sentinel (TASK-0406) and tournament (TASK-0404) write into.
- Add artifact hash verification.
- Generate a dossier for an existing local GBM model if available.
- Import a mock artifact from the mock dispatcher.
- Store dossiers immutable by version/hash.
- Add read-only API list/detail endpoints.

**Files likely touched:**

- `services/quant_foundry/src/quant_foundry/dossier.py`
- `services/quant_foundry/src/quant_foundry/artifacts.py`
- `services/quant_foundry/src/quant_foundry/registry.py`
- `services/quant_foundry/tests/test_dossier.py`
- `services/api/src/api/routes/quant_foundry.py`

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

---

### TASK-0404: Build Tournament Scoring Skeleton

> **Owner:** Builder 3 (GLM-5.2) — COMPLETED 2026-06-22 (commit `fd3f115`). REVIEW.
> Files owned: `services/quant_foundry/{tournament,leaderboard,significance}.py` + `tests/test_tournament.py`.
> File-disjoint from TASK-0401/0402 (Builder 1), TASK-0304/0305 (Builder 2),
> TASK-0405 (Builder 4), TASK-0203 (Builder 5). Consumes `SettlementRecord` and
> `DossierRecord` shapes via a local `ScoringInput` schema (no import of
> `outcomes.py`/`settlement.py`/`dossier.py` — keeps file-disjoint).
> Tests: 38/38 green; full suite 184/184 green; ruff + mypy clean.

**Order:** 25

**Objective:** Rank models based on settled evidence and baseline
comparisons.

**Context:** The tournament is the scoreboard. It takes settled
predictions from TASK-0401 and dossiers from TASK-0403 and produces a
ranked leaderboard. A model with a high ML score but poor
cost-adjusted return should lose to a simpler model that makes money.
This is what prevents overfit models from being promoted.

**Score components (the score must be cost- and luck-adjusted, not just
ML-accuracy):**

- out-of-sample net edge **after** modeled costs (fees, spread, slippage,
  borrow/financing) — gross edge is not allowed to drive ranking
- **Deflated Sharpe Ratio** (discounts for trial count + return
  non-normality), not raw Sharpe — see cross-cutting rigor §2
- calibration (reliability curve) and Brier score
- realized return by confidence bucket (monotonic edge-vs-confidence is a
  health signal; non-monotonic is a red flag)
- drawdown penalty
- turnover penalty (if available)
- feature availability penalty
- latency penalty
- capacity/decay penalty

**What to do:**

- Define scoring input schema (must carry trial count and the OOS return
  series, not just summary stats — bootstrap significance needs the
  series).
- Implement deterministic baseline comparison. Baselines must include at
  minimum: zero-skill (always-flat), the naive persistence/last-value
  predictor, and the relevant buy-and-hold. A model that cannot beat
  these net of cost is not a candidate.
- Significance test vs. baseline using a **stationary/block bootstrap or
  Diebold-Mariano** test that respects horizon-overlap autocorrelation —
  not an IID t-test. Store the p-value and the trial count used to deflate
  it.
- Add a simple, explainable weighted score over the components above; the
  weights and the deflation inputs are recorded so a rank is auditable.
- Add a blocking issues list (e.g. "fails net-of-cost vs persistence",
  "DSR ≤ 0 after deflation", "calibration not monotonic").
- Add stale evidence handling and a minimum-settled-sample gate (a model
  with too few settled predictions is `insufficient-evidence`, never
  ranked above one with sufficient evidence).
- Add deterministic fixture tests, including a **negative-control test**:
  a model trained on pure-noise/shuffled labels must NOT clear the gate.

**Files likely touched:**

- `services/quant_foundry/src/quant_foundry/tournament.py`
- `services/quant_foundry/src/quant_foundry/leaderboard.py`
- `services/quant_foundry/src/quant_foundry/significance.py`
- `services/quant_foundry/tests/test_tournament.py`

**Tests:**

```powershell
uv run pytest services/quant_foundry/tests/test_tournament.py -q
```

**Acceptance criteria:**

- Two fixture models rank deterministically.
- A model with high ML score but poor cost-adjusted return loses to a
  simpler profitable one.
- A noise/shuffled-label model fails the gate (negative control).
- A model that beats baseline gross but not net-of-cost is blocked.
- Deflated Sharpe and the bootstrap p-value are recorded and shown.
- Stale or insufficient evidence blocks promotion recommendation.
- Tournament output can feed a promotion packet later.

**Dependencies:** TASK-0401 and TASK-0403.

**Risk:** Medium. Keep scoring explainable; an opaque score the operator
cannot interrogate is itself a risk.

---

### TASK-0405: Build Feature Lake Builder MVP

> **Owner:** Builder 4 (GLM) — COMPLETED 2026-06-22 (commit `7f704bd`, 18/18 tests + ruff + mypy clean).
> Files owned: `services/quant_foundry/{feature_lake,dataset_manifest,feature_availability}.py` + `tests/test_feature_lake.py`.
> `schemas.py` and `services/features/computer.py` are NOT touched (richer manifest kept local; fixtures only).

**Order:** 26

**Objective:** Export point-in-time datasets and manifests for training
and shadow inference.

**Context:** RunPod workers need datasets to train on. But they should
never have direct DB credentials. The feature lake exports
point-in-time datasets with manifests that include feature schema
hashes, label schema hashes, train/val/test windows, row counts, and
checksums. A training job references a manifest, not a DB connection.

**What to do:**

- Start with fixture-backed dataset export.
- Generate dataset manifest: feature schema hash, label schema hash,
  train/validation/test windows, point-in-time proof fields, row
  count, checksum.
- **Point-in-time proof fields are mandatory, not optional** (cross-cutting
  rigor §1): each row records the `observed_at` (vendor-availability) time
  alongside the `event_ts`, and the export asserts every feature value's
  `observed_at <= ` the row's decision time. Joins are as-of/backward
  only; the builder rejects any forward join at construction time.
- Emit purged-k-fold + embargo split boundaries in the manifest so
  training and tournament use the *same* leakage-safe folds rather than
  re-deriving them inconsistently. The embargo length is recorded and
  must be ≥ the maximum label horizon in the dataset.
- Reconstruct the as-of universe (include delisted/renamed symbols) so the
  exported dataset is not survivorship-biased.
- Add feature availability report.
- Write export receipt.

**Files likely touched:**

- `services/quant_foundry/src/quant_foundry/feature_lake.py`
- `services/quant_foundry/src/quant_foundry/dataset_manifest.py`
- `services/quant_foundry/src/quant_foundry/feature_availability.py`
- `services/quant_foundry/tests/test_feature_lake.py`
- `services/features/src/features/computer.py`

**Tests:**

```powershell
uv run pytest services/quant_foundry/tests/test_feature_lake.py -q
uv run pytest services/features/tests -q
```

**Acceptance criteria:**

- Fixture dataset exports with stable manifest.
- Manifest hash changes when source data changes.
- A deliberately leaky fixture (a feature whose `observed_at` is after the
  decision time) is rejected at export, not silently included.
- Purged-fold boundaries and embargo length are present in the manifest;
  embargo ≥ max label horizon.
- Feature availability report exists.
- Training jobs can reference manifest instead of DB credentials.

**Dependencies:** TASK-0401.

**Risk:** Medium to high if attempting real provider data too soon.
Start with fixtures.

---

### TASK-0406: Build the Leakage and Overfit Sentinel

> **Owner:** Builder 3 (GLM-5.2) — COMPLETED 2026-06-22 (commit `d864b94`). REVIEW.
> Files owned: `services/quant_foundry/{sentinel,pbo}.py` + `tests/test_sentinel.py`.
> File-disjoint from TASK-0401/0402 (Builder 1), TASK-0304/0305/0501 (Builder 2),
> TASK-0405 (Builder 4), TASK-0203 (Builder 5). Imports from my own
> `dossier.py`/`registry.py` (TASK-0403 — my files). Does NOT import
> `outcomes.py`/`settlement.py` (Builder 1), `feature_lake.py`/
> `dataset_manifest.py` (Builder 4) — uses local schemas for feature/settlement data.
> Tests: 30/30 green; full suite 242/242 green (excl. Builder 2's in-progress file); ruff + mypy clean.

**Order:** 26b

**Objective:** Make data leakage and overfit promotion mechanically
detectable rather than relying on reviewer vigilance.

**Context:** TASK-0405 enforces point-in-time correctness at *export* and
TASK-0404 deflates scores at *ranking*, but nothing yet actively hunts for
the leakage and overfit patterns that survive both. This task adds the
adversarial check that an automated, high-throughput training pipeline
needs: when you train thousands of candidates, leakage and luck are not
edge cases, they are the default failure mode. This is the cheapest
insurance the Quant Foundry can buy — it runs on CPU against fixtures and
dossiers, with no GPU cost.

**What to do:**

- **Negative-control battery:** shuffle labels, time-reverse features, and
  inject a known future-leaking feature; assert each collapses measured
  edge to ≈ baseline. A pipeline that still "finds alpha" on shuffled
  labels is leaking, full stop.
- **Purged-fold verifier:** confirm a dossier's reported folds actually
  carry purge + embargo and that no training row overlaps a validation
  label window.
- **PBO estimate:** compute the Probability of Backtest Overfitting (CSCV)
  over a candidate family and attach it to the dossier; flag families
  above a configurable threshold.
- **Train/live gap check:** compare in-sample vs. settled live calibration
  and edge; a large, persistent gap is an overfit flag feeding TASK-0703
  (edge-decay).
- **Feature stability:** flag features whose importance or distribution
  is wildly unstable across folds (likely artifacts, not signal).
- Emit a sentinel receipt per candidate family; a failing sentinel is a
  hard `blocking_issue` on the dossier that the promotion gate (TASK-0702)
  refuses to override without an explicit, recorded human waiver.

**Files likely created:**

- `services/quant_foundry/src/quant_foundry/sentinel.py`
- `services/quant_foundry/src/quant_foundry/pbo.py`
- `services/quant_foundry/tests/test_sentinel.py`

**Tests:**

```powershell
uv run pytest services/quant_foundry/tests/test_sentinel.py -q
```

**Acceptance criteria:**

- Shuffled-label and future-leak fixtures are flagged as leaking.
- A fold set without purge/embargo is rejected.
- PBO is computed and attached to the dossier.
- A failing sentinel blocks promotion server-side, not just visually.

**Dependencies:** TASK-0403, TASK-0404, and TASK-0405.

**Risk:** Low to medium. CPU-only, fixture-driven; high leverage for the
cost.

---

## Phase 5: RunPod Research Foundry MVP

### Goal

Use RunPod for GPU training and evaluation while Fincept only
receives artifacts, manifests, receipts, and dossiers.

### Why this comes after evidence foundations

The Research Foundry should generate candidates for a scoreboard that
already exists. Without settlement and dossiers, training scale
creates ungoverned model sprawl. Now that the scoreboard exists
(Phase 4), RunPod training can begin.

**This is the first phase that touches GPU infrastructure.**

---

### TASK-0501: Build RunPod Training Container MVP

**Order:** 27
**Owner:** Builder 2 (GLM-5.2) — COMPLETED 2026-06-22 (commit `2283b43`)
  Files owned: `services/quant_foundry/src/quant_foundry/runpod_training.py`,
  `services/quant_foundry/tests/test_runpod_training.py`,
  `runpod/quant-foundry-training/handler.py`,
  `runpod/quant-foundry-training/Dockerfile`,
  `runpod/quant-foundry-training/README.md`. File-disjoint from all
  active builders. See `docs/AAA_GLM_SUPERTEAM_LOGS/BUILDER2.md`.

**Objective:** Create a minimal RunPod-compatible training worker.

**Context:** This is the first RunPod worker. It runs in a container
on RunPod's GPU infrastructure. It receives a `RunPodTrainingRequest`,
reads a dataset manifest, trains a tiny baseline model, writes an
artifact manifest, and sends a signed callback. It has no broker
credentials, no Redis access, no stream write capability.

**What to do:**

- Start with a local container-compatible handler.
- Accept `RunPodTrainingRequest`.
- Read dataset manifest.
- Train a tiny baseline or fake model first.
- Write artifact manifest, pinning the full reproducibility set
  (cross-cutting rigor §3): dataset snapshot hash, feature/label schema
  hashes, code git SHA, dependency lockfile hash, container image digest,
  random seed(s), and hardware class. Re-running the same request must
  produce the same artifact hash on the same hardware class; any known
  nondeterminism source is recorded, not hidden.
- Write training receipt.
- Send signed callback.
- Enforce time and budget limits.

**Files likely created:**

- `runpod/quant-foundry-training/handler.py`
- `runpod/quant-foundry-training/Dockerfile`
- `runpod/quant-foundry-training/README.md`
- `services/quant_foundry/src/quant_foundry/runpod_training.py`
- `services/quant_foundry/tests/test_runpod_training.py`

**Tests:**

```powershell
uv run pytest services/quant_foundry/tests -q -k runpod_training
docker build -t fincept-qf-training:local runpod/quant-foundry-training
```

**Acceptance criteria:**

- Local mock trainer and container handler use the same contract.
- No broker credentials are available.
- Artifact manifest is hash-verifiable.
- Training failure returns a safe terminal or retryable status.

**Dependencies:** TASK-0403 and TASK-0405.

**Risk:** Medium. Container dependency drift is likely.

---

### TASK-0502: Implement RunPod Job Dispatch Client

**Order:** 28
**Owner:** Builder 2 (GLM-5.2) — COMPLETED 2026-06-22 (commit `b3fc4e1`)
  Files owned: `services/quant_foundry/src/quant_foundry/runpod_client.py`,
  `services/quant_foundry/tests/test_runpod_client.py`,
  `services/quant_foundry/src/quant_foundry/gateway.py` (additive —
  RunPodClient injection). File-disjoint from all active builders. See
  `docs/AAA_GLM_SUPERTEAM_LOGS/BUILDER2.md`.

**Objective:** Dispatch training jobs from Fincept outbox to RunPod
without coupling core services to RunPod.

**Context:** This is the dispatcher that reads the outbox (TASK-0304)
and calls RunPod's API. It is the only component in Fincept that talks
to RunPod. It is behind a config flag (`QUANT_FOUNDRY_MODE=runpod`).
When disabled, the mock dispatcher (TASK-0305) is used instead.

**What to do:**

- Add `RunPodClient` interface.
- Add mock implementation.
- Add real HTTP implementation behind config.
- Read RunPod API key only server-side.
- Enforce dispatch rate limits.
- Enforce per-job budget metadata.
- Enforce a **global monthly GPU budget ceiling with a hard kill switch**
  that sits above per-job and per-sweep budgets and fails closed (refuses
  to dispatch) when exceeded — the cost analogue of the JWT runtime guard
  (cross-cutting rigor §4). The ceiling is a config value, never inferred.
- Prefer interruptible/spot capacity with checkpoint + resume so a
  preempted long job restarts from its last checkpoint rather than
  re-billing from zero; classify preemption as transient (retryable),
  not terminal.
- Record actual cost + duration per job in the outbox so
  cost-per-validated-edge (GPU dollars per model that clears the
  tournament gate) can be computed in TASK-0901.
- Store RunPod job ID in outbox.
- Classify transient and terminal errors.

**Files likely touched:**

- `services/quant_foundry/src/quant_foundry/runpod_client.py`
- `services/quant_foundry/src/quant_foundry/gateway.py`
- `services/quant_foundry/tests/test_runpod_client.py`
- `docs/ENVIRONMENT.md`

**Tests:**

```powershell
uv run pytest services/quant_foundry/tests/test_runpod_client.py -q
```

**Acceptance criteria:**

- No RunPod call happens unless explicitly enabled.
- Failed RunPod calls leave retryable jobs.
- Rate and budget limits are enforced.
- The global budget ceiling fails closed: dispatch is refused once the
  monthly cap is hit, with a clear receipt (not a silent drop).
- A simulated spot preemption resumes from checkpoint, not from zero.
- API key is never returned to dashboard or logs.

**Dependencies:** TASK-0501.

**Risk:** Medium to high (external API behavior can drift).

**Rollback:** Switch `QUANT_FOUNDRY_MODE=local_mock`.

---

### TASK-0503: Add Artifact Import From Object Storage

> **Owner:** Builder 3 (GLM-5.2) — COMPLETED 2026-06-22 (commit `ae893a6`). REVIEW.
> Files owned: `services/quant_foundry/src/quant_foundry/artifacts.py` (extended),
> `services/quant_foundry/tests/test_artifacts.py` (new), `docs/ENVIRONMENT.md` (new).
> Extends my own `artifacts.py` (TASK-0403) with S3/object storage URI support,
> size limits, content type validation, quarantine/staging path, and security
> receipts. File-disjoint from all other active builders.
> Tests: 28/28 green; full suite 270/270 green (excl. Builder 2's in-progress file); ruff + mypy clean.

**Order:** 29

**Objective:** Import model artifacts by pulling from controlled
storage and verifying hash/size/content type.

**Context:** RunPod workers write model artifacts to S3 or object
storage. The callback only contains the manifest (hash, size, URI).
Fincept pulls the artifact, verifies it, and registers it in the
dossier registry. This is pull-based, not push-based, so RunPod never
pushes large blobs into Fincept callbacks.

**What to do:**

- Define allowed artifact URI schemes (allowlist, not arbitrary).
- Add size limits.
- Add content type validation.
- Download to a quarantine/staging path.
- Verify hash before registration.
- Store immutable metadata.
- Reject mismatches and emit a security receipt.

**Files likely touched:**

- `services/quant_foundry/src/quant_foundry/artifacts.py`
- `services/quant_foundry/src/quant_foundry/registry.py`
- `services/quant_foundry/tests/test_artifacts.py`
- `docs/ENVIRONMENT.md`

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

---

### TASK-0504: Train First Real Baseline Model Family

> **Owner:** Builder 3 (GLM-5.2) — COMPLETED 2026-06-22 (commit `caeb468`). REVIEW.
> Files owned: `services/quant_foundry/src/quant_foundry/baseline_family.py` (new),
> `services/quant_foundry/tests/test_baseline_family.py` (new).
> Creates a file-disjoint baseline training orchestrator that uses my sentinel
> (TASK-0406), artifact import (TASK-0503), and dossier registry (TASK-0403).
> Does NOT touch Builder 2's `runpod_training.py` / `test_runpod_training.py` /
> `runpod/quant-foundry-training/handler.py` — those are Builder 2's files for
> the RunPod container; this task creates the workflow orchestration layer that
> connects training → validation → sentinel → artifact → dossier.
> Tests: 30/30 green; full suite 300/300 green (excl. Builder 2's in-progress file); ruff + mypy clean.

**Order:** 30

**Objective:** Produce the first RunPod-trained candidate, starting
simple.

**Context:** The recommended first family is LightGBM or CatBoost, NOT
a transformer. A simple model makes leakage, manifests, artifacts, and
tournament behavior easier to verify. If a simple baseline cannot pass
the evidence loop, a frontier model will not save the system.

**What to do:**

- Select one small dataset manifest.
- Train one baseline model family (LightGBM/CatBoost — gradient-boosted
  trees are the right first family: strong tabular baselines, fast,
  interpretable feature importance, and cheap to retrain, which makes
  leakage and tournament behavior easy to verify before any frontier
  model is attempted).
- Run **purged walk-forward** validation using the embargoed folds from the
  manifest (TASK-0405) — not a plain expanding-window split.
- Run the negative-control check (shuffled labels) as part of the job and
  record the result in the dossier; a candidate that scores well on
  shuffled labels is quarantined, not imported.
- Produce calibration report.
- Produce feature importance report (with cross-fold stability).
- Produce economic metrics net of the versioned cost model (TASK-0401).
- Record the trial count for this family so the tournament can deflate
  Sharpe correctly.
- Package artifact.
- Import dossier.
- Keep model at `candidate` or `research-approved`.

**Files likely touched:**

- `runpod/quant-foundry-training/handler.py`
- `services/quant_foundry/src/quant_foundry/runpod_training.py`
- `services/quant_foundry/tests/test_runpod_training.py`
- `docs/quant-foundry/` (later)

**Tests:**

```powershell
uv run pytest services/quant_foundry/tests -q -k "feature_lake or dossier or tournament or runpod_training"
```

**Acceptance criteria:**

- One real trained artifact imports.
- Dossier includes dataset and feature schema plus the full
  reproducibility set, and re-running reproduces the artifact hash.
- The shuffled-label negative control is recorded and passes (no edge on
  noise).
- Model cannot influence predictions or orders yet.
- Costs and duration are recorded.

**Dependencies:** TASK-0503 and TASK-0406 (the sentinel must exist before
the first real model is ranked).

**Risk:** Medium to high. Training environment issues are common.

---

## Phase 6: Shadow Inference Swarm MVP

### Goal

Run live non-trading predictions through RunPod and measure them in
Fincept.

### Why this comes after research MVP

Only imported and dossiered candidate models should be eligible for
shadow inference. Shadow inference is a measurement lane, not a
trading lane. It produces predictions that are settled against
realized outcomes but never reach `sig.predict`.

---

### TASK-0601: Build RunPod Inference Container MVP

> **Owner:** Builder 3 (GLM-5.2) — COMPLETED 2026-06-22 (commit `df326d4`). REVIEW.
> Files owned: `services/quant_foundry/src/quant_foundry/shadow_inference.py` (new),
> `services/quant_foundry/tests/test_shadow_inference.py` (new),
> `runpod/quant-foundry-inference/handler.py` (new),
> `runpod/quant-foundry-inference/Dockerfile` (new),
> `runpod/quant-foundry-inference/README.md` (new).
> File-disjoint from Builder 2's `runpod/quant-foundry-training/` (different
> subdirectory). Imports `ShadowPrediction` from `schemas.py` (read-only) and
> `ArtifactRecord` from my `artifacts.py` (TASK-0503).
> Tests: 30/30 green; full suite 330/330 green (excl. Builder 2's in-progress file); ruff + mypy clean.

**Order:** 31

**Objective:** Run candidate model predictions on RunPod and return
shadow-only prediction batches.

**What to do:**

- Accept `RunPodInferenceRequest`.
- Load a candidate artifact from read-only cache or controlled URI.
- Score fixture feature snapshots first.
- Return `ShadowPrediction` batch with `authority: shadow-only`.
- Include latency and feature availability.
- Send signed callback.

**Files likely created:**

- `runpod/quant-foundry-inference/handler.py`
- `runpod/quant-foundry-inference/Dockerfile`
- `runpod/quant-foundry-inference/README.md`
- `services/quant_foundry/src/quant_foundry/shadow_inference.py`
- `services/quant_foundry/tests/test_shadow_inference.py`

**Acceptance criteria:**

- Container returns valid shadow predictions.
- Invalid feature snapshot fails safely.
- No output contains order fields.
- Inference can be disabled without breaking Fincept.

**Dependencies:** TASK-0504.

---

### TASK-0602: Add Live Feature Snapshot Export

> **Owner:** Builder 3 (GLM-5.2) — COMPLETED 2026-06-22 (commit `1a91a82`). REVIEW.
> Files owned: `services/quant_foundry/src/quant_foundry/feature_snapshot_export.py` (new),
> `services/quant_foundry/tests/test_feature_snapshots.py` (new).
> Creates a file-disjoint feature snapshot exporter that imports from
> Builder 4's `feature_lake.py` + `feature_availability.py` (read-only) and
> my `shadow_inference.py` (TASK-0601, `FeatureSnapshot`). Does NOT modify
> `feature_lake.py`, `feature_availability.py`, or `services/features/`.
> Tests: 29/29 green; full suite 359/359 green (excl. Builder 2's in-progress file); ruff + mypy clean.

**Order:** 32

**Objective:** Provide compact, point-in-time feature snapshots for
shadow inference.

**Context:** The inference worker needs feature inputs. These are
exported from Fincept's feature store as compact snapshots with
freshness metadata and availability scores. If availability is too
low, the worker abstains rather than predicting on incomplete data.

**Files likely touched:**

- `services/quant_foundry/src/quant_foundry/feature_lake.py`
- `services/quant_foundry/src/quant_foundry/feature_availability.py`
- `services/features/src/features/computer.py`
- `services/quant_foundry/tests/test_feature_snapshots.py`

**Acceptance criteria:**

- Feature snapshots are compact.
- Feature availability is measurable.
- Missing required features produce abstain or degraded state.

**Dependencies:** TASK-0405 and TASK-0601.

---

### TASK-0603: Store and Settle Shadow Predictions

> **Owner:** Builder 3 (GLM-5.2) — COMPLETED 2026-06-22 (commit `0aa4aef`). REVIEW.
> Files owned: `services/quant_foundry/src/quant_foundry/shadow_settlement.py` (new),
> `services/quant_foundry/tests/test_shadow_settlement.py` (new).
> Creates a file-disjoint shadow settlement orchestrator that imports from
> Builder 1's `shadow_ledger.py` + `settlement.py` (read-only) and my
> `shadow_inference.py` (TASK-0601). Does NOT modify `shadow_ledger.py`,
> `settlement.py`, or `schemas.py`.
> Tests: 17/17 green; full suite 376/376 green (excl. Builder 2's in-progress file); ruff + mypy clean.

**Order:** 33

**Objective:** Connect RunPod shadow predictions to the settlement
ledger.

**What to do:**

- Store signed prediction batches.
- Reject bad signatures and schemas.
- Mark predictions pending by horizon.
- Settle after horizon expires.
- Update model live calibration metrics.
- Emit receipt for settled batches.

**Files likely touched:**

- `services/quant_foundry/src/quant_foundry/shadow_ledger.py`
- `services/quant_foundry/src/quant_foundry/settlement.py`
- `services/quant_foundry/tests/test_shadow_settlement.py`

**Acceptance criteria:**

- Shadow predictions settle into outcomes.
- Settlement lag is visible.
- No prediction reaches `sig.predict`.
- Invalid callback is stored as rejected, not silently discarded.

**Dependencies:** TASK-0602.

---

### TASK-0604: Add Shadow Inference Health Dashboard

**Order:** 34

**Objective:** Let the operator see shadow model health, latency,
drift, and settlement progress.

**What to do:**

- Add API methods for shadow status.
- Add dashboard page showing: enabled/disabled, models running, latest
  prediction, latency p50/p95, feature availability, callback
  rejection rate, settlement lag, circuit breaker state.
- Add loading, empty, degraded, disabled, and error states.

**Files likely touched:**

- `apps/dashboard/src/app/quant-foundry/shadow/page.tsx`
- `apps/dashboard/src/app/quant-foundry/page.tsx`
- `apps/dashboard/src/lib/api.ts`
- `services/api/src/api/routes/quant_foundry.py`

**Acceptance criteria:**

- Operator can distinguish disabled from broken.
- Stale shadow predictions are visible.
- Rejected callbacks are visible without exposing secrets.

**Dependencies:** TASK-0603.

---

## Phase 7: Tournament Governor and Promotion Workflow

### Goal

Turn prediction evidence into ranked recommendations, not automatic
trades.

### Why this comes last (before dashboard)

This phase defines governance. It is the gate between "model exists"
and "model can influence paper trading." It must come after shadow
inference has produced enough settled evidence to make promotion
decisions meaningful.

---

### TASK-0701: Expand Tournament Leaderboards

> **Owner:** Builder 3 (GLM-5.2) — COMPLETED 2026-06-22 (commit `0831e2c`). REVIEW.
> Files owned: `services/quant_foundry/src/quant_foundry/leaderboard_expanded.py` (new),
> `services/quant_foundry/tests/test_leaderboard_expanded.py` (new).
> Creates a file-disjoint expanded leaderboard with horizon/regime/symbol-cluster
> slices, baseline deltas, calibration summaries, and decay indicators.
> Imports from my `leaderboard.py` + `tournament.py` (read-only). Does NOT
> modify them (avoids breaking existing TASK-0404 tests).
> Tests: 28/28 green; full suite 404/404 green (excl. Builder 2's in-progress file); ruff + mypy clean.

**Order:** 35

**Objective:** Score models by horizon, regime, symbol cluster, event
type, and live shadow evidence.

**What to do:**

- Add horizon slices, regime slices, symbol-cluster slices,
  event/news-type slices.
- Add baseline deltas.
- Add confidence calibration summaries.
- Add decay indicators.

**Acceptance criteria:**

- A model can rank high in one horizon and low in another.
- Stale or decayed models are flagged.
- Leaderboard explains why a model ranks where it does.

**Dependencies:** TASK-0603.

---

### TASK-0702: Build Promotion Review Queue

> **Owner:** Builder 3 (GLM-5.2) — COMPLETED 2026-06-22 (commit `60f9e61`). REVIEW.
> Files owned: `services/quant_foundry/src/quant_foundry/promotion.py` (new),
> `services/quant_foundry/tests/test_promotion.py` (new).
> Creates a file-disjoint promotion review queue. Does NOT touch
> `services/api/src/api/routes/quant_foundry.py` (Builder 2's file) or
> `apps/dashboard/` (Builder 1's files) — those are separate tasks.
> Imports from my `dossier.py` (TASK-0403), `sentinel.py` (TASK-0406),
> `tournament.py` (TASK-0404), and `leaderboard_expanded.py` (TASK-0701).
> Tests: 24/24 green; full suite 428/428 green (excl. Builder 2's in-progress file); ruff + mypy clean.

**Order:** 36

**Objective:** Require human approval and evidence packets for model
promotion.

**Context:** This is the governance gate. Promotion levels:
`candidate` → `research-approved` → `shadow-approved` →
`paper-approved` → `limited-live-approved` → `active`. For MVP, allow
only up to `shadow-approved`.

**What to do:**

- Require: dossier, artifact hash, settlement evidence, tournament
  score, **a clean leakage/overfit sentinel result (TASK-0406)**, an empty
  (or explicitly human-waived) blocking issues list, and a human review
  note. A non-empty blocking issue cannot be promoted past without a
  recorded, named waiver — the gate fails closed.
- Enforce a **minimum settled-evidence bar** server-side: a model below a
  configured count of settled predictions, or below a configured live
  observation window, is `insufficient-evidence` and not promotable
  regardless of score.
- Add rejection reasons.
- Add immutable promotion receipt.

**Files likely touched:**

- `services/quant_foundry/src/quant_foundry/promotion.py`
- `services/quant_foundry/tests/test_promotion.py`
- `services/api/src/api/routes/quant_foundry.py`
- `apps/dashboard/src/app/quant-foundry/promotion/page.tsx`

**Acceptance criteria:**

- No model can be promoted without a dossier.
- No model can be promoted without settlement evidence.
- Human approval is stored.
- Rejection is stored with reason.

**Dependencies:** TASK-0701.

**Risk:** Medium to high (defines governance).

---

### TASK-0703: Add Retirement and Edge-Decay Flags

> **Owner:** Builder 3 (GLM-5.2) — COMPLETED 2026-06-22 (commit `ffe9ce7`). REVIEW.
> Files owned: `services/quant_foundry/src/quant_foundry/retirement.py` (new),
> `services/quant_foundry/tests/test_retirement.py` (new).
> Creates a file-disjoint retirement + edge-decay flagger. Imports from my
> `leaderboard_expanded.py` (TASK-0701) + `tournament.py` (TASK-0404).
> Does NOT modify them.
> Tests: 24/24 green; full suite 452/452 green (excl. Builder 2's in-progress file); ruff + mypy clean.

**Order:** 37

**Objective:** Automatically flag models that stop working.

**What to do:**

- Define decay thresholds.
- Detect: calibration degradation, net edge below baseline, feature
  availability degradation, latency budget violations, drawdown
  contribution warnings.
- Emit retirement recommendations.

**Acceptance criteria:**

- A decayed fixture model is flagged.
- Flag includes reason.
- Retirement recommendation cannot delete artifacts.
- Dashboard shows retire/retrain suggestion.

**Dependencies:** TASK-0701.

---

### TASK-0704: Build Paper-Only Model Pointer Bridge

> **Owner:** Builder 3 (GLM-5.2) — COMPLETED 2026-06-22 (commit `e95c51f`). REVIEW.
> Files owned: `services/quant_foundry/src/quant_foundry/paper_bridge.py` (new),
> `services/quant_foundry/tests/test_paper_bridge.py` (new).
> Creates a file-disjoint paper-only model pointer bridge. Does NOT touch
> `libs/fincept-core/`, `libs/fincept-bus/`, `services/orchestrator/`,
> `services/risk/`, or `services/oms/` (other builders' files). Imports
> from my `promotion.py` (TASK-0702), `dossier.py` (TASK-0403),
> `schemas.py` (read-only).
> Tests: 24/24 green; full suite 476/476 green (excl. Builder 2's in-progress file); ruff + mypy clean.

**Order:** 38

**Objective:** Allow approved models to influence paper workflows
without live trading authority.

**Context:** This is the **first dangerous connection point.** The
existing orchestrator can consume `sig.predict`. This bridge converts
a shadow prediction to an existing `Prediction` schema and publishes
it — but only in paper mode, only for `paper-approved` models, only
when `QUANT_FOUNDRY_ALLOW_PAPER_BRIDGE=true`.

**What to do:**

- Require `paper-approved` model status.
- Require explicit config: `QUANT_FOUNDRY_ALLOW_PAPER_BRIDGE=true`.
- Convert shadow prediction to existing `Prediction` schema.
- Publish only in paper mode.
- Store rollback pointer before enabling.
- Add circuit breaker for bad predictions or missing evidence.
- Keep OMS and risk authoritative.

**Files likely touched:**

- `services/quant_foundry/src/quant_foundry/paper_bridge.py`
- `services/quant_foundry/src/quant_foundry/promotion.py`
- `libs/fincept-core/src/fincept_core/schemas.py`
- `libs/fincept-bus/src/fincept_bus/streams.py`
- `services/orchestrator/`
- `services/risk/`
- `services/oms/`
- `services/quant_foundry/tests/test_paper_bridge.py`

**Acceptance criteria:**

- Bridge is disabled by default.
- Bridge refuses non-paper runtime.
- Bridge refuses models without evidence packet.
- Rollback pointer exists.
- Risk/OMS boundaries remain unchanged.

**Dependencies:** TASK-0702 and TASK-0703.

**Risk:** HIGH. This is the first path that can influence
trading-adjacent decisioning.

**Rollback:** Disable `QUANT_FOUNDRY_ALLOW_PAPER_BRIDGE`.

---

## Phase 8: Quant Foundry Dashboard and Operator Control

### Goal

Expose Quant Foundry clearly to the one-person operator without
creating accidental authority.

---

### TASK-0801: Add Quant Foundry Overview Page

**Order:** 39

**What to do:**

- Add route `apps/dashboard/src/app/quant-foundry/page.tsx`.
- Show module status cards: Gateway, Outbox, Callback Inbox, Feature
  Lake, Settlement, Dossier Registry, Tournament, RunPod Research,
  Shadow Inference.
- Show global mode: disabled, local mock, RunPod research, RunPod
  shadow, paper bridge.
- Show cost/budget state.
- Show latest receipts.

**Acceptance criteria:**

- Page loads in disabled mode.
- Disabled is not shown as failure.
- No action can promote or trade from overview.

**Dependencies:** TASK-0306 and TASK-0202.

---

### TASK-0802: Add Jobs, Dossiers, Tournament, and Promotion Pages

**Order:** 40

**What to do:**

- Jobs page: queued, running, retrying, failed, completed.
- Models page: dossier list, artifact hash, status, evidence
  completeness.
- Tournament page: leaderboard, baseline deltas, blocking issues,
  decay flags.
- Promotion page: review packet, approve/reject, confirmation,
  rollback visibility.
- Add empty/error/loading/stale states everywhere.

**Acceptance criteria:**

- Operator can see all Quant Foundry states.
- Promotion requires confirmation.
- Missing evidence blocks promotion visually and server-side.

**Dependencies:** TASK-0801 and Phase 7.

---

## Phase 9: Deployment and Cost-Optimized Runtime

### Goal

Run the system cheaply for one operator while keeping a clear path to
serious production infrastructure.

### Deployment principle

Always-on thin shell + on-demand workers:

- **Always-on:** dashboard, API, Redis, Postgres, orchestrator, OMS,
  risk, core ingestion.
- **On-demand:** OpenBB, news analysis, feature jobs, backtests,
  RunPod training, RunPod inference.

---

### TASK-0901: Create Local/Staging Module Runtime Plan

**Order:** 41

**What to do:**

- Define module list with start/stop scripts, health checks, idle
  timeout, max instances, estimated monthly cost (always-on vs 2
  hours/day).
- Add budget guard before heavy jobs.
- Add "stop all optional modules."

**Dependencies:** TASK-0203.

---

### TASK-0902: Use Railway for Test/Staging Only

**Order:** 42

**Context:** Railway is cost-effective for staging but NOT for GPU
training or production broker-adjacent OMS.

**Recommended Railway use:**

- dashboard staging, API staging, small Postgres test DB, Redis test
  instance, mock Quant Foundry gateway, on-demand worker-lite jobs,
  route smoke and operator demos.

**Do NOT use Railway for:**

- GPU model training, serious artifact storage, broker-adjacent
  production OMS, always-on heavy backtests, high-frequency inference,
  long-running data ingestion.

**Dependencies:** TASK-0101 (done) and TASK-0901.

---

### TASK-0903: Prepare AWS Production Control Plane Later

**Order:** 43

**Context:** Design the serious deployment path without moving too
early. Keep RunPod for GPUs. Keep OMS/risk boundaries inside the
trusted Fincept deployment.

**Recommended AWS shape:**

- ECS Fargate or App Runner for API/control services
- S3 for receipts, dossiers, artifacts
- ECR for images
- Secrets Manager
- CloudWatch
- VPC/private subnets
- ElastiCache Redis/Valkey
- managed Postgres or Timescale-compatible service
- ALB plus WAF

**Dependencies:** Phase 1 and Phase 3.

---

## Phase 10: Frontier Performance Modules

### Goal

Add cutting-edge accuracy improvements only after the core evidence
loop works.

### Why this comes late

Frontier modules are powerful but overfit-prone. The system needs
settlement, dossiers, tournament scoring, retirement, and shadow proof
before these modules can be trusted.

---

### TASK-1001: Mixture-of-Experts Model Router (Order 44)

> **Owner:** Builder 3 (GLM-5.2) — COMPLETED 2026-06-22 (commit `a88e8c2`). REVIEW.
> Files owned: `services/quant_foundry/src/quant_foundry/moe_router.py` (new),
> `services/quant_foundry/tests/test_moe_router.py` (new),
> `services/quant_foundry/src/quant_foundry/leaderboard_expanded.py` (modified — added settled_count).
> Creates a file-disjoint MoE model router. Imports from my
> `leaderboard_expanded.py` (TASK-0701), `tournament.py` (TASK-0404),
> `shadow_inference.py` (TASK-0601). Does NOT modify them (only backward-
> compatible addition of settled_count to leaderboard_expanded.py).
> Tests: 25/25 green; full suite 501/501 green (excl. Builder 2's in-progress file); ruff + mypy clean.

Learn which model to trust by regime, symbol, liquidity, volatility,
news type, horizon, feature availability, and recent calibration.
Start with rules from tournament evidence. Add learned router only
after enough settled data exists. Add abstain output.

### TASK-1002: Causal Market Memory Graph (Order 45)

Represent historical relationships between symbols, sectors, events,
regimes, and outcomes. Start with offline graph build. Use graph
features in research only. Add analog explanations.

### TASK-1003: Conformal Prediction Risk Gate (Order 46)

> **Owner:** Builder 3 (GLM-5.2) — COMPLETED 2026-06-22 (commit `e272b6e`). REVIEW.
> Files owned: `services/quant_foundry/src/quant_foundry/conformal_gate.py` (new),
> `services/quant_foundry/tests/test_conformal_gate.py` (new).
> Creates a file-disjoint conformal prediction risk gate. Imports from my
> `shadow_inference.py` (TASK-0601). Does NOT modify it.
> Tests: 26/26 green; full suite 527/527 green (excl. Builder 2's in-progress file); ruff + mypy clean.

Produce uncertainty intervals (q10/q50/q90) and abstain when the model
cannot make a reliable prediction. Feed uncertainty into tournament
and paper bridge.

### TASK-1004: Adversarial Drift Sentinel (Order 47)

> **Owner:** Builder 3 (GLM-5.2) — ADOPTED 2026-06-22. IN PROGRESS (TDD, file-disjoint).
> Files owned: `services/quant_foundry/src/quant_foundry/drift_sentinel.py` (new),
> `services/quant_foundry/tests/test_drift_sentinel.py` (new).
> Creates a file-disjoint adversarial drift sentinel. Imports from my
> `retirement.py` (TASK-0703), `leaderboard_expanded.py` (TASK-0701).
> Does NOT modify them.

Detect when the current market is hostile to the active or shadow
model set. Track feature distribution drift, calibration drift,
provider freshness drift, prediction disagreement spikes, live edge
decay. Emit recommendations: lower trust, shadow-only, retrain,
retire.

### TASK-1005: Alpha Genome Lab (Order 48)

Automate feature/model recipe generation while forcing every recipe
through leakage, walk-forward, shadow, and economic gates. Enforce
trial budgets. Kill underperforming sweeps early. Register only
evidence-backed candidates.

**Dependencies for all Phase 10:** Phase 5, Phase 6, and Phase 7.

---

## Phase 11: Limited Live Readiness

### Goal

Prepare for live influence only after long shadow and paper evidence.

### Hard gate

Do NOT implement limited live mode until ALL of these are true:

- runtime safety guards enforced everywhere [DONE]
- backtest path handling locked down [DONE]
- verification receipts exist [DONE]
- Quant Foundry is contract-tested
- settlement ledger is reliable (net-of-cost, point-in-time correct)
- dossier registry is reliable (full reproducibility set per model)
- tournament scoring is reliable (deflated/luck-adjusted, net of cost)
- the leakage/overfit sentinel is green on the promoted model family
- shadow inference has enough settled history
- paper bridge has run safely
- rollback pointer exists
- OMS and risk are unchanged and authoritative
- human approval workflow is working
- deployment environment has secure secrets and monitoring
- live provider/broker credentials are never available to RunPod

### TASK-1101: Limited Live Readiness Review (Order 49)

**Objective:** Produce a go/no-go report for limited live mode.

**What to do:**

- Summarize all evidence.
- List every remaining blocker.
- Prove rollback.
- Prove risk caps.
- Prove no RunPod broker credential access.
- Prove human approval.
- Require explicit operator decision.

**Acceptance criteria:**

- Report says either "not ready" with blockers or "ready for limited
  paper-to-live pilot" with exact caps.
- No code path can skip risk/OMS.
- Live mode remains disabled by default.

**Dependencies:** All previous phases.

**Risk:** VERY HIGH if rushed.

---

## Recommended execution order for the next work session

1. **TASK-0104** (optional) — Harden CI. Low risk, prevents guard
   regressions from merging.
2. **TASK-0201** — Dashboard route atlas. Documentation-only, makes
   the next targets obvious.
3. **TASK-0202** — System readiness center. Brings health visibility
   into the dashboard.
4. **TASK-0203** — On-demand module control. High value for the
   one-operator cost workflow. High risk if command execution is not
   allowlisted.
5. **TASK-0204** — Fetch timeouts. Prevents frozen dashboard.
6. **TASK-0205** — Provider evidence redaction. Makes data freshness
   reviewable safely.
7. **TASK-0301** — Quant Foundry package skeleton. First Quant Foundry
   code.
8. **TASK-0302** — Core schemas. The contract boundary.
9. **TASK-0303** — HMAC signatures + idempotency. Makes callbacks
   safe.
10. **TASK-0304** — Durable outbox + inbox. Prevents lost jobs and
    duplicate effects.
11. **TASK-0305** — Mock dispatcher. Proves the loop without RunPod.
12. **TASK-0306** — API routes in mock mode. Operator can create jobs.
13. **TASK-0401 → TASK-0406** — Evidence loop (settlement, shadow
    ledger, dossier, tournament, feature lake, and the leakage/overfit
    sentinel). The sentinel (TASK-0406) is non-negotiable before any
    high-throughput training: it is what stops the pipeline from
    promoting leaked or lucky models at scale.
14. **TASK-0501 → TASK-0504** — First RunPod training (only after the
    scoreboard *and* the sentinel exist).

---

## The principle behind this ordering

From the plan's final section:

> The fastest path to a powerful Fincept Quant Foundry is not to start
> with the biggest GPU cluster. It is to make every future prediction
> become evidence, every artifact become reproducible, every promotion
> require proof, and every optional module turn off when it is not
> needed.

Every phase before Phase 5 exists to make Phase 5 safe. Every phase
before Phase 7 exists to make Phase 7's promotion gate meaningful.
Phase 11 (limited live) exists only to prove that all of the above
worked before any real money is at stake.
