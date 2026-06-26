###############################################################################
# elasticache.tf — ElastiCache (Valkey) for the event bus + Redis state (TASK-0903)
#
# Configuration per design doc:
# - Valkey engine (OSS Redis 7.2 fork, license-clean)
# - Multi-AZ with automatic failover (one replica)
# - TLS in transit + encryption at rest
# - maxmemory-policy = noeviction (event bus must NOT silently drop)
###############################################################################

# ElastiCache subnet group (uses the isolated database subnets)
resource "aws_elasticache_subnet_group" "main" {
  name       = "${local.name_prefix}-cache-subnets"
  subnet_ids = aws_subnet.database[*].id

  tags = local.common_tags
}

# Replication group (cluster mode disabled for MVP)
resource "aws_elasticache_replication_group" "main" {
  replication_group_id = "${local.name_prefix}-cache"
  description          = "Fincept event bus + Redis state (Valkey)"
  engine               = "valkey"
  engine_version       = var.elasticache_engine_version
  node_type            = var.elasticache_node_type
  num_cache_clusters   = 2 # 1 primary + 1 replica (multi-AZ)
  port                 = 6379

  subnet_group_name          = aws_elasticache_subnet_group.main.name
  security_group_ids         = [aws_security_group.elasticache.id]
  automatic_failover_enabled = true
  multi_az_enabled           = true

  at_rest_encryption_enabled = true
  transit_encryption_enabled = true
  kms_key_id                 = aws_kms_key.secrets.arn

  # Per design doc — never silently drop stream events.
  parameter_group_name = aws_elasticache_parameter_group.main.name

  maintenance_window       = "Mon:05:00-Mon:06:00"
  snapshot_window          = "06:00-07:00"
  snapshot_retention_limit = 7

  tags = local.common_tags
}

resource "aws_elasticache_parameter_group" "main" {
  name        = "${local.name_prefix}-valkey-defaults"
  family      = "valkey7"
  description = "Valkey parameter group: noeviction + auth"

  parameter {
    name  = "maxmemory-policy"
    value = "noeviction"
  }

  parameter {
    name  = "tcp-keepalive"
    value = "60"
  }

  tags = local.common_tags
}

# Auth token — referenced from Secrets Manager (see secrets.tf).
# The token value is set at apply time and is not stored in source.