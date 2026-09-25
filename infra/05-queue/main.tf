# 05-queue: SQS job queue + dead-letter queue. A job that fails 3 receives lands in the DLQ
# (alarmed in 09). Visibility timeout 3600s: a worker must extend it (ChangeMessageVisibility)
# for jobs that wait on a human gate longer than an hour.

variable "visibility_timeout_seconds" {
  description = "Job visibility timeout."
  type        = number
  default     = 3600
}

variable "max_receive_count" {
  description = "Receives before a job goes to the DLQ."
  type        = number
  default     = 3
}

data "terraform_remote_state" "security" {
  backend = "s3"
  config = {
    bucket = var.state_bucket
    key    = "${var.env}/02-security.tfstate"
    region = "us-west-1"
  }
}

locals {
  sec = data.terraform_remote_state.security.outputs
}

resource "aws_sqs_queue" "dlq" {
  name                              = "${local.sec.names.jobs_queue}-dlq"
  message_retention_seconds         = 1209600 # 14 days
  kms_master_key_id                 = local.sec.kms_key_arn
  kms_data_key_reuse_period_seconds = 3600
}

resource "aws_sqs_queue" "jobs" {
  name                              = local.sec.names.jobs_queue
  visibility_timeout_seconds        = var.visibility_timeout_seconds
  message_retention_seconds         = 345600 # 4 days
  receive_wait_time_seconds         = 20     # long polling: fewer empty receives
  kms_master_key_id                 = local.sec.kms_key_arn
  kms_data_key_reuse_period_seconds = 3600

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq.arn
    maxReceiveCount     = var.max_receive_count
  })
}

resource "aws_sqs_queue_redrive_allow_policy" "dlq" {
  queue_url = aws_sqs_queue.dlq.id
  redrive_allow_policy = jsonencode({
    redrivePermission = "byQueue"
    sourceQueueArns   = [aws_sqs_queue.jobs.arn]
  })
}

output "jobs_queue_url" {
  value = aws_sqs_queue.jobs.url
}

output "jobs_queue_arn" {
  value = aws_sqs_queue.jobs.arn
}

output "jobs_queue_name" {
  value = aws_sqs_queue.jobs.name
}

output "dlq_url" {
  value = aws_sqs_queue.dlq.url
}

output "dlq_name" {
  value = aws_sqs_queue.dlq.name
}
