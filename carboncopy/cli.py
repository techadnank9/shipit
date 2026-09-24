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
    out = pipeline.run(repo, request, auto_approve=yes)
    if open_report and sys.platform == "darwin":
        subprocess.run(["open", str(out)])


if __name__ == "__main__":
    app()
