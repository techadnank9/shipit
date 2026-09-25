# Carbon Copy worker AMI: Amazon Linux 2023 arm64 + Docker + Compose/Buildx plugins +
# gVisor (runsc, registered as a Docker runtime) + CloudWatch agent + pre-pulled copy images.
#
#   cd packer && packer init . && packer build -var env=staging .
#
# Output AMI name: ccopy-worker-<timestamp> (07-workers picks the newest one by default).

packer {
  required_plugins {
    amazon = {
      source  = "github.com/hashicorp/amazon"
      version = "~> 1.3"
    }
  }
}

variable "region" {
  type    = string
  default = "us-west-1"
}

variable "env" {
  type    = string
  default = "staging"
}

variable "instance_type" {
  type    = string
  default = "c7g.large"
}

variable "compose_version" {
  type    = string
  default = "v5.5.1"
}

variable "buildx_version" {
  type    = string
  default = "v0.37.1"
}

variable "gvisor_release" {
  description = "gVisor release directory under storage.googleapis.com/gvisor/releases/release/ ('latest' or a dated release, e.g. 20260921). Pin for reproducible AMIs."
  type        = string
  default     = "latest"
}

variable "default_runtime" {
  description = "Docker default runtime. Keep runc: copies opt in to runsc per service (runtime: runsc)."
  type        = string
  default     = "runc"
}

variable "prepull_images" {
  description = "Images every copy uses; pre-pulling saves minutes per cold worker and Docker Hub rate limits."
  type        = list(string)
  default = [
    "postgres:17-alpine",
    "localstack/localstack:4.14",
    "axllent/mailpit:v1.27",
    "curlimages/curl:8.16.0",
    "python:3.12-slim",
  ]
}

locals {
  stamp = formatdate("YYYYMMDD-hhmmss", timestamp())
}

source "amazon-ebs" "worker" {
  region          = var.region
  instance_type   = var.instance_type
  ssh_username    = "ec2-user"
  ami_name        = "ccopy-worker-${local.stamp}"
  ami_description = "Carbon Copy worker (AL2023 arm64, docker, gVisor, CloudWatch agent)"

  source_ami_filter {
    owners      = ["amazon"]
    most_recent = true
    filters = {
      name                = "al2023-ami-2023.*-kernel-*-arm64"
      architecture        = "arm64"
      virtualization-type = "hvm"
      root-device-type    = "ebs"
    }
  }

  # Temporary builder in the default VPC with a throwaway key; IMDSv2 only.
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }
  imds_support = "v2.0"

  launch_block_device_mappings {
    device_name           = "/dev/xvda"
    volume_size           = 30
    volume_type           = "gp3"
    encrypted             = true
    delete_on_termination = true
  }

  tags = {
    Name       = "ccopy-worker-${local.stamp}"
    project    = "carboncopy"
    env        = var.env
    managed_by = "packer"
    base_ami   = "{{ .SourceAMI }}"
  }
  run_tags = {
    project = "carboncopy"
    env     = var.env
  }
}

build {
  sources = ["source.amazon-ebs.worker"]

  provisioner "shell" {
    environment_vars = [
      "COMPOSE_VERSION=${var.compose_version}",
      "BUILDX_VERSION=${var.buildx_version}",
      "GVISOR_RELEASE=${var.gvisor_release}",
      "DEFAULT_RUNTIME=${var.default_runtime}",
      "PREPULL=${join(" ", var.prepull_images)}",
    ]
    execute_command = "chmod +x {{ .Path }}; {{ .Vars }} sudo -E bash -euxo pipefail '{{ .Path }}'"
    script          = "${path.root}/scripts/provision.sh"
  }

  post-processor "manifest" {
    output     = "manifest.json"
    strip_path = true
  }
}
