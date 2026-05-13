# OpenBB + Research Integration Handoff

Last updated: 2026-05-08

## Summary

Fincept Terminal now has a read-only research/data path that combines:

- Exa web research briefs on the `/research` page.
- OpenBB quote lookup on the same `/research` page.
- A backend OpenBB tool that prefers the local OpenBB Platform API and falls back to the Python package only if needed.
- OpenBB health/readiness diagnostics that separate API reachability from provider-backed quote/fundamental failures.

The goal was to let Fincept use OpenBB without forcing the whole OpenBB extension ecosystem into Fincept's own virtual environment.

## What Changed

### OpenBB Tool

Added an OpenBB-backed tool:

- `libs/fincept-tools/src/fincept_tools/research/openbb.py`
- Tool name: `research.openbb_quote`
- Input: `symbol`, `provider`
- Output: `ok`, `provider`, `results`, `error`, `error_type`

The tool call order is:

1. Call local OpenBB API at `OPENBB_API_URL`.
2. If unavailable, try the in-process Python `openbb` package.
3. If both are unavailable, return structured `OpenBBUnavailable`.

Default local API:

```text
OPENBB_API_URL=http://127.0.0.1:6900
```

### API Route

Added an authenticated FastAPI route:

```text
POST /research/openbb/quote
```

The API also exposes:

```text
GET /research/openbb/health
GET /research/openbb/health/history
GET /research/openbb/readiness?symbol=NVDA&provider=yfinance
POST /research/openbb
```

Use `/health` for a fast local OpenBB API process check. Use `/readiness` when
you need provider diagnostics: it checks `openapi`, quote, and fundamentals
paths independently and returns per-check `ok`, `error_type`, latency, provider,
and result-shape metadata.

Request:

```json
{
  "symbol": "NVDA",
  "provider": "yfinance"
}
```

Success-style response:

```json
{
  "ok": true,
  "provider": "yfinance",
  "results": [
    {
      "symbol": "NVDA",
      "last_price": 900.12
    }
  ]
}
```

Unavailable response:

```json
{
  "ok": false,
  "error_type": "OpenBBUnavailable",
  "error": "OpenBB API is not reachable...",
  "provider": "yfinance",
  "results": []
}
```

### Dashboard

Updated the Research page:

- `apps/dashboard/src/app/research/page.tsx`
- Added an OpenBB quote panel.
- Added API client method `openbbQuote`.
- Added TypeScript types for `OpenBBQuoteRequest` and `OpenBBQuoteResponse`.

The Research page now supports both:

- Exa structured research briefs.
- OpenBB market quote snapshots.

### Configuration

Added OpenBB API configuration:

- `.env`
- `.env.example`

```env
OPENBB_API_URL=http://127.0.0.1:6900
```

### Safer Tool Package Imports

Changed `libs/fincept-tools/src/fincept_tools/__init__.py` so importing `fincept_tools` no longer auto-loads every tool family.

Before, `import fincept_tools` eagerly imported analytics, data, research, and execution tools. That caused startup and test hangs when one tool family dragged in heavier dependencies or order-path side effects.

Now:

- `import fincept_tools` exposes protocol and registry only.
- Tool families register when imported explicitly, such as:

```python
import fincept_tools.research
import fincept_tools.data
import fincept_tools.analytics
import fincept_tools.exec
```

This keeps Research/OpenBB startup read-only and isolated.

## OpenBB Desktop Setup

The Open Data Platform app showed:

- OpenBB API: `openbb-api --host 127.0.0.1 --port 6900`
- OpenBB MCP: `openbb-mcp --transport streamable-http --host 127.0.0.1 --port 8001`

Fincept currently uses the OpenBB API backend on port `6900`.

To make live OpenBB quote calls work:

1. Open the Open Data Platform desktop app.
2. Go to **Backends**.
3. Press **Start** on **OpenBB API**.
4. Keep Fincept API running on `8010`.
5. Use the OpenBB quote panel on `http://localhost:3000/research`.

The OpenBB MCP backend is useful later for agent/tool workflows, but the current Fincept integration uses REST.

## How To Run

Start or restart the Fincept API with BLAS thread guards:

```powershell
$env:OPENBLAS_NUM_THREADS='1'
$env:OMP_NUM_THREADS='1'
$env:MKL_NUM_THREADS='1'
uv run --package api uvicorn api.main:app --port 8010
```

Dashboard:

```powershell
cd apps/dashboard
npm run dev
```

Then open:

```text
http://localhost:3000/research
```

## Verification Completed

The following checks passed after the infrastructure changes:

```text
fincept-tools: 91 passed
Ruff: clean
Mypy: clean
services/api/tests/test_research.py: 4 passed
Dashboard typecheck: clean
```

Live route probe also returned a structured response:

```text
POST http://127.0.0.1:8010/research/openbb/quote
```

At the time of the probe, OpenBB API was not running, so the response correctly returned `OpenBBUnavailable` rather than crashing.

## Known Current State

- Fincept API is configured for `OPENBB_API_URL=http://127.0.0.1:6900`.
- OpenBB API must be started from the Open Data Platform app before live quote rows appear.
- Fincept does not currently require the `openbb` Python package in its own `.venv`.
- If the local OpenBB API is down and the Python package is not installed, the UI will show a structured unavailable message.

## Next Useful Steps

1. Start OpenBB API from the Open Data Platform app and run `uv run python scripts/openbb_live_proof.py --symbol NVDA`.
2. Use `/research/openbb/readiness` when a live proof fails to distinguish API-down from provider-specific quote/fundamental failures.
3. Add provider-specific API keys inside Open Data Platform if specific premium OpenBB extensions need them.
4. Surface the readiness checks in the dashboard so operators can see which OpenBB provider/path failed.
5. Later, wire OpenBB MCP on `8001` for agent workflows if we want Codex/LLM tools to browse OpenBB capabilities dynamically.
