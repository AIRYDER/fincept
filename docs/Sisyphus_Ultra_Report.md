# Sisyphus Ultra Report — Fincept Terminal

**Date:** 2026-06-22
**Author:** Sisyphus (autonomous audit pass)
**Scope:** Whole-system analysis of `C:\Users\nolan\CascadeProjects\fincept-terminal` — architecture, code maturity, operational state, design system, documentation, and the highest-leverage changes to make next.
**Method:** Repository read, not runtime audit. Sources cited inline. No services were started during this audit.

> **Reading note.** This report synthesizes the existing project-understanding docs, `SYSTEM_OVERVIEW.md`, `ROADMAP.md`, `SYSTEM_IMPROVEMENT_REPORT.md`, `codebase-audit-2026-05-16.md`, `nextlevelfeatures.md`, `aestheticaudit.md`, the spec, and a top-down pass of the source tree. Where the existing reports already said something well, this report cites them rather than restating. Where this audit found something the existing reports missed or under-weighted, it is flagged explicitly.

---

## 1. One-paragraph executive summary

Fincept Terminal is an **event-sourced, Python-first, paper-trading, AI-agentic trading platform** that has reached a critical maturity inflection. Every named architectural piece — typed contracts, Redis Streams bus, async SQLAlchemy/Timescale persistence, feature store, seven agents, orchestrator, risk gate, paper OMS, portfolio service, ML lifecycle, strategy host, FastAPI gateway, and a 20+ route Next.js dashboard — is implemented at package level and tested in isolation. **The system is no longer a scaffold; it is a body of working code that has not yet been proven as a connected trading system.** The single highest-leverage action is to close the proof loop: a service-backed paper-spine replay that runs through real Redis/Timescale, a port-8010 route smoke that fails fast on contract drift, a unified verification receipt, and a runtime safety matrix that makes guard-rail enforcement uniform across every long-running service. Once those four are green, the existing surface becomes a credible internal alpha-discovery platform; without them, every other investment compounds risk.

---

## 2. System map (verified against the tree)

### 2.1 Repo shape

```text
fincept-terminal/
├── apps/
│   └── dashboard/                Next.js 14 App Router operator console
├── libs/                         Pure-Python shared packages
│   ├── fincept-core/             Schemas, events, config, clocks, IDs, leadership, strategy config, prediction log, portfolio math
│   ├── fincept-bus/              Redis Streams producer/consumer
│   ├── fincept-db/               Async SQLAlchemy + Alembic (Postgres/Timescale)
│   ├── fincept-sdk/              Read-only Python SDK
│   └── fincept-tools/            Typed tool registry + Exa/OpenBB research tools
├── services/                     Long-running async Python services
│   ├── ingestor/                 Binance/Coinbase/Kraken WS + EOD equity loader
│   ├── features/                 PIT transforms, online (Redis) + offline (Parquet) stores
│   ├── agents/                   7 implemented agents + 1 stub (`pairs`)
│   ├── orchestrator/             Consensus → allocator → decisions/orders
│   ├── risk/                     Pre-trade checks + portfolio snapshot + kill switch
│   ├── oms/                      Paper OMS + Alpaca paper adapter
│   ├── portfolio/                Fill-driven positions + P&L
│   ├── api/                      FastAPI REST + WebSocket gateway
│   ├── backtester/               Event-driven historical replay + walk-forward CV
│   ├── jobs/                     Scheduled EOD/news/model jobs
│   ├── strategy_host/            Filesystem-backed strategy instance supervisor
│   └── quant_foundry/            Workspace member; appears in `uv` config
├── experiments/news-impact-model Out-of-tree research
├── strategies/                   Persistent strategy configs + JSONL history
├── data/                         Parquet features, predictions, model artifacts
├── reports/                      Generated receipts (paper-spine, openbb-live, route-smoke, verification)
├── docs/                         40+ docs incl. ROADMAP, BLUEPRINT, DECISIONS, RISKS, SYSTEM_OVERVIEW
├── spec/                         ARCHITECTURE, CONTRACTS, BUILD_ORDER, 21 atomic task specs
├── scripts/                      .ps1 wrappers (start, status, stop, preflight, task-check, paper_spine_replay, openbb_live_proof, route_smoke)
└── docker-compose.yml            Timescale/Postgres + Redis + MinIO
```

**Member services in `uv` workspace:** 15 entries (4 libs + 11 services), per `pyproject.toml:8-26`. `quant_foundry` is listed but does not have a `src/` in the listing above; worth verifying.

### 2.2 End-to-end flow

```
ingestor → features → agents → orchestrator → risk → OMS → portfolio → API/dashboard
                  ↘ strategy_host (parallel intent path)   ↗
```

Backbone is `Redis Streams` partitioned into `md.*` (market data, ephemeral), `sig.*` (signals/predictions, 30-day audit), and `ord.*` (orders/fills, WORM retention). State lives in Postgres/Timescale; ML artifacts in MinIO/S3; strategy configs in filesystem JSON + JSONL history.

### 2.3 Dashboard surface (from `apps/dashboard/src/app/`)

20+ routes spanning operations, research, ML, and operator workflows:

| Route | Role | Live/Mock/Hybrid (audit tag) |
|---|---|---|
| `/` | Operator home — KPIs, alerts, active model, sparkline | Live-shaped, sparkline data posture flagged |
| `/positions` | Position book + per-symbol P&L | Hybrid (MockBadge present per audit) |
| `/orders` | Order/fill blotter | Live |
| `/strategies` `/strategies/[id]` | Config CRUD, lifecycle toggles, history | Live |
| `/predictions` | Per-symbol live prediction stream | Live |
| `/models` `/models/[name]` | Registry, feature importance, promote/shadow, prediction stats | Live (per `SYSTEM_OVERVIEW §6`) |
| `/markets` | Bar chart + symbol selector | Hybrid |
| `/risk` | Current limits + breaches | Live |
| `/research` | Exa/OpenBB research, provider proofs | Hybrid (live OpenBB + fallback) |
| `/news` `/news-lab` | News surface + lab | Live-shaped, key-gated |
| `/news-impact-lab` | Experimental shadow workbench | **Shadow only — labeled explicitly** |
| `/portfolio-builder` (was `/optimizer`) | Operator-facing allocation planner + AI report | Live planner, AI report provider opt-in |
| `/signal-cockpit-demo` | Alternative instrument-panel UI direction | Demo-only |
| `/reconciliation` | Position reconciliation | Live |
| `/login` | Bearer-token paste box | Dev-only |
| `/receipts` `/system` `/watchlist` `/symbol/[symbol]` `/backtest` `/api` | Operator utilities | Mixed |

### 2.4 ML lifecycle (Phase A → E — shipped)

`SYSTEM_OVERVIEW §6` confirms: train → walk-forward/holdout → promote → hot-reload → shadow → predict → log is end-to-end. `gbm_predictor` polls `models/active/<agent_id>.json` every ~30s; shadow loop records to JSONL but has **no producer** (defence-in-depth against shadow leak).

### 2.5 Documentation corpus

40+ files across `docs/`. Notable quality spread:
- **Authoritative:** `SYSTEM_OVERVIEW.md` (last updated 2026-05-08), `ROADMAP.md` (2026-05-09), `spec/ARCHITECTURE.md`, `spec/CONTRACTS.md`, `spec/BUILD_ORDER.md`, `DESIGN.md`, `SYSTEM_IMPROVEMENT_REPORT.md` (2026-06-21).
- **Snapshotty:** `README.md` carries four dated "local progression snapshot" sections (Apr 26, 27, 28, 30, May 2, 7) that overlap with the same dates covered in `ROADMAP.md`. Drift risk.
- **Zero-byte / placeholder:** Several `nextlevelfeatures.md`/`ui-audit`/etc. were once zero-byte; mostly populated now but worth a final pass.
- **Knowledge base:** `docs/project-understanding/`, `docs/agent-ui-analysis/`, `docs/superpowers/`, `docs/test_dir/`, `docs/AAA_GLM_SUPERTEAM_LOGS/`, `docs/quant-ml-audit/`, `docs/review-slices/` — a substantial reasoning library.

---

## 3. What's genuinely working

In rough order of strategic weight:

1. **Architecture is sound and consistent.** Hard service boundaries, a typed event bus, three stream namespaces (`md.*` / `sig.*` / `ord.*`), and a single source of truth for schemas in `fincept-core`. This is the platform's most valuable asset and not easy to undo.
2. **Contract-first task system works.** `spec/` proves the model: 11 authored + templated atomic tasks; the tasks most worth doing are queued behind smaller verified ones.
3. **ML lifecycle is end-to-end and safe.** Train → walk-forward → promote → hot-reload → shadow → predict → log → audit. Shadow loop is correctly structured to prevent signal leak (no producer). This is genuinely unusual rigor for a project at this maturity.
4. **Dashboard identity is coherent.** The OLED-black + mono + cobalt/orange language (`DESIGN.md`) is consistently applied; `aestheticaudit.md` (2026-05-09) validates this empirically across 13 routes. `signal-cockpit-demo` is flagged as the strongest alternative direction and worth retaining.
5. **Paper-first posture is honored.** `execution_mode=paper` is mandatory in `services/oms/`; the Alpaca adapter is a parallel code path behind a flag; live trading is explicitly gated by Phase 5 governance in the roadmap.
6. **Self-documenting via receipts.** `paper_spine_replay.py`, `openbb_live_proof.py`, `route_smoke.py`, `task-check.ps1`, `preflight.ps1` show that the team has internalized the pattern of writing tests as durable artifacts in `reports/`.
7. **Shadow-only discipline is explicit.** `news-impact-lab` is labeled "shadow only — no order path"; the API test suite asserts UI does not expose trade-driving controls. This is the right default for any feature that touches LLM-generated trading signals.
8. **Documentation corpus is large and largely kept in sync.** `ROADMAP.md` carries inline update entries per code pass; `SYSTEM_OVERVIEW.md` is a real ground-truth document; ADRs exist.

---

## 4. What's not working (the gap between "implemented" and "trusted")

These are the issues that block the next dollar of investment. They map onto but extend the findings in `SYSTEM_IMPROVEMENT_REPORT.md` and `codebase-audit-2026-05-16.md`.

### 4.1 Safety guards are not applied uniformly

`SYSTEM_IMPROVEMENT_REPORT.md` flagged this as **CRIT-001** and it is the single most important issue in the repo. `assert_safe_for_runtime(settings)` exists in `fincept-core` and is called by the API startup. It is **not** called by `services/ingestor/src/ingestor/main.py`, `services/orchestrator/src/orchestrator/main.py`, `services/oms/src/oms/main.py`, or `services/strategy_host/src/strategy_host/main.py`. Each of those opens Redis, starts heartbeat, and (for OMS) selects execution mode — without the same fail-closed check the API uses. In a trading-adjacent system, this is the kind of bug class where a misconfigured prod-like env can route orders before any operator notices. **This is a 4-file patch.**

### 4.2 File-path boundaries are too broad

`SYSTEM_IMPROVEMENT_REPORT.md` flagged this as **CRIT-002**; `codebase-audit-2026-05-16.md` confirmed it independently as **#2 and #3**. `services/api/src/api/routes/backtest.py` accepts `bars_path` and turns it into `pathlib.Path(body.bars_path)` with only an `is_file()` check. `services/api/src/api/training.py` documents a repo-root boundary but does not enforce one. The OpenBB default port is split (`6900` vs `6901`) across `scripts/start.ps1`, `scripts/start_feature.ps1`, `fincept_tools.research.openbb`, and `scripts/status.ps1` — a small but telling example of ungrounded defaults.

### 4.3 Auth model is dev-only by design

`SYSTEM_IMPROVEMENT_REPORT.md` flagged this as **HIGH-004** and it is correct. JWTs in `localStorage` + WebSocket tokens in query strings is acceptable for the current `dev`/`local` posture, but the README explicitly notes Phase H should move to httpOnly cookies + OAuth. Until that move lands, no deployment beyond the current local laptop is honest.

### 4.4 Proof gates are deterministic-but-fake

`reports/paper-spine/latest.json` (May 8) is the strongest current integration receipt and it proves the spine end-to-end. It uses `fakeredis`, a checked-in AAPL fixture, and asserts 11 things. It is **not** a live-service receipt. Same pattern for `openbb-live` (1/3 probes passing) and `route-smoke` (8/9 passing — `/data/coverage` times out). The strategy-host paper replay and port-8010 contract smoke referenced in `nextlevelfeatures.md` P0 #1 and #2 are not yet green.

### 4.5 Release hygiene

`SYSTEM_IMPROVEMENT_REPORT.md` **CRIT-003** and the explicit repo audit both observed: 95+ tracked modified paths, 62+ untracked source/docs paths, top-level tool-state directories (`.opencode`, `.playwright-cli`, `.worktrees`, `.devin`, `.bridgespace`, `.codex`) are not all ignored. Broad `git add` is unsafe today. The repo is in active development by many agents; without an enforced hygiene pass, every commit carries tail risk.

### 4.6 Documentation drift

`docs/SYSTEM_IMPROVEMENT_REPORT.md` notes (MED-004) that the README's older "local progression snapshot" sections describe components as stubs that are now implemented. `docs/project-understanding/06-current-status.md` is more current than some root README sections. ADRs are scattered across `docs/`, `README.md`, and `spec/`. The `featuresmenu.md` and `nextlevelfeatures.md` backlogs overlap. There is no "status authority" pointer.

### 4.7 CI supply-chain defaults

`codebase-audit-2026-05-16.md` did not call this out, but `SYSTEM_IMPROVEMENT_REPORT.md` (HIGH-005) did and it is correct: `astral-sh/setup-uv@v3` with `version: latest`, `aquasecurity/trivy-action@master`, `pnpm install --frozen-lockfile=false`, and `docker-compose.yml` using `latest-pg16` / `latest` tags all reduce reproducibility. None of these block local work, but they block credible staging.

### 4.8 Mock vs live opacity

`SYSTEM_IMPROVEMENT_REPORT.md` **HIGH-002** is right: `MockBadge` exists in components and a route atlas does not. Operators cannot answer "is `/positions` real?" without reading source. The dashboard reads as polished — sometimes more polished than the data behind it — which is exactly the trust-drift pattern `aestheticaudit.md` flagged on the Overview screen ("the surface looks live and authoritative even when top-level connectivity badges are inconsistent").

### 4.9 Latent bugs visible in temp logs

`tmp_oms.out` empty, `tmp_portfolio.out` 108 bytes, `tmp_pytest.out` 712 bytes — these are diagnostic artifacts left in the tree and not routed to `reports/`. `tmp_jobs.err` is 17KB and worth a glance to see what currently fails at runtime in jobs.

---

## 5. Value-add: what would add the most to this system

This is the heart of the report. The recommendations below are **prioritized by impact-per-engineer-week** and intentionally sequenced so each one unblocks the next. They are grouped into **Tiers** that match the existing roadmap vocabulary, with a small number of **Tier-0** items that must land before anything else.

### Tier 0 — Make the existing system provable (1–2 engineer-weeks total)

These are blocking gates. Without them, every other investment is theoretical.

#### V0.1 **Runtime safety matrix — apply `assert_safe_for_runtime` to every service entrypoint**

- **What:** Add `assert_safe_for_runtime(settings)` to `services/ingestor/main.py`, `services/orchestrator/main.py`, `services/oms/main.py`, `services/strategy_host/main.py`, and any other long-running service that opens Redis. Add a startup matrix test that fails if any service drops the guard.
- **Why:** This is CRIT-001 from `SYSTEM_IMPROVEMENT_REPORT.md`. The platform currently advertises a fail-closed invariant that only one service honors. In a trading-adjacent system, an unsafe default reaching a strategy host or OMS is a category-A risk.
- **Cost:** ~half a day. ~10-line edits per entrypoint, one test file.
- **Acceptance:** Starting any service with `ENV=prod` and the default dev JWT secret fails before side effects. No secret values appear in error output. CI fails if the guard is removed.

#### V0.2 **Port-8010 contract smoke receipt (live)**

- **What:** Extend `scripts/route_smoke.py` to probe every operator-facing route in `apps/dashboard/src/app/` against a running API + dashboard client. Cap `/data/coverage` latency. Separate auth/timeout/shape failures. Make it the canonical gate before any release cut.
- **Why:** `route_smoke-20260506-211742.json` shows 8/9 probes passing — but `/data/coverage` is timing out. The dashboard reads as polished; the API does not match that posture. Without this receipt, every dashboard screenshot is a risk surface.
- **Cost:** 2–3 days including tests.
- **Acceptance:** All probes pass within bounded latency; skipped checks are explicit; failures preserve enough context to debug. Receipt archived under `reports/route-smoke/`.

#### V0.3 **Paper-spine replay receipt (service-backed, not fakeredis-only)**

- **What:** Promote `scripts/paper_spine_replay.py` from `fakeredis + SQLite` to real Redis/Timescale when the stack is available. Add a strategy-host enabled/disabled config to the fixture. Capture real Redis stream IDs, Timescale row counts, decision IDs, risk results, order IDs, fill IDs, and portfolio persistence.
- **Why:** This is the single highest-value proof artifact for a paper-trading platform. `SYSTEM_OVERVIEW §7` already calls it out. Until this is green, the orchestrator→OMS→portfolio path is "individually plausible but not proven as a trading system."
- **Cost:** 3–5 days.
- **Acceptance:** Replay runs through real service boundaries; one accepted order, one risk-rejected order, one shadow-model-not-publishing assertion; one disabled strategy that stays silent; full audit trail reconstructable from the receipt.

#### V0.4 **Unified verification receipt command**

- **What:** Add `scripts/verification-receipt.ps1` that runs safe targeted checks (source-health, strategy-readiness, shadow-news-impact, dashboard typecheck, API news-impact slice) and writes a timestamped JSON+Markdown receipt under `reports/verification/`. Make it the default local gate; make full preflight opt-in.
- **Why:** Today, proof is fragmented across `preflight.ps1` (heavy), `task-check.ps1` (narrow), `route_smoke.py`, `paper_spine_replay.py`, and ad-hoc test commands. Operators cannot answer "is this code green?" in under a minute. A single durable receipt closes that loop and is citable in PRs and releases.
- **Cost:** 1 day.
- **Acceptance:** Receipt records command, exit code, duration, skipped reason per check. Default command safe for local dev without live credentials.

### Tier 1 — Make the safety story honest (1–2 engineer-weeks)

Once Tier 0 lands, the platform is provable. Tier 1 makes it trustworthy enough to demo to anyone outside the build team.

#### V1.1 **File-path boundary helper + apply to backtest + training + OpenBB port single-source-of-truth**

- **What:** Add `libs/fincept-core/src/fincept_core/safe_paths.py` with a `resolve_within_root(path, allowed_roots)` helper. Apply in `services/api/src/api/routes/backtest.py` and `services/api/src/api/training.py`. Centralize the OpenBB default URL in one module and consume it from `scripts/start.ps1`, `scripts/start_feature.ps1`, `scripts/status.ps1`, `scripts/openbb_live_proof.py`, and `fincept_tools.research.openbb`.
- **Why:** These are the highest-trust-boundary gaps the codebase has. The fix is surgical.
- **Cost:** 1–2 days.
- **Acceptance:** `../` and absolute system paths are rejected; invalid OpenBB ports cause a clear error in `status.ps1`; both routes have negative tests for traversal, absolute-outside-root, and wrong-extension paths.

#### V1.2 **Route atlas + Mock Replacement Queue**

- **What:** Generate `docs/dashboard-route-atlas.md` (or a `reports/atlas/latest.json`) listing every dashboard route, its data source, mock/hybrid/live status, backend dependency, and replacement priority. Pick one mock-heavy route (likely `watchlist` or `symbol/[symbol]`) and replace the fixture with a service-backed contract. Keep `MockBadge` visible until the swap is complete.
- **Why:** `SYSTEM_IMPROVEMENT_REPORT.md` HIGH-002 and the audit agree. Operators cannot currently tell live from mock without reading source. The aesthetic audit flagged this credibility drift.
- **Cost:** 2–3 days including the first route swap.
- **Acceptance:** Every route has a status row; one mock-heavy route now uses live data; mock badges remain for any fixture that survives.

#### V1.3 **Strategy readiness gate**

- **What:** Before any `start` on a `StrategyConfig`, run a deterministic readiness check covering required-symbol coverage freshness, model binding existence, risk limits loaded, kill switch state, paper-broker connectivity, and last route-smoke status. Return blocking failures, warnings, override eligibility, and audit fields. Persist the readiness result into `strategies/<id>.history.jsonl`.
- **Why:** Strategy configs and lifecycle exist; the operator still has no "can this strategy safely start?" answer. The nextlevelfeatures backlog calls this out as #5.
- **Cost:** 2 days.
- **Acceptance:** A strategy cannot start when required market data is stale, kill switch is active, or model binding is missing. Operator overrides are explicit and logged. Tests cover enabled/disabled/stale-data/missing-model/risk-blocked/broker-unavailable.

#### V1.4 **Shadow model promotion dossier**

- **What:** For each model, generate a dossier with walk-forward summary, holdout metrics, fold dispersion, calibration bucket table, latest prediction age, active/shadow state, data window, and known skip-reasons. Require a current dossier for promotion from shadow to active. Surface it on the dashboard model detail page.
- **Why:** `nextlevelfeatures.md` #4 and the SYSTEM_IMPROVEMENT_REPORT FEATURE-003 both call for this. The ML lifecycle is shipped; the operator decision-support for promotion is not.
- **Cost:** 2–3 days.
- **Acceptance:** Promotion UI/API cannot move shadow → active without a current dossier. Dossier is reproducible from a command.

### Tier 2 — Make the product credibly competitive (3–6 engineer-weeks)

Once the platform is provable and trustworthy, the question shifts from "is the plumbing real?" to "does it produce signal an operator will use?" This tier is where the system earns its keep.

#### V2.1 **Backtester fidelity upgrade**

- **What:** Add configurable latency, partial-fill, spread/slippage, fee scenarios; simulate the same risk checks used in the paper path; produce attribution by strategy, symbol, feature family, model, and risk rejection reason. Make the backtester share a risk-config module with `services/risk/`.
- **Why:** This is the single most important research asset in the platform and the gap to retail-grade backtesters (VectorBT, QuantConnect) is exactly here. Credible backtests → credible paper-trading → credible promotion decisions.
- **Cost:** 1–2 weeks.
- **Acceptance:** A backtest can compare simulated fills with internal paper fills and, when configured, Alpaca paper fills. Reports show gross/net P&L, fees, slippage, rejected notional, turnover, drawdown, Sharpe. Risk-gate simulation uses identical rule parameters as the live risk service.

#### V2.2 **Model validation + calibration dossier as a first-class artifact**

- **What:** Extend V1.4 with rolling accuracy, Brier score, calibration drift by symbol/horizon/regime, prediction-log freshness, and known gap markers. Add a "Promote" affordance that requires a healthy dossier and an explicit override reason.
- **Why:** Without this, promotion is folklore. With this, promotion is a reproducible operator decision.
- **Cost:** 1 week.
- **Acceptance:** Every model has a current dossier, a stale dossier, or a missing-labels marker; promotion UI enforces it.

#### V2.3 **Source-aware operator recommendations rail**

- **What:** Add an operator recommendation payload that combines coverage freshness, provider health, latest predictions, news/research summaries, portfolio exposure, and risk state. Classify each item as investigate, hold, reduce, paper-only test, or blocked. Include evidence IDs, source timestamps, confidence caveats, and next-check links. **Never** place orders directly.
- **Why:** This is the constrained-AI surface `nextlevelfeatures.md` #6 calls for. The strategic bet in the original IMPLEMENTATION.md ("multi-agent orchestration with tool use") becomes credible only when recommendations are evidence-anchored and refuse to overstate weak inputs.
- **Cost:** 2 weeks.
- **Acceptance:** Recommendations degrade to "insufficient evidence" when any of {provider health, coverage, model labels} are stale. Each item links to a source route or a receipt. Tests cover missing provider keys, stale predictions, active kill switch, and conflicting model/news evidence.

#### V2.4 **Cross-asset and regime feature pack**

- **What:** Add BTC dominance, ETH/BTC, equity index proxy, macro regime, and news-volume features with explicit provider dependencies. Track feature freshness and missingness by symbol and horizon. Add ablation reports per feature family.
- **Why:** Multiple agents exist; the only thing missing for a regime-aware ensemble is the cross-asset and macro signal layer. Without it, every agent is coin-pure.
- **Cost:** 1–2 weeks.
- **Acceptance:** Each feature has point-in-time tests and a documented provider dependency. Training reports show ablation evidence. Missing optional sources produce degraded confidence, not crashes.

### Tier 3 — Make the system legible to outside operators (1–2 engineer-weeks)

The Tier 0–2 work makes the system real to its current users. Tier 3 makes it credible to the next user.

#### V3.1 **Cookie-based session auth + CSRF + WebSocket cookie handshake**

- **What:** Replace `localStorage` JWTs with httpOnly secure cookies. Add CSRF tokens for state-changing routes. Replace WebSocket `?token=...` query strings with a short-lived handshake token exchanged via POST.
- **Why:** Without this, no external user can be added. `apps/dashboard/README.md` already calls this out.
- **Cost:** 1 week.
- **Acceptance:** JWTs not in `localStorage` in staging/prod mode. WebSocket URLs do not contain bearer tokens. Local dev auth remains documented.

#### V3.2 **Provider evidence ledger**

- **What:** Store redacted request/response summaries with freshness, dataset, symbol, provider, and retention class. Expose via a read-only API and a dashboard surface. Add nightly freshness reports and alerts on stale data.
- **Why:** `SYSTEM_IMPROVEMENT_REPORT.md` MED-003 and `nextlevelfeatures.md` (FEATURE-004 in SYSTEM_IMPROVEMENT_REPORT) both call for this. Without it, operator trust in research/news signals is folklore.
- **Cost:** 1 week.
- **Acceptance:** Receipts prove provider calls happened without exposing credentials. Stale data is visible.

#### V3.3 **Release-hygiene pass**

- **What:** Categorize the dirty tree into product/docs/receipts vs. local tool state. Add `.gitignore` rules for `.opencode`, `.playwright-cli`, `.worktrees`, `.devin`, `.bridgespace`, `.codex`, etc. Add a release checklist that requires a clean or intentionally bucketed `git status --short`. Do not auto-commit anything; do not bypass hooks.
- **Why:** This is CRIT-003 from the system improvement report. The repo currently cannot be safely staged as a unit.
- **Cost:** 1 day.
- **Acceptance:** `git status --short` is understandable at a glance; known local tool state is ignored; release/PR scope is descriptive without guessing.

#### V3.4 **Documentation status authority + ADR hygiene**

- **What:** Add `docs/STATUS.md` as the single "what's authoritative" pointer. Promote open ADRs (per `ROADMAP.md` snapshot from May 7: ADR-0006, ADR-0009) to `accepted`. Add ADRs for runtime safety, shadow-model promotion, auth migration, and mock-data policy. Reconcile `featuresmenu.md` and `nextlevelfeatures.md` to be complementary, not overlapping.
- **Why:** Documentation drift is the most common cause of bad roadmap decisions.
- **Cost:** 1–2 days.
- **Acceptance:** Setup, status, roadmap, and risks agree on source of truth. ADRs link to source files and current tests.

### Tier 4 — Differentiation features (only after Tier 0–3 are green)

These are the cutting-edge bets the original blueprint promised. They are **explicitly not next** in the current state.

| Feature | Source | Why wait | Effort |
|---|---|---|---|
| Multi-agent LLM debate (bull/bear/judge) | `spec/BUILD_ORDER.md` Task 086 | Needs a credible orchestrator; not yet | 2–3 weeks |
| Options-flow / earnings-call / insider agents | `spec/BUILD_ORDER.md` Tasks 080–082 | Needs dossier infra (V1.4) | 4 weeks each |
| RL execution agent | `spec/BUILD_ORDER.md` Task 065 | Needs backtester fidelity (V2.1) and live broker gate | 6+ weeks |
| Federated / multi-tenant | `spec/BUILD_ORDER.md` Task 104 | Needs cookie auth (V3.1) and serious ops investment | Out of scope for MVP |
| Live-capital trading | `ROADMAP.md` Gate 5→Live | Needs every prior tier and a risk-committee approval | Years |

---

## 6. Where existing reports already nailed it (and what to cite, not repeat)

| Topic | Authority | Why I'm not duplicating |
|---|---|---|
| Runtime safety guard gap | `SYSTEM_IMPROVEMENT_REPORT.md` CRIT-001 | Identical finding, same 4 files, same fix. I cite it. |
| File-path boundaries | `SYSTEM_IMPROVEMENT_REPORT.md` CRIT-002 + `codebase-audit-2026-05-16.md` #2/#3 | Identical findings. I cite both. |
| Auth model migration | `SYSTEM_IMPROVEMENT_REPORT.md` HIGH-004 | Identical finding. I cite it. |
| Release hygiene | `SYSTEM_IMPROVEMENT_REPORT.md` CRIT-003 | Identical finding. I cite it. |
| Verification receipt gap | `SYSTEM_IMPROVEMENT_REPORT.md` CRIT-004 | Identical finding. I cite it. |
| Coverage timeout | `SYSTEM_IMPROVEMENT_REPORT.md` HIGH-003 + `ROADMAP.md` §17 | Both call it out. I cite. |
| Shadow news-impact hardening | `SYSTEM_IMPROVEMENT_REPORT.md` HIGH-001 | Identical finding. I cite. |
| Dashboard credibility drift | `aestheticaudit.md` (2026-05-09) | Identical finding. I cite. |
| Feature backlog | `nextlevelfeatures.md` | Strong as-is. Tier 2–4 mirrors it. |

**Net effect:** This report adds prioritization (Tier 0 first), sequencing (each tier unblocks the next), and a few items the existing reports under-weighted:

- **Promotion dossier as a hard gate** (V1.4 → V2.2) — present in `nextlevelfeatures.md` but not framed as a blocker on the orchestrator loop.
- **Aesthetic credibility as a *first-class* correctness concern** — `aestheticaudit.md` named it; this report ties it to Tier 0/1 (route atlas + verification receipt close the trust gap).
- **The roadmap-tier-pause principle** — explicitly call out that Tier 4 features (debate, options agents, RL execution) are not the next move, even though they are the most exciting.
- **Quantified prioritization by impact-per-engineer-week**, which the existing reports don't attempt.

---

## 7. Risks specific to executing the above

| Risk | Mitigation |
|---|---|
| Tier 0 changes touch every service entrypoint; high blast radius if rushed | Ship behind a feature flag; one service at a time; CI runs each service's startup test |
| Service-backed paper-spine replay exposes long-tail timing bugs | Run replay in CI behind `make test-e2e` for one week before merging; have a fallback "fakeredis-only" mode |
| Backtester fidelity upgrade can introduce drift vs. paper path | Share a single `risk-rules` module between backtester and `services/risk/`; test on the same fixture |
| Auth migration breaks existing local dev workflows | Keep dev mode on localStorage until cookie path is proven; gate behind `FINCEPT_AUTH_MODE` |
| Provider evidence ledger can leak secrets if mis-configured | Define redaction rules *before* writing the writer; fixture tests for redaction |
| Documentation drift re-emerges if `STATUS.md` is not enforced | Add a docs-link-check to CI; run weekly |
| The dirty tree blocks any single large PR | Slice commits into Tier 0.1 → 0.4 in this order; each is a small, reviewable PR |

---

## 8. Suggested 30/60/90-day plan

**30 days — Tier 0 only.** Land V0.1, V0.2, V0.3, V0.4. Every PR should leave the system measurably more provable. End of 30 days: every service has the safety guard, the route smoke is green, the paper-spine replay is service-backed, and `scripts/verification-receipt.ps1` produces a single durable artifact.

**60 days — Tier 1 + start Tier 2.** Land V1.1, V1.2, V1.3, V1.4. Start V2.1 (backtester fidelity). End of 60 days: the system is internally trustworthy, has a documented mock-replacement queue, a strategy readiness gate, and a credible promotion dossier flow.

**90 days — Tier 2 + Tier 3.** Complete V2.1–V2.4. Land V3.1 (auth), V3.2 (provider ledger), V3.3 (hygiene), V3.4 (docs). End of 90 days: the platform is ready for an internal beta with non-build-team operators. Tier 4 is still not next.

---

## 9. Anti-recommendations (what *not* to do next)

In service of focus, here is what this report explicitly recommends **against**:

1. **Do not add a new dashboard page** until the route atlas (V1.2) is in place. The dashboard already has 20+ routes; adding more without a mock-replacement queue is debt.
2. **Do not wire news-impact-lab into the orchestrator loop** — even via shadow-on-active — until V1.4 (promotion dossier) exists. Shadow-only is the right default and is documented; do not weaken that boundary.
3. **Do not publish a `latest` Docker image tag for deploys** until supply-chain pinning (HIGH-005 in `SYSTEM_IMPROVEMENT_REPORT.md`) is done. `latest` is a foot-gun.
4. **Do not propose live trading gates** until Tier 0–3 are green and a risk-committee review has occurred. The roadmap is correct on this; preserve it.
5. **Do not start Tier 4 features** (multi-agent debate, options agents, RL execution). They are exciting and they are not next.
6. **Do not commit `tmp_*.out` / `tmp_*.err`** to the repo. Route them through `reports/` or delete them.
7. **Do not assume fakeredis proofs are live proofs.** Label every receipt with its substrate (`fakeredis`, `local-redis`, `service-backed`) and treat only the latter as a release gate.

---

## 10. Appendix A — Inventory of dashboard routes and their current readiness

Sources: `apps/dashboard/src/app/` directory listing (server-compiled), `SYSTEM_OVERVIEW.md §4`, `aestheticaudit.md` (2026-05-09), `SYSTEM_IMPROVEMENT_REPORT.md` HIGH-002.

| Route | First impression | Mock/Live/Hybrid | Primary backend | Notes |
|---|---|---|---|---|
| `/` | Strong first impression; KPI cards + sparkline | Hybrid; sparkline data posture flagged | Aggregator | Aesthetic audit: credibility drift risk |
| `/positions` | Clean render | Hybrid (MockBadge) | `/positions` API | Quieter than shell |
| `/orders` | Clean render with visible filters | Live | `/orders` API | Acceptable empty state |
| `/reconciliation` | Strong utilitarian surface | Live | `/reconciliation` API | One of the better utilitarian screens |
| `/strategies` `/strategies/[id]` | Consistent styling | Live | `/strategies/configs`, `/start`, `/stop`, `/history` | React ref warning in `row-actions.tsx` |
| `/models` `/models/[name]` | Better operational clarity than polish | Live (per §6) | `/models`, `/models/{name}/...` | Promote + shadow + feature importance live |
| `/predictions` | Extension of shell | Live | `/predictions`, `/models/{name}/predictions` | Threshold filters present |
| `/markets` | Useful control set | Hybrid | `/markets`, `/data/...` | Autopilot/Seed/Run demo controls |
| `/risk` | Appropriately high-stakes | Live | `/risk` API | Scenario/control naming strong |
| `/research` | Loads; sometimes API OFFLINE badge | Hybrid | `/research/openbb/...`, `/research/exa` | Live OpenBB with fallback |
| `/news` `/news-lab` | Clean render | Hybrid (key-gated) | `/news`, `/news/...` | Doesn't separate from shell enough yet |
| `/news-impact-lab` | Strong workbench; empty pre-score | Shadow only | `/news-impact/...` | Right pane too empty pre-generation |
| `/portfolio-builder` `/optimizer` | Strong concept; empty right pane | Hybrid | Portfolio builder API | Idle state too empty; rich when populated |
| `/signal-cockpit-demo` | Best contrast surface | Demo | Demo data | Reference surface per aesthetic audit |
| `/receipts` `/system` | Operator utility | Live (file-backed) | Local receipts | Important for V0.4 |
| `/watchlist` | Mock-heavy | Mock | None yet | V1.2 candidate |
| `/symbol/[symbol]` | Multiple MockBadge surfaces | Mock | None yet | V1.2 candidate |
| `/login` | Bearer-token paste | Dev only | Auth | V3.1 candidate |
| `/backtest` | Run trigger + results | Live | `/backtest/runs` | V2.1 candidate |

## 11. Appendix B — Verification receipts status (today)

Source: `ROADMAP.md §17`, `PROJECT_OVERVIEW.md §10`, `SYSTEM_IMPROVEMENT_REPORT.md`.

| Receipt | Latest status | Substrate | Action |
|---|---|---|---|
| `reports/paper-spine/latest.json` | Passed, 11 assertions true | fakeredis + AAPL fixture | Promote to service-backed (V0.3) |
| `reports/route-smoke/route-smoke-20260506-211742.json` | 8/9 passed; `/data/coverage` timeout | Local API | Fix coverage latency, rerun (V0.2) |
| `reports/openbb-live/openbb-live-20260505-151250.json` | 1/3 passed; health OK, quote/dispatcher 503 | OpenBB backend not running | Split readiness states (HIGH-001 in SYSTEM_IMPROVEMENT_REPORT) |
| `reports/route-smoke/route-smoke-20260505-151250.json` | 9/9 passed | Local API | Show regression shape vs 06/05 receipt |
| Shadow news-impact dashboard tests | 3 passed | Dashboard unit | Make durable (HIGH-001 in SYSTEM_IMPROVEMENT_REPORT) |
| Source-health dashboard tests | 5 passed | Dashboard unit | Make durable (V0.4) |
| Strategy-readiness dashboard tests | 4 passed | Dashboard unit | Make durable (V0.4) |
| `uv run pytest services/api/tests/test_news_impact.py -q` | 6 passed | API unit | Make durable (V0.4) |
| `pnpm --dir apps/dashboard exec tsc --noEmit` | Passed | Typecheck | Make durable (V0.4) |

## 12. Appendix C — Cross-reference of major audit/review docs

| Document | Date | Use it for |
|---|---|---|
| `README.md` | rolling | Status overview + how-to-run |
| `docs/SYSTEM_OVERVIEW.md` | 2026-05-08 | Authoritative architecture description |
| `docs/ROADMAP.md` | 2026-05-09 | Pragmatic phase plan with weekly status updates |
| `docs/SYSTEM_IMPROVEMENT_REPORT.md` | 2026-06-21 | 4 critical issues + 5 high + 5 medium + 8 features |
| `docs/codebase-audit-2026-05-16.md` | 2026-05-16 | Focused security/boundary audit (OpenBB port, backtest path, training path) |
| `docs/aestheticaudit.md` | 2026-05-09 | Aesthetic audit of dashboard routes |
| `docs/nextlevelfeatures.md` | (rolling) | Backlog for P0/P1/P2 features |
| `docs/ui-feature-improvement-roadmap.md` | (rolling) | UI backlog |
| `docs/dashboard-route-atlas.md` | (rolling) | Per-route status (sparse today) |
| `docs/ui-audit-2026-05-16.md` | 2026-05-16 | UI audit (companion to codebase-audit) |
| `docs/text-readability-audit-2026-05-16.md` | 2026-05-16 | Text readability audit |
| `docs/datasources.md` | (rolling) | Datasource registry routing |
| `docs/portfoliooptimizer.md` | (rolling) | Portfolio builder/optimizer notes |
| `docs/openbb-research-handoff.md` | (rolling) | OpenBB research notes |
| `docs/RAILWAY_STAGING_GUIDE.md` | (rolling) | Railway deployment notes |
| `docs/AWS_PRODUCTION_CONTROL_PLANE.md` | (rolling) | AWS production plan |
| `spec/ARCHITECTURE.md` | initial | One-page architecture mental model |
| `spec/CONTRACTS.md` | initial | Typed event contracts |
| `spec/BUILD_ORDER.md` | rolling | Sequenced task graph with checkpoints |
| `spec/EDGE_ROADMAP.md` | initial | Phase X+ alpha thesis |

---

## 13. Closing note

Fincept Terminal has done the hard architectural work well. The event-sourced spine, the typed contracts, the seven-agent layer, the ML lifecycle, the strategy host, and the dashboard identity are all in place and individually credible. What stands between the system and "operator-trusted internal alpha platform" is a focused Tier 0 push to make the existing pieces provably correct as a connected whole, a Tier 1 push to make the safety and readiness story honest, and a Tier 2 push to make the backtester and model-validation story credible. None of those require new architecture. They require proof, tightening, and discipline.

Tier 3 makes the system legible to outside operators. Tier 4 — the genuinely exciting multi-agent debate, options-flow agents, RL execution, federated learning — is the *future*, not the *next*. Hold the line on that ordering and the project will compound; flip it and the existing rigor becomes the foundation of a more interesting future than the original blueprint promised.
