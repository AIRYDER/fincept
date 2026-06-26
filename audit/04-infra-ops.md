# Audit: Infrastructure & Operations

## Executive Summary

The Fincept Terminal infrastructure footprint spans four deployment surfaces: (1) RunPod GPU serverless containers for ML training/inference, (2) an AWS production control plane defined in Terraform (ECS Fargate + RDS + ElastiCache + ALB/WAF + S3), (3) a Railway paper-trading staging topology, and (4) a local docker-compose dev stack. This is wrapped by ~60 operational scripts and a mature, SHA-pinned CI/CD pipeline (ci.yml, build-images.yml, aws-iac-validate.yml, nightly.yml).

The overall quality is **high and clearly safety-conscious**. Security boundaries are explicit and enforced in code: RunPod containers carry no broker credentials and sign callbacks with HMAC; AWS secrets come exclusively from Secrets Manager; S3 audit buckets use COMPLIANCE object lock; ElastiCache runs `noeviction`; ALB enforces TLS 1.3 + WAF; CI pins every third-party action to a commit SHA. The Terraform is unusually well-documented and compartmentalized.

However, there are **several concrete defects and drift issues** that will bite at deploy time:

- **ALB access logs are written to an S3 bucket with COMPLIANCE object lock and no ELB-service write grant** — the access-log delivery will fail (missing bucket policy for the ELB service principal), and mixing ALB logs with immutable audit receipts is architecturally wrong.
- **ECS `REDIS_URL` secret mapping is broken** — it points at the raw auth-token secret, not a `rediss://` URL, while ElastiCache has TLS in-transit enabled.
- **The WAF rate-limit value (2000) contradicts its own comment ("100 req / 5 min")**.
- **`COPY apps apps 2>/dev/null || true`** appears in four production Dockerfiles and is invalid Dockerfile syntax (COPY is not shell-evaluated) — builds will break if `apps/` is absent.
- **RunPod containers run as root with no HEALTHCHECK and no non-root USER**, unlike the ECS Dockerfiles which are hardened.
- **RunPod handler dev-secret fallback** silently uses `dev-callback-secret-DO-NOT-USE-IN-PROD` instead of failing closed.
- **`recreate_endpoints.py` runs all its logic at module import time** (no `__main__` guard) with hardcoded template/volume IDs.
- **Hardcoded RunPod endpoint/template/network-volume IDs** are duplicated across `railway-production.json`, `rebuild_runpod_containers.py`, `verify_runpod_containers.py`, `e2e_runpod_real_ml.py`, and `recreate_endpoints.py`.
- **nixpacks.toml pins `uv==0.4.30` while the Dockerfiles pin `uv:0.5.7`** — Railway and ECS builds diverge.
- **docker-compose.yml header references `infra/k8s/` which does not exist** (AWS uses ECS).

The RunPod handler protocol understanding is correct (runpod SDK polling, `RUNPOD_WEBHOOK_GET_JOB` diagnostics, stdin fallback), and the dual-mode `LocalTrainer`/`RealLightGBMTrainer` toggle is a clean contract-proof bridge. The operational script library is large and functional but lacks a shared RunPod client module — the same GraphQL/REST boilerplate is copy-pasted across ~15 scripts with inconsistent API hosts (`api.runpod.ai` vs `api.runpod.io`).

---

## RunPod GPU Containers

### Training Container (`runpod/quant-foundry-training/`)

**Layout**
- `Dockerfile` (66 lines) — `python:3.12-slim` base, `WORKDIR /worker`, copies `services/quant_foundry/src/quant_foundry/` and `handler.py`, `pip install`s pydantic/httpx/runpod/lightgbm/pyarrow, `ENTRYPOINT ["python","-u","/worker/handler.py"]`.
- `handler.py` (199 lines) — RunPod entrypoint bridging `RunPodTrainingRequest` → `RunPodTrainingHandler` → signed `RunPodCallbackEnvelope`.
- `README.md` — contract, env vars, security boundary.
- `real_trainer.py` and `real_inference.py` are **not in this directory** — they live in `services/quant_foundry/src/quant_foundry/` and are imported lazily. (The task brief listed them under runpod/, but they are owned by the quant_foundry service.)

**How Handler Works**
1. RunPod serverless loader calls `handler(event)`.
2. `event["input"]` is validated against `RunPodTrainingRequest` (pydantic). An `inline_dataset_csv` extension field is popped *before* validation (schema forbids extras) and written to a temp file for E2E tests.
3. `_build_trainer()` selects `RealLightGBMTrainer` (when `QUANT_FOUNDRY_USE_REAL_TRAINER=true`) or `LocalTrainer` (deterministic stub).
4. `RunPodTrainingHandler.handle(req)` trains, builds `ArtifactManifest` + `ModelDossier`, signs the callback with `QUANT_FOUNDRY_CALLBACK_SECRET`.
5. Returns `{job_id, callback_payload, callback_signature, callback_ts, artifact_id, dossier_id}` or an error envelope `{error_code, error_summary, job_id}`.
6. `__main__` block: imports `runpod`, dumps `RUNPOD_*` env vars to a debug log on the network volume (`/runpod-volume/handler-debug.log` or `/workspace/handler-debug.log`), then calls `runpod.serverless.start({"handler": handler})`. Falls back to stdin JSON if the SDK is missing.

**Dockerfile Analysis**
- Base `python:3.12-slim` — no CUDA/GPU libraries. LightGBM runs on CPU; RunPod injects the GPU but the image doesn't use it. This is fine for the current LightGBM workload but means `onnxruntime` (inference) won't use GPU either.
- `build-essential` installed for compilation; `apt lists` cleaned.
- `pip install --no-cache-dir` with **loose lower bounds** (`pydantic>=2.7`, `lightgbm>=4.0`, etc.) — **no lockfile**, so builds are not reproducible. The README claims `lockfile_hash` is pinned at build time, but there is no lockfile to hash.
- `ARG GIT_SHA=unknown` → `ENV QUANT_FOUNDRY_GIT_SHA` — good reproducibility pin, wired into `build-images.yml` (`build-args: GIT_SHA=${{ github.sha }}`).
- `ENTRYPOINT` (not `CMD`) — correct for RunPod so `dockerArgs` can't override it.
- **No `USER` directive — runs as root.** No `HEALTHCHECK`. No `.dockerignore` referenced. No multi-stage build (single layer).

**What's Optimally Implemented**
- Security boundary is pristine: no broker creds, no Redis, no stream write. The container is a pure function over its inputs.
- Signed callbacks with HMAC + fail-closed verification on the dispatcher side.
- `authority=SHADOW_ONLY` enforced — promotion is a separate human-gated step.
- Deadline enforcement via `QUANT_FOUNDRY_TRAINING_DEADLINE_SECONDS`.
- Excellent debug logging in the `__main__` block — dumps `RUNPOD_*` env vars and warns explicitly when they're missing (the "jobs stay IN_QUEUE forever" footgun is documented inline).
- `inline_dataset_csv` handler extension is a thoughtful E2E testing escape hatch that doesn't pollute the schema.
- Robust error envelope: schema validation failures and `TrainingFailure` both return terminal error dicts, never crash.

**What Needs Work**
- **Dev-secret fallback** (`_get_callback_secret` returns `"dev-callback-secret-DO-NOT-USE-IN-PROD"` when env is empty) — in production this silently signs callbacks with a known public secret instead of failing closed. The README even documents this default. Should `raise` when `QUANT_FOUNDRY_ENABLED`/prod mode is set.
- **No lockfile / reproducible deps** — `pip install` with `>=` ranges. The `ArtifactManifest.lockfile_hash` field is documented but unbacked.
- **Runs as root**, no `HEALTHCHECK`, no non-root UID — inconsistent with the hardened ECS Dockerfiles (`USER fincept`, UID 1001).
- **No `.dockerignore`** — the build context is the repo root (`docker build -f runpod/.../Dockerfile .`), so the entire monorepo (including `apps/`, `.git`, `node_modules`, `__pycache__`) is sent as context. Slow and leaky.
- `pyarrow` and `lightgbm` installed but `numpy`/`pandas`/`scikit-learn` are not pinned explicitly (transitive only) — RealLightGBMTrainer may need them.

**What Might Break**
- If `QUANT_FOUNDRY_CALLBACK_SECRET` is unset on RunPod, callbacks are signed with the dev secret and the dispatcher will reject them (or worse, accept them if the dispatcher also defaults). Silent failure mode.
- Loose pip pins mean a upstream `lightgbm` 5.x or `pydantic` 3.x release breaks the image with no pin to roll back to.
- `runpod>=1.6` — the SDK's `serverless.start()` API has changed across versions; an upstream breaking change would wedge every worker.

**Better Approaches**
- Add a `uv.lock`-based install (`uv pip install --frozen`) or a pinned `requirements.txt` with hashes.
- Add `USER 1001` + `HEALTHCHECK` (a stdin echo probe or a TCP probe on the runpod SDK's local test server).
- Add a repo-root `.dockerignore` excluding `apps/`, `.git`, `node_modules`, `__pycache__`, `reports/`, `audit/`.
- Fail closed on missing callback secret when a `FINCEPT_ENV=prod` (or similar) flag is set.
- Consider a multi-stage build to drop `build-essential` from the runtime image.

### Inference Container (`runpod/quant-foundry-inference/`)

**Layout**
- `Dockerfile` (60 lines) — `python:3.12-slim`, `WORKDIR /app`, copies `quant_foundry/` + `handler.py`, installs pydantic/httpx/runpod/onnxruntime/lightgbm/numpy, `ENTRYPOINT ["python","-u","/app/handler.py"]`.
- `handler.py` (127 lines) — RunPod shadow inference entrypoint.
- `README.md`.

**How Handler Works**
1. `handler(event)` reads `event["input"]`, parses `input_data["request"]` into `RunPodInferenceRequest` and `input_data["snapshot"]` into `FeatureSnapshot`.
2. Selects `RealInferenceEngine` (when `QUANT_FOUNDRY_USE_REAL_INFERENCE=true` AND `request.artifact_ref` is set) or `ShadowInferenceEngine` (stub).
3. `engine.run(request, snapshot, model_id)` returns predictions + callback.
4. Signs callback, returns `{job_id, callback_payload, callback_signature, callback_ts, callback, predictions, latency_ms}`.
5. `__main__`: tries `runpod.serverless.start()`, falls back to stdin JSON. **No debug logging** (unlike training).

**Dockerfile Analysis**
- Same pattern as training: slim base, `build-essential`, loose pip pins, `GIT_SHA` arg, no USER, no HEALTHCHECK, no .dockerignore.
- Installs `onnxruntime>=1.17` — **CPU-only** `onnxruntime`, not `onnxruntime-gpu`. On a RunPod GPU pod this wastes the GPU. The RealInferenceEngine loads ONNX/LightGBM artifacts but ONNX will run on CPU.
- `numpy>=1.26` pinned but not `pandas`/`scikit-learn`.

**What's Optimally Implemented**
- Shadow-only authority enforced; `InferenceDisabledError` is fail-safe (no predictions produced when disabled).
- Clean stub/real engine toggle gated on both env var AND `artifact_ref` presence.
- Latency + feature availability reported per prediction.

**What Needs Work**
- **Input validation is brittle**: `input_data = event.get("input", {})` then `request = RunPodInferenceRequest(**input_data["request"])` — if `"request"` is missing this raises a raw `KeyError`, not a safe error envelope. The training handler validates `isinstance(input_data, dict)` first; this one doesn't.
- **No `TrainingFailure`-style error envelope** — only `InferenceDisabledError` is caught. Any other exception (schema error, model load failure) crashes the worker with a traceback.
- **No debug logging** in `__main__` — the training handler's excellent `RUNPOD_*` diagnostics are absent here, so diagnosing stuck-queue issues on the inference endpoint is harder.
- **`sys.path` manipulation hack** at module top (`_quant_foundry_paths` loop inserting repo-relative paths) — brittle; works because `PYTHONPATH=/app` is set, but the repo-relative paths (`../../services/quant_foundry/src`) only resolve when running from the repo, not in the container. Dead code in production.
- Same dev-secret fallback, root user, no healthcheck, no lockfile issues as training.
- `onnxruntime` (CPU) on a GPU pod — wasted spend.

**What Might Break**
- A malformed event (missing `request` or `snapshot` key) crashes the worker instead of returning an error envelope — RunPod will mark the job FAILED with an opaque traceback.
- `RealInferenceEngine` loading a model artifact from `file:///model.pkl` requires the artifact to be present on the container/volume; if the network volume isn't mounted or the path is wrong, the error path is unhandled.
- `event.get("input", {})` defaults to a dict even when `event` is not a dict (e.g., `event=None`) → `None.get` crashes.

**Better Approaches**
- Mirror the training handler's input validation (`isinstance(event, dict)`, `isinstance(input_data, dict)`, try/except around schema parse returning an error envelope).
- Catch `Exception` broadly and return an error dict (the training handler's pattern).
- Port the debug-logging `__main__` block from training.
- Install `onnxruntime-gpu` (or use RunPod's CUDA base image) if GPU inference is intended.
- Remove the dead `sys.path` manipulation; rely on `PYTHONPATH=/app`.

---

## Infrastructure

### AWS Terraform (`infra/aws/`)

**Layout** — 16 `.tf` files + README + tfvars example + `.terraform.lock.hcl`. Provider pinned `hashicorp/aws ~> 5.50`, Terraform `>= 1.9.0, < 2.0.0`. Default tags on every resource. Files: `providers`, `variables` (287 lines), `locals`, `data`, `network`, `iam`, `ecr`, `s3`, `secrets`, `cloudwatch`, `ecs`, `rds`, `elasticache`, `alb_waf`, `outputs`.

**What's Optimally Implemented**
- **Excellent compartmentalization** — one resource group per file, each with a descriptive header tying back to a design doc (TASK-0903).
- **Default tags** on the provider + `local.common_tags` merged everywhere — cost allocation and ownership are clean.
- **Three-tier subnet topology** (public/private/database) with database subnets having **no internet route** (isolated). Single NAT gateway for cost (documented tradeoff).
- **S3 audit buckets** (receipts, dossiers, settlements) have **COMPLIANCE object lock, 365-day retention** — cannot be deleted even by root. Versioning + public access block + SSL-only bucket policy on every bucket.
- **ECR**: immutable tags, scan-on-push, lifecycle policy (keep last 10 `v*` tags, expire untagged after 7 days). `force_delete = false`.
- **RDS**: encrypted at rest (KMS CMK), Multi-AZ in prod, deletion protection in prod, 35-day backup retention (PITR), non-default master username, `publicly_accessible = false`, TimescaleDB via `shared_preload_libraries`.
- **ElastiCache (Valkey)**: multi-AZ failover, TLS in-transit + at-rest encryption, `maxmemory-policy = noeviction` (event bus must not silently drop), auth token from Secrets Manager.
- **IAM**: separate execution + task roles, least-privilege inline policies scoped to `fincept/*` secret ARNs and `/fincept/*` log groups. No managed `AmazonEC2ContainerServiceRole` over-grant.
- **ALB/WAF**: TLS 1.3 policy, HTTP→HTTPS redirect, path-based routing (`/api/*` → api, `/*` → dashboard), WAF with AWS Managed Rules (Common, Known Bad Inputs, IP Reputation) + custom rate limit, deletion protection in prod, access logs to S3.
- **CloudWatch**: log groups (30-day retention), 4 alarms (API p95 latency, API 5xx rate, settlement lag with `breaching` on missing, BudgetGuard kill-switch), operator dashboard with 4 widgets.
- **Variables**: every input has a type, description, and validation where appropriate (`az_count` 2–3, `environment` prod/staging, `rds_backup_retention_days` 7–35, CIDR validity). Sensitive values marked `sensitive = true`.
- **Outputs**: scrubbed (no secret values), operator-facing identifiers only.
- **Secrets**: KMS CMK with key rotation, `recovery_window_in_days = 30`, `lifecycle { ignore_changes = [secret_string] }` so out-of-band rotation doesn't drift. Initial value defaults to `REPLACE_ME_AT_APPLY_TIME`.
- **Backend**: intentionally left as operator-supplied S3 backend (commented example with DynamoDB lock) — forces a conscious decision rather than committing state config.

**What Needs Work**
- **ALB access logs bucket policy is missing.** `aws_lb.main.access_logs` points at `aws_s3_bucket.main["receipts"]` with prefix `alb-access-logs`, but there is **no `aws_s3_bucket_policy` granting the ELB service principal `s3:PutObject`** on that prefix. ALB access logging will silently fail to deliver. The `ssl_only` bucket policy denies non-SSL but doesn't grant ELB write. AWS requires a bucket policy with `elasticloadbalancing.amazonaws.com` (or the account's ELB service account ARN) `s3:PutObject` permission.
- **Mixing ALB access logs into a COMPLIANCE-object-Lock audit bucket** is architecturally wrong — ALB logs are append-only operational telemetry, not immutable audit receipts. They should have their own bucket (with versioning, no object lock, shorter retention).
- **ECS `REDIS_URL` secret mapping is broken.** `ecs.tf` line 49: `{ name = "REDIS_URL", valueFrom = aws_secretsmanager_secret.main["fincept/redis-auth-token"].arn }` — this injects the **raw auth token** as `REDIS_URL`, not a `rediss://:token@host:6379/0` URL. With `transit_encryption_enabled = true` on ElastiCache, the app must use `rediss://` with the token embedded. The app will get a bare string like `"my-auth-token"` and fail to connect. The `DATABASE_URL` mapping uses the `"::password::"` suffix trick (also fragile — assumes the secret value is just the password and the app constructs the URL), but `REDIS_URL` doesn't even have that.
- **WAF rate-limit value contradicts its comment.** `alb_waf.tf` line 282: `limit = 2000 # 100 req / 5 min normalized per the design doc`. 2000 req / 5 min is 400 req/min, not 100 req/5min. Either the comment is wrong or the value is wrong. The README repeats "100 req / 5 min".
- **S3 encryption mismatch.** `s3.tf` header says "Server-side encryption (SSE-KMS via AWS-managed key — KMS CMK optional)" but the actual `aws_s3_bucket_server_side_encryption_configuration` uses `sse_algorithm = "AES256"` (SSE-S3), not KMS. For audit-integrity buckets holding receipts/dossiers, SSE-KMS with the customer-managed key (`aws_kms_key.secrets`) would be more defensible.
- **CloudWatch Logs are not KMS-encrypted** — the `kms_key_id` is commented out in `cloudwatch.tf`. Audit logs in `/fincept/<env>/vpc/flow-logs` and service logs are only SSE-S3 encrypted.
- **IAM `ecs_task_s3` policy is broad** — `ListBucket` on all 5 buckets and `GetObject`/`PutObject` on all objects under all buckets. The api service (read-only views) gets the same S3 write perms as the orchestrator. Should be split per-service (api: read-only on receipts/dossiers; orchestrator: write on settlements).
- **No RDS Proxy** despite the README mentioning it ("Connection pooling recommended via RDS Proxy (out of scope for MVP)"). With 2 api + 1 orchestrator tasks the connection count is low, but worth flagging.
- **No ECS autoscaling target tracking** — the `ecs_autoscale` role is created but no `aws_appautoscaling_policy` is attached. The services run at fixed `desired_count`.
- **`aws_db_option_group.main`** is created empty (no options) — TimescaleDB on RDS only needs `shared_preload_libraries` (which is set in the parameter group), so the option group is dead weight. Not harmful but unnecessary.
- **Terraform backend defaults to local** — if an operator forgets to supply `-backend-config`, state is written locally with no locking. A `terraform init` without backend config should fail loud in prod, not silently fall back.
- **`locals.ecs_services`** only includes api/dashboard/orchestrator — OMS and risk are in `ecr_repositories` (repos created) but have no task definitions or services. This is intentional per the README ("Reserved, NOT deployed in v1"), but the `cloudwatch_log_group.service` `for_each` uses `keys(local.ecs_services)`, so OMS/risk log groups aren't created. Consistent, but means the reserved Dockerfiles' log groups don't exist.
- **Dashboard `NEXT_PUBLIC_API_URL`** in the task definition uses `https://${var.domain_name != "" ? var.domain_name : aws_lb.main.dns_name}/api` — but `NEXT_PUBLIC_*` env vars in Next.js are **baked at build time**, not runtime. Setting it as an ECS env var at task start does NOT override the value compiled into the JS bundle. The dashboard will use whatever was baked in the Dockerfile (`http://localhost:8000/api`) unless rebuilt per-environment. This is a well-known Next.js footgun.

**What Might Break**
- ALB access logging fails silently (no bucket policy for ELB).
- `REDIS_URL` secret injection produces an invalid URL → every service that touches Redis crashes on startup.
- Dashboard client-side API calls hit `localhost:8000` in production because `NEXT_PUBLIC_API_URL` isn't actually runtime-overridable.
- WAF rate limit is 4× higher than intended.
- If an operator runs `terraform init` without a backend config, state goes local → no locking → concurrent applies corrupt state.

**Better Approaches**
- Add a dedicated `aws_s3_bucket` for ALB access logs (no object lock, 30-day lifecycle) + a bucket policy granting `elasticloadbalancing.amazonaws.com` `s3:PutObject` on `alb-access-logs/*`.
- Store `REDIS_URL` as a full `rediss://:token@host:6379/0` string in Secrets Manager (or use a Lambda rotation function that composes it), and inject the whole URL.
- Use SSE-KMS with `aws_kms_key.secrets` for the audit buckets.
- Split `ecs_task_s3` into per-service policies.
- Bake `NEXT_PUBLIC_API_URL` at Docker build time via a build arg, or use Next.js runtime config / a public runtime env var.
- Make the S3 backend mandatory (uncomment + require `TF_VAR_backend_*`).
- Add `aws_appautoscaling_policy` (target tracking on CPU/RPS) for the api service.

### Docker Configuration (`infra/docker/`)

**Layout** — 5 Dockerfiles: `api`, `dashboard`, `orchestrator`, `oms`, `risk`. All multi-stage, non-root UID 1001, healthcheck-enabled.

**What's Optimally Implemented**
- **Multi-stage builds** — builder stage with `uv` syncs the workspace package + deps; runtime stage copies only `.venv` + source. Small attack surface.
- **`uv` pinned via `COPY --from=ghcr.io/astral-sh/uv:0.5.7`** — reproducible package manager.
- **`uv sync --frozen --no-dev --package <svc>`** — lockfile-enforced, no dev deps in prod.
- **Non-root `USER fincept` (UID 1001)** on every image.
- **`HEALTHCHECK`** on every image (python urllib for Python services, wget for dashboard).
- **`PYTHONDONTWRITEBYTECODE`, `PYTHONUNBUFFERED`, `PYTHONFAULTHANDLER`** set.
- **`--chown=fincept:fincept`** on every COPY — correct ownership.
- Dashboard uses Next.js standalone output (~50–80MB vs 300MB).
- API runs `uvicorn ... --proxy-headers` (honors ALB `X-Forwarded-For`).
- Clear file-level comments explaining the security boundary of each service (e.g., OMS is the only service with broker creds; risk has no broker creds).

**What Needs Work**
- **`COPY apps apps 2>/dev/null || true`** in `api.Dockerfile` (line 32), `orchestrator.Dockerfile` (line 23), `oms.Dockerfile` (line 24), `risk.Dockerfile` (line 20). `COPY` is **not shell-evaluated** — `2>/dev/null` and `|| true` are parsed as additional destination paths/flags, which is invalid. If `apps/` doesn't exist, the build fails. If it does exist, the extra tokens likely cause a parse error or are silently ignored depending on BuildKit version. This should be `COPY apps apps` (and ensure `apps/` exists) or removed if the Python services don't need the dashboard.
- **No `.dockerignore`** at repo root — the build context (`.`) ships the entire monorepo including `apps/`, `.git`, `node_modules`, `reports/`, `audit/`, `__pycache__`. Slow + leaky.
- **`uv:0.5.7` pinned in Dockerfiles but `nixpacks.toml` pins `uv==0.4.30`** — Railway builds use a different uv version than ECS builds. Dependency resolution can diverge.
- **OMS Dockerfile comment** claims "drop all capabilities, read-only root filesystem, no new privileges" but **none of these are actually set** in the Dockerfile (no `--cap-drop`, no `--read-only`, no `--security-opt no-new-privileges`). These are runtime flags, not Dockerfile directives — the comment is misleading. They'd need to be set in the ECS task definition (`linuxParameters` / `readonlyRootFilesystem`), which they aren't.
- **Dashboard `NEXT_PUBLIC_API_URL` baked as `http://localhost:8000/api`** default — same Next.js runtime-vs-build-time issue as the Terraform. The ECS env var won't override the baked value for client-side fetches.
- **`pnpm-lock.yaml*` glob** in `dashboard.Dockerfile` COPY — if no lockfile exists, `pnpm install --frozen-lockfile` fails. The glob is a COPY convenience but the frozen install is brittle.
- **No image labels** (OCI `org.opencontainers.image.*`) — no provenance metadata in the images.
- **`CMD` uses `--workers 1`** for all Python services — fine for ECS (scale via desired_count) but the orchestrator is a stream consumer, not an HTTP server; running it under uvicorn is unusual (it serves `/health` but the stream consumer is the real workload — is that launched in lifespan?).

**What Might Break**
- The `COPY apps apps 2>/dev/null || true` line will break builds once `apps/` is renamed/removed or BuildKit tightens COPY parsing.
- Railway vs ECS uv version drift can produce different transitive deps → "works on Railway, fails on ECS" bugs.
- Dashboard in production fetches from `localhost:8000` if the build-time default isn't overridden correctly.

**Better Approaches**
- Fix the `COPY apps` lines (remove the shell-ism or make `apps/` a guaranteed-present path).
- Add a repo-root `.dockerignore`.
- Pin uv to the same version in `nixpacks.toml` and the Dockerfiles.
- Move OMS hardening claims into the ECS task definition (`readonlyRootFilesystem: true`, `disableNetworking: false`, `user: "1001"`) or remove the misleading comment.
- Add OCI image labels via `LABEL` or `docker/metadata-action` in CI.
- Bake `NEXT_PUBLIC_API_URL` via a build arg, or switch the dashboard to use a server-side API route that reads a runtime env var.

---

## Operational Scripts

### Inventory

~60 scripts in `scripts/`. Categorized:

**AWS deployment (PowerShell):**
- `aws_preflight.ps1` — pre-flight checklist (tooling, account, quotas, secrets, no plaintext leaks). Writes receipt.
- `aws_postapply_verify.ps1` — post-apply verification harness (runbook §3.1–§3.10).
- `aws_receipt.ps1` — generates binding deployment receipt (runbook §3.11).
- `verification-receipt.ps1` — general verification receipt (called by CI `receipt-runner` job).

**RunPod deploy/verify:**
- `deploy_runpod_endpoints.py` — updates bound template's `imageName` + env via GraphQL `saveTemplate`. Supports `--dry-run`, redacts secrets.
- `recreate_endpoints.py` — deletes + recreates endpoints to fix scheduler state corruption (documented RunPod bug).
- `rebuild_runpod_containers.py` — builds + pushes RunPod containers, optionally refreshes endpoints. Dataclass-based result types, exit codes.
- `verify_runpod_containers.py` — sends test payloads, verifies real ML output (not stubs). Polls async jobs.
- `update_image_sha.py` — updates image SHA on endpoints.
- `restore_endpoint.py` — restore an endpoint.
- `set_registry_auth.py` — set container registry auth.

**RunPod probes/checks:**
- `check_pod_runtime.py`, `check_pod_logs.py`, `get_pod_logs.py`, `check_runpod_pods.py`, `check_runpod_endpoint.py`, `check_template_full.py`, `check_endpoint_full.py`, `check_ghcr.py`, `check_both_health.py`.
- `probe_runpod.py`, `probe_inference.py`, `probe_inference_new.py`, `probe_new_endpoints.py`.
- `wait_worker.py`, `wait_heartbeat.py`.
- `recycle_runpod_workers.py`, `detach_volume.py`, `purge_queue.py`.
- `clear_docker_args.py`.

**E2E / proof scripts:**
- `e2e_runpod_real_ml.py` — full training→inference pipeline test with real ML verification.
- `live_runpod_proof.py`, `openbb_live_proof.py`, `paper_bridge_proof.py`, `paper_spine_replay.py`.
- `test_runpod_dispatch.py`, `test_sentiment_pipeline.py`, `inject_test_prediction.py`.
- `route_smoke.py`, `close_the_loop.py`, `_gbm_smoke.py`.

**Data / backtest:**
- `build_dataset_manifest.py`, `build_synth_ohlcv.py`, `build_synth_parquet.py`, `build_synthetic_dataset.py`, `capture_to_parquet.py`, `ingest_bars.py`.
- `run_backtest.py`, `walk_forward.py`, `run_intraday_walkforward.py`, `stage_baseline_training.py`.
- `sync_alpaca.py`, `sync_alpaca_fills.py`.

**Local dev (PowerShell):**
- `start.ps1`, `stop.ps1`, `status.ps1`, `dev-setup.ps1`, `preflight.ps1`, `start_feature.ps1`, `stop_feature.ps1`, `task-check.ps1`.

### Deploy & Verify

**`deploy_runpod_endpoints.py`** — the most mature script. Fetches the endpoint template via GraphQL, merges env updates (preserving non-updated keys), redacts `QUANT_FOUNDRY_CALLBACK_SECRET` in output, supports `--dry-run` and `--force`, sets `dockerArgs` to the explicit `python -u <handler>` command (with an inline comment explaining why empty `dockerArgs` broke the handler). Good exit-code semantics. Uses `https://api.runpod.io/graphql` with the API key as both Bearer header and query param (RunPod accepts either).

**`rebuild_runpod_containers.py`** — well-structured (dataclass result types, precondition checks for Docker installed/running/Dockerfile exists, `--dry-run` honored, exit codes 0–4). Builds from repo root (`.`) so COPY paths resolve. **Bug**: `CONTAINERS` dict has a `context_rel` field that is **never used** — `build_container` hardcodes `.` as the context. Dead config. Also uses `RUNPOD_API_BASE = "https://api.runpod.ai/v2"` for refresh while `deploy_runpod_endpoints.py` uses `https://api.runpod.io/graphql` — inconsistent API hosts across scripts (both are valid RunPod endpoints, but the divergence is confusing).

**`verify_runpod_containers.py`** — sends minimal payloads, polls async `/status` until COMPLETED/FAILED, checks for real ML output (not stub patterns like `accuracy = 0.5 + pbo/2.0`). Lazy httpx/requests fallback. Good verification logic.

**`e2e_runpod_real_ml.py`** — generates a synthetic CSV with real signal, dispatches training then inference, verifies real metrics (accuracy range, logloss > 0, brier range, max_drawdown ≤ 0, authority = shadow_only). Solid end-to-end proof.

### E2E Tests

The E2E scripts (`e2e_runpod_real_ml.py`, `live_runpod_proof.py`, `paper_bridge_proof.py`, `paper_spine_replay.py`, `close_the_loop.py`) are proof-of-correctness scripts that dispatch real jobs and verify the full pipeline. They require `RUNPOD_API_KEY` and endpoint IDs as env vars. They are **not wired into CI** (no `@pytest` marker, no GitHub Actions job) — they're manual operator-run proofs. This is appropriate (they cost GPU time) but means there's no automated regression check for the RunPod path.

### Utility Scripts

The probe/check/recycle scripts (`probe_runpod.py`, `check_pod_runtime.py`, `recycle_runpod_workers.py`, `wait_worker.py`, `detach_volume.py`, `purge_queue.py`, etc.) are **ad-hoc operator diagnostics**. Most share these traits:
- Module-level execution (no `if __name__ == "__main__"` guard) — importing them runs their logic.
- `os.environ["RUNPOD_API_KEY"]` at module top — crashes on import if the env var is missing.
- Copy-pasted GraphQL/REST boilerplate (`graphql()` helper, `httpx.post(f"https://api.runpod.io/graphql?api_key={api_key}", ...)`).
- Hardcoded endpoint/template/network-volume IDs.
- No error handling beyond `raise_for_status()`.

### What's Optimally Implemented
- `deploy_runpod_endpoints.py`, `rebuild_runpod_containers.py`, `verify_runpod_containers.py`, `e2e_runpod_real_ml.py` are production-grade: dataclass result types, dry-run support, exit codes, secret redaction, precondition checks.
- The AWS PowerShell scripts (`aws_preflight.ps1`, `aws_postapply_verify.ps1`, `aws_receipt.ps1`) emit timestamped Markdown + JSON receipts under `reports/verification/` — binding audit trail.
- `verification-receipt.ps1` is wired into CI (`receipt-runner` job) — fails PRs that drop runtime safety checks.

### What Needs Work
- **No shared RunPod client module.** The same GraphQL/REST/dispatch/poll logic is reimplemented in ~15 scripts. A `scripts/lib/runpod_client.py` with a `RunPodClient` class would eliminate ~500 lines of duplication and the API-host inconsistency.
- **`recreate_endpoints.py` runs all logic at module import** (no `__main__` guard) — importing this module deletes and recreates production endpoints. Extremely dangerous if accidentally imported by a test or another script.
- **Hardcoded IDs duplicated everywhere**: `TRAINING_TEMPLATE_ID = "me58r5vdrp"`, `INFERENCE_TEMPLATE_ID = "wnasp3v5jn"`, `networkVolumeId = "rrsd005i3g"`, endpoint IDs `h2blqodcicxqyy` / `t31u1z426jy1ub` appear in `railway-production.json`, `rebuild_runpod_containers.py`, `verify_runpod_containers.py`, `e2e_runpod_real_ml.py`, `recreate_endpoints.py`. These should be in a single config file or env-var-driven.
- **Module-level `os.environ["RUNPOD_API_KEY"]`** in ~10 scripts — crashes with an opaque `KeyError` instead of a helpful message. Use `os.environ.get(...)` with a clear error.
- **Inconsistent API hosts**: `api.runpod.ai/v2` (REST) vs `api.runpod.io/graphql` (GraphQL). Both valid, but the mix within the same script library is confusing and suggests copy-paste from different RunPod doc eras.
- **`rebuild_runpod_containers.py` `CONTAINERS` dict has an unused `context_rel`** — dead config that misleads readers.
- **No script tests** — the operational scripts themselves have no unit tests; a bug in `deploy_runpod_endpoints.py`'s env-merge logic would only surface at deploy time.
- **`_gbm_smoke.py`** (leading underscore) is a private smoke script — unclear if it's still used.

### Better Approaches
- Extract `scripts/lib/runpod_client.py` (one client class, one config source for endpoint/template IDs).
- Add `if __name__ == "__main__":` guards to every script (especially `recreate_endpoints.py`).
- Centralize RunPod resource IDs in `scripts/lib/runpod_config.py` or a `.env.runpod` template.
- Add a `--check` mode to `deploy_runpod_endpoints.py` that diffs current vs desired template state (like `terraform plan`).
- Add unit tests for the env-merge / redaction logic in `deploy_runpod_endpoints.py`.
- Consider converting the ad-hoc probe scripts into a single `runpodctl` CLI with subcommands.

---

## Deployment Configuration

### Railway (`railway.json`, `railway-production.json`)

**`railway.json`** — minimal staging config: NIXPACKS builder, `uvicorn api.main:app` start command, `/health` healthcheck, `ON_FAILURE` restart (max 3 retries). Clean.

**`railway-production.json`** — **operator documentation, not a parsed config.** The `_description` field explicitly notes "Railway does not natively parse a multi-service JSON config the way docker-compose does; this file is operator documentation that mirrors the service graph." It documents 5 services (postgres, redis, object-storage, api, dashboard) with env var wiring, safety invariants (`FINCEPT_TRADING_MODE=paper`, `FINCEPT_OMS_ROUTER=sim`, no broker creds on Railway), and secret injection instructions.

**What's Optimally Implemented**
- Safety invariants are explicit and documented (`_safety_invariants` array).
- Secrets use `${{secrets.NAME}}` placeholders — never committed.
- `never_on_railway` list explicitly calls out broker creds that must NOT be set.
- Cross-references to `RAILWAY_DEPLOY_GUIDE.md`, `.env.example`, and the FastAPI lifespan.
- Background poll task cadences are documented (15s result poll, 60s settlement, 300s tournament, 300s shadow dispatch).

**What Needs Work**
- **Hardcoded RunPod endpoint IDs** (`h2blqodcicxqyy`, `t31u1z426jy1ub`) committed in the production config — if these endpoints are recreated (via `recreate_endpoints.py`), this file is stale and the API will dispatch to dead endpoints.
- **`railway.json` start command** uses `/opt/venv/bin/uvicorn` (absolute path) while `railway-production.json` uses bare `uvicorn` — inconsistent. The nixpacks build puts uvicorn at `/opt/venv/bin/`, so the staging path is correct, but the production doc would fail if copied verbatim.
- **No `healthcheckPath` on the dashboard** in production (`/api/health`) vs staging — staging has no dashboard service at all. Minor.
- The production JSON is not machine-validated — drift between it and the actual Railway dashboard state is invisible.

### Docker Compose (`docker-compose.yml`)

**Layout** — 3 services: `timescale/timescaledb:latest-pg16`, `redis:7-alpine`, `minio/minio:latest`. Named volumes for each. Healthchecks on all three. `restart: unless-stopped`.

**What's Optimally Implemented**
- TimescaleDB image matches the RDS engine (pg16 + TimescaleDB).
- Redis `--maxmemory-policy noeviction` matches the prod ElastiCache config — local dev behaves like prod.
- MinIO provides an S3-compatible local stand-in.
- Healthchecks with appropriate intervals.
- `TS_TUNE_MEMORY` / `TS_TUNE_NUM_CPUS` tuning for TimescaleDB.

**What Needs Work**
- **Stale/misleading header comment**: "Production runs the same images via Kubernetes (see infra/k8s/)." — `infra/k8s/` does not exist; production uses AWS ECS Fargate. This comment is wrong and will mislead operators.
- **No api/dashboard services** in compose — only datastores. The dev workflow requires separately running the API/dashboard. A `make dev` that boots compose + the API would be smoother. (The comment says "Boot with `make dev`" — implying a Makefile orchestrates this, but compose alone doesn't start the app.)
- **MinIO root password `fincept-minio-pw`** hardcoded — acceptable for dev but should be in a `.env.compose` that's gitignored, or use Docker secrets.
- **Redis `--maxmemory 2gb`** — on a machine with < 4GB RAM this will OOM. No memory guard.
- **`minio/minio:latest`** and `timescale/timescaledb:latest-pg16` — `:latest` tags are not reproducible. Pin to specific digests or version tags.
- **No network isolation** — all three services are on the default bridge network with ports exposed to the host. Fine for dev.

### Nixpacks (`nixpacks.toml`)

- Pins `python312` nix package, creates venv at `/opt/venv`, installs `uv==0.4.30`, runs `uv sync --no-dev --package api`, appends PATH to `.profile`, starts `uvicorn api.main:app`.
- **`uv==0.4.30` vs Dockerfiles' `uv:0.5.7`** — version drift. Railway builds resolve deps with a different uv than ECS builds. Should match.
- **`--no-dev`** — correct for prod (strips pytest/mypy).
- **`printf '\nPATH=/opt/venv/bin:$PATH' >> /root/.profile`** — fragile; relies on Railway sourcing `.profile`. The `railway.json` start command uses the absolute `/opt/venv/bin/uvicorn` path, so this is belt-and-suspenders.
- Only builds the `api` package — dashboard has its own nixpacks config (or uses `pnpm start` per `railway-production.json`).

### Pre-commit (`.pre-commit-config.yaml`)

- **Hooks**: trailing-whitespace (excludes `.md`), end-of-file-fixer, check-yaml, check-toml, check-added-large-files (1MB), check-merge-conflict, detect-private-key, mixed-line-ending (LF), ruff (fix + exit-non-zero-on-fix), ruff-format, mypy (with pydantic/types-redis deps, scoped to `libs|services`), gitleaks.
- **All hooks pinned to release tags** (v5.0.0, v0.7.4, v1.13.0, v8.21.2) — not SHAs, but tags are acceptable for pre-commit (less strict than CI actions).
- **mypy uses `--config-file=mypy.ini`** — `mypy.ini` is referenced but not in the audited file list; if it's missing mypy will error. (Likely exists; not audited here.)
- **gitleaks** runs both in pre-commit and CI — defense in depth.
- **`check-added-large-files` at 1MB** — catches accidental model/binary commits.
- Solid config. No issues.

### Gitleaks (`.gitleaks.toml`)

- Minimal allowlist with one regex (`sk_live_1234567890abcdef`) for fake redaction-test tokens.
- Appropriate — keeps the allowlist tiny.

### Python Config (`pyproject.toml`, `ruff.toml`)

**`pyproject.toml`**
- uv workspace with 17 members (5 libs + 12 services). `tool.uv.sources` maps workspace members.
- `requires-python = ">=3.12"`.
- Dev dependency group: pytest, pytest-asyncio, pytest-cov, ruff, mypy, pre-commit, pydantic, types-redis.
- `[tool.pytest.ini_options]`: `asyncio_mode = "auto"`, `filterwarnings = ["error"]` (strict — warnings become errors), `addopts` excludes `long`, `gpu`, `live` markers by default. Markers documented.
- `[tool.ruff.lint]` ignores `B008` (FastAPI `Depends()` defaults — well-justified).
- `[tool.coverage]` branch coverage on the 4 core libs, omits tests/migrations.

**`ruff.toml`**
- Line length 100, py312 target.
- Selects E/F/I/UP/B/SIM/RUF/ASYNC/S/T20 — broad, security-aware (bandit + no-print).
- Per-file ignores: tests/scripts/notebooks allow `T20` (print), tests allow `S`.
- isort `known-first-party` lists all workspace packages.
- Format: double quotes, spaces.

**What's Optimally Implemented**
- Strict warning policy (`filterwarnings = ["error"]`) — no silent deprecations.
- Marker-based test segregation (`long`/`gpu`/`live`) wired into both `pyproject.toml` and `nightly.yml`.
- Coverage on core libs with branch coverage.
- Ruff config is thoughtful (B008 justification, per-file ignores for scripts/tests).

**What Needs Work**
- `tool.coverage.run.source` only covers the 4 core libs (`fincept_core`, `fincept_bus`, `fincept_db`, `fincept_tools`) — services (api, orchestrator, quant_foundry, etc.) have **no coverage tracking**. Given the trading-critical nature of the services, this is a gap.
- `mypy.ini` referenced but not audited — can't confirm type strictness settings.
- No `[tool.mypy]` section in `pyproject.toml` — config is split across files.

---

## CI/CD Pipeline

Four workflows in `.github/workflows/`:

### `ci.yml` (main + PRs)
- **Concurrency** cancels in-progress runs on the same ref — saves CI minutes.
- **Least-privilege** `permissions: contents: read`.
- **Jobs**: `py-lint-typecheck` (ruff check + format check + mypy), `py-test` (pytest + coverage with redis + postgres services, Alembic upgrade first), `js-lint-typecheck-test` (pnpm lint/typecheck/test/build), `security` (gitleaks with full history), `receipt-runner` (runs `verification-receipt.ps1` via pwsh, uploads receipt), `startup-safety-matrix` (runs `test_startup_safety_matrix.py`), `lockfile-sync` (uv lock --check + pnpm --frozen-lockfile).
- **All third-party actions pinned to commit SHAs** (checkout, setup-uv, setup-node, pnpm/action-setup, upload-artifact, gitleaks) — excellent supply-chain hygiene.
- **Pytest exit-code 5 handling** (no tests collected → notice + pass) — graceful for scaffold phase.
- **Service containers** (redis:7-alpine, timescale/timescaledb:latest-pg16) with healthchecks — tests run against real datastores.

### `build-images.yml` (push to main + workflow_dispatch)
- **Matrix builds 6 ECS images** (ingestor, agents, api, orchestrator, risk, oms) + **2 RunPod images** (quant-foundry-training, quant-foundry-inference).
- **Path filters** — only runs when `services/**`, `infra/docker/**`, `runpod/**`, or the workflow itself changes.
- **`fail-fast: false`** — one image failure doesn't cancel the others.
- **GHCR push** with `:latest` + `:${{ github.sha }}` tags, GHA cache (`type=gha`).
- **`GIT_SHA` build-arg** passed to RunPod builds.
- **Dockerfile-exists check** — gracefully skips matrix entries without a Dockerfile (ingestor, agents).
- **`packages: write`** only — least privilege.

**Gap**: No `:latest` tag immutability (GHCR allows mutable tags). No image vulnerability scan (Trivy) on the built images — `nightly.yml` scans the filesystem, not the images. No SBOM, no image signing (cosign).

### `aws-iac-validate.yml` (PRs/pushes touching `infra/aws/**` or `infra/docker/**`)
- **Parallel jobs**: `fmt-and-validate` (terraform fmt -check + init + validate), `tflint` (recursive), `tfsec` (SARIF output, uploaded to GitHub code scanning), `plan` (mock creds, posts PR comment), `docker-lint` (best-effort grep for USER/HEALTHCHECK/EXPOSE).
- **Plan job** uses `environment: aws-plan` (GitHub deployment environment) and skips gracefully if AWS creds are missing.
- **PR comment** with plan summary (truncated to 60KB) — visible diff in the PR.
- **Docker lint is `continue-on-error: true`** — warns but doesn't fail. Reasonable for the scaffold phase but should graduate to failing.
- **All actions SHA-pinned.**

**Gap**: `tfsec` is deprecated in favor of `trivy config` — the action still works but won't receive updates. The docker-lint is a grep, not hadolint — misses many best practices.

### `nightly.yml` (cron `0 7 * * *` + workflow_dispatch)
- **`long-tests`** — runs `pytest -m "long"` with 60-min timeout, redis + postgres services.
- **`vuln-scan`** — Trivy filesystem scan (CRITICAL + HIGH, exit 1, ignore-unfixed).
- **`pip-audit`** — `pip-audit` on the synced env with one ignored vuln (`GHSA-4xh5-x5gv-qwph`).
- **No `security-events: write`** — can't upload SARIF to the Security tab (noted in a comment).

**Gap**: No nightly image rebuild + scan. No dependency update PR (Dependabot/Renovate not visible).

### Overall CI/CD Assessment
- **Excellent action pinning discipline** — every `uses:` is a 40-char SHA, not a tag. Supply-chain-hardened.
- **Comprehensive gates**: lint, typecheck, tests, coverage, secret scan, receipt, startup safety, lockfile sync, IaC validate, image build, nightly vuln scan.
- **Gaps**: no image-level Trivy scan, no SBOM, no cosign, no Dependabot, tfsec deprecated, docker-lint is best-effort grep, no automated RunPod E2E in CI (manual only).

---

## Recommendations Summary

### Critical (will break production)
1. **Fix ALB access-log S3 bucket policy** — add a bucket policy granting `elasticloadbalancing.amazonaws.com` `s3:PutObject` on `alb-access-logs/*`, or move ALB logs to a separate non-object-lock bucket. (`infra/aws/alb_waf.tf` + `s3.tf`)
2. **Fix `REDIS_URL` ECS secret mapping** — inject a full `rediss://:token@host:6379/0` URL, not the bare auth token. (`infra/aws/ecs.tf` line 49)
3. **Fix `COPY apps apps 2>/dev/null || true`** in `api`, `orchestrator`, `oms`, `risk` Dockerfiles — invalid Dockerfile syntax. (`infra/docker/*.Dockerfile`)
4. **Fix dashboard `NEXT_PUBLIC_API_URL`** — bake at build time via build-arg or switch to server-side API routes. (`infra/docker/dashboard.Dockerfile` + `infra/aws/ecs.tf`)
5. **Fix WAF rate-limit value** — 2000 contradicts the "100 req / 5 min" intent. (`infra/aws/alb_waf.tf` line 282)

### High (security/correctness)
6. **Fail closed on missing `QUANT_FOUNDRY_CALLBACK_SECRET`** in RunPod handlers when in prod mode. (`runpod/quant-foundry-training/handler.py`, `runpod/quant-foundry-inference/handler.py`)
7. **Add `USER 1001` + `HEALTHCHECK`** to both RunPod Dockerfiles. (`runpod/quant-foundry-*/Dockerfile`)
8. **Add input validation + error envelope to inference handler** — mirror the training handler's `isinstance` checks and broad exception handling. (`runpod/quant-foundry-inference/handler.py`)
9. **Add `if __name__ == "__main__":` guard to `recreate_endpoints.py`** — currently runs destructive logic on import. (`scripts/recreate_endpoints.py`)
10. **Pin RunPod container deps with a lockfile** — current `pip install >=` ranges are non-reproducible; the `lockfile_hash` reproducibility claim is unbacked.
11. **Centralize hardcoded RunPod endpoint/template/volume IDs** — duplicated across 5+ files. (`railway-production.json`, `rebuild_runpod_containers.py`, `verify_runpod_containers.py`, `e2e_runpod_real_ml.py`, `recreate_endpoints.py`)

### Medium (consistency / maintainability)
12. **Align `nixpacks.toml` uv version (0.4.30) with Dockerfile uv version (0.5.7)**.
13. **Extract a shared `scripts/lib/runpod_client.py`** to eliminate ~500 lines of duplicated GraphQL/REST boilerplate and the `api.runpod.ai` vs `api.runpod.io` inconsistency.
14. **Fix `docker-compose.yml` header comment** — references nonexistent `infra/k8s/`; production uses ECS.
15. **Add a repo-root `.dockerignore`** — currently the full monorepo (including `.git`, `apps/`, `node_modules`) is sent as Docker build context.
16. **Use SSE-KMS (customer-managed key) for S3 audit buckets** instead of SSE-S3 (AES256) — the `s3.tf` header claims KMS but the code uses AES256.
17. **Split `ecs_task_s3` IAM policy per service** — api (read-only) vs orchestrator (write).
18. **Add ECS autoscaling policies** — the autoscale role exists but no `aws_appautoscaling_policy` is attached.
19. **Extend coverage `source` to include services** — currently only the 4 core libs are tracked.
20. **Remove misleading OMS Dockerfile hardening comment** (claims cap-drop/read-only/no-new-privileges that aren't set) or implement them in the ECS task definition.
21. **Pin `minio/minio:latest` and `timescale/timescaledb:latest-pg16`** in docker-compose to reproducible tags.

### Low (polish)
22. **Add image-level Trivy scan + SBOM + cosign signing** to `build-images.yml`.
23. **Migrate `tfsec` → `trivy config`** in `aws-iac-validate.yml`.
24. **Graduate docker-lint from `continue-on-error: true` to failing** (or switch to hadolint).
25. **Add `--check` (plan-like) mode to `deploy_runpod_endpoints.py`**.
26. **Remove unused `context_rel` field** in `rebuild_runpod_containers.py` `CONTAINERS` dict.
27. **Port the training handler's debug-logging `__main__` block** to the inference handler.
28. **Add OCI image labels** to all Dockerfiles for provenance.
29. **Make the Terraform S3 backend mandatory** (uncomment + require operator config) to prevent accidental local state.
30. **Add a `runpodctl` CLI** consolidating the ~15 ad-hoc probe/check/recycle scripts.
