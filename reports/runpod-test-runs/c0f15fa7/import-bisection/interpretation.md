# Import Bisection Test F — Interpretation

**Image SHA:** c0f15fa7be38460c6c1930ef5394caf152615199
**Image tag:** ghcr.io/airyder/fincept/quant-foundry-training:c0f15fa7be38460c6c1930ef5394caf152615199
**Template ID:** qxqqh6wals

> **CORRECTED by hourly receipt consolidation pass (2026-07-03).**
> The original interpretation claimed "lightgbm poisons the worker." The raw
> probe evidence contradicts this. See the correction notes at the bottom.

## Results

| Profile | Result | Failure Reason | Endpoint | Job ID | Final Status |
|---------|--------|----------------|----------|--------|--------------|
| sentinel | pass | None | 9uasiygmknikn7 | 65950bb9-30ee-4dab-a520-9c2948011b35-u1 | COMPLETED |
| pandas_numpy | pass | None | yxw1bt6w2shg07 | 4dd03395-b14e-4353-8adc-5a0b4606d24e-u2 | COMPLETED |
| xgboost | pass | None | oklx91df3kh8eg | 67fabf62-d47a-4464-ab39-3b393a957f15-u1 | COMPLETED |
| catboost | pass | None | b0hiaap9tdfetg | 40153391-9b4b-4da0-87d3-9bbadee8bce0-u1 | COMPLETED |
| lightgbm | inconclusive (false negative) | probe bug — see notes | ccdkamkwkpb19c | 5d2baa2f-3f11-452c-a4fa-c20dbca50912-u2 | IN_QUEUE |
| torch | pass | None | osoafmz74bayp8 | 04a93ae0-c7d8-43d3-b8c4-9e053d8b4c24-u1 | COMPLETED |
| signatures_schemas | pass | None | b2ut0grt2img9n | 7c5a3aa1-e1f0-415f-8c11-25f03da44eeb-u2 | COMPLETED |
| runpod_training | pass | None | yyx35wipyp0pf5 | 3f8f418a-be61-4883-b15f-a78390d1a115-u1 | COMPLETED |
| quality_report | pass | None | 4yyzv7f5zpwe7x | 4aca5854-1b7a-4191-9bdb-566416506a0f-u2 | COMPLETED |
| dataset_manifest | pass | None | 5b8mfw104jgwid | 3afa8f0d-5d3c-4ebd-8060-3323f0735558-u2 | COMPLETED |
| full_handler_import | pass | None | enpgwuvvhnl1d4 | 317f0615-766a-4021-934f-1842dd225502-u1 | COMPLETED |
| full_handler_call | pass | None | l4g3f0egagavmc | 11f344ec-041d-4c6a-9434-aba00601a649-u1 | COMPLETED |

## Summary

- **First failing profile:** none (lightgbm was a false negative — see below)
- **Last passing profile:** full_handler_call
- **Profiles tested:** 12

## Key Findings

### 1. lightgbm "failure" is a FALSE NEGATIVE caused by a probe script bug

The probe script (`run_import_bisection.py` line 478) declares failure when
`job_status == "IN_QUEUE" and workers.get("ready", 0) == 0`. But `ready=0`
also occurs when the worker transitions from idle to **running** (it picks up
the job). The raw evidence proves the lightgbm worker was alive and processing:

- `health-after-lightgbm.json`: `running=1, unhealthy=0` (alive, processing)
- `cleanup-lightgbm.json`: `running=1, unhealthy=0` (still alive after scale-down)
- `probe-lightgbm.jsonl` last poll (20:23:02): `running=1, unhealthy=0`

The worker was NOT unhealthy at any point. The probe stopped after ~21 seconds
(before the job completed) because of the buggy `ready=0` check. Compare with
the REAL failure in `c508103f` where the worker went `unhealthy=1, running=0`
and stayed dead for 2+ minutes.

### 2. full_handler_call PASSED — production handler works live via bisect wrapper

The `full_handler_call` profile imports `handler_full` (the production handler)
at module top and calls `handler_full.handler(event)` at dispatch time. The job
COMPLETED in ~5 seconds (dispatched 20:27:15, completed 20:27:20). The worker
stayed healthy throughout.

This means the production handler's canary path works live in the c0f15fa7
image shape (python:3.12-slim + libgomp1 + runpod==1.7.13).

### 3. This contradicts the c508103f failure

In c508103f, the production handler was the direct RunPod entrypoint
(`/worker/handler.py` IS the production handler). The worker went
`unhealthy=1` 6 seconds after dispatch. In c0f15fa7 full_handler_call, the
production handler is called via the bisect wrapper and PASSES.

The only structural difference is which `handler` function is passed to
`runpod.serverless.start()`:
- c508103f: `start({"handler": production_handler})`
- c0f15fa7: `start({"handler": bisect_trivial_handler})` → at dispatch, bisect
  calls `handler_full.handler(event)`

Possible explanations (not yet tested):
1. The c508103f failure was a transient RunPod platform fluke (single data point).
2. The production handler's `__main__` block runs a startup preflight that the
   bisect handler skips — but preflight runs at boot, not dispatch, and the
   worker was ready=1 before dispatch in c508103f.
3. The RunPod SDK inspects or wraps the handler function in a way that crashes
   on the production handler's `handler` but not on the bisect's trivial one.

## Next Steps

The **full_handler_call PASS** is the most important new finding. The
production handler code works live. The next step is to retest the production
handler as the **direct RunPod entrypoint** (not via the bisect wrapper) to
determine whether the c508103f failure was a transient fluke or a real
entrypoint-mode crash.

1. Restore the Dockerfile `COPY` line to use `handler.py` as `/worker/handler.py`
   (revert the bisection COPY).
2. Build/push a fresh image with the SAME base/SDK/deps as c0f15fa7.
3. Create a fresh endpoint (same shape: ADA_24, QUEUE_DELAY, registry auth,
   idleTimeout=300, scalerValue=4, containerDiskInGb=20, dockerArgs="").
4. Dispatch a canary job.
5. If it COMPLETED → the c508103f failure was a transient fluke and the
   production handler is fixed. Update the index and close the investigation.
6. If it FAILS (unhealthy=1) → the crash is specific to entrypoint mode (the
   production handler's `__main__` block or how the SDK wraps its `handler`
   function). Compare the `__main__` block's preflight and startup logging
   with the bisect handler's `main()` to find the difference.

Do NOT pursue the "lightgbm poisons the worker" hypothesis — it was disproven
by the raw evidence. Do NOT retry individual import bisection profiles —
full_handler_call already proved the full import tree + handler call works.

## Correction Notes (2026-07-03 hourly consolidation)

The original `interpretation.md` and `summary.json` claimed:
- `first_failing_profile: "lightgbm"`
- `"lightgbm poisons the worker at dispatch time"`

These claims were **false**. The raw probe evidence
(`probe-lightgbm.jsonl`, `health-after-lightgbm.json`, `cleanup-lightgbm.json`)
shows the worker was `running=1, unhealthy=0` (alive, processing the job) when
the probe declared failure. The false negative was caused by a bug in
`run_import_bisection.py` line 478 where `ready=0` is treated as "worker died"
but `ready=0` also occurs when the worker transitions to `running=1`.

No raw evidence files were modified. Only `summary.json` and
`interpretation.md` were corrected to match the authoritative raw evidence.
