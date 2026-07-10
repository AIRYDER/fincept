# Image Dependencies — BEFORE (Production Dockerfile)

**Source:** `runpod/quant-foundry-training/Dockerfile`
**Base image:** `python:3.12-slim` (~150 MB compressed)
**Estimated image size:** ~6 GB

## OS packages (apt-get)

| Package | Purpose |
|---------|---------|
| libgomp1 | OpenMP runtime (xgboost, lightgbm, scikit-learn) |
| libglib2.0-0 | GLib (ML library shared objects) |
| build-essential | Headers for pip sdists |
| curl | Diagnostics |
| ca-certificates | TLS root certs for pip + runpod SDK |

## Python packages (pip install)

### torch layer (separate RUN, ~2 GB wheel)

| Package | Version spec | Source | Est. size |
|---------|-------------|--------|-----------|
| torch | ==2.4.1 | download.pytorch.org/whl/cu124 | ~2 GB (wheel) + ~2.5 GB extracted CUDA runtime |

### ML + runtime layer (single RUN)

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
| torch CUDA wheel (downloaded + extracted) | ~4.5 GB |
| ML + runtime pip packages | ~260 MB |
| Source code (quant_foundry, fincept_core, handler, preflight) | ~5 MB |
| **Total estimated** | **~5.0–6.0 GB** |

The torch layer dominates: the ~2 GB wheel downloads and extracts to
~4.5 GB of CUDA runtime libraries + the torch Python package. This single
layer accounts for ~75–80% of the total image size.

## Cold-pull impact

On RunPod serverless, a ~6 GB image cold-pull takes 155s+ (measured on
ADA_24). This delays the first job dispatch after a worker scale-up event.
