# Audit Fixes — 2026-06-27

**Date:** 2026-06-27
**Scope:** Fixes for 7 of 8 CRITICAL/HIGH findings from the 2026-06-25 audit re-verification.
**Status:** All 7 fixes complete and verified. M-11 (config consolidation) deferred.

---

## Summary

A 2026-06-25 audit found 11 CRITICAL/HIGH severity issues across the platform. A re-verification on 2026-06-27 confirmed 3 were already fixed, 3 were partially fixed, and 5 were still present. This document covers the 7 fixes applied on 2026-06-27 (5 still-present + 2 partially-fixed). M-11 (config split) is deferred as a large refactor.

**Test results after all fixes:**
- api: 485 passed
- oms: 91 passed
- orchestrator: 82 passed
- strategy_host: 71 passed
- fincept-core: 316 passed
- fincept-db: 59 skipped (needs postgres)
- `ruff check` + `ruff format`: clean across all changed files

---

## M-2: Audit failures silently dropped (CRITICAL)

**Problem:** `with contextlib.suppress(Exception):` wrapped `audit.append()` calls across 8 files. The audit trail became best-effort exactly when the system was under stress (Postgres down, connection pool exhausted), with no metric or log to detect the outage.

**Fix:** Added a `safe_append` helper to `fincept_db/audit.py` that logs failures at WARNING level with structured context (actor, event_type, correlation_id) instead of silencing them. Replaced all 8 `contextlib.suppress(Exception)` call sites.

**Files changed:**
- `libs/fincept-db/src/fincept_db/audit.py` — Added `safe_append()` function (lines 42-75)
- `services/oms/src/oms/main.py` — 3 audit suppressions → `audit.safe_append()` (lines 86, 104, 116)
- `services/api/src/api/routes/orders.py` — 1 audit suppression → `audit.safe_append()` (line 204)
- `services/orchestrator/src/orchestrator/router.py` — 1 audit suppression → `audit.safe_append()` (line 133)
- `services/oms/src/oms/alpaca/marks.py` — 1 provider-data suppression → try/except with logging (line 59)
- `services/oms/src/oms/alpaca/runtime.py` — 1 on_terminal callback suppression → try/except with logging (line 235)
- `services/api/src/api/ws.py` — 1 Redis cleanup suppression → try/except with logging (line 156)
- `services/strategy_host/src/strategy_host/main.py` — 1 Redis cleanup suppression → try/except with logging (line 72)
- `libs/fincept-core/src/fincept_core/heartbeat.py` — 1 Redis cleanup suppression → try/except with logging (line 101)

**Pattern for other agents:** When you need best-effort audit writes, use `await audit.safe_append(...)` instead of `with contextlib.suppress(Exception): await audit.append(...)`. The `safe_append` function returns `str | None` (event_id on success, None on failure) so callers can check if the write succeeded.

**Note:** `contextlib.suppress(asyncio.CancelledError)` and `contextlib.suppress(NotImplementedError)` patterns (used for task cleanup and Windows signal handling) were NOT changed — those are correct and intentional.

---

## M-3: next.config.mjs comment wrong port (CRITICAL)

**Problem:** The comment in `apps/dashboard/next.config.mjs` said "default localhost:8000" but the API runs at `:8010`.

**Fix:** Updated the comment to say `localhost:8010`. Code unchanged — the dashboard uses `NEXT_PUBLIC_API_URL` env var, no hardcoded default.

**File changed:** `apps/dashboard/next.config.mjs` (lines 4-6)

---

## M-5: Dashboard env-var catalog wrong name (CRITICAL)

**Problem:** `apps/dashboard/src/components/system/system-readiness.ts` listed `FINCEPT_API_URL` in the `REQUIRED_ENV_VARS` catalog, but the dashboard actually uses `NEXT_PUBLIC_API_URL` (the Next.js convention for client-side env vars). Operators following the catalog would set the wrong env var.

**Fix:** Changed `FINCEPT_API_URL` → `NEXT_PUBLIC_API_URL` in the catalog. Verified the dashboard uses `NEXT_PUBLIC_API_URL` in `src/lib/api.ts:99` and `src/app/system/page.tsx`.

**File changed:** `apps/dashboard/src/components/system/system-readiness.ts` (line 153)

---

## M-6: Hallucinated LLM model names (CRITICAL)

**Problem:** `apps/dashboard/src/app/api/portfolio-report/route.ts` referenced `gpt-5.5` and `claude-opus-4-7` which don't exist in any OpenAI/Anthropic API.

**Fix:** Updated to real production model names:
- `"gpt-5.5"` → `"gpt-4o"`
- `"claude-opus-4-7"` → `"claude-3-5-sonnet-20241022"`

**File changed:** `apps/dashboard/src/app/api/portfolio-report/route.ts` (lines 18-19)

---

## M-8: Path traversal in training.py (HIGH)

**Problem:** `services/api/src/api/training.py`'s `_validate_input_path` only checked `is_file()` with no root-prefix containment. A caller bypassing the route layer (direct store call) could pass any readable file path.

**Fix:** Added `default_approved_roots().resolve()` call to `_validate_input_path`, mirroring the `ApprovedRoots.resolve()` pattern from `services/api/src/api/routes/backtest.py:127`. Catches `ApprovedRootsError` and converts to `TrainingValidationError` to preserve the function's single-exception-type contract.

**Files changed:**
- `services/api/src/api/training.py` — `_validate_input_path` now uses `ApprovedRoots.resolve()` (lines 268-292)
- `services/api/tests/test_models_train.py` — Fixture now patches `api.training.default_approved_roots` in addition to `api.routes.models._get_approved_roots` (line 93)
- `services/api/tests/test_training.py` — `test_rejects_missing_input_file` updated to expect "rejected" match instead of "not found" (line 307)

**Pattern for other agents:** When adding path validation that uses `ApprovedRoots`, always:
1. Import `default_approved_roots` from `fincept_core.datasets`
2. Call `default_approved_roots().resolve(input_path)` which returns a `ResolvedPath` with `.path` and `.inside_root`
3. Catch `ApprovedRootsError` and convert to your service's validation error type
4. In tests, patch `default_approved_roots` to admit `tmp_path` via `ApprovedRoots(roots=[], extra_dev_roots=[tmp_path])`

---

## M-9: JWT query string leak in ws.py (HIGH)

**Problem:** `services/api/src/api/ws.py`'s `_authenticate` function had a `?token=` query-string fallback for WebSocket auth. Query strings are logged by web servers in access logs, leaking JWTs.

**Fix:** Removed the `?token=` query-string fallback entirely. WebSocket clients must now use the `Authorization: Bearer <token>` header. If the header is missing or invalid, the function returns `None` (unauthenticated).

**File changed:** `services/api/src/api/ws.py` (lines 55-71)

**Pattern for other agents:** Never accept JWTs via query strings. Always use the `Authorization` header. If a WebSocket client can't set headers (e.g. browser EventSource), use a short-lived single-use token exchange via a REST endpoint instead.

---

## M-10: OMS Alpaca unsafe error handling (HIGH)

**Problem:** `services/oms/src/oms/alpaca/runtime.py`'s `submit_intent` only caught `AlpacaError`, not `httpx.HTTPError`, `OSError`, or `TimeoutError`. A network blip (DNS failure, connection reset, timeout) would crash the OMS loop.

**Fix:** Added `except (httpx.HTTPError, OSError, TimeoutError)` clause after the existing `except AlpacaError` block. Network-level errors are:
- Logged at **error** level with `order_id`, `reason="network_error"`, exception type, and message
- Mapped to `OrderStatus.REJECTED` (same terminal state as an Alpaca-side rejection)
- Returned as an `IntentResult` so the caller gets a single exception type to handle

**File changed:** `services/oms/src/oms/alpaca/runtime.py` (lines 111-141)

**Pattern for other agents:** When catching errors from external HTTP clients, always catch the full exception hierarchy:
- `httpx.HTTPError` (base for all httpx errors)
- `OSError` (base for socket-level errors)
- `TimeoutError` (asyncio timeouts)
- The vendor's custom exception type (e.g. `AlpacaError`)

Map network errors to a safe terminal state (REJECTED) rather than crashing the loop.

---

## M-11: Config split (HIGH) — DEFERRED

**Problem:** Configuration is split between `pydantic-settings` (in `libs/fincept-core/src/fincept_core/config.py`) and 50+ raw `os.environ.get()` calls across `services/` and `libs/` with two competing naming conventions (`FINCEPT_*` vs bare names).

**Status:** Deferred. This is a large refactor that needs careful scoping to avoid breaking env-var compatibility. Should be a separate focused task.

**Files affected (not changed):**
- `services/api/src/api/main.py` — 4 raw calls
- `services/quant_foundry/src/quant_foundry/gateway.py` — 15 raw calls
- `services/api/src/api/training.py` — 4 raw calls
- Plus 30+ more across other services

**Recommendation for future work:** Migrate raw `os.environ.get()` calls to the centralized `Settings` class in `fincept_core/config.py` one service at a time. Keep backward-compat env-var aliases during the migration.

---

## Findings already fixed (not touched in this session)

| # | Finding | Status |
|---|---------|--------|
| M-1 | Kill-switch state divergence | Already fixed — OMS reads from Redis via `_sync_from_redis()` |
| M-4 | verification-receipt.ps1 npm scripts | Already fixed — scripts exist in package.json |
| M-7 | Path traversal in backtest.py | Already fixed — uses `ApprovedRoots.resolve()` |

---

## File Change Summary

```
libs/fincept-core/src/fincept_core/heartbeat.py        — M-2: logging on cleanup failure
libs/fincept-db/src/fincept_db/audit.py                — M-2: added safe_append() helper
services/api/src/api/routes/orders.py                  — M-2: audit.safe_append()
services/api/src/api/training.py                       — M-8: ApprovedRoots gate in _validate_input_path
services/api/src/api/ws.py                             — M-9: removed ?token= fallback + M-2: logging on cleanup
services/orchestrator/src/orchestrator/router.py       — M-2: audit.safe_append()
services/oms/src/oms/main.py                           — M-2: audit.safe_append() (3 sites)
services/oms/src/oms/alpaca/marks.py                   — M-2: logging on provider-data failure
services/oms/src/oms/alpaca/runtime.py                 — M-10: catch network errors + M-2: logging on callback failure
services/strategy_host/src/strategy_host/main.py       — M-2: logging on cleanup failure
services/api/tests/test_models_train.py                — M-8: fixture patches default_approved_roots
services/api/tests/test_training.py                    — M-8: test expects "rejected" match
apps/dashboard/next.config.mjs                         — M-3: comment :8000 → :8010
apps/dashboard/src/components/system/system-readiness.ts — M-5: FINCEPT_API_URL → NEXT_PUBLIC_API_URL
apps/dashboard/src/app/api/portfolio-report/route.ts   — M-6: gpt-4o + claude-3-5-sonnet
```

---

*End of report.*
