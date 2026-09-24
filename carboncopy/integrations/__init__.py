"""Integrations: GitHub App, Slack, MCP, scheduler and `ccopy ci`.

Kept import-light on purpose (no FastAPI, no server imports at module level) so the
server can import `on_run_finished` cheaply and `ccopy ci` works without the server.
"""
import logging
import os

log = logging.getLogger("carboncopy.integrations")

TERMINAL = {"passed", "failed", "shipped", "blocked", "rejected"}
GATES = {"awaiting_requirements", "awaiting_ship"}
SUCCESS = {"passed", "shipped"}


def public_url() -> str:
    return os.environ.get("CCOPY_PUBLIC_URL", "http://127.0.0.1:8080").rstrip("/")


def run_url(run_id: str) -> str:
    return f"{public_url()}/run?id={run_id}"


def report_url(run_id: str) -> str:
    return f"{public_url()}/api/runs/{run_id}/report"


def _results(state: dict) -> dict | None:
    """Browser/DB results of a run. Change runs: `after` is the final gate; test runs keep
    their single result in `after` (or `before`, whichever the worker filled)."""
    return (state or {}).get("after") or (state or {}).get("before")


def summarize(run: dict) -> dict:
    """Flatten a contract Run dict into what every integration shows: per-test pass/fail and,
    for failures, the first failing step and the error the AI tester saw."""
    state = run.get("state") or {}
    res = _results(state) or {}
    tests = []
    for t in res.get("tests") or []:
        item = {"name": t.get("name", "?"), "passed": bool(t.get("passed"))}
        steps = t.get("steps") or []
        bad = next(((i, s) for i, s in enumerate(steps) if not s.get("ok", True)), None)
        if not item["passed"] and bad:
            i, s = bad
            item["failed_step"] = {
                "number": i + 1, "of": len(steps), "action": s.get("action", ""), "target": s.get("target", ""),
                "value": s.get("value", ""), "why": s.get("why", ""), "error": (s.get("error") or "").strip(),
            }
        tests.append(item)
    db = [{"description": d.get("description", "?"), "passed": bool(d.get("passed")), "expect": d.get("expect"), "got": d.get("got")}
          for d in res.get("db_checks") or []]
    final = [{"name": c.get("name", "?"), "passed": bool(c.get("passed")), "summary": c.get("summary", "")}
             for c in state.get("final_checks") or []]
    status = run.get("status") or state.get("status")
    passed = run.get("passed")
    if passed is None and status in TERMINAL:
        passed = status in SUCCESS
    return {
        "id": run.get("id"), "project_id": run.get("project_id"), "kind": run.get("kind") or state.get("kind"),
        "status": status, "trigger": run.get("trigger"), "title": run.get("title") or state.get("request") or "",
        "passed": passed, "tests": tests, "db_checks": db, "final_checks": final, "error": state.get("error"),
        "url": run_url(run.get("id", "")), "report_url": report_url(run.get("id", "")) if state.get("report") else None,
    }


def step_text(fs: dict) -> str:
    target = f" `{fs['target']}`" if fs.get("target") else ""
    value = f" = \"{fs['value']}\"" if fs.get("value") and fs.get("action") in ("fill", "expect_text", "expect_no_text") else ""
    return f"step {fs['number']}/{fs['of']}: {fs['action']}{target}{value}"


def headline(s: dict) -> str:
    n, ok = len(s["tests"]) + len(s["db_checks"]), sum(x["passed"] for x in s["tests"] + s["db_checks"])
    if s["error"] and not n:
        return "Run errored"
    if not n:
        return "Passed" if s["passed"] else "Failed"
    if ok == n:
        return "1 check passed" if n == 1 else f"All {n} checks passed"
    return f"{n - ok} of {n} checks failed"


def on_run_finished(run: dict) -> None:
    """Server hook, called when a run finishes or pauses at a human gate. Never raises."""
    try:
        from . import slack
        slack.notify(run)
    except Exception:  # noqa: BLE001
        log.exception("slack notify failed for run %s", run.get("id") if isinstance(run, dict) else run)
    try:
        if (run.get("status") or (run.get("state") or {}).get("status")) in TERMINAL:
            from . import github
            github.complete_check(run)
    except Exception:  # noqa: BLE001
        log.exception("github complete_check failed for run %s", run.get("id") if isinstance(run, dict) else run)
