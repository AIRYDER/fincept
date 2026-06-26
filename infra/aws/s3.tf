###############################################################################
# s3.tf — durable buckets for receipts, dossiers, settlements, artifacts (TASK-0903)
#
# Invariants per design doc:
# - Versioning enabled on every bucket
# - Object lock (WORM) enabled on audit-integrity-critical buckets
# - Server-side encryption (SSE-KMS via AWS-managed key — KMS CMK optional)
# - Bucket policy denies non-SSL
# - Lifecycle rules transition artifacts to Glacier after 90 days
###############################################################################

# --- Buckets --------------------------------------------------------------

resource "aws_s3_bucket" "main" {
  for_each = { for b in var.s3_buckets : b.name_suffix => b }

  bucket        = local.bucket_names[each.key]
  force_destroy = false # production guardrail

  tags = merge(local.common_tags, {
    Name    = local.bucket_names[each.key]
    Purpose = each.value.purpose
  })
}

# --- Versioning -----------------------------------------------------------

resource "aws_s3_bucket_versioning" "main" {
  for_each = aws_s3_bucket.main

  bucket = each.value.id

  versioning_configuration {
    status = "Enabled"
  }
}

# --- Object lock (WORM) for audit buckets ---------------------------------

resource "aws_s3_bucket_object_lock_configuration" "main" {
  for_each = { for k, b in aws_s3_bucket.main : k => b if var.s3_buckets[index([for bucket in var.s3_buckets : bucket.name_suffix], k)].enable_object_lock }

  bucket = each.value.id

  rule {
    default_retention {
      mode = "COMPLIANCE" # cannot be overridden, even by root account
      days = 365          # 1 year minimum retention for audit trail
    }
  }

  depends_on = [aws_s3_bucket_versioning.main]
}

# --- Encryption (SSE-S3 by default; upgrade to SSE-KMS via custom CMK if needed) --

resource "aws_s3_bucket_server_side_encryption_configuration" "main" {
  for_each = aws_s3_bucket.main

  bucket = each.value.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

# --- Public access block (deny all public access by default) -------------

resource "aws_s3_bucket_public_access_block" "main" {
  for_each = aws_s3_bucket.main

  bucket = each.value.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# --- SSL-only bucket policy -----------------------------------------------

data "aws_iam_policy_document" "ssl_only" {
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = ["*"]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "ssl_only" {
  for_each = aws_s3_bucket.main

  bucket = each.value.id
  policy = data.aws_iam_policy_document.ssl_only.json

  depends_on = [aws_s3_bucket_public_access_block.main]
}

# --- Lifecycle (artifacts → Glacier after 90 days) -----------------------

resource "aws_s3_bucket_lifecycle_configuration" "main" {
  for_each = { for b in var.s3_buckets : b.name_suffix => b if b.lifecycle_to_glacier_days > 0 }

  bucket = aws_s3_bucket.main[each.key].id

  rule {
    id     = "transition-to-glacier"
    status = "Enabled"

    filter {
      prefix = ""
    }

    transition {
      days          = each.value.lifecycle_to_glacier_days
      storage_class = "GLACIER"
    }

    noncurrent_version_transition {
      noncurrent_days = 30
      storage_class   = "GLACIER"
    }
  }

  depends_on = [aws_s3_bucket_versioning.main]
}
