# 09-observability: one encrypted SNS alert topic, alarms on the things that page a human,
# spot interruption / launch failure events, and account budgets.
# Log groups (30-day retention, KMS) are created next to their producers in 06-api and
# 07-workers so they exist before the first log line.

variable "alert_emails" {
  description = "Addresses subscribed to alerts and budget notifications (each must confirm by email)."
  type        = list(string)
  default     = []
}

variable "create_account_budgets" {
  description = "Create the account-wide monthly budget (true in exactly one env per account)."
  type        = bool
  default     = false
}

variable "budget_thresholds_usd" {
  description = "Actual-spend notification thresholds in USD."
  type        = list(number)
  default     = [50, 150, 300, 500]
}

variable "queue_age_alarm_seconds" {
  description = "Alarm when the oldest queued job is older than this."
  type        = number
  default     = 900
}

data "terraform_remote_state" "security" {
  backend = "s3"
  config = {
    bucket = var.state_bucket
    key    = "${var.env}/02-security.tfstate"
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

data "terraform_remote_state" "api" {
  backend = "s3"
  config = {
    bucket = var.state_bucket
    key    = "${var.env}/06-api.tfstate"
    region = "us-west-1"
  }
}

data "terraform_remote_state" "workers" {
  backend = "s3"
  config = {
    bucket = var.state_bucket
    key    = "${var.env}/07-workers.tfstate"
    region = "us-west-1"
  }
}

locals {
  sec     = data.terraform_remote_state.security.outputs
  db      = data.terraform_remote_state.database.outputs
  queue   = data.terraform_remote_state.queue.outputs
  api     = data.terraform_remote_state.api.outputs
  workers = data.terraform_remote_state.workers.outputs
  alarm   = [aws_sns_topic.alerts.arn]
}

# ====================================================================== SNS
resource "aws_sns_topic" "alerts" {
  name              = "${local.name}-alerts"
  kms_master_key_id = local.sec.kms_key_arn
}

data "aws_iam_policy_document" "alerts" {
  statement {
    sid       = "AwsServicesPublish"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.alerts.arn]
    principals {
      type        = "Service"
      identifiers = ["cloudwatch.amazonaws.com", "events.amazonaws.com", "budgets.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_sns_topic_policy" "alerts" {
  arn    = aws_sns_topic.alerts.arn
  policy = data.aws_iam_policy_document.alerts.json
}

resource "aws_sns_topic_subscription" "email" {
  for_each  = toset(var.alert_emails)
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = each.value
}

# ====================================================================== alarms
resource "aws_cloudwatch_metric_alarm" "dlq_not_empty" {
  alarm_name          = "${local.name}-dlq-not-empty"
  alarm_description   = "A job failed 3 times and is parked in the DLQ. Inspect, fix, then redrive."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = local.queue.dlq_name }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm
  ok_actions          = local.alarm
}

resource "aws_cloudwatch_metric_alarm" "queue_age" {
  alarm_name          = "${local.name}-queue-age"
  alarm_description   = "Oldest job waited > ${var.queue_age_alarm_seconds}s: workers not scaling (spot capacity? AMI? image?)."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateAgeOfOldestMessage"
  dimensions          = { QueueName = local.queue.jobs_queue_name }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = var.queue_age_alarm_seconds
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm
  ok_actions          = local.alarm
}

resource "aws_cloudwatch_metric_alarm" "api_5xx" {
  alarm_name          = "${local.name}-api-5xx"
  alarm_description   = "API returned 5xx responses."
  namespace           = "AWS/ApiGateway"
  metric_name         = "5xx"
  dimensions          = { ApiId = local.api.api_id, Stage = "$default" }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 5
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm
  ok_actions          = local.alarm
}

resource "aws_cloudwatch_metric_alarm" "api_errors" {
  alarm_name          = "${local.name}-api-lambda-errors"
  alarm_description   = "API Lambda invocation errors (crashes, timeouts)."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = local.api.function_name }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 3
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm
}

resource "aws_cloudwatch_metric_alarm" "db_storage" {
  alarm_name          = "${local.name}-db-free-storage"
  alarm_description   = "Postgres free storage below 2 GiB."
  namespace           = "AWS/RDS"
  metric_name         = "FreeStorageSpace"
  dimensions          = { DBInstanceIdentifier = local.db.db_instance_id }
  statistic           = "Minimum"
  period              = 300
  evaluation_periods  = 2
  threshold           = 2147483648
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "missing"
  alarm_actions       = local.alarm
}

resource "aws_cloudwatch_metric_alarm" "db_cpu_credits" {
  alarm_name          = "${local.name}-db-cpu-credits"
  alarm_description   = "db.t4g.micro burst credits nearly exhausted: time to size up."
  namespace           = "AWS/RDS"
  metric_name         = "CPUCreditBalance"
  dimensions          = { DBInstanceIdentifier = local.db.db_instance_id }
  statistic           = "Minimum"
  period              = 900
  evaluation_periods  = 2
  threshold           = 20
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "missing"
  alarm_actions       = local.alarm
}

# ====================================================================== EventBridge
resource "aws_cloudwatch_event_rule" "spot_interruption" {
  name        = "${local.name}-spot-interruption"
  description = "Two-minute spot interruption warnings (the job's SQS message is redelivered)."
  event_pattern = jsonencode({
    source        = ["aws.ec2"]
    "detail-type" = ["EC2 Spot Instance Interruption Warning"]
  })
}

resource "aws_cloudwatch_event_target" "spot_interruption" {
  rule      = aws_cloudwatch_event_rule.spot_interruption.name
  target_id = "sns"
  arn       = aws_sns_topic.alerts.arn
}

resource "aws_cloudwatch_event_rule" "launch_failed" {
  name        = "${local.name}-worker-launch-failed"
  description = "Worker ASG could not launch an instance (spot capacity, AMI, quota)."
  event_pattern = jsonencode({
    source        = ["aws.autoscaling"]
    "detail-type" = ["EC2 Instance Launch Unsuccessful"]
    detail        = { AutoScalingGroupName = [local.workers.asg_name] }
  })
}

resource "aws_cloudwatch_event_target" "launch_failed" {
  rule      = aws_cloudwatch_event_rule.launch_failed.name
  target_id = "sns"
  arn       = aws_sns_topic.alerts.arn
}

# ====================================================================== budgets
resource "aws_budgets_budget" "monthly" {
  count        = var.create_account_budgets ? 1 : 0
  name         = "carboncopy-monthly"
  budget_type  = "COST"
  limit_amount = tostring(max(var.budget_thresholds_usd...))
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  # Credits would net spend to ~$0 and silence every alert; watch gross spend instead.
  cost_types {
    include_credit  = false
    include_refund  = false
    include_support = true
    include_tax     = true
  }

  dynamic "notification" {
    for_each = var.budget_thresholds_usd
    content {
      comparison_operator        = "GREATER_THAN"
      threshold                  = notification.value
      threshold_type             = "ABSOLUTE_VALUE"
      notification_type          = "ACTUAL"
      subscriber_email_addresses = var.alert_emails
      subscriber_sns_topic_arns  = [aws_sns_topic.alerts.arn]
    }
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = var.alert_emails
    subscriber_sns_topic_arns  = [aws_sns_topic.alerts.arn]
  }
}

output "alerts_topic_arn" {
  value = aws_sns_topic.alerts.arn
}
