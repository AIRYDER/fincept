# RunPod Quant Foundry — Compacted Session Context

> Source: Codex session `019f000e-c6b8-7131-9c87-a85173da36d9` (2026-06-25, 18:33–20:04 CT)
> Branch: `codex/portfolio-optimizer-core` | Repo: `fincept-terminal`

## Session arc (4 turns)

1. **Handoff intake** — User pasted the prior RunPod-setup session's last state. Codex confirmed the deployed endpoint IDs and `dockerEntrypoint: []` fix already live in `docs/GPU_DEPLOYMENT_GUIDE.md`.
2. **Readiness question** — "what else do we need to do before this system is producing results?" → Codex returned a 7-point gap list (below).
3. **Implementation** — User said "ok do the following" with the full 7-point list. Codex implemented all of it over ~1h.
4. **Test-env fix** — "how do we fix it" (the broken local test run). Root cause was system Python 3.10 vs required `>=3.12`; fixed via `uv sync --package api --dev`. Focused tests then passed.

## Deployed RunPod state (live, prior-session evidence — not re-verified this session)

| Endpoint | ID | Template ID | Network Volume |
|---|---|---|---|
| Training | `8vol1uc9l75jgs` | `me58r5vdrp` | `rrsd005i3g` (10GB, US-NC-1) |
| Inference | `36mz2q30jdyvru` | `wnasp3v5jn` | `rrsd005i3g` (10GB, US-NC-1) |

- Base image: `runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04`
- Serverless mount: `/runpod-volume` (pods: `/workspace`)
- Code on volume: `/runpod-volume/fincept-terminal/` (git clone of `codex/portfolio-optimizer-core`)
- Python libs on volume: `/runpod-volume/python-libs/` (pydantic, httpx, runpod SDK)
- Start scripts: `/runpod-volume/start-training.sh`, `/runpod-volume/start-inference.sh`
- **Critical fix:** templates need `dockerEntrypoint: []` or the `runpod/pytorch` ENTRYPOINT (nginx/ssh) makes workers go `unhealthy`.
- **Handler fix (from prior session):** `runpod.serverless.start({"handler": handler})` must be called at **module level**, not inside `if __name__ == "__main__"`.

## What was implemented in turn 3

- API startup now attaches `app.state.quant_foundry_gateway` + starts a cancellable RunPod poller in enabled RunPod modes.
- Gateway routes `training` and `inference` jobs to separate endpoint clients/env vars (`RUNPOD_TRAINING_ENDPOINT_ID`, `RUNPOD_INFERENCE_ENDPOINT_ID`).
- RunPod polling ingests `callback_payload`, `callback_signature`, `callback_ts` into the existing callback endpoint path.
- Callback effects now persist to durable `DossierRegistry` and `ShadowLedger` (was `DossierStub`/`ShadowLedgerStub`).
- Inference dispatch wraps requests as `{"request", "snapshot", "model_id"}` using feature snapshot export from `feature_rows`.
- Inference RunPod handler now returns signed callback fields like training.
- Dashboard RunPod labels now reflect real route state instead of "not wired."
- Guide documents the new env vars, polling, and snapshot payloads.

## Validation status (end of session)

| Check | Result |
|---|---|
| `services/quant_foundry/tests/test_gateway_runpod_loop.py` | 4 passed |
| `services/api/tests/test_quant_foundry_startup.py` | 1 passed |
| Manual gateway loop (training callback → dossier count 1; inference callback → shadow prediction count 1) | passed |
| Dashboard `tsc --noEmit` | passed |
| `py_compile` on touched backend files | passed |
| `git diff --check` | passed (only existing CRLF warnings) |

Test-env fix command (in case venv desyncs again):
```powershell
$env:UV_CACHE_DIR=(Resolve-Path '.').Path + '\.uv-cache'
uv sync --package api --dev
```

## Live proof status

**Full end-to-end loop: PROVEN LIVE** (2026-06-25, commits `f3bc3d0` + `3f29bbb`)

### Inference (completed in ~5s)
```
Dispatch inference job (qf:infer:live:b4e40df8)
  → RunPod endpoint 36mz2q30jdyvru (24GB Pro GPU, ready worker)
  → Poller detects completion (poll 1, ~5s)
  → Backward-compat callback signing (old handler format)
  → Callback ingested: inbox_status=processed, outbox_status=completed
  → Shadow prediction stored in durable ShadowLedger
```

### Training (completed in ~75s, queued waiting for GPU)
```
Dispatch training job (qf:train:live:85bcc44e)
  → RunPod endpoint 8vol1uc9l75jgs (throttled, queued ~70s)
  → Worker picked up job after GPU freed
  → Poller detects completion (poll 15, ~75s)
  → Backward-compat callback signing
  → Callback ingested: inbox_status=processed, outbox_status=completed
  → Dossier stored in durable DossierRegistry
```

### Final state
- `prediction_count=2`, `feature_availability=1.0`, `latency_p50=0.12ms`
- `dossier_count=1` (model:qf:train:live:85bcc44e, artifact:4ac7e423edbd4cf7)
- All 5 jobs in outbox: 3 completed, 2 failed (earlier runs before compat fix)

**Deployed handler update needed (low priority):**
The RunPod volume has old handler code (returns unsigned `callback` dict). The backward-compat shim handles this. Once the volume code is updated (git pull on `/runpod-volume/fincept-terminal/`), the normal signed-callback path takes over.

## Key files

- `docs/GPU_DEPLOYMENT_GUIDE.md` — deployed IDs, env vars, deployment steps
- `services/api/src/api/main.py:127` — Quant Foundry route registration + startup wiring
- `services/quant_foundry/src/quant_foundry/gateway.py:83` — gateway / dispatcher
- `services/quant_foundry/src/quant_foundry/callbacks.py:213` — callback ingestion → durable stores
- `runpod/quant-foundry-training/{Dockerfile,handler.py,README.md}` — training worker
- `runpod/quant-foundry-inference/{Dockerfile,handler.py,README.md}` — inference worker
- `apps/dashboard/src/app/quant-foundry/page.tsx:212` — dashboard RunPod truth labels
- `AAAAAAAAA_BIG_PLAN.md` — master implementation order (RunPod is one slice; do not start it independently of safety/evidence foundations)

## Security invariants (non-negotiable, from BIG_PLAN)

- No RunPod worker gets broker credentials.
- No RunPod worker writes to `ord.orders`, `ord.decisions`, `ord.fills`, `ord.positions`, or `sig.predict`.
- All callback envelopes are HMAC-signed (`QUANT_FOUNDRY_CALLBACK_SECRET`); dispatcher verifies signature, fail-closed.
- `ModelDossier` always carries `authority=SHADOW_ONLY`; promotion to live is human-gated (TASK-0702).
