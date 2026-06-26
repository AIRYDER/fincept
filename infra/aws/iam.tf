###############################################################################
# iam.tf — execution + task roles for ECS, plus service policies (TASK-0903)
#
# Three roles:
#   1. ecs_task_execution_role — used by ECS at task start to pull images,
#      read Secrets Manager, write CloudWatch logs.
#   2. ecs_task_role           — used by the running task to read S3, write
#      CloudWatch logs/metrics, and read Secrets Manager.
#   3. codebuild_role          — optional, only if you build images in AWS.
#
# NO broker credentials are ever placed in task definitions. The OMS task
# reads broker secrets from Secrets Manager via the task role.
###############################################################################

# --- Task execution role -------------------------------------------------

data "aws_iam_policy_document" "ecs_task_execution_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "ecs_task_execution" {
  name               = "${local.name_prefix}-ecs-task-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_task_execution_assume.json

  tags = local.common_tags
}

resource "aws_iam_role_policy_attachment" "ecs_task_execution_managed" {
  role       = aws_iam_role.ecs_task_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# Inline policy for Secrets Manager read at task start (managed policy does
# not include Secrets Manager by default).
data "aws_iam_policy_document" "ecs_task_execution_secrets" {
  statement {
    sid       = "ReadFinceptSecrets"
    effect    = "Allow"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = ["arn:aws:secretsmanager:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:secret:fincept/*"]
  }
}

resource "aws_iam_role_policy" "ecs_task_execution_secrets" {
  name   = "${local.name_prefix}-ecs-task-execution-secrets"
  role   = aws_iam_role.ecs_task_execution.id
  policy = data.aws_iam_policy_document.ecs_task_execution_secrets.json
}

# --- Task role (runtime) --------------------------------------------------

data "aws_iam_policy_document" "ecs_task_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "ecs_task" {
  name               = "${local.name_prefix}-ecs-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_task_assume.json

  tags = local.common_tags
}

# S3 read/write for receipts, dossiers, settlements, artifacts.
data "aws_iam_policy_document" "ecs_task_s3" {
  statement {
    sid    = "ReceiptsReadWrite"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:ListBucket",
      "s3:GetBucketVersioning",
      "s3:GetBucketObjectLockConfiguration",
    ]
    resources = [
      for b in keys(local.bucket_names) :
      "arn:aws:s3:::${local.bucket_names[b]}"
    ]
  }

  statement {
    sid     = "ReceiptsObjectsReadWrite"
    effect  = "Allow"
    actions = ["s3:GetObject", "s3:PutObject", "s3:GetObjectVersion"]
    resources = [
      for b in keys(local.bucket_names) :
      "arn:aws:s3:::${local.bucket_names[b]}/*"
    ]
  }
}

resource "aws_iam_role_policy" "ecs_task_s3" {
  name   = "${local.name_prefix}-ecs-task-s3"
  role   = aws_iam_role.ecs_task.id
  policy = data.aws_iam_policy_document.ecs_task_s3.json
}

# Secrets Manager read at runtime for the few secrets the app needs.
data "aws_iam_policy_document" "ecs_task_secrets" {
  statement {
    sid       = "ReadFinceptSecrets"
    effect    = "Allow"
    actions   = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
    resources = ["arn:aws:secretsmanager:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:secret:fincept/*"]
  }
}

resource "aws_iam_role_policy" "ecs_task_secrets" {
  name   = "${local.name_prefix}-ecs-task-secrets"
  role   = aws_iam_role.ecs_task.id
  policy = data.aws_iam_policy_document.ecs_task_secrets.json
}

# CloudWatch metrics + logs write.
data "aws_iam_policy_document" "ecs_task_cloudwatch" {
  statement {
    sid    = "CloudWatchMetrics"
    effect = "Allow"
    actions = [
      "cloudwatch:PutMetricData",
      "cloudwatch:GetMetricStatistics",
      "cloudwatch:ListMetrics",
    ]
    resources = ["*"]
  }

  statement {
    sid    = "CloudWatchLogs"
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
      "logs:DescribeLogStreams",
    ]
    resources = ["arn:aws:logs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:log-group:/fincept/*:*"]
  }
}

resource "aws_iam_role_policy" "ecs_task_cloudwatch" {
  name   = "${local.name_prefix}-ecs-task-cloudwatch"
  role   = aws_iam_role.ecs_task.id
  policy = data.aws_iam_policy_document.ecs_task_cloudwatch.json
}

# --- ECS auto-scaling role (application auto-scaling service principal) --

data "aws_iam_policy_document" "ecs_autoscale_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["application-autoscaling.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "ecs_autoscale" {
  name               = "${local.name_prefix}-ecs-autoscale"
  assume_role_policy = data.aws_iam_policy_document.ecs_autoscale_assume.json

  tags = local.common_tags
}

resource "aws_iam_role_policy_attachment" "ecs_autoscale_managed" {
  role       = aws_iam_role.ecs_autoscale.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEC2ContainerServiceAutoscaleRole"
}