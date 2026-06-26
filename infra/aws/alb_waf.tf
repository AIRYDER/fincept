###############################################################################
# alb_waf.tf — Application Load Balancer + WAF + ACM + Route53 (TASK-0903)
#
# Path-based routing:
#   /api/*  -> api target group (FastAPI on port 8000)
#   /*      -> dashboard target group (Next.js on port 3000)
#
# WAF rules:
#   - AWS Managed Rules (Core + Known Bad Inputs + IP Reputation)
#   - Custom rate limit per IP (100 req / 5 min)
#
# ACM:
#   - Use var.acm_certificate_arn if supplied
#   - Otherwise issue via DNS validation using var.domain_name
###############################################################################

# --- ACM (conditional) ---------------------------------------------------

resource "aws_acm_certificate" "main" {
  count = var.acm_certificate_arn == "" && local.manage_dns ? 1 : 0

  domain_name       = var.domain_name
  validation_method = "DNS"

  subject_alternative_names = ["*.${var.domain_name}"]

  lifecycle {
    create_before_destroy = true
  }

  tags = local.common_tags
}

resource "aws_route53_record" "cert_validation" {
  count = var.acm_certificate_arn == "" && local.manage_dns ? 1 : 0

  zone_id = data.aws_route53_zone.primary[0].zone_id
  name    = one(aws_acm_certificate.main[*].domain_validation_options[*].resource_record_name)
  type    = one(aws_acm_certificate.main[*].domain_validation_options[*].resource_record_type)
  records = [one(aws_acm_certificate.main[*].domain_validation_options[*].resource_record_value)]
  ttl     = 60
}

resource "aws_acm_certificate_validation" "main" {
  count = var.acm_certificate_arn == "" && local.manage_dns ? 1 : 0

  certificate_arn         = aws_acm_certificate.main[0].arn
  validation_record_fqdns = [aws_route53_record.cert_validation[0].fqdn]
}

locals {
  acm_certificate_arn = (
    var.acm_certificate_arn != "" ? var.acm_certificate_arn :
    (local.manage_dns ? aws_acm_certificate.main[0].arn : "")
  )
}

# --- ALB ----------------------------------------------------------------

resource "aws_lb" "main" {
  name               = "${local.name_prefix}-alb"
  internal           = false
  load_balancer_type = "application"
  ip_address_type    = "dualstack"
  security_groups    = [aws_security_group.alb.id]
  subnets            = aws_subnet.public[*].id

  drop_invalid_header_fields = true

  enable_deletion_protection = var.environment == "prod"

  access_logs {
    bucket  = aws_s3_bucket.main["receipts"].id
    prefix  = "alb-access-logs"
    enabled = true
  }

  tags = merge(local.common_tags, {
    Name = "${local.name_prefix}-alb"
  })
}

# --- Listeners ---------------------------------------------------------

# HTTPS listener (only when a certificate is available).
resource "aws_lb_listener" "https" {
  count = local.acm_certificate_arn != "" ? 1 : 0

  load_balancer_arn = aws_lb.main.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = local.acm_certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.dashboard.arn
  }

  tags = local.common_tags
}

# HTTP -> HTTPS redirect.
resource "aws_lb_listener" "http_redirect" {
  count = local.acm_certificate_arn != "" ? 1 : 0

  load_balancer_arn = aws_lb.main.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = "redirect"

    redirect {
      port        = "443"
      protocol    = "HTTPS"
      status_code = "HTTP_301"
    }
  }

  tags = local.common_tags
}

# Path-based routing on the HTTPS listener: /api/* -> api service.
resource "aws_lb_listener_rule" "api_path" {
  count = local.acm_certificate_arn != "" ? 1 : 0

  listener_arn = aws_lb_listener.https[0].arn
  priority     = 100

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }

  condition {
    path_pattern {
      values = ["/api/*"]
    }
  }

  tags = local.common_tags
}

# --- Target groups ------------------------------------------------------

resource "aws_lb_target_group" "api" {
  name        = "${local.name_prefix}-api-tg"
  port        = var.api_container_port
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = aws_vpc.main.id

  health_check {
    path                = "/health"
    matcher             = "200"
    interval            = 30
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  deregistration_delay = 30

  tags = local.common_tags
}

resource "aws_lb_target_group" "dashboard" {
  name        = "${local.name_prefix}-dashboard-tg"
  port        = var.dashboard_container_port
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = aws_vpc.main.id

  health_check {
    path                = "/"
    matcher             = "200-399"
    interval            = 30
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  deregistration_delay = 30

  tags = local.common_tags
}

# --- WAF ----------------------------------------------------------------

resource "aws_wafv2_web_acl" "main" {
  count = local.acm_certificate_arn != "" ? 1 : 0

  name        = "${local.name_prefix}-waf"
  description = "WAF for Fincept ALB"
  scope       = "REGIONAL"

  default_action {
    allow {}
  }

  # AWS Managed Rules — Core Rule Set (OWASP Top 10)
  rule {
    name     = "AWSManagedRulesCommonRuleSet"
    priority = 1

    override_action {
      none {}
    }

    statement {
      managed_rule_group_statement {
        name        = "AWSManagedRulesCommonRuleSet"
        vendor_name = "AWS"
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "common"
      sampled_requests_enabled   = true
    }
  }

  # AWS Managed Rules — Known Bad Inputs
  rule {
    name     = "AWSManagedRulesKnownBadInputsRuleSet"
    priority = 2

    override_action {
      none {}
    }

    statement {
      managed_rule_group_statement {
        name        = "AWSManagedRulesKnownBadInputsRuleSet"
        vendor_name = "AWS"
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "known-bad-inputs"
      sampled_requests_enabled   = true
    }
  }

  # AWS Managed Rules — IP Reputation (anonymous + known bad)
  rule {
    name     = "AWSManagedRulesAmazonIpReputationList"
    priority = 3

    override_action {
      none {}
    }

    statement {
      managed_rule_group_statement {
        name        = "AWSManagedRulesAmazonIpReputationList"
        vendor_name = "AWS"
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "ip-reputation"
      sampled_requests_enabled   = true
    }
  }

  # Custom rate limit per IP: 100 req / 5 min
  rule {
    name     = "RateLimitPerIP"
    priority = 4

    action {
      block {}
    }

    statement {
      rate_based_statement {
        limit              = 100 # 100 req / 5 min per IP
        aggregate_key_type = "IP"
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "rate-limit"
      sampled_requests_enabled   = true
    }
  }

  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = "${local.name_prefix}-waf"
    sampled_requests_enabled   = true
  }

  tags = local.common_tags
}

resource "aws_wafv2_web_acl_association" "main" {
  count = local.acm_certificate_arn != "" ? 1 : 0

  resource_arn = aws_lb.main.arn
  web_acl_arn  = aws_wafv2_web_acl.main[0].arn
}

# --- Route53 alias (conditional) ----------------------------------------

resource "aws_route53_record" "apex" {
  count = local.manage_dns ? 1 : 0

  zone_id = data.aws_route53_zone.primary[0].zone_id
  name    = var.domain_name
  type    = "A"

  alias {
    name                   = aws_lb.main.dns_name
    zone_id                = aws_lb.main.zone_id
    evaluate_target_health = true
  }
}

resource "aws_route53_record" "www" {
  count = local.manage_dns ? 1 : 0

  zone_id = data.aws_route53_zone.primary[0].zone_id
  name    = "www.${var.domain_name}"
  type    = "A"

  alias {
    name                   = aws_lb.main.dns_name
    zone_id                = aws_lb.main.zone_id
    evaluate_target_health = true
  }
}
