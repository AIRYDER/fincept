# Next Steps

## Immediate Stabilization

### 1. Bound `/data/coverage` and Restore Green Route Smoke

- Goal: Make `/data/coverage` return 200 or structured 503 inside the smoke
  timeout.
- Why it matters: Latest route-smoke receipt fails only this probe; it blocks
  confidence in API/dashboard health.
- Suggested files to inspect/change:
  - `services/api/src/api/routes/data.py`
  - `libs/fincept-db/src/fincept_db/bars.py`
  - `services/api/tests/test_data.py`
  - `scripts/route_smoke.py`
- Risk level: Medium.
- Validation method:
  - `uv run pytest services/api/tests/test_data.py -q`
  - `uv run python scripts/route_smoke.py --base-url http://127.0.0.1:8010`

### 2. Add Approved-Root Checks for Backtest Inputs

- Goal: Reject traversal, arbitrary absolute paths, and files outside approved
  data/report roots for `POST /backtest/run`.
- Why it matters: Prevents filesystem probing or unintended local file reads by
  authenticated operators.
- Suggested files to inspect/change:
  - `services/api/src/api/routes/backtest.py`
  - `services/api/tests/test_backtest.py`
  - `data/` fixture conventions
- Risk level: Medium.
- Validation method:
  - Add tests for allowed relative fixture, traversal, absolute out-of-root, bad
    suffix, and missing path.
  - `uv run pytest services/api/tests/test_backtest.py -q`

### 3. Record a Fresh Current Verification Matrix

- Goal: Create or update a short receipt with current pass/fail/skip results for
  Python, dashboard, route smoke, paper-spine, and OpenBB.
- Why it matters: Existing docs contain multiple historical snapshots; future
  agents need a current state anchor.
- Suggested files to inspect/change:
  - `reports/`
  - `docs/project-understanding/06-current-status.md`
  - possibly a new `docs/VERIFICATION_REPORT.md`
- Risk level: Low.
- Validation method:
  - Run only commands that are available locally and clearly mark skipped
    Docker/live-provider checks.

## Short-Term Improvements

### 4. Add Dashboard Route Inventory Smoke

- Goal: Probe all dashboard routes for load/redirect/API-contract status.
- Why it matters: The dashboard surface is broad and currently outgrows route
  proof coverage.
- Suggested files to inspect/change:
  - `apps/dashboard/src/app`
  - `apps/dashboard/scripts`
  - existing dashboard run-*.cjs scripts
- Risk level: Medium.
- Validation method:
  - Start dashboard locally and run a browser or HTTP route inventory check.
  - Include `/predictions`, `/reconciliation`, `/portfolio-builder`,
    `/news-lab`, `/news-impact-lab`, `/optimizer`, `/signal-cockpit-demo`,
    `/system`, and `/receipts`.

### 5. Split OpenBB Readiness Into an Operator Matrix

- Goal: Distinguish API process status, package availability, backend
  reachability, provider credentials, allowlist policy, and provider result.
- Why it matters: Current proof shows health can be structurally okay while
  quote/dispatcher calls are unavailable.
- Suggested files to inspect/change:
  - `services/api/src/api/routes/research.py`
  - `libs/fincept-tools/src/fincept_tools/research/openbb.py`
  - `apps/dashboard/src/app/research/page.tsx`
  - `reports/openbb-live/`
- Risk level: Medium.
- Validation method:
  - `uv run python scripts/openbb_live_proof.py --symbol NVDA`
  - targeted API tests for each readiness branch.

### 6. Reconcile Build Order and Current Agent Inventory

- Goal: Update task status and agent entries to match implemented source and
  known stubs.
- Why it matters: Future agents follow `spec/BUILD_ORDER.md`; stale status can
  waste work or cause overclaims.
- Suggested files to inspect/change:
  - `spec/BUILD_ORDER.md`
  - `services/agents/src/agents`
  - `docs/SYSTEM_OVERVIEW.md`
- Risk level: Low.
- Validation method:
  - Docs-only review plus source links.

## Medium-Term Architecture Work

### 7. Promote Paper-Spine Replay to a Service-Backed Profile

- Goal: Keep the deterministic fakeredis proof and add a Docker-service profile
  that captures Redis stream IDs, Timescale rows, API correlation IDs, and
  portfolio persistence.
- Why it matters: The current receipt proves contracts but not live service
  wiring.
- Suggested files to inspect/change:
  - `scripts/paper_spine_replay.py`
  - `docker-compose.yml`
  - `libs/fincept-bus`
  - `libs/fincept-db`
  - service entrypoints
- Risk level: High.
- Validation method:
  - Deterministic proof still passes.
  - Service-backed receipt records expected stream/database evidence.

### 8. Move Long Backtests and Training Runs Off Request Threads

- Goal: Use background run records/queue semantics for expensive work while
  keeping dashboard polling contracts stable.
- Why it matters: Synchronous request-thread work can degrade API responsiveness.
- Suggested files to inspect/change:
  - `services/api/src/api/routes/backtest.py`
  - `services/api/src/api/background.py`
  - model training run patterns
  - dashboard backtest page
- Risk level: Medium.
- Validation method:
  - API tests for queued state transitions.
  - Dashboard typecheck.

### 9. Harden Auth for Staging

- Goal: Remove unsafe defaults outside local/test mode and introduce a safer
  browser session model.
- Why it matters: Auth is currently prototype-grade.
- Suggested files to inspect/change:
  - `libs/fincept-core/src/fincept_core/config.py`
  - `services/api/src/api/auth.py`
  - `apps/dashboard/src/lib/auth.ts`
  - `apps/dashboard/src/app/login/page.tsx`
- Risk level: High.
- Validation method:
  - Auth tests for missing/default secret behavior.
  - Dashboard login/logout tests.

## Long-Term Product Work

### 10. Formalize Research Tool Governance Receipts

- Goal: Every Exa/OpenBB/news-impact call records caller, route/tool, latency,
  rate-limit state, input hash, output hash, error type, and side-effect class.
- Why it matters: LLM/research tools should be auditable before autonomous use.
- Suggested files to inspect/change:
  - `libs/fincept-tools`
  - `services/api/src/api/routes/research.py`
  - `services/api/src/api/routes/news_impact.py`
  - dashboard research/news-impact pages
- Risk level: Medium.
- Validation method:
  - Unit tests for receipts on success and failure.
  - A generated report showing no order route for shadow-only tools.

### 11. Define Promotion Dossiers for Agents

- Goal: Each agent has tests, data window, calibration status, side-effect
  policy, promotion status, and rollback/shadow behavior documented.
- Why it matters: The agent layer is expanding faster than proof artifacts.
- Suggested files to inspect/change:
  - `services/agents/src/agents`
  - `services/agents/tests`
  - `models/`
  - `docs/`
- Risk level: Medium.
- Validation method:
  - Per-agent targeted tests.
  - Docs table with evidence links and promotion status.

### 12. Prepare Production Deployment Boundaries

- Goal: Document or implement staging/prod deployment surfaces, secret handling,
  allowed origins, singleton/leader election, and live-capital gates.
- Why it matters: Current architecture discusses Kubernetes and Phase H, but the
  repo is local-dev oriented.
- Suggested files to inspect/change:
  - `docs/ROADMAP.md`
  - `docs/DECISIONS.md`
  - deployment docs or future `infra/`
  - service settings
- Risk level: High.
- Validation method:
  - Security review checklist.
  - Deployment dry run in staging before any live-capital pathway.

## Automation Priority Reset - 2026-06-06

The current local worktree has enough new implementation that the next work
should be verification and decomposition, not more broad feature expansion.

### 1. Split the Dirty Tree Into Review Slices

- Goal: Produce a review plan that groups current changes into docs,
  dashboard widgets/pages, news-impact shadow integration, API/core contracts,
  provider/data hardening, and local/generated artifacts.
- Why it matters: The tree is too broad for one honest review or one safe
  commit.
- Validation method:
  - A Markdown review-slice table exists before any commit/push work.
  - Generated/local folders are either ignored or explicitly justified.

### 2. Prove News-Impact Shadow Safety End-to-End

- Goal: Verify `info.enriched -> news_impact_agent -> sig.news_impact ->
  GET /news-impact/signals -> ShadowNewsImpactPanel` with no execution fields.
- Why it matters: This is the newest high-value capability and must stay
  non-trading until promotion evidence exists.
- Validation method:
  - `uv run pytest services/agents/tests/test_news_impact_agent.py services/api/tests/test_news_impact.py -q`
  - `pnpm --dir apps/dashboard exec node scripts/run-shadow-news-impact-tests.cjs`

### 3. Convert Mock Watchlist And Symbol Pages Into Contracts

- Goal: Keep the new terminal-like watchlist/symbol UX, but define the API
  contract needed to replace inline fixtures.
- Why it matters: The UI is useful, but mock rows can become misleading if they
  are not clearly tied to a delivery path.
- Validation method:
  - Add a small `GET /watchlist` or `GET /symbols/{symbol}/snapshot` contract
    spec before replacing fixtures.
  - Dashboard route smoke includes `/watchlist` and `/symbol/AAPL`.

### 4. Verify Data Freshness And Coverage Recovery

- Goal: Prove whether the local provider/data changes fixed the prior
  `/data/coverage` timeout and expose `DataFreshness` consistently.
- Why it matters: Strategy readiness and dashboard provenance depend on stable
  data heartbeat semantics.
- Validation method:
  - `uv run pytest services/api/tests/test_data.py libs/fincept-core/tests/test_schemas.py -q`
  - `uv run python scripts/route_smoke.py --base-url http://127.0.0.1:8010`

### 5. Make Runtime Safety Checks Uniform

- Goal: Ensure every service entrypoint that binds a port or consumes streams
  calls the `FINCEPT_ENV` / JWT default guard before starting.
- Why it matters: Adding `assert_safe_for_runtime` helps only if every
  deployable process uses it.
- Validation method:
  - Unit test that `FINCEPT_ENV=prod` with the dev JWT secret refuses startup.
  - API/agent startup tests or smoke logs show the guard runs.

## Next Skills To Deepen - 2026-06-06

| Skill | First exercise | Done when |
|---|---|---|
| Change decomposition | Produce the review-slice plan before committing. | Each slice has files, tests, and excluded generated artifacts listed. |
| Shadow-signal governance | Prove the news-impact lane end-to-end with no execution fields. | Tests fail if order, broker, sizing, or venue controls enter the signal/UI. |
| Dashboard route QA | Auto-smoke `/watchlist`, `/symbol/AAPL`, `/news-impact-lab`, `/system`, and existing operator pages. | One receipt lists route, status, console errors, and API contract failures. |
| Data provenance engineering | Thread `DataFreshness` through coverage, marks, watchlist/symbol snapshots, and UI chips. | Every operator price or signal says realtime/delayed/cached/simulated/stale. |
| Runtime security gates | Apply and test startup fail-closed behavior outside local/test. | Production-like env cannot bind with default JWT or broad CORS settings. |

## Automation Priority Reset - 2026-06-08

The next useful work is not another broad audit. The tree needs proof artifacts
that make the current implementation reviewable and keep shadow/model/provider
features bounded.

### 1. Create the Review-Slice Ledger

- Goal: Write `docs/review-slices/2026-06-08-local-worktree.md` with slices for
  docs, dashboard UX, mock routes, news-impact shadow integration, API/core
  contracts, providers, tests, and local/generated artifacts.
- Why it matters: The current dirty tree spans too many ownership boundaries
  for one honest review.
- Validation method:
  - The ledger lists exact files, one owner/risk per slice, and one focused
    command per slice.

### 2. Add a Shadow-Lane Forbidden-Field Guard

- Goal: Mechanically check `NewsImpactSignal`, `/news-impact/signals`, and
  `ShadowNewsImpactPanel` for execution-shaped fields.
- Why it matters: The safest current innovation is the shadow lane precisely
  because it does not contain order authority.
- Validation method:
  - The guard fails on `side`, `quantity`, `venue`, `broker`, `order`, or
    `sizing` in the signal/API/panel path.

### 3. Build the Mock Route Atlas

- Goal: Inventory every dashboard route using inline fixtures, `mock-data.ts`,
  or `MockBadge`, starting with `/watchlist`, `/symbol/[symbol]`, and
  `/positions`.
- Why it matters: Mock surfaces are useful design scaffolds only when their
  replacement contracts are visible.
- Validation method:
  - Each atlas row names route, mock source, intended API endpoint, and first
    fixture-backed contract test.

### 4. Promote Provider Captures Into an Audit Store

- Goal: Use `/research/provider-data` as an operator evidence ledger with
  redacted errors, retention policy, provider readiness summary, and request
  hashes.
- Why it matters: OpenBB/Exa calls are high-value but need traceability before
  agents can use them safely.
- Validation method:
  - Tests cover success, disabled capture, provider failure, and sanitized
    unavailable state.

### 5. Convert Freshness Into a Strategy Gate

- Goal: Carry `DataFreshness` through backend fixtures, dashboard chips, and a
  strategy-readiness check.
- Why it matters: The terminal should block stale/simulated prerequisites
  before strategy-host can emit order intent.
- Validation method:
  - One fixture proves realtime/delayed/cached/simulated/stale display states,
    and stale blocks readiness without explicit paper-mode override.

## Next Skills To Deepen - 2026-06-08

| Skill | First exercise | Done when |
|---|---|---|
| Review decomposition | Write the review-slice ledger before commit work. | Each change group has files, risks, and validation commands. |
| Shadow invariant engineering | Build the forbidden-field guard for news-impact. | Shadow output cannot silently become an order-intent shape. |
| Mock-to-contract migration | Replace `/watchlist` inline rows with a fixture-backed API contract. | Frontend and backend share one tested snapshot shape. |
| Provider audit operations | Harden `/research/provider-data` as the evidence ledger. | Provider failures are visible without leaking raw details. |
| Freshness-gated execution design | Make freshness state block strategy readiness. | Stale/simulated prerequisites cannot start a strategy by default. |

## Automation Priority Reset - 2026-06-10

The review-slice ledger now exists. The next pass should stop broad discovery
and execute one proof slice at a time.

### 1. Run One Review Slice

- Goal: Pick a slice from `docs/review-slices/2026-06-10-local-worktree.md`
  and run only its listed validation command.
- Why it matters: This keeps docs, dashboard UI, provider behavior, and model
  safety from being reviewed as one oversized change.
- Validation method:
  - The staged set, if any, matches exactly one slice and excludes local tool
    artifacts.

### 2. Convert The Shadow Guard Into Code

- Goal: Add a script for the forbidden-field scan documented in the ledger.
- Why it matters: Shadow-model safety should be mechanical, not reviewer
  memory.
- Validation method:
  - The script fails if order-shaped fields appear in the signal schema, API
    route, or dashboard shadow panel.

### 3. Build The Mock Route Atlas

- Goal: Create `apps/dashboard/docs/mock-route-atlas.md` for routes using
  `MockBadge`, inline fixtures, or `mock-data.ts`.
- Why it matters: Mock terminal pages are acceptable only when their live
  replacement contracts are explicit.
- Validation method:
  - Each row names route, mock source, intended API endpoint, and first fixture
    contract test.

## Next Skills To Deepen - 2026-06-10

| Skill | First exercise | Done when |
|---|---|---|
| Slice execution discipline | Execute one ledger slice before any staging. | Review scope is bounded by exact files and one command. |
| Automated non-agency checks | Turn the news-impact forbidden-field scan into a script. | Shadow signals cannot gain execution shape silently. |
| Route contract documentation | Build the mock route atlas. | Every mock route has a named backend replacement. |

## Automation Priority Reset - 2026-06-14

The next useful work is to turn the current local helpers and ledgers into
repeatable proof. Do not expand the terminal surface until one high-risk slice
has a current receipt.

### 1. Promote The Shadow Test Helper

- Goal: Make `apps/dashboard/scripts/run-shadow-news-impact-tests.cjs` the
  documented receipt command for the news-impact slice.
- Why it matters: Shadow-model safety is the strongest current product idea
  only if non-agency is mechanically enforced.
- Validation method:
  - The command fails on execution-shaped fields and records which schema, API,
    and dashboard paths were checked.

### 2. Verify The Devin Workflow Move

- Goal: Confirm `.devin/workflows/phase-kickoff.md` is the active kickoff path
  and no current documentation still points agents at `.windsurf`.
- Why it matters: Workflow docs are part of the local operating system; stale
  kickoff paths waste future agent runs.
- Validation method:
  - `rg -n "\.windsurf|phase-kickoff" .devin .windsurf docs README.md`
    returns only intentional migration notes.

### 3. Build The Mock Route Atlas

- Goal: Add `apps/dashboard/docs/mock-route-atlas.md` for every route using
  `MockBadge`, `mock-data.ts`, or inline fixture rows.
- Why it matters: Mock terminal screens should be useful scaffolds without
  pretending to be live-market surfaces.
- Validation method:
  - Each atlas row names route, mock source, replacement endpoint, and first
    fixture-backed API test.

### 4. Add Provider Evidence Redaction Tests

- Goal: Test provider-data success, disabled capture, provider failure, and
  unavailable states for sanitized output.
- Why it matters: Provider evidence will become agent input; raw exceptions,
  paths, and secret-like strings cannot cross that boundary.
- Validation method:
  - Tests fail if response bodies include local paths, raw tracebacks, or
    high-entropy token-like values.

### 5. Make Freshness A Readiness Gate

- Goal: Build one fixture that proves every `DataFreshness` state through API,
  dashboard display, and strategy readiness.
- Why it matters: A trading terminal should block stale prerequisites before
  strategy intent exists.
- Validation method:
  - Realtime/delayed/cached can proceed under policy; stale/simulated block by
    default unless a paper-mode override is explicitly stored.

## Next Skills To Deepen - 2026-06-14

| Skill | First exercise | Done when |
|---|---|---|
| Shadow receipt engineering | Run the news-impact helper as the receipt for Slice 4. | Non-agency is proven by command output, not reviewer memory. |
| Workflow-path governance | Audit `.devin` versus retired `.windsurf` references. | Agent handoffs start from the active folder only. |
| Mock-to-contract migration | Write the mock route atlas before route conversion. | Mock surfaces have replacement endpoints and fixture tests. |
| Provider evidence security | Add redaction tests around provider-data responses. | Operator evidence is sanitized by default. |
| Freshness policy implementation | Add the readiness fixture for all freshness states. | Stale or simulated data cannot silently unlock strategy start. |

## Automation Priority Reset - 2026-06-21

The next useful work is to convert the now-visible validation scripts and
shadow API tests into durable receipts. Avoid adding more terminal UI until the
current mock-backed surfaces have an atlas and replacement endpoint plan.

### 1. Persist The Shadow UI/API Receipt

- Goal: Add a receipt runner that executes `npm run test:shadow-news-impact`
  and the focused `/news-impact/signals` API test.
- Why it matters: The shadow lane is the strongest current product slice only
  if non-agency is proven from schema to stream to API to UI.
- Validation method:
  - A file under `reports/news-impact/` records branch, commit, commands,
    `sig.news_impact`, tested files, and forbidden execution fields.

### 2. Add Malformed Stream Accounting

- Goal: Return `skipped_count` from `/news-impact/signals` for malformed or
  mismatched Redis rows.
- Why it matters: Silent skips hide stream health problems from operators.
- Validation method:
  - A test publishes one valid signal and one malformed row, then asserts the
    valid signal remains visible and the skipped row is counted without raw
    exception output.

### 3. Connect Source Health To Readiness

- Goal: Pair `npm run test:source-health` with
  `npm run test:strategy-readiness`.
- Why it matters: Freshness and source quality should determine whether a
  strategy can start, not only color a chip.
- Validation method:
  - The receipt shows stale/simulated fixtures blocking by default and records
    the policy reason for delayed/cached/realtime cases.

### 4. Generate The Mock Route Atlas

- Goal: Create `apps/dashboard/docs/mock-route-atlas.md` from `MockBadge` and
  `mock-data.ts` hits.
- Why it matters: Mock terminal screens are useful only when their live-data
  contract and blocker are explicit.
- Validation method:
  - Rows cover `/positions`, `/watchlist`, `/symbol/[symbol]`, and overview
    watchlist preview with replacement endpoint and first fixture test.

### 5. Verify Devin Workflow References

- Goal: Add a path/reference check for `.devin/workflows/phase-kickoff.md` and
  retired `.windsurf/workflows/phase-kickoff.md`.
- Why it matters: Tool-folder moves should not strand future agent runs.
- Validation method:
  - The check fails on active `.windsurf` kickoff references outside explicit
    migration notes.

## Next Skills To Deepen - 2026-06-21

| Skill | First exercise | Done when |
|---|---|---|
| Receipt orchestration | Persist the shadow UI/API proof. | One artifact proves non-agency across schema, stream, API, and UI. |
| Stream health operations | Add `skipped_count` and a malformed-row fixture. | Stream degradation is visible and sanitized. |
| Freshness policy enforcement | Tie source-health results to strategy readiness. | Source quality blocks or permits launch deterministically. |
| Mock governance | Generate the atlas before route conversion. | Mock surfaces have endpoint targets and tests. |
| Workflow migration checks | Fail on stale kickoff-path references. | Agent handoffs use active `.devin` docs only. |

## Automation Priority Reset - 2026-06-23

The next useful work is to turn the new Quant Foundry implementation stack into
reviewable receipts while preserving the existing news-impact and mock-terminal
safety queue. Do not treat local Quant Foundry code as live-ready until the
limited-readiness blockers have dated proof.

### 1. Build The Quant Foundry Release Receipt

- Goal: Create one receipt under `reports/quant-foundry/` that records focused
  service tests, dashboard type checks, provider redaction/freshness proof,
  budget-gateway proof, branch, commit, and skipped live dependencies.
- Why it matters: The implementation is now broad enough that commit history is
  not an adequate readiness signal.
- Validation method:
  - Include `services/quant_foundry/tests/*` slices for gateway/budget,
    settlement, promotion, conformal, drift, MoE, and causal graph.
  - Include dashboard type check or route smoke for `/quant-foundry/*`.
  - Include explicit blockers from `docs/LIMITED_LIVE_READINESS_REVIEW.md`.
  - Restore `npm run test:shadow-news-impact` or document the direct
    `node scripts/run-shadow-news-impact-tests.cjs` helper as the canonical
    shadow receipt command.

### 2. Compose Promotion Safety State

- Goal: Emit a model-level state from shadow settlement, tournament scoring,
  conformal intervals, drift sentinel, retirement flags, and paper-only pointer
  status.
- Why it matters: Promotion must be a fail-closed decision, not a page action.
- Validation method:
  - A fixture model with missing settlement, wide conformal interval, or drift
    breach is blocked with a machine-readable reason.

### 3. Drill RunPod Budget And Callback Safety

- Goal: Exercise gateway dispatch, budget ceilings, kill switch, idempotency,
  signed callbacks, and artifact import without spending GPU dollars.
- Why it matters: Cloud cost and callback trust are the main Quant Foundry
  agency boundaries.
- Validation method:
  - A dry-run fixture proves denied dispatch leaves a ledger entry and no
    active job; invalid callback signatures are rejected.

### 4. Add Quant Foundry Route Smoke

- Goal: Probe `/quant-foundry`, `/quant-foundry/jobs`, `/quant-foundry/models`,
  `/quant-foundry/shadow`, `/quant-foundry/tournament`, and
  `/quant-foundry/promotion`.
- Why it matters: The dashboard has become an operator surface, so page load and
  degraded-state behavior need receipts.
- Validation method:
  - One command records route, status, latency, mode, and missing dependency
    message for each page.

### 5. Classify The Current Lockfile And Artifacts

- Goal: Explain whether the tracked `uv.lock` delta is required by Quant
  Foundry dependencies and keep local tool/report folders out of review slices.
- Why it matters: The next commit/review should not bundle local automation
  state with product changes.
- Validation method:
  - `git diff -- uv.lock` is summarized in the review slice, and untracked
    `.omo/`, `.opencode/`, `.playwright-cli/`, `.worktrees/`, screenshots, and
    scratch transcripts remain excluded unless intentionally promoted.

## Next Skills To Deepen - 2026-06-23

| Skill | First exercise | Done when |
|---|---|---|
| Release-readiness receipts | Build the Quant Foundry release receipt. | Local readiness is proven by dated tests and blockers. |
| Model promotion governance | Compose promotion state from all risk gates. | Every model has an explainable promote/block/retire reason. |
| Cost and callback security | Dry-run RunPod dispatch through budget and signature gates. | Cloud agency fails closed without spending or trusting bad callbacks. |
| Operator route smoke | Probe every Quant Foundry dashboard route. | UI surfaces have load/degraded-state evidence. |
| Review hygiene | Classify `uv.lock` and local artifacts before staging. | The next review slice is intentional and reproducible. |
