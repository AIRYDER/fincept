# Fincept Dashboard

Operator console for the Fincept paper-trading platform.  Built with
Next.js 14 (App Router), Tailwind, Radix UI primitives, TanStack
Query, Zustand, Recharts, and Framer Motion.

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
- **Predictions** — live tiles per (agent, symbol) showing direction,
  confidence, horizon; scrolling stream feed.
- **Markets** — universe browser + 1m/1h/1d bar chart (Recharts).
- **Risk** — kill-switch (`POST/DELETE /kill-switch`); per-symbol &
  gross exposure usage bars; live alert feed.
- **Command palette** — `⌘ K` opens; type a page, mnemonic
  (OV/PS/OR/ST/PR/MK/RK), or "kill switch".

## Run

Set the API URL (defaults to `http://localhost:8000`):

```pwsh
copy .env.example .env.local
```

Then:

```pwsh
pnpm install
pnpm --filter @fincept/dashboard dev
```

Open http://localhost:3000 → paste a JWT minted via
`api.auth.encode_token({"sub": "operator"})` in the API service.

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
- Bloomberg-style two-letter mnemonics for nav (OV/PS/OR/ST/PR/MK/RK).
- All pages render inside `<AppShell>` for the sidebar + topbar +
  command palette.

Phase H replaces the localStorage token with httpOnly cookies + OAuth.
