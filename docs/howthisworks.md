# How Fincept Terminal Works

Fincept Terminal is a paper-first AI trading operator terminal. It combines Python services, Redis Streams, Postgres/Timescale-oriented storage, optional external research/data providers, and a Next.js dashboard.

## Runtime loop

```text
market data -> features -> agents -> orchestrator -> risk -> OMS -> fills -> portfolio -> dashboard
```

- **Market data:** ingestors normalize venue data into canonical events.
- **Features:** feature services consume bars and publish point-in-time `FeatureFrame` records to `features.online`.
- **Agents:** GBM, sentiment, and regime components publish predictions/signals when their models and API keys are available.
- **Orchestrator:** combines fresh predictions into decisions and order intents.
- **Risk:** owns pre-trade checks and kill-switch behavior.
- **OMS:** simulates or paper-routes orders and publishes fills.
- **Portfolio:** rolls fills into positions and P&L.
- **API/dashboard:** expose read models, controls, service health, strategy config, model state, and operator workflows.

## Services

Core expected services:

- `api`
- `ingestor`
- `features`
- `orchestrator`
- `oms`
- `portfolio`
- `jobs`

Optional/key-gated services:

- `gbm_predictor` when `models/gbm_predictor/model.txt` exists.
- `sentiment_agent` when `NEWSAPI_API_KEY` and either `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` are configured.
- `regime_agent` when `FRED_API_KEY` is configured.

Strategy runtime:

- `strategy_host` watches filesystem-backed configs under `strategies/`.
- Each config records `strategy_id`, `class_name`, `symbols`, `params`, optional `model_binding`, and `enabled`.
- API writes append audit history to `strategies/<id>.history.jsonl`.

## Dashboard pages

- **Overview** — platform summary.
- **Positions** — live per-strategy positions.
- **Orders** — order audit and manual order entry.
- **Strategies** — config CRUD, params, lifecycle toggles, history, model binding.
- **Optimizer** — portfolio-builder workflows.
- **News** — news surfaces.
- **News Lab** — experimental news-impact model UI.
- **Predictions** — prediction stream and agent outputs.
- **Markets** — universe, symbol search, bars, and coverage.
- **Backtest** — historical replay UI.
- **Models** — training runs, active models, promotion/shadow controls.
- **Risk** — kill switch, exposure, alerts, and service health.

## Local startup

```pwsh
.\scripts\start.ps1
.\scripts\start.ps1 -WithGbm
.\scripts\start.ps1 -NoServices
.\scripts\stop.ps1
.\scripts\status.ps1
```

The API defaults to `http://localhost:8010`; the dashboard defaults to `http://localhost:3000`.

## Safety posture

- The platform is paper-first.
- Live capital requires an explicit gate, not just a config flip.
- Alpaca is the primary brokerage direction for paper/live brokerage integration.
- External data/research providers must be documented with safety tier, health check, return shape, and rate-limit behavior.
