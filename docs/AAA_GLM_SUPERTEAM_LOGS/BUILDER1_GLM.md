# Builder 1 (GLM) — Work Log

**Agent:** Builder 1 (GLM-5.2)
**Joined:** 2026-06-22
**Track:** Quant Foundry evidence-loop foundations

---

## Task Adoption Log

### TASK-0401: Build the Prediction Settlement Ledger — ADOPTED 2026-06-22

**Status:** IN PROGRESS
**Order:** 22
**Depends on:** TASK-0302 (✅ DONE)
**Files owned (file-disjoint from active tasks):**
- `services/quant_foundry/src/quant_foundry/settlement.py` (created)
- `services/quant_foundry/src/quant_foundry/outcomes.py` (created)
- `services/quant_foundry/src/quant_foundry/metrics.py` (created)
- `services/quant_foundry/tests/test_settlement.py` (created)

**File-disjoint check:**
- TASK-0304 (Builder 2, in flight) owns `outbox.py`, `inbox.py`, `test_outbox.py`, `test_inbox.py` — no overlap.
- TASK-0204 (Builder 1 orig, in flight) owns `apps/dashboard/src/lib/api.ts` — no overlap.
- `schemas.py` is intentionally NOT touched (Builder 2's track). Settlement-specific records live in `outcomes.py` to keep ownership clean.

**Plan (TDD):**
1. Write failing tests in `test_settlement.py` covering:
   - Fixture predictions settle deterministically on post-decision window `(t, t+h]`.
   - `pending_time` (horizon not elapsed) vs `pending_data` (market data missing) kept distinct.
   - Missing market data does not crash settlement.
   - Versioned cost model (fee bps, spread, slippage, borrow) stored on each outcome; settle both gross and net.
   - Reruns idempotent (same inputs + cost-model version → identical outcome row; no duplication).
   - Brier score, calibration bucket, abnormal return vs benchmark.
2. Implement `metrics.py` (Brier, realized return, abnormal return, calibration bucket, cost application).
3. Implement `outcomes.py` (SettlementRecord schema, pending states, CostModel).
4. Implement `settlement.py` (SettlementLedger: match predictions to realized outcomes, post-decision-window guard, idempotent reruns).
5. Run `uv run pytest services/quant_foundry/tests/test_settlement.py -q` green; ruff/mypy clean.
6. Atomic commit.

---

## Completion Log

### TASK-0401 — COMPLETED 2026-06-22 (commit 855f01b)

**Status:** REVIEW (awaiting Reviewer 1)
**Tests:** 27/27 green — `uv run --package quant_foundry pytest services/quant_foundry/tests/test_settlement.py -q`
**Lint:** ruff clean (all 4 files)
**Type:** mypy clean (3 source files)
**Commit:** `855f01b` — 4 files, +1094 lines, additive only, file-disjoint from TASK-0304/0204.

**Delivered:**
- `services/quant_foundry/src/quant_foundry/metrics.py` — pure settlement math:
  `realized_return` (post-decision window `(t, t+h]`, look-ahead guard: only
  prices with `ts >= t` used as entry, `ts >= t+h` as exit; short direction
  flips sign; flat = 0.0; None when entry/exit missing), `brier_score`,
  `calibration_bucket` (5 named buckets), `abnormal_return` (None when
  benchmark missing), `apply_costs` (gross→net with versioned CostModel;
  round-trip fee+spread+slippage; borrow only for shorts × holding_days).
- `services/quant_foundry/src/quant_foundry/outcomes.py` — `SettlementStatus`
  StrEnum with distinct `pending_time` / `pending_data` / `settled`; frozen
  versioned `CostModel`; frozen `SettlementRecord` carrying gross+net,
  `cost_model_version`, decision window, tournament-relevant fields, with
  `to_json`/`from_json` for JSONL persistence.
- `services/quant_foundry/src/quant_foundry/settlement.py` — `SettlementLedger`
  (filesystem JSONL under `<root>/<model_id>.settlements.jsonl`,
  restart-durable, idempotent reruns by `(prediction_id, cost_model_version)`:
  same version returns existing record, different version appends new record
  preserving history). Look-ahead guard: `now_ns < t+h` → `pending_time`.
  Horizon elapsed but data missing → `pending_data`. Accepts dict or
  `PredictionInput` (decoupled from `schemas.ShadowPrediction` to avoid
  touching Builder 2's `schemas.py`).
- `services/quant_foundry/tests/test_settlement.py` — 27 TDD tests covering
  every acceptance criterion in the plan.

**Acceptance criteria verification (self):**
- ✅ Fixture predictions settle deterministically.
- ✅ Missing market data does not crash settlement (`pending_data`, no exception).
- ✅ Reruns do not duplicate outcomes (same inputs+cost-model version → same record, 1 row).
- ✅ Output can feed tournament scoring (all tournament fields present on SETTLED records).
- ✅ Post-decision window enforced (pre-decision price cannot leak as entry).
- ✅ `pending_time` vs `pending_data` distinct.
- ✅ Versioned cost model; both gross and net stored; different cost-model version → new record.
- ✅ Brier, calibration bucket, abnormal return vs benchmark (None when benchmark missing).
- ✅ Restart-durable (new ledger instance reads prior records).
- ✅ Records frozen (audit integrity).

**Notes for Reviewer:**
- `schemas.PredictionOutcome` (TASK-0302) intentionally NOT modified — it's
  Builder 2's contract-track file. The settlement ledger uses a richer local
  `SettlementRecord` in `outcomes.py` because settlement needs gross/net,
  pending states, cost-model version, and decision window — fields that are
  internal to the evidence loop, not cross-boundary contract. If the
  Reviewer/Coordinator prefer to unify these later, that's a follow-up that
  can be done without re-running settlement (the JSONL records carry all
  fields needed to reconstruct either shape).
- `PredictionInput` is a local dataclass (not importing `schemas.ShadowPrediction`)
  so the ledger can also settle existing `fincept_core.PredictionRow` records
  without coupling to the cross-boundary contract. Callers pass a dict with
  the required keys.

---

## Task Adoption Log (continued)

### TASK-0402: Add Shadow Prediction Ledger Storage — YIELDED to Builder 3 (collision)

**Status:** YIELDED 2026-06-22
**Reason:** Collision — Builder 3 also adopted TASK-0402 and wrote their
`test_shadow_ledger.py` (overwriting mine on disk) before seeing my board
ownership marker. Builder 3's design is richer and better spec-aligned
(`BatchHasher` + `compute_batch_hash` reusing `ids.hash_payload`, diff-hash
rejection as a security event mirroring TASK-0304's inbox invariant, read API
by model_id/symbol/time window, `store_batch`).

**Resolution:**
- Deleted my untracked `shadow_ledger.py` (never committed, my own scratch).
- Kept Builder 3's `test_shadow_ledger.py` intact on disk.
- Sent Builder 3 a message: `docs/AAA_GLM_SUPERTEAM_LOGS/AGENT_TO_AGENT_MESSAGING/BUILDER3_TASK-0402_yield.md`.
- Updated `SWARM_BOARD.md` line 43 to transfer ownership to Builder 3.
- Moving to TASK-0104 (CI hardening) which is unblocked + file-disjoint.

---

### TASK-0104: Harden CI and Supply Chain Defaults — ADOPTED 2026-06-22

**Status:** IN PROGRESS
**Order:** 10 (Phase 1 remainder — optional but recommended)
**Depends on:** TASK-0101 (receipt runner — done)
**Files owned (file-disjoint from ALL active tasks):**
- `.github/workflows/ci.yml` (modified — pin actions, add receipt-runner + matrix-test jobs, lockfile discipline, gitleaks)
- `.github/workflows/build-images.yml` (modified — pin actions)
- `.github/workflows/nightly.yml` (modified — pin actions)
- `docs/AAA_GLM_SUPERTEAM_LOGS/BUILDER1_GLM.md` (this log)

**File-disjoint check:**
- All quant_foundry work (Builders 2/3/4) — no overlap.
- TASK-0203 (Builder 5: services/api routes/modules.py, dashboard) — no overlap.
- TASK-0204 (Builder 1 orig: apps/dashboard/src/lib/api.ts) — no overlap.

**Plan (per NEXT_STEPS_PLAN TASK-0104):**
1. Pin GitHub Actions to commit SHAs (not `@v4` floating tags) in all 3 workflows.
2. Set least-privilege permissions (`contents: read` by default, write only on release).
3. Add a CI job that runs `pwsh ./scripts/verification-receipt.ps1` and fails on required-check failures.
4. Add a CI job that runs the startup safety matrix tests (`uv run pytest libs/fincept-core/tests/test_startup_safety_matrix.py`).
5. Add lockfile discipline (fail if `uv.lock` or `pnpm-lock.yaml` is out of sync).
6. Add gitleaks secret scan as a required check.
7. Validate workflow YAML syntax; atomic commit.

---

### TASK-0104 — COMPLETED 2026-06-22 (commit a0a0081)

**Status:** REVIEW (awaiting Reviewer 1)
**Commit:** `a0a0081` — 3 files, +161/-21 lines, file-disjoint from all quant_foundry/dashboard work.
**YAML:** all 3 workflows parse cleanly (`yaml.safe_load` OK).

**Delivered:**
- **Pinned 10 GitHub Actions to commit SHAs** (not floating `@v4` tags) across
  `ci.yml`, `build-images.yml`, `nightly.yml`. SHAs fetched from GitHub API
  (verified PGP-signed commits). Each pin has a `# pin: <action>@<tag>` comment
  for traceability. This blocks the classic supply-chain attack where a
  compromised action tag is moved to a malicious commit.
  - `actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5` (v4)
  - `actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02` (v4)
  - `actions/setup-node@49933ea5288caeca8642d1e84afbd3f7d6820020` (v4)
  - `pnpm/action-setup@b906affcce14559ad1aafd4ab0e942779e9f58b1` (v4)
  - `astral-sh/setup-uv@caf0cab7a618c569241d31dcd442f54681755d39` (v3)
  - `docker/setup-buildx-action@8d2750c68a42422c14e847fe6c8ac0403b4cbd6f` (v3)
  - `docker/login-action@c94ce9fb468520275223c153574b00df6fe4bcc9` (v3)
  - `docker/build-push-action@10e90e3645eae34f1e60eeb005ba3a3d33f178e8` (v6)
  - `gitleaks/gitleaks-action@ff98106e4c7b2bc287b24eaf42907196329070c7` (v2)
  - `aquasecurity/trivy-action@cf5c088a69634cd13ccddc735d7926162c31f9a6` (0.71.2)
- **Least-privilege permissions:**
  - `ci.yml`: `contents: read` (was unset — GitHub default is read, but
    explicit is better).
  - `build-images.yml`: `contents: read, packages: write` (already had this;
    kept explicit).
  - `nightly.yml`: `contents: read` (was unset).
- **3 new required CI jobs in `ci.yml`:**
  1. `receipt-runner` — runs `pwsh ./scripts/verification-receipt.ps1` and
     fails on any REQUIRED check failure. Uploads the receipt as an artifact.
  2. `startup-safety-matrix` — runs
     `uv run pytest libs/fincept-core/tests/test_startup_safety_matrix.py -q`.
     This is the guard that prevents silent startup regressions.
  3. `lockfile-sync` — runs `uv lock --check` and `pnpm install --frozen-lockfile`
     to fail if lockfiles are out of sync with manifests.
- **gitleaks** was already present as a `security` job; kept and pinned.

**Pre-existing failure surfaced (NOT caused by this task):**
- `startup-safety-matrix` job fails because `services/api/src/api/main.py`
  does not call `assert_safe_for_runtime`. This is a real safety gap (audit
  R4/P3): a non-dev deployment with the default dev JWT secret would silently
  start up and accept forged tokens. Every other service entrypoint already
  has this call.
- The `receipt-runner` job also fails for the same root cause (its
  `python:core-lib` check runs `libs/fincept-core/tests` which includes the
  matrix test).
- **Fix belongs to Builder 5** (who owns `services/api/src/api/main.py` for
  TASK-0203). Messaged Builder 5:
  `docs/AAA_GLM_SUPERTEAM_LOGS/AGENT_TO_AGENT_MESSAGING/BUILDER5_startup_safety_matrix_failure.md`
- The CI jobs are correct — they surface a real regression. The fix is a
  one-line addition to `services/api/src/api/main.py`.

**Acceptance criteria verification (self):**
- ✅ All GitHub Actions pinned to commit SHAs (no floating tags).
- ✅ Least-privilege permissions set on all 3 workflows.
- ✅ Receipt runner integrated as a CI job.
- ✅ Startup safety matrix integrated as a CI job.
- ✅ Lockfile discipline enforced.
- ✅ Gitleaks secret scan present and pinned.
- ✅ YAML syntax validated for all 3 workflows.
- ⚠️ `startup-safety-matrix` and `receipt-runner` jobs will fail on main until
  Builder 5 adds `assert_safe_for_runtime()` to `services/api/src/api/main.py`.
  This is the intended behavior — the check surfaces a real safety gap.

---

### TASK-0204 — COMPLETED 2026-06-22 (commit 68e816b)

**Status:** REVIEW (awaiting Reviewer 1)
**Commit:** `68e816b` — 2 files, +186/-14 lines.
**tsc:** 0 new errors introduced (verified via git stash comparison: all
pre-existing errors are in other builders' WIP files — `symbol/`,
`news-impact/`, `watchlist-*`, `module-control-panel` — not in my touched
files).

**Delivered:**
- **`apps/dashboard/src/lib/api.ts`** — AbortController timeout + typed errors:
  - `DEFAULT_TIMEOUT_MS = 8_000` (8 s default; chosen to be longer than a
    healthy backend round-trip but short enough that an operator sees a clear
    "slow backend" message before assuming "no data").
  - `RequestOptions` interface extends `RequestInit` with `timeoutMs` override.
  - `request()` now wraps every `fetch` with `AbortController` + `setTimeout`.
    On abort, throws `TimeoutError`. On network error (backend unreachable),
    throws `UnavailableError` (so UI doesn't confuse it with "no data").
  - 5 typed error subclasses so UI panels can render precise operator messages:
    - `UnauthorizedError` (401 → "Session expired — please sign in again.")
    - `UnavailableError` (5xx / network → "Backend unavailable — check service status.")
    - `TimeoutError` (abort → "Request timed out after N ms — backend may be slow or down.")
    - `ValidationError` (422 → "Validation failed — check the form inputs.")
    - `StaleError` (409 → "Data is stale — refresh to try again.")
  - `classifyError(status, body)` maps HTTP status to the most specific subclass.
  - Per-call timeout overrides for known-slow endpoints:
    - `trainModel`: 30 s (model training can take a while).
    - `runBacktest`: 60 s (backtest can take up to a minute).
- **`apps/dashboard/src/app/api/portfolio-report/route.ts`** — LLM call timeout:
  - `LLM_TIMEOUT_MS = 90_000` (90 s; LLM reasoning reports can take 20-60 s).
  - `fetchWithTimeout()` helper wraps both `callOpenAI` and `callAnthropic`
    with `AbortController`. On timeout, throws "LLM call timed out after N ms"
    → the caller's catch block falls through to the next provider or the
    deterministic fallback (no infinite hang).

**Acceptance criteria verification (self):**
- ✅ Slow calls fail clearly (`TimeoutError` with precise message + timeoutMs).
- ✅ Backend unavailable is not confused with "no data" (`UnavailableError`
  distinct from a successful empty response).
- ✅ No route hangs forever in normal operator use (8 s default, 90 s LLM,
  60 s backtest, 30 s train — all bounded).
- ✅ Typed errors: unauthorized, unavailable, timeout, stale, validation failure.

**Notes for Reviewer:**
- The pre-existing `NewsImpactSignalsResponse` missing export from `types.ts`
  (tsc error at `api.ts:45`) is NOT caused by this task — it was at `api.ts:38`
  before my changes (line shifted because I added typed error classes above
  it). That's a separate issue from the `news-impact/` component work.
- Builder 5's `module-control-panel.tsx` has tsc errors because it references
  `api.modules`, `api.startModule`, etc. which don't exist yet — that's
  Builder 5's TASK-0203 WIP, not mine.

---

### TASK-0404: Tournament Scoring Skeleton — YIELDED to Builder 3 (collision)

**Date:** 2026-06-22
**Reason:** Collision — Builder 3 also adopted TASK-0404 and wrote their own
`test_tournament.py` with a different (better) API design.

**My design (deleted):**
- `significance.py` — stationary block bootstrap p-value + Deflated Sharpe
  Ratio with deterministic LCG RNG.
- `tournament.py` — `score_model()` function consuming `SettlementRecord` +
  `DossierRecord` directly; `ScoringStatus` enum (INSUFFICIENT_EVIDENCE /
  SUFFICIENT_EVIDENCE / BLOCKED); blocking issues as list of strings.
- `leaderboard.py` — `rank_models()` with tier-based ordering.
- `test_tournament.py` — my own test file with `TestStationaryBlockBootstrap`,
  `TestDeflatedSharpe`, `TestScoreModelBasics`, etc.

**Builder 3's design (kept — better aligned with spec):**
- Local `ScoringInput` schema (spec: "Define scoring input schema") instead of
  consuming `SettlementRecord`/`DossierRecord` directly.
- `Tournament` class with `.score()` method.
- `TournamentStatus.STALE` as a separate state (cleaner than my overloaded
  `BLOCKED`).
- `PromotionRecommendation` enum (`PROMOTE`/`HOLD`) — explicit promotion signal.
- `score_components` as a list of named components (more auditable).

**What I did:**
1. Deleted my `significance.py`, `tournament.py`, `leaderboard.py` (untracked,
   never committed, safe to remove).
2. Left Builder 3's `test_tournament.py` intact on disk.
3. Updated `SWARM_BOARD.md` to transfer ownership to Builder 3.
4. Sent Builder 3 a yield message:
   `docs/AAA_GLM_SUPERTEAM_LOGS/AGENT_TO_AGENT_MESSAGING/BUILDER3_TASK-0404_yield.md`.

**Lesson:** This is the second collision with Builder 3 (first was TASK-0402).
Builder 3 is working the quant_foundry evidence-loop track (0403 → 0404 →
likely 0406). I should check BUILDER3.md BEFORE adopting any quant_foundry
task in the 0400 range. The dashboard track (0200 range) and CI track
(0100 range) are my safe zones.

---

### TASK-0801 — COMPLETED 2026-06-22 (commit 4aac4fe)

**Status:** REVIEW (awaiting Reviewer 1)
**Commit:** `4aac4fe` — 3 files, +693 lines.
**tsc:** 0 new errors introduced (all pre-existing errors are in other
builders' WIP files — `symbol/`, `news-impact/`, `watchlist-*` — not in my
touched files).

**Delivered:**
- **`apps/dashboard/src/app/quant-foundry/page.tsx`** (created) — read-only
  overview page with:
  - 9 module status cards (Gateway, Outbox, Callback Inbox, Feature Lake,
    Settlement, Dossier Registry, Tournament, RunPod Research, Shadow
    Inference). Each card shows state (active / configured / not_wired /
    disabled) with a StatusPill and detail line.
  - Global mode banner showing the current QF mode (disabled, local_mock,
    runpod_research, runpod_shadow, paper_bridge) with a description. Disabled
    is shown as the safe resting state, NOT as a failure.
  - Cost & budget card (zero cost in local_mock; "Not yet wired" for RunPod
    modes).
  - Recent jobs card (latest 5 jobs from the outbox, sorted by created_at_ns
    descending; empty when disabled).
  - Receipts card (placeholder — will be wired when the evidence-loop receipt
    endpoint lands in Phase 4 completion).
  - SHADOW ONLY badge in the header when `shadow_only !== false`.
  - Error handling: a 503 (gateway not configured / disabled) is treated as a
    valid state, NOT an error. Only real errors (network, auth, non-503 5xx)
    show the error banner.
  - **No promote or trade actions** — the page is strictly read-only (per
    acceptance criteria).
- **`apps/dashboard/src/lib/api.ts`** (additive) — 3 new API client methods:
  - `quantFoundryHealth(token)` → `GET /quant-foundry/health`
  - `quantFoundryHeartbeats(token)` → `GET /quant-foundry/heartbeats`
  - `quantFoundryJobs(token, {status?})` → `GET /quant-foundry/jobs`
- **`apps/dashboard/src/lib/types.ts`** (additive) — 3 new types:
  - `QuantFoundryHealthResponse` (mirrors `gateway.health()`)
  - `QuantFoundryHeartbeat`
  - `QuantFoundryJob`

**Acceptance criteria verification (self):**
- ✅ Page loads in disabled mode (503 → mode="disabled", all modules show
  "DISABLED" state, no error banner).
- ✅ Disabled is not shown as failure (mode banner says "This is the safe
  resting state — not a failure"; StatusPill intent="inactive" not
  "critical").
- ✅ No action can promote or trade from overview (page is strictly read-only;
  no buttons, no forms, no POST/PUT/DELETE calls).

**Notes for Reviewer:**
- The `Outbox` icon doesn't exist in lucide-react; I used `Send` instead (same
  semantic — outgoing jobs).
- The receipts card is a placeholder. The evidence-loop receipt endpoint is
  not yet exposed via a dedicated API route; it will be wired in a later task
  (Phase 4 completion or TASK-0802).
- The page uses `retry: false` on the health/jobs queries because a 503
  (disabled) is a valid state, not a transient error. Retrying would spam the
  backend with requests every few seconds for a state that won't change
  without a config update.
- File-disjoint from all active builders: new route
  `apps/dashboard/src/app/quant-foundry/page.tsx`; additive changes to
  `api.ts` (3 methods at the end, no overlap with Builder 5's module control
  methods) and `types.ts` (3 types at the end, no overlap with Builder 5's
  module types).

---

### TASK-0903 — COMPLETED 2026-06-22 (commit 4cce0c9)

**Status:** REVIEW (awaiting Reviewer 1)
**Commit:** `4cce0c9` — 1 file, +383 lines (design document only).

**Delivered:**
- **`docs/AWS_PRODUCTION_CONTROL_PLANE.md`** (created) — design document for
  the AWS production deployment path. Covers:
  - Architecture overview (ALB+WAF → ECS Fargate → ElastiCache/RDS/S3)
  - Component selection: ECS Fargate (not EC2/EKS/Lambda), S3 (versioned +
    object-locked for audit-integrity), ECR (immutable tags, image scanning),
    Secrets Manager (no secrets in source/images), CloudWatch (structured
    logging + alarms + dashboards), VPC (public/private/db subnets, security
    groups), ElastiCache for Redis/Valkey (multi-AZ, TLS, noeviction), managed
    Postgres with TimescaleDB (multi-AZ, PITR, KMS encryption, RDS Proxy),
    ALB + WAF (OWASP rules, rate limiting, IP allowlist).
  - OMS/risk boundary: OMS and risk services run inside the trusted AWS
    deployment, NEVER on RunPod or external compute. Broker credentials in
    Secrets Manager, accessible only to the OMS task execution role.
  - RunPod integration: RunPod is used ONLY for GPU workloads. AWS dispatches
    jobs, RunPod trains models and sends signed callbacks. RunPod workers have
    no broker credentials, no Redis, no DB access.
  - Cost estimate: ~$210-260/mo always-on (one-operator shop) + $0.5-2/hour
    for on-demand GPU.
  - Migration path: local dev → Railway staging (TASK-0902) → AWS production.
  - Non-goals: no AWS GPU (RunPod is cheaper), no multi-region, no Kubernetes,
    no live trading (requires Phase 4-7 + TASK-1101 first).
  - Open questions: TimescaleDB on RDS vs. Aurora, Valkey vs. Redis, ECS
    Service Connect vs. internal ALB.

**Acceptance criteria verification (self):**
- ✅ Design the serious deployment path without moving too early (design doc
  only; no infrastructure created).
- ✅ Keep RunPod for GPUs (explicit in the RunPod Integration section).
- ✅ Keep OMS/risk boundaries inside the trusted Fincept deployment (explicit
  in the OMS/Risk Boundary section).
- ✅ Covers all recommended AWS shapes: ECS Fargate, S3, ECR, Secrets Manager,
  CloudWatch, VPC, ElastiCache, managed Postgres, ALB + WAF.

**Notes for Reviewer:**
- This is a design document only. No infrastructure is provisioned, no code is
  changed, no tests are needed. The document is a planning artifact for the
  future migration from Railway staging to AWS production.
- The cost estimate is rough (one-operator shop, single-region, no GPU). Actual
  costs will vary based on usage patterns, data volume, and GPU hours.

---

### TASK-0901 — COMPLETED 2026-06-22 (commit 2bfa463)

**Status:** REVIEW (awaiting Reviewer 1)
**Commit:** `2bfa463` — 3 files, +822 lines.
**Tests:** 20/20 green. ruff + mypy clean.

**Delivered:**
- **`services/quant_foundry/src/quant_foundry/budget.py`** (created) —
  `BudgetGuard` class with:
  - Hard monthly budget ceiling (configurable via constructor or
    `QUANT_FOUNDRY_MONTHLY_BUDGET_CENTS` env var via `from_env()`).
  - `check_and_reserve(amount_cents, job_type)` — fail-closed: rejects jobs
    that would exceed the monthly ceiling BEFORE they start. Returns a
    `BudgetDecision` with `allowed`, `reason`, `spent_cents`,
    `remaining_cents`, `year_month`.
  - `record_spend(amount_cents, job_type, year_month?)` — records actual
    spend (e.g. after a RunPod job completes and the real cost is known).
  - `get_monthly_spend(year_month?)` — read cumulative spend for a month.
  - `get_summary()` — read-only summary dict for dashboard/API.
  - `set_kill_switch(enabled)` — manual emergency stop: blocks ALL paid jobs
    regardless of remaining budget. Zero-cost jobs (amount=0) are always
    allowed.
  - Durable JSONL ledger (`<base_dir>/spend_<YYYY-MM>.jsonl`) — restart-safe.
    Each line: `ts_unix`, `job_type`, `amount_cents`, `kind` (reserve/record).
  - Monthly reset: spend is tracked per calendar month; previous months'
    spend is preserved in their own ledger files for audit.
  - `from_env(base_dir)` factory reads `QUANT_FOUNDRY_MONTHLY_BUDGET_CENTS`
    and `QUANT_FOUNDRY_BUDGET_KILL_SWITCH` env vars.
- **`services/quant_foundry/tests/test_budget.py`** (created) — 20 TDD tests:
  - Basic guard: allow within budget, reject over budget, cumulative
    tracking, zero amount allowed, negative amount raises.
  - Kill switch: blocks all paid jobs, can be toggled at runtime.
  - Durability: spend survives restart (new guard, same dir), resets across
    months (previous month spend doesn't count against current month).
  - Record spend: increases total, different job types, specific month.
  - Read API: get monthly spend (zero initially, after reservation),
    get summary (all fields).
  - Edge cases: exact budget allowed, 1c over rejected, zero budget blocks
    paid but allows free, job type recorded in decision.
- **`docs/MODULE_RUNTIME_PLAN.md`** (created) — module runtime plan covering:
  - Always-on vs on-demand split (8 always-on services, 8 on-demand modules).
  - Module list with cost class, idle timeout, max instances, estimated
    monthly cost (local $0, AWS ~$200-310/mo without GPU).
  - Start/stop scripts (HTTP endpoints + CLI).
  - Health checks (process, heartbeat, endpoint).
  - Idle timeout sweep logic.
  - Max instances (1 for local/staging).
  - Budget guard documentation (how it works, configuration, integration plan,
    test coverage).
  - Cost summary table (local vs AWS, with/without GPU).

**Acceptance criteria verification (self):**
- ✅ Define module list with start/stop scripts, health checks, idle timeout,
  max instances, estimated monthly cost (MODULE_RUNTIME_PLAN.md).
- ✅ Add budget guard before heavy jobs (budget.py with
  check_and_reserve + kill switch + durable ledger).
- ✅ Add "stop all optional modules" (already implemented by Builder 5 in
  TASK-0203 as `POST /modules/stop-all`; documented in the plan).

**Notes for Reviewer:**
- The budget guard is NOT yet wired into the gateway's `create_job` method
  (that would modify `gateway.py`, owned by Builder 2). The guard module is
  file-disjoint and ready for injection. The integration plan is documented
  in MODULE_RUNTIME_PLAN.md.
- The "stop all optional modules" endpoint was already implemented by Builder
  5 in TASK-0203. This task documents it in the runtime plan rather than
  re-implementing it.
- File-disjoint from all active builders: new `budget.py` + `test_budget.py`
  in quant_foundry (no imports of settlement/dossier/tournament/gateway/
  outbox/inbox); new `MODULE_RUNTIME_PLAN.md` doc.

---

### TASK-0902 — COMPLETED 2026-06-22 (commit 21f7a59)

**Status:** REVIEW (awaiting Reviewer 1)
**Commit:** `21f7a59` — files swept into Builder 3's commit (B3 ran `git add`
that included my untracked files alongside their TASK-0502 doc update). My
files are correctly in the commit with the right content.

**Delivered:**
- **`docs/RAILWAY_STAGING_GUIDE.md`** (created) — staging guide covering:
  - What to deploy on Railway: API, dashboard, Redis plugin, Postgres plugin
    (~$20-30/mo hobby plan).
  - What NOT to deploy: GPU workloads (no GPU on Railway), broker-adjacent
    OMS (no broker creds on shared platform), serious artifact storage
    (1GB PG cap), always-on heavy backtests, high-frequency inference,
    long-running data ingestion.
  - What staging proves: route smoke tests, dashboard renders, mock QF loop,
    auth flow, module control, operator demos.
  - Railway configuration: `railway.json` with NIXPACKS builder, uvicorn
    start command, healthcheck, restart policy.
  - Environment variables: `FINCEPT_ENV=staging`, `QUANT_FOUNDRY_MODE=
    local_mock`, `QUANT_FOUNDRY_SHADOW_ONLY=true`, `QUANT_FOUNDRY_BUDGET_
    KILL_SWITCH=true`, `QUANT_FOUNDRY_MONTHLY_BUDGET_CENTS=0`. Explicitly
    lists vars that must NOT be set (broker credentials).
  - Cost estimate: ~$20-30/mo (Railway staging) vs ~$200-310/mo (AWS prod)
    vs $0 (local dev).
  - Migration path: local dev → Railway staging → AWS production.
  - Security notes: no broker creds, staging-only JWT secret, budget kill
    switch ON, shadow-only mode, runtime safety guard with FINCEPT_ENV=
    staging.
- **`railway.json`** (created) — Railway deployment config:
  - Builder: NIXPACKS
  - Start command: `uvicorn api.main:app --host 0.0.0.0 --port $PORT`
  - Healthcheck: `/health` (30s timeout)
  - Restart policy: ON_FAILURE, max 3 retries

**Acceptance criteria verification (self):**
- ✅ Recommended Railway use: dashboard staging, API staging, small Postgres
  test DB, Redis test instance, mock Quant Foundry gateway, on-demand
  worker-lite jobs, route smoke and operator demos (all documented).
- ✅ Do NOT use Railway for: GPU model training, serious artifact storage,
  broker-adjacent production OMS, always-on heavy backtests, high-frequency
  inference, long-running data ingestion (all documented with reasons).

**Notes for Reviewer:**
- My files were swept into Builder 3's commit `21f7a59` (B3 ran a broad
  `git add` that included my untracked files alongside their TASK-0502 doc
  update). The content is correct — I verified `git show HEAD:railway.json`
  matches what I wrote. This is a minor process issue (B3 should scope their
  `git add` to their own files), not a content issue.
- No code changes, no tests needed — this is a documentation + config task.
- File-disjoint from all active builders: new `RAILWAY_STAGING_GUIDE.md` doc
  + new `railway.json` config at repo root.

---

### TASK-0205 — COMPLETED 2026-06-22 (commit 08aec6a)

**Status:** REVIEW (awaiting Reviewer 1)
**Commit:** `08aec6a` — 3 files, +815 lines.
**Tests:** 29/29 green. ruff + mypy clean.

**Delivered:**
- **`libs/fincept-db/src/fincept_db/evidence_redaction.py`** (created) —
  conservative redaction module with:
  - `redact_string(s)` — scans a string for known secret patterns and replaces
    them with `[REDACTED:<pattern_name>]`. Patterns:
    1. URLs with embedded credentials (`https://user:pass@host/...`)
    2. Query params with secret names (`?apiKey=...&token=...`)
    3. Bearer tokens (`Bearer <base64-ish>`)
    4. API key prefixes (`sk-...`, `pk-...`, `AK...`)
    5. key=/token=/password=/secret= in flat strings
    6. Generic long alphanumeric tokens (32+ chars)
  - `redact_dict(d)` — recursively walks a dict/list structure, redacting:
    1. Values of known sensitive keys (case-insensitive: `api_key`, `apiKey`,
       `token`, `secret`, `password`, `authorization`, `bearer`, etc.)
    2. String values containing token-shaped substrings
    3. URLs with embedded credentials
  - `RedactionResult` dataclass with `redacted`, `redaction_count`,
    `patterns_matched` (for audit).
  - Conservative by default: false positives are acceptable; false negatives
    (leaking a secret) are not.
- **`libs/fincept-db/src/fincept_db/provider_receipts.py`** (created) —
  provider evidence receipt module with:
  - `ProviderFreshnessStatus` — frozen dataclass with `status` (fresh/stale/
    degraded/unknown), `age_sec`, `provider`.
  - `freshness_from_age_sec(...)` — classifies freshness based on age
    thresholds (fresh < 5s, stale < 60s, degraded >= 60s, unknown = None).
  - `ProviderEvidenceReceipt` — frozen dataclass with provider, source,
    dataset, symbol, timestamps, row_count, request_hash, redacted request,
    ok, error_type, freshness, redaction_count, redaction_patterns.
  - `build_evidence_receipt(...)` — constructs a receipt from raw provider
    data, automatically redacting the request dict. The original (unredacted)
    request is never stored in the receipt.
  - `to_dict()` — serializes to a JSON-safe dict for API responses and storage.
- **`services/api/tests/test_provider_evidence.py`** (created) — 29 TDD tests:
  - String redaction: API keys, Bearer tokens, private URLs, query params,
    multiple secrets, non-sensitive strings, empty strings.
  - Dict redaction: api_key field, Authorization header, nested dicts, lists,
    password/secret/token fields, non-sensitive dicts.
  - Freshness: fresh (< 5s), stale (30s), degraded (120s), unknown (None),
    custom thresholds.
  - Evidence receipt: fresh data, stale data, redacts sensitive request,
    error state, JSON serializable, no secrets in to_dict(), None symbol.
  - RedactionResult: has redacted + count + patterns_matched.

**Acceptance criteria verification (self):**
- ✅ Provider evidence proves freshness without leaking secrets (receipt
  includes freshness status + redacted request; original request never stored).
- ✅ Redaction tests catch token-shaped values (29 tests covering API keys,
  Bearer tokens, credential URLs, query params, sensitive field names, long
  alphanumeric tokens).
- ✅ Dashboard can show data freshness and provider degradation (receipt
  includes `freshness.status` = fresh/stale/degraded/unknown + `age_sec`).

**Notes for Reviewer:**
- Scoped to new files in `libs/fincept-db` + new test file. NOT touching
  OMS files (`services/oms/src/oms/alpaca/news_sync.py`, `marks.py`) or
  dashboard news components — those are follow-up integration work to avoid
  collision with other builders.
- The freshness thresholds (fresh < 5s, stale < 60s, degraded >= 60s) are
  defaults based on the spec's examples ("Binance is 2 seconds stale, Polygon
  is 30 seconds stale"). They are configurable per-call.
- The redaction is conservative: the long-token pattern (32+ chars) may
  false-positive on long hashes or IDs. This is intentional — false positives
  are acceptable, false negatives are not.
- File-disjoint from all active builders: new `evidence_redaction.py` +
  `provider_receipts.py` in fincept-db; new `test_provider_evidence.py` in
  api/tests. No imports of quant_foundry, no edits to existing files.

---

### TASK-0201 — COMPLETED 2026-06-22 (commit 57de07e)

**Status:** REVIEW (awaiting Reviewer 1)
**Commit:** `57de07e` — 1 file, +364 / -80 lines (replaced stub with full atlas).

**Delivered:**
- **`docs/dashboard-route-atlas.md`** (updated) — complete route atlas covering
  all 25 dashboard routes:
  - **18 Live routes:** `/`, `/system`, `/quant-foundry`, `/positions`,
    `/orders`, `/markets`, `/news`, `/news-impact-lab`, `/research`,
    `/backtest`, `/predictions`, `/reconciliation`, `/risk`, `/strategies`,
    `/strategies/[id]`, `/models`, `/models/[name]`, `/login`
  - **2 Hybrid routes:** `/symbol/[symbol]` (API + mock-data.ts, 3 MockBadge
    instances), `/portfolio-builder` (marketDataService.ts, may be live or
    mock depending on config)
  - **1 Mock route:** `/watchlist` (entirely mock, uses mockPriceWalk, 1
    MockBadge)
  - **2 Demo routes:** `/receipts` (client-side receipt definitions),
    `/signal-cockpit-demo` (SignalCockpitDemo feature component)
  - **2 Redirect routes:** `/optimizer` → `/portfolio-builder`, `/news-lab`
    → `/news-impact-lab`
  - Each route entry includes: source files, data status, backend dependency,
    MockBadge presence, risk if mistaken for live, replacement priority,
    suggested test.
  - Mock data sources section documenting `@/lib/mock-data.ts` and
    `@/components/widgets/mock-badge.tsx`.
  - Next conversion targets: `/watchlist` (High), `/symbol/[symbol]` (High),
    `/portfolio-builder` (Medium), `/receipts` (Low), `/signal-cockpit-demo`
    (Low).

**Acceptance criteria verification (self):**
- ✅ Every dashboard route has a readiness status in the atlas (25/25 routes
  mapped).
- ✅ Mock-heavy screens are visible in one doc (the Route Summary table
  highlights `/watchlist` as Mock and `/symbol/[symbol]` as Hybrid with 3
  MockBadge instances).
- ✅ The next conversion target is obvious (Next Conversion Targets section
  ranks `/watchlist` and `/symbol/[symbol]` as High priority).

**Notes for Reviewer:**
- This is a documentation-only task. No code changes, no tests needed.
- The atlas was built by scanning all `page.tsx` files under
  `apps/dashboard/src/app/` and searching for `MockBadge`, `mock-data`,
  `placeholder`, `demo`, `fixture`, `useQuery`, `useMutation`, `useFinceptStream`,
  and `@/lib/api` imports.
- File-disjoint from all active builders: new doc file only.
