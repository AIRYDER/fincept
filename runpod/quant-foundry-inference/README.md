# Quant Foundry Shadow Inference Container (TASK-0601)

RunPod container for shadow-only model inference. This container runs
candidate model predictions and returns shadow-only prediction batches
that are settled against realized outcomes but **never reach `sig.predict`**.

## Key Invariants

- **Shadow-only authority.** All predictions have `authority: shadow_only`.
  No order/trading fields are ever produced.
- **Disabled by default.** Inference is disabled unless
  `QUANT_FOUNDRY_MODE=runpod_shadow`. When disabled, the engine raises
  `InferenceDisabledError` (fail-safe — no predictions produced).
- **Fails safely on invalid input.** Missing symbols, empty snapshots, and
  low feature availability produce abstaining predictions, not crashes.
- **Latency + feature availability.** Each prediction includes `latency_ms`
  and `feature_availability` for diagnostics.

## Usage

### Local testing

```bash
echo '{"input": {"request": {"job_id": "job-1", "artifact_ref": "file:///model.pkl", "symbols": ["AAPL"], "horizons_ns": [3600000000000]}, "snapshot": {"symbols": ["AAPL"], "features": {"AAPL": [0.1, 0.2]}, "availability": {"AAPL": true}, "ts_event": 1000, "freshness_ns": 500}, "model_id": "m1"}}' | python handler.py
```

### RunPod deployment

Build the Docker image and deploy to RunPod. The container reads JSON from
stdin (or the RunPod event) and returns the callback envelope + predictions.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `QUANT_FOUNDRY_MODE` | — | Must be `runpod_shadow` to enable inference. |
| `QUANT_FOUNDRY_USE_REAL_INFERENCE` | `false` | Set to `true` to use `RealInferenceEngine` (loads ONNX/LightGBM model artifacts). `false` uses `ShadowInferenceEngine` (stub). |
| `QUANT_FOUNDRY_CALLBACK_SECRET` | — | HMAC secret for signing callbacks. |
| `PYTHONPATH` | `/app` | Path to the quant_foundry package. |

## Architecture

```
RunPodInferenceRequest → handler.py → ShadowInferenceEngine → ShadowPrediction batch
                                      ↓
                              RunPodCallbackEnvelope (signed callback to Fincept)
```

The handler is a thin wrapper around `quant_foundry.shadow_inference`.
The actual inference logic (model loading, scoring) lives in the
`ShadowInferenceEngine` class, which can be injected with a real model
loader for production use.

## File Ownership

- `runpod/quant-foundry-inference/handler.py` — RunPod handler entry point.
- `runpod/quant-foundry-inference/Dockerfile` — Docker build config.
- `runpod/quant-foundry-inference/README.md` — this file.
- `services/quant_foundry/src/quant_foundry/shadow_inference.py` — inference engine.
- `services/quant_foundry/tests/test_shadow_inference.py` — tests.

File-disjoint from `runpod/quant-foundry-training/` (Builder 2's training
container — different subdirectory).
