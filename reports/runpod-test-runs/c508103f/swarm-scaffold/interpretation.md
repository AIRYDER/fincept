# RunPod Training Worker Validation — c508103f

Last updated: 2026-07-03

## Identity

- Branch: `fix/test-harness-optional-deps-guards`
- SHA: `c508103fbac4b38b8f3c369f216f6e18177f72a4`
- Image: `ghcr.io/airyder/fincept/quant-foundry-training:c508103fbac4b38b8f3c369f216f6e18177f72a4`
- Workflow run id: `28669550090` (success, 10m13s)
- Endpoint id: `635ywogaldb3r2` (created fresh, registry auth copied from `z6xy0iflvxcjtr`)
- Endpoint status after test: scaled to `workersMin=0 workersMax=0`

## Decisions (operator-approved before live test)

- (a) Validate current HEAD as multi-variable acceptance (not single-variable Test A from failed control `412080c6`).
- (b) Trust the new root-cause finding (base image `python:3.12-slim` + `libgomp1`, not the healthcheck).
- (c) Fresh endpoint created with registry auth copied from source endpoint `z6xy0iflvxcjtr` (smoke).
- (d) No Dockerfile code change.

## LIVE PROBE RESULT — CANARY FAILED

**The current image `c508103f` fails live in the SAME pattern as the failed control `412080c6`.**

### Timeline

- `18:47:41Z` — endpoint `635ywogaldb3r2` created.
- `18:50:18Z` (approx) — worker reached `ready=1 idle=1 unhealthy=0` (healthy).
- `18:50:41Z` — canary job `eada80f8-...` submitted via `/run`. Status `IN_QUEUE`.
- `18:50:47Z` — worker went `unhealthy=1` (6 seconds after dispatch). `idle=0 ready=0`.
- `18:50:47Z` → `18:54:41Z` — job remained `IN_QUEUE`, worker remained `unhealthy=1` for the entire probe window.
- `18:54:46Z` — probe timed out (exit code 3). `last_status: IN_QUEUE`.
- `18:54:55Z` (approx) — job cancelled (`CANCELLED`).
- `18:55:10Z` (approx) — endpoint scaled to `workersMin=0 workersMax=0`.

### Evidence files in this directory

- `endpoint-create-redacted.txt` — endpoint creation transcript (redacted)
- `health-before.json` — `ready=1 idle=1 unhealthy=0` before dispatch
- `canary-probe.jsonl` — full JSONL probe output (run_response, status, health sequence, probe_timeout)
- `cancel.json` — job cancellation confirmation
- `final-status.json` — `CANCELLED`
- `health-after.json` — `unhealthy=1` after probe
- `cleanup.json` — endpoint scaled to `workersMin=0 workersMax=0`

### Local tests (passed — do NOT count as live proof)

- Production canary local: PASS (exit 0, preflight passed, callback envelope produced)
- Layered handler Layer 0: PASS
- Layered handler Layer 1: PASS
- Layered handler Layers 2-5: FAIL locally (`handler_full.py` missing — current Dockerfile does not copy layered mapping; this is expected and unrelated to the live failure)

## Root Cause Reconciliation

| Theory | Source | Live status |
|--------|--------|-------------|
| Docker healthcheck causes dispatch failure | Plan `00-system-context.md` leading theory | **DISPROVED** — current image has NO healthcheck and still fails the same way |
| Base image (`python:3.12-slim`) + missing `libgomp1` | Dockerfile comments, commits `40a35973`/`8c45c484` | **DISPROVED** — current image uses `python:3.12-slim` + `libgomp1` and still fails the same way |
| Production handler import/startup crashes worker at dispatch | Not yet tested | **NEW LEADING THEORY** — smoke worker (trivial handler) works live on the same base; training worker (production handler) fails live; local production canary passes; failure is in the container/serverless runtime interaction with the production handler's import/startup path |

### What this means

The failure is NOT the healthcheck and NOT the base image. The smoke worker (trivial handler, same registry, same endpoint shape) completes live jobs. The training worker (production handler with heavy imports: `quant_foundry`, `fincept_core`, `torch`, `xgboost`, `catboost`, `lightgbm`) goes unhealthy within 6 seconds of job dispatch. The worker reaches ready/idle before dispatch, so startup imports alone are not killing it — the failure triggers when RunPod delivers the job to the handler.

### Next test candidates (NOT run — awaiting operator direction)

1. **Test E (sentinel handler in training image):** Replace only `/worker/handler.py` with a trivial sentinel inside the same training image. If sentinel completes, the failure is isolated to the production handler's import/startup path. This is the plan's prescribed next step when Layer 0/canary fails with no healthcheck.
2. **Compare smoke vs training startup:** The smoke worker imports only `runpod`. The training worker imports `quant_foundry`, `torch`, `xgboost`, etc. A bisection of imports could identify which import crashes the worker at dispatch time.
3. **Pod logs:** RunPod may expose pod/container logs that show the actual crash. Check if the RunPod API or dashboard provides worker logs for the unhealthy pod.

## Acceptance Checklist (from `05-acceptance-criteria.md`)

### Required Final Success Criteria

- [ ] Smoke worker still completes a live RunPod job. — **NOT TESTED in this run** (previously proven; smoke endpoint `z6xy0iflvxcjtr` exists and is cold)
- [x] ~~Training layered endpoint Layer 0 completes live.~~ — **N/A**: current image uses production handler directly, no layered mapping. Canary probe used instead.
- [ ] ~~Layers 1 through 5 complete live~~ — **N/A**: same as above.
- [ ] **Full canary path completes live.** — **FAILED**: job stuck `IN_QUEUE`, worker `unhealthy=1`, probe timed out, job cancelled.
- [ ] Worker remains healthy after canary completion. — **FAILED**: worker went unhealthy at dispatch.
- [ ] Job reaches a terminal status instead of staying `IN_QUEUE`. — **FAILED**: job stayed `IN_QUEUE` until manually cancelled.
- [x] No debug endpoint is left with `workersMin=1`. — **DONE**: endpoint `635ywogaldb3r2` scaled to `workersMin=0 workersMax=0`.
- [x] No API keys or callback secrets are printed in receipts. — **DONE**: all receipts redacted.
- [x] Registry auth ids are redacted in shared summaries. — **DONE**.
- [x] Callback signatures are not printed except in local-only evidence. — **DONE**: local canary signature captured locally only, not in live receipts.
- [x] Build workflow produces an exact SHA-tagged image. — **DONE**: run `28669550090`, success.
- [x] Exact accepted image tag is recorded. — **DONE**: `ghcr.io/airyder/fincept/quant-foundry-training:c508103fbac4b38b8f3c369f216f6e18177f72a4`.
- [x] Existing inference endpoint remains untouched. — **DONE**: no inference endpoints modified.
- [x] Fincept / Quant Foundry product use cases remain unchanged. — **DONE**: no product changes.
- [x] RunPod training-worker architecture remains intact. — **DONE**: no architecture changes.
- [x] Callback-secret canary use case remains intact. — **DONE**: canary path exercised (failed at runtime, not removed).
- [x] No unrelated UI, app, product identity, or user journey changes. — **DONE**.

## Minimum Evidence Bundle

- [x] branch name — `fix/test-harness-optional-deps-guards`
- [x] accepted commit SHA — `c508103fbac4b38b8f3c369f216f6e18177f72a4` (NOT accepted — failed)
- [x] image tag — `ghcr.io/airyder/fincept/quant-foundry-training:c508103fbac4b38b8f3c369f216f6e18177f72a4`
- [x] GitHub Actions training build run id — `28669550090`
- [ ] smoke worker image tag and endpoint id — not re-tested this run
- [x] training endpoint id — `635ywogaldb3r2`
- [x] redacted endpoint settings — in `endpoint-create-redacted.txt`
- [x] `/health` before canary — `health-before.json` (`ready=1 idle=1 unhealthy=0`)
- [x] `/run` response for canary — in `canary-probe.jsonl` (job id `eada80f8-...`)
- [x] `/status` sequence for canary through terminal status — `canary-probe.jsonl` + `final-status.json` (`CANCELLED` after timeout)
- [x] `/health` after canary — `health-after.json` (`unhealthy=1`)
- [x] cleanup receipt showing debug endpoint scale-down — `cleanup.json`
- [x] short interpretation naming the proven root cause or the still-open failing boundary — this file

## Conclusion

**The image `c508103f` is NOT accepted.** The canary failed live with the same pattern as the failed control `412080c6`. Both the healthcheck theory and the base-image+libgomp1 theory are disproved by live evidence. The new leading theory is that the production handler's import/startup path crashes the worker when RunPod dispatches a job. The next prescribed step is Test E (sentinel handler in the training image) to isolate whether the failure is in the handler itself or in the SDK/image runtime.

**Awaiting operator direction before further live tests.**
