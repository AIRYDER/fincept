# RunPod Serverless Unhealthy Workers — Root Cause & Fix

## Date
2026-07-03

## Summary
RunPod serverless workers using `pytorch/pytorch:2.4.1-cuda12.4-cudnn9-runtime`
as the base image were going unhealthy within 90 seconds of initialization on
ADA_24 GPUs. The root cause was a CUDA base image incompatibility with RunPod's
serverless runtime. Switching to `nvidia/cuda:12.4.1-runtime-ubuntu22.04`
resolved the issue completely.

## Symptoms
- Workers enter `initializing` state, then go `unhealthy` within 60-90 seconds
- No jobs can be processed (jobs stay `IN_QUEUE` indefinitely)
- The non-CUDA smoke image (`python:3.12-slim`) works fine
- Extending `RUNPOD_INIT_TIMEOUT` to 900s helped the smoke image but not CUDA images
- Disk size (10GB, 20GB, 40GB, 50GB) made no difference

## Root Cause
The `pytorch/pytorch` and `runpod/base` CUDA base images have entrypoint/driver
compatibility issues on RunPod serverless ADA_24 GPUs. ADA_24 GPUs run driver
550, which supports CUDA up to 12.4. Key findings:

1. **`pytorch/pytorch:2.4.1-cuda12.4-cudnn9-runtime`**: Despite using CUDA 12.4
   (which should match driver 550), the image causes workers to go unhealthy
   within 90 seconds. The exact mechanism is unclear but likely related to the
   NVIDIA entrypoint hook or conda environment initialization conflicting with
   RunPod's serverless worker lifecycle.

2. **`runpod/base:1.0.2-cuda1281-ubuntu2204`**: Uses CUDA 12.8 which exceeds
   driver 550's maximum (CUDA 12.4). The `nvidia_entrypoint.sh` hook kills the
   container with: `unsatisfied condition: cuda>=12.8, please update your
   driver to a newer version`.

3. **`nvidia/cuda:12.4.1-runtime-ubuntu22.04`**: Works correctly. The CUDA 12.4
   version matches driver 550. Our Dockerfile overrides the default
   `nvidia_entrypoint.sh` with `ENTRYPOINT ["python", "-u", "/worker/handler.py"]`,
   bypassing any driver version checks.

Reference: [RunPod/containers commit a97beae](https://github.com/runpod/containers/commit/a97beaec8bff74695c1261f2817de7717aaa7ab6)
— RunPod themselves fixed this by overriding the NVIDIA entrypoint in their
autoresearch template.

## Fix
Changed base images in both Dockerfiles:

### CUDA Test (`runpod/quant-foundry-cuda-test/Dockerfile`)
```dockerfile
# Before (broken):
FROM pytorch/pytorch:2.4.1-cuda12.4-cudnn9-runtime

# After (working):
FROM nvidia/cuda:12.4.1-runtime-ubuntu22.04
```

### Training (`runpod/quant-foundry-training/Dockerfile`)
```dockerfile
# Before (broken):
FROM pytorch/pytorch:2.4.1-cuda12.4-cudnn9-runtime

# After (working):
FROM nvidia/cuda:12.4.1-runtime-ubuntu22.04
```

Additional changes for the training Dockerfile:
- Install Python 3 explicitly via apt (nvidia/cuda doesn't include Python)
- Add `DEBIAN_FRONTEND=noninteractive` for non-interactive apt

## Verification
1. **CUDA test endpoint** (`k7wshfn9y6jab3`): Became healthy in ~10 minutes
   with `nvidia/cuda` base image (sha b6b0c5e2)
2. **Training endpoint** (`gt9r90hxsip48l`): Became healthy in ~90 seconds
   with `nvidia/cuda` base image (sha ad24f100)
3. **Test job**: Submitted `gpu_healthcheck` canary job → `COMPLETED` in ~15
   seconds with `ok: true` output

## Images Tested
| Base Image | CUDA Version | Result |
|---|---|---|
| `python:3.12-slim` | None | ✅ Healthy (smoke test) |
| `pytorch/pytorch:2.4.1-cuda12.4-cudnn9-runtime` | 12.4 | ❌ Unhealthy in 90s |
| `runpod/base:1.0.2-cuda1281-ubuntu2204` | 12.8 | ❌ Unhealthy in 90s |
| `nvidia/cuda:12.4.1-runtime-ubuntu22.04` | 12.4 | ✅ Healthy in ~10min |

## Endpoints
| Endpoint ID | Name | Status |
|---|---|---|
| `bo366l5j00ciin` | smoke-ada24 | ✅ Healthy (python:3.12-slim) |
| `k7wshfn9y6jab3` | cuda-test-10gb | ✅ Healthy (nvidia/cuda) |
| `gt9r90hxsip48l` | training-lazy | ✅ Healthy (nvidia/cuda) |
| `mxp0bv8itggwev` | training (old) | Deprecated (pytorch base) |

## Key Commits
- `b6b0c5e2`: Switch cuda-test Dockerfile to nvidia/cuda base
- `ad24f100`: Switch training Dockerfile to nvidia/cuda base
