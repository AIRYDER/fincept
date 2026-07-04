# Image Dependencies — AFTER (Slim Dockerfile)

**Source:** `runpod/quant-foundry-training/Dockerfile.slim`
**Base image:** `python:3.12-slim` (~150 MB compressed)
**Estimated image size:** <1.5 GB

## OS packages (apt-get) — UNCHANGED

| Package | Purpose |
|---------|---------|
| libgomp1 | OpenMP runtime (xgboost, lightgbm, scikit-learn) |
| libglib2.0-0 | GLib (ML library shared objects) |
| build-essential | Headers for pip sdists |
| curl | Diagnostics |
| ca-certificates | TLS root certs for pip + runpod SDK |

## Python packages (pip install) — torch REMOVED

### torch layer — DROPPED ENTIRELY

| Package | Status |
|---------|--------|
| torch ==2.4.1 (from download.pytorch.org/whl/cu124) | **REMOVED** |

The entire torch pip install RUN directive (including the
`--index-url https://download.pytorch.org/whl/cu124` flag) is removed.
This eliminates the ~2 GB wheel download and ~4.5 GB of extracted CUDA
runtime libraries.

### ML + runtime layer (single RUN — UNCHANGED from production)

| Package | Version spec | Est. size |
|---------|-------------|-----------|
| pydantic | >=2.7,<3 | ~5 MB |
| pydantic-settings | >=2.0,<3 | ~1 MB |
| httpx | >=0.27,<1 | ~3 MB |
| runpod | ==1.7.13 | ~5 MB |
| numpy | >=1.26,<3 | ~18 MB |
| pandas | >=2.0,<3 | ~40 MB |
| pyarrow | >=14.0,<20 | ~40 MB |
| scikit-learn | >=1.4,<2 | ~30 MB |
| lightgbm | >=4.0,<5 | ~5 MB |
| xgboost | >=2.0,<3 | ~30 MB |
| catboost | >=1.2,<2 | ~80 MB |

## Size breakdown (estimated)

| Layer | Est. size |
|-------|-----------|
| python:3.12-slim base | ~150 MB |
| OS packages (apt-get) | ~50 MB |
| ~~torch CUDA wheel~~ | ~~4.5 GB~~ → **0 (removed)** |
| ML + runtime pip packages | ~260 MB |
| Source code (quant_foundry, fincept_core, handler, preflight) | ~5 MB |
| **Total estimated** | **~0.5–1.0 GB** |

## What was removed

| Item | Reason |
|------|--------|
| `RUN pip install --no-cache-dir "torch==2.4.1" --index-url https://download.pytorch.org/whl/cu124` | ~2 GB wheel + ~4.5 GB extracted CUDA runtime. Current lightgbm/xgboost training does not import torch. |

## What was kept (identical to production)

All COPY commands, OS packages, non-root user, GIT_SHA arg, ENV vars,
ENTRYPOINT, preflight.py, handler.py, and the full ML pip install block
(minus torch). The dispatch path is identical.

## Cold-pull impact (estimated)

With the torch layer removed, the image drops from ~6 GB to <1.5 GB.
RunPod cold-pull time is expected to drop from 155s+ to under 60s
(proportional to image size reduction). This must be verified with a live
probe once the image is built and pushed.
