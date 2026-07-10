# Risks

## Risk 1: No Docker build verification (HIGH — mitigated)

**Risk:** The Dockerfile.slim has not been built with Docker. There could
be syntax errors, broken COPY paths, or pip install failures that only
surface at build time.

**Mitigation:** 23 static validation tests parse the Dockerfile text and
verify the base image, HEALTHCHECK absence, torch absence, required
package presence, ENTRYPOINT parity, and structural elements (GIT_SHA,
non-root user, COPY paths, libgomp1). These catch the most common
Dockerfile regressions. A Docker build must be run before pushing the
image to a registry.

**Action required:** Build the image in an environment with Docker:
```bash
DOCKER_BUILDKIT=1 docker build \
  -t fincept-qf-training:slim \
  -f runpod/quant-foundry-training/Dockerfile.slim \
  --build-arg GIT_SHA=$(git rev-parse HEAD) .
```

## Risk 2: handler.py may import torch (MEDIUM — needs verification)

**Risk:** The production `handler.py` is copied unchanged into the slim
image. If the handler (or any of its transitive imports) imports torch at
module load time, the slim worker will crash on startup with
`ModuleNotFoundError: No module named 'torch'`.

**Mitigation:** The task scope says "current lightgbm training doesn't use
torch." The handler's training path is expected to be torch-free for
tree-model jobs. However, this has NOT been verified by importing handler
in a torch-free environment.

**Action required:** After building the slim image, run a local canary:
```bash
docker run --rm -i -e QUANT_FOUNDRY_CALLBACK_SECRET=secret \
  fincept-qf-training:slim \
  <<< '{"input": {"task": "gpu_healthcheck", "mode": "canary"}}'
```
If the handler imports torch, it will fail immediately. The fix would be
to make the torch import conditional (lazy import inside the NN training
path, not at module top level).

## Risk 3: XGBoost/CatBoost GPU won't work without torch CUDA runtime (LOW — documented)

**Risk:** The production Dockerfile's comment says XGBoost GPU
(`device="cuda"`) works with the "torch-bundled CUDA runtime." Without
torch, the CUDA runtime libraries are not present in the slim image.
XGBoost GPU and CatBoost GPU (`task_type="GPU"`) will fail.

**Mitigation:** This is by design. The slim image is for CPU-only
lightgbm/xgboost/catboost training. The Dockerfile.slim comment block
explicitly documents this limitation and directs users to the production
`-torch` Dockerfile for GPU tree-model training. The static tests verify
that the CPU-only pip packages are installed.

**Action required:** None. This is a documented design choice, not a bug.

## Risk 4: catboost kept despite no GPU support in slim image (LOW — documented)

**Risk:** catboost (~80 MB) is the largest remaining pip package in the
slim image. Its GPU mode won't work without the torch CUDA runtime. It
could be dropped to further reduce image size.

**Mitigation:** Per the hard constraints, catboost must not be removed if
the roadmap expects a GPU backend later. catboost CPU training works fine
without torch. The ~80 MB cost is acceptable. Dropping catboost would be
a separate image-choice decision.

**Action required:** None. Documented in the Dockerfile.slim comment block
and in SUMMARY.md.

## Risk 5: Dockerfile.minimal has a HEALTHCHECK (LOW — not used)

**Risk:** The existing `Dockerfile.minimal` defines a HEALTHCHECK
directive (line 29–30) that would break RunPod job dispatch if used. This
file was NOT modified (outside owned files) and is NOT used by the slim
variant.

**Mitigation:** The new `Dockerfile.slim` does NOT use
`Dockerfile.minimal` as a base — it's built fresh from the production
Dockerfile with torch removed. The static tests verify Dockerfile.slim
has no HEALTHCHECK.

**Action required:** Consider filing a separate task to fix or deprecate
`Dockerfile.minimal`'s HEALTHCHECK. It is a latent footgun for anyone who
tries to use it.

## Risk 6: Size estimates are not measured (LOW — documented)

**Risk:** The ~6 GB → <1.5 GB size reduction is an estimate based on
known wheel sizes, not a measured `docker images` output.

**Mitigation:** The estimate is conservative — the torch CUDA wheel is
well-known to be ~2 GB download / ~4.5 GB extracted. Removing it
necessarily drops the image by that amount. The SIZE_ESTIMATE.md document
includes a methodology section and a "what to verify after building"
checklist.

**Action required:** Measure the actual image size after building and
update SIZE_ESTIMATE.md with real numbers.
