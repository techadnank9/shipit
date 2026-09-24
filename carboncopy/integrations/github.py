"""GitHub App: pull request webhooks start a test run and a "Carbon Copy" check run;
`complete_check` reports the verdict back on the PR when the run ends.

Env: GITHUB_APP_ID, GITHUB_APP_PRIVATE_KEY (PEM text, PEM with literal \\n, or a file path),
GITHUB_WEBHOOK_SECRET, optional GITHUB_API_URL (GitHub Enterprise; default https://api.github.com).
"""
import hashlib
import hmac
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from . import headline, run_url, step_text, summarize

log = logging.getLogger("carboncopy.integrations.github")
router = APIRouter()

CHECK_NAME = "Carbon Copy"
PR_ACTIONS = {"opened", "synchronize", "reopened"}
_tokens: dict[int, tuple[str, float]] = {}
_lock = threading.Lock()


def _api() -> str:
    return os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")


def _store():
    from carboncopy.server.store import get_store
    return get_store()


# ---------- signature ----------

def verify_signature(body: bytes, header: str | None, secret: str | None) -> bool:
    """True when no secret is configured, else the X-Hub-Signature-256 HMAC must match."""
    if not secret:
        return True
    if not header or not header.startswith("sha256="):
        return False
    want = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, header.strip())


# ---------- app auth ----------

def configured() -> bool:
    return bool(os.environ.get("GITHUB_APP_ID") and os.environ.get("GITHUB_APP_PRIVATE_KEY"))


def _private_key() -> str:
    raw = os.environ["GITHUB_APP_PRIVATE_KEY"].strip()
    if "-----BEGIN" not in raw:
        return Path(raw).expanduser().read_text()
    return raw.replace("\\n", "\n")


def app_jwt() -> str:
    import jwt
    now = int(time.time())
    return jwt.encode({"iat": now - 60, "exp": now + 540, "iss": str(os.environ["GITHUB_APP_ID"])}, _private_key(), algorithm="RS256")


def _headers(token: str, scheme: str = "token") -> dict:
    return {"Authorization": f"{'Bearer' if scheme == 'bearer' else 'token'} {token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "carboncopy"}


def installation_token(installation_id: int) -> str:
    """Installation access token, cached until 60s before GitHub's expiry."""
    with _lock:
        hit = _tokens.get(installation_id)
        if hit and hit[1] - 60 > time.time():
            return hit[0]
        r = httpx.post(f"{_api()}/app/installations/{installation_id}/access_tokens", headers=_headers(app_jwt(), "bearer"), timeout=20)
        r.raise_for_status()
        data = r.json()
        try:
            exp = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00")).timestamp()
        except (KeyError, ValueError):
            exp = time.time() + 3000
        _tokens[installation_id] = (data["token"], exp)
        return data["token"]


def _gh(method: str, path: str, installation_id: int, body: dict) -> dict:
    r = httpx.request(method, f"{_api()}{path}", headers=_headers(installation_token(installation_id)), json=body, timeout=20)
    if r.status_code >= 400:
        raise RuntimeError(f"GitHub {method} {path} -> {r.status_code}: {r.text[:300]}")
    return r.json() if r.content else {}


# ---------- webhook ----------

def url_variants(*urls: str | None) -> list[str]:
    """Candidate spellings for store.project_by_git_url: as given, lowercased, without .git,
    with .git, and the https form of git@host:owner/repo."""
    out: list[str] = []
    for u in urls:
        if not u:
            continue
        u = u.strip().rstrip("/")
        if u.startswith("git@") and ":" in u:
            host, path = u[4:].split(":", 1)
            u = f"https://{host}/{path}"
        base = u[:-4] if u.lower().endswith(".git") else u
        for v in (u, base, base + ".git", base.lower(), base.lower() + ".git"):
            if v not in out:
                out.append(v)
    return out


def find_project(store, repo: dict) -> dict | None:
    for u in url_variants(repo.get("clone_url"), repo.get("html_url"), repo.get("ssh_url"), repo.get("git_url")):
        p = store.project_by_git_url(u)
        if p:
            return p
    return None


def handle_pull_request(payload: dict) -> tuple[int, dict]:
    action = payload.get("action")
    if action not in PR_ACTIONS:
        return 202, {"ignored": f"pull_request.{action}"}
    repo = payload.get("repository") or {}
    pr = payload.get("pull_request") or {}
    head_sha = (pr.get("head") or {}).get("sha")
    installation_id = (payload.get("installation") or {}).get("id")
    full_name = repo.get("full_name", "")
    store = _store()
    project = find_project(store, repo)
    if not project:
        log.info("github: no project for %s, ignoring PR #%s", full_name, pr.get("number"))
        return 202, {"ignored": f"no project for {full_name}"}

    check_run_id = None
    if configured() and installation_id and head_sha:
        try:
            check = _gh("POST", f"/repos/{full_name}/check-runs", installation_id, {
                "name": CHECK_NAME, "head_sha": head_sha, "status": "in_progress",
                "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
                "output": {"title": "Testing on a carbon copy", "summary": "Running your saved plain-English tests on a fresh copy of production."},
            })
            check_run_id = check.get("id")
        except Exception:  # noqa: BLE001 — the test run is still useful without the check
            log.exception("github: creating check run failed for %s@%s", full_name, head_sha)
    else:
        log.info("github: app not configured (GITHUB_APP_ID/GITHUB_APP_PRIVATE_KEY); running tests without a check run")

    run = store.create_test_run(project["id"], "pr")
    meta = {"installation_id": installation_id, "repo_full_name": full_name, "head_sha": head_sha,
            "check_run_id": check_run_id, "pr_number": pr.get("number")}
    store.set_run_meta(run["id"], "github", meta)
    if check_run_id:
        try:
            _gh("PATCH", f"/repos/{full_name}/check-runs/{check_run_id}", installation_id,
                {"external_id": run["id"], "details_url": run_url(run["id"])})
        except Exception:  # noqa: BLE001
            log.warning("github: could not set details_url on check %s", check_run_id)
    log.info("github: PR #%s on %s -> run %s (check %s)", pr.get("number"), full_name, run["id"], check_run_id)
    return 200, {"ok": True, "run_id": run["id"], "check_run_id": check_run_id}


@router.post("")
@router.post("/", include_in_schema=False)
async def webhook(request: Request):
    body = await request.body()
    if not verify_signature(body, request.headers.get("X-Hub-Signature-256"), os.environ.get("GITHUB_WEBHOOK_SECRET")):
        return JSONResponse({"detail": "bad signature"}, status_code=401)
    event = request.headers.get("X-GitHub-Event", "")
    try:
        payload = json.loads(body or b"{}")
    except ValueError:
        return JSONResponse({"detail": "invalid JSON"}, status_code=400)
    if event == "ping":
        return {"ok": True, "zen": payload.get("zen"), "hook_id": payload.get("hook_id")}
    if event != "pull_request":
        return JSONResponse({"ignored": event or "unknown"}, status_code=202)
    code, out = await run_in_threadpool(handle_pull_request, payload)
    return JSONResponse(out, status_code=code)


# ---------- completion ----------

def check_output(run: dict) -> tuple[str, dict]:
    """(conclusion, output) for the check run PATCH."""
    s = summarize(run)
    conclusion = "success" if s["passed"] else "failure"
    title = headline(s)
    lines = [f"**{title}** on a carbon copy of production.", ""]
    for t in s["tests"]:
        if t["passed"]:
            lines.append(f"- ✅ {t['name']}")
        else:
            lines.append(f"- ❌ **{t['name']}**")
            if fs := t.get("failed_step"):
                lines.append(f"  - Failed at {step_text(fs)}")
                if fs["error"]:
                    lines.append(f"  - What the AI tester saw: `{fs['error'][:500]}`")
    for d in s["db_checks"]:
        lines.append(f"- {'✅' if d['passed'] else '❌'} DB: {d['description']}" + ("" if d["passed"] else f" (expected {d['expect']}, got {d['got']})"))
    if s["error"]:
        lines += ["", f"Error: `{str(s['error'])[:1000]}`"]
    lines += ["", f"[Open the run]({s['url']})" + (f" · [proof report]({s['report_url']})" if s["report_url"] else "")]
    return conclusion, {"title": title, "summary": "\n".join(lines)[:65000]}


def complete_check(run: dict) -> None:
    meta = None
    try:
        meta = _store().get_run_meta(run["id"], "github")
    except Exception:  # noqa: BLE001
        log.exception("github: reading run meta failed for %s", run.get("id"))
    if not meta or not meta.get("check_run_id"):
        return
    if not configured():
        log.info("github: app not configured; not completing check %s", meta.get("check_run_id"))
        return
    conclusion, output = check_output(run)
    _gh("PATCH", f"/repos/{meta['repo_full_name']}/check-runs/{meta['check_run_id']}", meta["installation_id"], {
        "status": "completed", "conclusion": conclusion, "details_url": run_url(run["id"]), "external_id": run["id"],
        "completed_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"), "output": output,
    })
    log.info("github: check %s on %s -> %s", meta["check_run_id"], meta["repo_full_name"], conclusion)
