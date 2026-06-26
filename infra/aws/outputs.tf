###############################################################################
# outputs.tf — operator-facing outputs (TASK-0903)
#
# All outputs are scrubbed: no secret values, no internal IPs that change
# per-deploy. Use these for `terraform output -json` and the deployment
# verification receipt (see docs/AWS_DEPLOY_RUNBOOK.md).
###############################################################################

# --- Identity / region --------------------------------------------------

output "aws_region" {
  description = "Region the stack was deployed to."
  value       = var.aws_region
}

output "environment" {
  description = "Environment name."
  value       = var.environment
}

output "aws_account_id" {
  description = "AWS account ID the stack runs in."
  value       = data.aws_caller_identity.current.account_id
}

# --- Network ------------------------------------------------------------

output "vpc_id" {
  description = "VPC ID."
  value       = aws_vpc.main.id
}

output "public_subnet_ids" {
  description = "Public subnet IDs (ALB)."
  value       = aws_subnet.public[*].id
}

output "private_subnet_ids" {
  description = "Private subnet IDs (ECS tasks)."
  value       = aws_subnet.private[*].id
}

output "database_subnet_ids" {
  description = "Database subnet IDs (RDS, ElastiCache)."
  value       = aws_subnet.database[*].id
}

# --- ALB / DNS ----------------------------------------------------------

output "alb_dns_name" {
  description = "ALB DNS name (CNAME target)."
  value       = aws_lb.main.dns_name
}

output "alb_arn" {
  description = "ALB ARN (referenced by alarms + dashboard)."
  value       = aws_lb.main.arn
}

output "api_target_group_arn" {
  description = "API target group ARN."
  value       = aws_lb_target_group.api.arn
}

output "dashboard_target_group_arn" {
  description = "Dashboard target group ARN."
  value       = aws_lb_target_group.dashboard.arn
}

output "waf_web_acl_arn" {
  description = "WAF Web ACL ARN."
  value       = local.acm_certificate_arn != "" ? aws_wafv2_web_acl.main[0].arn : null
}

# --- ECR ---------------------------------------------------------------

output "ecr_repository_urls" {
  description = "Map of service name -> ECR repository URL."
  value = {
    for r in var.ecr_repositories : r.name => aws_ecr_repository.service[r.name].repository_url
  }
}

# --- ECS ---------------------------------------------------------------

output "ecs_cluster_name" {
  description = "ECS cluster name."
  value       = aws_ecs_cluster.main.name
}

output "ecs_cluster_arn" {
  description = "ECS cluster ARN."
  value       = aws_ecs_cluster.main.arn
}

output "ecs_service_names" {
  description = "ECS service names."
  value       = local.ecs_services
}

# --- RDS ---------------------------------------------------------------

output "rds_endpoint" {
  description = "RDS endpoint (host:port)."
  value       = aws_db_instance.main.endpoint
  sensitive   = false # endpoint itself is not a secret; the password is
}

output "rds_database_name" {
  description = "Initial database name."
  value       = aws_db_instance.main.db_name
}

output "rds_arn" {
  description = "RDS instance ARN."
  value       = aws_db_instance.main.arn
}

# --- ElastiCache -------------------------------------------------------

output "elasticache_primary_endpoint" {
  description = "ElastiCache primary endpoint."
  value       = aws_elasticache_replication_group.main.primary_endpoint_address
}

output "elasticache_reader_endpoint" {
  description = "ElastiCache reader endpoint."
  value       = aws_elasticache_replication_group.main.reader_endpoint_address
}

# --- S3 buckets --------------------------------------------------------

output "s3_bucket_names" {
  description = "Map of bucket suffix -> bucket name."
  value       = local.bucket_names
}

# --- Secrets (names only, NOT values) ----------------------------------

output "secrets_arns" {
  description = "Map of secret name -> Secrets Manager ARN (no values)."
  value = {
    for s in var.secrets : s.name => aws_secretsmanager_secret.main[s.name].arn
  }
}

# --- CloudWatch -------------------------------------------------------

output "sns_alarm_topic_arn" {
  description = "SNS topic ARN for CloudWatch alarms."
  value       = aws_sns_topic.alarms.arn
}

output "cloudwatch_dashboard_name" {
  description = "CloudWatch dashboard name."
  value       = aws_cloudwatch_dashboard.main.dashboard_name
}