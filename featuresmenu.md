# Fincept Terminal Features Menu

Last updated: 2026-04-28

## Local Analysis Snapshot

The local project is no longer just planning docs. It has a monorepo scaffold with uv workspace members, pnpm dashboard workspace, Docker services, Makefile targets, implementation specs, and task prompts. The actual runtime is still mostly empty stubs, so recommendations below focus on turning the scaffold into a measurable paper-trading platform before adding expensive model complexity.

## Innovative Features To Add

| Feature | Build next action | Why it fits this repo | Dependencies | Risk |
|---|---|---|---|---|
| Contract replay harness | Generate JSON fixtures from `spec/CONTRACTS.md` and replay `md.*`, `sig.*`, and `ord.*` streams through fake services. | The contracts are already the source of truth; this makes every future service testable before real market data. | `fincept-core`, pytest fixtures, Redis Streams test adapter. | Medium: contract drift if fixture generation is manual. |
| Latency budget ledger | Add a per-event `latency_trace` object that records ingest, normalize, store, feature, signal, risk, and OMS timestamps. | The roadmap has latency gates, but no local mechanism to prove them. | Core schemas, bus envelope, observability hooks. | Low: adds fields and discipline early. |
| Paper-trading black box recorder | Persist every decision, risk result, simulated fill, position update, and source signal into immutable JSONL plus Timescale tables. | Makes audit, debugging, and forward-return labeling possible from day one. | `ord.*` stream, OMS state, jobs archive. | Medium: storage volume needs retention policy. |
| Regime stress test pack | Add canned 2008, 2020, 2022, crypto crash, and high-volatility replay profiles for backtests and risk gate tests. | Keeps the system risk-first instead of only chasing headline Sharpe. | Backtester datasource, cost model, risk limits. | Medium: scenario data quality matters. |
| Strategy autopsy reports | After each backtest or paper run, emit a plain-English report: edge source, worst trades, regime failures, cost drag, and kill-switch triggers. | Converts the platform from raw metrics into an operator learning loop. | Backtester report, portfolio attribution, LLM optional. | Low: can start deterministic without LLM. |
| Agent calibration board | Track prediction confidence buckets versus realized forward returns at 5m, 30m, 1h, and 1d. | Prevents agents from sounding confident without being calibrated. | Prediction schema, labels job, dashboard panel. | Medium: needs enough observations. |
| Tool-use sandbox for research agents | Restrict LLM agents to typed tools in `fincept-tools`, recording tool inputs/outputs and banning direct order paths. | Matches the architecture boundary: agents propose, risk/OMS execute. | Tool protocol, audit log, orchestrator. | Medium: prompt/tool security must be explicit. |
| Human kill-switch drill mode | Add weekly simulated failure drills where the UI injects bad fills, stale market data, and risk breaches for operator practice. | Makes risk controls operational, not decorative. | Dashboard, risk gate, paper OMS. | Low: paper-only until proven. |
| Feature lineage graph | Store which raw events produced each feature row and which features fed each signal. | Helps debug model drift and bad data faster than looking at logs. | Feature store, event IDs, dashboard graph. | Medium: lineage can get large. |
| Capital allocator simulator | Compare equal weight, volatility target, drawdown throttle, and Kelly variants before any live mode exists. | The roadmap already includes Kelly; this builds the safer comparison harness first. | Backtester, portfolio service, risk gate. | Medium: false precision if costs are weak. |

## Roadmap Placement

1. Add contract replay and latency ledger to Phase F before beginning real venue adapters.
2. Add black box recorder and stress test pack before Phase B checkpoint.
3. Add calibration board, strategy autopsy, and feature lineage during Phase U dashboard work.
4. Keep tool-use sandbox and capital allocator behind paper-only controls until the end-to-end loop is stable.

## Next Skills To Deepen

| Skill | Concrete practice target | Local artifact to create |
|---|---|---|
| Event-sourced Python systems | Implement Redis Streams producer/consumer with idempotent handlers and replay tests. | `libs/fincept-bus` plus `tests/fixtures/replay/`. |
| Financial data modeling | Encode Decimal-safe market/order schemas and Timescale hypertables without float leakage. | `libs/fincept-core` and `libs/fincept-db` migrations. |
| Backtesting rigor | Build deterministic event replay with transaction costs, slippage, and point-in-time feature joins. | `services/backtester` reference run. |
| Risk engineering | Turn max notional, concentration, drawdown, and kill-switch rules into tested gates. | `services/risk/gate.py` with scenario tests. |
| Operator UX | Design dense, low-latency dashboard panels for P&L, positions, agent confidence, and emergency controls. | `apps/dashboard` shell and websocket panels. |
| Model calibration | Score every prediction bucket against forward returns and show degradation by regime. | `services/jobs/label_forward_returns.py` and dashboard calibration board. |

## Automation Additions — 2026-04-26 07:01 America/Chicago

| Feature | Build next action | Why it fits this repo | Dependencies | Risk |
|---|---|---|---|---|
| Schema drift sentinel | Add CI checks that compare `spec/CONTRACTS.md` examples, `fincept-core` pydantic/msgspec models, and replay fixtures. | The repo is spec-first; drift would silently invalidate the whole build order. | Task 002, pytest, fixture examples. | Low-medium: requires disciplined examples. |
| Paper-only order guard | Add `execution_mode`, `live_confirmed_by`, and `live_confirmed_at` fields to decision/order contracts with tests that reject live mode by default. | The architecture should stay paper-first even after OMS code exists. | Core schemas, risk gate, OMS. | Low: cheap if added before OMS. |
| Event provenance hash | Add a deterministic hash over source market events, feature rows, signal inputs, and risk result for every order decision. | Makes black-box recorder and post-run labeling auditable. | Core IDs, bus envelope, risk decision schema. | Medium: hash inputs must be stable. |
| Shadow-vs-paper comparator | Run an agent in shadow mode and compare its hypothetical fills to paper OMS fills without allowing order submission. | Lets agent work begin without weakening execution safety. | Backtester, paper OMS, jobs service. | Medium. |
| Regime-aware kill switch | Define separate limits for crash, trend, chop, illiquid, and data-stale regimes before sizing changes. | Matches the risk-first objective and prevents one global limit from hiding bad regimes. | Risk gate, regime detector, scenario fixtures. | Medium: regime classifier can be wrong. |
| Operator decision journal | Add a local journal where human overrides, kill-switch actions, and strategy restarts are timestamped and linked to market state. | Paper trading needs operator learning, not just system logs. | API, dashboard control actions, audit log. | Low. |

## Skill Progression Map

| Skill to deepen | First concrete exercise | Done when |
|---|---|---|
| Contract-first Python | Implement `MarketEvent`, `SignalEvent`, `DecisionEvent`, `OrderEvent`, and `FillEvent` in `libs/fincept-core`. | Fixture replay validates all event types with Decimal-safe prices and explicit clocks. |
| Event replay testing | Build replay fixtures for `md.*`, `sig.*`, and `ord.*`. | A failing handler can be reproduced from one fixture file. |
| Trading safety engineering | Encode paper-only order submission and second live confirmation in schemas before OMS code exists. | Tests prove live mode is rejected without explicit second confirmation fields. |
| Audit-grade observability | Add provenance hashes and latency traces to bus envelopes. | One decision can be traced from market event to risk result to simulated fill. |
| Repo hygiene | Isolate this folder as its own Git repo or document why it is intentionally under `C:/Users/nolan`. | `git status --short -- .` reports only Fincept Terminal files. |

## Automation Additions — 2026-04-27 America/Chicago

| Feature | Build next action | Why it fits this repo | Dependencies | Risk |
|---|---|---|---|---|
| Tool-call black box | In `fincept-tools`, wrap every tool invocation with caller ID, run ID, input hash, output hash, duration, and explicit side-effect classification. | The next task is tools; adding audit metadata now prevents opaque agent behavior later. | `TASK-005`, core IDs, audit tables. | Low-medium: needs consistent wrapper discipline. |
| Paper-exec capability firewall | Split tools into `read`, `analysis`, `paper_exec`, and `blocked_live_exec`, then make live execution unimportable unless a separate confirmation module is present. | Keeps the paper-first safety model enforceable at import time, not just by convention. | `fincept-tools`, order schemas, risk gate. | Low if done before OMS. |
| CI parity launcher | Add a local command that mirrors `.github/workflows/ci.yml`: ruff, format check, mypy, alembic upgrade, pytest, pnpm checks, and secret scan. | The CI workflow exists but needs a repeatable local preflight before push. | Task 006, Docker dev stack, uv, pnpm. | Medium: Windows/Linux parity can drift. |
| Redis-to-Timescale replay drill | Publish fixture market events through Redis Streams, consume them, write ticks/bars into Timescale, and assert deterministic readback. | Tasks 002-004 now exist separately; this proves the spine works as one system. | Redis service, Timescale service, bus consumer, db writers. | Medium: requires service orchestration. |
| Schema version covenant | Add `schema_version` and migration notes to event payloads; fail tests when version changes without a compatibility note. | The project is contract-first; versioning needs to start before external adapters generate data. | Core schemas, docs, replay fixtures. | Low. |
| Run-quality receipt | After every test/backtest/paper run, emit a compact receipt with git hash, config hash, data window, skipped checks, and pass/fail gates. | The current test run had meaningful skips; receipts make those boundaries visible. | Core IDs, jobs service, CI artifacts. | Low. |
| Tool sandbox fuzz pack | Fuzz malformed tool arguments, unknown symbols, bad decimals, stale timestamps, and oversized payloads before agent tool use begins. | Prevents future LLM agents from discovering untested tool edges. | `fincept-tools`, Hypothesis or custom fixtures. | Medium. |

## Next Skills To Deepen — 2026-04-27

| Skill to deepen | First concrete exercise | Done when |
|---|---|---|
| Tool-protocol design | Implement typed `fincept-tools` registry with side-effect classes and audit wrappers. | A paper order tool can be called in tests and produces an audit record without any live venue import. |
| CI/service-container debugging | Run Redis and Timescale-backed tests locally, then align failures with `.github/workflows/ci.yml`. | The 11 skipped DB/Redis tests either pass locally or are documented as CI-only with reasons. |
| Cross-library integration testing | Build the Redis-to-Timescale replay drill using current core schemas. | One fixture proves event serialize, bus consume, DB write, and DB readback in order. |
| Financial safety controls | Encode capability firewalls before OMS work. | Live execution paths fail closed at import/config/test time. |

## Automation Additions — 2026-04-28 America/Chicago

| Feature | Build next action | Why it fits this repo | Dependencies | Risk |
|---|---|---|---|---|
| Tool manifest compiler | Add a manifest object for every `fincept-tools` function with side-effect class, capability, schema version, and audit policy. | The tools package exists as a stub; a manifest-first implementation keeps later agents inspectable. | `TASK-005`, pydantic/msgspec schemas, audit hash helper. | Low. |
| Paper order dry-run lens | Implement one paper order proposal tool that returns a proposed `OrderEvent`, risk precheck status, and audit receipt without touching a venue. | It proves the paper-first boundary before OMS or live adapters exist. | Core order schemas, tool registry, audit wrapper. | Low-medium. |
| Service skip explainer | Extend preflight output or pytest reporting so skipped Redis/Timescale checks list the exact missing service and command to enable it. | The current verification boundary is defined by skipped service-backed tests. | `scripts/preflight.ps1`, pytest markers, Docker Compose. | Low. |
| Event causality DAG | Record parent event IDs from core event, Redis stream ID, DB row ID, and tool run ID into a queryable graph/table. | Tasks 002-004 are separate; this shows whether the spine is causally reconstructable. | Core IDs, bus consumer, DB audit table, tools registry. | Medium. |
| Secret-safe preflight profile | Add an env validation phase that checks required variables by name and shape but never prints values. | CI and local scripts now touch secret scanning and service configs; safer diagnostics avoid token leakage. | `.env.example`, preflight script, gitleaks. | Low. |
| Tool replay cassette | Persist tool request/response/audit triples as JSON fixtures that can be replayed without external services. | Gives `fincept-tools` deterministic tests before real data adapters exist. | Tool registry, audit wrapper, fixture directory. | Medium. |

## Next Skills To Deepen — 2026-04-28

| Skill to deepen | First concrete exercise | Done when |
|---|---|---|
| Manifest-driven tool design | Build `manifest.py` and make tests assert every tool has schema, side-effect, capability, and audit metadata. | Adding a tool without a manifest entry fails tests. |
| Fail-closed execution safety | Implement `blocked_live_exec` as an explicit capability class and prove it cannot be imported through normal registry loading. | A live execution test fails closed before runtime config is read. |
| Verification reporting | Add a preflight summary that separates pass, fail, skipped, and not-installed checks. | A user can tell whether Redis, Timescale, JS, secret scan, or Python checks blocked Foundation completion. |
| Financial event traceability | Connect one tool run ID to one core event ID, one Redis stream ID, and one DB audit row. | A replay fixture can reconstruct the decision path from one ID. |
