# Goals and Scope

## Confirmed Goals

- Build an internal AI-agentic stock and crypto research plus paper-trading
  terminal.
  Evidence: `README.md`, `docs/ROADMAP.md`, and `apps/dashboard/README.md`.

- Keep the near-term system paper-first.
  Evidence: `docs/ROADMAP.md` says paper trading before capital; `Settings.TRADING_MODE`
  defaults to `paper`; `oms` includes a paper simulator and an Alpaca paper
  adapter.

- Use a Python-first service stack with a web dashboard.
  Evidence: `pyproject.toml` defines a uv Python workspace; `apps/dashboard`
  is a Next.js 14 application; `docs/ROADMAP.md` rejects Qt6 for the MVP.

- Separate shared contracts from service runtime logic.
  Evidence: `libs/fincept-core`, `libs/fincept-bus`, `libs/fincept-db`,
  `libs/fincept-tools`, and `libs/fincept-sdk` are separate workspace members.

- Preserve typed event boundaries over Redis Streams.
  Evidence: `libs/fincept-bus/src/fincept_bus/streams.py`, `spec/ARCHITECTURE.md`,
  and service modules that consume/publish `md.*`, `sig.*`, `ord.*`, and alert
  streams.

- Provide local proof receipts for important operator flows.
  Evidence: `scripts/paper_spine_replay.py`, `scripts/route_smoke.py`,
  `scripts/openbb_live_proof.py`, and receipts under `reports/`.

## Inferred Goals

- Make future agentic coding safer by encoding architecture boundaries,
  contracts, and validation commands in docs and scripts.
  Evidence: `spec/tasks/`, `spec/prompts/`, `scripts/task-check.ps1`, and the
  requested documentation pass.

- Build toward a governance-ready live trading platform eventually, while
  deliberately not enabling live capital now.
  Evidence: Phase H in `docs/ROADMAP.md`, live-execution warnings in tools and
  OMS code, and hardening tasks around secrets, mTLS, audit archival, and staged
  rollout.

- Use dashboard operator rails rather than free-form autonomous actions.
  Evidence: dashboard pages for risk, system readiness, receipts, strategies,
  reconciliation, and structured AI portfolio reports.

## Out of Scope

Confirmed out of scope for the current MVP:

- Live capital execution.
- HFT latency targets such as sub-100us tick-to-trade.
- FPGA or kernel-bypass execution.
- Native Qt6 Bloomberg-style terminal.
- Multi-tenant auth and external customer API.
- Full Bloomberg replacement scope.
- Regulatory live-trading hardening.

Evidence:

- `docs/ROADMAP.md` explicitly cuts these from the MVP.
- `apps/dashboard/README.md` says Phase H replaces localStorage auth with
  OAuth/httpOnly cookies.
- `docs/SYSTEM_OVERVIEW.md` says live execution and Phase H concerns are not
  changing in the current proof phase.

## Success Criteria

Near-term success criteria:

- `scripts/preflight.ps1` or the equivalent CI gates pass for Python and
  dashboard work.
- `scripts/route_smoke.py --base-url http://127.0.0.1:8010` records a green or
  intentionally degraded receipt without timeouts.
- `scripts/paper_spine_replay.py` remains green and is promoted from fakeredis
  proof toward service-backed Redis/Timescale proof.
- Dashboard TypeScript checks pass after UI changes.
- Any route or schema change has corresponding tests and keeps the typed
  dashboard client aligned with FastAPI responses.
- Research and news-impact routes remain read-only or shadow-only until they
  have calibration, governance, and side-effect receipts.

## Open Questions

- Should paper-spine replay become a CI gate, and if so should it remain
  deterministic/fakeredis first or require service containers?
- Should `/data/coverage` be optimized, bounded, or split into fast summary and
  slower detail endpoints?
- Should backtest and model training input paths be restricted to approved data
  roots before more dashboard workflows are exposed?
- Should auth move to httpOnly cookies before more state-changing endpoints are
  added?
- Which generated artifacts under `reports/`, `models/`, and `data/` should be
  kept as ignored receipts versus promoted to tracked fixtures?
