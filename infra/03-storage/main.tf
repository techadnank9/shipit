# 03-storage: evidence bucket (Object Lock governance 365d, versioned, KMS, replicated to
# us-west-2), ECR repositories and Secrets Manager placeholders.

variable "backup_region" {
  description = "Region for the evidence replica."
  type        = string
  default     = "us-west-2"
}

variable "evidence_retention_days" {
  description = "Object Lock governance retention for proof evidence."
  type        = number
  default     = 365
}

variable "ecr_keep_images" {
  description = "Tagged images kept per ECR repository."
  type        = number
  default     = 30
}

provider "aws" {
  alias  = "backup"
  region = var.backup_region
  default_tags {
    tags = {
      project    = var.project
      env        = var.env
      layer      = "03-storage"
      managed_by = "terraform"
    }
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

locals {
  sec   = data.terraform_remote_state.security.outputs
  names = local.sec.names
}

# ====================================================================== evidence (primary)
resource "aws_s3_bucket" "evidence" {
  # checkov:skip=CKV_AWS_18:Server access logs would cost more than the evidence itself; CloudTrail data events can be enabled later.
  # checkov:skip=CKV2_AWS_62:No consumer for S3 event notifications.
  bucket              = local.names.evidence_bucket
  object_lock_enabled = true
  force_destroy       = false
}

resource "aws_s3_bucket_versioning" "evidence" {
  bucket = aws_s3_bucket.evidence.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_object_lock_configuration" "evidence" {
  bucket = aws_s3_bucket.evidence.id
  rule {
    default_retention {
      mode = "GOVERNANCE"
      days = var.evidence_retention_days
    }
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "evidence" {
  bucket = aws_s3_bucket.evidence.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = local.sec.kms_key_arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "evidence" {
  bucket                  = aws_s3_bucket.evidence.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "evidence" {
  bucket = aws_s3_bucket.evidence.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

data "aws_iam_policy_document" "evidence_tls" {
  statement {
    sid     = "DenyInsecureTransport"
    effect  = "Deny"
    actions = ["s3:*"]
    resources = [
      aws_s3_bucket.evidence.arn,
      "${aws_s3_bucket.evidence.arn}/*",
    ]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "evidence" {
  bucket = aws_s3_bucket.evidence.id
  policy = data.aws_iam_policy_document.evidence_tls.json
}

resource "aws_s3_bucket_lifecycle_configuration" "evidence" {
  bucket = aws_s3_bucket.evidence.id
  rule {
    id     = "tidy"
    status = "Enabled"
    filter {}
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
    # Old versions become deletable once their lock expires.
    noncurrent_version_expiration {
      noncurrent_days = var.evidence_retention_days + 30
    }
  }
}

# ====================================================================== evidence replica (us-west-2)
resource "aws_s3_bucket" "replica" {
  # checkov:skip=CKV_AWS_18:Replica of an unlogged bucket; access logs not worth the cost here.
  # checkov:skip=CKV_AWS_144:This is the cross-region replica.
  # checkov:skip=CKV2_AWS_62:No consumer for S3 event notifications.
  provider            = aws.backup
  bucket              = local.names.replica_bucket
  object_lock_enabled = true
}

resource "aws_s3_bucket_versioning" "replica" {
  provider = aws.backup
  bucket   = aws_s3_bucket.replica.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_object_lock_configuration" "replica" {
  provider = aws.backup
  bucket   = aws_s3_bucket.replica.id
  rule {
    default_retention {
      mode = "GOVERNANCE"
      days = var.evidence_retention_days
    }
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "replica" {
  provider = aws.backup
  bucket   = aws_s3_bucket.replica.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = local.sec.kms_backup_key_arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "replica" {
  provider                = aws.backup
  bucket                  = aws_s3_bucket.replica.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "replica" {
  provider = aws.backup
  bucket   = aws_s3_bucket.replica.id
  rule {
    id     = "cold"
    status = "Enabled"
    filter {}
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
    noncurrent_version_expiration {
      noncurrent_days = var.evidence_retention_days + 30
    }
  }
}

resource "aws_s3_bucket_replication_configuration" "evidence" {
  bucket = aws_s3_bucket.evidence.id
  role   = local.sec.replication_role_arn

  rule {
    id     = "to-${var.backup_region}"
    status = "Enabled"
    filter {}

    delete_marker_replication {
      status = "Disabled"
    }

    source_selection_criteria {
      sse_kms_encrypted_objects {
        status = "Enabled"
      }
    }

    destination {
      bucket        = aws_s3_bucket.replica.arn
      storage_class = "STANDARD_IA"
      encryption_configuration {
        replica_kms_key_id = local.sec.kms_backup_key_arn
      }
    }
  }

  depends_on = [aws_s3_bucket_versioning.evidence, aws_s3_bucket_versioning.replica]
}

# ====================================================================== ECR
resource "aws_ecr_repository" "repo" {
  for_each             = toset(["api", "worker", "sample"])
  name                 = "${local.names.ecr_prefix}/${each.key}"
  image_tag_mutability = "IMMUTABLE"
  force_delete         = false

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "KMS"
    kms_key         = local.sec.kms_key_arn
  }
}

resource "aws_ecr_lifecycle_policy" "repo" {
  for_each   = aws_ecr_repository.repo
  repository = each.value.name
  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Drop untagged layers after 7 days"
        selection    = { tagStatus = "untagged", countType = "sinceImagePushed", countUnit = "days", countNumber = 7 }
        action       = { type = "expire" }
      },
      {
        rulePriority = 2
        description  = "Keep the newest ${var.ecr_keep_images} images"
        selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = var.ecr_keep_images }
        action       = { type = "expire" }
      },
    ]
  })
}

# ====================================================================== secrets (values set by hand, see docs/deploy.md)
locals {
  secrets = {
    "github-app"       = "GitHub App: JSON {\"app_id\",\"private_key\",\"webhook_secret\"}"
    "slack-webhook"    = "Slack incoming webhook URL (plain string)"
    "localstack-token" = "LocalStack auth token for commercial use (plain string)"
    "db"               = "Postgres credentials JSON {\"username\",\"password\",\"host\",\"port\",\"dbname\",\"url\"}; written by 04-database"
  }
}

resource "aws_secretsmanager_secret" "app" {
  # checkov:skip=CKV2_AWS_57:Third-party credentials rotate at the provider; the db password is rotated by bumping db_password_version in 04-database.
  for_each                = local.secrets
  name                    = "${local.names.secret_prefix}/${each.key}"
  description             = each.value
  kms_key_id              = local.sec.kms_key_arn
  recovery_window_in_days = 7
}

# ====================================================================== outputs
output "evidence_bucket" {
  value = aws_s3_bucket.evidence.bucket
}

output "evidence_bucket_arn" {
  value = aws_s3_bucket.evidence.arn
}

output "replica_bucket" {
  value = aws_s3_bucket.replica.bucket
}

output "ecr_repository_urls" {
  value = { for k, r in aws_ecr_repository.repo : k => r.repository_url }
}

output "secret_arns" {
  value = { for k, s in aws_secretsmanager_secret.app : k => s.arn }
}
