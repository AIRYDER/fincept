###############################################################################
# providers.tf — Terraform + provider version pins (TASK-0903)
#
# Provider versions are pinned to a known-compatible range to avoid silent
# upgrade drift. Bump deliberately, then re-run `terraform init -upgrade`
# and re-record receipts in docs/AWS_DEPLOY_RUNBOOK.md.
#
# This file is intentionally minimal: backend, region, and provider versions
# only. All actual resources live in component modules.
###############################################################################

terraform {
  required_version = ">= 1.9.0, < 2.0.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.50"
    }
  }

  # Backend is operator-supplied via -backend-config= or env vars at init time.
  # Defaults to local backend if no backend block is uncommented below.
  # Example (uncomment + supply bucket/key/dynamodb_table at init):
  #
  # backend "s3" {
  #   bucket         = "fincept-tfstate-prod"
  #   key            = "infra/aws/terraform.tfstate"
  #   region         = "us-east-1"
  #   dynamodb_table = "fincept-tfstate-lock"
  #   encrypt        = true
  # }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = "fincept-terminal"
      Component   = "production-control-plane"
      ManagedBy   = "terraform"
      Environment = var.environment
      Owner       = var.owner_contact
      CostCenter  = var.cost_center
    }
  }
}