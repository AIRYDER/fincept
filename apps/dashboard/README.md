# Fincept Dashboard

Operator console for the Fincept paper-trading and research platform. Built with
Next.js 14 (App Router), Tailwind, Radix UI primitives, TanStack Query, Zustand,
Recharts, and Framer Motion.

## What ships

- **Auth** — JWT bearer token (paste in `/login`); stored in
  `localStorage` for v1.
- **Live overview** — equity, unrealized P&L, open positions, recent
  fills, real-time activity feed (predictions + fills + alerts).
- **Positions** — per-(strategy, symbol) live table; pulses on
  WebSocket update; long/short/flat badges; per-row P&L breakdown.
- **Orders** — newest-first audit-log materialisation, status filters,
  search.
- **Strategies** — registry + per-strategy realized/unrealized/fees
  rollups.
- **Predictions** — live tiles/cards per (agent, symbol) showing direction,
  confidence, horizon, stream views, and signal inspection surfaces for agent
  outputs.
- **Markets** — universe browser + 1m/1h/1d bar chart (Recharts).
- **Risk** — kill-switch (`POST/DELETE /kill-switch`); per-symbol &
  gross exposure usage bars; live alert feed.
- **Command palette** — `⌘ K` opens; type a page, mnemonic
  (OV/PS/OR/ST/PR/MK/RK), or "kill switch".
- **Models** — model lifecycle views for registry status, promoted models,
  prediction details, and model-specific drilldowns.
- **Research and news** — `/research`, `/news`, `/news-lab`, and
  `/news-impact-lab` connect source discovery, OpenBB/Exa-style research flows,
  and experimental news-impact modeling.
- **Reconciliation** — operator workflow for comparing orders, fills, positions,
  and internal ledger assumptions.
- **Portfolio builder** — `/portfolio-builder` builds deterministic allocations
  and requests an AI-readable investment committee packet from Auto, GPT-5.5, or
  Claude Opus 4.7. `/optimizer` redirects here for compatibility.
- **Signal cockpit demo** — experimental graph/cockpit UI concept derived from
  the agent UI analysis docs.

## Run

Set the API URL. The current local backend default is `http://localhost:8010`:

```pwsh
copy .env.example .env.local
```

Then:

```pwsh
pnpm install
pnpm --filter @fincept/dashboard dev
```

Open <http://localhost:3000> → paste a JWT minted via
`api.auth.encode_token({"sub": "operator"})` in the API service.

For a repo-level startup flow, prefer:

```pwsh
powershell -ExecutionPolicy Bypass -File .\scripts\start.ps1
```

## Verify

From `apps/dashboard`:

```pwsh
pnpm exec tsc --noEmit --pretty false
```

From the repo root, run the broader local preflight when you need parity with CI:

```pwsh
powershell -ExecutionPolicy Bypass -File .\scripts\preflight.ps1
```

## Troubleshooting

- **Build error with random terminal text in a `.tsx` file** — server output was
  likely pasted into source. The page will fail before Next can compile. Remove
  the pasted log block and rerun `pnpm exec tsc --noEmit --pretty false`.
- **API calls hit the wrong port** — confirm `.env.local` uses
  `NEXT_PUBLIC_API_URL=http://localhost:8010` unless you intentionally started
  the API elsewhere.
- **Portfolio report falls back to local output** — set `OPENAI_API_KEY` or
  `FINCEPT_OPENAI_API_KEY` for GPT-5.5, and `ANTHROPIC_API_KEY` or
  `FINCEPT_ANTHROPIC_API_KEY` for Claude Opus 4.7.

## Architecture notes

- All schemas mirror `libs/fincept-core/.../schemas.py` in
  `src/lib/types.ts`.  Decimals come over the wire as strings.
- Numeric tabular columns use `font-variant-numeric: tabular-nums` so
  digits don't dance as values change.
- WebSocket reconnects with exponential backoff (capped at 15 s);
  reconnect re-sends the topic subscription frame.
- React Query has aggressive refetch on focus (5 s stale time),
  retries 3× except on 401 (which forces sign-in).
- The kill switch button in the topbar is a hard navigate to `/risk`
  so the operator can see exposure context before tripping it.

## Conventions

- Long → green (hsl 142 71% 45%), Short → red (hsl 0 84% 60%),
  Warning → amber (hsl 38 92% 55%).
- Bloomberg-style two-letter mnemonics for nav where practical.
- All pages render inside `<AppShell>` for the sidebar + topbar +
  command palette.
- AI-assisted UX should prefer structured operator rails, source/evidence
  disclosure, and explicit safety state over open-ended chat.

Phase H replaces the localStorage token with httpOnly cookies + OAuth.
