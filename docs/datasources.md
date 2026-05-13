# Fincept Datasource Registry

Last updated: 2026-05-02

This document is the operator-facing map for data and research sources. Keep it aligned with `GET /data/sources`, dashboard types, and tests whenever a provider, table, or route changes.

## Current Sources

| Source | Primary surface | Safety tier | Health signal | Operator proof to add |
|---|---|---|---|---|
| Exa | `/research/exa`, `fincept_tools.research.exa` | Read-only research | API key present, last request ok, structured source-grounded response | Usage receipt with caller, route, input hash, output hash, latency, and error type. |
| OpenBB | `/research/openbb`, `/research/openbb/quote`, `/research/openbb/health` | Read-only market data | Local OpenBB API probe, provider-key availability, health history | Short health timeout, preset registry, blocked-path tests, provider status cards. |
| Timescale bars | `/data/bars/{symbol}`, `/data/coverage` | Read-only local timeseries | DB reachable, latest bar timestamp, count and freshness by symbol | Batch coverage query, safe public errors, freshness history. |
| Universe and symbol search | `/data/universe`, `/data/symbols/search` | Read-only metadata | Universe rows available and response contract stable | Resolve `venue_default` vs `venue` contract and add frontend type parity tests. |
| Redis | Rate limits, marks, cached state | Internal state | Ping, stream/cache availability, rate-limit bucket state | Include Redis availability in route smoke receipts and provider health display. |
| Alpaca | Paper broker/data adapters | Paper-first broker surface | Credentials present, paper account reachable | Keep live paths fail-closed; record paper connectivity separately from live readiness. |
| Binance | Crypto market-data adapters | Read-only market data | Public endpoint reachable, subscriptions healthy | Add provider-specific stale-data and retry notes before strategy enablement. |
| Local predictions | `data/predictions`, model API surfaces | Internal model output | Artifact present, model metadata readable, forward labels available | Record model/data window and calibration state before use in strategies. |
| News impact model | `/news-impact/*`, `experiments/news-impact-model` | Shadow research only | Workbench sample data and deterministic report output | Promotion dossier with calibration, drawdown impact, and explicit no-order-route assertion. |

## Contract Rules

- Response keys must match dashboard TypeScript types. When a backend uses `venue_default`, the frontend should not silently type it as `venue`.
- Health endpoints should return stable public error codes and short operator messages. Raw exception text belongs in server logs only.
- Omitted provider or venue filters should have explicit semantics. For coverage, omitted `venue` should mean either all venues or documented default venue behavior, with tests.
- Every source that can cost money, rate-limit, or influence strategy state needs a usage receipt.

## Next Implementation Slice

1. Fix universe and coverage venue semantics.
2. Add batch coverage reads plus freshness history.
3. Add a port-`8010` smoke command that probes `/data/sources`, `/data/coverage`, symbol search, OpenBB health, strategy configs, and orders.
4. Render the datasource registry in the dashboard as a provider health/control center.
