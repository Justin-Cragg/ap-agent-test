# AWS: ECS Fargate service behind an ALB (API + approval UI), EventBridge
# Scheduler running the same image as ECS tasks (mailbox poll, payment-run
# proposal), S3 for invoice files, Secrets Manager for credentials, Amazon
# Bedrock for Claude.
#
# Secrets are created out-of-band and passed by ARN in var.secret_env.
# Uses the default VPC for brevity; point vpc_id/subnet_ids at your own.

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
variable "image" {
  type        = string
  description = "ECR image URI"
}
variable "plain_env" {
  type    = map(string)
  default = {}
}
variable "secret_env" {
  type        = map(string)
  description = "ENV_VAR => Secrets Manager secret ARN"
  default     = {}
}
variable "certificate_arn" {
  type        = string
  description = "ACM certificate for the HTTPS listener"
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

locals {
  env = merge({
    LLM_PROVIDER = "bedrock"
    AWS_REGION   = var.region
    BLOB_BACKEND = "s3"
    BLOB_BUCKET  = aws_s3_bucket.invoices.bucket
  }, var.plain_env)
  container = {
    name         = "ap-agent"
    image        = var.image
    essential    = true
    portMappings = [{ containerPort = 8080 }]
    environment  = [for k, v in local.env : { name = k, value = v }]
    secrets      = [for k, v in var.secret_env : { name = k, valueFrom = v }]
    logConfiguration = {
      logDriver = "awslogs"
      options   = { awslogs-group = aws_cloudwatch_log_group.agent.name, awslogs-region = var.region, awslogs-stream-prefix = "ap" }
    }
  }
}

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

resource "aws_cloudwatch_log_group" "agent" {
  name              = "/ecs/ap-agent"
  retention_in_days = 90
}

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
  count = length(var.secret_env) > 0 ? 1 : 0
  role  = aws_iam_role.execution.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = values(var.secret_env) }] })
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
      { Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject"], Resource = "${aws_s3_bucket.invoices.arn}/*" },
    ]
  })
}

resource "aws_ecs_cluster" "ap" { name = "ap-agent" }

resource "aws_ecs_task_definition" "agent" {
  family                   = "ap-agent"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 1024
  memory                   = 2048
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn
  container_definitions    = jsonencode([local.container])
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
  name               = "ap-agent"
  load_balancer_type = "application"
  subnets            = data.aws_subnets.default.ids
  security_groups    = [aws_security_group.alb.id]
  idle_timeout       = 900
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
  certificate_arn   = var.certificate_arn
  # Add an authenticate-oidc action here to put the approval UI behind SSO.
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.agent.arn
  }
}

resource "aws_ecs_service" "api" {
  name            = "ap-agent"
  cluster         = aws_ecs_cluster.ap.id
  task_definition = aws_ecs_task_definition.agent.arn
  desired_count   = 1
  launch_type     = "FARGATE"
  network_configuration {
    subnets          = data.aws_subnets.default.ids
    security_groups  = [aws_security_group.task.id]
    assign_public_ip = true
  }
  load_balancer {
    target_group_arn = aws_lb_target_group.agent.arn
    container_name   = "ap-agent"
    container_port   = 8080
  }
}

# ---- scheduled jobs: EventBridge Scheduler -> ECS RunTask with a command override
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
      { Effect = "Allow", Action = ["ecs:RunTask"], Resource = "${aws_ecs_task_definition.agent.arn_without_revision}:*" },
      { Effect = "Allow", Action = ["iam:PassRole"], Resource = [aws_iam_role.execution.arn, aws_iam_role.task.arn] },
    ]
  })
}

resource "aws_scheduler_schedule" "job" {
  for_each            = { poll = var.poll_schedule, payment-run = var.payment_run_schedule }
  name                = "ap-agent-${each.key}"
  schedule_expression = each.value
  flexible_time_window { mode = "OFF" }
  target {
    arn      = aws_ecs_cluster.ap.arn
    role_arn = aws_iam_role.scheduler.arn
    ecs_parameters {
      task_definition_arn = aws_ecs_task_definition.agent.arn
      launch_type         = "FARGATE"
      network_configuration {
        subnets          = data.aws_subnets.default.ids
        security_groups  = [aws_security_group.task.id]
        assign_public_ip = true
      }
    }
    input = jsonencode({ containerOverrides = [{ name = "ap-agent", command = ["ap-agent", each.key] }] })
  }
}

output "alb_dns_name" { value = aws_lb.agent.dns_name }
