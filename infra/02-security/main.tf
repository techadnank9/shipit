# 02-security: one multi-region KMS key (replica in us-west-2 for backups), IAM roles for the
# API Lambda, the worker instances and S3 replication, and GitHub OIDC deploy roles.
#
# Grants to data resources use the deterministic names the later layers create
# (ccopy-<env>-*, secrets under ccopy/<env>/), so every permission lives in this one file
# and the layers can be applied strictly in order.

variable "backup_region" {
  description = "Region for backups and replicas."
  type        = string
  default     = "us-west-2"
}

variable "github_repo" {
  description = "GitHub repository allowed to deploy (owner/name)."
  type        = string
  default     = "techadnank9/carboncopy"
}

variable "create_github_oidc_provider" {
  description = "Create the account-wide GitHub OIDC provider (true in exactly one env per account)."
  type        = bool
  default     = false
}

variable "bedrock_profile_prefix" {
  description = "Bedrock cross-region inference profile prefix Claude is called through."
  type        = string
  default     = "us"
}

provider "aws" {
  alias  = "backup"
  region = var.backup_region
  default_tags {
    tags = {
      project    = var.project
      env        = var.env
      layer      = "02-security"
      managed_by = "terraform"
    }
  }
}

locals {
  arn_prefix_sm   = "arn:${local.partition}:secretsmanager:${var.region}:${local.account_id}:secret:ccopy/${var.env}/*"
  evidence_bucket = "${local.name}-evidence-${local.account_id}"
  replica_bucket  = "${local.name}-evidence-replica-${local.account_id}"
  web_bucket      = "${local.name}-web-${local.account_id}"
  jobs_queue_arn  = "arn:${local.partition}:sqs:${var.region}:${local.account_id}:${local.name}-jobs"
  ecr_repo_arns   = "arn:${local.partition}:ecr:${var.region}:${local.account_id}:repository/${local.name}/*"
  worker_asg_name = "${local.name}-workers"
  github_oidc_url = "token.actions.githubusercontent.com"
  deploy_subjects = var.env == "prod" ? [
    "repo:${var.github_repo}:environment:production",
    ] : [
    "repo:${var.github_repo}:ref:refs/heads/main",
    "repo:${var.github_repo}:environment:staging",
  ]
}

# ====================================================================== KMS
data "aws_iam_policy_document" "kms" {
  # checkov:skip=CKV_AWS_111:Key policy: "*" means this key only; the root statement delegates to IAM (AWS default).
  # checkov:skip=CKV_AWS_356:Key policy: "*" means this key only.
  # checkov:skip=CKV_AWS_109:Key policy: account root must administer its own key.
  statement {
    sid       = "AccountAdmin"
    actions   = ["kms:*"]
    resources = ["*"]
    principals {
      type        = "AWS"
      identifiers = ["arn:${local.partition}:iam::${local.account_id}:root"]
    }
  }

  statement {
    sid       = "CloudWatchLogs"
    actions   = ["kms:Encrypt*", "kms:Decrypt*", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:Describe*"]
    resources = ["*"]
    principals {
      type        = "Service"
      identifiers = ["logs.${var.region}.amazonaws.com"]
    }
    condition {
      test     = "ArnLike"
      variable = "kms:EncryptionContext:aws:logs:arn"
      values   = ["arn:${local.partition}:logs:${var.region}:${local.account_id}:log-group:*"]
    }
  }

  # CloudWatch alarms, EventBridge and Budgets publish to the encrypted SNS alert topic.
  statement {
    sid       = "AlertPublishers"
    actions   = ["kms:Decrypt", "kms:GenerateDataKey*"]
    resources = ["*"]
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

resource "aws_kms_key" "main" {
  description             = "${local.name}: evidence, database, queue, secrets, logs"
  multi_region            = true
  enable_key_rotation     = true
  deletion_window_in_days = 30
  policy                  = data.aws_iam_policy_document.kms.json
}

resource "aws_kms_alias" "main" {
  name          = "alias/${local.name}"
  target_key_id = aws_kms_key.main.key_id
}

# Same key material in us-west-2 so replicated evidence and backup snapshots stay decryptable.
resource "aws_kms_replica_key" "backup" {
  provider                = aws.backup
  description             = "${local.name}: backup replica"
  primary_key_arn         = aws_kms_key.main.arn
  deletion_window_in_days = 30
  policy                  = data.aws_iam_policy_document.kms.json
}

resource "aws_kms_alias" "backup" {
  provider      = aws.backup
  name          = "alias/${local.name}"
  target_key_id = aws_kms_replica_key.backup.key_id
}

# ====================================================================== shared app policy
# Both the API and the workers read secrets, use the queue, write evidence and call Claude.
data "aws_iam_policy_document" "app" {
  # checkov:skip=CKV_AWS_356:Only bedrock:ListInferenceProfiles uses "*" (the API has no resource scoping).
  statement {
    sid = "BedrockInvokeViaProfile"
    actions = [
      "bedrock:InvokeModel",
      "bedrock:InvokeModelWithResponseStream",
    ]
    resources = [
      "arn:${local.partition}:bedrock:${var.region}:${local.account_id}:inference-profile/${var.bedrock_profile_prefix}.anthropic.*",
    ]
  }

  # A cross-region profile routes to the foundation model in other US regions.
  statement {
    sid = "BedrockFoundationModelsThroughProfile"
    actions = [
      "bedrock:InvokeModel",
      "bedrock:InvokeModelWithResponseStream",
    ]
    resources = ["arn:${local.partition}:bedrock:*::foundation-model/anthropic.*"]
    condition {
      test     = "StringLike"
      variable = "bedrock:InferenceProfileArn"
      values   = ["arn:${local.partition}:bedrock:${var.region}:${local.account_id}:inference-profile/${var.bedrock_profile_prefix}.anthropic.*"]
    }
  }

  statement {
    sid       = "BedrockDescribeProfile"
    actions   = ["bedrock:GetInferenceProfile"]
    resources = ["arn:${local.partition}:bedrock:${var.region}:${local.account_id}:inference-profile/${var.bedrock_profile_prefix}.anthropic.*"]
  }

  statement {
    sid       = "BedrockListProfiles"
    actions   = ["bedrock:ListInferenceProfiles"]
    resources = ["*"] # List* has no resource-level scoping
  }

  statement {
    sid       = "Secrets"
    actions   = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
    resources = [local.arn_prefix_sm]
  }

  statement {
    sid = "Queue"
    actions = [
      "sqs:SendMessage",
      "sqs:ReceiveMessage",
      "sqs:DeleteMessage",
      "sqs:ChangeMessageVisibility",
      "sqs:GetQueueAttributes",
      "sqs:GetQueueUrl",
    ]
    resources = [local.jobs_queue_arn]
  }

  statement {
    sid       = "EvidenceList"
    actions   = ["s3:ListBucket", "s3:GetBucketLocation"]
    resources = ["arn:${local.partition}:s3:::${local.evidence_bucket}"]
  }

  statement {
    sid       = "EvidenceObjects"
    actions   = ["s3:GetObject", "s3:GetObjectVersion", "s3:PutObject", "s3:AbortMultipartUpload"]
    resources = ["arn:${local.partition}:s3:::${local.evidence_bucket}/*"]
  }

  statement {
    sid       = "Kms"
    actions   = ["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey*", "kms:DescribeKey"]
    resources = [aws_kms_key.main.arn]
  }
}

resource "aws_iam_policy" "app" {
  name        = "${local.name}-app"
  description = "Carbon Copy API + worker data access (${var.env})"
  policy      = data.aws_iam_policy_document.app.json
}

# ====================================================================== API Lambda role
data "aws_iam_policy_document" "lambda_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "api" {
  name               = "${local.name}-api"
  assume_role_policy = data.aws_iam_policy_document.lambda_trust.json
}

resource "aws_iam_role_policy_attachment" "api" {
  for_each = {
    app  = aws_iam_policy.app.arn
    vpc  = "arn:${local.partition}:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
    xray = "arn:${local.partition}:iam::aws:policy/AWSXRayDaemonWriteAccess"
  }
  role       = aws_iam_role.api.name
  policy_arn = each.value
}

# ====================================================================== worker role
data "aws_iam_policy_document" "ec2_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "worker" {
  statement {
    sid       = "EcrAuth"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid       = "EcrPull"
    actions   = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"]
    resources = [local.ecr_repo_arns]
  }

  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"]
    resources = ["arn:${local.partition}:logs:${var.region}:${local.account_id}:log-group:/ccopy/${var.env}/worker*"]
  }

  # Lets a worker protect itself from scale-in while it holds a job (optional; see docs/deploy.md).
  statement {
    sid       = "SelfProtect"
    actions   = ["autoscaling:SetInstanceProtection"]
    resources = ["arn:${local.partition}:autoscaling:${var.region}:${local.account_id}:autoScalingGroup:*:autoScalingGroupName/${local.worker_asg_name}"]
  }
}

resource "aws_iam_policy" "worker" {
  name        = "${local.name}-worker"
  description = "Carbon Copy worker host (${var.env})"
  policy      = data.aws_iam_policy_document.worker.json
}

resource "aws_iam_role" "worker" {
  name               = "${local.name}-worker"
  assume_role_policy = data.aws_iam_policy_document.ec2_trust.json
}

resource "aws_iam_role_policy_attachment" "worker" {
  for_each = {
    app    = aws_iam_policy.app.arn
    worker = aws_iam_policy.worker.arn
    ssm    = "arn:${local.partition}:iam::aws:policy/AmazonSSMManagedInstanceCore"
    cwa    = "arn:${local.partition}:iam::aws:policy/CloudWatchAgentServerPolicy"
  }
  role       = aws_iam_role.worker.name
  policy_arn = each.value
}

resource "aws_iam_instance_profile" "worker" {
  name = "${local.name}-worker"
  role = aws_iam_role.worker.name
}

# ====================================================================== S3 replication role
data "aws_iam_policy_document" "s3_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["s3.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "replication" {
  statement {
    actions   = ["s3:GetReplicationConfiguration", "s3:ListBucket"]
    resources = ["arn:${local.partition}:s3:::${local.evidence_bucket}"]
  }
  statement {
    actions = [
      "s3:GetObjectVersionForReplication",
      "s3:GetObjectVersionAcl",
      "s3:GetObjectVersionTagging",
      "s3:GetObjectRetention",
      "s3:GetObjectLegalHold",
    ]
    resources = ["arn:${local.partition}:s3:::${local.evidence_bucket}/*"]
  }
  statement {
    actions = [
      "s3:ReplicateObject",
      "s3:ReplicateDelete",
      "s3:ReplicateTags",
      "s3:ObjectOwnerOverrideToBucketOwner",
    ]
    resources = ["arn:${local.partition}:s3:::${local.replica_bucket}/*"]
  }
  statement {
    actions   = ["kms:Decrypt"]
    resources = [aws_kms_key.main.arn]
  }
  statement {
    actions   = ["kms:Encrypt", "kms:GenerateDataKey*"]
    resources = [aws_kms_replica_key.backup.arn]
  }
}

resource "aws_iam_role" "replication" {
  name               = "${local.name}-s3-replication"
  assume_role_policy = data.aws_iam_policy_document.s3_trust.json
}

resource "aws_iam_role_policy" "replication" {
  name   = "replicate-evidence"
  role   = aws_iam_role.replication.id
  policy = data.aws_iam_policy_document.replication.json
}

# ====================================================================== GitHub OIDC deploy role
resource "aws_iam_openid_connect_provider" "github" {
  count          = var.create_github_oidc_provider ? 1 : 0
  url            = "https://${local.github_oidc_url}"
  client_id_list = ["sts.amazonaws.com"]
}

data "aws_iam_openid_connect_provider" "github" {
  count = var.create_github_oidc_provider ? 0 : 1
  url   = "https://${local.github_oidc_url}"
}

locals {
  github_oidc_arn = var.create_github_oidc_provider ? aws_iam_openid_connect_provider.github[0].arn : data.aws_iam_openid_connect_provider.github[0].arn
}

data "aws_iam_policy_document" "deploy_trust" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [local.github_oidc_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.github_oidc_url}:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.github_oidc_url}:sub"
      values   = local.deploy_subjects
    }
  }
}

# What CI needs: push images, apply 06-api and 07-workers, sync the dashboard, read state.
# Foundation layers (01-05, 08, 09) are applied by a human with admin credentials.
data "aws_iam_policy_document" "deploy" {
  # checkov:skip=CKV_AWS_356:Describe/List and Terraform-managed services need "*"; scoped by region condition.
  # checkov:skip=CKV_AWS_111:Write access is limited to the services the app layers manage, in us-west-1 only.
  # checkov:skip=CKV_AWS_109:No IAM write; kms/iam actions are use-only (PassRole scoped to two roles).
  statement {
    sid       = "StateBucket"
    actions   = ["s3:ListBucket", "s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = ["arn:${local.partition}:s3:::${var.state_bucket}", "arn:${local.partition}:s3:::${var.state_bucket}/*"]
  }

  statement {
    sid       = "EcrAuth"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid = "EcrPush"
    actions = [
      "ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:CompleteLayerUpload",
      "ecr:DescribeImages", "ecr:DescribeRepositories", "ecr:GetDownloadUrlForLayer",
      "ecr:InitiateLayerUpload", "ecr:PutImage", "ecr:UploadLayerPart", "ecr:ListImages",
    ]
    resources = [local.ecr_repo_arns]
  }

  # Prod promotes the exact image digests that passed staging.
  statement {
    sid       = "EcrPullStaging"
    actions   = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:DescribeImages", "ecr:BatchCheckLayerAvailability"]
    resources = ["arn:${local.partition}:ecr:${var.region}:${local.account_id}:repository/ccopy-staging/*"]
  }

  statement {
    sid = "AppLayers"
    actions = [
      "lambda:*",
      "apigateway:*",
      "cognito-idp:*",
      "autoscaling:*",
      "cloudwatch:*",
      "logs:*",
      "ec2:Describe*",
      "ec2:CreateLaunchTemplate", "ec2:CreateLaunchTemplateVersion", "ec2:ModifyLaunchTemplate",
      "ec2:DeleteLaunchTemplate", "ec2:DeleteLaunchTemplateVersions",
      "ec2:CreateSecurityGroup", "ec2:DeleteSecurityGroup",
      "ec2:AuthorizeSecurityGroupEgress", "ec2:AuthorizeSecurityGroupIngress",
      "ec2:RevokeSecurityGroupEgress", "ec2:RevokeSecurityGroupIngress",
      "ec2:CreateTags", "ec2:DeleteTags", "ec2:RunInstances", "ec2:GetLaunchTemplateData",
      "ec2:CreateNetworkInterface", "ec2:DeleteNetworkInterface",
      "ssm:GetParameter", "ssm:GetParameters",
      "sqs:GetQueueAttributes", "sqs:ListQueueTags",
      "rds:Describe*", "secretsmanager:DescribeSecret",
      "events:*", "sns:Get*", "sns:List*",
      "iam:GetRole", "iam:GetInstanceProfile", "iam:ListRolePolicies", "iam:ListAttachedRolePolicies",
      "iam:CreateServiceLinkedRole",
    ]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "aws:RequestedRegion"
      values   = [var.region]
    }
  }

  statement {
    sid       = "PassAppRoles"
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.api.arn, aws_iam_role.worker.arn]
  }

  statement {
    sid       = "UseKey"
    actions   = ["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey*", "kms:DescribeKey", "kms:CreateGrant"]
    resources = [aws_kms_key.main.arn]
  }

  statement {
    sid       = "WebSync"
    actions   = ["s3:ListBucket", "s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = ["arn:${local.partition}:s3:::${local.web_bucket}", "arn:${local.partition}:s3:::${local.web_bucket}/*"]
  }

  statement {
    sid       = "WebInvalidate"
    actions   = ["cloudfront:CreateInvalidation", "cloudfront:GetInvalidation", "cloudfront:ListDistributions", "cloudfront:GetDistribution"]
    resources = ["*"]
  }
}

resource "aws_iam_policy" "deploy" {
  name        = "${local.name}-deploy"
  description = "GitHub Actions deploy (${var.env})"
  policy      = data.aws_iam_policy_document.deploy.json
}

resource "aws_iam_role" "deploy" {
  name                 = "${local.name}-deploy"
  assume_role_policy   = data.aws_iam_policy_document.deploy_trust.json
  max_session_duration = 3600
}

resource "aws_iam_role_policy_attachment" "deploy" {
  role       = aws_iam_role.deploy.name
  policy_arn = aws_iam_policy.deploy.arn
}

# ====================================================================== outputs
output "kms_key_arn" {
  value = aws_kms_key.main.arn
}

output "kms_key_id" {
  value = aws_kms_key.main.key_id
}

output "kms_backup_key_arn" {
  value = aws_kms_replica_key.backup.arn
}

output "api_role_arn" {
  value = aws_iam_role.api.arn
}

output "worker_role_arn" {
  value = aws_iam_role.worker.arn
}

output "worker_instance_profile_name" {
  value = aws_iam_instance_profile.worker.name
}

output "worker_instance_profile_arn" {
  value = aws_iam_instance_profile.worker.arn
}

output "replication_role_arn" {
  value = aws_iam_role.replication.arn
}

output "deploy_role_arn" {
  description = "Put this in the GitHub Environment variable AWS_DEPLOY_ROLE_ARN."
  value       = aws_iam_role.deploy.arn
}

output "names" {
  description = "Deterministic names later layers must use (grants above depend on them)."
  value = {
    evidence_bucket = local.evidence_bucket
    replica_bucket  = local.replica_bucket
    web_bucket      = local.web_bucket
    jobs_queue      = "${local.name}-jobs"
    ecr_prefix      = local.name
    secret_prefix   = "ccopy/${var.env}"
    worker_asg      = local.worker_asg_name
  }
}
