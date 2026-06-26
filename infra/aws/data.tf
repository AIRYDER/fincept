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
