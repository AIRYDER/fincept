###############################################################################
# ecs.tf — ECS cluster, services, task definitions (TASK-0903)
#
# One Fargate cluster, three always-on services (api, dashboard, orchestrator).
# OMS + risk live in the same VPC but their task definitions are reserved here
# for future expansion — they are NOT created in this MVP because the local
# paper-trading spine (Railway staging) is the source of truth for v1.
#
# All secrets are injected from Secrets Manager via the task execution role.
# NO plaintext env vars for credentials.
###############################################################################

# --- Cluster --------------------------------------------------------------

resource "aws_ecs_cluster" "main" {
  name = local.name_prefix

  setting {
    name  = "containerInsights"
    value = "enabled"
  }

  tags = merge(local.common_tags, {
    Name = local.name_prefix
  })
}

resource "aws_ecs_cluster_capacity_providers" "main" {
  cluster_name       = aws_ecs_cluster.main.name
  capacity_providers = ["FARGATE", "FARGATE_SPOT"]

  default_capacity_provider_strategy {
    capacity_provider = "FARGATE"
    weight            = 100
    base              = 1
  }
}

# --- Common log / secrets helpers ----------------------------------------

# Task definition secrets block — built from Secrets Manager ARNs.
# These reference the secrets created in secrets.tf.
locals {
  ecs_task_secrets = [
    { name = "QUANT_FOUNDRY_CALLBACK_SECRET", valueFrom = aws_secretsmanager_secret.main["fincept/callback-secret"].arn },
    { name = "JWT_SIGNING_KEY", valueFrom = aws_secretsmanager_secret.main["fincept/jwt-signing-key"].arn },
    { name = "RUNPOD_API_KEY", valueFrom = aws_secretsmanager_secret.main["fincept/runpod-api-key"].arn },
    { name = "DATABASE_URL", valueFrom = "${aws_secretsmanager_secret.main["fincept/db-password"].arn}::password::" },
    { name = "REDIS_URL", valueFrom = aws_secretsmanager_secret.main["fincept/redis-auth-token"].arn },
  ]
}

# --- API task definition -------------------------------------------------

resource "aws_ecs_task_definition" "api" {
  family                   = "${local.name_prefix}-api"
  cpu                      = tostring(var.api_task_cpu)
  memory                   = tostring(var.api_task_memory)
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  execution_role_arn       = aws_iam_role.ecs_task_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  container_definitions = jsonencode([
    {
      name      = "api"
      image     = "${aws_ecr_repository.service["fincept-api"].repository_url}:latest"
      essential = true

      portMappings = [
        {
          containerPort = var.api_container_port
          protocol      = "tcp"
          appProtocol   = "http"
        }
      ]

      environment = [
        { name = "ENVIRONMENT", value = var.environment },
        { name = "AWS_REGION", value = var.aws_region },
      ]

      secrets = local.ecs_task_secrets

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.service["api"].name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "api"
        }
      }

      healthCheck = {
        command     = ["CMD-SHELL", "python -c \"import urllib.request; urllib.request.urlopen('http://localhost:${var.api_container_port}/health').read()\" || exit 1"]
        interval    = 30
        timeout     = 5
        retries     = 3
        startPeriod = 60
      }
    }
  ])

  tags = local.common_tags
}

# --- Dashboard task definition ------------------------------------------

resource "aws_ecs_task_definition" "dashboard" {
  family                   = "${local.name_prefix}-dashboard"
  cpu                      = tostring(var.dashboard_task_cpu)
  memory                   = tostring(var.dashboard_task_memory)
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  execution_role_arn       = aws_iam_role.ecs_task_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  container_definitions = jsonencode([
    {
      name      = "dashboard"
      image     = "${aws_ecr_repository.service["fincept-dashboard"].repository_url}:latest"
      essential = true

      portMappings = [
        {
          containerPort = var.dashboard_container_port
          protocol      = "tcp"
          appProtocol   = "http"
        }
      ]

      environment = [
        { name = "ENVIRONMENT", value = var.environment },
        { name = "NEXT_PUBLIC_API_URL", value = "https://${var.domain_name != "" ? var.domain_name : aws_lb.main.dns_name}/api" },
      ]

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.service["dashboard"].name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "dashboard"
        }
      }

      healthCheck = {
        command     = ["CMD-SHELL", "wget -q --spider http://localhost:${var.dashboard_container_port}/ || exit 1"]
        interval    = 30
        timeout     = 5
        retries     = 3
        startPeriod = 60
      }
    }
  ])

  tags = local.common_tags
}

# --- Orchestrator task definition ---------------------------------------

resource "aws_ecs_task_definition" "orchestrator" {
  family                   = "${local.name_prefix}-orchestrator"
  cpu                      = tostring(var.orchestrator_task_cpu)
  memory                   = tostring(var.orchestrator_task_memory)
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  execution_role_arn       = aws_iam_role.ecs_task_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  container_definitions = jsonencode([
    {
      name      = "orchestrator"
      image     = "${aws_ecr_repository.service["fincept-orchestrator"].repository_url}:latest"
      essential = true

      portMappings = [
        {
          containerPort = var.orchestrator_container_port
          protocol      = "tcp"
          appProtocol   = "http"
        }
      ]

      environment = [
        { name = "ENVIRONMENT", value = var.environment },
        { name = "AWS_REGION", value = var.aws_region },
      ]

      secrets = local.ecs_task_secrets

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.service["orchestrator"].name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "orchestrator"
        }
      }

      healthCheck = {
        command     = ["CMD-SHELL", "python -c \"import urllib.request; urllib.request.urlopen('http://localhost:${var.orchestrator_container_port}/health').read()\" || exit 1"]
        interval    = 30
        timeout     = 5
        retries     = 3
        startPeriod = 60
      }
    }
  ])

  tags = local.common_tags
}

# --- Services ------------------------------------------------------------

resource "aws_ecs_service" "api" {
  name             = local.ecs_services["api"]
  cluster          = aws_ecs_cluster.main.id
  task_definition  = aws_ecs_task_definition.api.arn
  desired_count    = var.api_desired_count
  launch_type      = "FARGATE"
  platform_version = "1.4.0"

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.ecs_tasks.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "api"
    container_port   = var.api_container_port
  }

  depends_on = [
    aws_iam_role_policy_attachment.ecs_task_execution_managed,
    aws_lb_target_group.api,
  ]

  tags = local.common_tags
}

resource "aws_ecs_service" "dashboard" {
  name             = local.ecs_services["dashboard"]
  cluster          = aws_ecs_cluster.main.id
  task_definition  = aws_ecs_task_definition.dashboard.arn
  desired_count    = var.dashboard_desired_count
  launch_type      = "FARGATE"
  platform_version = "1.4.0"

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.ecs_tasks.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.dashboard.arn
    container_name   = "dashboard"
    container_port   = var.dashboard_container_port
  }

  depends_on = [
    aws_iam_role_policy_attachment.ecs_task_execution_managed,
    aws_lb_target_group.dashboard,
  ]

  tags = local.common_tags
}

resource "aws_ecs_service" "orchestrator" {
  name             = local.ecs_services["orchestrator"]
  cluster          = aws_ecs_cluster.main.id
  task_definition  = aws_ecs_task_definition.orchestrator.arn
  desired_count    = var.orchestrator_desired_count
  launch_type      = "FARGATE"
  platform_version = "1.4.0"

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.ecs_tasks.id]
    assign_public_ip = false
  }

  tags = local.common_tags
}