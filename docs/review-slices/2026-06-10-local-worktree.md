# Local Worktree Review Slices - 2026-06-10

This ledger turns the current broad local Fincept worktree into reviewable
slices. It is grounded in `git status --short`, `git diff --stat`, and targeted
searches for `NewsImpactSignal`, `MockBadge`, `DataFreshness`,
`assert_safe_for_runtime`, and `/research/provider-data`.

## Current Baseline

| Field | Value |
|---|---|
| Branch | `codex/portfolio-optimizer-core` |
| Head | `9c1aba1 feat(dashboard): apply shared status widgets to positions page` |
| Commit boundary since last automation | No newer commit observed after the 2026-06-08 automation memory. |
| Worktree shape | Broad mixed tree across docs, dashboard UX, news-impact model, API/core contracts, providers, tests, and local tool artifacts. |
| Commit guidance | Do not stage as one commit. Pick one slice below and stage explicit paths only. |

## Slice 1 - Documentation And Planning

| Item | Detail |
|---|---|
| Files | `featuresmenu.md`, `docs/ROADMAP.md`, `docs/RISKS.md`, `docs/project-understanding/**`, `docs/quant-ml-audit/**`, `apps/dashboard/docs/**`, this ledger. |
| Risk | Low code risk, high review-noise risk if mixed with implementation. |
| First validation | `git diff --check -- featuresmenu.md docs apps/dashboard/docs` |
| Done when | The docs explain the current tree without claiming runtime tests passed. |

## Slice 2 - Dashboard Shell And Shared Widgets

| Item | Detail |
|---|---|
| Files | `apps/dashboard/src/app/globals.css`, `apps/dashboard/src/app/page.tsx`, `apps/dashboard/src/components/shell/**`, `apps/dashboard/src/components/widgets/**`, `apps/dashboard/src/components/ui/card.tsx`, `apps/dashboard/src/lib/design-tokens.ts`. |
| Risk | Visual consistency and layout regressions across most pages. |
| First validation | `cd apps/dashboard; npm run lint -- --file src/app/page.tsx --file src/components/shell/sidebar.tsx` |
| Done when | Shared widgets render without hiding mock/live status and no route loses navigation affordances. |

## Slice 3 - Mock Terminal Routes

| Item | Detail |
|---|---|
| Files | `apps/dashboard/src/app/watchlist/**`, `apps/dashboard/src/app/symbol/**`, `apps/dashboard/src/app/positions/page.tsx`, `apps/dashboard/src/components/overview/watchlist-preview.tsx`, `apps/dashboard/src/lib/mock-data.ts`. |
| Risk | Mock market data can look like live terminal data if badge and contract cues drift. |
| First validation | `rg -n "MockBadge|mock-data|buildMock" apps/dashboard/src/app apps/dashboard/src/components` |
| Done when | Every mock-backed route has a visible `MockBadge` and a named replacement API contract. |

## Slice 4 - News-Impact Shadow Lane

| Item | Detail |
|---|---|
| Files | `libs/fincept-core/src/fincept_core/schemas.py`, `libs/fincept-core/src/fincept_core/events.py`, `libs/fincept-core/tests/test_schemas.py`, `services/agents/src/agents/news_impact_agent/**`, `services/agents/tests/test_news_impact_agent.py`, `services/api/src/api/routes/news_impact.py`, `services/api/tests/test_news_impact.py`, `apps/dashboard/src/components/news-impact/**`, `apps/dashboard/src/app/news-impact-lab/page.tsx`, `experiments/news-impact-model/**`. |
| Risk | Shadow model output accidentally gains order authority. |
| First validation | `rg -n "\\b(side|quantity|venue|broker|order|sizing)\\b" libs/fincept-core/src/fincept_core/schemas.py services/api/src/api/routes/news_impact.py apps/dashboard/src/components/news-impact` |
| Done when | The search has only documented non-authoritative references, and tests prove signals remain read-only. |

## Slice 5 - API, Core Contracts, And Runtime Safety

| Item | Detail |
|---|---|
| Files | `libs/fincept-core/src/fincept_core/config.py`, `libs/fincept-core/src/fincept_core/http.py`, `libs/fincept-core/tests/test_http.py`, `services/api/src/api/main.py`, `services/api/src/api/routes/data.py`, `services/api/src/api/routes/research.py`, `services/api/tests/test_data.py`, `services/api/tests/test_provider_data.py`, `.env.example`. |
| Risk | Startup safety checks, request IDs, provider-data redaction, and `DataFreshness` semantics can diverge. |
| First validation | `uv run pytest services/api/tests/test_data.py services/api/tests/test_provider_data.py libs/fincept-core/tests/test_http.py -q` |
| Done when | Unsafe prod-like config fails closed and provider-data responses are sanitized. |

## Slice 6 - Provider And OMS Resilience

| Item | Detail |
|---|---|
| Files | `services/ingestor/src/ingestor/binance.py`, `services/ingestor/src/ingestor/eod_equity.py`, `services/jobs/src/jobs/daily_eod_load.py`, `services/oms/src/oms/alpaca/marks.py`, `services/oms/src/oms/alpaca/news_sync.py`, `libs/fincept-bus/src/fincept_bus/streams.py`, `libs/fincept-bus/tests/test_streams.py`. |
| Risk | Provider retries, disabled-provider states, stale marks, and stream semantics can fail only under live-ish conditions. |
| First validation | `uv run pytest libs/fincept-bus/tests/test_streams.py services/api/tests/test_data.py -q` |
| Done when | Mocked provider failures produce bounded stale/unavailable states, not raw exceptions. |

## Slice 7 - Launch Scripts And Tooling

| Item | Detail |
|---|---|
| Files | `scripts/start.ps1`, `.devin/workflows/phase-kickoff.md`, `.windsurf/workflows/phase-kickoff.md`, `apps/dashboard/scripts/run-shadow-news-impact-tests.cjs`. |
| Risk | Workflow moves can break local launch and agent handoffs. |
| First validation | `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/start.ps1 -Help` |
| Done when | Script failures remain visible and workflow docs point at the intended active tool folder. |

## Excluded Or Needs Decision

| Path | Reason |
|---|---|
| `.env` | Local secret-adjacent config; never stage without explicit secret review. |
| `.opencode/`, `.playwright-cli/`, `.worktrees/`, `.devin/dialectic-repo/` | Tool state or side worktrees; classify before staging. |
| `node_modules/`, `.venv/`, caches, `tmp_*` logs | Generated/local runtime artifacts. |

## Immediate Next Step

Start with Slice 4 or Slice 5. They protect the highest-risk boundaries:
shadow-model non-agency, runtime fail-closed behavior, provider-data redaction,
and `DataFreshness` contracts.
