# 08-web: the static dashboard (web/out) in a private S3 bucket behind CloudFront (OAC).
# The same distribution routes /api/*, /webhooks/*, /mcp* to the HTTP API from 06-api, so the
# dashboard calls its API same-origin and one optional WAF protects both.
#
# WAF trade-off: HTTP APIs cannot attach WAF. A web ACL on CloudFront costs ~$5/mo + $1 per
# rule group + $0.60 per million requests (~$9-10/mo here). On in prod, off in staging.
# Without it: Cognito JWTs on every private route + API Gateway throttling + CloudFront's
# built-in Shield Standard.

variable "domain_name" {
  description = "Optional custom domain for the dashboard (e.g. app.example.com). Empty = CloudFront domain."
  type        = string
  default     = ""
}

variable "acm_certificate_arn" {
  description = "ACM certificate in us-east-1 covering domain_name (required when domain_name is set)."
  type        = string
  default     = ""
}

variable "route53_zone_id" {
  description = "Optional Route 53 hosted zone id; when set, an alias record for domain_name is created."
  type        = string
  default     = ""
}

variable "enable_waf" {
  description = "Attach an AWS WAF web ACL (managed rules + rate limit) to CloudFront."
  type        = bool
  default     = true
}

variable "waf_rate_limit" {
  description = "Requests per 5 minutes per IP before WAF blocks."
  type        = number
  default     = 2000
}

provider "aws" {
  alias  = "us_east_1" # CloudFront-scoped WAF and ACM live here by AWS design
  region = "us-east-1"
  default_tags {
    tags = {
      project    = var.project
      env        = var.env
      layer      = "08-web"
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

data "terraform_remote_state" "api" {
  backend = "s3"
  config = {
    bucket = var.state_bucket
    key    = "${var.env}/06-api.tfstate"
    region = "us-west-1"
  }
}

locals {
  sec          = data.terraform_remote_state.security.outputs
  api          = data.terraform_remote_state.api.outputs
  custom       = var.domain_name != ""
  s3_origin_id = "web"
  api_origin   = "api"
  api_paths    = ["/api/*", "/webhooks/*", "/mcp", "/mcp/*"]
}

# ====================================================================== bucket
resource "aws_s3_bucket" "web" {
  # checkov:skip=CKV_AWS_18:Static build output; CloudFront is the only reader.
  # checkov:skip=CKV_AWS_144:Rebuilt from git on every deploy; replication adds nothing.
  # checkov:skip=CKV2_AWS_62:No consumer for S3 event notifications.
  # checkov:skip=CKV_AWS_145:Public dashboard assets; SSE-S3 avoids a KMS grant for CloudFront.
  bucket        = local.sec.names.web_bucket
  force_destroy = true
}

resource "aws_s3_bucket_versioning" "web" {
  bucket = aws_s3_bucket.web.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "web" {
  bucket = aws_s3_bucket.web.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "web" {
  bucket                  = aws_s3_bucket.web.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "web" {
  bucket = aws_s3_bucket.web.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "web" {
  bucket = aws_s3_bucket.web.id
  rule {
    id     = "old-builds"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration {
      noncurrent_days = 30
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}

data "aws_iam_policy_document" "web" {
  statement {
    sid       = "CloudFrontRead"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.web.arn}/*"]
    principals {
      type        = "Service"
      identifiers = ["cloudfront.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "AWS:SourceArn"
      values   = [aws_cloudfront_distribution.main.arn]
    }
  }

  statement {
    sid     = "DenyInsecureTransport"
    effect  = "Deny"
    actions = ["s3:*"]
    resources = [
      aws_s3_bucket.web.arn,
      "${aws_s3_bucket.web.arn}/*",
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

resource "aws_s3_bucket_policy" "web" {
  bucket = aws_s3_bucket.web.id
  policy = data.aws_iam_policy_document.web.json
}

# ====================================================================== CloudFront
data "aws_cloudfront_cache_policy" "optimized" {
  name = "Managed-CachingOptimized"
}

data "aws_cloudfront_cache_policy" "disabled" {
  name = "Managed-CachingDisabled"
}

data "aws_cloudfront_origin_request_policy" "all_but_host" {
  name = "Managed-AllViewerExceptHostHeader"
}

data "aws_cloudfront_response_headers_policy" "security" {
  name = "Managed-SecurityHeadersPolicy"
}

resource "aws_cloudfront_origin_access_control" "web" {
  name                              = "${local.name}-web"
  description                       = "Carbon Copy dashboard bucket"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

# Next.js static export: /project -> /project.html, /dir/ -> /dir/index.html.
resource "aws_cloudfront_function" "rewrite" {
  name    = "${local.name}-rewrite"
  runtime = "cloudfront-js-2.0"
  comment = "Map extensionless dashboard paths to exported .html files"
  publish = true
  code    = <<-JS
    function handler(event) {
      var req = event.request;
      var uri = req.uri;
      if (uri.endsWith('/')) {
        req.uri = uri + 'index.html';
      } else if (uri.lastIndexOf('.') <= uri.lastIndexOf('/')) {
        req.uri = uri + '.html';
      }
      return req;
    }
  JS
}

resource "aws_cloudfront_distribution" "main" {
  # checkov:skip=CKV_AWS_86:Access logs cost extra; API Gateway access logs cover the API paths.
  # checkov:skip=CKV_AWS_310:Single S3 origin for static assets; a failover origin adds cost, not safety here.
  # checkov:skip=CKV_AWS_374:Customers can be anywhere; no geo restriction.
  # checkov:skip=CKV_AWS_68:WAF is attached when enable_waf = true (prod); staging trades it for cost.
  # checkov:skip=CKV2_AWS_47:The WAF (when enabled) includes AWSManagedRulesKnownBadInputsRuleSet (Log4j).
  # checkov:skip=CKV_AWS_174:TLSv1.2_2021 is set whenever a custom certificate is used; the default *.cloudfront.net cert cannot be pinned.
  # checkov:skip=CKV2_AWS_42:A custom certificate is optional (domain_name var).
  enabled             = true
  is_ipv6_enabled     = true
  comment             = "Carbon Copy ${var.env}"
  default_root_object = "index.html"
  price_class         = "PriceClass_100"
  http_version        = "http2and3"
  aliases             = local.custom ? [var.domain_name] : []
  web_acl_id          = var.enable_waf ? aws_wafv2_web_acl.main[0].arn : null

  origin {
    origin_id                = local.s3_origin_id
    domain_name              = aws_s3_bucket.web.bucket_regional_domain_name
    origin_access_control_id = aws_cloudfront_origin_access_control.web.id
  }

  origin {
    origin_id   = local.api_origin
    domain_name = local.api.api_domain
    custom_origin_config {
      http_port                = 80
      https_port               = 443
      origin_protocol_policy   = "https-only"
      origin_ssl_protocols     = ["TLSv1.2"]
      origin_read_timeout      = 30
      origin_keepalive_timeout = 5
    }
  }

  default_cache_behavior {
    target_origin_id           = local.s3_origin_id
    viewer_protocol_policy     = "redirect-to-https"
    allowed_methods            = ["GET", "HEAD", "OPTIONS"]
    cached_methods             = ["GET", "HEAD"]
    compress                   = true
    cache_policy_id            = data.aws_cloudfront_cache_policy.optimized.id
    response_headers_policy_id = data.aws_cloudfront_response_headers_policy.security.id

    function_association {
      event_type   = "viewer-request"
      function_arn = aws_cloudfront_function.rewrite.arn
    }
  }

  dynamic "ordered_cache_behavior" {
    for_each = local.api_paths
    content {
      path_pattern               = ordered_cache_behavior.value
      target_origin_id           = local.api_origin
      viewer_protocol_policy     = "https-only"
      allowed_methods            = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
      cached_methods             = ["GET", "HEAD"]
      compress                   = true
      cache_policy_id            = data.aws_cloudfront_cache_policy.disabled.id
      origin_request_policy_id   = data.aws_cloudfront_origin_request_policy.all_but_host.id
      response_headers_policy_id = data.aws_cloudfront_response_headers_policy.security.id
    }
  }

  custom_error_response {
    error_code            = 404
    response_code         = 404
    response_page_path    = "/404.html"
    error_caching_min_ttl = 60
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = !local.custom
    acm_certificate_arn            = local.custom ? var.acm_certificate_arn : null
    ssl_support_method             = local.custom ? "sni-only" : null
    minimum_protocol_version       = local.custom ? "TLSv1.2_2021" : "TLSv1"
  }

  lifecycle {
    precondition {
      condition     = !local.custom || var.acm_certificate_arn != ""
      error_message = "domain_name needs acm_certificate_arn (an ACM cert in us-east-1)."
    }
  }
}

# ====================================================================== WAF (us-east-1, CLOUDFRONT scope)
resource "aws_wafv2_web_acl" "main" {
  # checkov:skip=CKV2_AWS_31:WAF logging (Firehose/CloudWatch) costs more than the ACL at this scale; sampled requests are kept.
  count    = var.enable_waf ? 1 : 0
  provider = aws.us_east_1
  name     = local.name
  scope    = "CLOUDFRONT"

  default_action {
    allow {}
  }

  rule {
    name     = "rate-limit"
    priority = 0
    action {
      block {}
    }
    statement {
      rate_based_statement {
        limit              = var.waf_rate_limit
        aggregate_key_type = "IP"
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.name}-rate-limit"
      sampled_requests_enabled   = true
    }
  }

  rule {
    name     = "ip-reputation"
    priority = 1
    override_action {
      none {}
    }
    statement {
      managed_rule_group_statement {
        vendor_name = "AWS"
        name        = "AWSManagedRulesAmazonIpReputationList"
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.name}-ip-reputation"
      sampled_requests_enabled   = true
    }
  }

  rule {
    name     = "common"
    priority = 2
    override_action {
      none {}
    }
    statement {
      managed_rule_group_statement {
        vendor_name = "AWS"
        name        = "AWSManagedRulesCommonRuleSet"
        # GitHub webhook payloads and change requests routinely exceed 8 KB.
        rule_action_override {
          name = "SizeRestrictions_BODY"
          action_to_use {
            count {}
          }
        }
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.name}-common"
      sampled_requests_enabled   = true
    }
  }

  rule {
    name     = "known-bad-inputs"
    priority = 3
    override_action {
      none {}
    }
    statement {
      managed_rule_group_statement {
        vendor_name = "AWS"
        name        = "AWSManagedRulesKnownBadInputsRuleSet"
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.name}-bad-inputs"
      sampled_requests_enabled   = true
    }
  }

  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = local.name
    sampled_requests_enabled   = true
  }
}

# ====================================================================== DNS (optional)
resource "aws_route53_record" "web" {
  for_each = local.custom && var.route53_zone_id != "" ? toset(["A", "AAAA"]) : toset([])
  zone_id  = var.route53_zone_id
  name     = var.domain_name
  type     = each.key
  alias {
    name                   = aws_cloudfront_distribution.main.domain_name
    zone_id                = aws_cloudfront_distribution.main.hosted_zone_id
    evaluate_target_health = false
  }
}

output "web_bucket" {
  value = aws_s3_bucket.web.bucket
}

output "distribution_id" {
  value = aws_cloudfront_distribution.main.id
}

output "distribution_domain" {
  value = aws_cloudfront_distribution.main.domain_name
}

output "web_base_url" {
  description = "Set web_base_url in envs/<env>.tfvars to this, then re-apply 06-api and 07-workers."
  value       = local.custom ? "https://${var.domain_name}" : "https://${aws_cloudfront_distribution.main.domain_name}"
}
