# Shared boilerplate for every layer (kept identical across layers; edit all together).
terraform {
  required_version = ">= 1.11"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.7"
    }
  }

  # Partial config: bucket and key come from infra/tf.sh (key = <env>/<layer>.tfstate).
  backend "s3" {
    region       = "us-west-1"
    encrypt      = true
    use_lockfile = true
  }
}

variable "env" {
  description = "Environment name."
  type        = string
  validation {
    condition     = contains(["staging", "prod"], var.env)
    error_message = "env must be staging or prod."
  }
}

variable "region" {
  description = "Home region. Carbon Copy lives in us-west-1 only (backups go to us-west-2)."
  type        = string
  default     = "us-west-1"
  validation {
    condition     = var.region == "us-west-1"
    error_message = "Home region is us-west-1."
  }
}

variable "state_bucket" {
  description = "S3 bucket holding Terraform state for every layer (passed by infra/tf.sh)."
  type        = string
  default     = ""
}

variable "project" {
  description = "Project tag value."
  type        = string
  default     = "carboncopy"
}

provider "aws" {
  region = var.region
  default_tags {
    tags = {
      project    = var.project
      env        = var.env
      layer      = "08-web"
      managed_by = "terraform"
    }
  }
}

locals {
  name       = "ccopy-${var.env}"
  account_id = data.aws_caller_identity.current.account_id
  partition  = data.aws_partition.current.partition
}

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}
