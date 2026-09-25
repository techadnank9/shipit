# 06-api: the FastAPI server as an arm64 Lambda container image (AWS Lambda Web Adapter
# inside the image turns Lambda events into HTTP on :8080, so the app code is unchanged),
# fronted by an API Gateway HTTP API with a Cognito JWT authorizer.
#
# WAF: HTTP APIs cannot attach WAF directly, so 08-web puts CloudFront (+ optional WAF) in
# front of both the dashboard and this API on one origin. The execute-api URL still works
# directly, but every non-public route requires a Cognito JWT either way.

variable "image_tag" {
  description = "API image tag in ECR (the git sha; set by CI)."
  type        = string
  default     = ""
}

variable "ccopy_ai" {
  description = "standin (scripted, sample-app only) or claude (real Claude via Bedrock)."
  type        = string
  default     = "standin"
  validation {
    condition     = contains(["standin", "claude"], var.ccopy_ai)
    error_message = "ccopy_ai must be standin or claude."
  }
}

variable "ccopy_model" {
  description = "Model when ccopy_ai = claude. On Bedrock use an inference profile id (us.anthropic.…). Empty = app default."
  type        = string
  default     = ""
}

variable "web_base_url" {
  description = "Public base URL of the dashboard (CloudFront or custom domain), no trailing slash."
  type        = string
  default     = "http://localhost:3000"
}

variable "api_memory_mb" {
  description = "Lambda memory (CPU scales with it)."
  type        = number
  default     = 1024
}

variable "api_throttle_rate" {
  description = "Steady-state requests per second across the API."
  type        = number
  default     = 20
}

variable "api_throttle_burst" {
  description = "Burst request limit across the API."
  type        = number
  default     = 50
}

variable "log_retention_days" {
  description = "CloudWatch log retention."
  type        = number
  default     = 30
}

variable "cognito_deletion_protection" {
  description = "Protect the user pool from deletion (true in prod)."
  type        = bool
  default     = true
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

locals {
  net     = data.terraform_remote_state.network.outputs
  sec     = data.terraform_remote_state.security.outputs
  storage = data.terraform_remote_state.storage.outputs
  db      = data.terraform_remote_state.database.outputs
  queue   = data.terraform_remote_state.queue.outputs
  secrets = local.storage.secret_arns

  # ENV=secret-arn[#json-key]; docker/bootstrap.py resolves these at container start.
  secret_env = join(",", [
    "DATABASE_URL=${local.secrets["db"]}#url",
    "GITHUB_APP_ID=${local.secrets["github-app"]}#app_id",
    "GITHUB_APP_PRIVATE_KEY=${local.secrets["github-app"]}#private_key",
    "GITHUB_WEBHOOK_SECRET=${local.secrets["github-app"]}#webhook_secret",
    "SLACK_WEBHOOK_URL=${local.secrets["slack-webhook"]}",
    "LOCALSTACK_AUTH_TOKEN=${local.secrets["localstack-token"]}",
  ])

  app_env = merge(
    {
      CCOPY_AI                = var.ccopy_ai
      CLAUDE_CODE_USE_BEDROCK = "1"
      CCOPY_QUEUE             = "sqs"
      CCOPY_SQS_URL           = local.queue.jobs_queue_url
      CCOPY_EVIDENCE_BUCKET   = local.storage.evidence_bucket
      CCOPY_DATA_DIR          = "/tmp/ccopy" # the only writable path in Lambda
      CCOPY_PUBLIC_URL        = var.web_base_url
      CCOPY_SECRETS           = local.secret_env
      CCOPY_ENV               = var.env
    },
    var.ccopy_model == "" ? {} : { CCOPY_MODEL = var.ccopy_model, ANTHROPIC_MODEL = var.ccopy_model },
  )
}

# ====================================================================== logs
resource "aws_cloudwatch_log_group" "api" {
  # checkov:skip=CKV_AWS_338:30-day retention is the agreed cost/benefit; evidence lives in S3 for 365d.
  name              = "/ccopy/${var.env}/api"
  retention_in_days = var.log_retention_days
  kms_key_id        = local.sec.kms_key_arn
}

resource "aws_cloudwatch_log_group" "api_access" {
  # checkov:skip=CKV_AWS_338:30-day retention is the agreed cost/benefit.
  name              = "/ccopy/${var.env}/api-access"
  retention_in_days = var.log_retention_days
  kms_key_id        = local.sec.kms_key_arn
}

# ====================================================================== Lambda
resource "aws_security_group" "api" {
  name        = "${local.name}-api"
  description = "API Lambda: HTTPS out (AWS APIs, GitHub, Slack) via fck-nat"
  vpc_id      = local.net.vpc_id

  egress {
    description = "HTTPS"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${local.name}-api" }
}

resource "aws_lambda_function" "api" {
  # checkov:skip=CKV_AWS_116:Invoked synchronously by API Gateway; a DLQ never receives anything.
  # checkov:skip=CKV_AWS_115:New accounts have a concurrency quota of 10; reserving would fail. Throttled at API Gateway.
  # checkov:skip=CKV_AWS_272:Container images are signed with cosign in CI (Lambda code signing covers zip packages only).
  function_name = "${local.name}-api"
  role          = local.sec.api_role_arn
  package_type  = "Image"
  image_uri     = "${local.storage.ecr_repository_urls["api"]}:${var.image_tag}"
  architectures = ["arm64"]
  memory_size   = var.api_memory_mb
  timeout       = 29 # API Gateway's integration limit is 30s
  kms_key_arn   = local.sec.kms_key_arn

  ephemeral_storage {
    size = 1024
  }

  vpc_config {
    subnet_ids         = local.net.private_subnet_ids
    security_group_ids = [aws_security_group.api.id, local.db.db_client_security_group_id]
  }

  environment {
    variables = local.app_env
  }

  tracing_config {
    mode = "Active"
  }

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.api.name
  }

  lifecycle {
    precondition {
      condition     = var.image_tag != ""
      error_message = "Set image_tag (the pushed git sha): -var image_tag=<sha>."
    }
  }
}

# ====================================================================== Cognito
resource "random_id" "cognito_domain" {
  byte_length = 3
}

resource "aws_cognito_user_pool" "main" {
  name                     = local.name
  deletion_protection      = var.cognito_deletion_protection ? "ACTIVE" : "INACTIVE"
  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]
  mfa_configuration        = "OPTIONAL"

  software_token_mfa_configuration {
    enabled = true
  }

  # Invite-only: an admin creates users (aws cognito-idp admin-create-user).
  admin_create_user_config {
    allow_admin_create_user_only = true
  }

  password_policy {
    minimum_length                   = 12
    require_lowercase                = true
    require_uppercase                = true
    require_numbers                  = true
    require_symbols                  = false
    temporary_password_validity_days = 3
  }

  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }
}

resource "aws_cognito_user_pool_domain" "main" {
  domain       = "carbon-copy-${var.env}-${random_id.cognito_domain.hex}"
  user_pool_id = aws_cognito_user_pool.main.id
}

resource "aws_cognito_user_pool_client" "web" {
  name         = "${local.name}-web"
  user_pool_id = aws_cognito_user_pool.main.id

  # Public SPA client: authorization code + PKCE, no secret.
  generate_secret                      = false
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["openid", "email", "profile"]
  supported_identity_providers         = ["COGNITO"]
  callback_urls                        = distinct(["${var.web_base_url}/", "http://localhost:3000/"])
  logout_urls                          = distinct(["${var.web_base_url}/", "http://localhost:3000/"])
  explicit_auth_flows                  = ["ALLOW_REFRESH_TOKEN_AUTH", "ALLOW_USER_SRP_AUTH"]
  prevent_user_existence_errors        = "ENABLED"
  enable_token_revocation              = true

  access_token_validity  = 60
  id_token_validity      = 60
  refresh_token_validity = 30
  token_validity_units {
    access_token  = "minutes"
    id_token      = "minutes"
    refresh_token = "days"
  }
}

# ====================================================================== HTTP API
resource "aws_apigatewayv2_api" "main" {
  name          = local.name
  protocol_type = "HTTP"
  description   = "Carbon Copy API (${var.env})"
}

resource "aws_apigatewayv2_integration" "api" {
  api_id                 = aws_apigatewayv2_api.main.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.api.invoke_arn
  payload_format_version = "2.0"
  timeout_milliseconds   = 30000
}

resource "aws_apigatewayv2_authorizer" "cognito" {
  api_id           = aws_apigatewayv2_api.main.id
  name             = "cognito"
  authorizer_type  = "JWT"
  identity_sources = ["$request.header.Authorization"]
  jwt_configuration {
    issuer   = "https://cognito-idp.${var.region}.amazonaws.com/${aws_cognito_user_pool.main.id}"
    audience = [aws_cognito_user_pool_client.web.id]
  }
}

locals {
  # route key => requires JWT?
  routes = {
    "GET /api/health"        = false # public health check (smoke tests, uptime)
    "ANY /webhooks/{proxy+}" = false # GitHub webhooks: verified by GITHUB_WEBHOOK_SECRET in the app
    "ANY /api/{proxy+}"      = true
    "ANY /mcp"               = true
    "ANY /mcp/{proxy+}"      = true
    "$default"               = true
  }
}

resource "aws_apigatewayv2_route" "route" {
  # checkov:skip=CKV_AWS_309:Health and GitHub webhook routes are public by design (webhooks are HMAC-verified in the app).
  for_each           = local.routes
  api_id             = aws_apigatewayv2_api.main.id
  route_key          = each.key
  target             = "integrations/${aws_apigatewayv2_integration.api.id}"
  authorization_type = each.value ? "JWT" : "NONE"
  authorizer_id      = each.value ? aws_apigatewayv2_authorizer.cognito.id : null
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.main.id
  name        = "$default"
  auto_deploy = true

  default_route_settings {
    throttling_rate_limit  = var.api_throttle_rate
    throttling_burst_limit = var.api_throttle_burst
  }

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.api_access.arn
    format = jsonencode({
      requestId      = "$context.requestId"
      ip             = "$context.identity.sourceIp"
      requestTime    = "$context.requestTime"
      routeKey       = "$context.routeKey"
      status         = "$context.status"
      latency        = "$context.integrationLatency"
      responseLength = "$context.responseLength"
      userSub        = "$context.authorizer.claims.sub"
      error          = "$context.error.message"
      integrationErr = "$context.integrationErrorMessage"
    })
  }
}

resource "aws_lambda_permission" "apigw" {
  statement_id  = "AllowHttpApi"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.api.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.main.execution_arn}/*/*"
}

# ====================================================================== outputs
output "api_endpoint" {
  description = "Direct execute-api URL (CloudFront in 08-web is the public front door)."
  value       = aws_apigatewayv2_api.main.api_endpoint
}

output "api_domain" {
  value = replace(aws_apigatewayv2_api.main.api_endpoint, "https://", "")
}

output "api_id" {
  value = aws_apigatewayv2_api.main.id
}

output "function_name" {
  value = aws_lambda_function.api.function_name
}

output "cognito_user_pool_id" {
  value = aws_cognito_user_pool.main.id
}

output "cognito_client_id" {
  value = aws_cognito_user_pool_client.web.id
}

output "cognito_hosted_ui" {
  value = "https://${aws_cognito_user_pool_domain.main.domain}.auth.${var.region}.amazoncognito.com"
}

output "app_env" {
  description = "Non-secret app environment (07-workers reuses it)."
  value       = local.app_env
}
