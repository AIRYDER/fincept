# AWS Production Control Plane — Operator Runbook

**Task:** TASK-0903 (operational staging)
**Date:** 2026-06-25
**Status:** IaC scaffolding staged, **not applied**.
**Scope:** Operator-only. No agent runs `terraform apply` without explicit human authorization.

This runbook is the operator-facing companion to
[`AWS_PRODUCTION_CONTROL_PLANE.md`](./AWS_PRODUCTION_CONTROL_PLANE.md) (the
design) and the [`infra/aws/`](../infra/aws/) directory (the IaC).

---

## 1. Pre-flight checklist

Complete every step before running `terraform apply`. Skipping any of these
is the single most common source of AWS deployment incidents.

### 1.1 Account + credentials

- [ ] AWS account is provisioned and billing is active.
- [ ] IAM user/role has `AdministratorAccess` (or the operator-chosen scoped
  policy — never use the root account for day-to-day ops).
- [ ] AWS CLI v2 installed and configured: `aws configure` or SSO profile.
- [ ] Terraform 1.9.x installed locally (`terraform version`).
- [ ] `tflint` + `tfsec` installed (recommended) — both surface common
  security issues pre-apply.

### 1.2 Quotas (request increases if needed)

- [ ] `Running On-Demand Fargate vCPUs` — default 100; raise to 1000.
- [ ] `Running On-Demand Fargate Memory (GB)` — default 4096; raise to 8000.
- [ ] `EC2-VPC Elastic IPs` — default 5; raise to 10 (NAT EIP + spare).
- [ ] `RDS DB Instances` — default 40; raise to 100.
- [ ] `ElastiCache Replication Groups` — default 50.
- [ ] `Application Load Balancers` — default 50.
- [ ] `Secrets Manager secrets` — default 100.

### 1.3 Domain + DNS (optional but recommended)

- [ ] Domain is registered and a Route53 hosted zone exists (or skip DNS
  and use the ALB-provided DNS name).
- [ ] If `var.domain_name` is supplied: ACM cert validation will use DNS
  records automatically created in the hosted zone.

### 1.4 Image registry

- [ ] Container images built and pushed to ECR before the first ECS service
  is started. Tags MUST follow semver (`v1.0.0`) for the lifecycle policy
  to keep them.
- [ ] Image scans run automatically on push (already enabled in IaC).

### 1.5 Secret material

Prepare the initial secret values. **Never commit these.** Pass via
`TF_VAR_secrets` or `-var-file=secrets.auto.tfvars` (the file MUST be
gitignored).

| Secret name                     | Notes                                         |
|---------------------------------|-----------------------------------------------|
| `fincept/callback-secret`       | 32+ random bytes (HMAC). `openssl rand -hex 32` |
| `fincept/jwt-signing-key`       | 32+ random bytes (HMAC). `openssl rand -hex 32` |
| `fincept/runpod-api-key`        | From RunPod account dashboard                 |
| `fincept/db-password`           | 24+ chars, alphanumeric + symbols             |
| `fincept/redis-auth-token`      | 32+ random bytes                              |
| `fincept/openai-api-key`        | From OpenAI dashboard                         |
| `fincept/anthropic-api-key`     | From Anthropic dashboard                      |

Example secrets.auto.tfvars (GITIGNORED):

```hcl
secrets = [
  { name = "fincept/callback-secret",   description = "Quant Foundry HMAC callback secret", initial_value = "REPLACE_ME" },
  # ... etc
]
```

After the first apply, rotate the initial values via Secrets Manager
console/CLI. Terraform will NOT update them after the initial write
(`lifecycle.ignore_changes = [secret_string]` in `secrets.tf`).

---

## 2. First-time apply

### 2.1 Clone + init

```bash
git clone <repo>
cd infra/aws
terraform init
```

### 2.2 Format + validate

```bash
terraform fmt -recursive -check
terraform validate
```

Both MUST exit 0. CI runs the same checks; failures block merge.

### 2.3 Plan

```bash
terraform plan -out=tfplan \
  -var="domain_name=terminal.example.com" \
  -var-file=secrets.auto.tfvars
```

Review the plan. Pay particular attention to:

- **No `Delete` actions on existing S3 buckets with data** — if you see
  `aws_s3_bucket.main["tfstate"]` being recreated, the remote state backend
  is misconfigured.
- **No policy statements that grant `*:*` on `*`** — tfsec should catch
  this; manual review is the second line of defense.
- **No secret values appear in the plan output** — if you see a string that
  looks like a password or token, stop. The Secrets Manager placeholders
  should be `REPLACE_ME_AT_APPLY_TIME` until rotated.

### 2.4 Apply

```bash
terraform apply tfplan
```

Expected resource count (prod, default sizing): ~80 resources. Duration:
~10-15 minutes for cold VPC + RDS + ElastiCache; ~2 minutes for incremental
updates.

### 2.5 Capture outputs

```bash
terraform output -json > outputs.json
```

Save `outputs.json` (gitignored) to a password manager / secret store. The
identifiers (ARNs, DNS names, IDs) are not secret themselves but they are
the keys to the deployment.

---

## 3. Post-apply verification

A deployment is NOT "done" until every check below passes. Each check has
a corresponding test in `reports/verification/aws-production-deployment-<date>.md`
(the receipt).

### 3.1 Network

- [ ] `aws ec2 describe-vpcs --vpc-ids $(terraform output -raw vpc_id)` —
  VPC exists, DNS hostnames + DNS support enabled.
- [ ] `aws ec2 describe-flow-logs --filter "Name=resource-id,Values=$(terraform output -raw vpc_id)"` —
  flow logs active for REJECT traffic, target log group = `/fincept/<env>/vpc/flow-logs`.
- [ ] From an ECS task, `curl -I https://api.runpod.ai/v2/health` should
  succeed (NAT egress works).

### 3.2 Secrets

- [ ] `aws secretsmanager list-secrets --filters Key=name,Values=fincept/` —
  every expected secret exists.
- [ ] `aws secretsmanager get-secret-value --secret-id fincept/callback-secret` —
  returns the rotated value, not the `REPLACE_ME_AT_APPLY_TIME` placeholder.
- [ ] IAM policy simulator: the ECS task role can `secretsmanager:GetSecretValue`
  on every `arn:aws:secretsmanager:*:*:secret:fincept/*`.

### 3.3 S3 buckets

- [ ] `aws s3api get-bucket-versioning --bucket <bucket>` returns
  `Status: Enabled` for every bucket.
- [ ] `aws s3api get-object-lock-configuration --bucket fincept-prod-receipts`
  returns `ObjectLockEnabled: Enabled` and a COMPLIANCE rule.
- [ ] `aws s3api get-bucket-policy --bucket fincept-prod-receipts` denies
  insecure transport (`aws:SecureTransport = false`).
- [ ] No bucket policy contains `Principal: "*"` with `Action: "*"` on
  `Resource: "*"` (this would be public-by-default).

### 3.4 ECS

- [ ] `aws ecs list-services --cluster $(terraform output -raw ecs_cluster_name)`
  returns the 3 services (api, dashboard, orchestrator).
- [ ] `aws ecs describe-services --cluster <cluster> --services api` reports
  `desired_count == running_count` and `deployments[0].status == PRIMARY`.
- [ ] `curl -fsS http://<api-task-ip>:8000/health` (from inside the VPC or
  via ECS Exec) returns `{"ok": true}`.

### 3.5 ALB + WAF

- [ ] `curl -fsSI https://<alb_dns>/health` returns HTTP 200 (ALB listener
  default routes `/api/*` to the API service).
- [ ] `curl -fsSI http://<alb_dns>/health` redirects to HTTPS (HTTP 301).
- [ ] `aws wafv2 get-web-acl --name <waf-name>` returns the managed rules
  + custom rate-limit rule.
- [ ] WAF rate-limit test: send 3000 requests from a single IP in 5 minutes
  and verify HTTP 403 for the excess.

### 3.6 RDS

- [ ] `aws rds describe-db-instances --db-instance-identifier <rds-id>`
  shows `StorageEncrypted: true`, `MultiAZ: true` (prod).
- [ ] `aws rds describe-db-parameter-groups --db-parameter-group-name <pg-name>`
  includes `shared_preload_libraries = timescaledb`.
- [ ] From an ECS task, `psql "postgresql://<user>@<rds-endpoint>:5432/fincept"`
  succeeds. Then `\dx` lists `timescaledb`.

### 3.7 ElastiCache

- [ ] `aws elasticache describe-replication-groups --replication-group-id <id>`
  shows `Status: available`, `MultiAZ: enabled`, `AutomaticFailover: enabled`.
- [ ] `redis-cli -h <primary-endpoint> -p 6379 --tls --auth <token> ping`
  returns `PONG`.

### 3.8 CloudWatch alarms

- [ ] Every alarm in `outputs.sns_alarm_topic_arn` shows `OK` state on the
  CloudWatch console (or `INSUFFICIENT_DATA` with no data yet — expected
  on day 1).
- [ ] SNS topic has at least one email subscriber (operator alarm email).
- [ ] CloudWatch dashboard `$(terraform output -raw cloudwatch_dashboard_name)`
  renders with widgets for API latency, settlement lag, 5xx count, ECS CPU.

### 3.9 No-secrets-in-containers

- [ ] `aws ecs describe-task-definition --task-definition api` JSON output
  does NOT contain any value matching a known secret format (no `Bearer eyJ`,
  no Postgres connection string with a password, no `sk-` OpenAI key).
- [ ] Every credential reference uses `valueFrom: arn:aws:secretsmanager:...`
  in the `secrets` block of the container definition.
- [ ] Run a test task with ECS Exec, `env | grep -i secret` returns no real
  values (placeholder references are OK; raw credentials are not).

### 3.10 OMS / Risk boundary

- [ ] OMS + Risk task definitions are NOT present in this MVP. If you see
  them in `aws ecs list-task-definitions`, they are out-of-scope for v1 and
  must NOT have secrets attached.

### 3.11 Write the deployment receipt

Create `reports/verification/aws-production-deployment-<YYYY-MM-DD>.md`
with:

- Terraform plan output summary (`terraform show -json tfplan | jq ...`).
- Each section above, marked PASS/FAIL with the AWS CLI output pasted in.
- Output of `terraform output -json` (identifiers only, not secrets).
- Sign-off line: "Operator: ____ Date: ____".

---

## 4. Day-2 operations

### 4.1 Deploy a new image

```bash
# 1. Build + tag locally
docker build -t fincept-api:v1.2.3 apps/api

# 2. Push to ECR
aws ecr get-login-password --region us-east-1 | \
  docker login --username AWS --password-stdin \
  $(terraform output -raw ecr_repository_urls | jq -r .api)
docker push $(terraform output -raw ecr_repository_urls | jq -r .api):v1.2.3

# 3. Update the task definition
aws ecs update-service \
  --cluster $(terraform output -raw ecs_cluster_name) \
  --service fincept-prod-api \
  --force-new-deployment
```

The deployment circuit breaker (enabled in `ecs.tf`) auto-rolls back on
repeated task failures.

### 4.2 Rotate a secret

```bash
aws secretsmanager put-secret-value \
  --secret-id fincept/callback-secret \
  --secret-string "$(openssl rand -hex 32)"

# Force ECS to pick up the new value
aws ecs update-service \
  --cluster $(terraform output -raw ecs_cluster_name) \
  --service fincept-prod-api \
  --force-new-deployment
```

### 4.3 Scale a service

```bash
aws ecs update-service \
  --cluster $(terraform output -raw ecs_cluster_name) \
  --service fincept-prod-api \
  --desired-count 4
```

(For autoscale, wire target-tracking on `ECSServiceAverageCPUUtilization` —
out of scope for this MVP.)

### 4.4 Drain + stop a service for maintenance

```bash
# Set desired_count to 0 — ALB drains existing targets gracefully.
aws ecs update-service \
  --cluster $(terraform output -raw ecs_cluster_name) \
  --service fincept-prod-api \
  --desired-count 0

# Wait for running_count == 0
aws ecs wait services-stable \
  --cluster $(terraform output -raw ecs_cluster_name) \
  --services fincept-prod-api
```

### 4.5 Query logs

```bash
# Tail the API log group
aws logs tail /fincept/prod/api --follow
```

---

## 5. Rollback / destroy

### 5.1 Soft rollback (revert a single change)

Re-run `terraform apply` against the previous `tfplan` or commit. The
deployment circuit breaker on the ECS service handles most bad deploys
automatically.

### 5.2 Hard rollback (destroy the stack)

```bash
terraform destroy \
  -var="domain_name=terminal.example.com" \
  -var-file=secrets.auto.tfvars
```

**WARNING:** Destruction is non-recoverable for the audit buckets
(receipts, dossiers, settlements, tfstate) because of the
COMPLIANCE-mode object lock. To destroy those, first disable object lock
(or wait out the retention period). The `final_snapshot_identifier`
on the RDS instance retains the last snapshot — restore from it if
needed.

### 5.3 Nuclear option

If Terraform state is corrupted:

1. Manually delete every resource in the AWS console (ALB → ECS cluster
   → RDS → ElastiCache → S3 → VPC). **Audit buckets will refuse to delete
   during the object-lock retention period.**
2. Delete the S3 bucket holding the remote state (after the tfstate bucket
   retention window expires).
3. Re-apply from scratch via this runbook.

The expected recovery time is ~30 minutes (resources) + retention window
(audit buckets cannot be deleted for the full year).

---

## 6. Known gaps (out of scope for this MVP)

These items are intentionally NOT included and must be addressed in a
follow-up task:

- [ ] **Auto-scaling policies** on the API service (target-tracking on
  CPU + memory).
- [ ] **RDS Proxy** for connection pooling (current config can exhaust
  connections under scale-out).
- [ ] **Secrets Manager rotation Lambda** for the DB password.
- [ ] **CodePipeline** for image CI/CD (currently operator-driven via
  `docker push`).
- [ ] **Cost anomaly detection** alarms (AWS Cost Anomaly Detection API).
- [ ] **Disaster recovery** (cross-region snapshot copy for the RDS
  instance, S3 cross-region replication for audit buckets).
- [ ] **EKS / k8s migration** if service count grows past ~20.
- [ ] **GPU workloads** on AWS (RunPod remains the GPU provider per
  design doc; AWS GPU is 3-5x more expensive).

---

## 7. References

- [`docs/AWS_PRODUCTION_CONTROL_PLANE.md`](./AWS_PRODUCTION_CONTROL_PLANE.md) — design doc
- [`infra/aws/README.md`](../infra/aws/README.md) — IaC file map
- [`infra/aws/`](../../infra/aws/) — Terraform module
- `docs/AAA_GLM_SUPERTEAM_LOGS/BUILDER2.md` — TASK-0306 (gateway surface
  that this deployment hosts)
- `libs/fincept-core/src/fincept_core/config.py` — runtime safety guard
  (must pass in the ECS task environment)
- `libs/fincept-bus/src/fincept_bus/streams.py` — Redis stream names
  (ElastiCache must support all streams)