###############################################################################
# variables.tf — all operator-tunable inputs (TASK-0903)
#
# Every variable has an explicit type and description. Sensitive values are
# declared `sensitive = true` so they never appear in plan/apply output.
# Defaults intentionally err on the side of cheap + small (one-operator shop).
###############################################################################

variable "aws_region" {
  type        = string
  description = "AWS region for all resources. Single-region deployment (us-east-1 default)."
  default     = "us-east-1"
}

variable "environment" {
  type        = string
  description = "Environment name (prod / staging). Used in resource names and tags."
  default     = "prod"

  validation {
    condition     = contains(["prod", "staging"], var.environment)
    error_message = "environment must be one of: prod, staging."
  }
}

variable "owner_contact" {
  type        = string
  description = "Operator email or team contact for resource tags + alarms."
  default     = "ops@fincept.local"
}

variable "cost_center" {
  type        = string
  description = "Cost-center tag for billing allocation."
  default     = "fincept-platform"
}

variable "domain_name" {
  type        = string
  description = "Public DNS name served by the ALB (e.g. terminal.example.com). Used for ACM cert SAN and ALB listener default rule. Optional — omit to skip Route53/ACM provisioning and use the ALB-provided DNS name."
  default     = ""
}

variable "acm_certificate_arn" {
  type        = string
  description = "Existing ACM certificate ARN for HTTPS listener. If empty and domain_name is set, an ACM cert is requested via DNS validation."
  default     = ""
}

variable "vpc_cidr" {
  type        = string
  description = "VPC CIDR block (RFC1918)."
  default     = "10.40.0.0/16"

  validation {
    condition     = can(cidrhost(var.vpc_cidr, 0))
    error_message = "vpc_cidr must be a valid CIDR."
  }
}

variable "az_count" {
  type        = number
  description = "Number of Availability Zones (2 minimum for prod, 3 for SLA). Used for both public/private subnets."
  default     = 2

  validation {
    condition     = var.az_count >= 2 && var.az_count <= 3
    error_message = "az_count must be 2 or 3."
  }
}

# ---- Container images (ECR repos) ----------------------------------------

variable "ecr_repositories" {
  type = list(object({
    name                  = string
    image_tag_mutability  = optional(string, "IMMUTABLE")
    scan_on_push          = optional(bool, true)
    keep_tagged_count     = optional(number, 10)
    untagged_expire_days  = optional(number, 7)
  }))
  description = "ECR repositories to provision for Fincept services."
  default = [
    { name = "fincept-api" },
    { name = "fincept-dashboard" },
    { name = "fincept-orchestrator" },
    { name = "fincept-oms" },
    { name = "fincept-risk" },
  ]
}

# ---- ECS service sizing --------------------------------------------------

variable "api_task_cpu" {
  type        = number
  description = "API Fargate task CPU units."
  default     = 512
}

variable "api_task_memory" {
  type        = number
  description = "API Fargate task memory (MiB)."
  default     = 1024
}

variable "api_desired_count" {
  type        = number
  description = "API desired task count."
  default     = 2
}

variable "api_container_port" {
  type        = number
  description = "API container port."
  default     = 8000
}

variable "dashboard_task_cpu" {
  type        = number
  description = "Dashboard Fargate task CPU units."
  default     = 512
}

variable "dashboard_task_memory" {
  type        = number
  description = "Dashboard Fargate task memory (MiB)."
  default     = 1024
}

variable "dashboard_desired_count" {
  type        = number
  description = "Dashboard desired task count."
  default     = 2
}

variable "dashboard_container_port" {
  type        = number
  description = "Dashboard container port."
  default     = 3000
}

variable "orchestrator_task_cpu" {
  type        = number
  description = "Orchestrator Fargate task CPU units."
  default     = 512
}

variable "orchestrator_task_memory" {
  type        = number
  description = "Orchestrator Fargate task memory (MiB)."
  default     = 1024
}

variable "orchestrator_desired_count" {
  type        = number
  description = "Orchestrator desired task count."
  default     = 1
}

variable "orchestrator_container_port" {
  type        = number
  description = "Orchestrator container port."
  default     = 8000
}

# ---- Database (RDS + TimescaleDB) ----------------------------------------

variable "rds_instance_class" {
  type        = string
  description = "RDS instance class for the Postgres+TimescaleDB instance."
  default     = "db.t4g.medium"
}

variable "rds_allocated_storage_gb" {
  type        = number
  description = "RDS allocated storage (GB)."
  default     = 40
}

variable "rds_engine_version" {
  type        = string
  description = "Postgres engine version (TimescaleDB extension supported)."
  default     = "16.3"
}

variable "rds_database_name" {
  type        = string
  description = "Initial database name."
  default     = "fincept"
}

variable "rds_master_username" {
  type        = string
  description = "Master DB username (must be non-default per AWS best practice)."
  default     = "fincept_admin"
}

variable "rds_backup_retention_days" {
  type        = number
  description = "RDS automated backup retention (days)."
  default     = 35

  validation {
    condition     = var.rds_backup_retention_days >= 7 && var.rds_backup_retention_days <= 35
    error_message = "RDS backup retention must be between 7 and 35 days."
  }
}

# ---- Cache (ElastiCache / Valkey) ----------------------------------------

variable "elasticache_node_type" {
  type        = string
  description = "ElastiCache node type (Valkey cluster mode disabled for MVP)."
  default     = "cache.t4g.small"
}

variable "elasticache_engine_version" {
  type        = string
  description = "Valkey / Redis engine version."
  default     = "7.2"
}

# ---- S3 buckets ----------------------------------------------------------

variable "s3_buckets" {
  type = list(object({
    name_suffix        = string
    purpose            = string
    enable_object_lock = optional(bool, false)
    lifecycle_to_glacier_days = optional(number, 0)  # 0 = no transition (audit buckets)
  }))
  description = "S3 buckets for Fincept durable storage."
  default = [
    { name_suffix = "receipts",   purpose = "verification receipts (JSONL, immutable)",  enable_object_lock = true,  lifecycle_to_glacier_days = 0 },
    { name_suffix = "dossiers",   purpose = "model dossiers (JSONL, immutable)",          enable_object_lock = true,  lifecycle_to_glacier_days = 0 },
    { name_suffix = "settlements", purpose = "settlement records (JSONL, immutable)",    enable_object_lock = true,  lifecycle_to_glacier_days = 0 },
    { name_suffix = "artifacts",  purpose = "trained model artifacts (binary, hashed)",   enable_object_lock = false, lifecycle_to_glacier_days = 90 },
    { name_suffix = "tfstate",    purpose = "Terraform remote state (locked)",           enable_object_lock = true,  lifecycle_to_glacier_days = 0 },
  ]
}

# ---- Secrets (Secrets Manager) -------------------------------------------

variable "secrets" {
  type = list(object({
    name        = string
    description = string
    initial_value = optional(string, "")  # only used by operator at apply time via TF_VAR_* env; never committed
  }))
  description = "Secrets to provision in AWS Secrets Manager (placeholders; values injected at apply time)."
  default = [
    { name = "fincept/callback-secret",   description = "Quant Foundry HMAC callback secret" },
    { name = "fincept/jwt-signing-key",   description = "API JWT signing key" },
    { name = "fincept/runpod-api-key",    description = "RunPod serverless API key" },
    { name = "fincept/db-password",       description = "RDS master password (initial)" },
    { name = "fincept/redis-auth-token",  description = "ElastiCache auth token" },
    { name = "fincept/openai-api-key",    description = "OpenAI API key (portfolio reports)" },
    { name = "fincept/anthropic-api-key", description = "Anthropic API key (portfolio reports)" },
  ]
  sensitive = true
}

# ---- CloudWatch alarms --------------------------------------------------

variable "api_latency_p95_threshold_ms" {
  type        = number
  description = "API p95 latency alarm threshold (milliseconds)."
  default     = 1500
}

variable "api_error_rate_threshold_pct" {
  type        = number
  description = "API 5xx error rate alarm threshold (percent of total requests)."
  default     = 1.0
}

variable "settlement_lag_seconds_threshold" {
  type        = number
  description = "Settlement lag alarm threshold (seconds since last settlement)."
  default     = 600
}

variable "budget_kill_switch_topic_arn" {
  type        = string
  description = "SNS topic ARN for BudgetGuard kill-switch notifications. Empty disables the alarm wiring."
  default     = ""
}
