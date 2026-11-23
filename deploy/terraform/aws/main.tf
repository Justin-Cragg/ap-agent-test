# AWS
#
#   ALB (HTTPS, WAF) --authenticate-oidc--> ECS Fargate service (API + approval/review UI)
#                    \-- /webhooks/*, /healthz forwarded without SSO (secret-checked by the app)
#   Task security group accepts traffic only from the ALB; the app also verifies
#   the ALB's signed x-amzn-oidc-data header, so identity can't be forged.
#   EventBridge Scheduler -> ECS tasks: poll, payment-run, purge
#   Payment executor: separate task definition, task role and ERP credentials.
#   S3 (invoice files, lifecycle-expired), Secrets Manager, Bedrock, CloudWatch alarms.

terraform {
  required_providers {
    aws = { source = "hashicorp/aws", version = ">= 5.40" }
  }
}

provider "aws" { region = var.region }

variable "region" {
  type    = string
  default = "us-east-1"
}
variable "image" { type = string }
variable "domain" { type = string }
variable "certificate_arn" { type = string }
variable "oidc" {
  description = "Your IdP (Entra ID, Okta, Google...) for ALB authentication"
  type = object({
    issuer                 = string
    authorization_endpoint = string
    token_endpoint         = string
    user_info_endpoint     = string
    client_id              = string
    client_secret_arn      = string # Secrets Manager ARN holding the OIDC client secret
  })
}
variable "plain_env" {
  type    = map(string)
  default = {}
}
variable "secret_env" {
  type        = map(string)
  description = "ENV_VAR => Secrets Manager ARN for the agent (ERP user WITHOUT payment rights)"
  default     = {}
}
variable "executor_secret_env" {
  type        = map(string)
  description = "ENV_VAR => Secrets Manager ARN for the payment executor (ERP user WITH payment rights)"
  default     = {}
}
variable "alert_email" { type = string }
variable "retention_days" {
  type    = number
  default = 2555
}
variable "poll_schedule" {
  type    = string
  default = "rate(5 minutes)"
}
variable "payment_run_schedule" {
  type    = string
  default = "cron(0 9 ? * TUE,THU *)"
}

data "aws_caller_identity" "me" {}
data "aws_vpc" "default" { default = true }
data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}
data "aws_secretsmanager_secret_version" "oidc" { secret_id = var.oidc.client_secret_arn }

locals {
  env = merge({
    ENVIRONMENT       = "production"
    LLM_PROVIDER      = "bedrock"
    AWS_REGION        = var.region
    BLOB_BACKEND      = "s3"
    BLOB_BUCKET       = aws_s3_bucket.invoices.bucket
    SECRET_STORE      = "aws"
    AUTH_MODE         = "alb"
    ALB_ARN           = aws_lb.agent.arn
    PUBLIC_BASE_URL   = "https://${var.domain}"
    PAYMENT_EXECUTION = "separate"
  }, var.plain_env)
  log_config = {
    logDriver = "awslogs"
    options   = { awslogs-group = aws_cloudwatch_log_group.agent.name, awslogs-region = var.region, awslogs-stream-prefix = "ap" }
  }
  agent_container = {
    name = "ap-agent", image = var.image, essential = true, portMappings = [{ containerPort = 8080 }]
    environment      = [for k, v in local.env : { name = k, value = v }]
    secrets          = [for k, v in var.secret_env : { name = k, valueFrom = v }]
    logConfiguration = local.log_config
  }
  executor_container = {
    name = "ap-agent", image = var.image, essential = true, command = ["ap-agent", "execute-payments"]
    environment      = [for k, v in local.env : { name = k, value = v }]
    secrets          = [for k, v in var.executor_secret_env : { name = k, valueFrom = v }]
    logConfiguration = local.log_config
  }
}

# ---------------------------------------------------------------- storage
resource "aws_s3_bucket" "invoices" { bucket_prefix = "ap-agent-invoices-" }
resource "aws_s3_bucket_public_access_block" "invoices" {
  bucket                  = aws_s3_bucket.invoices.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
resource "aws_s3_bucket_versioning" "invoices" {
  bucket = aws_s3_bucket.invoices.id
  versioning_configuration { status = "Enabled" }
}
resource "aws_s3_bucket_lifecycle_configuration" "invoices" {
  bucket = aws_s3_bucket.invoices.id
  rule {
    id     = "retention"
    status = "Enabled"
    filter {}
    expiration { days = var.retention_days }
    noncurrent_version_expiration { noncurrent_days = 30 }
  }
}

resource "aws_cloudwatch_log_group" "agent" {
  name              = "/ecs/ap-agent"
  retention_in_days = 400
}

# ---------------------------------------------------------------- IAM
data "aws_iam_policy_document" "ecs_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "execution" {
  name               = "ap-agent-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}
resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}
resource "aws_iam_role_policy" "execution_secrets" {
  role   = aws_iam_role.execution.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = concat(["arn:aws:secretsmanager:${var.region}:${data.aws_caller_identity.me.account_id}:secret:ap-agent-none"], values(var.secret_env), values(var.executor_secret_env)) }] })
}

resource "aws_iam_role" "task" {
  name               = "ap-agent-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}
resource "aws_iam_role_policy" "task" {
  role = aws_iam_role.task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # Claude via Bedrock's Messages-API (Mantle) endpoint. Confirm the action names
      # for your account in the Bedrock IAM docs and scope Resource to the model.
      { Effect = "Allow", Action = ["bedrock-mantle:*", "bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"], Resource = "*" },
      { Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"], Resource = "${aws_s3_bucket.invoices.arn}/*" },
      # Rotating OAuth refresh tokens (SECRET_STORE=aws) - only ap-agent-* secrets.
      { Effect = "Allow", Action = ["secretsmanager:GetSecretValue", "secretsmanager:PutSecretValue", "secretsmanager:CreateSecret"], Resource = "arn:aws:secretsmanager:${var.region}:${data.aws_caller_identity.me.account_id}:secret:ap-agent-*" },
    ]
  })
}

resource "aws_iam_role" "executor" {
  name               = "ap-payment-executor-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}
resource "aws_iam_role_policy" "executor" {
  role   = aws_iam_role.executor.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow", Action = ["secretsmanager:GetSecretValue", "secretsmanager:PutSecretValue"], Resource = "arn:aws:secretsmanager:${var.region}:${data.aws_caller_identity.me.account_id}:secret:ap-agent-*" }] })
}

# ---------------------------------------------------------------- compute
resource "aws_ecs_cluster" "ap" {
  name = "ap-agent"
  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

resource "aws_ecs_task_definition" "agent" {
  family                   = "ap-agent"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 1024
  memory                   = 2048
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn
  container_definitions    = jsonencode([local.agent_container])
}

resource "aws_ecs_task_definition" "executor" {
  family                   = "ap-payment-executor"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 512
  memory                   = 1024
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.executor.arn
  container_definitions    = jsonencode([local.executor_container])
}

resource "aws_security_group" "alb" {
  vpc_id = data.aws_vpc.default.id
  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}
resource "aws_security_group" "task" {
  vpc_id = data.aws_vpc.default.id
  ingress {
    from_port       = 8080
    to_port         = 8080
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_lb" "agent" {
  name                       = "ap-agent"
  load_balancer_type         = "application"
  subnets                    = data.aws_subnets.default.ids
  security_groups            = [aws_security_group.alb.id]
  idle_timeout               = 900
  drop_invalid_header_fields = true
}
resource "aws_lb_target_group" "agent" {
  name        = "ap-agent"
  port        = 8080
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = data.aws_vpc.default.id
  health_check { path = "/healthz" }
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.agent.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.certificate_arn
  default_action {
    type  = "authenticate-oidc"
    order = 1
    authenticate_oidc {
      issuer                     = var.oidc.issuer
      authorization_endpoint     = var.oidc.authorization_endpoint
      token_endpoint             = var.oidc.token_endpoint
      user_info_endpoint         = var.oidc.user_info_endpoint
      client_id                  = var.oidc.client_id
      client_secret              = data.aws_secretsmanager_secret_version.oidc.secret_string
      scope                      = "openid email profile"
      on_unauthenticated_request = "authenticate"
      session_timeout            = 28800
    }
  }
  default_action {
    type             = "forward"
    order            = 2
    target_group_arn = aws_lb_target_group.agent.arn
  }
}

resource "aws_lb_listener_rule" "webhooks" {
  listener_arn = aws_lb_listener.https.arn
  priority     = 10
  condition {
    path_pattern { values = ["/webhooks/*", "/healthz"] }
  }
  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.agent.arn
  }
}

resource "aws_wafv2_web_acl" "agent" {
  name  = "ap-agent"
  scope = "REGIONAL"
  default_action {
    allow {}
  }
  rule {
    name     = "common"
    priority = 1
    override_action {
      none {}
    }
    statement {
      managed_rule_group_statement {
        vendor_name = "AWS"
        name        = "AWSManagedRulesCommonRuleSet"
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "common"
      sampled_requests_enabled   = true
    }
  }
  rule {
    name     = "bad-inputs"
    priority = 2
    override_action {
      none {}
    }
    statement {
      managed_rule_group_statement {
        vendor_name = "AWS"
        name        = "AWSManagedRulesKnownBadInputsRuleSet"
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "bad-inputs"
      sampled_requests_enabled   = true
    }
  }
  rule {
    name     = "rate"
    priority = 3
    action {
      block {}
    }
    statement {
      rate_based_statement {
        limit              = 1500
        aggregate_key_type = "IP"
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "rate"
      sampled_requests_enabled   = true
    }
  }
  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = "ap-agent"
    sampled_requests_enabled   = true
  }
}
resource "aws_wafv2_web_acl_association" "agent" {
  resource_arn = aws_lb.agent.arn
  web_acl_arn  = aws_wafv2_web_acl.agent.arn
}

resource "aws_ecs_service" "api" {
  name            = "ap-agent"
  cluster         = aws_ecs_cluster.ap.id
  task_definition = aws_ecs_task_definition.agent.arn
  desired_count   = 2
  launch_type     = "FARGATE"
  network_configuration {
    subnets          = data.aws_subnets.default.ids
    security_groups  = [aws_security_group.task.id]
    assign_public_ip = true # needed in the default VPC for egress; use private subnets + NAT in production
  }
  load_balancer {
    target_group_arn = aws_lb_target_group.agent.arn
    container_name   = "ap-agent"
    container_port   = 8080
  }
}

# ---------------------------------------------------------------- scheduled jobs
data "aws_iam_policy_document" "scheduler_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
  }
}
resource "aws_iam_role" "scheduler" {
  name               = "ap-agent-scheduler"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json
}
resource "aws_iam_role_policy" "scheduler" {
  role = aws_iam_role.scheduler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["ecs:RunTask"], Resource = ["${aws_ecs_task_definition.agent.arn_without_revision}:*", "${aws_ecs_task_definition.executor.arn_without_revision}:*"] },
      { Effect = "Allow", Action = ["iam:PassRole"], Resource = [aws_iam_role.execution.arn, aws_iam_role.task.arn, aws_iam_role.executor.arn] },
    ]
  })
}

locals {
  jobs = {
    poll        = { schedule = var.poll_schedule, td = aws_ecs_task_definition.agent.arn, cmd = ["ap-agent", "poll"] }
    payment-run = { schedule = var.payment_run_schedule, td = aws_ecs_task_definition.agent.arn, cmd = ["ap-agent", "payment-run"] }
    purge       = { schedule = "cron(17 3 ? * SUN *)", td = aws_ecs_task_definition.agent.arn, cmd = ["ap-agent", "purge"] }
    pay-execute = { schedule = "rate(10 minutes)", td = aws_ecs_task_definition.executor.arn, cmd = ["ap-agent", "execute-payments"] }
  }
}

resource "aws_scheduler_schedule" "job" {
  for_each            = local.jobs
  name                = "ap-agent-${each.key}"
  schedule_expression = each.value.schedule
  flexible_time_window { mode = "OFF" }
  target {
    arn      = aws_ecs_cluster.ap.arn
    role_arn = aws_iam_role.scheduler.arn
    ecs_parameters {
      task_definition_arn = each.value.td
      launch_type         = "FARGATE"
      network_configuration {
        subnets          = data.aws_subnets.default.ids
        security_groups  = [aws_security_group.task.id]
        assign_public_ip = true
      }
    }
    input = jsonencode({ containerOverrides = [{ name = "ap-agent", command = each.value.cmd }] })
  }
}

# ---------------------------------------------------------------- alerting
resource "aws_sns_topic" "alerts" { name = "ap-agent-alerts" }
resource "aws_sns_topic_subscription" "email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}
resource "aws_cloudwatch_log_metric_filter" "errors" {
  name           = "ap-agent-errors"
  log_group_name = aws_cloudwatch_log_group.agent.name
  pattern        = "{ $.severity = \"ERROR\" }"
  metric_transformation {
    name      = "Errors"
    namespace = "APAgent"
    value     = "1"
  }
}
resource "aws_cloudwatch_metric_alarm" "errors" {
  alarm_name          = "ap-agent-errors"
  namespace           = "APAgent"
  metric_name         = "Errors"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 5
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

output "alb_dns_name" { value = aws_lb.agent.dns_name }
