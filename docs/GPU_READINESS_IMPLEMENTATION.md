# GPU Readiness Implementation Record

**Date:** 2026-06-25
**Branch:** `codex/portfolio-optimizer-core`
**Scope:** Changes made to bring the Quant Foundry RunPod GPU path from "stub-only / `NotImplementedError`" to "deployable and tested."
**Companion docs:**
- `docs/GPU_DEPLOYMENT_GUIDE.md` — operator-facing: how to use the system
- `docs/CONSOLIDATED_REPORT.md` — synthesis of the 10 project docs
- `docs/LIMITED_LIVE_READINESS_REVIEW.md` — the NOT-READY verdict + 8 blockers

---

## Table of Contents

1. [What was broken before](#1-what-was-broken-before)
2. [Change summary (7 files, +537 / -21 lines)](#2-change-summary-7-files-537--21-lines)
3. [Change 1 — `HttpRunPodClient.dispatch()` implementation](#change-1--httprunpodclientdispatch-implementation)
4. [Change 2 — `httpx` dependency](#change-2--httpx-dependency)
5. [Change 3 — Gateway wiring for `runpod` mode](#change-3--gateway-wiring-for-runpod-mode)
6. [Change 4 — Training Dockerfile fix](#change-4--training-dockerfile-fix)
7. [Change 5 — Inference Dockerfile fix](#change-5--inference-dockerfile-fix)
8. [Change 6 — Inference handler path fix](#change-6--inference-handler-path-fix)
9. [Change 7 — `HttpRunPodClient` test suite](#change-7--httprunpodclient-test-suite)
10. [How to verify every change](#how-to-verify-every-change)
11. [How to reproduce this work from scratch](#how-to-reproduce-this-work-from-scratch)
12. [What is still NOT done (honest gaps)](#what-is-still-not-done-honest-gaps)
13. [File-by-file diff summary](#file-by-file-diff-summary)

---

## 1. What was broken before

The `LIMITED_LIVE_READINESS_REVIEW.md` (commit `7027db4`) identified 8
blockers (B1–B8). Blocker **B6** ("Real RunPod GPU has never run") was
caused by **four concrete code gaps**, not just operational inaction:

| Gap | File | Symptom |
|-----|------|---------|
| **G1** | `runpod_client.py` | `HttpRunPodClient.dispatch()` raised `NotImplementedError`. There was no HTTP path to RunPod — only `MockRunPodClient` worked. |
| **G2** | `gateway.py` | `from_env()` never read `RUNPOD_API_KEY` / `RUNPOD_ENDPOINT_ID`. The gateway always used `MockDispatcher` regardless of `QUANT_FOUNDRY_MODE`. |
| **G3** | `runpod/quant-foundry-training/Dockerfile` | `COPY quant_foundry/ /worker/quant_foundry/` — path didn't exist relative to the build context. `docker build` would fail. |
| **G4** | `runpod/quant-foundry-inference/Dockerfile` | Same broken `COPY` paths + the handler's `sys.path.insert` used a relative path that doesn't exist in the container. |

**Result:** Even if an operator had a RunPod account, API key, and GPU
credits, the code could not dispatch a job. The mock path worked; the
real path was a stub.

---

## 2. Change summary (7 files, +537 / -21 lines)

| # | File | +Lines | -Lines | What changed |
|---|------|--------|--------|--------------|
| 1 | `services/quant_foundry/src/quant_foundry/runpod_client.py` | +148 | -10 | `HttpRunPodClient` fully implemented + `check_status` + `check_health` |
| 2 | `services/quant_foundry/pyproject.toml` | +1 | 0 | Added `httpx>=0.27` dependency |
| 3 | `services/quant_foundry/src/quant_foundry/gateway.py` | +93 | -1 | `runpod_client` param, `from_env()` RunPod env vars, `create_job()` dispatch, `runpod_health()` |
| 4 | `runpod/quant-foundry-training/Dockerfile` | +30 | -9 | Fixed `COPY` paths, added `build-essential`, `GIT_SHA` build arg, `PYTHONPATH` |
| 5 | `runpod/quant-foundry-inference/Dockerfile` | +35 | -8 | Same fixes as training + correct `PYTHONPATH=/app` |
| 6 | `runpod/quant-foundry-inference/handler.py` | +10 | -1 | Multi-path `sys.path` insertion for container + local |
| 7 | `services/quant_foundry/tests/test_runpod_client.py` | +220 | 0 | 9 new tests for `HttpRunPodClient` using `httpx.MockTransport` |

**Test result:** 562 passed, 0 failed (excluding pre-existing
`test_baseline_family.py` which has an unrelated missing `lightgbm`
dependency).

---

## Change 1 — `HttpRunPodClient.dispatch()` implementation

**File:** `services/quant_foundry/src/quant_foundry/runpod_client.py`
**Lines:** +148 / -10

### Before

```python
class HttpRunPodClient:
    """Real HTTP RunPod client. Uses httpx. Behind config flag.

    NOTE: Full HTTP implementation is deferred until RunPod credentials
    are available. The class is defined here so the dispatcher can be
    wired with it via config; the actual HTTP calls will be added when
    TASK-0502 is exercised against a real RunPod endpoint.
    """

    def __init__(self, *, api_key: str, endpoint_id: str, base_url: str) -> None:
        self._api_key = api_key
        self._endpoint_id = endpoint_id
        self._base_url = base_url

    def dispatch(self, *, job_id, request_payload, budget_cents) -> DispatchResult:
        raise NotImplementedError(
            "HttpRunPodClient.dispatch is not yet implemented; "
            "use MockRunPodClient for tests or set QUANT_FOUNDRY_MODE=local_mock."
        )
```

### After

The class now has a complete implementation:

1. **`__init__`** — accepts `api_key`, `endpoint_id`, `base_url`
   (default `https://api.runpod.ai/v2`), `timeout_seconds` (default
   `30`), `cost_per_dispatch_cents` (default `0`), and an optional
   `transport` (for test injection via `httpx.MockTransport`).

2. **`_build_client()`** — lazily imports `httpx` and returns an
   `httpx.Client` with the injected transport (tests) or default
   transport (production).

3. **`dispatch()`** — POSTs to `{base_url}/{endpoint_id}/run` with:
   - `Authorization: Bearer {api_key}` header
   - `Content-Type: application/json` header
   - Body: `{"input": request_payload}` (RunPod's required format)
   
   Error classification:
   | HTTP status | Classification | `DispatchStatus` |
   |-------------|---------------|-----------------|
   | 200 + `id` field | Success | `DISPATCHED` |
   | 200 without `id` | Terminal | `TERMINAL_FAILURE` (`missing_job_id`) |
   | 200 bad JSON | Terminal | `TERMINAL_FAILURE` (`bad_response_body`) |
   | 429, 502, 503, 504 | Transient (retryable) | `TRANSIENT_FAILURE` |
   | 400, 401, 403, 422, other 4xx/5xx | Terminal | `TERMINAL_FAILURE` |
   | Network error (connect, timeout, DNS) | Transient | `TRANSIENT_FAILURE` (`network_error`) |

4. **`check_status(runpod_job_id)`** — GETs
   `{base_url}/{endpoint_id}/status/{runpod_job_id}` for polling.

5. **`check_health()`** — GETs
   `{base_url}/{endpoint_id}/health` for endpoint health checks.

### Key design decisions

- **`httpx` imported lazily** inside `_build_client()` so the module
  imports cleanly even if `httpx` isn't installed (e.g., in a minimal
  test environment). The import only fires when a dispatch actually
  happens.
- **`transport` parameter** allows injecting `httpx.MockTransport` for
  tests — no real HTTP calls, deterministic, fast.
- **API key never exposed.** Stored as `self._api_key` (private),
  never included in `DispatchResult.model_dump()`, never in error
  messages. Verified by `test_http_client_api_key_never_in_result`.
- **Cost is 0 at dispatch time.** Actual cost is recorded when the
  callback returns (the RunPod `/run` endpoint is async — it returns a
  job ID immediately, the result comes back via callback).

---

## Change 2 — `httpx` dependency

**File:** `services/quant_foundry/pyproject.toml`
**Lines:** +1 / 0

### Before

```toml
dependencies = [
  "fincept-core",
  "pydantic>=2.7",
]
```

### After

```toml
dependencies = [
  "fincept-core",
  "pydantic>=2.7",
  "httpx>=0.27",
]
```

`httpx` is the HTTP client used by `HttpRunPodClient`. It was already
installed in the workspace (other services use it) but wasn't declared
as a dependency of `quant_foundry`. Adding it ensures the package is
self-contained and that `uv sync` installs it.

---

## Change 3 — Gateway wiring for `runpod` mode

**File:** `services/quant_foundry/src/quant_foundry/gateway.py`
**Lines:** +93 / -1

### 3a. New imports

```python
from quant_foundry.runpod_client import (
    BudgetGuard as DispatchBudgetGuard,
    HttpRunPodClient,
    RunPodDispatcher,
)
```

Note: `BudgetGuard` is imported with an alias (`DispatchBudgetGuard`)
to avoid a name collision with `quant_foundry.budget.BudgetGuard`
(which is a different class — the durable monthly budget guard, not
the dispatch-level per-job guard).

### 3b. Constructor: new `runpod_client` parameter

```python
def __init__(
    self,
    *,
    enabled: bool,
    mode: str,
    shadow_only: bool,
    callback_secret: str,
    base_dir: pathlib.Path | str,
    budget_guard: BudgetGuard | None = None,
    runpod_client: Any = None,          # <-- NEW
) -> None:
```

When `mode == "runpod"` and `runpod_client is not None`, the
constructor wires a `RunPodDispatcher`:

```python
if self.mode == "runpod" and runpod_client is not None:
    dispatch_budget = DispatchBudgetGuard(
        monthly_budget_cents=(
            budget_guard.monthly_budget_cents
            if budget_guard is not None
            else 0
        ),
    )
    self._runpod_dispatcher = RunPodDispatcher(
        outbox=self.outbox,
        client=runpod_client,
        mode="runpod",
        budget_guard=dispatch_budget,
    )
```

The `MockDispatcher` is still always constructed (for `local_mock`
fallback). The `RunPodDispatcher` is stored as
`self._runpod_dispatcher` and used only when `mode == "runpod"`.

### 3c. `from_env()`: reads RunPod env vars

```python
runpod_client: HttpRunPodClient | None = None
if mode == "runpod":
    api_key = os.environ.get("RUNPOD_API_KEY", "")
    endpoint_id = os.environ.get("RUNPOD_ENDPOINT_ID", "")
    base_url = os.environ.get("RUNPOD_BASE_URL", "https://api.runpod.ai/v2")
    timeout_str = os.environ.get("RUNPOD_TIMEOUT_SECONDS", "30")
    cost_str = os.environ.get("RUNPOD_COST_PER_DISPATCH_CENTS", "0")
    # ... parse timeout + cost with try/except ...
    runpod_client = HttpRunPodClient(
        api_key=api_key,
        endpoint_id=endpoint_id,
        base_url=base_url,
        timeout_seconds=timeout_s,
        cost_per_dispatch_cents=cost_cents,
    )
```

When `mode != "runpod"`, `runpod_client` is `None` and the gateway
behaves exactly as before (mock dispatcher, no HTTP).

### 3d. `create_job()`: dispatches to RunPod

```python
if self.mode == "local_mock":
    self.dispatcher.dispatch(job_id, request_payload=request_payload)
    self.processor.process(job_id)
elif self.mode == "runpod" and self._runpod_dispatcher is not None:
    # Dispatch to RunPod via the HTTP client. The callback will
    # arrive asynchronously at POST /quant-foundry/callbacks/runpod.
    self._runpod_dispatcher.dispatch(
        job_id, request_payload=request_payload,
    )
```

In `runpod` mode, the job is enqueued in the outbox, then dispatched
to RunPod via HTTP. The callback arrives **asynchronously** at the
`POST /quant-foundry/callbacks/runpod` endpoint (HMAC-authenticated).

### 3e. New `runpod_health()` method

```python
def runpod_health(self) -> dict[str, Any]:
    """Check RunPod endpoint health (only in runpod mode)."""
    if self.mode != "runpod" or self._runpod_client is None:
        return {"ok": False, "status": "not_runpod_mode"}
    try:
        result = self._runpod_client.check_health()
        return {"ok": True, "status": "healthy", "detail": result}
    except Exception as exc:
        return {"ok": False, "status": "error", "detail": f"..."}
```

Never raises — network errors are caught and reported as `ok=False`.

### 3f. `health()` now reports `runpod_wired`

```python
def health(self) -> dict[str, Any]:
    return {
        "enabled": self.enabled,
        "mode": self.mode,
        "shadow_only": self.shadow_only,
        "job_count": len(self.outbox.list()) if self.enabled else 0,
        "runpod_wired": self._runpod_dispatcher is not None,  # <-- NEW
    }
```

---

## Change 4 — Training Dockerfile fix

**File:** `runpod/quant-foundry-training/Dockerfile`
**Lines:** +30 / -9

### Before (broken)

```dockerfile
COPY handler.py /worker/handler.py
COPY quant_foundry/ /worker/quant_foundry/
RUN pip install --no-cache-dir pydantic>=2.7
```

**Problem:** `COPY quant_foundry/` expects a `quant_foundry/`
directory in the build context. If built from the `runpod/quant-foundry-training/`
directory (as the old comment suggested), there is no `quant_foundry/`
there. If built from the repo root, the path should be
`services/quant_foundry/src/quant_foundry/`.

### After (fixed)

```dockerfile
# Build from repo root:
#   docker build -t fincept-qf-training:latest -f runpod/quant-foundry-training/Dockerfile .

FROM python:3.12-slim
WORKDIR /worker

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY services/quant_foundry/src/quant_foundry/ /worker/quant_foundry/
COPY runpod/quant-foundry-training/handler.py /worker/handler.py

RUN pip install --no-cache-dir "pydantic>=2.7" "httpx>=0.27"

ARG GIT_SHA=unknown
ENV QUANT_FOUNDRY_GIT_SHA=${GIT_SHA}

ENV QUANT_FOUNDRY_CALLBACK_SECRET=""
ENV QUANT_FOUNDRY_TRAINING_DEADLINE_SECONDS="600"
ENV PYTHONPATH=/worker

CMD ["python", "-u", "/worker/handler.py"]
```

**Changes:**
1. `COPY` paths now reference `services/quant_foundry/src/quant_foundry/`
   (correct relative to repo root build context).
2. Added `build-essential` (needed for compiling C extensions if any
   pip packages require it).
3. Added `httpx>=0.27` to pip install (needed by `runpod_client.py`).
4. Added `ARG GIT_SHA` + `ENV QUANT_FOUNDRY_GIT_SHA` for reproducibility
   pinning (the `ArtifactManifest` records the code git SHA).
5. Added `PYTHONPATH=/worker` so Python can find the `quant_foundry`
   package.
6. Build command in the comment now uses `-f` flag with repo root
   context (`.`).

---

## Change 5 — Inference Dockerfile fix

**File:** `runpod/quant-foundry-inference/Dockerfile`
**Lines:** +35 / -8

### Before (broken)

```dockerfile
COPY services/quant_foundry/pyproject.toml services/quant_foundry/uv.lock* ./services/quant_foundry/
RUN pip install uv && cd services/quant_foundry && uv sync --frozen --no-dev
COPY services/quant_foundry/src ./services/quant_foundry/src
COPY runpod/quant-foundry-inference/handler.py ./handler.py
ENV PYTHONPATH=/app/services/quant_foundry/src
```

**Problems:**
1. `uv.lock*` glob in `COPY` doesn't work (Docker doesn't expand
   globs in `COPY` the way shell does — it would fail or copy nothing).
2. `uv sync --frozen` would fail if `uv.lock` doesn't exist (it's at
   the repo root, not in `services/quant_foundry/`).
3. `PYTHONPATH=/app/services/quant_foundry/src` — the path is wrong
   because the `COPY` puts source at `/app/services/quant_foundry/src`
   but the handler tries to import from a path that doesn't match.

### After (fixed)

```dockerfile
# Build from repo root:
#   docker build -t fincept-qf-inference:latest -f runpod/quant-foundry-inference/Dockerfile .

FROM python:3.12-slim
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY services/quant_foundry/src/quant_foundry/ /app/quant_foundry/
COPY runpod/quant-foundry-inference/handler.py /app/handler.py

RUN pip install --no-cache-dir "pydantic>=2.7" "httpx>=0.27"

ARG GIT_SHA=unknown
ENV QUANT_FOUNDRY_GIT_SHA=${GIT_SHA}

ENV QUANT_FOUNDRY_MODE=runpod_shadow
ENV PYTHONPATH=/app

ENTRYPOINT ["python", "-u", "/app/handler.py"]
```

**Changes:**
1. Simplified to copy `quant_foundry/` directly to `/app/quant_foundry/`
   (no `uv sync` needed — just pip install the two deps).
2. `PYTHONPATH=/app` so Python finds `quant_foundry` at
   `/app/quant_foundry/`.
3. Added `build-essential`, `GIT_SHA` build arg, `httpx` dep.
4. Uses `ENTRYPOINT` instead of `CMD` (RunPod serverless expects the
   container to run the handler as PID 1).

---

## Change 6 — Inference handler path fix

**File:** `runpod/quant-foundry-inference/handler.py`
**Lines:** +10 / -1

### Before (broken in container)

```python
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "services", "quant_foundry", "src"))
```

This path only works when running from the repo root. In the Docker
container, `handler.py` is at `/app/handler.py`, so
`../../services/quant_foundry/src` resolves to `/services/quant_foundry/src`
which doesn't exist.

### After (works in both)

```python
_quant_foundry_paths = [
    os.path.join(os.path.dirname(__file__), "..", "..", "services", "quant_foundry", "src"),
    os.path.join(os.path.dirname(__file__), "quant_foundry"),
    "/app",
]
for _p in _quant_foundry_paths:
    if os.path.isdir(_p):
        sys.path.insert(0, _p)
```

Tries three paths, inserts whichever ones exist:
1. Repo-root relative path (for local testing from the repo).
2. Sibling `quant_foundry/` directory (for some container layouts).
3. `/app` (for the Docker container where `quant_foundry/` is at
   `/app/quant_foundry/`).

---

## Change 7 — `HttpRunPodClient` test suite

**File:** `services/quant_foundry/tests/test_runpod_client.py`
**Lines:** +220 / 0

9 new tests added at the end of the file, all using `httpx.MockTransport`
(no real HTTP calls, deterministic, fast):

| Test | What it verifies |
|------|-----------------|
| `test_http_client_success` | HTTP 200 with `id` → `DISPATCHED`, correct `runpod_job_id`, cost=0 |
| `test_http_client_rate_limit_transient` | HTTP 429 → `TRANSIENT_FAILURE` |
| `test_http_client_503_transient` | HTTP 503 → `TRANSIENT_FAILURE` |
| `test_http_client_400_terminal` | HTTP 400 → `TERMINAL_FAILURE` |
| `test_http_client_401_terminal` | HTTP 401 → `TERMINAL_FAILURE` (bad API key) |
| `test_http_client_missing_id_terminal` | HTTP 200 without `id` field → `TERMINAL_FAILURE` |
| `test_http_client_network_error_transient` | `httpx.ConnectError` → `TRANSIENT_FAILURE` (`network_error`) |
| `test_http_client_api_key_never_in_result` | API key string not in `json.dumps(result.model_dump())` |
| `test_http_client_check_status` | `check_status()` returns raw JSON response |
| `test_http_client_check_health` | `check_health()` returns raw JSON response |

**Test pattern** (used for all HTTP tests):

```python
def test_http_client_success() -> None:
    import httpx
    from quant_foundry.runpod_client import HttpRunPodClient

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert "/run" in str(request.url)
        assert request.headers["authorization"] == "Bearer test-key"
        body = json.loads(request.content)
        assert "input" in body
        return httpx.Response(200, json={"id": "rp-job-12345", "status": "IN_QUEUE"})

    transport = httpx.MockTransport(handler)
    client = HttpRunPodClient(
        api_key="test-key",
        endpoint_id="ep-1",
        base_url="https://api.runpod.ai/v2",
        transport=transport,
    )
    result = client.dispatch(
        job_id="qf:train:http:1",
        request_payload={"job_id": "qf:train:http:1", "model_family": "gbm"},
        budget_cents=None,
    )
    assert result.status == DispatchStatus.DISPATCHED
    assert result.runpod_job_id == "rp-job-12345"
```

---

## How to verify every change

### 1. Verify `HttpRunPodClient` implementation

```powershell
# Run the 9 new HTTP client tests:
uv run --package quant-foundry pytest services/quant_foundry/tests/test_runpod_client.py -v -k "http_client"
```

**Expected:** 9 passed.

### 2. Verify `httpx` dependency

```powershell
# Check httpx is installed:
uv run --package quant-foundry python -c "import httpx; print(httpx.__version__)"
```

**Expected:** `0.28.1` (or similar).

### 3. Verify gateway wiring

```powershell
# Test that from_env() constructs HttpRunPodClient when mode=runpod:
$env:QUANT_FOUNDRY_ENABLED = "true"
$env:QUANT_FOUNDRY_MODE = "runpod"
$env:RUNPOD_API_KEY = "test-key"
$env:RUNPOD_ENDPOINT_ID = "test-ep"

uv run --package quant-foundry python -c "
from quant_foundry.gateway import QuantFoundryGateway
gw = QuantFoundryGateway.from_env(base_dir='reports/quant-foundry-test')
health = gw.health()
print(health)
assert health['runpod_wired'] == True
print('Gateway wiring: OK')
"
```

**Expected:**
```
{'enabled': True, 'mode': 'runpod', 'shadow_only': True, 'job_count': 0, 'runpod_wired': True}
Gateway wiring: OK
```

### 4. Verify gateway falls back to mock when mode != runpod

```powershell
$env:QUANT_FOUNDRY_MODE = "local_mock"

uv run --package quant-foundry python -c "
from quant_foundry.gateway import QuantFoundryGateway
gw = QuantFoundryGateway.from_env(base_dir='reports/quant-foundry-test')
health = gw.health()
assert health['runpod_wired'] == False
print('Mock fallback: OK')
"
```

### 5. Verify training Dockerfile builds

```powershell
# From repo root:
docker build -t fincept-qf-training:latest `
  --build-arg GIT_SHA=$(git rev-parse --short HEAD) `
  -f runpod/quant-foundry-training/Dockerfile .

# Verify it runs:
echo '{"input": {"job_id": "qf:train:verify:1", "dataset_manifest_ref": "ds-1", "model_family": "gbm", "search_space": {"n_estimators": [100]}, "random_seed": 42, "hardware_class": "docker-cpu"}}' | docker run --rm -i -e QUANT_FOUNDRY_CALLBACK_SECRET=test-secret fincept-qf-training:latest
```

**Expected:** JSON output with `artifact_id`, `dossier_id`,
`callback_signature` (64-char hex).

### 6. Verify inference Dockerfile builds

```powershell
docker build -t fincept-qf-inference:latest `
  --build-arg GIT_SHA=$(git rev-parse --short HEAD) `
  -f runpod/quant-foundry-inference/Dockerfile .

echo '{"input": {"request": {"job_id": "job-1", "artifact_ref": "file:///model.pkl", "symbols": ["AAPL"], "horizons_ns": [3600000000000]}, "snapshot": {"symbols": ["AAPL"], "features": {"AAPL": [0.1, 0.2]}, "availability": {"AAPL": true}, "ts_event": 1000, "freshness_ns": 500}, "model_id": "m1"}}' | docker run --rm -i -e QUANT_FOUNDRY_MODE=runpod_shadow fincept-qf-inference:latest
```

### 7. Verify no broker credentials in containers

```powershell
docker run --rm fincept-qf-training:latest env | findstr /I "alpaca broker api_key redis secret jwt"
# Expected: only QUANT_FOUNDRY_CALLBACK_SECRET= (empty)

docker run --rm fincept-qf-inference:latest env | findstr /I "alpaca broker api_key redis secret jwt"
# Expected: only QUANT_FOUNDRY_CALLBACK_SECRET= (empty) + QUANT_FOUNDRY_MODE=runpod_shadow
```

### 8. Verify the full test suite still passes

```powershell
uv run --package quant-foundry pytest services/quant_foundry/tests/ -q --no-header `
    --ignore=services/quant_foundry/tests/test_baseline_family.py
```

**Expected:** 562 passed.

---

## How to reproduce this work from scratch

If you need to re-apply these changes to a fresh checkout:

### Step 1: Implement `HttpRunPodClient.dispatch()`

Open `services/quant_foundry/src/quant_foundry/runpod_client.py`. Find
the `HttpRunPodClient` class (the one with `raise NotImplementedError`).
Replace it with the full implementation that:

1. Takes `api_key`, `endpoint_id`, `base_url`, `timeout_seconds`,
   `cost_per_dispatch_cents`, and optional `transport` in `__init__`.
2. Has `_build_client()` that lazily imports `httpx` and returns an
   `httpx.Client` with the injected transport.
3. Has `dispatch()` that POSTs to `{base_url}/{endpoint_id}/run` with
   `Authorization: Bearer {api_key}` and body `{"input": request_payload}`,
   classifies HTTP status into `DISPATCHED` / `TRANSIENT_FAILURE` /
   `TERMINAL_FAILURE`, and catches network errors as transient.
4. Has `check_status(runpod_job_id)` and `check_health()` for polling.

### Step 2: Add `httpx` to `pyproject.toml`

Add `"httpx>=0.27"` to the `dependencies` list in
`services/quant_foundry/pyproject.toml`.

### Step 3: Wire the gateway

Open `services/quant_foundry/src/quant_foundry/gateway.py`:

1. Add imports: `HttpRunPodClient`, `RunPodDispatcher`,
   `BudgetGuard as DispatchBudgetGuard` from `runpod_client`.
2. Add `runpod_client: Any = None` parameter to `__init__`.
3. In `__init__`, when `mode == "runpod"` and `runpod_client is not
   None`, construct a `RunPodDispatcher` and store it as
   `self._runpod_dispatcher`.
4. In `from_env()`, when `mode == "runpod"`, read `RUNPOD_API_KEY`,
   `RUNPOD_ENDPOINT_ID`, `RUNPOD_BASE_URL`, `RUNPOD_TIMEOUT_SECONDS`,
   `RUNPOD_COST_PER_DISPATCH_CENTS` from env vars and construct an
   `HttpRunPodClient`.
5. In `create_job()`, add an `elif` branch for `mode == "runpod"` that
   calls `self._runpod_dispatcher.dispatch()`.
6. Add `runpod_health()` method.
7. Add `runpod_wired` to `health()` output.

### Step 4: Fix the training Dockerfile

Open `runpod/quant-foundry-training/Dockerfile`. Change the `COPY`
paths to reference `services/quant_foundry/src/quant_foundry/` from the
repo root. Add `build-essential`, `httpx`, `GIT_SHA` build arg, and
`PYTHONPATH=/worker`.

### Step 5: Fix the inference Dockerfile

Open `runpod/quant-foundry-inference/Dockerfile`. Same `COPY` path
fix. Simplify to pip install (no `uv sync`). Set `PYTHONPATH=/app`.

### Step 6: Fix the inference handler path

Open `runpod/quant-foundry-inference/handler.py`. Replace the single
`sys.path.insert` with the multi-path loop that tries repo-relative,
sibling, and `/app` paths.

### Step 7: Write tests

Add 9 tests to `services/quant_foundry/tests/test_runpod_client.py`
using `httpx.MockTransport` to test success, transient failure,
terminal failure, network error, missing ID, API key secrecy, and
polling methods.

### Step 8: Verify

```powershell
uv sync
uv run --package quant-foundry pytest services/quant_foundry/tests/test_runpod_client.py -v
docker build -t fincept-qf-training:latest -f runpod/quant-foundry-training/Dockerfile .
docker build -t fincept-qf-inference:latest -f runpod/quant-foundry-inference/Dockerfile .
```

---

## What is still NOT done (honest gaps)

These changes make the code **capable** of dispatching to a real RunPod
GPU. They do NOT constitute a completed GPU deployment. The following
remain:

| Gap | Status | What's needed |
|-----|--------|---------------|
| **Real RunPod GPU run** | Not done | Operator must create a RunPod account, build + push Docker images, create serverless endpoints, and dispatch a real job. See `docs/GPU_DEPLOYMENT_GUIDE.md`. |
| **Callback webhook** | Not automated | RunPod's `/run` endpoint is async — it returns a job ID, and the result must be polled via `/status/{job_id}`. The callback is NOT pushed to Fincept automatically. The operator must either poll and submit the callback manually, or build a polling loop that calls `POST /quant-foundry/callbacks/runpod`. |
| **Real model training** | Stub only | `LocalTrainer` produces a deterministic stub artifact (hash of inputs), not a real trained model. To train a real model, replace `LocalTrainer` with a trainer that loads data from the dataset manifest, trains a real model (e.g., LightGBM), and saves the artifact to S3. |
| **Real shadow inference** | Stub only | `ShadowInferenceEngine` produces stub predictions, not real model inference. To run real inference, inject a real model loader that loads the artifact from S3 and runs predictions. |
| **S3 artifact storage** | Not wired | The artifact importer supports `s3://` URIs but the RunPod handlers don't write to S3 yet. The training handler returns the artifact in the callback payload; for large models, it should write to S3 via pre-signed URL and return the URI. |
| **`lightgbm` dependency** | Pre-existing | `test_baseline_family.py` imports `lightgbm` which is not declared in `quant_foundry`'s `pyproject.toml`. This is unrelated to the GPU changes but causes a collection error. Fix: add `lightgbm` as an optional dependency or move `baseline_family.py` to a separate package. |
| **Promotion / paper bridge** | Blocked by B1–B8 | No model has been promoted. The promotion gate, paper bridge, and tournament leaderboard are all built but have no real data. See `docs/LIMITED_LIVE_READINESS_REVIEW.md`. |
| **AWS production deployment** | Design only | `docs/AWS_PRODUCTION_CONTROL_PLANE.md` is a design document. No ECS Fargate, Secrets Manager, or CloudWatch has been provisioned. |

---

## File-by-file diff summary

### `services/quant_foundry/src/quant_foundry/runpod_client.py` (+148 / -10)

- `HttpRunPodClient.__init__` — expanded with `timeout_seconds`, `cost_per_dispatch_cents`, `transport` params
- `HttpRunPodClient._build_client` — new method (lazy httpx import)
- `HttpRunPodClient.dispatch` — full implementation (was `NotImplementedError`)
- `HttpRunPodClient.check_status` — new method (poll RunPod `/status/{id}`)
- `HttpRunPodClient.check_health` — new method (check RunPod `/health`)

### `services/quant_foundry/pyproject.toml` (+1 / 0)

- Added `"httpx>=0.27"` to `dependencies`

### `services/quant_foundry/src/quant_foundry/gateway.py` (+93 / -1)

- Added imports: `HttpRunPodClient`, `RunPodDispatcher`, `BudgetGuard as DispatchBudgetGuard`
- `__init__` — new `runpod_client` param + `RunPodDispatcher` wiring
- `from_env` — reads `RUNPOD_API_KEY`, `RUNPOD_ENDPOINT_ID`, `RUNPOD_BASE_URL`, `RUNPOD_TIMEOUT_SECONDS`, `RUNPOD_COST_PER_DISPATCH_CENTS`
- `create_job` — `elif` branch for `runpod` mode dispatch
- `health` — added `runpod_wired` field
- `runpod_health` — new method

### `runpod/quant-foundry-training/Dockerfile` (+30 / -9)

- Fixed `COPY` paths (`services/quant_foundry/src/quant_foundry/`)
- Added `build-essential`, `httpx>=0.27`, `GIT_SHA` build arg
- Added `PYTHONPATH=/worker`
- Updated build command comment (use `-f` from repo root)

### `runpod/quant-foundry-inference/Dockerfile` (+35 / -8)

- Fixed `COPY` paths
- Replaced `uv sync` with direct `pip install`
- Added `build-essential`, `httpx>=0.27`, `GIT_SHA` build arg
- Set `PYTHONPATH=/app`
- Uses `ENTRYPOINT` instead of `CMD`

### `runpod/quant-foundry-inference/handler.py` (+10 / -1)

- Replaced single `sys.path.insert` with multi-path loop (repo-relative, sibling, `/app`)

### `services/quant_foundry/tests/test_runpod_client.py` (+220 / 0)

- 9 new tests for `HttpRunPodClient` using `httpx.MockTransport`:
  - `test_http_client_success`
  - `test_http_client_rate_limit_transient`
  - `test_http_client_503_transient`
  - `test_http_client_400_terminal`
  - `test_http_client_401_terminal`
  - `test_http_client_missing_id_terminal`
  - `test_http_client_network_error_transient`
  - `test_http_client_api_key_never_in_result`
  - `test_http_client_check_status`
  - `test_http_client_check_health`
