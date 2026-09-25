# Staging: same shape as prod, cheaper knobs. Used by every layer (each reads what it declares).
env          = "staging"
region       = "us-west-1"
state_bucket = "CHANGE_ME-ccopy-tfstate" # e.g. ccopy-tfstate-<account-id>; see docs/deploy.md Phase 0

# 02-security
github_repo                 = "techadnank9/carboncopy"
create_github_oidc_provider = true # the OIDC provider is account-wide: create it once (here)

# 04-database
db_deletion_protection = false
db_backup_replication  = false # staging data is disposable

# 06-api / 07-workers
ccopy_ai     = "standin"
ccopy_model  = ""                      # e.g. the us.anthropic.… inference profile id when ccopy_ai = "claude"
web_base_url = "http://localhost:3000" # replace with 08-web output web_base_url after first apply
worker_max   = 3
# image_tag and worker_ami_id are passed on the command line / by CI.
cognito_deletion_protection = false

# 08-web
enable_waf          = false # ~$9/mo saved; Cognito JWT + API throttling still apply
domain_name         = ""
acm_certificate_arn = ""
route53_zone_id     = ""

# 09-observability
alert_emails           = [] # e.g. ["founder@example.com"]
create_account_budgets = false
