# Operator Approval Needed

## Live RunPod Probes (NOT run in this swarm)

The following live probes should be run with operator approval after merging the Tier 0 branches:

### 1. Slim image canary
- **Purpose:** Verify Dockerfile.slim builds and the worker boots without torch.
- **Command:** `docker build -f runpod/quant-foundry-training/Dockerfile.slim -t fincept-qf-training:slim .` then `python runpod/quant-foundry-training/run_live_canary.py --sha <slim-image-sha>`
- **Risk:** handler.py may import torch at module load time, crashing the slim image.
- **Cost:** One RTX 4090 serverless worker for ~5 minutes.

### 2. executionTimeout verification
- **Purpose:** Verify the RunPod API accepts `executionTimeout` in the endpoint template.
- **Command:** `python runpod/quant-foundry-training/run_live_canary.py --sha <sha>` (with the lifecycle helper changes)
- **Risk:** If the field name is wrong, the endpoint may silently use the 600s default.
- **Cost:** One RTX 4090 serverless worker for ~5 minutes.

### 3. Durable artifact verification
- **Purpose:** Verify the /tmp deny gate fires correctly on a real RunPod worker (no volume mounted).
- **Command:** `python runpod/quant-foundry-training/run_train_model.py --sha <sha>` (without setting output_prefix to a volume)
- **Risk:** The deny gate may not fire if runpod_data_root() returns something other than /tmp on RunPod serverless.
- **Cost:** One RTX 4090 serverless worker for ~5 minutes.

## Cleanup

No live RunPod resources were created in this swarm. No cleanup needed.
