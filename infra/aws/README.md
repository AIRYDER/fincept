###############################################################################
# README.md — AWS Production Control Plane (TASK-0903)
#
# Stage: scaffolding + operator tooling ready. `terraform plan` works;
# `terraform apply` requires real AWS credentials and explicit operator
# authorization (see docs/AWS_DEPLOY_RUNBOOK.md).
###############################################################################

# Fincept Terminal — AWS Production Control Plane (TASK-0903)

Terraform scaffolding for the production deployment described in
[`docs/AWS_PRODUCTION_CONTROL_PLANE.md`](../../docs/AWS_PRODUCTION_CONTROL_PLANE.md),
plus the operator tooling (scripts + CI) that gates and verifies it.

## What this deploys

### Infrastructure-as-Code (`infra/aws/`)

| Resource group | Module file      | Notes                                                          |
|----------------|------------------|----------------------------------------------------------------|
| Provider / TF  | `providers.tf`   | Pinned versions, default tags, optional S3 backend             |
| Variables      | `variables.tf`   | All operator-tunable inputs                                    |
| Locals         | `locals.tf`      | Derived names + CIDR plan                                      |
| Lookups        | `data.tf`        | Account / region / AZ / DNS                                    |
| Network        | `network.tf`     | VPC + 3 tier subnets + NAT + flow logs                         |
| IAM            | `iam.tf`         | Execution + task + autoscaling roles                           |
| ECR            | `ecr.tf`         | Immutable image repos with scan-on-push + lifecycle             |
| S3             | `s3.tf`          | Versioned + object-lock audit buckets, artifacts to Glacier    |
| Secrets        | `secrets.tf`     | Secrets Manager placeholders + KMS CMK                         |
| CloudWatch     | `cloudwatch.tf`  | Log groups + alarms + operator dashboard                       |
| ECS            | `ecs.tf`         | Fargate cluster + api / dashboard / orchestrator services       |
| RDS            | `rds.tf`         | Postgres + TimescaleDB (parameter group)                       |
| ElastiCache    | `elasticache.tf` | Valkey cluster (noeviction policy)                             |
| ALB + WAF      | `alb_waf.tf`     | ALB, HTTPS listener, WAF managed rules, Route53 alias          |
| Outputs        | `outputs.tf`     | Operator-facing identifiers (no secret values)                 |

### Container images (`infra/docker/`)

Multi-stage, non-root, healthcheck-enabled production Dockerfiles for the
five ECS services. Pinned via `uv.lock` (Python) or `pnpm-lock.yaml` (Node).

| Dockerfile                                | Service           | Notes                                                                |
|-------------------------------------------|-------------------|----------------------------------------------------------------------|
| `infra/docker/api.Dockerfile`             | FastAPI HTTP/WS   | Reads from Redis + Postgres + S3, no broker creds                    |
| `infra/docker/dashboard.Dockerfile`       | Next.js operator  | Standalone build, served by ALB path `/`                             |
| `infra/docker/orchestrator.Dockerfile`    | Stream consumer   | Emits Decisions + OrderIntents                                       |
| `infra/docker/oms.Dockerfile`             | Order Management  | **Reserved, NOT deployed in v1** — Railway staging is the source of truth |
| `infra/docker/risk.Dockerfile`            | Pre-trade checks  | **Reserved, NOT deployed in v1**                                     |

The OMS and Risk Dockerfiles are intentionally built ahead of need — when
the v1 paper-trading spine graduates from Railway to AWS, the images are
already pinned and CI-validated.

### Operator tooling (`scripts/`)

| Script                                  | Purpose                                                      | Runbook ref |
|-----------------------------------------|--------------------------------------------------------------|-------------|
| `scripts/aws_preflight.ps1`             | Pre-flight checklist (tooling, account, quotas, secrets)     | §1          |
| `scripts/aws_postapply_verify.ps1`      | Post-apply verification harness (§3.1–§3.10)                 | §3          |
| `scripts/aws_receipt.ps1`               | Generate the binding deployment receipt                      | §3.11       |

All three scripts emit timestamped Markdown + JSON receipts under
`reports/verification/`, matching the project's existing
`scripts/verification-receipt.ps1` pattern.

### CI gate (`.github/workflows/`)

| Workflow                                  | Trigger                                                                | What it does                                                                                                              |
|-------------------------------------------|------------------------------------------------------------------------|---------------------------------------------------------------------------------------------------------------------------|
| `.github/workflows/aws-iac-validate.yml`  | PR or push touching `infra/aws/**`, `infra/docker/**`, or itself       | Parallel `fmt + validate`, `tflint`, `tfsec`, `terraform plan` (mock creds); Docker lint warns on missing hardening       |

### Deployment receipts (`reports/verification/`)

| File                                                       | Purpose                                                       |
|------------------------------------------------------------|---------------------------------------------------------------|
| `aws-production-deployment-<YYYY-MM-DD>.md`                | The binding proof-of-deploy artifact (runbook §3.11)         |
| `aws-production-deployment-<YYYY-MM-DD>.json`              | Machine-readable mirror                                       |
| `aws-preflight-<timestamp>.md/json`                        | Pre-flight run record (runbook §1)                            |
| `aws-verify-<timestamp>.md/json`                           | Post-apply verification run record (runbook §3.1–§3.10)       |

The PENDING receipt dated 2026-06-25 records that the deployment framework
is ready but no `terraform apply` has been run yet — see the file for
status.

## What this does NOT deploy

- **No actual `apply` runs from the agent.** This is scaffolding only.
- **No GPU compute on AWS.** RunPod is used for GPU workloads.
- **No multi-region.** Single-region (default `us-east-1`), multi-AZ.
- **No live-trading wiring.** Broker credentials are NOT provisioned.
  `OMS` and `Risk` task definitions are reserved in the registry but not
  deployed — Railway staging remains the source of truth for v1.

## Hard invariants (enforced in code)

- No plaintext secret in any container environment. Every credential is
  injected from Secrets Manager via the ECS task execution role.
- S3 buckets for receipts/dossiers/settlements have **object lock (COMPLIANCE)**.
  They cannot be deleted by anyone, including the root account, for the
  retention window.
- RDS is encrypted at rest with a customer-managed KMS key.
- ElastiCache `maxmemory-policy = noeviction` — the event bus must never
  silently drop events.
- ALB terminates HTTP, redirects to HTTPS, and enforces TLS 1.3 minimum.
- WAF blocks OWASP Top 10 + known bad inputs + IP reputation lists, with
  a 100 req / 5 min rate limit per IP.
- VPC flow logs ship to CloudWatch.
- All production containers run as non-root UID 1001.

## Apply (operator workflow)

The three scripts in `scripts/` implement the runbook end-to-end:

```powershell
# 1. Pre-flight — confirm tooling, account, quotas, secrets, no plaintext leaks
pwsh ./scripts/aws_preflight.ps1 -Profile fincept-prod

# 2. Plan + apply — operator-driven; outputs captured to outputs.json
cd infra/aws
terraform init
terraform plan -out=tfplan -var-file=secrets.auto.tfvars
terraform apply tfplan
terraform output -json > outputs.json
cd ../..

# 3. Post-apply verify — runs every §3 check, writes aws-verify-<ts>.{md,json}
pwsh ./scripts/aws_postapply_verify.ps1 -Profile fincept-prod

# 4. Receipt — binds preflight + verify + outputs into the proof-of-deploy artifact
pwsh ./scripts/aws_receipt.ps1 -OperatorName "Builder 1" -OperatorDate 2026-06-25
```

See [`docs/AWS_DEPLOY_RUNBOOK.md`](../../docs/AWS_DEPLOY_RUNBOOK.md) for
the full prose walkthrough (and the rationale behind every check).

## Cost estimate (per design doc)

~$210-260 / mo always-on (Fargate + RDS + ALB + WAF + ElastiCache + S3 +
ECR + Secrets Manager + CloudWatch + NAT) plus RunPod GPU on-demand
($0.5-2 / hour) when training or inference is active.