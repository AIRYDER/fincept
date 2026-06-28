# Railway ↔ RunPod Connection Hardening

**Commit:** `ed643d6` on `codex/portfolio-optimizer-core`
**Date:** 2026-06-25
**Scope:** `services/quant_foundry/`, `services/api/`, `runpod/`, `docs/`, `railway-production.json`, `.env.example`
**Status:** All tests pass (21 new + 976 existing), ruff + mypy clean

---

## 1. Problem Statement

An audit of the Railway-hosted API ↔ RunPod-deployed GPU workers connection
identified several issues that broke deployment determinism on fresh deploys.
The most critical: **an env var naming mismatch that caused silent dispatch
failure** — the Railway config used `QUANT_FOUNDRY_RUNPOD_*` names while
`gateway.from_env()` expected `RUNPOD_*` names, resulting in an empty API key
and endpoint IDs at runtime.

This was a known issue from a previous live training session
(`docs/RUNPOD_LIVE_TRAINING_SESSION_SUMMARY.md`, Bug 2 + Bug 3).

### Issues by severity

| # | Severity | Issue |
|---|----------|-------|
| 1 | Critical | Env var naming mismatch: `QUANT_FOUNDRY_RUNPOD_*` vs `RUNPOD_*` |
| 2 | High | Stale hardcoded endpoint IDs committed to repo |
| 3 | High | Callback secret drift between RunPod and Railway (no parity check) |
| 4 | High | `HttpRunPodClient.dispatch()` docstring claimed POST callback path; actual path is polling |
| 5 | Medium | Inference Dockerfile missing `ENV QUANT_FOUNDRY_CALLBACK_SECRET=""` placeholder |
| 6 | Medium | No fail-closed for empty API key (failed at dispatch time, not startup) |
| 7 | Low | `.env.example` incomplete (no RunPod vars documented) |

---

## 2. Changes Made

### 2.1 Env var compat shim + fail-closed validation (Critical)

**Files:**
- `services/quant_foundry/src/quant_foundry/gateway_helpers.py` — new `env_first()` helper
- `services/quant_foundry/src/quant_foundry/gateway.py` — `from_env()` rewritten + `RunPodConfigError`

#### `env_first()` helper

```python
def env_first(primary: str, *fallbacks: str, default: str = "") -> str:
    """Resolve an env var preferring the canonical name, falling back to
    deprecated names with a DeprecationWarning."""
```

- Reads the canonical name first. If set (non-empty), returns it.
- Falls back to deprecated names in order, emitting a `DeprecationWarning` for each fallback used.
- Returns `default` if neither primary nor any fallback is set.
- Empty-string env vars do NOT count as "set" — they fall through to the next fallback.

#### Canonical env var names (single source of truth)

Defined as module-level constants in `gateway.py`:

| Canonical (preferred) | Deprecated (fallback with warning) | Required? |
|-----------------------|------------------------------------|-----------|
| `RUNPOD_API_KEY` | `QUANT_FOUNDRY_RUNPOD_API_KEY` | Yes (runpod mode) |
| `RUNPOD_TRAINING_ENDPOINT_ID` | `QUANT_FOUNDRY_RUNPOD_TRAINING_ENDPOINT` | Yes (runpod mode) |
| `RUNPOD_INFERENCE_ENDPOINT_ID` | `QUANT_FOUNDRY_RUNPOD_INFERENCE_ENDPOINT` | Yes (runpod mode) |
| `RUNPOD_BASE_URL` | — | No (default: `https://api.runpod.ai/v2`) |
| `RUNPOD_TIMEOUT_SECONDS` | — | No (default: `30`) |
| `RUNPOD_COST_PER_DISPATCH_CENTS` | — | No (default: `0`) |
| `QUANT_FOUNDRY_CALLBACK_SECRET` | — | Yes (runpod mode) |

Also reads `RUNPOD_ENDPOINT_ID` as a legacy fallback for both training and
inference (single-endpoint setups).

#### `RunPodConfigError` (fail-closed)

```python
class RunPodConfigError(RuntimeError):
    """Raised when RunPod dispatch mode is enabled but required env vars
    are missing."""
```

`from_env()` raises this at startup when `QUANT_FOUNDRY_MODE` is a runpod
mode (`runpod`, `runpod_research`, `runpod_shadow`) and any of the four
required env vars are missing. The error message lists the missing names
and points to `docs/RAILWAY_DEPLOY_GUIDE.md`.

**This prevents a silent deploy that looks healthy but cannot dispatch.**
The API will fail to start rather than running with an empty API key.

### 2.2 Health reporting (High)

**File:** `services/quant_foundry/src/quant_foundry/gateway.py`

#### New `health()` fields

```python
{
    "enabled": True,
    "mode": "runpod_shadow",
    "shadow_only": True,
    "job_count": 0,
    "runpod_wired": True,
    "runpod_config_valid": True,          # NEW
    "missing_env": [],                     # NEW (list of missing env var names)
    "runpod_routes": {"training": "...", "inference": "..."},
    "paper_bridge": {...},
}
```

#### New `runpod_config_status()` method

Returns `{"valid": bool, "missing_env": list[str]}`. In non-runpod modes,
`valid` is always `True` and `missing_env` is empty. In runpod modes,
checks that clients are wired for both training and inference and that
the callback secret is non-empty.

**Never returns secret values** — only the names of missing env vars.

### 2.3 Callback-secret canary (High)

**Files:**
- `services/quant_foundry/src/quant_foundry/gateway.py` — `runpod_canary()` method
- `services/api/src/api/routes/quant_foundry.py` — `GET /health/runpod-canary` route
- `runpod/quant-foundry-training/handler.py` — `_handle_canary()` + dispatch check
- `runpod/quant-foundry-inference/handler.py` — `_handle_canary()` + dispatch check

#### How the canary works

1. The API dispatches a tiny job to a RunPod endpoint with
   `task=callback_secret_canary` and a random nonce.
2. The RunPod worker sees `task=callback_secret_canary`, signs the
   nonce-bearing payload with its copy of `QUANT_FOUNDRY_CALLBACK_SECRET`,
   and returns the signed callback fields immediately (no training/inference
   pipeline).
3. The API polls `check_status()`, extracts the callback fields, and
   verifies the HMAC signature with its own copy of the secret.
4. If `verified: true`, both sides share the same secret. If
   `verified: false`, there is secret drift.

#### Endpoint

```
GET /quant-foundry/health/runpod-canary?job_type=training
```

Bearer-auth required. Returns:

```json
{
    "ok": true,
    "verified": true,
    "job_type": "training",
    "nonce": "abc123...",
    "runpod_job_id": "rp-...",
    "detail": "signature verified"
}
```

Never raises — errors are reported as `ok: false` with a detail string.
Never returns secret values.

#### Handler-side implementation

Both training and inference handlers check for `task=callback_secret_canary`
at the top of `handler()` and route to `_handle_canary()`, which bypasses
the training/inference pipeline entirely and returns immediately with the
signed callback fields.

### 2.4 Remove stale hardcoded endpoint IDs (High)

**File:** `railway-production.json`

**Before:** Hardcoded production endpoint IDs (`h2blqodcicxqyy`,
`t31u1z426jy1ub`) committed to the repo.

**After:** Uses `${{secrets.RUNPOD_TRAINING_ENDPOINT_ID}}` and
`${{secrets.RUNPOD_INFERENCE_ENDPOINT_ID}}` placeholders. The
`_secret_injection` section documents how to set them in the Railway
dashboard.

### 2.5 Doc/impl mismatch fix (High)

**File:** `services/quant_foundry/src/quant_foundry/runpod_client.py`

The `HttpRunPodClient` class docstring previously stated that results come
back via the HMAC-signed callback endpoint
(`POST /quant-foundry/callbacks/runpod`). The actual implementation polls
RunPod's `check_status()` API and extracts signed callback fields from the
polled output.

The docstring now correctly describes the polling path as the real
production path, and clarifies that the `POST /callbacks/runpod` endpoint
is only for operator-initiated manual callback submission or external
webhook integrations.

### 2.6 Inference Dockerfile (Medium)

**File:** `runpod/quant-foundry-inference/Dockerfile`

Added `ENV QUANT_FOUNDRY_CALLBACK_SECRET=""` placeholder, matching the
training Dockerfile. The handler fails closed if the secret is not
injected at runtime via the RunPod template environment.

### 2.7 .env.example (Low)

**File:** `.env.example`

Added documentation for all RunPod env vars with canonical names:

```
QUANT_FOUNDRY_ENABLED=false
QUANT_FOUNDRY_MODE=local_mock
QUANT_FOUNDRY_CALLBACK_SECRET=
RUNPOD_API_KEY=
RUNPOD_TRAINING_ENDPOINT_ID=
RUNPOD_INFERENCE_ENDPOINT_ID=
RUNPOD_BASE_URL=https://api.runpod.ai/v2
RUNPOD_TIMEOUT_SECONDS=60
RUNPOD_COST_PER_DISPATCH_CENTS=0
```

### 2.8 Docs + verification template

**Files:**
- `docs/RAILWAY_DEPLOY_GUIDE.md` — updated env var tables, deprecated name notes, canary verification step
- `reports/verification/railway-deployment-template.md` — updated RunPod connectivity checklist + secrets hygiene checklist

---

## 3. Files Changed

| File | Change |
|------|--------|
| `services/quant_foundry/src/quant_foundry/gateway_helpers.py` | + `env_first()` helper |
| `services/quant_foundry/src/quant_foundry/gateway.py` | + `RunPodConfigError`, `from_env()` rewrite, `runpod_config_status()`, `runpod_canary()`, `health()` fields |
| `services/quant_foundry/src/quant_foundry/runpod_client.py` | `HttpRunPodClient` docstring fix |
| `services/quant_foundry/tests/test_runpod_connection_hardening.py` | **NEW** — 21 tests |
| `services/api/src/api/routes/quant_foundry.py` | + `GET /health/runpod-canary` route |
| `runpod/quant-foundry-training/handler.py` | + `_handle_canary()` + dispatch check |
| `runpod/quant-foundry-inference/handler.py` | + `_handle_canary()` + dispatch check |
| `runpod/quant-foundry-inference/Dockerfile` | + `ENV QUANT_FOUNDRY_CALLBACK_SECRET=""` |
| `railway-production.json` | Canonical env names + secret placeholders + safety invariants |
| `.env.example` | + RunPod env vars |
| `docs/RAILWAY_DEPLOY_GUIDE.md` | Updated env var tables + canary verification |
| `reports/verification/railway-deployment-template.md` | Updated connectivity + secrets checklist |

---

## 4. Tests

**File:** `services/quant_foundry/tests/test_runpod_connection_hardening.py`

21 tests covering:

### `env_first()` helper (4 tests)
- `test_env_first_prefers_primary` — canonical wins when both set
- `test_env_first_falls_back_with_warning` — fallback emits DeprecationWarning
- `test_env_first_returns_default_when_neither_set` — default returned
- `test_env_first_empty_string_falls_through` — empty string doesn't count as set

### `from_env()` canonical + legacy (3 tests)
- `test_from_env_reads_canonical_runpod_vars` — canonical names load correctly
- `test_from_env_reads_legacy_quant_foundry_runpod_vars_with_warning` — legacy names work with warnings
- `test_from_env_prefers_canonical_over_legacy` — canonical wins when both set

### Fail-closed (4 tests)
- `test_from_env_fails_closed_when_runpod_mode_and_api_key_missing`
- `test_from_env_fails_closed_when_runpod_mode_and_endpoint_missing`
- `test_from_env_fails_closed_when_runpod_mode_and_callback_secret_missing`
- `test_from_env_does_not_raise_in_local_mock_mode` — local_mock doesn't require RunPod vars

### Health (4 tests)
- `test_health_reports_runpod_config_valid_when_wired`
- `test_health_reports_runpod_config_invalid_when_endpoints_missing`
- `test_health_reports_valid_in_local_mock_mode`
- `test_health_never_exposes_secrets` — secret value never appears in health JSON

### Canary (4 tests)
- `test_runpod_canary_verifies_when_secrets_match` — `verified: true`
- `test_runpod_canary_fails_when_secrets_differ` — `verified: false`
- `test_runpod_canary_returns_not_runpod_mode_in_local_mock`
- `test_runpod_canary_returns_no_client_when_endpoint_not_wired`

### Polling callback verification (2 tests)
- `test_polled_runpod_output_with_valid_hmac_registers_callback` — valid signature registers dossier
- `test_polled_runpod_output_missing_signature_fails_closed` — missing fields fail closed

### Test helpers

The test file includes a `CanaryRecordingClient` mock that simulates a
RunPod serverless endpoint: dispatch returns a job ID, and check_status
returns COMPLETED with canary callback fields signed with a configurable
worker secret. This allows testing secret-match and secret-drift scenarios
without real RunPod dispatch.

---

## 5. Migration Guide for Operators

### If you have an existing Railway dashboard setup

1. **Rename your env vars** in the Railway dashboard:
   - `QUANT_FOUNDRY_RUNPOD_API_KEY` → `RUNPOD_API_KEY`
   - `QUANT_FOUNDRY_RUNPOD_TRAINING_ENDPOINT` → `RUNPOD_TRAINING_ENDPOINT_ID`
   - `QUANT_FOUNDRY_RUNPOD_INFERENCE_ENDPOINT` → `RUNPOD_INFERENCE_ENDPOINT_ID`

2. **The old names still work** as fallbacks (with a DeprecationWarning in
   logs), so you won't break anything during migration. But migrate ASAP.

3. **After your next deploy**, verify the connection:
   ```bash
   # Check health (bearer-auth)
   curl -H "Authorization: Bearer $TOKEN" \
     https://<api>.up.railway.app/quant-foundry/health
   # Expect: "runpod_config_valid": true, "missing_env": []

   # Run the callback-secret canary
   curl -H "Authorization: Bearer $TOKEN" \
     https://<api>.up.railway.app/quant-foundry/health/runpod-canary
   # Expect: "verified": true
   ```

4. **If the canary returns `verified: false`**, your RunPod endpoint
   template has a different `QUANT_FOUNDRY_CALLBACK_SECRET` than Railway.
   Fix the drift by copying the secret from one side to the other.

### If you are setting up a fresh deploy

Follow `docs/RAILWAY_DEPLOY_GUIDE.md` sections h–i. Use the canonical
`RUNPOD_*` env var names. Set endpoint IDs as Railway variables (not
secrets — they are not sensitive, but should not be committed to the repo).

---

## 6. Key Invariants for Other Agents

1. **Never commit production RunPod endpoint IDs to the repo.** Use
   `${{secrets.*}}` placeholders in `railway-production.json`.

2. **Never use `QUANT_FOUNDRY_RUNPOD_*` env var names in new code.** Use
   the canonical `RUNPOD_*` names. The legacy names are read as fallbacks
   only for backward compatibility.

3. **`from_env()` fails closed in runpod mode.** If you add a new required
   env var, add it to the `missing` check in `from_env()` and to
   `runpod_config_status()`.

4. **`health()` never exposes secret values.** Only missing env var names
   are returned. If you add a new secret, do NOT add its value to health.

5. **The canary is a LIVE check.** `runpod_canary()` dispatches a real
   job to RunPod. It is NOT a static config check. Use it for post-deploy
   verification, not for healthcheck polling.

6. **The polling path is the real production path.** RunPod does not push
   results to `POST /quant-foundry/callbacks/runpod`. The API polls
   `check_status()` and extracts signed callback fields from the polled
   output. The POST endpoint exists only for manual/operator use.

7. **Both training and inference handlers must handle
   `task=callback_secret_canary`.** If you add a new RunPod handler, add
   the canary check at the top of `handler()`.

8. **`env_first()` treats empty string as unset.** This is intentional —
   an empty env var should not mask a fallback that has a real value.

---

## 7. Related Documentation

- `docs/RAILWAY_DEPLOY_GUIDE.md` — operator deploy guide (updated)
- `docs/RUNPOD_LIVE_TRAINING_SESSION_SUMMARY.md` — original bug report (Bug 2 + Bug 3)
- `docs/GPU_DEPLOYMENT_GUIDE.md` — GPU deployment guide
- `reports/verification/railway-deployment-template.md` — post-deploy verification checklist (updated)
- `railway-production.json` — Railway service topology (updated)
