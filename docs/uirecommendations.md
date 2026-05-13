# UI Recommendations

Fincept Terminal's dashboard is the operator console for a paper-first trading system. The UI should prioritize safety, state clarity, and auditability over decorative complexity.

## Current navigation

- Overview (`OV`)
- Positions (`PS`)
- Orders (`OR`)
- Strategies (`ST`)
- Optimizer (`PO`)
- News (`NW`)
- News Lab (`NL`)
- Predictions (`PR`)
- Markets (`MK`)
- Backtest (`BT`)
- Models (`ML`)
- Risk (`RK`)

## Design priorities

- **State first:** always show whether the platform is paper, sim, or live-gated.
- **Freshness everywhere:** bars, predictions, model artifacts, service heartbeats, and external provider health need timestamps.
- **Safe writes:** order placement, strategy start/stop, model promotion, shadow activation, and kill-switch actions need explicit confirmation and audit history.
- **Contract visibility:** show raw IDs where operators debug systems: `strategy_id`, `agent_id`, `model_name`, `symbol`, stream names, and timestamps.
- **Failure transparency:** distinguish missing config, provider unavailable, stale data, and permission/auth failures.

## Page-specific recommendations

- **Strategies:** keep create/edit/delete/lifecycle dialogs tied to filesystem-backed strategy configs and show history from `strategies/<id>.history.jsonl`.
- **Models:** make active vs shadow vs historical artifacts visually distinct. Promotion should never be confused with training.
- **Markets:** datasource coverage should show freshness, provider, symbol, frequency, and error state without exposing raw exceptions.
- **Research:** clearly label Exa/OpenBB results as external research, not internal truth.
- **Risk:** keep service health and kill-switch context together so the operator knows whether a risk action is responding to stale services, exposure, or model behavior.
