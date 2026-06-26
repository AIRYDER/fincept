###############################################################################
# locals.tf — derived names + computed values (TASK-0903)
###############################################################################

locals {
  name_prefix = "fincept-${var.environment}"

  common_tags = {
    Project     = "fincept-terminal"
    Component   = "production-control-plane"
    ManagedBy   = "terraform"
    Environment = var.environment
    Owner       = var.owner_contact
    CostCenter  = var.cost_center
  }

  # Bucket name pattern: fincept-<env>-<suffix>. Lowercase, hyphenated, DNS-safe.
  bucket_names = {
    for b in var.s3_buckets :
    b.name_suffix => "${local.name_prefix}-${b.name_suffix}"
  }

  # ECR repo names: lowercase + prefix.
  ecr_repo_names = [
    for r in var.ecr_repositories :
    "${local.name_prefix}-${r.name}"
  ]

  # AZ selection: take the first N AZs from the region.
  azs = slice(data.aws_availability_zones.available.names, 0, var.az_count)

  # VPC subnet plan:
  #   /20 public  subnets (ALB only)
  #   /20 private subnets (ECS tasks)
  #   /20 isolated db subnets (RDS, ElastiCache) — no internet route
  public_subnet_cidrs  = [for i in range(var.az_count) : cidrsubnet(var.vpc_cidr, 8, 0 + i)]
  private_subnet_cidrs = [for i in range(var.az_count) : cidrsubnet(var.vpc_cidr, 8, 16 + i)]
  database_subnet_cidrs = [for i in range(var.az_count) : cidrsubnet(var.vpc_cidr, 8, 32 + i)]

  # Whether to manage Route53 + ACM for the domain.
  manage_dns = var.domain_name != ""

  # ECS service names.
  ecs_services = {
    api          = "${local.name_prefix}-api"
    dashboard    = "${local.name_prefix}-dashboard"
    orchestrator = "${local.name_prefix}-orchestrator"
  }
}