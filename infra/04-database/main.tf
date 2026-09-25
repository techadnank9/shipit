# 04-database: RDS PostgreSQL 17 on db.t4g.micro in the private subnets, KMS-encrypted,
# 7-day automated backups replicated to us-west-2, TLS enforced.
#
# The master password is an ephemeral value written with write-only arguments, so it never
# lands in Terraform state. Rotate by bumping db_password_version and re-applying.

variable "backup_region" {
  description = "Region the automated backups are replicated to."
  type        = string
  default     = "us-west-2"
}

variable "db_instance_class" {
  description = "RDS instance class."
  type        = string
  default     = "db.t4g.micro"
}

variable "db_allocated_storage" {
  description = "Initial storage in GiB (gp3)."
  type        = number
  default     = 20
}

variable "db_max_allocated_storage" {
  description = "Storage autoscaling ceiling in GiB."
  type        = number
  default     = 50
}

variable "db_deletion_protection" {
  description = "Deletion protection (true in prod)."
  type        = bool
  default     = true
}

variable "db_backup_replication" {
  description = "Replicate automated backups to the backup region."
  type        = bool
  default     = true
}

variable "db_password_version" {
  description = "Bump to generate and apply a new master password."
  type        = number
  default     = 1
}

provider "aws" {
  alias  = "backup"
  region = var.backup_region
  default_tags {
    tags = {
      project    = var.project
      env        = var.env
      layer      = "04-database"
      managed_by = "terraform"
    }
  }
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

locals {
  net      = data.terraform_remote_state.network.outputs
  sec      = data.terraform_remote_state.security.outputs
  storage  = data.terraform_remote_state.storage.outputs
  db_name  = "ccopy"
  db_user  = "ccopy"
  pg_major = "17"
}

resource "aws_db_subnet_group" "main" {
  name       = local.name
  subnet_ids = local.net.private_subnet_ids
}

# Clients (API Lambda, workers) join this group; the DB group only admits it.
resource "aws_security_group" "db_client" {
  # checkov:skip=CKV2_AWS_5:Attached in 06-api (Lambda) and 07-workers (launch template).
  name        = "${local.name}-db-client"
  description = "Members may connect to the Carbon Copy database"
  vpc_id      = local.net.vpc_id
  tags        = { Name = "${local.name}-db-client" }
}

resource "aws_security_group" "db" {
  name        = "${local.name}-db"
  description = "Postgres, reachable only from db-client members"
  vpc_id      = local.net.vpc_id
  tags        = { Name = "${local.name}-db" }
}

resource "aws_vpc_security_group_ingress_rule" "db_from_clients" {
  security_group_id            = aws_security_group.db.id
  description                  = "Postgres from db clients"
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  referenced_security_group_id = aws_security_group.db_client.id
}

resource "aws_vpc_security_group_egress_rule" "clients_to_db" {
  security_group_id            = aws_security_group.db_client.id
  description                  = "Postgres to the database"
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  referenced_security_group_id = aws_security_group.db.id
}

resource "aws_db_parameter_group" "main" {
  name   = "${local.name}-pg${local.pg_major}"
  family = "postgres${local.pg_major}"

  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }
  parameter {
    name  = "log_min_duration_statement"
    value = "1000"
  }
  parameter {
    name  = "log_connections"
    value = "1"
  }
  parameter {
    name  = "log_disconnections"
    value = "1"
  }
}

ephemeral "random_password" "db" {
  length  = 32
  special = true
  # URL-safe so the password can sit inside DATABASE_URL unescaped.
  override_special = "-_"
}

resource "aws_db_instance" "main" {
  # checkov:skip=CKV_AWS_157:Multi-AZ doubles the cost; 7-day backups + cross-region replication cover recovery for now.
  # checkov:skip=CKV_AWS_118:Enhanced monitoring not needed at this size; CloudWatch basic metrics + alarms in 09.
  # checkov:skip=CKV_AWS_353:Performance Insights is not offered on db.t4g.micro.
  # checkov:skip=CKV_AWS_293:Deletion protection is on in prod (tfvars); staging stays disposable.
  identifier     = local.name
  engine         = "postgres"
  engine_version = local.pg_major
  instance_class = var.db_instance_class

  db_name             = local.db_name
  username            = local.db_user
  password_wo         = ephemeral.random_password.db.result
  password_wo_version = var.db_password_version

  allocated_storage     = var.db_allocated_storage
  max_allocated_storage = var.db_max_allocated_storage
  storage_type          = "gp3"
  storage_encrypted     = true
  kms_key_id            = local.sec.kms_key_arn

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.db.id]
  publicly_accessible    = false
  multi_az               = false
  parameter_group_name   = aws_db_parameter_group.main.name

  iam_database_authentication_enabled = true
  auto_minor_version_upgrade          = true
  allow_major_version_upgrade         = false
  backup_retention_period             = 7
  backup_window                       = "10:00-10:30" # 03:00 PDT
  maintenance_window                  = "sun:11:00-sun:12:00"
  copy_tags_to_snapshot               = true
  deletion_protection                 = var.db_deletion_protection
  skip_final_snapshot                 = !var.db_deletion_protection
  final_snapshot_identifier           = var.db_deletion_protection ? "${local.name}-final" : null
  enabled_cloudwatch_logs_exports     = ["postgresql"]
  apply_immediately                   = var.env != "prod"
}

resource "aws_db_instance_automated_backups_replication" "backup" {
  count                  = var.db_backup_replication ? 1 : 0
  provider               = aws.backup
  source_db_instance_arn = aws_db_instance.main.arn
  kms_key_id             = local.sec.kms_backup_key_arn
  retention_period       = 7
}

# Connection details for the app. The secret itself lives in 03-storage.
resource "aws_secretsmanager_secret_version" "db" {
  secret_id = local.storage.secret_arns["db"]
  secret_string_wo = jsonencode({
    username = local.db_user
    password = ephemeral.random_password.db.result
    host     = aws_db_instance.main.address
    port     = aws_db_instance.main.port
    dbname   = local.db_name
    url      = "postgresql://${local.db_user}:${ephemeral.random_password.db.result}@${aws_db_instance.main.address}:${aws_db_instance.main.port}/${local.db_name}?sslmode=require"
  })
  secret_string_wo_version = var.db_password_version
}

output "db_address" {
  value = aws_db_instance.main.address
}

output "db_port" {
  value = aws_db_instance.main.port
}

output "db_client_security_group_id" {
  description = "Attach to anything that must reach Postgres."
  value       = aws_security_group.db_client.id
}

output "db_security_group_id" {
  value = aws_security_group.db.id
}

output "db_instance_id" {
  value = aws_db_instance.main.identifier
}
