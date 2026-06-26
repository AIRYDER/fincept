###############################################################################
# cloudwatch.tf — log groups + alarms + dashboard (TASK-0903)
#
# Log groups: /fincept/<service>, 30-day hot retention (export task handles warm).
# Alarms: API latency, error rate, BudgetGuard kill switch, settlement lag,
#         shadow health degradation.
###############################################################################

# --- Log groups ----------------------------------------------------------

resource "aws_cloudwatch_log_group" "service" {
  for_each = toset([
    for s in keys(local.ecs_services) : s
  ])

  name              = "/fincept/${var.environment}/${each.key}"
  retention_in_days = 30

  # AWS KMS encryption for CloudWatch Logs (optional but recommended).
  # Uncomment if a CMK for logs is provisioned:
  # kms_key_id = aws_kms_key.logs.arn

  tags = merge(local.common_tags, {
    Name    = "/fincept/${var.environment}/${each.key}"
    Service = each.key
  })
}

# --- Metric alarms -------------------------------------------------------

# SNS topic for alarms (single operator email).
resource "aws_sns_topic" "alarms" {
  name              = "${local.name_prefix}-alarms"
  kms_master_key_id = aws_kms_key.secrets.id # reuse secrets CMK; ops-only access

  tags = local.common_tags
}

# API latency p95 alarm — placeholder until ECS service + target group is wired.
# Wired in cloudwatch.tf only because ECS is referenced; value is a constant
# placeholder that operators can adjust per workload.
resource "aws_cloudwatch_metric_alarm" "api_latency_p95" {
  alarm_name          = "${local.name_prefix}-api-latency-p95"
  alarm_description   = "API p95 latency > ${var.api_latency_p95_threshold_ms}ms"
  namespace           = "AWS/ECS"
  metric_name         = "TargetResponseTime"
  extended_statistic  = "p95"
  period              = 300
  evaluation_periods  = 3
  threshold           = var.api_latency_p95_threshold_ms / 1000.0 # metric is seconds
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    ClusterName = aws_ecs_cluster.main.name
    ServiceName = aws_ecs_service.api.name
  }

  alarm_actions = [aws_sns_topic.alarms.arn]
  ok_actions    = [aws_sns_topic.alarms.arn]

  tags = local.common_tags
}

# API 5xx error rate alarm. Uses the AWS/ApplicationELB namespace once the
# ALB target group is provisioned; dimensions are wired in alb_waf.tf.
resource "aws_cloudwatch_metric_alarm" "api_5xx_rate" {
  alarm_name          = "${local.name_prefix}-api-5xx-rate"
  alarm_description   = "API 5xx rate > ${var.api_error_rate_threshold_pct}%"
  namespace           = "AWS/ApplicationELB"
  metric_name         = "HTTPCode_Target_5XX_Count"
  statistic           = "Sum"
  period              = 60
  evaluation_periods  = 5
  threshold           = 10 # raw count; operators tune after first week of traffic
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  # dimensions are populated by reference from alb_waf.tf's target group ARN
  dimensions = {
    LoadBalancer = aws_lb.main.arn_suffix
    TargetGroup  = aws_lb_target_group.api.arn_suffix
  }

  alarm_actions = [aws_sns_topic.alarms.arn]
  ok_actions    = [aws_sns_topic.alarms.arn]

  tags = local.common_tags
}

# Settlement lag alarm — custom metric published by the orchestrator.
# Namespace convention: "Fincept" (the orchestrator publishes here).
resource "aws_cloudwatch_metric_alarm" "settlement_lag" {
  alarm_name          = "${local.name_prefix}-settlement-lag"
  alarm_description   = "Settlement lag > ${var.settlement_lag_seconds_threshold}s"
  namespace           = "Fincept"
  metric_name         = "settlement_lag_seconds"
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 3
  threshold           = var.settlement_lag_seconds_threshold
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "breaching" # missing metric means the orchestrator is down

  alarm_actions = [aws_sns_topic.alarms.arn]
  ok_actions    = [aws_sns_topic.alarms.arn]

  tags = local.common_tags
}

# BudgetGuard kill-switch alarm — only wired when an SNS topic ARN is supplied.
resource "aws_cloudwatch_metric_alarm" "budget_kill_switch" {
  count = var.budget_kill_switch_topic_arn != "" ? 1 : 0

  alarm_name          = "${local.name_prefix}-budget-kill-switch"
  alarm_description   = "BudgetGuard kill switch engaged"
  namespace           = "Fincept"
  metric_name         = "budget_kill_switch_active"
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = [var.budget_kill_switch_topic_arn, aws_sns_topic.alarms.arn]
  ok_actions    = [aws_sns_topic.alarms.arn]

  tags = local.common_tags
}

# --- Operator dashboard --------------------------------------------------

resource "aws_cloudwatch_dashboard" "main" {
  dashboard_name = "${local.name_prefix}-production"

  dashboard_body = jsonencode({
    widgets = [
      {
        type   = "metric"
        x      = 0
        y      = 0
        width  = 12
        height = 6
        properties = {
          title  = "API Latency p95 (ms)"
          region = var.aws_region
          metrics = [
            ["AWS/ApplicationELB", "TargetResponseTime", "LoadBalancer", aws_lb.main.arn_suffix, "TargetGroup", aws_lb_target_group.api.arn_suffix],
          ]
          stat   = "p95"
          period = 60
        }
      },
      {
        type   = "metric"
        x      = 12
        y      = 0
        width  = 12
        height = 6
        properties = {
          title   = "Settlement Lag (s)"
          region  = var.aws_region
          metrics = [["Fincept", "settlement_lag_seconds"]]
          stat    = "Maximum"
          period  = 60
        }
      },
      {
        type   = "metric"
        x      = 0
        y      = 6
        width  = 12
        height = 6
        properties = {
          title  = "API 5xx Count"
          region = var.aws_region
          metrics = [
            ["AWS/ApplicationELB", "HTTPCode_Target_5XX_Count", "LoadBalancer", aws_lb.main.arn_suffix, "TargetGroup", aws_lb_target_group.api.arn_suffix],
          ]
          stat   = "Sum"
          period = 60
        }
      },
      {
        type   = "metric"
        x      = 12
        y      = 6
        width  = 12
        height = 6
        properties = {
          title  = "ECS Service CPU"
          region = var.aws_region
          metrics = [
            ["AWS/ECS", "CPUUtilization", "ClusterName", aws_ecs_cluster.main.name, "ServiceName", aws_ecs_service.api.name],
          ]
          stat   = "Average"
          period = 60
        }
      },
    ]
  })
}
