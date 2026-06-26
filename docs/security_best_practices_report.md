# Security Review — Recent Changes on `codex/portfolio-optimizer-core`

**Scope:** Security-focused review of recent commits on branch
`codex/portfolio-optimizer-core` (vs `main`). No code was modified.
Focus areas: authentication, input validation, and secret handling.

**Reviewed surfaces (sampled):**
- `services/api/src/api/auth.py` — JWT bearer auth
- `services/api/src/api/routes/quant_foundry.py` — Quant Foundry HTTP endpoints
- `services/quant_foundry/src/quant_foundry/signatures.py` — HMAC callback signing/verification
- `services/quant_foundry/src/quant_foundry/gateway.py` — gateway facade + budget guard wiring
- `services/quant_foundry/src/quant_foundry/budget.py` — budget guard env config
- `services/quant_foundry/src/quant_foundry/runpod_client.py` — RunPod dispatch client
- `services/quant_foundry/src/quant_foundry/artifacts.py` — artifact URI import
- `services/quant_foundry/src/quant_foundry/causal_graph.py` — causal graph models
- `services/quant_foundry/src/quant_foundry/mock_dispatcher.py` — mock dispatch + payload storage
- `services/quant_foundry/src/quant_foundry/outbox.py` / `registry.py` / `inbox.py` — durable stores
- `apps/dashboard/src/app/api/portfolio-report/route.ts` — LLM report route
- `apps/dashboard/src/features/portfolio-builder/marketDataService.ts` — demo market data
- `.env.example`, `.gitignore`, `strategies/alpaca.live.json`

---

## Executive Summary

The recent Quant Foundry work is, on the whole, defensively designed. The
HMAC callback path is fail-closed with constant-time comparison, timestamp
skew, and job-id binding; operator endpoints require bearer JWT; artifact
URIs reject path traversal; durable stores key records by id (not by
user-supplied file paths); and no secrets are committed (`.env.example`
uses placeholders, `.gitignore` covers `.env`, strategy JSON contains no
credentials). No critical remotely-exploitable vulnerability was found.

The findings below are mostly **medium/low** hardening gaps. The most
noteworthy are: (1) JWTs are not required to carry an `exp` claim and the
helper does not stamp one, so tokens can be valid indefinitely; (2) the
portfolio-report route reads `.env` files from disk at request time; and
(3) `CreateJobRequest.request_payload` is typed `Any` with no size cap.

---

## Findings

### CRITICAL
None.

### HIGH

#### H1 — JWT tokens have no enforced expiry
**Impact:** A leaked/intercepted bearer token remains valid forever,
giving an attacker persistent operator access to all Quant Foundry
endpoints (job creation, dossiers, promotion queue, shadow health).

`require_user` decodes with `jwt.decode(token, ..., algorithms=["HS256"])`
and no `options={"require": ["exp"]}`, so a token without an `exp` claim
is accepted. The helper `encode_token` does not stamp `exp` either, so any
caller using it produces non-expiring tokens.

- `services/api/src/api/auth.py:24-26` — `encode_token` adds no `exp`.
- `services/api/src/api/auth.py:55-56` — `jwt.decode` does not require `exp`.
- `services/api/src/api/ws.py:71` — same pattern in the WebSocket path.

**Recommendation:** Stamp `exp` (and `iat`) in `encode_token`, and decode
with `options={"require": ["exp", "iat"], "verify_exp": True}`. Consider
also binding `iss`/`aud` and verifying them. Keep HS256 only until a
key-rotation story exists; for multi-operator scope, move to RS256/EdDSA.

### MEDIUM

#### M1 — Portfolio-report route reads `.env` files from disk at request time
**Impact:** Secrets are read from the filesystem on every request via a
hand-rolled parser instead of being loaded once at startup into the
process environment. This broadens the secret-read surface (any process
working dir change or planted `.env` file is honored), bypasses the
pydantic-settings validation that the rest of the stack relies on, and
makes secret rotation/audit harder to reason about.

- `apps/dashboard/src/app/api/portfolio-report/route.ts:283-317` —
  `getEnvSecret` + `envFiles()` + `readEnvFileValue` scan
  `.env.local`, `.env`, and `../../.env` relative to `process.cwd()`.

**Recommendation:** Read provider keys exclusively from `process.env`
(populated once at boot by the deployment secret manager). If local `.env`
support is required for dev, load it once at module init via the same
mechanism the Python services use, not per-request with a custom regex
parser. At minimum, restrict the file search to a single known path and
fail closed in non-dev environments.

#### M2 — `CreateJobRequest.request_payload` is unbounded `Any`
**Impact:** An authenticated operator can POST an arbitrarily large JSON
body as `request_payload`. It is serialized and hashed (`outbox.py`), and
in `local_mock` mode the mock dispatcher writes a derived envelope to
disk (`mock_dispatcher.py:154-155`). With no body-size cap on the route
and no schema constraint on `request_payload`, this is a storage/DoS
vector and lets unstructured data flow into durable ledgers.

- `services/api/src/api/routes/quant_foundry.py:43-53` —
  `request_payload: Any` with `extra="forbid"` on the envelope only.
- `services/quant_foundry/src/quant_foundry/outbox.py:166-209` —
  `_serialize_payload` accepts any JSON.
- `services/quant_foundry/src/quant_foundry/mock_dispatcher.py:151-155` —
  envelope written to `payloads/<job_id>.json`.

**Recommendation:** Constrain `request_payload` to a typed union of the
real request schemas (`RunPodTrainingRequest` / `RunPodInferenceRequest`)
or at least enforce a max byte size on the route (FastAPI/Starlette body
limit). Reject unknown `job_type` values at the route before enqueue.

#### M3 — HMAC callback secret defaults to empty string
**Impact:** If an operator enables Quant Foundry
(`QUANT_FOUNDRY_ENABLED=true`) and the RunPod/callback path without
setting `QUANT_FOUNDRY_CALLBACK_SECRET`, every inbound callback fails
signature verification (fail-closed — good), but there is no startup
guard that *refuses to enable* the surface with an empty secret. This
mirrors the JWT dev-default footgun that `fincept_core.config` already
guards against; the callback secret has no equivalent guard.

- `services/quant_foundry/src/quant_foundry/gateway.py:103` —
  `callback_secret = os.environ.get("QUANT_FOUNDRY_CALLBACK_SECRET", "")`.
- `services/quant_foundry/src/quant_foundry/signatures.py:59-60` —
  empty secret → `verify_callback` returns `False` (fail-closed).

**Recommendation:** Add a startup assertion (analogous to the
`FINCEPT_JWT_SECRET` dev-default check in
`libs/fincept-core/src/fincept_core/config.py:142-150`) that refuses to
construct an enabled gateway in non-dev environments when
`QUANT_FOUNDRY_CALLBACK_SECRET` is empty or below a minimum entropy
threshold.

### LOW

#### L1 — Error details echo user-controlled identifiers
**Impact:** Minor information reflection. HTTP error `detail` strings
interpolate `job_id`, `model_id`, and `status_filter` verbatim. These are
operator-supplied and already known to the caller, so disclosure is low,
but it is a habit worth avoiding (and can leak internal path/format
expectations to a fuzzier).

- `services/api/src/api/routes/quant_foundry.py:126,142,161,177,291,301`
  (e.g. `f"invalid status filter: {status_filter}"`,
  `f"unknown job_id: {job_id}"`).

**Recommendation:** Return generic messages with a correlated request ID;
log the raw value server-side.

#### L2 — `safe_name` filename sanitization does not reject `..`
**Impact:** The mock dispatcher builds a payload filename from `job_id`
by replacing `:`, `/`, `\\` with `_` but does not reject `..` segments.
Because path separators are stripped, this does **not** yield a real
traversal today (a `job_id` of `..` becomes the literal file `...json`
inside `payloads/`), but the defense is incidental rather than explicit
and could regress if the sanitization changes.

- `services/quant_foundry/src/quant_foundry/mock_dispatcher.py:152-155`.

**Recommendation:** Validate `job_id` against a strict allowlist regex
(e.g. `^[A-Za-z0-9._-]{1,128}$`) at the route/enqueue boundary and reject
`..`; do not rely on character substitution for path safety.

#### L3 — Budget guard silently coerces malformed env to zero budget
**Impact:** If `QUANT_FOUNDRY_MONTHLY_BUDGET_CENTS` is set to a
non-integer, `from_env` falls back to `0`, which means *no paid jobs are
allowed* (fail-closed on spend — safe direction), but the misconfig is
silent. An operator who typos the budget could believe paid jobs are
enabled when they are in fact blocked, or vice versa if the default
semantics ever change.

- `services/quant_foundry/src/quant_foundry/budget.py:309-313`.

**Recommendation:** Log a warning (or raise in non-dev envs) when the env
value is present but unparseable, mirroring the JWT secret guard
philosophy.

#### L4 — `HttpRunPodClient.dispatch` is a `NotImplementedError` stub
**Impact:** Not a vulnerability, but a readiness note: the production
RunPod HTTP path is unimplemented, so the only live dispatch mode is
`local_mock`. When this is filled in, ensure the API key is never logged,
the endpoint URL is allowlisted (no user-controlled base URL), and TLS
verification is enforced. The current scaffolding already keeps the key
private (`runpod_client.py:190-191`) and excludes it from
`model_dump()` — good baselines to preserve.

- `services/quant_foundry/src/quant_foundry/runpod_client.py:178-205`.

---

## Things done well (worth preserving)

- **HMAC callback auth** (`signatures.py`): constant-time compare via
  `hmac.compare_digest`, 5-minute skew window, job-id binding, payload
  hashing, fail-closed on every malformed input. The route verifies the
  signature *before* creating any durable inbox record
  (`gateway.py:384-400`).
- **Bearer auth on operator endpoints**: every read/write Quant Foundry
  route depends on `require_user`; the callback route deliberately does
  *not* use bearer and documents why (`routes/quant_foundry.py:246-303`).
- **Disabled-by-default**: `QUANT_FOUNDRY_ENABLED=false` default; disabled
  state returns safe empty responses with no job creation
  (`gateway.py:156-157`).
- **Budget guard fail-closed**: per-job and global monthly ceilings refuse
  over-budget dispatch with explicit receipts; a kill switch is wired into
  the gateway (`gateway.py:158-179`).
- **Artifact URI hardening**: scheme allowlist (`file://`, `s3://`) and
  explicit `..` traversal rejection for both file paths and S3 keys, with
  `SecurityReceipt` audit trail (`artifacts.py:200-225, 282-293`).
- **Pydantic `extra="forbid"`** on request envelopes and causal graph
  models (`routes/quant_foundry.py:46`, `causal_graph.py:29,38,57`) and
  field validators (e.g. `strength` in `[0,1]`).
- **No secrets committed**: `.env.example` uses empty placeholders and the
  documented dev default; `.gitignore` covers `.env`/`.env.*` with an
  `!.env.example` carve-out; `strategies/alpaca.live.json` and
  `alpaca.live.history.jsonl` contain only symbols/params, no credentials.
- **No client-side secret exposure**: no `NEXT_PUBLIC_*API_KEY*`/`SECRET`
  usage in the dashboard; no `dangerouslySetInnerHTML`/`eval`/`new
  Function` in `apps/dashboard/src`.
- **No shell injection surface**: no `subprocess`/`os.system`/`shell=True`/
  `eval`/`exec` in `services/strategy_host/src`.
- **Fixed LLM provider URLs** in the portfolio-report route (no
  user-controlled URL → no SSRF); API keys read server-side only and
  never returned in responses; `sanitizeReportResponse` is applied to LLM
  output before it is returned.

---

## Suggested fix priority

1. **H1** (JWT expiry) — highest payoff, small change.
2. **M3** (callback secret startup guard) — closes the analog of the
   already-guarded JWT footgun.
3. **M1** (per-request `.env` reads) — align secret handling with the
   rest of the stack.
4. **M2** (typed/bounded `request_payload`) — pair with a route body-size
   limit.
5. L1–L4 — hardening pass.

> Note: TLS/secure-cookie/HSTS were intentionally *not* flagged, per the
> review guidance (dev/local setups commonly run without TLS and a
> premature HSTS recommendation can cause outages).
