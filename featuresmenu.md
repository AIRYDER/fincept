# Fincept Terminal Features Menu

Last updated: 2026-04-26

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
