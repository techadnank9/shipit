# 07-workers: Graviton spot workers that scale 0 -> N on queue depth and back to 0 when idle.
# One job per worker (copies bind fixed host ports), so capacity tracks the backlog 1:1.

variable "image_tag" {
  description = "Worker image tag in ECR (the git sha; set by CI)."
  type        = string
  default     = ""
}

variable "worker_ami_id" {
  description = "AMI built by packer/worker.pkr.hcl. Empty = newest self-owned ccopy-worker-* AMI."
  type        = string
  default     = ""
}

variable "worker_instance_types" {
  description = "Spot instance types, in preference order (all arm64). More types = fewer spot shortfalls."
  type        = list(string)
  default     = ["c7g.large", "c6g.large", "m7g.large", "m6g.large"]
}

variable "worker_max" {
  description = "Maximum concurrent workers (= concurrent jobs)."
  type        = number
  default     = 10
}

variable "worker_volume_gb" {
  description = "Root volume (images, copy workspaces)."
  type        = number
  default     = 40
}

variable "worker_idle_minutes" {
  description = "Scale to zero after the queue has been empty (incl. in-flight) this long."
  type        = number
  default     = 15
}

variable "ccopy_ai" {
  description = "standin or claude."
  type        = string
  default     = "standin"
}

variable "ccopy_model" {
  description = "Bedrock inference profile id when ccopy_ai = claude. Empty = app default."
  type        = string
  default     = ""
}

variable "web_base_url" {
  description = "Public dashboard URL (links in Slack / GitHub checks)."
  type        = string
  default     = "http://localhost:3000"
}

variable "log_retention_days" {
  description = "CloudWatch log retention."
  type        = number
  default     = 30
}

data "terraform_remote_state" "network" {
  backend = "s3"
  config = {
    bucket = var.state_bucket
    key    = "${var.env}/01-network.tfstate"
    region = "us-west-1"
  }
}

data "terraform_remote_state" "security" {
  backend = "s3"
  config = {
    bucket = var.state_bucket
    key    = "${var.env}/02-security.tfstate"
    region = "us-west-1"
  }
}

data "terraform_remote_state" "storage" {
  backend = "s3"
  config = {
    bucket = var.state_bucket
    key    = "${var.env}/03-storage.tfstate"
    region = "us-west-1"
  }
}

data "terraform_remote_state" "database" {
  backend = "s3"
  config = {
    bucket = var.state_bucket
    key    = "${var.env}/04-database.tfstate"
    region = "us-west-1"
  }
}

data "terraform_remote_state" "queue" {
  backend = "s3"
  config = {
    bucket = var.state_bucket
    key    = "${var.env}/05-queue.tfstate"
    region = "us-west-1"
  }
}

data "aws_ami" "worker" {
  count       = var.worker_ami_id == "" ? 1 : 0
  most_recent = true
  owners      = ["self"]
  filter {
    name   = "name"
    values = ["ccopy-worker-*"]
  }
  filter {
    name   = "architecture"
    values = ["arm64"]
  }
}

locals {
  net      = data.terraform_remote_state.network.outputs
  sec      = data.terraform_remote_state.security.outputs
  storage  = data.terraform_remote_state.storage.outputs
  db       = data.terraform_remote_state.database.outputs
  queue    = data.terraform_remote_state.queue.outputs
  secrets  = local.storage.secret_arns
  asg_name = local.sec.names.worker_asg
  ami_id   = var.worker_ami_id != "" ? var.worker_ami_id : data.aws_ami.worker[0].id
  image    = "${local.storage.ecr_repository_urls["worker"]}:${var.image_tag}"
  registry = split("/", local.storage.ecr_repository_urls["worker"])[0]
  data_dir = "/var/lib/ccopy"

  worker_env = merge(
    {
      CCOPY_AI                = var.ccopy_ai
      CLAUDE_CODE_USE_BEDROCK = "1"
      AWS_REGION              = var.region
      AWS_DEFAULT_REGION      = var.region
      CCOPY_QUEUE             = "sqs"
      CCOPY_SQS_URL           = local.queue.jobs_queue_url
      CCOPY_EVIDENCE_BUCKET   = local.storage.evidence_bucket
      CCOPY_DATA_DIR          = local.data_dir
      CCOPY_PUBLIC_URL        = var.web_base_url
      CCOPY_ENV               = var.env
      CCOPY_ASG_NAME          = local.asg_name
      CCOPY_SECRETS = join(",", [
        "DATABASE_URL=${local.secrets["db"]}#url",
        "GITHUB_APP_ID=${local.secrets["github-app"]}#app_id",
        "GITHUB_APP_PRIVATE_KEY=${local.secrets["github-app"]}#private_key",
        "GITHUB_WEBHOOK_SECRET=${local.secrets["github-app"]}#webhook_secret",
        "SLACK_WEBHOOK_URL=${local.secrets["slack-webhook"]}",
        "LOCALSTACK_AUTH_TOKEN=${local.secrets["localstack-token"]}",
      ])
    },
    var.ccopy_model == "" ? {} : { CCOPY_MODEL = var.ccopy_model, ANTHROPIC_MODEL = var.ccopy_model },
  )
}

resource "aws_cloudwatch_log_group" "worker" {
  # checkov:skip=CKV_AWS_338:30-day retention is the agreed cost/benefit; evidence lives in S3 for 365d.
  name              = "/ccopy/${var.env}/worker"
  retention_in_days = var.log_retention_days
  kms_key_id        = local.sec.kms_key_arn
}

# No ingress at all: operators use SSM Session Manager.
resource "aws_security_group" "worker" {
  name        = "${local.name}-worker"
  description = "Workers: HTTPS out only (Bedrock, ECR, S3, SQS, Secrets, git); Postgres via db-client SG"
  vpc_id      = local.net.vpc_id

  egress {
    description = "HTTPS to AWS APIs and git/package hosts via fck-nat"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${local.name}-worker" }
}

resource "aws_launch_template" "worker" {
  name_prefix            = "${local.name}-worker-"
  image_id               = local.ami_id
  update_default_version = true
  ebs_optimized          = true
  vpc_security_group_ids = [aws_security_group.worker.id, local.db.db_client_security_group_id]

  iam_instance_profile {
    arn = local.sec.worker_instance_profile_arn
  }

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1 # containers on bridge networks cannot reach IMDS
    instance_metadata_tags      = "disabled"
  }

  block_device_mappings {
    device_name = "/dev/xvda"
    ebs {
      volume_type           = "gp3"
      volume_size           = var.worker_volume_gb
      encrypted             = true
      delete_on_termination = true
    }
  }

  monitoring {
    enabled = false # detailed monitoring costs extra; the agent ships mem/disk
  }

  user_data = base64encode(templatefile("${path.module}/user_data.sh.tftpl", {
    env       = local.worker_env
    region    = var.region
    registry  = local.registry
    image     = local.image
    data_dir  = local.data_dir
    log_group = aws_cloudwatch_log_group.worker.name
  }))

  tag_specifications {
    resource_type = "instance"
    tags          = { Name = "${local.name}-worker" }
  }

  tag_specifications {
    resource_type = "volume"
    tags          = { Name = "${local.name}-worker" }
  }

  lifecycle {
    precondition {
      condition     = var.image_tag != ""
      error_message = "Set image_tag (the pushed git sha): -var image_tag=<sha>."
    }
  }
}

resource "aws_autoscaling_group" "worker" {
  name                      = local.asg_name
  min_size                  = 0
  max_size                  = var.worker_max
  vpc_zone_identifier       = local.net.private_subnet_ids
  health_check_type         = "EC2"
  health_check_grace_period = 300
  capacity_rebalance        = true
  default_instance_warmup   = 300
  enabled_metrics           = ["GroupInServiceInstances", "GroupDesiredCapacity", "GroupPendingInstances"]

  mixed_instances_policy {
    instances_distribution {
      on_demand_base_capacity                  = 0
      on_demand_percentage_above_base_capacity = 0
      spot_allocation_strategy                 = "price-capacity-optimized"
    }

    launch_template {
      launch_template_specification {
        launch_template_id = aws_launch_template.worker.id
        version            = aws_launch_template.worker.latest_version
      }

      dynamic "override" {
        for_each = var.worker_instance_types
        content {
          instance_type = override.value
        }
      }
    }
  }

  # A new image tag or AMI changes the launch template and rolls running workers.
  # Launch-before-terminate; instances a worker protected while holding a job are waited for.
  instance_refresh {
    strategy = "Rolling"
    preferences {
      min_healthy_percentage       = 100
      max_healthy_percentage       = 200
      skip_matching                = true
      scale_in_protected_instances = "Wait"
      instance_warmup              = 300
    }
  }

  tag {
    key                 = "Name"
    value               = "${local.name}-worker"
    propagate_at_launch = true
  }

  lifecycle {
    ignore_changes = [desired_capacity] # owned by the scaling policies
  }
}

# ---------------------------------------------------------------- scaling
# Scale out: jobs waiting. +1 for a trickle, faster for a burst (bounded by max).
resource "aws_autoscaling_policy" "scale_out" {
  name                      = "${local.name}-scale-out"
  autoscaling_group_name    = aws_autoscaling_group.worker.name
  policy_type               = "StepScaling"
  adjustment_type           = "ChangeInCapacity"
  estimated_instance_warmup = 300

  step_adjustment {
    metric_interval_lower_bound = 0
    metric_interval_upper_bound = 3
    scaling_adjustment          = 1
  }
  step_adjustment {
    metric_interval_lower_bound = 3
    metric_interval_upper_bound = 9
    scaling_adjustment          = 3
  }
  step_adjustment {
    metric_interval_lower_bound = 9
    scaling_adjustment          = 5
  }
}

resource "aws_cloudwatch_metric_alarm" "jobs_waiting" {
  alarm_name          = "${local.name}-jobs-waiting"
  alarm_description   = "Jobs visible in the queue: add workers"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = local.queue.jobs_queue_name }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_autoscaling_policy.scale_out.arn]
}

# Scale in: only when nothing is visible AND nothing is in flight (a worker waiting on a
# human gate keeps its message in flight), so a running job is never cut short.
resource "aws_autoscaling_policy" "scale_to_zero" {
  name                   = "${local.name}-scale-to-zero"
  autoscaling_group_name = aws_autoscaling_group.worker.name
  policy_type            = "StepScaling"
  adjustment_type        = "ExactCapacity"

  step_adjustment {
    metric_interval_upper_bound = 0
    scaling_adjustment          = 0
  }
}

resource "aws_cloudwatch_metric_alarm" "queue_idle" {
  alarm_name          = "${local.name}-queue-idle"
  alarm_description   = "No visible or in-flight jobs for ${var.worker_idle_minutes} minutes: scale workers to zero"
  evaluation_periods  = var.worker_idle_minutes
  threshold           = 0
  comparison_operator = "LessThanOrEqualToThreshold"
  treat_missing_data  = "breaching" # SQS stops emitting for long-idle queues
  alarm_actions       = [aws_autoscaling_policy.scale_to_zero.arn]

  metric_query {
    id          = "backlog"
    expression  = "visible + inflight"
    label       = "Jobs visible + in flight"
    return_data = true
  }
  metric_query {
    id = "visible"
    metric {
      namespace   = "AWS/SQS"
      metric_name = "ApproximateNumberOfMessagesVisible"
      dimensions  = { QueueName = local.queue.jobs_queue_name }
      stat        = "Maximum"
      period      = 60
    }
  }
  metric_query {
    id = "inflight"
    metric {
      namespace   = "AWS/SQS"
      metric_name = "ApproximateNumberOfMessagesNotVisible"
      dimensions  = { QueueName = local.queue.jobs_queue_name }
      stat        = "Maximum"
      period      = 60
    }
  }
}

output "asg_name" {
  value = aws_autoscaling_group.worker.name
}

output "launch_template_id" {
  value = aws_launch_template.worker.id
}

output "worker_security_group_id" {
  value = aws_security_group.worker.id
}

output "worker_log_group" {
  value = aws_cloudwatch_log_group.worker.name
}

output "worker_image" {
  value = local.image
}
