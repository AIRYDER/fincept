# RunPod Serverless Unhealthy Workers — Root Cause & Fix

## Date
2026-07-03

## Summary
RunPod serverless workers using CUDA base images (`pytorch/pytorch`,
`nvidia/cuda`, `runpod/base`) all fail on RunPod serverless ADA_24 GPUs.
The `pytorch/pytorch` and `runpod/base` images cause workers to go
unhealthy within 90 seconds. The `nvidia/cuda` image allows workers to
become "ready" but the runpod SDK never picks up jobs (jobs stay
`IN_QUEUE` forever while the worker shows `ready=1, idle=1`).

The fix is to use `python:3.12-slim` as the base image and install
PyTorch with CUDA 12.4 runtime libraries via pip from
`download.pytorch.org/whl/cu124`. The torch wheel includes the CUDA
runtime (~2GB) so no CUDA base image is needed.

## Symptoms
Two distinct failure modes were observed:

### Failure Mode 1: Unhealthy (pytorch/pytorch, runpod/base)
- Workers enter `initializing` state, then go `unhealthy` within 60-90 seconds
- No jobs can be processed
- Extending `RUNPOD_INIT_TIMEOUT` to 900s did not help
- Disk size (10GB, 20GB, 40GB, 50GB) made no difference

### Failure Mode 2: Ready but no job pickup (nvidia/cuda)
- Workers become `ready=1, idle=1` successfully
- Jobs are submitted and stay `IN_QUEUE` indefinitely
- The worker never transitions to `running` state
- The runpod SDK's HTTP webhook for job dispatch is not functioning
- This is the most deceptive failure mode — the worker looks healthy but
  silently never processes any work

### Working (python:3.12-slim)
- Workers become `ready=1, idle=1` in ~90 seconds (smoke) or ~14 minutes
  (slim+CUDA with PyTorch)
- Jobs are picked up and completed in ~15 seconds
- All operations work as expected

## Root Cause
All CUDA base images tested break the runpod SDK's job dispatch mechanism
on RunPod serverless ADA_24 GPUs (driver 550, CUDA 12.4 max). The exact
mechanism differs by image:

1. **`pytorch/pytorch:2.4.1-cuda12.4-cudnn9-runtime`**: Workers go
   unhealthy within 90 seconds. Likely related to the NVIDIA entrypoint
   hook or conda environment initialization conflicting with RunPod's
   serverless worker lifecycle.

2. **`runpod/base:1.0.2-cuda1281-ubuntu2204`**: Uses CUDA 12.8 which
   exceeds driver 550's maximum (CUDA 12.4). The `nvidia_entrypoint.sh`
   hook kills the container.

3. **`nvidia/cuda:12.4.1-runtime-ubuntu22.04`**: Workers become ready
   but the runpod SDK never picks up jobs. Despite overriding the
   `nvidia_entrypoint.sh` with a direct Python entrypoint, something
   in the nvidia/cuda base image prevents the SDK's HTTP webhook from
   receiving job dispatches from RunPod's platform.

4. **`python:3.12-slim` + pip PyTorch CUDA**: Works perfectly. The
   runpod SDK's job dispatch mechanism functions correctly. PyTorch
   CUDA wheels from `download.pytorch.org/whl/cu124` include the CUDA
   runtime libraries, so GPU operations work without a CUDA base image.

## Fix
Changed the training Dockerfile to use `python:3.12-slim` as the base
image and install PyTorch with CUDA 12.4 via pip:

### Training (`runpod/quant-foundry-training/Dockerfile`)
```dockerfile
# Before (broken — ready but no job pickup):
FROM nvidia/cuda:12.4.1-runtime-ubuntu22.04

# After (working — jobs picked up and completed):
FROM python:3.12-slim

# Install PyTorch with CUDA 12.4 runtime from pytorch.org
RUN pip install --no-cache-dir "torch==2.4.1" \
    --index-url https://download.pytorch.org/whl/cu124
```

Additional changes:
- Removed layered handler wrapper (`handler_layered.py`), use `handler.py` directly
- Removed `entrypoint.sh` wrapper, use direct `ENTRYPOINT ["python", "-u", "/worker/handler.py"]`
- Removed Python 3.10 compat shims (python:3.12 has `StrEnum`, `UTC`, `Self` natively)
- Extracted `preflight.py` as a standalone file, run from handler `__main__` block

## Verification
1. **Smoke endpoint** (`z6xy0iflvxcjtr`, python:3.12-slim): Job submitted →
   `COMPLETED` in ~15 seconds with `ok: true` output
2. **Slim+CUDA test endpoint** (`53ssdfywfn9gg8`, python:3.12-slim + pip torch):
   Worker ready in ~14 minutes, job submitted → `COMPLETED` in ~15 seconds
   with `ok: true` output
3. **Training endpoint**: Pending new image build verification

## Images Tested
| Base Image | CUDA Version | Result |
|---|---|---|
| `python:3.12-slim` | None | ✅ Jobs picked up and completed |
| `python:3.12-slim` + pip torch CUDA 12.4 | 12.4 (pip) | ✅ Jobs picked up and completed |
| `pytorch/pytorch:2.4.1-cuda12.4-cudnn9-runtime` | 12.4 | ❌ Unhealthy in 90s |
| `runpod/base:1.0.2-cuda1281-ubuntu2204` | 12.8 | ❌ Unhealthy in 90s |
| `nvidia/cuda:12.4.1-runtime-ubuntu22.04` | 12.4 | ❌ Ready but no job pickup |

## Key Commits
- `b6b0c5e2`: Switch cuda-test Dockerfile to nvidia/cuda base (partial fix)
- `ad24f100`: Switch training Dockerfile to nvidia/cuda base (partial fix)
- `349c194d`: Build slim+CUDA test image (python:3.12-slim + pip torch)
- `8c45c484`: Switch training Dockerfile to python:3.12-slim + pip PyTorch CUDA (complete fix)
