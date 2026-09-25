# Production. Used by every layer (each reads what it declares).
env          = "prod"
region       = "us-west-1"
state_bucket = "CHANGE_ME-ccopy-tfstate" # same bucket as staging; keys are prefixed by env

# 02-security
github_repo                 = "techadnank9/carboncopy"
create_github_oidc_provider = false # created by staging (account-wide)

# 04-database
db_deletion_protection = true
db_backup_replication  = true

# 06-api / 07-workers
ccopy_ai     = "standin" # switch to "claude" once Bedrock access is confirmed (docs/deploy.md)
ccopy_model  = ""
web_base_url = "http://localhost:3000" # replace with 08-web output web_base_url after first apply
worker_max   = 10
# image_tag and worker_ami_id are passed on the command line / by CI.
cognito_deletion_protection = true

# 08-web
enable_waf          = true
domain_name         = "" # e.g. "app.example.com" (needs acm_certificate_arn in us-east-1)
acm_certificate_arn = ""
route53_zone_id     = ""

# 09-observability
alert_emails           = []   # e.g. ["founder@example.com"]
create_account_budgets = true # $50/$150/$300/$500 actual-spend alerts, credits excluded
