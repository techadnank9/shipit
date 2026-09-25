# 01-network: VPC 10.40.0.0/16, 2 public + 2 private subnets across us-west-1's two AZs,
# one fck-nat instance (t4g.nano, ~$4/mo) instead of a NAT Gateway (~$33/mo + data),
# and a free S3 gateway endpoint (ECR image layers and evidence uploads skip the NAT).

variable "vpc_cidr" {
  description = "VPC CIDR."
  type        = string
  default     = "10.40.0.0/16"
}

variable "nat_instance_type" {
  description = "fck-nat instance type (Graviton)."
  type        = string
  default     = "t4g.nano"
}

data "aws_availability_zones" "available" {
  # checkov:skip=CKV_AWS_394:Only the first two zones are used (slice below); new zones cannot change the layout.
  state = "available"
  filter {
    name   = "zone-type"
    values = ["availability-zone"]
  }
}

locals {
  # us-west-1 exposes exactly two AZs to most accounts.
  azs             = slice(data.aws_availability_zones.available.names, 0, 2)
  public_subnets  = [for i in range(2) : cidrsubnet(var.vpc_cidr, 8, i)]     # 10.40.0.0/24, 10.40.1.0/24
  private_subnets = [for i in range(2) : cidrsubnet(var.vpc_cidr, 4, i + 1)] # 10.40.16.0/20, 10.40.32.0/20
}

resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_hostnames = true
  enable_dns_support   = true
  tags                 = { Name = local.name }
}

# ---------- VPC flow logs (all traffic, 14 days) ----------
# The KMS key is created in 02-security (after this layer), so this group uses CloudWatch's
# default encryption at rest.
resource "aws_cloudwatch_log_group" "flow" {
  # checkov:skip=CKV_AWS_158:Created before the layer-02 KMS key exists; CloudWatch encrypts at rest by default.
  # checkov:skip=CKV_AWS_338:Flow logs are for recent forensics; 14 days keeps cost near zero.
  name              = "/ccopy/${var.env}/vpc-flow"
  retention_in_days = 14
}

data "aws_iam_policy_document" "flow_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["vpc-flow-logs.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

data "aws_iam_policy_document" "flow" {
  statement {
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"]
    resources = ["${aws_cloudwatch_log_group.flow.arn}:*"]
  }
}

resource "aws_iam_role" "flow" {
  name               = "${local.name}-vpc-flow"
  assume_role_policy = data.aws_iam_policy_document.flow_trust.json
}

resource "aws_iam_role_policy" "flow" {
  name   = "write-flow-logs"
  role   = aws_iam_role.flow.id
  policy = data.aws_iam_policy_document.flow.json
}

resource "aws_flow_log" "main" {
  vpc_id                   = aws_vpc.main.id
  traffic_type             = "ALL"
  log_destination_type     = "cloud-watch-logs"
  log_destination          = aws_cloudwatch_log_group.flow.arn
  iam_role_arn             = aws_iam_role.flow.arn
  max_aggregation_interval = 600
}

# Lock down the default security group (nothing should use it).
resource "aws_default_security_group" "default" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${local.name}-default-unused" }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = local.name }
}

resource "aws_subnet" "public" {
  count             = 2
  vpc_id            = aws_vpc.main.id
  cidr_block        = local.public_subnets[count.index]
  availability_zone = local.azs[count.index]
  # Nothing but the NAT instance lives here and it gets its public IP explicitly.
  map_public_ip_on_launch = false
  tags                    = { Name = "${local.name}-public-${local.azs[count.index]}", tier = "public" }
}

resource "aws_subnet" "private" {
  count             = 2
  vpc_id            = aws_vpc.main.id
  cidr_block        = local.private_subnets[count.index]
  availability_zone = local.azs[count.index]
  tags              = { Name = "${local.name}-private-${local.azs[count.index]}", tier = "private" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${local.name}-public" }
}

resource "aws_route" "public_internet" {
  route_table_id         = aws_route_table.public.id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.main.id
}

resource "aws_route_table_association" "public" {
  count          = 2
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

resource "aws_route_table" "private" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${local.name}-private" }
}

resource "aws_route" "private_nat" {
  route_table_id         = aws_route_table.private.id
  destination_cidr_block = "0.0.0.0/0"
  network_interface_id   = aws_network_interface.nat.id
}

resource "aws_route_table_association" "private" {
  count          = 2
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private.id
}

# ---------- S3 gateway endpoint (free) ----------
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.private.id, aws_route_table.public.id]
  tags              = { Name = "${local.name}-s3" }
}

# ---------- fck-nat (https://fck-nat.dev) ----------
data "aws_ami" "fck_nat" {
  most_recent = true
  owners      = ["568608671756"] # fck-nat publisher account
  filter {
    name   = "name"
    values = ["fck-nat-al2023-*-arm64-ebs"]
  }
  filter {
    name   = "architecture"
    values = ["arm64"]
  }
}

resource "aws_security_group" "nat" {
  name        = "${local.name}-nat"
  description = "fck-nat: forward traffic from the VPC to the internet"
  vpc_id      = aws_vpc.main.id

  ingress {
    description = "All traffic from inside the VPC (to be NATed)"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = [var.vpc_cidr]
  }

  # checkov:skip=CKV_AWS_382:A NAT instance must forward arbitrary outbound traffic; worker/lambda SGs restrict ports.
  egress {
    description = "Forwarded outbound traffic"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${local.name}-nat" }
}

# Static ENI so the private route survives instance replacement.
resource "aws_network_interface" "nat" {
  subnet_id         = aws_subnet.public[0].id
  security_groups   = [aws_security_group.nat.id]
  source_dest_check = false
  description       = "${local.name} fck-nat"
  tags              = { Name = "${local.name}-nat" }
}

resource "aws_eip" "nat" {
  # checkov:skip=CKV2_AWS_19:Attached to the NAT instance's primary ENI (static, survives instance replacement).
  domain            = "vpc"
  network_interface = aws_network_interface.nat.id
  tags              = { Name = "${local.name}-nat" }
}

resource "aws_instance" "nat" {
  # checkov:skip=CKV_AWS_126:Detailed monitoring costs extra; basic metrics are enough for a NAT box.
  # checkov:skip=CKV2_AWS_41:The NAT instance calls no AWS APIs, so it needs no IAM role.
  ami           = data.aws_ami.fck_nat.id
  instance_type = var.nat_instance_type
  ebs_optimized = true

  primary_network_interface {
    network_interface_id = aws_network_interface.nat.id
  }

  metadata_options {
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
    http_endpoint               = "enabled"
  }

  root_block_device {
    volume_type = "gp3"
    volume_size = 8
    encrypted   = true
  }

  maintenance_options {
    auto_recovery = "default"
  }

  tags = { Name = "${local.name}-nat" }

  lifecycle {
    ignore_changes = [ami] # new fck-nat AMIs should not force a replacement on every plan
  }
}

output "vpc_id" {
  value = aws_vpc.main.id
}

output "vpc_cidr" {
  value = aws_vpc.main.cidr_block
}

output "public_subnet_ids" {
  value = aws_subnet.public[*].id
}

output "private_subnet_ids" {
  value = aws_subnet.private[*].id
}

output "private_route_table_id" {
  value = aws_route_table.private.id
}

output "s3_endpoint_prefix_list_id" {
  description = "Prefix list for the S3 gateway endpoint (usable in SG egress rules)."
  value       = aws_vpc_endpoint.s3.prefix_list_id
}

output "nat_public_ip" {
  description = "Egress IP for all private traffic (allowlist it at GitHub/Slack if needed)."
  value       = aws_eip.nat.public_ip
}
