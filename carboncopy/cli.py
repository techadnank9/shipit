"""ccopy: test every AI change on a working copy of production before it ships."""
import json
import subprocess
import sys
from pathlib import Path

import typer
from rich.console import Console

from . import checks, pipeline
from .copier import Copy
from .reader import scan

app = typer.Typer(no_args_is_help=True, add_completion=False)
con = Console()


@app.command()
def map(repo: Path = typer.Argument(Path("."))):
    """Read a repo and print its system map."""
    m = scan(repo)
    con.print_json(json.dumps({k: m[k] for k in ("routes", "tables", "env_vars", "aws_services")}))


@app.command()
def up(repo: Path = typer.Argument(Path("."))):
    """Start a carbon copy of the repo and leave it running."""
    c = Copy(repo, scan(repo), "ccopy-dev")
    c.up()
    con.print(f"[green]copy running[/] app {c.app_url} · mail http://127.0.0.1:18025 · stop with: ccopy down {repo}")


@app.command()
def down(repo: Path = typer.Argument(Path("."))):
    """Stop the carbon copy started with `ccopy up`."""
    Copy(repo, scan(repo), "ccopy-dev").down()


@app.command()
def check(repo: Path = typer.Argument(Path("."))):
    """Run the policy and secret gates without changing anything."""
    for c in (checks.policy(repo.resolve()), checks.checkov(repo.resolve()), checks.secrets(repo.resolve())):
        con.print(("[green]✔[/] " if c.passed else "[red]✖[/] ") + f"{c.name}: {c.summary}")
        for d in c.details[:8]:
            con.print(f"    {d}")


@app.command()
def run(request: str, repo: Path = typer.Option(Path("."), "--repo", "-r"), yes: bool = typer.Option(False, "--yes", "-y", help="auto-approve both gates"),
        open_report: bool = typer.Option(True, "--open/--no-open")):
    """Full loop: map → copy → requirements → AI test → agent → gates → proof report."""
    def emit(event, data):
        if event == "status":
            con.rule(data["status"].replace("_", " "))
        elif event == "log":
            con.print(f"  {data['message']}")
        elif event == "tool":
            con.print(f"  [dim]agent →[/] {data['tool']} {data['target']}")
        elif event == "round":
            con.print(f"  [bold]round {data['round']}[/] " + " ".join(("[green]✔" if c["passed"] else "[red]✖") + f"[/] {c['name']}" for c in data["checks"]))
        elif event == "browser":
            con.print(f"  AI test {'[green]PASS' if data['passed'] else '[red]FAIL'}[/] {data['test']}")

    def gate(question: str) -> bool:
        return yes or con.input(f"[bold yellow]GATE[/] {question} [y/N] ").strip().lower() in ("y", "yes")

    r = pipeline.ChangeRun.create(repo, request, repo.resolve() / ".ccopy" / "runs", emit)
    r.prepare()
    if r.status == pipeline.RunStatus.AWAITING_REQUIREMENTS:
        reqs = r.state["requirements"]
        con.print(f"[bold]{reqs['title']}[/] · risk {reqs['risk']}")
        for a in reqs["acceptance_criteria"]:
            con.print(f"  • {a}")
        for b in reqs["browser_tests"]:
            con.print(f"  [cyan]AI test[/] {b['name']}: {b['instructions']}")
        for d in reqs["db_invariants"]:
            con.print(f"  [cyan]DB check[/] {d['description']} → {d['expect']}")
        if gate("Approve these requirements?"):
            r.approve_requirements("cli")
            r.execute()
        else:
            r.reject("cli")
    if r.status == pipeline.RunStatus.AWAITING_SHIP and gate("All gates passed. Approve this change to ship?"):
        r.approve_ship("cli")
    color = {"shipped": "green", "awaiting_ship": "yellow"}.get(r.status, "red")
    con.print(f"[bold {color}]{r.status.upper()}[/] · report {r.dir / 'report.html'}")
    if r.state.get("error"):
        con.print(f"[red]{r.state['error']}[/]  (details: {r.dir / 'error.log'})")
    if open_report and sys.platform == "darwin" and (r.dir / "report.html").exists():
        subprocess.run(["open", str(r.dir / "report.html")])


@app.command()
def ci(api: str = typer.Option(..., "--api", help="Carbon Copy server, e.g. https://ccopy.example.com"),
       project: str = typer.Option(..., "--project", help="project id (p_…)"),
       token: str = typer.Option(None, "--token", envvar="CCOPY_API_TOKEN"),
       output: Path = typer.Option(Path("result.json"), "--output"),
       timeout: int = typer.Option(1800, "--timeout", help="seconds to wait for the run")):
    """Run the project's saved tests on a fresh copy (for CI); exit 0 only if they pass."""
    from .integrations.ci import run_ci
    raise typer.Exit(run_ci(api, project, token, output, timeout))


@app.command()
def serve(host: str = typer.Option("127.0.0.1", "--host"), port: int = typer.Option(8080, "--port"), reload: bool = typer.Option(False, "--reload")):
    """Run the API server and dashboard."""
    import uvicorn

    uvicorn.run("carboncopy.server.app:app", host=host, port=port, reload=reload)


if __name__ == "__main__":
    app()
