###############################################################################
# rds.tf — managed Postgres with TimescaleDB extension (TASK-0903)
#
# Configuration per design doc:
# - Multi-AZ for prod, single-AZ for staging
# - TimescaleDB extension via shared_preload_libraries
# - Encryption at rest (KMS, customer-managed key for audit integrity)
# - Automated backups with 35-day retention for PITR
# - Connection pooling recommended via RDS Proxy (out of scope for MVP)
###############################################################################

# DB subnet group (uses the isolated database subnets)
resource "aws_db_subnet_group" "main" {
  name       = "${local.name_prefix}-db-subnets"
  subnet_ids = aws_subnet.database[*].id

  tags = merge(local.common_tags, {
    Name = "${local.name_prefix}-db-subnets"
  })
}

# DB parameter group with TimescaleDB loaded
resource "aws_db_parameter_group" "main" {
  name        = "${local.name_prefix}-pg-finctep"
  family      = "postgres16"
  description = "Postgres parameter group with TimescaleDB shared preload"

  parameter {
    name         = "shared_preload_libraries"
    value        = "timescaledb"
    apply_method = "pending-reboot"
  }

  parameter {
    name  = "log_min_duration_statement"
    value = "1000" # log queries > 1s
  }

  tags = local.common_tags
}

# Option group — TimescaleDB isn't a standard option group entry on RDS,
# but enabling the shared_preload_libraries parameter is sufficient for the
# TimescaleDB extension to be available at CREATE EXTENSION time.
resource "aws_db_option_group" "main" {
  name                 = "${local.name_prefix}-pg-options"
  engine_name          = "postgres"
  major_engine_version = "16"

  tags = local.common_tags
}

# RDS instance
resource "aws_db_instance" "main" {
  identifier        = "${local.name_prefix}-pg"
  engine            = "postgres"
  engine_version    = var.rds_engine_version
  instance_class    = var.rds_instance_class
  allocated_storage = var.rds_allocated_storage_gb
  storage_type      = "gp3"
  storage_encrypted = true
  kms_key_id        = aws_kms_key.secrets.arn

  db_name  = var.rds_database_name
  username = var.rds_master_username
  password = data.aws_secretsmanager_secret_version.db_password.secret_string

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.rds.id]
  parameter_group_name   = aws_db_parameter_group.main.name
  option_group_name      = aws_db_option_group.main.name

  multi_az            = var.environment == "prod"
  publicly_accessible = false

  backup_retention_period   = var.rds_backup_retention_days
  backup_window             = "03:00-04:00" # UTC, low traffic
  maintenance_window        = "Mon:04:00-Mon:05:00"
  copy_tags_to_snapshot     = true
  deletion_protection       = var.environment == "prod"
  skip_final_snapshot       = false
  final_snapshot_identifier = "${local.name_prefix}-pg-final"

  enabled_cloudwatch_logs_exports = ["postgresql", "upgrade"]
  auto_minor_version_upgrade      = true

  tags = local.common_tags
}

# Read the current DB password secret version. Created in secrets.tf.
data "aws_secretsmanager_secret_version" "db_password" {
  secret_id = aws_secretsmanager_secret.main["fincept/db-password"].arn
}