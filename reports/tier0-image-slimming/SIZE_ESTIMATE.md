# Size Estimate

## Summary

| Metric | Production (with torch) | Slim (no torch) | Reduction |
|--------|------------------------|-----------------|-----------|
| Estimated image size | ~6 GB | <1.5 GB | ~4.5 GB (75%+) |
| Largest layer | torch CUDA wheel (~4.5 GB) | catboost (~80 MB) | — |
| Est. RunPod cold-pull time | 155s+ | <60s (est.) | ~95s+ |

## Methodology

Sizes are estimated from known wheel sizes and the python:3.12-slim base
image size. No Docker build was performed (Docker is not available locally
— see RISKS.md). The estimates are conservative:

- `python:3.12-slim` base: ~150 MB (well-documented Docker Hub size).
- torch==2.4.1+cu124 wheel: ~2 GB download, ~4.5 GB extracted (CUDA 12.4
  runtime libraries + cudnn + torch Python package). This is the single
  dominant layer in the production image.
- Remaining pip packages (lightgbm, xgboost, catboost, pandas, pyarrow,
  scikit-learn, numpy, pydantic, pydantic-settings, httpx, runpod):
  ~260 MB total based on PyPI wheel sizes.
- OS packages (libgomp1, libglib2.0-0, build-essential, curl,
  ca-certificates): ~50 MB.
- Source code (quant_foundry, fincept_core, handler, preflight): ~5 MB.

## Why the reduction is so large

The torch CUDA wheel is the overwhelming majority of the production image:
- The wheel itself is ~2 GB (download size).
- When extracted, the CUDA 12.4 runtime libraries, cuDNN, and torch's own
  compiled extensions expand to ~4.5 GB on disk.
- No other layer in the image exceeds ~100 MB.

Removing this single layer drops the image from ~6 GB to <1.5 GB — a 75%+
reduction. The remaining layers (base image + OS packages + pip packages +
source) total well under 1 GB.

## Cold-pull time estimate

RunPod cold-pull time scales roughly linearly with image size (network
bandwidth is the bottleneck). The measured 155s+ cold-pull for the ~6 GB
production image implies:

- ~6 GB / 155s ≈ 39 MB/s effective pull rate.
- <1.5 GB / 39 MB/s ≈ <40s estimated cold-pull for the slim image.

Conservatively, the slim image cold-pull should be under 60s — a 95s+
improvement. This estimate must be verified with a live RunPod probe once
the image is built and pushed with an exact-SHA tag.

## What to verify after building

1. Build the slim image and check `docker images` for the actual size.
2. Push with an exact-SHA tag: `fincept-qf-training:slim-<sha>`.
3. Run a live canary against the slim image and measure cold-pull time.
4. Compare against the production image's 155s+ baseline.
5. Record results in `reports/runpod-test-runs/<sha>/`.
