# RunPod Fix Plan: System Context

Last updated: 2026-07-03

This scaffold is for fixing the Quant Foundry RunPod training-worker instability without redesigning Fincept, Quant Foundry, the app surface, or the training workflow.

## What This System Is

Fincept / Quant Foundry has a dispatch -> train -> callback -> verify -> score loop.

- Fincept owns job creation, budget checks, callback verification, model dossiers, tournament scoring, and promotion controls.
- RunPod owns the untrusted GPU container that receives a job, executes `handler(event)`, and returns a result.
- The RunPod training worker must remain a pure training worker. It must not receive broker credentials, Redis access, trading keys, or live signal-writing authority.
- Signed callbacks remain part of the design. The canary/callback-secret validation path must stay available.
- Existing inference worker behavior and unrelated product surfaces must remain untouched.

Important files:

- Production training worker directory: `runpod/quant-foundry-training/`
- Production handler: `runpod/quant-foundry-training/handler.py`
- Diagnostic handler: `runpod/quant-foundry-training/handler_diagnostic.py`
- Layered diagnostic handler: `runpod/quant-foundry-training/handler_layered.py`
- Training Dockerfile: `runpod/quant-foundry-training/Dockerfile`
- Minimal smoke worker directory: `runpod/quant-foundry-smoke/`
- Smoke handler: `runpod/quant-foundry-smoke/handler.py`
- Smoke Dockerfile: `runpod/quant-foundry-smoke/Dockerfile`
- Shared worker status helper: `runpod/shared/worker_status.py`
- Endpoint creation helper: `scripts/runpod_create_smoke_endpoint.py`
- Live probe helper: `scripts/runpod_smoke_probe.py`
- Worker recycle helper: `scripts/recycle_runpod_workers.py`
- Local handler test harness: `scripts/runpod_training_handler_local_test.py`
- Training image workflow: `.github/workflows/build-runpod-training.yml`
- Smoke image workflow: `.github/workflows/build-runpod-smoke.yml`

## What The Smoke Worker Is

The smoke worker is the smallest RunPod proof path. It imports only the RunPod SDK and returns a tiny response.

The smoke worker proves the infrastructure path, not the training product path:

- RunPod API authentication works.
- GHCR image pull works.
- Endpoint creation can work when the template shape is correct.
- `/health`, `/run`, and `/status` can work.
- `runpod==1.7.13` can process a trivial job.
- RunPod serverless is not globally broken.

Do not replace the training worker with the smoke worker. The smoke worker is a baseline and diagnostic control.

## What The Training Worker Must Do

The production training worker must:

1. Accept a RunPod serverless job through `handler(event)`.
2. Parse the training/canary request.
3. Run security preflight before side effects.
4. Execute canary, diagnostic, or later real training logic.
5. Return a JSON-serializable result.
6. For canary/training paths, return a signed callback envelope or safe terminal failure.
7. Stay healthy after the job instead of leaving the job stuck in `IN_QUEUE`.

The design constraints are unchanged:

- No Fincept product redesign.
- No workflow or user-journey redesign.
- No removal of RunPod training architecture.
- No removal of callback-secret canary.
- No simplification into smoke-only behavior.
- No unrelated UI, app, inference, or product identity changes.

## Current Bug Shape

Known failed live layered test:

- Failed image SHA: `412080c61a38cd138a92d43df8242503d27f71d2`
- Failed image: `ghcr.io/airyder/fincept/quant-foundry-training:412080c61a38cd138a92d43df8242503d27f71d2`
- Failed endpoint: `zbpy7m8s8dps7k`
- Failed payload:

```json
{
  "input": {
    "task": "callback_secret_canary",
    "job_id": "qf:diag-layer0:412080c6:001",
    "nonce": "layer0-nonce",
    "diag_layer": 0
  }
}
```

Layer 0 should return immediately from the handler wrapper:

- No preflight.
- No canary signing.
- No training validation.
- No model logic.
- No real training.

Observed live behavior:

- Worker reached `ready=1 idle=1`.
- Job stayed `IN_QUEUE`.
- Worker later moved toward `running=1`.
- Pod exited or became unhealthy around dispatch time.
- RunPod reported the pod as exited while the job never reached a terminal status.

Layer 0 failing is the key fact. Treat the first failure as occurring before meaningful handler body execution until a receipt proves otherwise.

## What Has Already Been Proven

Proven live:

- The pure smoke worker can complete a RunPod job.
- The RunPod API key works.
- GHCR registry auth works.
- Endpoint creation can work.
- Image pull can work.
- `/health`, `/run`, and `/status` can work.
- Basic RunPod serverless SDK behavior can work.

Proven by diagnostic training endpoint:

- A training image can boot.
- The full Quant Foundry import tree can load.
- Startup imports alone are not enough to kill the worker.
- The diagnostic endpoint reached `ready=1 idle=1 unhealthy=0 throttled=0`.

Proven locally:

- `handler_diagnostic.py` default `return_only` mode returns.
- `handler_diagnostic.py` full mode returns.
- Real `handler.py` canary path returns.
- `handler_layered.py` layers 0 through 5 return.
- The local harness must register imported modules in `sys.modules` before `exec_module`; the current local harness does this.

Proven by the failed layered live test:

- Local success does not prove RunPod serverless runtime success.
- The first live failure is likely in container lifecycle, healthcheck overlap, SDK job loop, process exit behavior, or RunPod scheduling around dispatch.

## Proven Facts vs Assumptions

| Type | Statement | Receipt required before relying on it |
| --- | --- | --- |
| Fact | Smoke worker completed live after endpoint template shape was fixed. | Smoke endpoint id, image tag, `/run` response, terminal `/status`. |
| Fact | Diagnostic training endpoint reached ready/idle with no unhealthy workers. | `/health` JSON with endpoint id and image tag. |
| Fact | Local diagnostic, real canary, and layered paths returned. | Local command transcript with exit code 0 and JSON result. |
| Fact | Layer 0 failed live on SHA `412080c6`. | Probe transcript showing `/run`, repeated `/status`, `/health`, and final timeout or pod exit. |
| Assumption | The Docker healthcheck caused the live Layer 0 failure. | Test A must prove this by removing only the healthcheck from the layered image. |
| Assumption | Preflight, canary signing, model validation, and training logic are not the first failure. | Layer 0 live behavior supports this, but a no-healthcheck Layer 0 test must confirm. |
| Assumption | SDK/base/entrypoint churn is unnecessary. | Only true if Test A passes while those variables stay fixed. |

## Current Leading Theory

The leading theory is a Docker healthcheck/container lifecycle interaction.

The known failed layered Dockerfile at commit `412080c61a38cd138a92d43df8242503d27f71d2` used:

- `handler_layered.py` copied to `/worker/handler.py`
- production `handler.py` copied to `/worker/handler_full.py`
- `runpod==1.7.13`
- Docker `HEALTHCHECK` that executed:

```text
python -c "import sys; sys.path.insert(0, '/worker'); import handler; print('healthcheck ok')"
```

Because `/worker/handler.py` was the layered wrapper, healthcheck import behavior can still involve diagnostic startup behavior and can compete with the RunPod SDK worker process. Earlier versions also involved heavy production imports during healthcheck or startup. The suspected failure mode is:

1. Worker starts and appears healthy.
2. Job dispatch starts.
3. Docker healthcheck or RunPod lifecycle polling overlaps with job handling.
4. A second Python process imports enough of the worker tree to cause delay, memory pressure, timeout, or lifecycle confusion.
5. RunPod marks the pod unhealthy or exits it.
6. The job remains `IN_QUEUE`.

This is plausible, not proven.

## Important Current-Repo Reconciliation

At the time this plan was scaffolded, local HEAD was:

```text
c508103fbac4b38b8f3c369f216f6e18177f72a4
```

The current local `runpod/quant-foundry-training/Dockerfile` already contained a "NO HEALTHCHECK" section and copied the production handler directly to `/worker/handler.py`. That is not the same shape as the known failed layered image at `412080c6`.

Swarm agents must not blindly edit the current Dockerfile. First reconcile the target branch/SHA:

```powershell
git status --short --branch
git rev-parse HEAD
git show 412080c61a38cd138a92d43df8242503d27f71d2:runpod/quant-foundry-training/Dockerfile |
  Select-String -Pattern "HEALTHCHECK|handler_layered|handler_full|ENTRYPOINT"
Select-String -Path runpod/quant-foundry-training/Dockerfile -Pattern "HEALTHCHECK|handler_layered|handler_full|ENTRYPOINT"
```

If the target branch already has no Docker healthcheck, do not re-apply the change. Treat Test A as a validation task for the exact SHA image that contains no healthcheck.

If the target branch is the known failed layered build or a branch derived from it, the first code change should be healthcheck-only.

## What Not To Re-Debug Blindly

Do not restart broad experiments unless a receipt disproves the current diagnosis.

Previously tested or mixed:

- `runpod==1.7.10`
- `runpod==1.7.13`
- `runpod>=1.10.0`
- `pytorch/pytorch`
- `runpod/pytorch`
- `python:3.12-slim`
- handler import healthcheck
- `pgrep` healthcheck
- removed healthcheck in older mixed tests
- bash wrapper entrypoint
- direct Python entrypoint
- stale endpoint cleanup

The next useful work is clean, exact-SHA, single-variable testing.

## Evidence Standard

Every later agent must leave receipts. A valid receipt includes:

- repo branch and exact commit SHA
- image tag
- workflow run id and result
- endpoint id
- redacted endpoint settings
- `/health` before dispatch
- `/run` response with job id
- `/status/{job_id}` sequence until terminal state or timeout
- `/health` sequence during and after the job
- final interpretation
- cleanup confirmation showing debug endpoints scaled down

Never print:

- `RUNPOD_API_KEY`
- `QUANT_FOUNDRY_CALLBACK_SECRET`
- registry auth ids in clear text
- callback signatures unless the test is explicitly local-only and the signature is necessary
