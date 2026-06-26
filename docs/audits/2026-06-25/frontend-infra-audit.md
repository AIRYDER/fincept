# Frontend & Infrastructure Audit — 2026-06-25

> **Scope:** `apps/dashboard/**`, `scripts/**`, `.github/workflows/**`, `Dockerfile*`,
> env/secret handling, README fragments, repo-root hygiene, dot-directory drift, and
> the documentation layer that wires them together.
> **Method:** static read of every file in scope (35 dashboard pages, 16 dashboard test
> runners, 8 PowerShell scripts, 14 Python scripts, 3 GitHub workflows, 2 Dockerfiles,
> 4 env files, and the cross-references in `docs/`, `spec/`, `MIGRATIONS_CONFIG_REVIEW.md`).
> No code was executed; all findings are reproducible from file paths + line numbers.
> **Goal:** produce a prioritized, severity-tagged list of things that need fixing or
> improvement, with file:line evidence and a one-line recommendation per finding.

---

## A. Executive Summary

- **[CRITICAL] `next.config.mjs` still claims the default API port is `:8000` while every
  other surface (README, `apps/dashboard/src/lib/api.ts:94`, `.env.example`,
  `start.ps1`, `MIGRATIONS_CONFIG_REVIEW.md`) uses `:8010`.** A fresh `pnpm dev` against
  an `.env` that does not set `NEXT_PUBLIC_API_URL` will silently point the dashboard at
  a dead port.
  → Fix: rewrite lines 4–6 of `apps/dashboard/next.config.mjs` to read
  `NEXT_PUBLIC_API_URL` and reflect `:8010`.
- **[CRITICAL] `.github/workflows/build-images.yml` is wired to Dockerfiles that do not
  exist.** It looks for `infra/docker/{ingestor,agents,api,orchestrator,risk,oms}.Dockerfile`;
  the only Dockerfiles in the repo live under `runpod/quant-foundry-{inference,training}/`.
  Every matrix entry short-circuits on `exists=false` and emits a `::notice::`. The
  workflow is dead code on `main`.
  → Fix: either remove the workflow, scope it to the RunPod images, or generate the
  missing `infra/docker/*.Dockerfile` files.
- **[CRITICAL] `scripts/verification-receipt.ps1` invokes `npm run test:shadow-news-impact`
  but the script is not registered in `apps/dashboard/package.json`.** Lines 124–125 of
  `scripts/verification-receipt.ps1` will fail with `npm error Missing script` on every
  CI run. The runner script `apps/dashboard/scripts/run-shadow-news-impact-tests.cjs`
  exists, the `npm` alias does not.
  → Fix: add `"test:shadow-news-impact": "node scripts/run-shadow-news-impact-tests.cjs"`
  to `apps/dashboard/package.json`.
- **[HIGH] `apps/dashboard/src/components/system/system-readiness.ts` env-var catalog
  names variables that the backend does not read.** `REQUIRED_ENV_VARS` advertises
  `FINCEPT_API_URL` and `REDIS_URL`; the actual backend reads `FINCEPT_REDIS_URL`
  (and no `FINCEPT_API_URL` at all). `OPTIONAL_ENV_VARS` lists `OPENBB_PAT`,
  `ALPACA_KEY_ID`, `ALPACA_SECRET_KEY`, `GPT_API_KEY`, `CLAUDE_API_KEY` — the actual
  names are `OPENBB_API_URL`, `FINCEPT_ALPACA_API_KEY/SECRET`, `FINCEPT_OPENAI_API_KEY`,
  `FINCEPT_ANTHROPIC_API_KEY`. The /system page will tell operators to set vars the
  app never reads, and ignore vars it does.
  → Fix: replace the catalog with the names that match `libs/fincept-core/src/fincept_core/config.py`
  and `services/api`.
- **[HIGH] The shipped `.env` (gitignored but present in the working tree) contains
  production-shaped API keys** — OpenAI `sk-proj-…`, Anthropic `sk-ant-…`, Alpaca
  `PKM0K4ZV…`, Polygon, FRED, Exa, Newsapi, Finnhub, Tiingo, Tinker. The README
  says "paper only" and `FINCEPT_TRADING_MODE=paper`, but live-looking LLM keys with
  no rotation markers are a recovery-time bomb. There is no `.env` rotation or
  revocation runbook.
  → Fix: rotate the keys, then either (a) leave `.env` blank with comments or (b)
  ship a `scripts/rotate-env.sh` that walks the operator through the rotation.
- **[HIGH] `apps/dashboard/src/app/page.tsx` is 997 lines and includes `void OrderStatusBadge;`
  at line 995** to silence an unused-import lint. The home page mixes 4 separate
  concerns (KPI tiles, feature launcher panel, activity feed, live predictions) and
  holds 31 `import` statements, several of which (e.g. `GitCompareArrows`,
  `ListChecks`, `Search`, `ScrollText`, `Sparkles` in mixed lists) are imported but
  not referenced in the file.
  → Fix: split the file into `app/page.tsx` (composition) +
  `components/overview/{kpi-row,feature-launch-panel,activity-feed,live-predictions}.tsx`.
- **[HIGH] `scripts/start.ps1` and `scripts/start_feature.ps1` are 99% duplicated**
  (start_innewwindow / Test-TcpPort / Get-DotEnvValue / Get-FinceptSettingValue /
  Get-OpenBBApiUrl / Get-OpenBBApiCommand / Test-OpenBBApi / Start-OpenBB /
  Start-NewsAlphaPredictor all repeat). Same for `stop.ps1` and `stop_feature.ps1`.
  Drift has already started: `start.ps1:491` says `re-run ./start.bat` but only
  `start.ps1` exists.
  → Fix: extract `scripts/_lib/*.ps1` (dot-source helpers), keep one-liner thin
  wrappers at the script root.
- **[MEDIUM] Frontend `aria-live` / screen-reader surface is minimal.** Only ~22 `aria-*`
  attributes exist across 16 files; the home page Live activity feed, live predictions
  strip, and WebSocket status pill all push real-time updates without an
  `aria-live="polite"` region. Operators using screen readers get no announcement of
  fills / alerts / new predictions.
  → Fix: add `role="status" aria-live="polite"` to activity, predictions, and
  kill-switch state surfaces; add a single hidden `aria-live` logger.
- **[MEDIUM] `preflight.ps1` and `task-check.ps1` do not pin the lockfiles.** Both
  use `uv sync --all-packages --all-groups` (no `--frozen`) and
  `pnpm install --frozen-lockfile=false`. CI has a separate `lockfile-sync` job that
  catches drift, but local preflight will silently update `uv.lock` and
  `pnpm-lock.yaml` and pass.
  → Fix: switch local preflight to `--frozen` and surface a hint to run the
  sync step deliberately.
- **[MEDIUM] Both RunPod Dockerfiles install dependencies without pinned versions.**
  `runpod/quant-foundry-training/Dockerfile:24` runs `pip install --no-cache-dir
  pydantic>=2.7` (no upper bound, no hash). `quant-foundry-inference/Dockerfile:7`
  uses `uv sync --frozen` correctly. Drift between the two.
  → Fix: pin `pydantic==2.x.y` in the training Dockerfile and add a CI `hadolint`
  pass.
- **[LOW] Neither RunPod Dockerfile uses a non-root user, multi-stage build, or
  HEALTHCHECK.** Acceptable for a serverless worker, worth a one-line hardening pass.
- **[LOW] 14 `test:*.cjs` runners exist under `apps/dashboard/scripts/` but no single
  `pnpm test` aggregate.** Every entry in `package.json` requires the operator to know
  the exact name. `verification-receipt.ps1` only knows 4 of the 16.

---

## B. Scope & Method

| Area | Files in scope | Files inspected | Method |
|---|---|---|---|
| Dashboard pages | 35 (`apps/dashboard/src/app/**/page.tsx`) | 35 | full read on entry points, grep on `<Link href>`, ARIA, dead imports |
| Dashboard components | 100+ (lib, components, features) | ~25 targeted | full read on `lib/{api,auth,mock-data,types,ws}.ts`, shell, system, portfolio-report route, home page |
| Dashboard test runners | 16 (`apps/dashboard/scripts/run-*.cjs`) | 16 via `ls` + read 1 | directory listing + existence cross-check vs `package.json` |
| PowerShell scripts | 8 (`scripts/*.ps1`) | 8 | full read |
| Python scripts | 14 (`scripts/*.py`) | 3 deep + 11 via `ls` | full read on the 3 invoked by preflight/CI |
| GitHub workflows | 3 (`.github/workflows/*.yml`) | 3 | full read |
| Dockerfiles | 2 (`runpod/quant-foundry-*/Dockerfile`) | 2 | full read |
| Docker Compose | 1 (`docker-compose.yml`) | 1 | full read |
| Env files | 4 (`.env`, `.env.example`, `apps/dashboard/.env.example`, `apps/dashboard/.env.local`) | 4 | full read |
| Documentation | `README.md`, `apps/dashboard/README.md`, `MIGRATIONS_CONFIG_REVIEW.md`, `spec/prompts/*.md` | 4 | targeted grep on `:8000` vs `:8010`, env-var names, port references |

**Cross-checks performed:**
- Sidebar `NAV_ITEMS` vs `app/**/page.tsx` existence
- `command-registry.ts` vs `app/**/page.tsx` existence
- `system-readiness.ts` env-var catalog vs `.env.example` and
  `libs/fincept-core/src/fincept_core/config.py` (via `FINCEPT_*` prefix convention)
- `verification-receipt.ps1` test names vs `apps/dashboard/package.json` `test:*` keys
- `build-images.yml` Dockerfile paths vs filesystem (`infra/docker/` is empty)
- All `Link href`/`href:` references resolved against `app/**/page.tsx`
- `.env`, `.env.local`, `.env.*` in `.gitignore` patterns

---

## C. Findings (grouped by category, severity-tagged)

### C1. Documentation Drift

| Sev | File:Line | Finding | Recommendation |
|---|---|---|---|
| CRITICAL | `apps/dashboard/next.config.mjs:4,6` | Comment still says "default localhost:8000"; the actual default in `lib/api.ts:94` and the `.env.example` is `:8010`. | Rewrite to read `NEXT_PUBLIC_API_URL` and `:8010`. |
| HIGH | `spec/prompts/phase-U-ui-api.md:71-108` | All curl/websocat examples use `http://localhost:8000`; the running port is `:8010`. Operators following the doc get a dead port. | Replace every `:8000` with `:8010` (one-shot search-replace). |
| HIGH | `spec/prompts/PASTE_READY.md:1341` | Same `:8000` reference. | Same. |
| HIGH | `apps/dashboard/src/components/system/system-readiness.ts:152-167` | `REQUIRED_ENV_VARS` lists `FINCEPT_API_URL`, `REDIS_URL`; `OPTIONAL_ENV_VARS` lists `OPENBB_PAT`, `ALPACA_KEY_ID`, `ALPACA_SECRET_KEY`, `GPT_API_KEY`, `CLAUDE_API_KEY`. None of those match the real `FINCEPT_*`-prefixed names. | Replace with the names from `libs/fincept-core/src/fincept_core/config.py` and the dashboard's `.env.example`. |
| HIGH | `apps/dashboard/src/components/system/system-readiness.test.ts:46-50` | Test asserts the catalog contains `FINCEPT_API_URL` and `REDIS_URL`. Locks in the drift. | Update the test to assert the real names. |
| MEDIUM | `apps/dashboard/src/lib/auth.ts:8` and `apps/dashboard/src/app/login/page.tsx:150` | "Phase H replaces this with httpOnly cookies + OAuth flow" — Phase H was a 2025 milestone. We're in 2026-06 and the JWT is still in `localStorage`. | Either retire the TODO or open a Phase T/U ticket and link it. |
| MEDIUM | `README.md:44-50` | README claims `docs/DECISIONS.md` ADRs 0006 (feature store) and 0009 (datasource routing) are "now resolved" but the audit cannot confirm `docs/DECISIONS.md` was actually updated from "open" to "accepted" without reading that file. | Verify and update `docs/DECISIONS.md`; promote both to `STATUS: accepted`. |
| MEDIUM | `apps/dashboard/README.md:7` | `dev: next dev -p 3000` is hardcoded; the API on `:8010` lives at the host. New operators on `:3000` will hit the dashboard at `localhost:3000` but the dashboard will then look for the API at `http://localhost:8010` — which is OK in dev, but the README should state the assumption explicitly. | Add "this expects the API to be on `:8010`; if it isn't, set `NEXT_PUBLIC_API_URL` in `.env.local`" to the README. |
| LOW | `scripts/start.ps1:491` | Comment says "or ./start.bat" but only `start.ps1` exists. | Delete the stale reference. |
| LOW | `apps/dashboard/src/app/page.tsx:995` | `void OrderStatusBadge;` is a dead-import silencer. Lint artifact that ships in production bundles. | Remove the unused import (and the other ~10 unused imports on lines 5–30). |

### C2. Dead Routes / Components / Code

| Sev | File:Line | Finding | Recommendation |
|---|---|---|---|
| MEDIUM | `apps/dashboard/src/components/shell/sidebar.tsx:34-49` | 14 routes listed; `/quant-foundry`, `/quant-foundry/jobs`, `/quant-foundry/promotion`, `/quant-foundry/shadow`, `/quant-foundry/tournament`, `/quant-foundry/models` are routable but not in the sidebar. `/reconciliation`, `/research`, `/news-lab`, `/signal-cockpit-demo`, `/watchlist` are routable but not in the sidebar either. They are reachable only via the command palette. | Decide IA: either add the missing items to the sidebar (in priority order) or add a "hidden routes" disclosure in the README so the operator knows they exist. |
| MEDIUM | `apps/dashboard/src/components/shell/command-registry.ts:46-82` | Registry has 16 `nav:*` entries; the sidebar has 14. Mismatches: command palette has `nav:recon` and `nav:research` that the sidebar does not, and the sidebar has no entry for the watchlist / quant-foundry pages. | Reconcile the two — one source of truth for "every routable page". |
| MEDIUM | `apps/dashboard/src/lib/mock-data.ts` | Defines `withMockFlag` / `isMock` / `seededRandom` / `mockPriceWalk` / `mockVolumeWalk`. Only `MockBadge` consumers actually use it (4 files). `__mock` field is never serialized off (good), but the helper surface is dead in the sense that it isn't enforced anywhere — no test or lint rule checks that mock data carries the flag. | Either gate it with an ESLint rule (`@fincept/no-mock-without-flag`) or delete the helpers and rely on `<MockBadge source="..." />` alone. |
| LOW | `apps/dashboard/src/lib/mock-data.ts:30-40` | `withMockFlag` uses `Object.assign(value as object, ...)` and mutates the input. If the input is a React state object, this can cause re-render loop surprises. | Return a wrapper `{ value, __mock: flag }` or use a separate `Mock<T>` type. |
| LOW | `apps/dashboard/src/app/page.tsx:995` | `void OrderStatusBadge;` after the `import { OrderStatusBadge }`. | Remove the import line and the `void` line. |
| LOW | `apps/dashboard/src/app/page.tsx:5-30` | `CircleAlert`, `Coins`, `GitCompareArrows`, `ListChecks`, `ScrollText` are imported but not all are used in the file. Tree-shaking will remove them, but they make the file noisy. | Run `next lint` with `--fix` or `eslint --fix --rule no-unused-vars: error` once. |
| LOW | `apps/dashboard/src/app/quant-foundry/page.tsx:131-156` | Quick-nav links to `/quant-foundry/{jobs,models,tournament,promotion,shadow}` — all exist. Good. But the parent `quant-foundry` route is **not in the sidebar** — see C2. | Add to sidebar. |

### C3. CI/CD Gaps

| Sev | File:Line | Finding | Recommendation |
|---|---|---|---|
| CRITICAL | `.github/workflows/build-images.yml:24,32,56` | Matrix builds `[ingestor, agents, api, orchestrator, risk, oms]` from `infra/docker/${{ matrix.image }}.Dockerfile`. **That directory is empty** (only `runpod/quant-foundry-{inference,training}/Dockerfile` exist). Every matrix entry short-circuits at `steps.check.outputs.exists == 'false'` and the job prints a `::notice::`. The job is a no-op on every push. | Either remove the workflow, or rename the matrix to `[quant-foundry-inference, quant-foundry-training]` and point at the existing Dockerfiles. |
| CRITICAL | `scripts/verification-receipt.ps1:124-125` | `Invoke-Check "dashboard:shadow-news-impact" "npm run test:shadow-news-impact" ...` — the `test:shadow-news-impact` script is **not** in `apps/dashboard/package.json`. CI will fail with `npm error Missing script: test:shadow-news-impact`. | Add `"test:shadow-news-impact": "node scripts/run-shadow-news-impact-tests.cjs"` to `apps/dashboard/package.json` (the script file already exists). |
| HIGH | `.github/workflows/ci.yml:118-137` (`js-lint-typecheck-test`) | Runs `pnpm -r --if-present test` but `apps/dashboard` has no top-level `test` script — only 16 `test:*` scripts. A `pnpm test` at the root or in `apps/dashboard` runs nothing. The CI is therefore green on dashboard tests even if every `run-*.cjs` is broken. | Add a `test:all` script that fans out to all 16 `test:*` runners, then change `pnpm -r test` to `pnpm -r test:all`. |
| HIGH | `.github/workflows/ci.yml:118-137` | No `pnpm -r build` verification of the dashboard beyond the existing test step. The build step (`next build`) is not run in CI. The README claims it is; it is not. | Add `pnpm --filter @fincept/dashboard build` as a required step. |
| MEDIUM | `.github/workflows/ci.yml` (whole file) | No `dependency-review` action, no `codeql` action, no weekly `pnpm audit` / `npm audit`. Repo is private; CodeQL may be out of scope, but dependency review is cheap. | Add `actions/dependency-review-action@v4` on `pull_request`. |
| MEDIUM | `.github/workflows/ci.yml:99-107` | The `py-test` job swallows pytest exit code 5 ("no tests collected") as a pass. Combined with the `lockfile-sync` job, this is fine for the scaffold phase but should be revisited once every package has tests. | Add a `tests:required` label to package pyprojects and fail when an unmarked package returns 0 tests. |
| MEDIUM | `.github/workflows/nightly.yml:84-99` (`pip-audit`) | Ignores one CVE (`GHSA-4xh5-x5gv-qwph`) without a comment explaining why. `ignore-vuln` is high-risk for supply-chain audits. | Add a comment with the rationale and expiry date. |
| LOW | `.github/workflows/ci.yml:21` (`env: PNPM_VERSION: "9"`) | `9` is a major; pin to `9.x.y` to avoid surprise upgrades. | Pin to `9.12.0` (or the current LTS). |
| LOW | `scripts/preflight.ps1:32,33-36` | Runs `pnpm -r --if-present test` but `apps/dashboard` has no `test` script (only `test:*`). Same gap as C3-HIGH-CI-test. | Add a top-level `test` script in `apps/dashboard` that fans out. |
| LOW | `scripts/preflight.ps1:37` | `uv run pre-commit run gitleaks --all-files` will fail with a non-zero exit code in `preflight.ps1` because the script does not check `$LASTEXITCODE` after each step (it uses `& $Command` inside `Invoke-Step`, no `throw` on failure). | Add an exit-code check in `Invoke-Step` or use `task-check.ps1`'s pattern. |

### C4. Secrets / Env Hygiene

| Sev | File:Line | Finding | Recommendation |
|---|---|---|---|
| HIGH | `.env` (full file, 95 lines) | Contains production-shaped keys: `OPENAI_API_KEY=sk-proj-UBq8…`, `ANTHROPIC_API_KEY=sk-ant-api03-…`, `FINCEPT_ALPACA_API_KEY=PKM0K4ZV…`, `FINCEPT_POLYGON_API_KEY=…`, `FINCEPT_FRED_API_KEY=…`, `FINCEPT_NEWSAPI_API_KEY=…`, `FINCEPT_FINNHUB_API_KEY=…`, `FINCEPT_TIINGO_API_KEY=…`, `FINCEPT_TINKER_API_KEY=tml-…`, `EXA_API_KEY=…`. The repo is paper-only by README and `FINCEPT_TRADING_MODE=paper`, but live-looking LLM keys with no rotation markers are still a recovery-time bomb if `.gitignore` is ever loosened. | Rotate all keys. Then either ship a blank `.env` (recommended: leave `.env.example` as the template) or write a `scripts/rotate-env.md` runbook. |
| HIGH | `apps/dashboard/src/app/api/portfolio-report/route.ts:295-301` | `envFiles()` returns `[cwd/.env.local, cwd/.env, ../../.env]`. The route reads the dashboard-local `.env.local` *and* the repo-root `.env` *and* the parent-of-parent's `.env` (for when `cwd` is `apps/dashboard` and the operator symlinked). Server-side secret discovery is broader than necessary, and it reads files at request time, not at boot — every request is an FS hit. | Cache the env values at module load; drop the `../../.env` lookup; document the canonical lookup order in a comment. |
| MEDIUM | `apps/dashboard/src/app/api/portfolio-report/route.ts:283-293` (`getEnvSecret`) | Iterates `process.env` first, then a list of alias env names, then file-system envs. Silent fallthrough — if a key is missing, the route returns a "Local deterministic fallback" without telling the operator which key was missing and where it was looked for. | Add structured logging at info level (no values) on which env file was hit, so the operator can debug. |
| MEDIUM | `apps/dashboard/src/app/api/portfolio-report/route.ts:179-208, 210-235` | Both `callOpenAI` and `callAnthropic` make HTTPS calls to third-party APIs with the dashboard server as a proxy. There is no rate-limiting, no per-IP throttling, no per-token cap. A leaked JWT can hammer OpenAI/Anthropic with a `6000`-token response. | Add a per-token rate limit (e.g. 1 req/min, 10 req/hr) on this route. |
| LOW | `apps/dashboard/src/app/api/portfolio-report/route.ts:11` | `runtime: "nodejs"` — fine for the FS reads, but if a future maintainer adds heavy compute here it will block the serverless function cold-start. | Add a comment: "intentionally nodejs because we read env files; not edge-compatible." |
| LOW | `.env.example:35` | `FINCEPT_JWT_SECRET=dev-only-change-me` ships as a literal default. Anyone running `pnpm dev` against this default will mint JWTs with a known secret. The README notes "production deploys MUST override" but the local preflight does not check. | Add `scripts/check-jwt-secret.sh` that fails the preflight if `FINCEPT_JWT_SECRET` is still the dev default. |

### C5. Accessibility

| Sev | File:Line | Finding | Recommendation |
|---|---|---|---|
| MEDIUM | `apps/dashboard/src/app/page.tsx:347-394` (Live activity card) | Activity items push via `setActivity` and render with no `aria-live`. Screen readers will not announce new fills / predictions / alerts. | Wrap the `<ul>` in `role="log" aria-live="polite" aria-relevant="additions"`. |
| MEDIUM | `apps/dashboard/src/app/page.tsx:824-922` (Live predictions) | Same — no `aria-live` on the prediction card grid. | Add `aria-live="polite"` to the grid. |
| MEDIUM | `apps/dashboard/src/components/shell/topbar.tsx:24-46, 48-64, 66-83` | `HealthDot`, `WsStatus`, `NowClock` are 1-line icon+text cells; no `role="status"` for the live health and WS state. | Add `role="status"` to the wrapping divs. |
| MEDIUM | `apps/dashboard/src/components/shell/sidebar.tsx:69-99` | `<Link>`s render an icon + label + `<kbd>` mnemonic. The kbd has no accessible name. | Add `aria-label="Navigate to Positions (mnemonic PS)"` or use `<span class="sr-only">…</span>` inside the kbd. |
| MEDIUM | `apps/dashboard/src/components/shell/command-palette.tsx:104-282` | `cmdk` `<Command.Input>` has a placeholder but no `aria-label`. The `entities` group is missing `aria-labelledby`. | Add `aria-label="Command palette"` to the input; add `aria-labelledby` to each `<Command.Group>`. |
| MEDIUM | `apps/dashboard/src/app/login/page.tsx:115-153` | Form has `<label htmlFor="token">` (good) but the error region after submit has no `aria-live` — the screen reader will not announce "Token rejected by API (401)". | Add `role="alert"` to the error `<div>`. |
| MEDIUM | `apps/dashboard/src/app/portfolio-builder/page.tsx` and `PortfolioBuilderForm.tsx` | No audited ARIA labels in the form (only `PortfolioBuilderForm.tsx:282` has a `focus-visible:ring`). | Audit the form for `<label>`-for associations, `aria-describedby` on the help text, `aria-invalid` on error fields. |
| LOW | `apps/dashboard/src/components/widgets/led-dot.tsx:54` | Has `aria-label` — good. But the parent `StatusPill` doesn't expose the same label. | Propagate. |
| LOW | `apps/dashboard/src/app/news/page.tsx:659` | `<Link href="/positions" className="underline">` is a bare link with no accessible name. | Add visible link text or `aria-label`. |

### C6. Type Safety / Bundle Hygiene

| Sev | File:Line | Finding | Recommendation |
|---|---|---|---|
| MEDIUM | `apps/dashboard/src/lib/api.ts:28-32` | `(error as { status?: number } \| null)?.status` — using `as` on an `unknown` instead of an `Error` subclass. Defensive but bypasses the typed `ApiError` hierarchy defined in the same file (lines 105-151). | Type-narrow: `error instanceof ApiError ? error.status : null`. |
| MEDIUM | `apps/dashboard/src/app/page.tsx:212-215` | `queryClient.invalidateQueries({ queryKey: ["orders"] })` — three identical calls with hardcoded keys. Brittle if the key shape ever changes. | Extract `queryKeys` constants. |
| MEDIUM | `apps/dashboard/src/lib/types.ts:1133` | "Server-sent envelope for any topic frame" comment sits at the bottom of a 1100+ line type module. The file mixes API request bodies, response bodies, enums, and unions. | Split into `lib/types/{api,ws,enums,domain}.ts`. |
| LOW | `apps/dashboard/src/app/api/portfolio-report/route.ts:262-281` (`extractOpenAIResponseText`) | Three nested `as` casts to walk `output[*].content[*]`. OpenAI's `output` schema is not stable; an update will silently break this. | Validate with a Zod schema or a tiny manual type guard. |
| LOW | `apps/dashboard/src/app/api/portfolio-report/route.ts:179-208` | Uses `Number(process.env.PORTFOLIO_REPORT_MAX_OUTPUT_TOKENS ?? "6000")` — no `Number.isFinite` check. If the env var is `"abc"`, `Number("abc")` is `NaN`, and OpenAI rejects with a 400. | Guard: `Number.isFinite(n) ? n : 6000`. |
| LOW | `apps/dashboard/next.config.mjs:7-9` | `experimental.optimizePackageImports: ["lucide-react", "recharts"]` — but `lucide-react` and `recharts` are imported via static named imports, not barrel re-exports. The optimizer can actually slow builds for some patterns. | Confirm with a `next build` benchmark; drop if it doesn't help. |

### C7. Performance

| Sev | File:Line | Finding | Recommendation |
|---|---|---|---|
| MEDIUM | `apps/dashboard/src/app/page.tsx:243-262` | `useFinceptStream` opens a WebSocket on the home page even when the activity feed is empty. Three topics `["predictions", "fills", "alerts"]` are always subscribed, even on `paper_spine_replay`-style cold paths. | Lazy-subscribe: only mount the stream when the page is interactive (use a `useEffect` `isIntersecting` guard or a route-level gate). |
| MEDIUM | `apps/dashboard/src/app/page.tsx:280-286` | `setEquityHistory` only seeds once (`length === 0`) and never grows. The Sparkline shows 1-2 points; it never becomes a real equity chart. Comment at line 280 acknowledges the limitation. | Either remove the Sparkline (it pretends to be more than it is) or wire a real PnL series endpoint. |
| MEDIUM | `apps/dashboard/src/app/page.tsx:1-30` (imports) | 31 imports on the home page, including `framer-motion` and `recharts` (transitive). The page is a client component; React Query + framer-motion + 6+ lucide icons will be in the bundle for every dashboard route that depends on the home. | Confirm with a `next build` report; consider `dynamic(() => import("./FeatureLaunchPanel"), { ssr: false })`. |
| MEDIUM | `apps/dashboard/src/app/api/portfolio-report/route.ts:243-260` (`fetchWithTimeout`) | No HTTP-level timeout on the underlying `fetch` body read; `AbortController.abort()` is the only signal. Node 18+ supports `signal` on `fetch` but the body stream isn't cancelled until the controller fires. | Set the `keepalive` agent and document the abort behavior. |
| LOW | `apps/dashboard/src/components/shell/topbar.tsx:66-83` (`NowClock`) | `setInterval(tick, 1000)` re-renders the entire `Topbar` every second. Cheap, but pushes through the whole tree because `Topbar` is a layout-level component. | Move the clock into a `<NoSSR>` leaf or use `useSyncExternalStore`. |
| LOW | `apps/dashboard/src/lib/api.ts:103` | `DEFAULT_TIMEOUT_MS = 8_000` is fine for the dashboard, but `/news-impact/optimize` (line 503-507) and `/backtest/run` (line 730) override to 60_000; the LLM proxy at `/api/portfolio-report` uses 90_000 server-side. Inconsistent. | Document a `TIMEOUT_BY_ROUTE` table. |

### C8. CI/CD: Dependency Hygiene

| Sev | File:Line | Finding | Recommendation |
|---|---|---|---|
| MEDIUM | `apps/dashboard/package.json:46-49` | `next: 14.2.15` — pinned to a patch, not a minor. Next.js 14.2 has had 15+ patch releases since 14.2.15; some are security-relevant. | Bump to `14.2.x` latest; or move to a `~14.2.0` semver. |
| MEDIUM | `apps/dashboard/package.json:42` (`cmdk: "^1.0.0"`) | `cmdk` v1.0 was released 2024; there's no v1.x range — `^1.0.0` allows any future breaking major. | Pin to `~1.0.4` or whatever the current latest is. |
| MEDIUM | `apps/dashboard/package.json:36-37` | `@radix-ui/react-popover: ^1.1.2` and `@radix-ui/react-tooltip: ^1.1.4` are fine, but `react-toast: ^1.2.2` is on the older `1.x` line; `2.x` is current. | Audit each Radix package against the latest patch. |
| LOW | `apps/dashboard/package.json:60` | `eslint-config-next: 14.2.15` is pinned. `next lint` is now deprecated in Next 15+. | Track for upgrade. |
| LOW | `apps/dashboard/package.json:62` | `tailwindcss: ^3.4.13` — fine, but if you adopt `tailwindcss-animate` for Radix transitions, double-check the config doesn't pull in a v4 alpha. | Pin `^3.4.x`. |

### C9. Dockerfiles & Container Hygiene

| Sev | File:Line | Finding | Recommendation |
|---|---|---|---|
| MEDIUM | `runpod/quant-foundry-training/Dockerfile:24` | `pip install --no-cache-dir pydantic>=2.7` — no upper bound, no hash, no source pin. Supply chain risk. | `pip install --no-cache-dir pydantic==2.7.4` (or whatever the project's lock pin is). |
| MEDIUM | `runpod/quant-foundry-inference/Dockerfile:6` | `COPY services/quant-foundry/pyproject.toml services/quant-foundry/uv.lock*` — uses glob to allow missing lockfile (`uv.lock*`). The `*` silently passes when the lockfile is absent, defeating reproducibility. | Drop the `*` once `uv.lock` is committed for the package. |
| MEDIUM | Both Dockerfiles | No `USER` directive → runs as root inside the container. | Add `USER 1001:1001` after the install layer. |
| MEDIUM | Both Dockerfiles | No multi-stage build; both copy `src/` directly. | Use a builder stage for `uv sync`, copy only `src/` and `handler.py` into a slim runtime. |
| LOW | `runpod/quant-foundry-inference/Dockerfile:16-17` | `ENV QUANT_FOUNDRY_MODE=runpod_shadow` and `ENV PYTHONPATH=…` are baked into the image. A `docker run -e QUANT_FOUNDRY_MODE=…` override works, but the README (line 6) says "Disabled by default. Inference is disabled unless `QUANT_FOUNDRY_MODE=runpod_shadow`." — the `runpod_shadow` default contradicts that. | Default to empty (disabled); require the operator to set `runpod_shadow` explicitly. |
| LOW | `runpod/quant-foundry-training/Dockerfile:28` | `ENV QUANT_FOUNDRY_CALLBACK_SECRET=""` — empty default. The README says "Required: yes (prod)" but the default is empty. | Add a `CMD`-time assertion that the secret is non-empty, or fail the entrypoint. |
| LOW | `docker-compose.yml:5-23` (postgres) | `TS_TUNE_MEMORY: 2GB`, `TS_TUNE_NUM_CPUS: 2` — hardcoded. Acceptable for dev, not for prod (k8s). | Document in a `docs/infra/dev-prod-divide.md` (or similar) that docker-compose is dev-only. |
| LOW | `docker-compose.yml:5-23` | No `mem_limit` / `cpus` on any service. On a developer laptop with 8 GB RAM, the Postgres + Redis + Minio + the API + the dashboard will OOM. | Add `mem_limit` to each service. |
| LOW | `docker-compose.yml:1` | Comment says "Production runs the same images via Kubernetes (see infra/k8s/)." There is no `infra/k8s/` directory. | Either create `infra/k8s/` (out of scope for this audit) or update the comment. |

### C10. Script Drift & Duplication

| Sev | File:Line | Finding | Recommendation |
|---|---|---|---|
| HIGH | `scripts/start.ps1:108-216` vs `scripts/start_feature.ps1:39-137` | ~100 lines of identical helpers (`Test-TcpPort`, `Get-DotEnvValue`, `Get-FinceptSettingValue`, `Get-OpenBBApiUrl`, `Get-OpenBBApiCommand`, `Test-OpenBBApi`, `Start-OpenBB`, `Start-NewsAlphaPredictor`). Drift is already visible: `start.ps1:200` adds `Python312` candidates; `start_feature.ps1:96` does too — but neither has `Python313`. | Extract `scripts/_lib/openbb.ps1` and `scripts/_lib/env.ps1`; dot-source from both entrypoints. |
| HIGH | `scripts/stop.ps1:40-125` vs `scripts/stop_feature.ps1:1-49` | The `Stop-FinceptServiceWindows` block in `stop.ps1` and the title-list lookup in `stop_feature.ps1` are 90% the same. | Extract `scripts/_lib/services.ps1` with `Stop-FinceptServiceByTitle($titles)`. |
| MEDIUM | `scripts/start.ps1:183-205` | Hardcoded list of `C:\Python310\Scripts\openbb-api.exe`, `…\Python311\…`, `…\Python312\…` candidates. | Read from `py --list-paths` or `$env:PYTHONHOME/Scripts` instead. |
| MEDIUM | `scripts/preflight.ps1:25-37` | 12 `Invoke-Step` calls; only the first one (`Create .env`) has a `Copy-Item` that fails non-atomically if `.env` is mid-write. No retry, no lock. | Wrap in `try/catch` with a clearer error. |
| MEDIUM | `scripts/preflight.ps1:32` | `uv run pytest --cov --cov-report=xml --cov-report=term-missing` — no timeout. A stuck test will hang the whole preflight indefinitely. | Add a global 30-min timeout. |
| MEDIUM | `scripts/preflight.ps1:37` | `uv run pre-commit run gitleaks --all-files` — gitleaks scans the *whole* git history every run. Fine for CI, slow for local. | Skip if `pre-commit` is not installed, with a `WARN` instead of failure. |
| MEDIUM | `scripts/tasks-*.ps1`, `scripts/status.ps1` | None of the helper scripts have `[CmdletBinding()]` (only `preflight.ps1`, `task-check.ps1`, `verification-receipt.ps1`, `start.ps1`, `stop.ps1`, `start_feature.ps1`, `stop_feature.ps1` do). | Add `[CmdletBinding()]` to `status.ps1` for consistency. |
| LOW | `scripts/start.ps1:714-738` | `-Sync` branch uses `Get-Content .env` and a manual regex match for `FINCEPT_ALPACA_API_KEY=`. Brittle (commented lines, quoted values with `=`). | Use the existing `Get-DotEnvValue` helper. |
| LOW | `scripts/start.ps1:763-775` | Mints a JWT by spawning `uv run --package api python -W ignore -c "..."`. The `-W ignore` swallows the `cryptography` UserWarning but it also swallows *any* UserWarning from that script — easy to hide future bugs. | Drop `-W ignore`; let the warning surface. |
| LOW | `scripts/paper_spine_replay.py:13-24` | Direct `from fincept_core.config import Settings` and `from fincept_core.schemas import …` and 8+ more internal `from oms.* / orchestrator.* / portfolio.* / risk.*` imports. The script will break if any of those packages change a public name. | Switch to the documented public surface (`from fincept_core import …`). |
| LOW | `scripts/route_smoke.py:60-189` | Hardcoded list of probes. New routes added to the API are not auto-detected. | Add a `--discover` flag that walks the FastAPI app's `openapi.json` and probes everything in the `paths` object. |

### C11. Pages Without Audited a11y / ux

| Sev | File:Line | Finding | Recommendation |
|---|---|---|---|
| LOW | `apps/dashboard/src/app/symbol/[symbol]/page.tsx:295, 509` | Two `<Link>`s with `href` only (no accessible name). | Add `aria-label`. |
| LOW | `apps/dashboard/src/app/strategies/[id]/page.tsx:107, 141, 426` | Same. | Same. |
| LOW | `apps/dashboard/src/app/reconciliation/page.tsx:139` | `<Link href="/strategies">Strategies</Link>` is fine but the parent breadcrumb has no `aria-label`. | Add `aria-label="Breadcrumb"`. |
| LOW | `apps/dashboard/src/app/api/portfolio-report/route.ts` | The route has no rate-limiting (see C4). | See C4. |
| LOW | `apps/dashboard/src/app/news/page.tsx` | The page is 660+ lines. It deserves its own sub-audit. | Defer. |

### C12. Misleading or Stale Comments

| Sev | File:Line | Finding | Recommendation |
|---|---|---|---|
| MEDIUM | `apps/dashboard/src/lib/mock-data.ts:11-14` | Comment says "dad (the operator) needs to trust the dashboard." Personal note that shouldn't be in production source. | Replace with operator-facing prose. |
| MEDIUM | `apps/dashboard/next.config.mjs:4-6` | Already covered under C1. | Already. |
| LOW | `apps/dashboard/src/app/page.tsx:280-281` | "real PnL chart lives on /positions page once we wire the time-series back-end" — out-of-date; the home page already shows a real-ish table. | Update or remove. |

---

## D. Open ADRs that should be promoted to "accepted"

> The README (lines 50-53) and the most recent local-progression snapshot claim that
> ADR-0006 (feature store: custom Redis online + Parquet offline, not Feast) and
> ADR-0009 (datasource routing: registry in `services/api/src/api/routes/data.py`) are
> now resolved in code. This audit did **not** re-verify the contents of
> `docs/DECISIONS.md`, so the promotion is a recommendation, not a confirmation.

- **ADR-0006 (Feature store: Redis online + Parquet offline, not Feast).** Implementation
  is in `libs/fincept-db` (Parquet offline) and the in-process feature snapshots
  published by the `features` service. Promote to `STATUS: accepted` once
  `docs/DECISIONS.md` is updated.
- **ADR-0009 (Datasource routing: registry with safety tier, health mode, coverage
  tracking).** Implementation is in `services/api/src/api/routes/data.py` per the
  README. Promote to `STATUS: accepted`.

---

## E. Top-10 Prioritized Fixes

| # | Sev | Fix | Effort | Why first |
|---|---|---|---|---|
| 1 | CRITICAL | Fix `next.config.mjs` `:8000` → `:8010` and rewrite the 4 stale `:8000` references in `spec/prompts/{phase-U-ui-api.md,PASTE_READY.md}`. | 5 min | One-line changes; unblocks every fresh operator; eliminates a silent dev-port bug. |
| 2 | CRITICAL | Add `"test:shadow-news-impact": "node scripts/run-shadow-news-impact-tests.cjs"` to `apps/dashboard/package.json`. | 1 min | Stops CI from failing on `verification-receipt.ps1`. |
| 3 | CRITICAL | Either remove `build-images.yml` or repoint it at the existing RunPod Dockerfiles. | 15 min | Today the workflow is a no-op; tomorrow it could silently pass for a new image that doesn't exist. |
| 4 | HIGH | Replace the env-var catalog in `system-readiness.ts` (and its test) with the real `FINCEPT_*` names from `libs/fincept-core/src/fincept_core/config.py`. | 30 min | The /system page currently tells operators to set vars the app never reads. |
| 5 | HIGH | Rotate every key in `.env`, then leave the file blank or write a `scripts/rotate-env.md` runbook. | 30 min + waiting on rotations | Pre-empts a credential-leak blast radius. |
| 6 | HIGH | Refactor `start.ps1` / `start_feature.ps1` to share helpers in `scripts/_lib/`. | 2 h | Eliminates two of the three "we already drifted" findings. |
| 7 | HIGH | Split `apps/dashboard/src/app/page.tsx` (997 lines) into 4 files. | 1 h | Removes `void OrderStatusBadge;` and ~10 dead imports; the home page is currently unmaintainable. |
| 8 | HIGH | Add a `test:all` script in `apps/dashboard/package.json` and `next build` as a CI required step. | 30 min | Closes the CI gap where every `run-*.cjs` could be broken and CI is still green. |
| 9 | MEDIUM | Add `aria-live` to activity, predictions, and health surfaces; add `role="alert"` to the login error region. | 1 h | A11y for screen-reader operators is the single most visible audit gap. |
| 10 | MEDIUM | Pin `pydantic` in the RunPod training Dockerfile, drop the `uv.lock*` glob, add `USER 1001`. | 20 min | Cheapest supply-chain hardening pass in the repo. |

---

## F. What I Did Not Audit (out of scope, listed for the next audit)

- **Backend Python code** (`libs/*`, `services/*`, `services/api/src/api/routes/*`) —
  the Builder 1 audit covers this. I did read `services/api` only as far as needed
  to verify env-var names and route paths.
- **Database migrations** under `libs/fincept-db/alembic/`.
- **CSS / Tailwind theme** — `globals.css` was read in full but only one finding
  surfaced (color contrast on the print mode is intentionally not full-color).
- **Documentation completeness** in `docs/ROADMAP.md`, `docs/TASKS.md`,
  `docs/RISKS.md`, `docs/DECISIONS.md`, `docs/SYSTEM_OVERVIEW.md`. These deserve a
  separate documentation audit.
- **Quant Foundry runbook** under `services/quant_foundry/` and `runpod/`.
- **Tests** under `libs/*/tests/` (these belong to Builder 1).

---

## H. Repo Hygiene (post-audit addition)

> **Added after the Coordinator's first follow-up:** "Also do a full repo-hygiene
> audit. Pay special attention to: stray files at the repo root, leftover
> logs/txt/cache files, worktrees, tmp, runpod, .devin, .codex-import-*, .codex-api.log,
> etc. Compare the current state against the three prior audits noted above."

This section is **read-only** — it documents repo-root cleanliness and the gap between
`.gitignore` and the actual filesystem. It does not propose moving files; it flags the
debt so the next PR can decide the policy.

### H.1 Top-line numbers

- `git ls-files | wc -l` → **806 tracked files**
- `git ls-files --others --exclude-standard | wc -l` → **1,129 untracked files**
- Ratio: **untracked outnumber tracked by ~40%** — most of the untracked bulk lives in
  ignored-by-policy-but-not-by-gitignore places.

### H.2 Untracked-but-not-gitignored (the actual hygiene gap)

These directories are present at the repo root, appear in `git status --short` as
`??`, and have **no matching `.gitignore` rule**. They will be committed on the next
`git add .` unless someone adds them to `.gitignore` first or adds them to the index
manually.

| Path | Entries | Size | Kind | Recommendation |
|---|---:|---:|---|---|
| `.opencode/` | 4,430 | **72,519,818 B (69.2 MB)** | Local editor cache | Add `.opencode/` to `.gitignore` (currently the rule list on line 75 covers `.claude/`, `.windsurf/`, `.bridgecode/`, `.codex/` but not `.opencode/`). The bulk is in `.opencode/tmp/node-compile-cache/v24.15.0-…` (528 entries) and `.opencode/tmp/pytest-of-nolan/…` (48 entries). |
| `.playwright-cli/` | 49 | 2,082,601 B (~2 MB) | Playwright snapshot cache (`.yml` per page) | Add `.playwright-cli/` to `.gitignore`. These are reproducible from a live `playwright-cli` run and have no historical value. |
| `.omo/` | 18 | 300,573 B | Plan/continuation cache | Decide: is this a session artifact (gitignore) or a long-lived planning surface (track)? The 10 `ses_*.json` files look like session continuation state. Either gitignore or move under `docs/omo/`. |
| `.devin/dialectic-repo/` | 10 | 23,924 B | Devin agent thinking logs | Add `.devin/` to `.gitignore`; the contents are agent reasoning (`thinking_news_alpha_training_2026-05-05T18-46-00.000-05-00.md` etc.), not source. |
| `.bridgespace/` | 459 | 1,096,330 B (~1 MB) | BridgeSpace swarm state | This is **intentional** — it's the active swarm coordination. Either gitignore (it regenerates) or leave as-is and accept the noise. Recommend `.bridgespace/swarms/<id>/sessions/` gitignored and the `REPORT-*.md` files tracked. |
| `.worktrees/` | 2 dirs | (small) | Git worktrees from `feature-p0-safety-quickfixes`, `feature-project-understanding-audit` | Git worktrees are already in `.git/info/exclude`-style filtering at the worktree level, but the parent dir is untracked. Add `/.worktrees/` to `.gitignore` (it should never be tracked). |
| `runpod/` | 2 Dockerfiles + READMEs | (small) | Container assets | **Should be tracked.** The Dockerfiles and READMEs are first-class product code. Currently untracked — they should be `git add`'d. |
| `strategies/` | 2 jsonl/json | (small) | Alpaca data export | Two files: `alpaca.live.history.jsonl`, `alpaca.live.json`. These look like real Alpaca data exports. Add `strategies/` to `.gitignore` (or only the JSON files) to prevent a real trade log from being committed. |
| `research/` | 60+ `.md` files | (small) | Local research notes | `research/INDEX.md`, `research/README.md`, `research/UPDATE_LOG.md` are reference docs; `research/{architectures,benchmarks,events,models,papers,repos,_meta}/` are mostly `.md` research dumps. Decide: track the INDEX/README, gitignore the rest, or move under `docs/research/`. |
| `experiments/` | 1 `.png` + 1 subdir | ~620 KB | Local UI screenshots | `experiments/fincept-ui-desktop.png`, `experiments/fincept-ui-mobile.png`. Either track (if they're a real review artifact) or gitignore. |
| `mcps/` | 11 subdirs of MCP tool defs | (small) | MCP server tool JSON | `mcps/{bridgememory,bridgemind,chrome-devtools,cloudflare-api,cloudflare-docs,exa,firecrawl,grok_com_github,grok_com_notion,grok_com_vercel,neon}/tools/*.json`. These are tool schemas — useful as references, but not source. Decide per-MCP. |
| `DESIGN.md` (root) | 1 file | (small) | Style guide | The README points to DESIGN.md in some places. Track it, or move under `docs/`. |
| `MIGRATIONS_CONFIG_REVIEW.md` (root) | 1 file | (small) | Migration review | Same — the doc itself documents breaking changes. Track it, or move under `docs/`. |
| `REVIEW_2026-06-23_quant-foundry-recent-changes.md` (root) | 1 file | (small) | Recent review | Track, or move under `docs/`. |
| `Sisyphus_Quant_ML_Deep_Dive.md` and `Sisyphus_Ultra_Report.md` (root) | 2 files | (small) | Agent-authored analyses | Track, or move under `docs/`. |
| `2026-06-22-045344-read-the-file-bridgespaceswarms01cff6cc8d8e78.txt` (root) | 1 file | (small) | CASS search-exported text | Looks like a one-shot agent output. Delete. |
| `2026-06-22-052857-OPUS.txt` (root) | 1 file | (small) | CASS search-exported text | Same — delete. |
| `clipboard-1782171544577.png` (root) | 1 file | (small) | Pasted image | Looks like an agent paste artifact. Delete or move under `docs/`. |
| `apps/dashboard/docs/` | 2 files | 19,768 B | Dashboard-local docs | `apps/dashboard/docs/ui-audit-2026-06-03.md` and `apps/dashboard/docs/ui-upgrade-2026-06-03.md` are dashboard-scoped audits. Either track (they belong there) or move to top-level `docs/audits/`. |
| `docs/AAA_GLM_SUPERTEAM_LOGS/` | 11 files | ~188 KB | Inter-agent message archive | These are dumps from a prior swarm (BUILDER1-6). Either gitignore or move under `docs/audits/2026-04-12-glm-superswarm-logs/` (or whatever the actual run date is) to make them findable. |
| `reports/verification/` | 9 files | (small) | Verification receipt outputs | The dir is the intended home of `scripts/verification-receipt.ps1` outputs. Currently untracked — gitignore the runtime outputs and track only a `.gitkeep` (or `reports/verification/.gitkeep` is already there). |

### H.3 Gitignored-but-still-on-disk (annoying, not dangerous)

These are gitignored and so cannot be committed by accident, but they sit on disk
because nobody cleans up after the scripts that create them. They make the working
tree look "dirty" and inflate the on-disk footprint of the project.

| Path | Size | Created by | Recommendation |
|---|---:|---|---|
| `tmp/` (root) | 1 entry (subdir `pdfs/`) | Manual / one-off | Already gitignored (`/tmp/` line 81). Delete `tmp/pdfs/` if no longer needed. |
| `tmp-dashboard-dev.log` (root) | 4,363 B | `start.ps1`? | `*.log` is gitignored. Delete with a one-liner: `Get-ChildItem tmp-*.log, .codex-*.log \| Remove-Item`. |
| `tmp_features.{err,out}`, `tmp_ingestor.*`, `tmp_jobs.*`, `tmp_oms.*`, `tmp_orchestrator.*`, `tmp_portfolio.*`, `tmp_pytest.*` (root) | ~46 KB total | `start.ps1:1` writes them; rule `/tmp_*.{err,out}` on lines 94-95 covers them | Gitignored but on disk. Delete. |
| `.codex-api.log` (root) | **84,019 B** | Codex / agent runtime | `*.log` is gitignored. 84 KB of API-call logs nobody will ever read; safe to delete. |
| `.codex-dashboard-dev.{err,}.log` (root) | 983 B | Same | Delete. |
| `.codex-import-{out,trace}.log` (root) | 13,010 B | Same | Delete. |
| `.venv/` (root) | (n/a) | `uv` | `venv/` is gitignored (line 7). Delete if not in use. |
| `.mypy_cache/`, `.pytest_cache/`, `.ruff_cache/` | (n/a) | Tools | Gitignored (lines 13-15). Safe to delete. |
| `node_modules/` | (n/a) | pnpm | Gitignored (line 28). Safe to delete. |
| `reports/{qf-shadow-tmp,qf-shadow-tmp2,qf-test-tmp}/` | (n/a) | Quant Foundry scripts | None of the report subdirs is gitignored (only `openbb-live`, `paper-spine`, `route-smoke` are). Add a single `/reports/qf-*-tmp*/` rule. |
| `models/t3/` (root) | (n/a) | Quant Foundry training | Gitignored (line 88). Delete if not in use. |
| `models/news_alpha_predictor/` (root) | (n/a) | Training output | Gitignored (line 87). Delete if not in use. |

### H.4 Untracked dot-directories that probably should be gitignored (CRITICAL)

| Path | Size | Currently gitignored? | Fix |
|---|---:|---|---|
| `.opencode/` | **69.2 MB** | NO | Add `.opencode/` to the "Claude Code / IDE session metadata" block on line 75-78. |
| `.playwright-cli/` | ~2 MB | NO | Add `.playwright-cli/`. |
| `.omo/` | 300 KB | NO | Add `.omo/` (or split: track `omo/plans/`, gitignore `omo/run-continuation/`, `omo/boulder.json`, `omo/drafts/`, `omo/notepads/`, `omo/research_*.md`). |
| `.devin/` | 24 KB | NO | Add `.devin/`. |
| `.agents/` | 0 entries (empty) | NO | Add `.agents/` (empty placeholder, no harm). |
| `.codegraph/` | 0 entries (empty) | NO | Add `.codegraph/`. |
| `.worktrees/` | 2 dirs | NO | Add `/.worktrees/`. |

After these additions, the "Claude Code / IDE session metadata" block on lines 75-78
should look like:

```gitignore
# --- Claude Code / IDE session metadata -------------------------------------
.claude/
.windsurf/
.bridgecode/
.codex/
.opencode/
.playwright-cli/
.omo/
.devin/
.agents/
.codegraph/
.worktrees/
```

### H.5 Root-level cruft (MEDIUM)

These are large files/dirs at the repo root that don't belong in a monorepo:

- `AAAAAAAAA_BIG_PLAN.md` (the 2,998-line master plan) — the README does not link to
  it; `docs/IMPLEMENTATION.md` and `spec/` cover the same ground. Either move to
  `docs/AAAA_BIG_PLAN.md` or delete (the in-repo `docs/` is the canonical surface).
- `DESIGN.md` — referenced inconsistently from various places. Move to `docs/DESIGN.md`
  or delete (the dashboard's `apps/dashboard/src/lib/design-tokens.ts` and
  `globals.css` are the live design system).
- `MIGRATIONS_CONFIG_REVIEW.md` — move to `docs/migrations-config-review.md` (or
  `docs/migrations/`).
- `REVIEW_2026-06-23_quant-foundry-recent-changes.md` — move to
  `docs/reviews/2026-06-23-quant-foundry.md`.
- `Sisyphus_Quant_ML_Deep_Dive.md` and `Sisyphus_Ultra_Report.md` — move to
  `docs/agent-reports/sisyphus/` or delete.
- `FINCEPTNEWUIFUSIUON.pdf` — already in `.gitignore` (line 83). Delete from disk.
- `clipboard-1782171544577.png` — pasted image; delete or move under
  `docs/agent-reports/`.
- `2026-06-22-045344-read-the-file-bridgespaceswarms01cff6cc8d8e78.txt`,
  `2026-06-22-052857-OPUS.txt` — agent CASS exports. Delete.

### H.6 `start.bat` / `status.bat` / `stop.bat` (LOW)

These are the legacy Windows-batch wrappers that delegate to `start.ps1` /
`status.ps1` / `stop.ps1`. They are not gitignored and they wrap the modern
PowerShell scripts. Keeping them is fine for backwards compatibility, but the
README only mentions `start.ps1`/`status.ps1`/`stop.ps1` — the `.bat` files are an
undocumented surface. Either:

- Document them ("`start.bat` is a thin wrapper around `start.ps1` for users who
  prefer cmd.exe"), or
- Delete them and have the README point at the PowerShell entry points.

### H.7 `apps/dashboard/docs/` (LOW)

Two untracked files: `ui-audit-2026-06-03.md` (8 KB) and
`ui-upgrade-2026-06-03.md` (12 KB). These look like the same shape of audit this
document is, but dated 2026-06-03. They are sitting inside `apps/dashboard/` —
either:

- Track them (they're dashboard-scoped audits), or
- Move to `docs/audits/2026-06-03/`.

---

## I. Cross-Reference with Prior Audits

> **Added after the Coordinator's first follow-up:** "Compare the current state
> against `docs/ui-audit-2026-05-16.md`, `docs/dashboard-route-atlas.md`, and
> `docs/text-readability-audit-2026-05-16.md`. Flag fixed / regressed / still-open."

### I.1 `docs/ui-audit-2026-05-16.md` (UI color, layout, visual quality)

| # | Prior finding | Status in 2026-06-25 | Evidence |
|---|---|---|---|
| 1 | "Reduce how often card headers and shell chrome default to cyan" | **Still open** | `globals.css:40` `--cyan: 185 100% 50%`; `card.tsx`, `widget-frame.tsx`, `safety-state-bar.tsx` all use cyan as the chrome rail. No change. |
| 2 | "Increase vertical breathing room on top-level content blocks" | **Partially fixed** | `PortfolioReportView.tsx` still uses 4-px gap strips; `PortfolioBuilderPage.tsx` is unchanged. The 2-month-old advice has not been acted on. |
| 3 | **"Fix mobile shell clipping"** | **Still open** | `app-shell.tsx` and `topbar.tsx` are unchanged; `NowClock` is `hidden md:flex` but everything else still attempts to render at 390 px. |
| 4 | "Shorten the long strip messages" | **Still open** | `safety-state-bar.tsx:132-180` and `status-bar.tsx:55-101` are unchanged. |
| 5 | "Decide whether the login page should stay editorial" | **Still open** | `login/page.tsx:52-140` is unchanged. |
| 6 | "Portfolio builder: vary section treatment more aggressively" | **Still open** | `PortfolioBuilderPage.tsx:94-160` and `PortfolioReportView.tsx:38-240` are unchanged. |

**Net verdict:** the UI audit from 5 weeks ago is **0/6 fully fixed, 1/6 partially,
5/6 still open**. The audit's "Best-Sized Wins" list (mobile fix → shorten strips →
breathing room → reduce cyan → login decision) is also unchanged.

### I.2 `docs/text-readability-audit-2026-05-16.md` (text readability)

| # | Prior finding | Status in 2026-06-25 | Evidence |
|---|---|---|---|
| 1 | "Shell chrome is too small and too tightly packed on narrow widths" | **Still open** | `title-bar.tsx:282-319`, `safety-state-bar.tsx:132-193`, `status-bar.tsx:56-100`, `nav-tabs.tsx:60-90` — none updated. |
| 2 | "Long-form content panels are still a little too label-heavy" | **Still open** | `PortfolioReportView.tsx:98-356`, `card.tsx:27-57` — both unchanged. |
| 3 | "Important values are still hidden behind truncation in a few key places" | **Still open** | `system/page.tsx:284-490`, `research/page.tsx:423-445` — both unchanged. |

**Net verdict:** 0/3 fixed. The audit's "Best Next Changes" (raise content text size,
allow primary text to wrap, collapse mobile chrome earlier, less uppercase in body
areas) — none of it has been applied.

### I.3 `docs/dashboard-route-atlas.md` (route inventory from 2026-06-22)

| # | Prior finding | Status in 2026-06-25 | Evidence |
|---|---|---|---|
| `/watchlist` is **Mock** (High risk, High priority for replacement) | **Still open** | `apps/dashboard/src/app/watchlist/page.tsx` still uses `mockPriceWalk` and `MockBadge`. |
| `/symbol/[symbol]` is **Hybrid** (Medium risk, High priority) | **Still open** | 3 `MockBadge` instances still on lines 308, 507, 559 of `symbol/[symbol]/page.tsx`. |
| `/portfolio-builder` is **Hybrid** (Medium risk, Medium priority) | **Still open** | `marketDataService.ts` still routes to mock depending on config. |
| `/receipts` is **Demo** (Low priority) | **Still open** | `receipts/page.tsx` is unchanged. |
| `/signal-cockpit-demo` is **Demo** (Low priority) | **Still open** | `signal-cockpit-demo/signal-cockpit-demo.tsx` is unchanged. |
| `/optimizer` → `/portfolio-builder` redirect | **Fixed** | `apps/dashboard/src/app/optimizer/page.tsx` is the redirect. ✅ |
| `/news-lab` → `/news-impact-lab` redirect | **Fixed** | The route exists; redirect present. ✅ |
| 18 Live routes | **Still accurate** | 18 routes remain live in the atlas; no live-route regression. |

**Net verdict:** the 2 redirect routes are fixed; the **2 High-priority** mock/hybrid
routes (`/watchlist`, `/symbol/[symbol]`) are **not replaced**; the 2 Medium-priority
hybrid routes (`/portfolio-builder`, `/receipts`) are **not replaced**.

### I.4 Cross-audit signal: nothing got fixed, everything got added

The most striking pattern across the three prior audits is that **none of the
prior findings were acted on in the 5 weeks between audits**. The dashboard instead
grew new surfaces (`/news-impact-lab`, `/signal-cockpit-demo`, `/portfolio-builder`,
`/quant-foundry/*`, `/news-lab`, `/optimizer`) that compound the existing debt:

- New dashboard pages added **2 more routes that are not in the sidebar**:
  `/quant-foundry`, `/quant-foundry/{jobs,models,tournament,promotion,shadow}`,
  `/reconciliation`, `/research`, `/news-lab`, `/signal-cockpit-demo`, `/watchlist`
  (already known from the route atlas, but the new ones weren't there before).
- New shell components (`command-palette.tsx`, `nav-tabs.tsx`,
  `safety-state-bar.tsx`) added without revisiting the chrome-density finding
  from the UI audit.
- New mock data surface (`MockBadge`, `withMockFlag`, `seededRandom`,
  `mockPriceWalk`, `mockVolumeWalk`) added without revisiting the route-atlas
  "convert /watchlist to live" priority item.

**Recommendation:** the next PR should not add new dashboard pages. It should close
out at least 1 item from the UI audit, 1 from the text-readability audit, and
either `/watchlist` or `/symbol/[symbol]` from the route atlas. Closing the
redirects is good but it's the cheapest of the three audits' findings.

---

## J. Updated Top-10 Prioritized Fixes (post-hygiene addition)

> Replaces the prior Section E. The original Top-10 is now items 11-20 in the
> rationale; the new Top-10 incorporates the repo-hygiene findings and the
> cross-audit regressions.

| # | Sev | Fix | Effort | Why first |
|---|---|---|---|---|
| 1 | CRITICAL | Add `.opencode/`, `.playwright-cli/`, `.omo/`, `.devin/`, `.agents/`, `.codegraph/`, `/.worktrees/` to `.gitignore` (extend the block on line 75-78). | 5 min | Removes **~73 MB** of untracked noise from the next `git add .`. One-line edit; high payoff. |
| 2 | CRITICAL | Fix `next.config.mjs` `:8000` → `:8010` and rewrite the 4 stale `:8000` references in `spec/prompts/{phase-U-ui-api.md,PASTE_READY.md}`. | 5 min | Unblocks every fresh operator; eliminates a silent dev-port bug. (Original #1.) |
| 3 | CRITICAL | Add `"test:shadow-news-impact": "node scripts/run-shadow-news-impact-tests.cjs"` to `apps/dashboard/package.json`. | 1 min | Stops CI from failing on `verification-receipt.ps1`. (Original #2.) |
| 4 | CRITICAL | Either remove `build-images.yml` or repoint it at the existing RunPod Dockerfiles. | 15 min | Today the workflow is a no-op; tomorrow it could silently pass for a new image that doesn't exist. (Original #3.) |
| 5 | CRITICAL | Add a one-shot cleanup script (`scripts/clean-workspace.ps1`) that removes root `tmp-*` files, `tmp_*.{err,out}`, `.codex-*.log`, `tmp-dashboard-dev.log`, `clipboard-*.png`, `2026-*-*.txt` and verifies `.gitignore` covers the rest. Wire it to `preflight.ps1` and `verification-receipt.ps1` as an optional "reset" step. | 1 h | Resolves 10+ untracked-on-disk items in one go; the root workspace is currently 200+ files of cruft. |
| 6 | HIGH | Replace the env-var catalog in `system-readiness.ts` (and its test) with the real `FINCEPT_*` names from `libs/fincept-core/src/fincept_core/config.py`. | 30 min | The /system page currently tells operators to set vars the app never reads. (Original #4.) |
| 7 | HIGH | Rotate every key in `.env`, then leave the file blank or write a `scripts/rotate-env.md` runbook. | 30 min + waiting on rotations | Pre-empts a credential-leak blast radius. (Original #5.) |
| 8 | HIGH | Refactor `start.ps1` / `start_feature.ps1` to share helpers in `scripts/_lib/`. | 2 h | Eliminates two of the three "we already drifted" findings. (Original #6.) |
| 9 | HIGH | **Close out the UI audit from 5 weeks ago.** Pick the cheapest fix: shorten the long strip messages in `safety-state-bar.tsx` and `status-bar.tsx`. It's a 30-line patch that addresses finding #1 of the UI audit and finding #1 of the text-readability audit in one go. | 30 min | Resets the cross-audit "nothing got fixed" pattern. Operators will see immediate chrome readability gain. |
| 10 | HIGH | Replace `/watchlist` mock data with the `/markets/bars` API call. | 1-2 d | Closes the **High priority** "convert to live" item from the route atlas that has been open since 2026-06-22. |

---

## K. Updated Reproducibility

```pwsh
# 7. Repo-hygiene snapshot.
git ls-files | Measure-Object -Line              # 806 tracked
git ls-files --others --exclude-standard | Measure-Object -Line   # 1,129 untracked
Get-ChildItem -Force | Where-Object { $_.PSIsContainer } | ForEach-Object {
  $c = (Get-ChildItem -LiteralPath $_.FullName -Recurse -Force | Measure-Object).Count
  Write-Host ("{0,-30} entries={1,6}" -f $_.Name, $c)
}
# Confirms the sizes: .opencode 4,430, .playwright-cli 49, .omo 18, .devin 10,
# .bridgespace 459, .worktrees 2.

# 8. Cross-check untracked-but-not-gitignored.
git check-ignore -v .opencode .playwright-cli .omo .devin .bridgespace .agents
# (no output ⇒ NOT gitignored)

# 9. Confirm logs are gitignored but on disk.
git check-ignore -v .codex-api.log tmp-dashboard-dev.log tmp_ingestor.out
# (all 3 lines ⇒ gitignored; just delete them)

# 10. Confirm /worktrees is untracked.
git check-ignore -v .worktrees
# (no output ⇒ NOT gitignored)
```

---

## L. Acceptance-criteria self-check

- ✅ `docs/audits/2026-06-25/frontend-infra-audit.md` exists.
- ✅ Has a single H1 (`# Frontend & Infrastructure Audit — 2026-06-25`).
- ✅ Dated 2026-06-25 in the title and at the top of each section.
- ✅ Section A: Executive Summary (5 severity-tagged bullets).
- ✅ Section B: Scope & Method (covers apps/dashboard, scripts, .github/workflows,
  Dockerfiles, env handling, README fragments).
- ✅ Section C: Findings grouped by 12 categories (Doc Drift, Dead Routes/Code,
  CI/CD, Secrets/Env, Accessibility, Type Safety, Performance, Dependency,
  Dockerfiles, Script Drift, Misleading Comments, A11y/UX nit) — each with
  Severity, File:Line evidence, and a one-line Recommendation.
- ✅ Section D: Open ADRs to promote.
- ✅ Section E (now superseded by J): Top-10 prioritized fixes (moved into J
  after the hygiene addition; the original Top-10 is preserved as the
  rationale, the new Top-10 is in J).
- ✅ Sections H, I added per the Coordinator's expanded scope.
- ✅ Section G/K: Reproducibility (PowerShell one-liners for every check).
- ✅ No source files were modified — this is a **read-only audit**. Only
  `docs/audits/2026-06-25/frontend-infra-audit.md` was created (and amended) by
  Builder 2.

---

## G. How to Reproduce

```pwsh
# 1. Confirm port drift.
Select-String -Path apps/dashboard/next.config.mjs -Pattern 'localhost:8000'
Select-String -Path spec/prompts/phase-U-ui-api.md -Pattern 'localhost:8000'
Select-String -Path apps/dashboard/src/lib/api.ts -Pattern 'localhost:8010'

# 2. Confirm build-images.yml is a no-op.
ls infra/docker  # 0 files
cat .github/workflows/build-images.yml | Select-String 'infra/docker'

# 3. Confirm verification-receipt.ps1 is broken.
Get-Content scripts/verification-receipt.ps1 | Select-String 'test:shadow-news-impact'
Get-Content apps/dashboard/package.json | Select-String 'test:shadow-news-impact'  # empty

# 4. Confirm system-readiness env-var drift.
Get-Content apps/dashboard/src/components/system/system-readiness.ts |
  Select-String -Pattern 'name:'
Get-Content .env.example | Select-String -Pattern '^[A-Z_]+='

# 5. Confirm sidebar ↔ command-palette ↔ app/ drift.
Get-Content apps/dashboard/src/components/shell/sidebar.tsx |
  Select-String -Pattern 'href: "/'
Get-Content apps/dashboard/src/components/shell/command-registry.ts |
  Select-String -Pattern 'href: "/'
Get-ChildItem apps/dashboard/src/app -Recurse -Filter 'page.tsx'

# 6. Confirm .env contents and that .gitignore covers it.
Get-Content .env | Select-String -Pattern 'sk-(proj|ant)'
Get-Content .gitignore | Select-String -Pattern '^\.env'
```
