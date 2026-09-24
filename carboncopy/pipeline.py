"""Orchestrator: the six steps, in order, with two human gates and a hash-chained audit trail."""
import getpass
import json
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import agent, browser, checks, report, requirements
from .audit import AuditLog
from .copier import Copy
from .reader import scan

con = Console()
IGNORE = shutil.ignore_patterns(".ccopy", ".git", ".venv", "__pycache__", "node_modules", ".pytest_cache")


def _gate(question: str, auto: bool) -> bool:
    if auto:
        con.print(f"[yellow]GATE[/] {question} [dim](auto-approved with --yes)[/]")
        return True
    return con.input(f"[bold yellow]GATE[/] {question} [y/N] ").strip().lower() in ("y", "yes")


def _sources(ws: Path, system_map: dict) -> dict[str, str]:
    files = [c["file"] for c in system_map["code"]] + [r["file"] for r in system_map["infrastructure"]] + system_map["schema_files"]
    return {f: (ws / f).read_text() for f in dict.fromkeys(files) if (ws / f).exists()}


def _browser_table(title: str, res: dict) -> Table:
    t = Table(title=title, show_lines=False)
    t.add_column("Check"); t.add_column("Result")
    for x in res["tests"]:
        failed = next((s for s in x["steps"] if not s["ok"]), None)
        t.add_row(x["name"], "[green]PASS" if x["passed"] else f"[red]FAIL[/] {failed['action']} {failed['target']}: {failed['error'][:80] if failed else ''}")
    for d in res["db_checks"]:
        t.add_row(f"DB: {d['description']}", "[green]PASS" if d["passed"] else f"[red]FAIL[/] expected {d['expect']}, got {d['got']}")
    return t


def run(repo: Path, request: str, auto_approve: bool = False) -> Path:
    t0 = time.time()
    repo = repo.resolve()
    run_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
    run_dir = repo / ".ccopy" / "runs" / run_id
    ws = run_dir / "workspace"
    shutil.copytree(repo, ws, ignore=IGNORE)
    subprocess.run(["git", "init", "-q"], cwd=ws, check=True)
    subprocess.run(["git", "add", "-A"], cwd=ws, check=True)
    subprocess.run(["git", "-c", "user.email=ccopy@local", "-c", "user.name=ccopy", "commit", "-qm", "baseline"], cwd=ws, check=True)
    audit = AuditLog(run_dir / "audit.jsonl")
    who = getpass.getuser()
    audit.record("run.started", who=who, request=request, repo=str(repo))

    con.print(Panel(f"[bold]{request}[/]\nrepo: {repo.name} · run {run_id}", title="Carbon Copy"))

    con.rule("1 · Read the system")
    system_map = scan(ws)
    con.print(f"{len(system_map['routes'])} routes · tables {system_map['tables']} · AWS {system_map['aws_services']} · {len(system_map['infrastructure'])} Terraform resources")
    audit.record("system.mapped", who="reader", routes=len(system_map["routes"]), tables=system_map["tables"])

    con.rule("2 · Build the carbon copy")
    copy = Copy(ws, system_map, f"ccopy-{run_id.lower()}")
    try:
        copy.up()
        fid = copy.fidelity()
        con.print(f"[green]copy running[/] at {copy.app_url} · fidelity {fid['score']}%")
        audit.record("copy.up", who="copier", fidelity=fid)

        con.rule("3 · Write requirements")
        pol = checks.policy(ws)
        reqs = requirements.write(request, system_map, pol.details, _sources(ws, system_map))
        (run_dir / "requirements.json").write_text(json.dumps(reqs, indent=2))
        con.print(Panel("\n".join(f"• {a}" for a in reqs["acceptance_criteria"]), title=f"{reqs['title']} · risk {reqs['risk']}"))
        for b in reqs["browser_tests"]:
            con.print(f"  [cyan]AI browser test[/] {b['name']}: {b['instructions']}")
        for d in reqs["db_invariants"]:
            con.print(f"  [cyan]DB check[/] {d['description']} → expect {d['expect']}")
        audit.record("requirements.written", who="claude", title=reqs["title"], risk=reqs["risk"])
        if not _gate("Approve these requirements?", auto_approve):
            audit.record("requirements.rejected", who=who)
            raise SystemExit("Stopped at requirements gate.")
        audit.record("requirements.approved", who=who)

        con.rule("4 · AI tester on the ORIGINAL code (baseline)")
        before = browser.run(copy.app_url, reqs["browser_tests"], reqs["db_invariants"], run_dir / "before")
        con.print(_browser_table("Before the change", before))
        audit.record("baseline.tested", who="ai-tester", passed=before["passed"])

        con.rule("5 · Coding agent in the sandbox")
        def on_event(kind, data):
            if kind == "tool":
                con.print(f"  [dim]agent →[/] {data['tool']} {data['target']}")
            else:
                marks = " ".join(("[green]✔" if c["passed"] else "[red]✖") + f"[/] {c['name']}" for c in data["checks"])
                con.print(f"  [bold]round {data['round']}[/] {marks}")
        result = agent.run(ws, copy, reqs, on_event=on_event)
        audit.record("agent.finished", who="claude-agent", rounds=len(result["rounds"]), cost_usd=result["cost_usd"], files=result["edited_files"])

        con.rule("6 · Final gate on a fresh copy")
        copy.rebuild_app()
        final = checks.run_all(copy, ws)
        after = browser.run(copy.app_url, reqs["browser_tests"], reqs["db_invariants"], run_dir / "after")
        con.print(_browser_table("After the change", after))
        for c in final:
            con.print(("[green]✔[/] " if c.passed else "[red]✖[/] ") + f"{c.name}: {c.summary}")
        all_passed = all(c.passed for c in final) and after["passed"]
        audit.record("gate.evaluated", who="carboncopy", passed=all_passed, checks={c.name: c.passed for c in final}, browser=after["passed"])

        diff = subprocess.run(["git", "diff", "--", ".", ":(exclude).ccopy"], cwd=ws, capture_output=True, text=True).stdout
        (run_dir / "change.patch").write_text(diff)
        shipped = False
        if all_passed and _gate("All gates passed. Approve this change to ship?", auto_approve):
            shipped = True
            audit.record("change.approved", who=who, patch_sha=__import__("hashlib").sha256(diff.encode()).hexdigest())
        elif not all_passed:
            audit.record("change.blocked", who="carboncopy")

        entries = audit.entries()
        for e in entries:
            e["who"] = e["data"].get("who", "")
        out = report.write(
            run_dir / "report.html", run_id=run_id, when=time.strftime("%Y-%m-%d %H:%M %Z"), request=request, reqs=reqs,
            shipped=shipped, before=before, after=after, final_checks=checks.as_dicts(final), rounds=result["rounds"],
            agent=result, diff=diff, fidelity=fid, cost=result["cost_usd"], duration=f"{int(time.time() - t0)}s",
            audit=entries, audit_ok=audit.verify(),
        )
        con.print(Panel(f"{'[green]READY TO SHIP' if shipped else '[red]BLOCKED'}[/]\nreport: {out}\npatch:  {run_dir / 'change.patch'}", title="Result"))
        return out
    finally:
        copy.down()
