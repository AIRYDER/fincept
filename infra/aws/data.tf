###############################################################################
# data.tf — read-only lookups (TASK-0903)
###############################################################################

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

data "aws_availability_zones" "available" {
  state = "available"
}

# Optional: Route53 zone for domain_name + ACM DNS validation.
data "aws_route53_zone" "primary" {
  count = local.manage_dns ? 1 : 0

  name         = var.domain_name
  private_zone = false
}

# ACM cert — either use a pre-issued ARN or request a new one via DNS validation.
data "aws_acm_certificate" "existing" {
  count = var.acm_certificate_arn != "" ? 1 : 0

  arn = var.acm_certificate_arn
}

# SSM parameter lookups (e.g. shared services AMI, latest ECS-optimized AMI).
# ECS-optimized AMI is the default on Fargate — no SSM lookup required.
# This block is reserved for future cross-stack lookups.
data "aws_ssm_parameter" "placeholder" {
  count = 0

  name = "/placeholder/parameter"
}