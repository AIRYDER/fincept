# Python Backend Audit — Fincept Terminal

**Date:** 2026-06-25
**Author:** Builder 1 (swarm audit pass)
**Scope:** `libs/*`, `services/*`, and `scripts/*.py` (excluding test directories).
**Method:** Static read of every Python module in scope (5 libs, 17 services, 16 scripts), cross-referenced against:
- `docs/quant-ml-audit/audit-2026-06-03.md` (the prior 2026-06 quant/ML pass)
- `docs/codebase-audit-2026-05-16.md` (the May codebase review)
- `reports/CORE_LOGIC_REVIEW_2026-06-23.md` (the 2026-06-23 core-logic review, two days ago)
- `docs/audits/2026-06-25/frontend-infra-audit.md` (Builder 2's parallel audit)
- `reports/CORE_LOGIC_REVIEW_2026-06-23.md` round 2 (kill-switch divergence independently surfaced by Scout 1)
No code was modified. Every finding cites a specific file:line. **Round 2 (post-review)**: corrected D-C-8 false positive (`HttpRunPodClient.dispatch` is implemented), added the kill-switch divergence as new blocker CRITICAL #1 (per Scout 1's addendum), fixed D-C-9 line drift (273 → 411), and tagged all 2026-06-23 cross-references with "verified against current HEAD".

**Finding count after round 2:** 66 findings across 9 categories (Security: 7, Correctness: 15, Error Handling: 10, Observability: 3, Dead Code: 5, Type Safety: 5, Configuration: 10, Performance: 4, Test Coverage: 8) plus 1 RESOLVED entry (G-D-1, trace-only). Removed 1 false positive (HttpRunPodClient stub claim), added 2 (kill-switch divergence + RESOLVED note for the removed stub).

---

## A. Executive Summary

The Python backend is in **good shape for its stage** — frozen Pydantic schemas, a typed event bus, a shared portfolio-math kernel, a paper-only OMS with an Alpaca adapter behind a runtime gate, a risk gate that backs both backtest and live, and a working ML lifecycle (train → walk-forward CV → promote → hot-reload → shadow → predict → JSONL log). The `paper_spine_replay.py` proves the end-to-end chain. The `docs/quant-ml-audit/audit-2026-06-03.md` from three weeks ago is mostly already addressed; only one of its P0 items remains open (`MAX_DAILY_LOSS_USD` not enforced).

The most material problems in the backend cluster in three places:

1. **[CRITICAL] Kill-switch state divergence between API and OMS** *(independently flagged by Scout 1, missed in round 1 of this audit)*. The API writes the kill-switch state to a Redis key `control:kill_switch:state` (`services/api/src/api/routes/control.py:55, 242`). The OMS-side `KillSwitchState` (`services/risk/src/risk/state.py:39-73`) **never reads this Redis key** — it only listens for in-memory `AlertEvent` objects delivered by the bus consumer. An operator engaging the kill switch on the dashboard sees `engaged=True` from `GET /kill-switch`, but `check_intent` in the OMS may still allow trades because the bus consumer hasn't observed the alert yet (or the alert was already past the consumer group's `$` cursor). **This is the single most important production-safety gap in the entire backend.**

2. **[HIGH/CRITICAL] The OMS Alpaca path is unsafe by construction, even in paper mode.** `submit_intent` only catches `AlpacaError` (HTTP 4xx/5xx) — not `httpx.HTTPError`, `OSError`, or `TimeoutError`. A network blip during order submission produces a stuck order with `venue_order_id = NULL` and an audit row that shows only `PENDING_NEW`. The strategy re-submits, the broker gets a duplicate, the operator sees no signal. The background poll loop and the `on_terminal` callback both swallow exceptions, so terminal rejections can be lost entirely. `_new_order_shell` synthesises `Order` objects with `strategy_id="alpaca.unknown"` and `side=Side.BUY` as placeholders that *can be published unchanged* if the response is malformed.

3. **[HIGH] Configuration is split between `pydantic-settings` and 30+ raw `os.environ.get` calls**, with no single source of truth and two competing naming conventions (`FINCEPT_*` vs bare). Operators regularly set the wrong env var and discover this only at runtime. Multiple magic constants (OMS poll intervals, ingestor batch sizes, news thresholds, VIX regime bands) are not env-driven at all. `MARK_TTL_SEC` is documented as a `Settings` field but does not exist there — the access silently returns `None` under `extra="ignore"`. Risk caps (`MAX_NOTIONAL_USD_PER_SYMBOL` etc.) accept negative values, which would silently disable the gate.

4. **[MEDIUM] Several 2026-06-23 core-logic review items remain unfixed** (after re-verification against current HEAD). `MARK_TTL_SEC` still missing from `Settings`; `shadow_ledger._reload()` still crashes on a torn JSONL line; `shadow_settlement:199` still has `except Exception` around schema validation; `HttpRunPodClient.dispatch` was stubbed in 2026-06-23 but is **now fully implemented** at `runpod_client.py:232-309` (the round-1 audit inherited the stub claim without re-verification; the 2026-06-23 C3 finding is therefore **addressed**).

**Three previously-audit findings confirmed still open** (one P0 + two medium):
- `MAX_DAILY_LOSS_USD` not enforced (audit-2026-06-03 P0-1) — `services/risk/src/risk/checks.py` still has zero references.
- `_validate_input_path` in training still does only `is_file()` (codebase-audit-2026-05-16) — no root-prefix containment check.
- `bars_path` body field still accepts any path (codebase-audit-2026-05-16) — no root-prefix containment check.

Everything below is paper-only-reachable (no live-trading path is proposed). Where live-trading risks exist (Alpaca adapter, kill-switch persistence, missing config validation), they are flagged but not patched.

---

## B. Scope & Method

### B.1 Files audited

| Area | Files | LOC (approx) | Coverage |
|---|---|---:|---|
| `libs/fincept-core` | 14 (excl. `__init__.py`) | ~1.4k | full read |
| `libs/fincept-bus` | 4 | ~0.5k | full read |
| `libs/fincept-db` | 8 (excl. `migrations/`) | ~1.6k | full read |
| `libs/fincept-tools` | 8 (4 subpackages) | ~2.0k | full read |
| `libs/fincept-sdk` | 1 | ~0.2k | full read |
| `services/api` | 26 (incl. 16 routes) | ~5.0k | full read |
| `services/oms` | 12 (incl. 7 `alpaca/`) | ~2.5k | full read |
| `services/risk` | 3 | ~0.3k | full read |
| `services/orchestrator` | 5 | ~0.7k | full read |
| `services/portfolio` | 3 | ~0.4k | full read |
| `services/strategy_host` | 5 | ~1.0k | full read |
| `services/backtester` | 11 | ~2.5k | full read |
| `services/features` | 8 (incl. `transforms/`) | ~1.2k | full read |
| `services/ingestor` | 11 (incl. `binance/coinbase/kraken`) | ~2.0k | full read |
| `services/agents` | 23 (across 9 agents) | ~4.0k | full read |
| `services/quant_foundry` | 38 | ~7.0k | full read |
| `services/jobs` | 3 | ~0.4k | full read |
| `scripts/*.py` | 16 | ~1.5k | full read |
| **TOTAL** | **180** | **~34k LOC** | — |

### B.2 Cross-references against prior audits

| Audit | Date | Reused | New since |
|---|---|---|---|
| `docs/codebase-audit-2026-05-16.md` | 2026-05-16 | Findings #1 (path traversal in backtest/training), #2 (OpenBB port split — frontend scope) | **Not addressed:** backtest/training path validation; fakeredis is now declared in 10+ pyproject.toml files (resolved blocker). |
| `docs/quant-ml-audit/audit-2026-06-03.md` | 2026-06-03 | P0-1 (MAX_DAILY_LOSS_USD), P0-4 (Decision confidence → OrderIntent), P0-5 (explainability), P1-1 (backtester report metrics) | **P0-1 still open** (verified at `services/risk/src/risk/{checks,snapshot,state}.py` zero references). P0-4, P0-5, P1-1 partially addressed elsewhere in the audit. |
| `reports/CORE_LOGIC_REVIEW_2026-06-23.md` | 2026-06-23 | C1 (MARK_TTL_SEC), C2 (timestamp units), H1 (cost defaults), H2 (budget atomicity), H3 (CORS), H5 (module start TOCTOU), M1 (shadow_ledger reload), M2 (shadow_settlement bare except), M4 (empty callback secret), M8 (silent evidence-write) | **C3 (HttpRunPodClient.dispatch stub) — ADDRESSED.** Current code at `runpod_client.py:232-309` is fully implemented with httpx.Client, status-code classification, and proper DispatchResult. The audit's D-C-8 finding was inherited without re-verification and is a false positive. **All other C/H/M findings verified still open at current line numbers.** |

### B.3 Things I verified are clean (the "no finding" list)

- **All SQL is parameterised.** `libs/fincept-db/src/fincept_db/{bars,universe,ticks,features,audit,provider_data}.py` all use SQLAlchemy 2.x ORM with bound parameters. The only `text(f"...")` usages are in `libs/fincept-db/tests/conftest.py` against the hardcoded `TEST_DB_NAME`.
- **No `pickle.loads`, `yaml.load(`, `eval(`, or bare `exec(`** in production code. The Redis-Lua `eval` calls (`libs/fincept-core/src/fincept_core/leadership.py:46,51`) are server-side script execution.
- **No `subprocess.*(shell=True)`** anywhere in the audited tree. All `subprocess.Popen` and `asyncio.create_subprocess_exec` callers pass a list argv.
- **Every API HTTP route is auth-gated** except the intentional `/health` liveness probe (`services/api/src/api/main.py:103-106`) and the HMAC-protected `/quant-foundry/callbacks/runpod` (`quant_foundry.py:249-303`, verified: missing/bad HMAC headers → 401).
- **No real network calls in any test** — all HTTP is mocked via `respx.mock(...)`; the FastAPI route tests use `ASGITransport`.
- **Exa client SSRF defence is correct** — `libs/fincept-tools/src/fincept_tools/research/exa.py:113-115` rejects any URL that does not start with `https://`.
- **HMAC constant-time compare + skew check** in `services/quant_foundry/src/quant_foundry/signatures.py:67-73`.
- **Redaction fail-closed on unknown sensitive field names** in `libs/fincept-db/src/fincept_db/evidence_redaction.py:228-237`.

---

## C. Findings

Severity legend: **CRITICAL** = paper-trading deployment blocker · **HIGH** = first-priority fix · **MEDIUM** = code-quality / defensive · **LOW** = nice-to-have.

### C. Security

#### C-S-1. **[HIGH]** Path traversal via `bars_path` body field *(regression of 2026-05-16 finding)*

**File:** `services/api/src/api/routes/backtest.py:112-148`

`POST /backtest/run` accepts an arbitrary `bars_path` from the request body and turns it into `pathlib.Path(body.bars_path)` with only an `exists()` check. There is no `Path.resolve()` + root-prefix containment. An authenticated user can pass `C:\Windows\System32\drivers\etc\hosts`, `/etc/passwd`, or any other file readable by the api process, and the route will read it into memory, run a backtest on it, and persist the report. The 2026-05-16 audit flagged this; the fix has not landed.

**Evidence:**
```python
112: @router.post("/run")
113: async def post_run(
114:     body: RunBacktestRequest,
115:     _: dict[str, Any] = Depends(require_user),
116: ) -> dict[str, Any]:
118:     bars_path = pathlib.Path(body.bars_path)
119:     if not bars_path.exists():
120:         raise HTTPException(
121:             status_code=400,
122:             detail=f"bars_path does not exist: {bars_path}",
123:         )
```

**Recommendation:** Resolve against an allow-listed root (`DATA_DIR` or `BACKTEST_DATA_DIR`), reject with 400 if `not bars_path.resolve(strict=True).is_relative_to(root)`.

---

#### C-S-2. **[HIGH]** Path validation in `TrainingStore` does not enforce a root prefix *(regression of 2026-05-16 finding)*

**File:** `services/api/src/api/training.py:252-259`

`_validate_input_path` docstring promises "refuse anything outside the repo root or with a `..` component". The implementation only checks `Path(input_path).is_file()`. A user-supplied `input_path` of `C:\Users\<you>\.ssh\id_rsa` is accepted; the trainer subprocess reads it; failure messages echo the bytes. The 2026-05-16 audit flagged this; the fix has not landed.

**Evidence:**
```python
252: def _validate_input_path(input_path: str) -> pathlib.Path:
253:     """The trainer reads a parquet file; refuse anything outside the repo
254:     root or with a ``..`` component (defence-in-depth even though the
255:     operator is trusted)."""
256:     p = pathlib.Path(input_path)
257:     if not p.is_file():
258:         raise TrainingValidationError(f"input path not found: {input_path}")
259:     return p
```

**Recommendation:** Replace with `Path.resolve(strict=True)` + `is_relative_to(allow_listed_root)`. Add a `FINCEPT_TRAINING_INPUT_ROOT` env var defaulting to `data/` for the allow-list.

---

#### C-S-3. **[HIGH]** WebSocket JWT passed via `?token=` query string

**File:** `services/api/src/api/ws.py:55-73`

The auth code falls back to accepting the JWT via the `?token=...` query string (browser WebSocket clients cannot easily set headers). Query-string tokens are routinely written to:
- HTTP access logs on the api, reverse proxy, and any intermediate hop.
- Browser history (WS handshake is initiated with the full URL).
- `Referer` headers on subsequent sub-resource fetches.
- APM/tracing tools that snapshot request URLs.

This converts a short-lived bearer token into a long-lived log artefact. With the dev-default JWT secret `"dev-only-change-me"` (21 chars, `libs/fincept-core/src/fincept_core/config.py:66`) it's literally a stable signing key until rotation.

**Evidence:**
```python
55: async def _authenticate(ws: WebSocket) -> dict[str, Any] | None:
62:     token: str | None = None
63:     auth = ws.headers.get("authorization")
64:     if auth and auth.lower().startswith("bearer "):
65:         token = auth.split(" ", 1)[1].strip()
66:     if token is None:
67:         token = ws.query_params.get("token")
```

**Recommendation:** Drop the `?token=` fallback; require the `Authorization` header. On the client side, exchange a one-time ticket from `POST /auth/ws-ticket` inside the first WS frame.

---

#### C-S-4. **[MEDIUM]** `Settings.JWT_SECRET` accepts the dev default in non-dev services that do not call `assert_safe_for_runtime`

**File:** `libs/fincept-core/src/fincept_core/config.py:65-66, 128-150`; `services/api/src/api/main.py:54`

`Settings` defaults `JWT_SECRET` to `"dev-only-change-me"`. The runtime guard `assert_safe_for_runtime()` that fails closed on this default is only called from **`services/api/src/api/main.py:54`** (and one agent: `services/agents/src/agents/news_impact_agent/main.py:165`). The 2026-06-23 review noted the same issue; the existing `libs/fincept-core/tests/test_startup_safety_matrix.py` asserts the *contract* that every service entrypoint must call this guard, but the orchestrator, ingestor, strategy_host, portfolio, OMS, features, and jobs services do not call it.

If a non-dev OMS or strategy_host starts with `ENV=production` and forgets to set `FINCEPT_JWT_SECRET`, it silently uses `"dev-only-change-me"`. Any token signed with the literal string in source is accepted.

**Evidence:**
```python
64:     # API auth secret (HS256 JWT signing).  The dev default is intentionally
65:     # unsafe so production deploys must set FINCEPT_JWT_SECRET explicitly.
66:     JWT_SECRET: str = Field(default="dev-only-change-me")
...
145:     if s.JWT_SECRET == _DEV_JWT_SECRET or not s.JWT_SECRET.strip():
146:         raise ConfigError(
147:             f"FINCEPT_JWT_SECRET is the dev default (or empty) in "
148:             f"environment '{env}'. Set a strong secret before starting "
149:             f"any non-dev service. See audit R4/P3."
150:         )
```

**Recommendation:** (a) Change the default to `""` so any service that forgets the env var crashes on first read with `ConfigError`; (b) add `min_length=32` so the dev placeholder (21 chars) cannot satisfy the type; (c) wire `assert_safe_for_runtime()` into every `services/*/src/*/main.py` startup path.

---

#### C-S-5. **[MEDIUM]** `FINCEPT_DEBUG_ERRORS` flag bypasses `ENV` check — can leak stack traces in production

**File:** `services/api/src/api/routes/data.py:194-206`

`_debug_errors_enabled()` returns `True` if `FINCEPT_DEBUG_ERRORS` is set, regardless of `ENV`. The route then returns `body["debug"] = str(exc)` to clients. A production deploy with `ENV=production` + `FINCEPT_DEBUG_ERRORS="local"` (one-off debug session, forgotten to unset) leaks stack traces to every dashboard user.

**Evidence:**
```python
194: def _debug_errors_enabled() -> bool:
195:     return os.getenv("FINCEPT_DEBUG_ERRORS", "").lower() in {"1", "true", "yes", "local"}
196: 
197: def _public_error(...):
...
203:     if exc is not None and _debug_errors_enabled():
204:         body["debug"] = str(exc)
```

**Recommendation:** `return get_settings().ENV == "dev" and <existing check>`.

---

#### C-S-6. **[MEDIUM]** OpenBB `.env` walk can be hijacked by a stray `.env` higher in the tree

**File:** `libs/fincept-tools/src/fincept_tools/research/openbb.py:100-121, 144-173`

`_resolve_openbb_url()` walks `Path.cwd()`, all `Path.cwd().parents`, and all `Path(__file__).resolve().parents` looking for `.env` files that override `OPENBB_API_URL`. The SSRF defence (`if scheme == "http" and hostname not in {"127.0.0.1","localhost"}`) is on the *base* URL only — a `https://attacker.example.com` base URL is accepted and the dispatcher will happily issue the GET.

**Evidence:**
```python
100: def _read_openbb_api_url_from_dotenv() -> str | None:
101:     search_roots = [Path.cwd(), *Path.cwd().parents, *Path(__file__).resolve().parents]
...
150: def load() -> dict[str, object]:
...
154:     if parsed_url.scheme == "http" and parsed_url.hostname not in {"127.0.0.1", "localhost"}:
155:         raise ToolBackendError("OpenBB API HTTP URL must be local")
156:     if parsed_url.scheme not in {"http", "https"}:
157:         raise ToolBackendError("OpenBB API URL must use HTTP or HTTPS")
```

**Recommendation:** Pin the resolved OpenBB base URL to local-only (`127.0.0.1`/`localhost`) and refuse any `https://` non-local host. Restrict the `.env` walk to a single explicit root (e.g. `Path(__file__).resolve().parents[2]/.env`).

---

#### C-S-7. **[MEDIUM]** `contextlib.suppress(Exception)` around audit-log writes hides every oms/api order audit trail during DB outage

**File:** `services/oms/src/oms/main.py:86-92, 104-113, 116-122`; `services/api/src/api/routes/orders.py:204-210`; `services/orchestrator/src/orchestrator/router.py:133`

Every `oms.intent`, `oms.state`, `oms.fill`, and `api.order_submitted` audit write is wrapped in `with contextlib.suppress(Exception)`. If Postgres/Timescale is down, **the audit trail silently has no record of any order for the entire outage** — exactly the period where the operator most needs forensic evidence. The OMS handler does not return early on audit failure, so the order is *still submitted to the venue*; the operator ends up with positions whose entire lifecycle is un-auditable.

**Evidence:**
```python
# services/oms/src/oms/main.py:85-92
async def _audit_intent(intent: OrderIntent, *, actor: str) -> None:
    with contextlib.suppress(Exception):
        await audit.append(actor=actor, event_type="oms.intent", ...)

# services/api/src/api/routes/orders.py:204-210
with contextlib.suppress(Exception):
    await audit.append(actor=f"api.orders.post:{actor}", event_type="api.order_submitted", ...)
```

**Recommendation:** (a) For the *intent* row, fail closed and block the order from being published. (b) For the *state* rows, log a WARNING + counter. (c) Expose "orders since last audit_success_at" on `/health/readiness` so silent suppressions become visible.

---

### D. Correctness

#### D-C-1. **[CRITICAL]** `MAX_DAILY_LOSS_USD` still not enforced *(2026-06-03 P0-1, still open)*

**File:** `services/risk/src/risk/checks.py` (entire file), `services/risk/src/risk/snapshot.py` (entire file)

The 2026-06-03 audit's P0-1 finding is still present. `MAX_DAILY_LOSS_USD=2000` is configured in `libs/fincept-core/src/fincept_core/config.py:78` and tested as a fixture parameter in `services/risk/tests/test_checks.py:59`, but `checks.py` and `snapshot.py` reference **zero** daily-loss logic. `RiskContext` does not carry a `daily_realized_pnl` or `daily_unrealized_pnl_estimate`. A dad-as-operator paper-trading deployment with a kill switch but no daily-loss cap means a runaway strategy can rack up paper losses all day until the operator notices.

**Evidence:**
```python
# libs/fincept-core/src/fincept_core/config.py:76-78
MAX_NOTIONAL_USD_PER_SYMBOL: int = Field(default=10000)
MAX_GROSS_NOTIONAL_USD: int = Field(default=50000)
MAX_DAILY_LOSS_USD: int = Field(default=2000)
```
```bash
# Verification: zero references in the risk-gate code path
$ grep -rn "MAX_DAILY_LOSS_USD" services/risk/src/
(no output)
```

**Recommendation:** Implement Patch 1 from the 2026-06-03 audit (lines 286-339): add `daily_realized_pnl` and `daily_unrealized_pnl_estimate` to `RiskContext`, extend `build_context` to accept a `DayPnl` snapshot built from the portfolio store + `get_price`, append a daily-loss check to `check_intent`. ~30 LOC + ~3 tests.

---

#### D-C-2. **[CRITICAL]** OMS Alpaca `submit_intent` only catches `AlpacaError` — `httpx`/network errors silently produce duplicate-fill hazards

**File:** `services/oms/src/oms/alpaca/runtime.py:109-122, 145-157`

`submit_intent` only catches `AlpacaError` (HTTP 4xx/5xx from Alpaca). A `httpx.ConnectTimeout`, `ReadTimeout`, `ConnectError`, `RemoteProtocolError`, or any other `httpx.HTTPError` is not caught — it propagates out of the intent handler. The OMS `main.py` calls `submit_intent` without a `try/except`; the bus consumer logs nothing and the message is never acked (per `Consumer._handle_message` returning `False` on exception). The order's `venue_order_id` is never persisted, the audit shows only `PENDING_NEW`. The strategy re-submits the same intent, and the broker receives a duplicate order once connectivity returns. **In a broker context this is a duplicate-fill hazard.**

**Evidence:**
```python
# runtime.py:107-122
try:
    response = await client.submit_order(intent)
except AlpacaError as exc:
    log.warning("alpaca.submit_rejected", ...)
    rejected = pending_order.model_copy(...)
    states.append(rejected)
    return IntentResult(order_states=states, fill=None)
# (no catch for httpx.HTTPError, OSError, asyncio.TimeoutError)
```

**Recommendation:** Wrap the entire `submit_intent` body in `try/except (httpx.HTTPError, OSError) as exc` and emit `OrderStatus.REJECTED` with `error_code="network_failure"` in `tags`, so the audit row explains the failure instead of leaving the order stuck in `PENDING_NEW`.

---

#### D-C-3. **[HIGH]** OMS Alpaca `_new_order_shell` publishes placeholder `Order` objects with wrong `strategy_id` and `side`

**File:** `services/oms/src/oms/alpaca/runtime.py:253-278`

The background poller doesn't carry the original `OrderIntent`, so `_new_order_shell` synthesises a minimal `Order` to feed callbacks — but it hardcodes `strategy_id="alpaca.unknown"` and `side=Side.BUY` (commented as "placeholder; gets overwritten via _try_terminal_from_response"). If the response is malformed or the field is missing, the `Order` is *published unchanged* with `Side.BUY` for a sell fill and `strategy_id="alpaca.unknown"`. Downstream analytics that join on `strategy_id` will be polluted with this sentinel.

**Evidence:**
```python
261: def _new_order_shell(pending_order: PendingOrder) -> Order:
262:     """Synthesise a minimal Order for state-update callbacks.
...
267:     strategy_id="alpaca.unknown",  # see audit log for true strategy
...
270:     side=Side.BUY,  # placeholder; gets overwritten via _try_terminal_from_response
271:     order_type=OrderType.MARKET,
272:     quantity=Decimal(0),
```

**Recommendation:** Store the original `OrderIntent` on `PendingOrder` (the dataclass already exists at line 75) and read it back in the poller. If reconstruction is impossible, raise rather than publishing a sentinel.

---

#### D-C-4. **[HIGH]** OMS Alpaca background poller's `on_terminal` callback is wrapped in `contextlib.suppress(Exception)`, then `pending.pop` runs unconditionally

**File:** `services/oms/src/oms/alpaca/runtime.py:235-237`

When a terminal-unfilled status (`canceled`/`expired`/`rejected`/`suspended`) is observed, the `on_terminal(terminal_order)` callback is wrapped in `with contextlib.suppress(Exception)`. `on_terminal` is the only path that publishes the rejection back to `ord.orders` and to the audit log. If the publish fails, the order sits in the operator's view forever in the wrong status. `pending.pop(order_id, None)` *still runs* immediately, so the OMS loses track of the order ID entirely.

**Evidence:**
```python
228: elif status in _TERMINAL_UNFILLED:
229:     terminal_order = _new_order_shell(pending_order).model_copy(update={...})
235:     with contextlib.suppress(Exception):
236:         await on_terminal(terminal_order)
237:     pending.pop(order_id, None)  # unconditional pop AFTER a suppressed call
```

**Recommendation:** Only pop the pending entry on success of the callback. On `on_terminal` failure, log at ERROR with the `order_id` and leave the order in `pending` so the next tick retries.

---

#### D-C-5. **[HIGH]** `AlpacaScheduler` and `NewsScheduler` retry forever with no backoff and no circuit breaker

**File:** `services/api/src/api/background.py:72-92, 100-175`

`AlpacaScheduler._loop` calls `sync_positions_and_marks` once per `interval_sec` with `except Exception as exc: log.warning(...)`. There is no backoff, no per-attempt retry, and no circuit-breaker. When the broker credentials are wrong (401) or the network is down for hours, the scheduler will spam Alpaca every 60s for the entire outage, burning rate-limit budget. The 401 will be retried indefinitely even though `is_unrecoverable_provider_error` already classifies it as never-recoverable.

**Evidence:**
```python
76: while True:
77:     try:
78:         summary = await sync_positions_and_marks(...)
79:         log.info("alpaca.sync.ok", **summary)
80:     except asyncio.CancelledError:
81:         raise
82:     except Exception as exc:  # noqa: BLE001
83:         log.warning("alpaca.sync.error", error=str(exc))
84:     try:
85:         await asyncio.sleep(self._interval)
```

**Recommendation:** Track last-error-time + error-class; on `AlpacaError` with status 401/403/insufficient_quota, mark the scheduler "permanently degraded" and stop hitting the wire until the operator clears the error; on transient errors, apply capped exponential backoff (1m → 5m → 30m).

---

#### D-C-6. **[HIGH]** `risk.build_context` has no fail-closed branch when Redis or prices are unavailable

**File:** `services/risk/src/risk/snapshot.py:53-66`; `services/oms/src/oms/main.py:198-209, 293-304`

`build_context` does no error handling around `store.get_all(strategy_id)` or `get_price(symbol)`. If Redis is unreachable, the call raises and propagates out of the intent handler; the bus consumer fails and the message is left un-acked, replayed on next claim, hammered until Redis recovers. There is no fail-closed "if I cannot read positions, I will not approve" branch — and a partial read that returns `{}` for some strategies (e.g. transient `HGETALL` returning empty during a network blip) is treated as zero exposure and the gate allows the trade. The portfolio state is the safety input; "I don't know" must mean "I will not let the trade through".

**Evidence:**
```python
53: for strategy_id in strategies:
54:     positions = await store.get_all(strategy_id)  # may raise OR return {}
55:     for symbol, position in positions.items():
56:         if position.quantity == 0:
57:             continue
58:         price = get_price(symbol)
59:         if price is None:
60:             continue  # SILENTLY SKIP
```

**Recommendation:** Treat `price is None` and any exception from `store.get_all` as "fail closed": return a `RiskContext` whose `gross_notional` is unknown and have `check_intent` reject with `reason="risk_context_unavailable"`. Add a kill-switch-style fail-closed sentinel in `check_intent`.

---

#### D-C-7. **[HIGH]** `Settings.MARK_TTL_SEC` reference crashes at runtime *(2026-06-23 C1, still open)*

**File:** `services/oms/src/oms/alpaca/marks.py:50`; `libs/fincept-core/src/fincept_core/config.py:25-78`

`get_settings().MARK_TTL_SEC or MARK_TTL_SEC` reads `Settings.MARK_TTL_SEC` but **that field does not exist** in `Settings`. Pydantic `BaseSettings` with `extra="ignore"` only ignores *unknown env inputs*; it does **not** synthesize undefined attributes. The first call raises `AttributeError` before the `or` fallback is evaluated. The surrounding `try/except Exception: pass` at lines 58-62 only wraps the *evidence* write, not the TTL line — the `AttributeError` propagates from line 50.

**Evidence:**
```python
# services/oms/src/oms/alpaca/marks.py:50
ttl = get_settings().MARK_TTL_SEC or MARK_TTL_SEC
```
```bash
# Verification: no MARK_TTL_SEC in Settings
$ grep "MARK_TTL_SEC" libs/fincept-core/src/fincept_core/config.py
(no output)
```

**Recommendation:** Add `MARK_TTL_SEC: int = Field(default=300)` to `Settings`, or use `getattr(get_settings(), "MARK_TTL_SEC", None) or MARK_TTL_SEC`.

---

#### D-C-8. **[CRITICAL]** Kill-switch state divergence: API writes `control:kill_switch:state` Redis key, OMS `KillSwitchState` ignores it *(verified against current HEAD)*

**File:** `services/api/src/api/routes/control.py:55, 227-243`; `services/risk/src/risk/state.py:39-73`; `services/oms/src/oms/main.py:227, 327`

This is the **single most important production-safety gap** in the entire backend (independently surfaced by Scout 1's addendum). The API writes the kill-switch state to a Redis key `control:kill_switch:state` (line 55, populated by `_record_kill_switch_state` at line 242). However, the OMS-side `KillSwitchState` (`services/risk/src/risk/state.py`) **never reads this Redis key** — it only listens for `AlertEvent` objects delivered by the in-process bus consumer (line 5's docstring says "The engaged flag is consulted by `check_intent` on every order intent" but `apply()` at line 53 only mutates the in-memory flag from `AlertEvent.code == "kill_switch_engaged"`).

Consequence: an operator clicks "engage kill switch" on the dashboard. `POST /kill-switch` writes `control:kill_switch:state=engaged` to Redis and publishes the alert to `STREAM_ALERTS`. The OMS process *may or may not* see that alert, depending on:
1. Whether the OMS alert consumer is currently connected to Redis (it is in the steady state, but on startup there is a race window).
2. Whether the consumer group has already acknowledged prior alerts (Redis Streams `$` cursor advances past the kill-switch alert after the first ack).

`GET /kill-switch` (control.py:285) reads the Redis key and returns `engaged=True`. The dashboard shows "kill switch ENGAGED". But `KillSwitchState.engaged` in the OMS process is `False` because the bus consumer hasn't observed the alert yet (or never will if it was already past the alert in the stream). `check_intent` allows the trade. **Paper-loss continues during a kill-switch engagement.**

**Evidence:**
```python
# services/api/src/api/routes/control.py:55
_KILL_SWITCH_STATE_KEY = "control:kill_switch:state"
...
242: await redis.set(_KILL_SWITCH_STATE_KEY, json.dumps(payload))
```
```python
# services/risk/src/risk/state.py:39-73 — never reads the Redis key
class KillSwitchState:
    def __init__(self) -> None:
        self._engaged = False
    @property
    def engaged(self) -> bool:
        return self._engaged
    def apply(self, event: AlertEvent) -> None:
        if event.code == CODE_ENGAGED:
            self._engaged = True  # only mutates from in-memory AlertEvent
```
```python
# services/oms/src/oms/main.py:227 — OMS only constructs KillSwitchState (no Redis wiring)
kill = KillSwitchState()
```

**Recommendation:** (a) `KillSwitchState.__init__` should accept a `Redis` client and a `kill_switch_state_key`; `engaged` should check `redis.exists(key)` first and fall back to the in-memory flag. (b) Alternatively, the OMS alert consumer must explicitly read the Redis key on startup and replay the kill-switch state. (c) Both sides must share the same source of truth: the Redis key, *not* an in-memory flag.

---

#### D-C-9. **[HIGH]** Prospective cost defaults to 0, silently bypassing the budget guard *(2026-06-23 H1, still open — verified against current HEAD at line 411)*

**File:** `services/quant_foundry/src/quant_foundry/runpod_client.py:411`

`prospective_cost = getattr(self.client, "cost_per_dispatch_cents", 0)`. If the client does not expose `cost_per_dispatch_cents`, prospective cost is `0`, so `budget_guard.check_and_reserve(0)` always passes. Unbounded GPU spend the moment the HTTP client is wired without `cost_per_dispatch_cents` set. **Note**: 2026-06-23 review cited this as H1; the line has since moved (was 273 in the snapshot the prior review was based on). The structural finding remains.

**Evidence:**
```python
# services/quant_foundry/src/quant_foundry/runpod_client.py:411
411: prospective_cost = getattr(self.client, "cost_per_dispatch_cents", 0)
```

**Recommendation:** `prospective_cost = self.client.cost_per_dispatch_cents` (let `AttributeError` surface), or validate at dispatcher construction. **Never** default a *security-relevant* value to `0` via `getattr`.

---

#### D-C-10. **[HIGH]** Budget reservation is not atomic with job enqueue *(2026-06-23 H2, still open — verified against current HEAD)*

**File:** `services/quant_foundry/src/quant_foundry/gateway.py:159-187`

`budget_guard.check_and_reserve()` appends to the spend ledger **before** the job is enqueued to the outbox. If enqueue fails after reservation, the ledger entry persists with no corresponding job. Under repeated enqueue failures this is a budget-DoS.

**Recommendation:** Enqueue-then-reserve with a compensating `release_reservation()` on enqueue failure, OR implement a single-file transaction across both writes.

---

#### D-C-11. **[MEDIUM]** `shadow_ledger._reload()` crashes on a malformed JSONL line *(2026-06-23 M1, still open — verified against current HEAD)*

**File:** `services/quant_foundry/src/quant_foundry/shadow_ledger.py:181-193`

`_reload()` calls `model_validate_json` per line with no per-line try/except. A single corrupted/partial trailing line (common after a crash mid-write) prevents the process from starting. `outbox.py` and `inbox.py` *do* skip malformed lines defensively (`outbox.py:162`); `shadow_ledger.py` is inconsistent with its siblings.

**Evidence:**
```python
186: with path.open("r", encoding="utf-8") as fh:
187:     for line in fh:
188:         line = line.strip()
189:         if not line:
190:             continue
191:         record = ShadowLedgerRecord.model_validate_json(line)
```

**Recommendation:** Wrap per-line validation in try/except, log the skip, and continue.

---

#### D-C-12. **[MEDIUM]** Bare `except Exception` in `shadow_settlement.store_batch` *(2026-06-23 M2, still open — verified against current HEAD)*

**File:** `services/quant_foundry/src/quant_foundry/shadow_settlement.py:196-208`

Catches `Exception` around Pydantic schema validation. Catches `MemoryError`, third-party surprises. Use `pydantic.ValidationError` specifically.

**Evidence:**
```python
197: try:
198:     ShadowPrediction(**p)
199: except Exception as e:
200:     return SettlementReceipt(rejected=[...reason=CallbackRejectionReason.BAD_SCHEMA...])
```

**Recommendation:** `except pydantic.ValidationError as e:`.

---

#### D-C-13. **[MEDIUM]** `_ALLOWED_TRANSITIONS` in outbox defined but never referenced

**File:** `services/quant_foundry/src/quant_foundry/outbox.py:60-69`

The dict exists and is documented ("kept permissive for MVP") but no transition enforcement references it. Status transitions are permissive in practice. This is the kind of dead code that implies an intended guard that was never wired.

**Evidence:**
```python
60: _ALLOWED_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
61:     JobStatus.QUEUED: frozenset(JobStatus),
62:     JobStatus.DISPATCHING: frozenset(JobStatus),
...
69: }
```

**Recommendation:** Either implement the guard (raise `ValueError` on illegal transitions) or delete the dict and the comment.

---

#### D-C-14. **[MEDIUM]** `_validate_input_path` subprocess `input_path` arg can contain shell-meaningful chars; logged to disk

**File:** `services/api/src/api/training.py:492-495`

The full command line (including the user-controlled `input_path` and `out_dir`) is logged to a file via `f"$ {' '.join(cmd)}\n"`. Even though `subprocess.Popen` uses a list (no `shell=True`), the trainer subprocess is launched with `cwd=None`, so the trainer inherits the api process's CWD. A user can put a `model_name` like `../../foo` — but `model_name` is filtered by `_BAD_NAME_CHARS`. The `input_path` is not filtered.

**Recommendation:** Log a sanitised argv (`shlex.quote` on each element) instead of `repr` of the raw list. Apply a path-length cap and reject `input_path` strings that contain newlines or NULs.

---

#### D-C-15. **[MEDIUM]** `health.py` readiness rollup is mathematically broken when all items are disabled/skipped

**File:** `services/api/src/api/routes/health.py:171-176`

`state_order = {"pass": 5, "warn": 4, "stale": 3, "fail": 1, "skipped": 5, "disabled": 5}`. The "worst" (lowest) wins, so `disabled`/`skipped` are tied with `pass` at 5. If every entry is `disabled` (Quant Foundry, model/dossier — the current state), `worst = min()` over a list of 5s returns 5 → "pass". A readiness endpoint that reports `overall: pass` when nothing is actually checked is misleading.

**Evidence:**
```python
171: state_order = {"pass": 5, "warn": 4, "stale": 3, "fail": 1, "skipped": 5, "disabled": 5}
172: worst = min((state_order.get(c["state"], 2) for c in checks), default=5)
173: overall = next((k for k, v in state_order.items() if v == worst), "warn")
```

**Recommendation:** Filter out `disabled` and `skipped` from the rollup. Map `fail=0, stale=1, warn=2, pass=3` so the math is unambiguous.

---

### E. Error Handling

#### E-E-1. **[CRITICAL]** OMS Alpaca poll loop continues retrying on `AlpacaError` and sleeps the full deadline without backoff

**File:** `services/oms/src/oms/alpaca/runtime.py:145-157`

When the broker returns a 5xx (e.g. 503 from Alpaca maintenance), the instant-poll loop calls `client.get_order` every `poll_interval_s=0.5s` for the entire `instant_poll_s=5s` window — that's ~10 wasted calls per stuck order. The failure is `log.warning`ed and `continue`d but the order is *still* handed to the background poller as if nothing happened.

**Evidence:**
```python
145: deadline = time.monotonic() + instant_poll_s
146: while time.monotonic() < deadline:
147:     await asyncio.sleep(poll_interval_s)
148:     try:
149:         poll = await client.get_order(alpaca_order_id)
150:     except AlpacaError as exc:
151:         log.warning("alpaca.poll_failed", ...)
152:         continue
```

**Recommendation:** Distinguish 4xx (terminal: 404 → already canceled, 422 → bad params). 5xx/429 should back off exponentially and **not** hand the order to the background poller until the broker recovers.

---

#### E-E-2. **[HIGH]** `Consumer._handle_message` returns `False` on every exception, leaving messages un-acked forever with no log

**File:** `libs/fincept-bus/src/fincept_bus/consumer.py:112-131`

The bus consumer's universal error path: handler raises → log nothing → return `False` → caller never `xack`s. The pending entry stays in PEL forever. The `claim_stale` path eventually re-claims it after `claim_idle_ms=60_000`, but the second handler call has no context about WHY the first call failed.

**Evidence:**
```python
120: try:
121:     await handler(event)
122: except Exception:
123:     return False
```

**Recommendation:** At least `log.exception` the handler error (with the event `type`/`payload` snippet). Better: NACK with backoff and after a retry budget mark as poison and move to a DLQ stream.

---

#### E-E-3. **[HIGH]** WebSocket `xread` loop has no per-call error handling — single Redis hiccup kills every dashboard client

**File:** `services/api/src/api/ws.py:124-158`

The `while True: await redis.xread(...)` loop has no per-call error handling. A single `redis.exceptions.ConnectionError` or `TimeoutError` propagates and the client gets a `WebSocket` close with no log line. The `try`/`except Exception` on line 144 swallows `deserialize()` failures with comment "logging would spam" — but a steady stream of malformed events on a production bus is exactly what an operator needs to see at WARNING.

**Evidence:**
```python
124: while True:
125:     try:
126:         messages = await redis.xread(streams, count=...)
...
144:     try:
145:         event = deserialize(fields)
146:     except Exception:  # noqa: S112 - malformed events on the bus must skip silently
147:         continue
```

**Recommendation:** Split the first-frame catch into `TimeoutError` (use defaults) and a more specific set for the JSON error case. For per-message `deserialize`, log at WARNING with a sampled rate. Wrap `xread` in a per-iteration `try/except (redis.RedisError, asyncio.TimeoutError)` with a short backoff.

---

#### E-E-4. **[HIGH]** `engine.session_scope` silently rolls back any commit failure

**File:** `libs/fincept-db/src/fincept_db/engine.py:50-59`

The `session_scope` async context manager commits on success, rolls back on any exception, then `raise`s. The roll-back is silent — the caller cannot distinguish "I rolled back because the handler raised" from "I rolled back because commit failed". If `session.commit()` itself raises (Postgres connection lost mid-commit), the original exception re-raises, masking the commit failure.

**Evidence:**
```python
50: @asynccontextmanager
51: async def session_scope() -> AsyncIterator[AsyncSession]:
52:     sm = get_sessionmaker()
53:     async with sm() as session:
54:         try:
55:             yield session
56:             await session.commit()
57:         except Exception:
58:             await session.rollback()
59:             raise
```

**Recommendation:** Wrap `await session.commit()` in its own `try/except`; on commit failure, log at ERROR with the payload size, then roll back and re-raise the commit error (or both via `ExceptionGroup`/`raise ... from e`).

---

#### E-E-5. **[MEDIUM]** OpenBB `_load_openbb_quote` has no timeout — a hung SDK call blocks the OMS tool dispatch indefinitely

**File:** `libs/fincept-tools/src/fincept_tools/research/openbb.py:124-141`

`_load_openbb_quote` calls `obb.equity.price.quote(...)` via `asyncio.to_thread(load)` with **no timeout**. A hung OpenBB SDK call blocks the OMS's tool dispatch indefinitely.

**Evidence:**
```python
124: async def _load_openbb_quote(symbol: str, provider: str) -> list[dict[str, object]]:
125:     def load() -> list[dict[str, object]]:
...
132:         result = obb.equity.price.quote(symbol=symbol, provider=provider)
```

**Recommendation:** Wrap in a `concurrent.futures` future with `Future.result(timeout=...)` or use `signal.alarm`-style timeout.

---

#### E-E-6. **[MEDIUM]** `_call_anthropic`/`_call_openai` URL-fetch path lumps all `URLError` into one "OpenBB unavailable"

**File:** `libs/fincept-tools/src/fincept_tools/research/openbb.py:163-169`

The `_get_json` URL-fetch path catches `error.URLError` and re-raises as `OpenBBUnavailable` regardless of the underlying reason. The operator cannot tell from the dashboard "OpenBB is down" (connection refused) from "the URL is wrong" (DNS failure) from "the request timed out" (slow OpenBB).

**Recommendation:** Inspect `exc.reason` and surface a different `error_type` (`OpenBBTimeout`, `OpenBBUnreachable`, `OpenBBHostNotFound`).

---

#### E-E-7. **[MEDIUM]** `training.py` exception tuple `(OSError, asyncio.CancelledError, Exception)` makes `Exception` redundant and cancels leak

**File:** `services/api/src/api/training.py:507-512`

The catch tuple `(OSError, asyncio.CancelledError, Exception)` is redundant: `Exception` is a superclass of `OSError`. In Python 3.8+, `asyncio.CancelledError` inherits from `BaseException`, NOT `Exception`. So if the API restarts mid-training, the cancellation propagates and the subprocess is **not cleaned up** (no `run.error` set, no `_persist(failed)`). The trainer subprocess is leaked, holding the trainer output directory and possibly the model file.

**Evidence:**
```python
507: except (OSError, asyncio.CancelledError, Exception) as exc:
508:     run.status = "failed"
509:     ...
```

**Recommendation:** Split into two: `except asyncio.CancelledError` (kill the subprocess, set `run.error = "api cancelled training"`, re-raise) and `except Exception` (log, set `run.error`, continue).

---

#### E-E-8. **[MEDIUM]** `BaseTool._run` validation uses `assert isinstance(...)` — disabled under `python -O`

**File:** `libs/fincept-tools/src/fincept_tools/{exec,data,analytics,research/openbb}/tools.py` (15 sites)

Every `BaseTool._run` method starts with `assert isinstance(payload, XxxInput)`. Python's `-O` flag strips assertions, so an `Agent` that calls `tool(SendOrderInput())` with a payload of a different runtime type would silently proceed into the `_run` body.

**Recommendation:** Replace `assert` with `if not isinstance(payload, XxxInput): raise TypeError(...)`.

---

#### E-E-9. **[MEDIUM]** `portfolio._make_audit_resolver` swallows audit-read errors and returns `None`, orphaning Fills

**File:** `services/portfolio/src/portfolio/main.py:52-66`

When the audit log is unreachable, every Fill that arrives while the DB is down has its `strategy_id` resolved to `None` and is dropped. The fill is lost from the portfolio even though OMS state still has it.

**Recommendation:** Re-raise the audit exception so the consumer handler returns `False` (no xack) and the message is replayed.

---

#### E-E-10. **[MEDIUM]** `oms.alpaca.sync_runner` does sequential Redis writes inside the loop — N+1 round-trips

**File:** `services/oms/src/oms/alpaca/sync_runner.py:94-108`

`sync_positions_and_marks` calls `store.put(position)` then `write_mark(redis, position.symbol, mark_px)` sequentially per position. For 200 positions this is 400 serial Redis round-trips. A single `MOVED` redirect or transient error mid-loop loses the rest. No per-position error handling: any single `write_mark` failure bubbles up.

**Recommendation:** Batch `store.put` and `write_mark` into a single Redis pipeline (one round-trip per scheduler tick, atomic). Add per-iteration `asyncio.wait_for` timeouts.

---

### F. Observability

#### F-O-1. **[MEDIUM]** Many silent failures produce no log line, no counter, no `/health/readiness` indicator

**File:** Repo-wide

`contextlib.suppress(Exception)` blocks at 9 sites (`services/strategy_host/src/strategy_host/main.py:73`, `services/orchestrator/src/orchestrator/router.py:133`, `services/oms/src/oms/main.py:86,104,116`, `libs/fincept-core/src/fincept_core/heartbeat.py:72`, `services/oms/src/oms/alpaca/runtime.py:235`, `services/api/src/api/ws.py:158`, `services/api/src/api/routes/orders.py:204`) produce no log. Operators have no way to know whether these paths are working.

**Recommendation:** Add a single `audit_writes_failed` counter exposed at `/health/readiness`. Increment in each swallow block. `pass/fail` is computed from `audit_writes_succeeded - audit_writes_failed > 0`.

---

#### F-O-2. **[LOW]** `httpx.AsyncClient()` without timeout in `sentiment_agent` and `regime_agent`

**File:** `services/agents/src/agents/sentiment_agent/main.py:289`; `services/agents/src/agents/regime_agent/main.py:113`

Both services use `httpx.AsyncClient()` with no explicit timeout. The default is 5s on all operations, fine for LLM/FRED, but undocumented.

**Recommendation:** Use `fincept_core.http.build_http_client(timeout_s=15.0, connect_timeout_s=5.0)`.

---

#### F-O-3. **[LOW]** `regime_agent` silent skip on FRED error leaves stale snapshot with no `stale_since` indicator

**File:** `services/agents/src/agents/regime_agent/main.py:119-124`

When FRED returns an error, the agent sets `view = None` and skips publishing the snapshot. After `SNAPSHOT_TTL_MULTIPLE * interval_sec = 4 * 3600 = 14400s` of FRED outage the dashboard's regime panel goes blank with no signal.

**Recommendation:** On FRED error, decrement a "stale_ttl" field on the snapshot via a separate Redis key with shorter TTL.

---

### G. Dead Code / Stubs

#### G-D-1. **[RESOLVED]** `HttpRunPodClient.dispatch` was a stub in 2026-06-23 review — now implemented

**File:** `services/quant_foundry/src/quant_foundry/runpod_client.py:232-309`

The 2026-06-23 review (C3) flagged `HttpRunPodClient.dispatch` as a `NotImplementedError` stub. Re-verification against current HEAD on 2026-06-25 shows the method is **fully implemented**: it builds `httpx.Client` with an injected transport, posts to `{base_url}/{endpoint_id}/run` with `Authorization: Bearer` + JSON body, classifies 200 (with `id` field) as `DISPATCHED`, 429/502/503/504 as `TRANSIENT_FAILURE`, all other 4xx/5xx as `TERMINAL_FAILURE`, and catches network errors as `TRANSIENT_FAILURE`. The D-C-8 finding in this audit round 1 was a false positive (inherited without re-verification) and has been **removed** from §D and replaced with the kill-switch divergence finding (now §D-C-8). This entry remains here for traceability only — no action needed.

#### G-D-2. **[MEDIUM]** `PolygonLoader` in `eod_equity.py` is a stub

**File:** `services/ingestor/src/ingestor/eod_equity.py:295-306`

`PolygonLoader.load_for_date_range` raises `NotImplementedError("PolygonLoader is a stub; enable in Phase H if budget approved")`. Comment in `pyproject.toml` confirms it's an intentional placeholder, but no `POLYGON_API_KEY` provider check exists — operators with `POLYGON_API_KEY` set will hit this NotImplementedError on first use.

**Recommendation:** Add a `ProviderDisabled` error class and document the provider-status response in `/data/sources`.

---

#### G-D-3. **[MEDIUM]** `pairs` agent is still a stub

**File:** `services/agents/src/agents/pairs/` (no implementation file)

The 2026-05-07 snapshot already flagged `pairs` as a stub. The build order `TASK-033` is not closed. Coin-based pairs trading cannot be validated.

**Recommendation:** Either implement or formally remove from `__init__.py` until Phase X.

---

#### G-D-4. **[MEDIUM]** False-positive test: `test_all_schemas_have_schema_version_and_forbid_extra` has an empty loop body

**File:** `services/quant_foundry/tests/test_schemas.py:56-74`

The for-loop body is bare `pass` with a comment "verified by other tests". A regression that removes `extra="forbid"` or `schema_version` from any of the 12 schemas goes undetected.

**Evidence:**
```python
56: def test_all_schemas_have_schema_version_and_forbid_extra() -> None:
...
72:     for _cls in schemas:
73:         pass  # verified by the roundtrip tests and the explicit ShadowPrediction rejection test below
```

**Recommendation:** Replace with `for cls in schemas: assert cls.model_config.get("extra") == "forbid"; assert "schema_version" in cls.model_fields`.

---

#### G-D-5. **[LOW]** Dead `pytest.importorskip("fakeredis")` calls in strategy_host tests

**File:** `services/strategy_host/tests/test_runner.py:146`, `test_runner_reload.py:116`, `test_supervisor.py:120`

`fakeredis` is now declared in `services/strategy_host/pyproject.toml`. The skip will never fire. Dead defensive code.

**Recommendation:** Remove the importor-skip lines; keep the direct `import fakeredis.aioredis`.

---

### H. Type Safety

#### H-T-1. **[MEDIUM]** `cast(Any, ...)` for SQLAlchemy 2.x typed results

**File:** `libs/fincept-db/src/fincept_db/{bars.py:60, ticks.py:46,132, features.py:52, provider_data.py:258}`

Every `session.execute(stmt)` is force-cast to `CursorResult[Any]`. The underlying `Row`/`RowMapping` types are abandoned.

**Evidence:**
```python
# libs/fincept-db/src/fincept_db/ticks.py:46
result = cast("CursorResult[Any]", await session.execute(stmt))
```

**Recommendation:** Type the statement (`Select[TradeRow]`) and use `Result[T]` directly.

---

#### H-T-2. **[MEDIUM]** `_ws: Any` in WebSocket adapters

**File:** `services/ingestor/src/ingestor/{binance.py:51, kraken.py:64, coinbase.py:54}`

`self._ws: Any = None  # websockets.WebSocketClientProtocol; Any avoids stub drift.` The comment admits this is a workaround.

**Recommendation:** Pin the websockets version and use the concrete `ClientConnection` type, or use `TYPE_CHECKING` to import the proper type only during static analysis.

---

#### H-T-3. **[MEDIUM]** `dict[str, Any]` for Alpaca order JSON

**File:** `services/oms/src/oms/alpaca/client.py:91-128`; `services/oms/src/oms/alpaca/runtime.py:281-330`

`AlpacaClient.submit_order` returns `dict[str, Any]`, which is then passed to `_try_terminal_from_response` and `_build_fill_from_response` that reach into it with `response.get("status", "")`, `response.get("filled_at")` etc. The Alpaca order JSON has a stable contract; defining an `AlpacaOrderResponse(TypedDict)` would prevent silent breakage.

**Recommendation:** Introduce `AlpacaOrderResponse` (TypedDict) with the keys actually used; mark unused keys as `NotRequired`.

---

#### H-T-4. **[MEDIUM]** `OMS_ROUTER` and `TRADING_MODE` accept any string

**File:** `libs/fincept-core/src/fincept_core/config.py:25, 75`; `services/oms/src/oms/main.py:413-431`

`OMS_ROUTER: str` accepts any string. The OMS checks `if settings.OMS_ROUTER == "alpaca"` / `== "sim"` and raises on unknown. Could use `Literal["sim", "alpaca"]` or a StrEnum to fail at startup, before any side effect.

**Recommendation:** Define `class OmsRouter(StrEnum)` and `class TradingMode(StrEnum)` in `fincept_core.config`; type fields accordingly.

---

#### H-T-5. **[MEDIUM]** `Event.type: str` instead of StrEnum

**File:** `libs/fincept-core/src/fincept_core/events.py:48-71`

`Event.type: str` and the lookup table `_EVENT_SCHEMAS: dict[str, type[EventPayload]]` use raw `str`. The mapping could be `dict[EventType, ...]` if `EventType(StrEnum)` were introduced.

**Recommendation:** Add `class EventType(StrEnum)` in `fincept_core.schemas` and type `Event.type: EventType`.

---

### I. Configuration

#### I-CF-1. **[HIGH]** Risk caps accept negatives (silent gate disable)

**File:** `libs/fincept-core/src/fincept_core/config.py:76-78`; `services/risk/src/risk/checks.py:100,110`

`MAX_NOTIONAL_USD_PER_SYMBOL=10000`, `MAX_GROSS_NOTIONAL_USD=50000`, `MAX_DAILY_LOSS_USD=2000`. None are positive-validated. A negative env var (`MAX_NOTIONAL_USD_PER_SYMBOL=-1`) makes `existing + intent > -1` always true — the gate is a no-op. **Fail-open risk bug.**

**Evidence:**
```python
76: MAX_NOTIONAL_USD_PER_SYMBOL: int = Field(default=10000)
77: MAX_GROSS_NOTIONAL_USD: int = Field(default=50000)
78: MAX_DAILY_LOSS_USD: int = Field(default=2000)
```

**Recommendation:** Add `field_validator("MAX_NOTIONAL_USD_PER_SYMBOL", "MAX_GROSS_NOTIONAL_USD", "MAX_DAILY_LOSS_USD")` requiring `> 0`.

---

#### I-CF-2. **[HIGH]** Magic timeouts / intervals in OMS Alpaca runtime (not in Settings)

**File:** `services/oms/src/oms/alpaca/runtime.py:49-51`

`DEFAULT_INSTANT_POLL_S=5.0`, `DEFAULT_INSTANT_INTERVAL_S=0.5`, `DEFAULT_BACKGROUND_INTERVAL_S=5.0` are module-level constants. Operator cannot tune Alpaca poll cadence without a code change.

**Recommendation:** Add `ALPACA_INSTANT_POLL_S`, `ALPACA_INSTANT_INTERVAL_S`, `ALPACA_BACKGROUND_INTERVAL_S` to `Settings`.

---

#### I-CF-3. **[HIGH]** Magic constants in ingestor writer / adapters

**File:** `services/ingestor/src/ingestor/writer.py:49,51`, `binance.py:39-41`, `kraken.py:51-54`, `coinbase.py:42-43`, `main.py:47-48`

`DEFAULT_BATCH_SIZE=500`, `NS_PER_MINUTE=60_000_000_000`, `PING_INTERVAL_S=15/10`, `PING_TIMEOUT_S=10`, `MAX_FRAME_BYTES=2**22 / 8*1024*1024`, `DEFAULT_BOOK_DEPTH=100`, `INITIAL_BACKOFF_S=1.0`, `MAX_BACKOFF_S=60.0`. None env-driven.

**Recommendation:** Add to `Settings` (`INGESTOR_BATCH_SIZE`, `INGESTOR_PING_INTERVAL_S`, etc.).

---

#### I-CF-4. **[HIGH]** Single `Settings` class but 30+ raw `os.environ.get` calls bypass it

**File:** `libs/fincept-core/src/fincept_core/config.py:12-119` (the only `BaseSettings` consumer); 30+ sites across services.

Inconsistent config strategy. `Settings` provides type validation, env precedence (shell > .env), and centralization; the dozens of `os.environ.get` lookups scattered across services bypass all of this. The same env var (`MODELS_DIR`) is read in **5 different files** (`services/api/src/api/promotions.py:73`, `services/api/src/api/training.py:89`, `services/api/src/api/routes/models.py:68`, `services/agents/src/agents/news_alpha_predictor/main.py:32`, `services/strategy_host/src/strategy_host/model_resolver.py:61`), each with `os.environ.get("MODELS_DIR", "models")` and `Settings` has no `MODELS_DIR` field — so `FINCEPT_MODELS_DIR` does nothing in these files.

**Recommendation:** Either (a) move every config knob to `Settings` and delete the raw `os.environ.get` calls, or (b) document the convention and enforce via a linter rule.

---

#### I-CF-5. **[HIGH]** Mixed `FINCEPT_*` and bare env var conventions

**File:** `libs/fincept-core/src/fincept_core/config.py:18-19` (prefix `"FINCEPT_"`) vs unprefixed `os.environ.get` lookups

`pydantic-settings` reads with `env_prefix="FINCEPT_"`, so `Settings.ALPACA_API_KEY` looks for `FINCEPT_ALPACA_API_KEY`. But raw `os.environ.get("ALPACA_API_KEY")` and `os.environ.get("NEWS_ALPHA_MODEL_DIR")` are *unprefixed*. Two parallel naming conventions for the same conceptual config. `scripts/ingest_bars.py:213,222` and `scripts/run_intraday_walkforward.py:282` use the bare `ALPACA_API_KEY`. An operator who follows the README and sets `FINCEPT_ALPACA_API_KEY` will find that nothing changes.

**Recommendation:** Pick one. Either drop the `FINCEPT_` prefix in `Settings` (rename env vars to bare names), or prefix all ad-hoc `os.environ.get` lookups.

---

#### I-CF-6. **[HIGH]** `assert_safe_for_runtime` only called from 2 production entrypoints

**File:** `services/api/src/api/main.py:54`; `services/agents/src/agents/news_impact_agent/main.py:165`

The runtime guard that fails closed on dev JWT in non-dev is only invoked by 2 production entrypoints. The orchestrator, ingestor, strategy_host, portfolio, OMS, features, and jobs services do not call it. The test `libs/fincept-core/tests/test_startup_safety_matrix.py` enforces the contract, but it tests *implementation calls* in only those 2 places.

**Recommendation:** Add `assert_safe_for_runtime()` to every `services/*/src/*/main.py` startup path.

---

#### I-CF-7. **[MEDIUM]** Empty HMAC secret allowed by default *(2026-06-23 M4, still open)*

**File:** `services/quant_foundry/src/quant_foundry/gateway.py:103`

`callback_secret` defaults to `""`. HMAC with an empty key is computable and verifiable; an operator who sets a low-entropy string without realising the callback endpoint is then wide-open.

**Recommendation:** Default to `None`; raise `ConfigError` at boot if the gateway is `enabled=True` and the secret is missing or shorter than 32 bytes.

---

#### I-CF-8. **[MEDIUM]** `QUANT_FOUNDRY_MONTHLY_BUDGET_CENTS` silently defaults to 0 on `ValueError`

**File:** `services/quant_foundry/src/quant_foundry/budget.py:309-313`

`int(budget_str)` raises `ValueError` on a non-numeric env var; caught and silently default to 0 (which blocks all paid jobs). Operator misconfiguration = silent block.

**Evidence:**
```python
309: budget_str = os.environ.get("QUANT_FOUNDRY_MONTHLY_BUDGET_CENTS", "0")
310: try:
311:     monthly_budget = int(budget_str)
312: except ValueError:
313:     monthly_budget = 0
```

**Recommendation:** Fail loudly on misconfigured env; surface in `gateway.from_env()`.

---

#### I-CF-9. **[MEDIUM]** CORS hardcoded; production override is a comment

**File:** `services/api/src/api/main.py:89-100`

Origins are `http://localhost:3000`, `127.0.0.1:3000`, `:5173`, etc. The docstring says "production should override via env", but nothing reads an env var and nothing fails if production runs with these defaults. Combined with `allow_methods=["*"]` + `allow_credentials=True`, this widens the CSRF surface if the API is exposed publicly.

**Recommendation:** Read `FINCEPT_CORS_ALLOW_ORIGINS` from env; fail at startup in non-dev `ENV` if unset.

---

#### I-CF-10. **[LOW]** `MODELS_DIR` and `ACTIVE_MODELS_DIR` inconsistent defaulting

**File:** `services/api/src/api/promotions.py:73-83, 520`

Five files read `os.environ.get("MODELS_DIR", "models")` — and `Settings` does **not** have a `MODELS_DIR` field. `_default_active_dir()` checks `ACTIVE_MODELS_DIR` override and falls back to `MODELS_DIR / "active"` silently.

**Recommendation:** Add to `Settings` with explicit `field_validator` requiring absolute paths in non-dev envs.

---

### J. Performance

#### J-P-1. **[MEDIUM]** `oms.alpaca.sync_runner` does 2N Redis writes per scheduler tick (see E-E-10)

Same finding as E-E-10 — counted here for the Performance category total.

---

#### J-P-2. **[MEDIUM]** `quant_foundry.settlement._find()` is O(n) over all predictions

**File:** `services/quant_foundry/src/quant_foundry/settlement.py:283-289`

Locating a prediction scans every model file. No index. Settlement latency grows linearly with total prediction count.

**Recommendation:** Maintain a `prediction_id → (file, offset)` index, or move settlement lookups to a database.

---

#### J-P-3. **[LOW]** No connection pooling on outbound httpx

**File:** `services/agents/src/agents/{sentiment_agent,regime_agent}/main.py`; `services/oms/src/oms/alpaca/*.py`

Each agent opens a fresh `httpx.AsyncClient` per loop iteration (or per long-lived `async with`). For the news sync, the `httpx.Client` is shared but connection pool defaults are not tuned.

**Recommendation:** Use `httpx.Limits(max_keepalive_connections=10, max_connections=20)` consistently.

---

#### J-P-4. **[LOW]** `risk.build_context` does serial Redis HGETALL per strategy per intent

**File:** `services/risk/src/risk/snapshot.py:53-54`

`for strategy_id in strategies: positions = await store.get_all(strategy_id)` is serial. For 50 strategies this is 50 round-trips. A pipelined fetch would be 1 round-trip.

**Recommendation:** Use `redis.pipeline()` to batch all `HGETALL` calls.

---

### K. Test Coverage

#### K-T-1. **[HIGH]** False-positive test `test_all_schemas_have_schema_version_and_forbid_extra` (see G-D-4)

Same as G-D-4 — counted here for the Test Coverage category total.

#### K-T-2. **[HIGH]** Zero test coverage for `oms.alpaca.sync_runner`

**File:** `services/oms/src/oms/alpaca/sync_runner.py`

Documented as "shared by `scripts/sync_alpaca.py` and `api.background.AlpacaScheduler`". A bug breaks live→paper position sync silently. No tests.

**Recommendation:** Add `services/oms/tests/test_alpaca_sync_runner.py` with fakeredis + respx fixture.

#### K-T-3. **[HIGH]** Zero test coverage for `oms.alpaca.marks`

**File:** `services/oms/src/oms/alpaca/marks.py`

Redis-backed mark store; every service reads `md:last:{symbol}`. TTL logic and decimal serialization are untested.

**Recommendation:** Add `services/oms/tests/test_alpaca_marks.py` asserting: (a) `write_mark` produces HASH with `px`/`ts_ns` strings, (b) `read_mark` returns the same Decimal, (c) TTL is set, (d) missing key returns None.

#### K-T-4. **[HIGH]** Zero test coverage for 12 quant_foundry modules

**Files:** `services/quant_foundry/src/quant_foundry/{callbacks, dataset_manifest, feature_availability, feature_snapshot_export, gateway, leaderboard, metrics, mock_dispatcher, outcomes, pbo, registry, significance}.py`

`gateway.py` is the public Quant Foundry API surface. `pbo.py` and `significance.py` are statistical guards; their regression would silently degrade model selection.

**Recommendation:** Prioritize in order: `gateway.py` (most-used), `pbo.py` + `significance.py` (math), `registry.py` + `leaderboard.py` (data integrity).

#### K-T-5. **[HIGH]** `fincept-core/heartbeat.py` untested

**File:** `libs/fincept-core/src/fincept_core/heartbeat.py`

Heartbeat is consumed by leadership/leader election. A bug breaks the single-leader invariant for `strategy_host`.

**Recommendation:** Add `libs/fincept-core/tests/test_heartbeat.py` with fakeredis.

#### K-T-6. **[MEDIUM]** 121 `pytest.raises` without `match=` (full list available on request)

**Files:** Across `quant_foundry`, `fincept-tools`, `fincept-core`, `fincept-bus`, etc.

If a refactor causes the wrong error class to be raised, tests still pass. Quant_foundry's tuple-types `(TypeError, ValueError)` are especially loose.

**Recommendation:** Sweep all 121 sites; add `match=` strings.

#### K-T-7. **[MEDIUM]** No shared conftest for 15 of 17 packages

Identical fakeredis + intent factory code is repeated 30+ times. Drift risk.

**Recommendation:** Add a workspace `tests/conftest.py` with shared `fake_redis`, `auth_headers`, `make_intent`, `make_bar`, `make_position` factories.

#### K-T-8. **[LOW]** DB conftest silently skips the entire `libs/fincept-db` suite without Postgres

**File:** `libs/fincept-db/tests/conftest.py:33-46`

On a developer machine without a Timescale container, **all 5 DB tests are skipped** rather than failing. The 2026-04-27 README explicitly noted "11 skipped" because of this.

**Recommendation:** Add a fast SQLite-backed shim for offline mode, or mark these tests with `@pytest.mark.integration` and run only in CI.

---

## D. Open ADRs to Promote to "accepted"

From `docs/DECISIONS.md`:

| ADR | Title | Current Status | Audit recommendation |
|---|---|---|---|
| ADR-0007 | orchestration: Kubernetes vs docker-compose + systemd | open | KEEP OPEN — depends on infrastructure choice. |
| ADR-0008 | cloud provider: AWS vs GCP vs on-prem colo | open | KEEP OPEN — depends on operator decision. |
| ADR-0010 | Alpaca paper/live brokerage boundary and approval process | open | **PROMOTE TO `proposed`:** The current code embodies an implicit decision (live = `OMS_ROUTER=alpaca` + `ALPACA_BASE_URL=https://api.alpaca.markets`); formalise the boundary. The kill-switch divergence (D-C-8) is a related safety boundary that should be formalised as part of this ADR. |
| **NEW ADR-0011** | kill-switch state ownership: Redis key vs in-memory flag | not written | **PROPOSE:** The kill-switch state must have a single source of truth. The current implementation splits this across a Redis key (`control:kill_switch:state`, written by `services/api/src/api/routes/control.py:55, 227-243`) and an in-memory flag (`KillSwitchState._engaged` in `services/risk/src/risk/state.py:47`, mutated by bus consumer). The two do not agree. This ADR should specify: (a) Redis is the source of truth; (b) `KillSwitchState.__init__` accepts a Redis client and reads the key on every `engaged` access; (c) the API and OMS must share the same key prefix and serialization format. |

Additionally:
- **ADR-0006 (feature store)** is already marked `accepted` in DECISIONS.md but the implementation in `services/features/src/features/store.py` uses Redis online + Parquet offline, consistent with the ADR text. No action needed.
- **ADR-0009 (datasource routing)** is already marked `accepted`. The implementation in `services/api/src/api/routes/data.py` has safety tier + health mode + coverage tracking as described. No action needed.

---

## E. Top-10 Prioritized Fixes

Ordered by safety-impact / effort ratio. "Blockers" are paper-deployment showstoppers; "Quick wins" are <1 day each; "Phase 2" needs 1-2 weeks.

### Blockers (do before any live-trading path is even considered)

| # | Severity | Finding | Action | Effort |
|---|---|---|---|---|
| 1 | **CRITICAL** | **D-C-8** Kill-switch state divergence (API writes Redis key, OMS ignores it) | Wire `KillSwitchState` to read `control:kill_switch:state` on startup + every check; make Redis the single source of truth | 1 day |
| 2 | **CRITICAL** | **D-C-1** `MAX_DAILY_LOSS_USD` not enforced | Add daily-realized-PnL snapshot to `RiskContext`; extend `check_intent` | 1 day |
| 3 | **CRITICAL** | **D-C-2** OMS Alpaca `submit_intent` only catches `AlpacaError` | Wrap in `try/except (httpx.HTTPError, OSError)` | 1 day |
| 4 | **CRITICAL** | **E-E-1** OMS Alpaca poll loop no backoff, no 4xx/5xx distinction | Distinguish transient/terminal; exponential backoff | 1 day |

### Quick wins (1 day each)

| # | Severity | Finding | Action | Effort |
|---|---|---|---|---|
| 4 | **HIGH** | **D-C-7** `MARK_TTL_SEC` not in Settings | Add field to Settings (one line) | 30 min |
| 5 | **HIGH** | **D-C-3** `_new_order_shell` placeholder values | Store original intent on `PendingOrder` | 2 hours |
| 6 | **HIGH** | **D-C-4** `on_terminal` swallow + unconditional `pending.pop` | Retry on failure | 1 hour |
| 7 | **HIGH** | **D-C-6** `risk.build_context` no fail-closed | Add `risk_context_unavailable` rejection | 2 hours |
| 8 | **MEDIUM** | **E-E-8** `assert isinstance(...)` in BaseTool | Replace with `if not isinstance: raise TypeError` | 30 min × 15 sites |
| 9 | **MEDIUM** | **G-D-4** False-positive `test_all_schemas_have_*` | Two real assertions in loop | 15 min |
| 10 | **MEDIUM** | **I-CF-1** Risk caps accept negatives | `field_validator(> 0)` × 3 | 30 min |

### Phase 2 (1-2 weeks)

- **D-C-9 + D-C-10** — Quant Foundry `getattr` cost default (H1 from 2026-06-23) and budget atomicity (H2).
- **C-S-1 + C-S-2** — Path traversal in `backtest.py` and `training.py` (the 2026-05-16 findings still open).
- **C-S-3** — Drop WS `?token=` fallback.
- **C-S-7 + E-E-9** — Audit-log swallow blocks (orchestrator + OMS + portfolio).
- **I-CF-4 + I-CF-5** — Consolidate `FINCEPT_*` vs bare env vars; move magic numbers to Settings.
- **NEW: ADR-0011 (kill-switch state ownership)** — formalise the Redis-key-as-source-of-truth contract.

### Phase 3 (2-4 weeks)

- **K-T-2 + K-T-3 + K-T-4 + K-T-5** — Add test files for the 12 untested quant_foundry modules + 2 OMS modules + 3 fincept-core modules.
- **I-CF-6** — Wire `assert_safe_for_runtime()` into every service entrypoint.
- **F-O-1** — Expose `audit_writes_failed` counter at `/health/readiness`.

---

## F. Things explicitly NOT in scope

Per the task acceptance criteria, this audit covers the **Python backend** (`libs/*`, `services/*`, `scripts/*`). The following are out of scope and are covered by the parallel **Builder 2 frontend & infra audit** at `docs/audits/2026-06-25/frontend-infra-audit.md`:

- `apps/dashboard/**` (Next.js UI, tsconfig, etc.)
- `scripts/*.ps1` (PowerShell wrappers)
- `.github/workflows/**`
- Dockerfiles
- `MIGRATIONS_CONFIG_REVIEW.md`

The bridge between this audit and the Builder 2 audit is in two places:
- **CORS / `FINCEPT_DEBUG_ERRORS`** (I-CF-9, C-S-5) — backend settings that affect the dashboard.
- **`MODELS_DIR` / `ACTIVE_MODELS_DIR`** (I-CF-10) — read by both API (Python) and possibly the dashboard (via `/models/*` API).

---

## G. Files Reviewed (full read)

```
libs/fincept-core/src/fincept_core/{__init__,clock,config,errors,events,heartbeat,http,ids,leadership,logging,portfolio,prediction_log,schemas,strategy_config,tracing}.py
libs/fincept-bus/src/fincept_bus/{__init__,consumer,producer,streams,types}.py
libs/fincept-db/src/fincept_db/{__init__,audit,bars,engine,evidence_redaction,features,models,provider_data,provider_receipts,ticks,universe}.py
libs/fincept-sdk/src/fincept_sdk/{__init__,strategy}.py
libs/fincept-tools/src/fincept_tools/{__init__,errors,protocol,registry}.py
libs/fincept-tools/src/fincept_tools/{analytics,data,exec,research}/{__init__,tools,openbb,exa}.py
services/api/src/api/{__init__,auth,background,deps,feature_importance,main,openbb_health_store,promotions,rate_limit,symbol_search,training,ws}.py
services/api/src/api/routes/{__init__,backtest,control,data,health,models,modules,news,news_impact,orders,positions,quant_foundry,regime,research,services,strategies}.py
services/oms/src/oms/{__init__,main,paper,prices,processor,state}.py
services/oms/src/oms/alpaca/{__init__,client,data,marks,news_sync,runtime,symbols,sync_runner}.py
services/risk/src/risk/{__init__,checks,snapshot,state}.py
services/orchestrator/src/orchestrator/{__init__,allocator,consensus,decisions,main,router}.py
services/portfolio/src/portfolio/{__init__,main,state,store}.py
services/strategy_host/src/strategy_host/{__init__,main,model_resolver,runtime,runner,supervisor}.py
services/backtester/src/backtester/{__init__,blotter,broker,costs,datasource,engine,gbm_features,ingest,report,runner,strategies,walk_forward}.py
services/features/src/features/{__init__,computer,main,offline,online,pit,store}.py
services/features/src/features/transforms/{__init__,cross,price,volatility}.py
services/ingestor/src/ingestor/{__init__,base,binance,coinbase,eod_equity,kraken,main,normalizer,quality,quality_main,writer}.py
services/agents/src/agents/{__init__,base}.py
services/agents/src/agents/{gbm_predictor,information_enricher,news_alpha_predictor,news_impact_agent,news_outcome_labeler,regime_agent,sentiment_agent,sentiment_features}/{__init__,main,...}.py
services/quant_foundry/src/quant_foundry/{__init__,artifacts,baseline_family,budget,callbacks,causal_graph,conformal_gate,dataset_manifest,dossier,drift_sentinel,feature_availability,feature_lake,feature_snapshot_export,gateway,ids,inbox,leaderboard,leaderboard_expanded,metrics,mock_dispatcher,moe_router,outbox,outcomes,paper_bridge,pbo,promotion,registry,retirement,runpod_client,runpod_training,schemas,sentinel,settlement,shadow_inference,shadow_ledger,shadow_settlement,signatures,significance,tournament}.py
services/jobs/src/jobs/{__init__,daily_eod_load,main,news_alpha_candidate_train}.py
scripts/{ingest_bars,sync_alpaca,sync_alpaca_fills,inject_test_prediction,paper_spine_replay,openbb_live_proof,run_intraday_walkforward,walk_forward,capture_to_parquet,build_synth_ohlcv,build_synth_parquet,run_backtest,test_sentiment_pipeline,wait_heartbeat,route_smoke}.py
```

Surfaced via grep / glob (not opened in full):
- All `tests/` directories (covered by the parallel test-coverage subagent).
- `.worktrees/`, `experiments/news-impact-model/`, `strategies/` JSON files.
- `spec/`, `docs/` markdown (read only for cross-reference).

---

*Generated 2026-06-25 from a static review by Builder 1 (orchestrated swarm audit pass). No source files were modified.*