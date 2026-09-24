"""`ccopy ci`: start a test run from CI, wait for it, print results, exit 0 only if it passed.

Also runnable without the rest of the CLI: `python -m carboncopy.integrations.ci --api URL --project PID`.
Writes a JSON result file and, inside GitHub Actions, a job summary ($GITHUB_STEP_SUMMARY).
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

import httpx

from . import TERMINAL, headline, step_text, summarize


def _p(msg: str = "") -> None:
    print(msg, flush=True)


def run_ci(api: str, project: str, token: str | None = None, output: Path | None = Path("result.json"),
           timeout: float = 1800, interval: float = 5, test_ids: list[str] | None = None) -> int:
    api = api.rstrip("/")
    if api.endswith("/api"):
        api = api[:-4]
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    result: dict = {"passed": False, "project_id": project}
    code = 1
    with httpx.Client(base_url=api, headers=headers, timeout=30) as c:
        try:
            body = {"trigger": "ci"} | ({"test_ids": test_ids} if test_ids else {})
            r = c.post(f"/api/projects/{project}/test-runs", json=body)
            r.raise_for_status()
            run = r.json()
            rid = run["id"]
            _p(f"Carbon Copy: started test run {rid} for project {project}")
            _p(f"  {api}/run?id={rid}")
            deadline, last = time.time() + timeout, None
            while run.get("status") not in TERMINAL:
                if time.time() > deadline:
                    raise TimeoutError(f"run {rid} still {run.get('status')} after {int(timeout)}s")
                time.sleep(interval)
                try:
                    r = c.get(f"/api/runs/{rid}")
                    r.raise_for_status()
                    run = r.json()
                except httpx.HTTPError as e:  # transient: keep polling until the deadline
                    _p(f"  poll error: {e}")
                    continue
                if run.get("status") != last:
                    last = run.get("status")
                    _p(f"  status: {last}")
            s = summarize(run)
            # links should point at the API the CI talked to, not the server's configured public URL
            s["url"] = f"{api}/run?id={rid}"
            if s["report_url"]:
                s["report_url"] = f"{api}/api/runs/{rid}/report"
            result = s | {"passed": bool(s["passed"])}
            _print(s)
            code = 0 if result["passed"] else 1
        except (httpx.HTTPError, TimeoutError, KeyError, ValueError) as e:
            msg = str(e)
            if isinstance(e, httpx.HTTPStatusError):
                msg = f"{e.request.method} {e.request.url} -> {e.response.status_code}: {e.response.text[:300]}"
            _p(f"Carbon Copy: error: {msg}")
            result["error"] = msg
    if output:
        Path(output).write_text(json.dumps(result, indent=2))
        _p(f"Result written to {output}")
    _step_summary(result)
    return code


def _print(s: dict) -> None:
    _p("")
    for t in s["tests"]:
        _p(f"  {'PASS' if t['passed'] else 'FAIL'}  {t['name']}")
        if fs := t.get("failed_step"):
            _p(f"        failed at {step_text(fs)}")
            if fs["error"]:
                _p(f"        saw: {fs['error'][:400]}")
    for d in s["db_checks"]:
        _p(f"  {'PASS' if d['passed'] else 'FAIL'}  DB: {d['description']}" + ("" if d["passed"] else f" (expected {d['expect']}, got {d['got']})"))
    if s["error"]:
        _p(f"  error: {s['error']}")
    _p("")
    _p(f"Carbon Copy: {'PASSED' if s['passed'] else 'FAILED'} · {headline(s)} · {s['url']}")


def _step_summary(result: dict) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    lines = [f"### Carbon Copy: {'✅ passed' if result.get('passed') else '❌ failed'}", ""]
    for t in result.get("tests", []):
        lines.append(f"- {'✅' if t['passed'] else '❌'} {t['name']}" + (f" (failed at {step_text(t['failed_step'])}: `{t['failed_step']['error'][:200]}`)" if t.get("failed_step") else ""))
    for d in result.get("db_checks", []):
        lines.append(f"- {'✅' if d['passed'] else '❌'} DB: {d['description']}")
    if result.get("error"):
        lines.append(f"\nError: `{result['error']}`")
    if result.get("url"):
        lines.append(f"\n[Open the run]({result['url']})")
    try:
        with open(path, "a") as f:
            f.write("\n".join(lines) + "\n")
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ccopy ci", description=__doc__)
    ap.add_argument("--api", required=True)
    ap.add_argument("--project", required=True)
    ap.add_argument("--token", default=os.environ.get("CCOPY_API_TOKEN"))
    ap.add_argument("--output", default="result.json")
    ap.add_argument("--timeout", type=float, default=1800)
    ap.add_argument("--interval", type=float, default=5)
    a = ap.parse_args(argv)
    return run_ci(a.api, a.project, a.token, Path(a.output) if a.output else None, a.timeout, a.interval)


if __name__ == "__main__":
    sys.exit(main())
