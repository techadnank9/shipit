# Carbon Copy

Test every AI change on a working copy of production before it ships.

```
ccopy run "Make refunds safe to retry and cap them at $500" --repo sample-app
```

1. **Read**: tree-sitter + Terraform parser build a system map (`.ccopy/map.json`)
2. **Copy**: app + Postgres + LocalStack (AWS) + Mailpit via Docker Compose
3. **Requirements**: Claude writes acceptance criteria, AI browser tests and DB checks → human gate
4. **Baseline**: the AI browser tester runs on the original code (shows the bug)
5. **Agent**: Claude edits code with no shell; it can only run checks on the copy
6. **Gate**: tests on the copy, OPA policy, Checkov, secret scan, AI browser tests + DB checks → human gate
7. **Proof**: `report.html`, `change.patch` and a hash-chained `audit.jsonl` per run

## Setup (Mac)

```
brew install colima docker docker-compose opa hashicorp/tap/terraform awscli
colima start --cpu 4 --memory 8
uv sync && uv run playwright install chromium-headless-shell
uv tool install checkov
```

Claude access, one of:
- `claude auth login` (local development)
- AWS Bedrock: `export CLAUDE_CODE_USE_BEDROCK=1 AWS_REGION=us-west-1` with AWS credentials
- `export ANTHROPIC_API_KEY=...`

## Other commands

`ccopy map <repo>` · `ccopy up <repo>` / `ccopy down <repo>` · `ccopy check <repo>`

LocalStack: the default image is `localstack/localstack:4.14` (last one without an account).
Commercial use needs a LocalStack license: set `LOCALSTACK_AUTH_TOKEN` and `CCOPY_AWS_IMAGE`.
