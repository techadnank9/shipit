# Integrations

Carbon Copy runs your saved plain-English tests on a fresh copy of production when something
asks it to: a pull request, CI after a deploy, a schedule, or a coding agent over MCP. Failures
go to Slack and back onto the PR as a check.

All of this is configured with environment variables on the server. Anything left unset is
skipped with a log line.

## GitHub App (pull request checks)

1. GitHub → Settings → Developer settings → GitHub Apps → **New GitHub App**.
   - Webhook URL: `https://<your-server>/webhooks/github`
   - Webhook secret: a random string (also set it as `GITHUB_WEBHOOK_SECRET`)
   - Repository permissions: **Checks: Read & write**, **Contents: Read**, **Pull requests: Read**, **Metadata: Read**
   - Subscribe to events: **Pull request**
2. Generate a private key, then install the app on your repositories.
3. Server environment:
   ```
   GITHUB_APP_ID=123456
   GITHUB_APP_PRIVATE_KEY=/secrets/carboncopy.pem   # or the PEM text itself
   GITHUB_WEBHOOK_SECRET=...
   CCOPY_PUBLIC_URL=https://ccopy.example.com         # used in check and Slack links
   ```
4. Create the project with the repository's `git_url` (for example `https://github.com/acme/shop`).
   The match ignores case and a trailing `.git`.

When a PR is opened, reopened or pushed to, Carbon Copy creates a **Carbon Copy** check on the head
commit and starts a test run. The check completes as success or failure. For each failed test it
names the step that failed and what the AI tester saw, with a link to the run.

## Slack

Create an app at api.slack.com/apps, turn on **Incoming Webhooks**, add one to a channel, then set:

```
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
SLACK_NOTIFY_SUCCESS=1   # optional: also post passed runs
```

Carbon Copy posts:
- failed or blocked runs: the failed test, the step, the error and a link
- runs waiting at a human gate (requirements or ship), with a review link

## Coding agents (MCP)

The server exposes MCP over Streamable HTTP at `/mcp`.

Claude Code:
```
claude mcp add --transport http carboncopy https://ccopy.example.com/mcp
# if CCOPY_API_TOKEN is set on the server:
claude mcp add --transport http carboncopy https://ccopy.example.com/mcp --header "Authorization: Bearer $CCOPY_API_TOKEN"
```

Cursor (`.cursor/mcp.json`) and other clients:
```json
{ "mcpServers": { "carboncopy": { "url": "https://ccopy.example.com/mcp" } } }
```

Tools: `list_projects`, `list_tests`, `create_test`, `run_tests`, `get_run`, `request_change`, `approve`.
Agents start a run, poll `get_run`, and read the failed steps. `approve` passes a human gate. Leave
it to people unless you tell your agent to use it. Every approval is written to the run's audit trail.

## Scheduled runs

Set a cron expression (UTC) on a project in the dashboard or with
`PUT /api/projects/{id}/schedule {"cron": "0 * * * *"}`. The server checks every 30 seconds. If the
server was down through several slots, it starts one run, not one run per missed slot.

## CI (GitHub Actions)

`ccopy ci` starts a test run, waits for it, prints each result, writes `result.json` and exits
non-zero on failure. Run it after your deploy:

```yaml
name: carbon-copy
on:
  workflow_run:
    workflows: [deploy]
    types: [completed]
jobs:
  test:
    if: ${{ github.event.workflow_run.conclusion == 'success' }}
    runs-on: ubuntu-latest
    steps:
      - uses: astral-sh/setup-uv@v6
      - name: Carbon Copy tests
        run: >
          uvx --from git+https://github.com/<org>/carboncopy ccopy ci
          --api https://ccopy.example.com --project p_abc123
          --output result.json --timeout 1800
        env:
          CCOPY_API_TOKEN: ${{ secrets.CCOPY_API_TOKEN }}
      - if: always()
        uses: actions/upload-artifact@v4
        with: { name: carbon-copy-result, path: result.json }
```

The token can also be passed as `--token`. Inside GitHub Actions the results are added to the job
summary as well. To skip the rest of the CLI, run `python -m carboncopy.integrations.ci`, which
takes the same flags.
