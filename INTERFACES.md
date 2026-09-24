# Carbon Copy: interfaces contract (v0.1)

Every builder codes against this file. Change it only by agreement.

## Layout

| Path | Owner | What |
|---|---|---|
| `carboncopy/` core (`reader`, `copier`, `checks`, `browser`, `agent`, `requirements`, `report`, `audit`, `llm`, `pipeline`, `standin/`) | core | engine |
| `carboncopy/server/` | server | FastAPI API, store, job queue, worker |
| `carboncopy/integrations/` | integrations | GitHub App, Slack, MCP, scheduler, `ccopy ci` |
| `web/` | web | dashboard (Next.js, static export) |
| `infra/`, `docker/`, `.github/workflows/`, `packer/` | infra | AWS us-west-1 Terraform, images, CI/CD |

## Settings (environment variables)

| Var | Default | Meaning |
|---|---|---|
| `CCOPY_AI` | `standin` | `standin` = scripted answers for `sample-app` only; `claude` = real Claude |
| `CCOPY_MODEL` | `claude-sonnet-5` | model when `CCOPY_AI=claude` |
| `CLAUDE_CODE_USE_BEDROCK` | unset | `1` on AWS so Claude goes through Bedrock |
| `DATABASE_URL` | `sqlite:///./.ccopy-server/ccopy.db` | server store; Postgres (`postgresql://…`) on AWS |
| `CCOPY_QUEUE` | `local` | `local` = in-process worker thread; `sqs` = AWS SQS (`CCOPY_SQS_URL`) |
| `CCOPY_DATA_DIR` | `./.ccopy-server` | runs, repos, reports on local disk (S3 on AWS: `CCOPY_EVIDENCE_BUCKET`) |
| `CCOPY_PUBLIC_URL` | `http://127.0.0.1:8080` | base URL used in links (Slack, GitHub checks) |
| `CCOPY_API_TOKEN` | unset | if set, API requires `Authorization: Bearer <token>` |
| `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY`, `GITHUB_WEBHOOK_SECRET` | unset | GitHub App |
| `SLACK_WEBHOOK_URL` | unset | Slack incoming webhook |
| `LOCALSTACK_AUTH_TOKEN`, `CCOPY_AWS_IMAGE` | unset / `localstack/localstack:4.14` | fake AWS inside copies |

## Core engine API (Python, owned by core)

```python
from carboncopy.pipeline import ChangeRun, TestRun, RunStatus

# A change request: the full Avcel-style loop, pausing at two human gates.
run = ChangeRun.create(repo: Path, request: str, runs_dir: Path, emit=callable|None) -> ChangeRun
run.prepare()                    # map → copy → requirements; ends at AWAITING_REQUIREMENTS
run.approve_requirements(who)    # records approval
run.reject(who, reason="")       # ends at REJECTED
run.execute()                    # baseline AI test → agent → final gate; ends at AWAITING_SHIP or BLOCKED
run.approve_ship(who)            # ends at SHIPPED, rewrites report
ChangeRun.load(run_dir: Path) -> ChangeRun   # state survives process restarts (state.json)
run.id, run.dir, run.status, run.state -> dict  (see "Run state")

# A test run: TesterArmy-style saved plain-English tests on a fresh copy, no code change.
TestRun.create(repo, tests: list[{"name","instructions"}], db_checks: list[{"description","sql","expect"}], runs_dir, emit=None) -> TestRun
TestRun.execute()                # ends at PASSED or FAILED, writes report.html
TestRun.load(run_dir)
```

`emit(event: str, data: dict)` is called for live progress. Events:
`status` {status}, `log` {message}, `round` {round, passed, checks}, `tool` {tool, target}, `browser` {test, passed}.

### RunStatus (string enum)
`queued, mapping, copying, writing_requirements, awaiting_requirements, baseline_testing, agent_working, final_gate, awaiting_ship, shipped, blocked, rejected, failed, testing, passed`

### Run state (`<run_dir>/state.json`, returned by `run.state`)
```json
{
  "id": "20260924-1530-ab12", "kind": "change|test", "status": "awaiting_requirements",
  "repo": "/abs/path", "request": "…", "created_at": 1727200000.0, "updated_at": 1727200100.0,
  "requirements": {…} | null,
  "fidelity": {"score": 100, "missing_env": [], "unemulated_aws": []} | null,
  "before": {"tests": [...], "db_checks": [...], "passed": false} | null,
  "after":  {…same…} | null,
  "final_checks": [{"name","passed","summary","details"}] | null,
  "rounds": [{"round": 1, "passed": false, "checks": [...]}],
  "agent": {"summary", "cost_usd", "edited_files"} | null,
  "error": null | "message",
  "report": "report.html" | null, "patch": "change.patch" | null
}
```
Browser test results inside `before`/`after`: `tests[i] = {name, instructions, passed, video, steps:[{kind, action, target, value, why, ok, error, ms, screenshot(base64 png)}]}`.
Files in `run_dir`: `state.json`, `audit.jsonl`, `report.html`, `change.patch`, `requirements.json`, `before/`, `after/` (videos).

## HTTP API (owned by server, base `/api`)

All JSON. Errors: `{"detail": "…"}` with 4xx/5xx.

| Method | Path | Body → Response |
|---|---|---|
| GET | `/api/health` | → `{"ok": true, "ai": "standin|claude"}` |
| GET | `/api/projects` | → `[Project]` |
| POST | `/api/projects` | `{"name", "repo_path"?, "git_url"?}` → `Project` (one of repo_path/git_url) |
| GET | `/api/projects/{pid}` | → `Project` |
| GET | `/api/projects/{pid}/map` | → system map JSON |
| GET | `/api/projects/{pid}/tests` | → `[SavedTest]` |
| POST | `/api/projects/{pid}/tests` | `{"name","instructions"}` → `SavedTest` |
| DELETE | `/api/tests/{tid}` | → `{"ok": true}` |
| GET | `/api/projects/{pid}/checks` | → `[DbCheck]` |
| POST | `/api/projects/{pid}/checks` | `{"description","sql","expect"}` → `DbCheck` |
| POST | `/api/projects/{pid}/changes` | `{"request"}` → `Run` (kind change, queued) |
| POST | `/api/projects/{pid}/test-runs` | `{"trigger": "manual|pr|schedule|ci|mcp", "test_ids"?: [..]}` → `Run` (kind test) |
| GET | `/api/runs?project_id=&kind=&status=&limit=` | → `[Run]` newest first |
| GET | `/api/runs/{rid}` | → `Run` including full `state` |
| GET | `/api/runs/{rid}/events` | Server-Sent Events stream of emit events |
| POST | `/api/runs/{rid}/approve-requirements` | `{"who"}` → `Run` (worker continues) |
| POST | `/api/runs/{rid}/reject` | `{"who","reason"}` → `Run` |
| POST | `/api/runs/{rid}/approve-ship` | `{"who"}` → `Run` |
| GET | `/api/runs/{rid}/report` | → text/html proof report |
| GET | `/api/runs/{rid}/patch` | → text/plain diff |
| GET | `/api/runs/{rid}/audit` | → `{"verified": bool, "entries": [...]}` |
| GET | `/api/projects/{pid}/schedule` | → `Schedule` |
| PUT | `/api/projects/{pid}/schedule` | `{"cron": "0 * * * *" | null}` → `Schedule` |

Models:
```
Project   {id, name, repo_path, git_url, created_at, last_run: Run|null}
SavedTest {id, project_id, name, instructions, created_at}
DbCheck   {id, project_id, description, sql, expect, created_at}
Run       {id, project_id, kind: "change"|"test", status, trigger, title, created_at, updated_at, passed: bool|null, state?: {…}}
Schedule  {project_id, cron, next_run_at}
```
Ids are short strings (`p_…`, `t_…`, `c_…`, run ids are the engine run id).

Worker: one job at a time per worker (copies bind fixed host ports 18000/15432/18025). Change runs: `prepare()` → wait for approval → `execute()` → wait → `approve_ship()`. Test runs: `execute()`.

Hooks the server calls (owned by integrations, may be absent → skip):
```python
from carboncopy.integrations import on_run_finished   # (run: dict) -> None; Slack + GitHub check
from carboncopy.integrations.github import router as github_router   # mounted at /webhooks/github
from carboncopy.integrations.mcp_server import router as mcp_router  # mounted at /mcp
from carboncopy.integrations.schedule import start_scheduler        # (store) -> None, background thread
```
Server exposes to integrations: `carboncopy.server.store.Store` with `get_project`, `list_projects`, `create_test_run(project_id, trigger, test_ids=None) -> Run`, `get_run`, `list_saved_tests`, `project_by_git_url(url)`, `set_run_meta(run_id, key, value)`, `get_run_meta(run_id, key)`; and `carboncopy.server.store.get_store() -> Store`.

## Dashboard (owned by web)

Static Next.js export (query-string pages, no dynamic route segments) served by the API server at `/` (files from `web/out`). Pages:
`/` projects · `/project?id=…` overview + tests + checks + schedule + runs · `/run?id=…` live run (SSE), gates with Approve/Reject buttons, before/after, steps with screenshots, report link.
API base = same origin (`/api`). Dev: `NEXT_PUBLIC_API=http://127.0.0.1:8080`.

## Ports (local)
API + dashboard `8080` · copy app `18000` · copy Postgres `15432` · copy Mailpit UI `18025`.
