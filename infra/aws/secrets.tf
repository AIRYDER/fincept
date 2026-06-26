###############################################################################
# secrets.tf — Secrets Manager placeholders + rotation (TASK-0903)
#
# Initial values are intentionally NOT stored in Terraform state. Operators
# pass real values at apply time via TF_VAR_secrets environment variable or
# a separate `terraform apply -var-file=secrets.auto.tfvars` file (gitignored).
#
# Per design doc, rotation is enabled for database credentials via a Lambda
# rotation function (out of scope for MVP — apply a stub rotation config).
###############################################################################

# KMS key for Secrets Manager encryption (customer-managed).
resource "aws_kms_key" "secrets" {
  description             = "KMS key for Fincept secrets"
  enable_key_rotation     = true
  deletion_window_in_days = 30

  tags = merge(local.common_tags, {
    Name = "${local.name_prefix}-secrets-kms"
  })
}

resource "aws_kms_alias" "secrets" {
  name          = "alias/${local.name_prefix}-secrets"
  target_key_id = aws_kms_key.secrets.key_id
}

resource "aws_secretsmanager_secret" "main" {
  for_each = { for s in var.secrets : s.name => s }

  name                    = each.value.name
  description             = each.value.description
  kms_key_id              = aws_kms_key.secrets.arn
  recovery_window_in_days = 30

  tags = merge(local.common_tags, {
    Name = each.value.name
  })
}

# Initial secret version. The value is taken from the variable list and is
# sensitive; it WILL appear in the Terraform state file (encryption-at-rest
# via S3 backend is recommended for tfstate).
resource "aws_secretsmanager_secret_version" "main" {
  for_each = aws_secretsmanager_secret.main

  secret_id     = each.value.id
  secret_string = try(each.value.initial_value, "REPLACE_ME_AT_APPLY_TIME")

  # Lifecycle: ignore drift when operator rotates the secret out-of-band.
  lifecycle {
    ignore_changes = [secret_string]
  }
}