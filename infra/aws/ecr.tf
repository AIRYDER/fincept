###############################################################################
# ecr.tf — ECR repositories with immutable tags + scan-on-push (TASK-0903)
#
# Invariants per design doc:
# - image_tag_mutability = "IMMUTABLE" (a tag cannot be overwritten)
# - image_scanning_configuration.scan_on_push = true
# - Lifecycle: keep last N tagged, expire untagged after 7 days
###############################################################################

resource "aws_ecr_repository" "service" {
  for_each = { for i, r in var.ecr_repositories : r.name => r }

  name                 = "${local.name_prefix}-${each.value.name}"
  image_tag_mutability = each.value.image_tag_mutability
  force_delete         = false # production guardrail — refuse to delete repos with images

  image_scanning_configuration {
    scan_on_push = each.value.scan_on_push
  }

  encryption_configuration {
    encryption_type = "AES256"
  }

  tags = merge(local.common_tags, {
    Name = "${local.name_prefix}-${each.value.name}"
  })
}

resource "aws_ecr_lifecycle_policy" "service" {
  for_each = aws_ecr_repository.service

  repository = each.value.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Keep last ${try(each.value.keep_tagged_count, 10)} tagged images"
        selection = {
          tagStatus     = "tagged"
          tagPrefixList = ["v"]
          countType     = "imageCountMoreThan"
          countNumber   = try(each.value.keep_tagged_count, 10)
        }
        action = { type = "expire" }
      },
      {
        rulePriority = 2
        description  = "Expire untagged images after ${try(each.value.untagged_expire_days, 7)} days"
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = try(each.value.untagged_expire_days, 7)
        }
        action = { type = "expire" }
      },
    ]
  })
}

# ECR pull-through cache is intentionally NOT enabled here. Pin every image
# to the Fincept-owned registry to keep the trust boundary explicit.