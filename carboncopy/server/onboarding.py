"""Project onboarding: draft a copy profile, prove it boots, and hold it for a person to approve.

Claude drafts the profile from the repo, Carbon Copy boots a copy from it, and when a step fails
Claude revises the profile from the error (up to MAX_ATTEMPTS boots). The result, with its boot
log and the pages that answered, waits for approval. Runs use the approved profile.
"""
from __future__ import annotations

import logging
import shutil
import time
import traceback
from typing import Any

import httpx

from carboncopy import profile as profiles
from carboncopy.copier import Copy
from carboncopy.pipeline import snapshot
from carboncopy.reader import scan

from . import config
from .repos import prepare_repo
from .store import Store

log = logging.getLogger("ccopy.onboarding")
MAX_ATTEMPTS = 4


def state(store: Store, pid: str) -> dict[str, Any]:
    return {
        "onboarding": store.get_project_meta(pid, "onboarding") or {"status": "not_started", "log": [], "attempts": []},
        "draft": store.get_project_meta(pid, "profile_draft"),
        "profile": store.get_project_meta(pid, "profile"),
        "approved_by": store.get_project_meta(pid, "profile_approved_by"),
    }


def _pages(copy: Copy) -> list[dict]:
    """Every page route without parameters, fetched once: what a person will see in the copy."""
    out = []
    for r in copy.map["routes"]:
        if r["method"] in ("PAGE", "GET") and ":" not in r["path"] and not r["path"].startswith("/api/"):
            try:
                code = httpx.get(copy.app_url + r["path"], timeout=120, follow_redirects=False).status_code
            except httpx.HTTPError as e:
                code = f"error: {type(e).__name__}"
            out.append({"path": r["path"], "status": code})
    return sorted({p["path"]: p for p in out}.values(), key=lambda p: p["path"])


def run(store: Store, pid: str) -> None:
    ob: dict[str, Any] = {"status": "drafting", "log": [], "attempts": [], "started_at": time.time()}

    def say(msg: str, **extra: Any) -> None:
        ob["log"].append({"t": time.time(), "message": msg, **extra})
        store.set_project_meta(pid, "onboarding", ob)

    project = store.get_project(pid)
    ws = config.data_dir() / "onboarding" / pid / "workspace"
    copy: Copy | None = None
    try:
        repo = prepare_repo(project, fresh=True)
        shutil.rmtree(ws.parent, ignore_errors=True)
        ws.mkdir(parents=True)
        snapshot(repo, ws)
        system_map = scan(ws)
        say(f"read the system: {len(system_map['routes'])} routes, {len(system_map['tables'])} tables, "
            f"{len(system_map['schema_files'])} SQL files, {len(system_map['env_vars'])} environment variables")
        prof = profiles.validate(profiles.draft(ws, system_map))
        say("drafted a boot recipe", profile=prof)
        for attempt in range(1, MAX_ATTEMPTS + 1):
            ob["status"] = "booting"
            say(f"boot attempt {attempt}")
            copy = Copy(ws, system_map, f"ccopy-onboard-{pid.lower()}", profile=prof)
            try:
                copy.up(on_step=lambda s: say(f"setup: {s['kind']} {s['value']}"))
            except Exception as e:  # noqa: BLE001 - a failed boot is data for the next draft
                err = str(e)
                ob["attempts"].append({"attempt": attempt, "ok": False, "error": err[-3000:]})
                say(f"boot attempt {attempt} failed", error=err[-1500:])
                copy.down()
                if attempt == MAX_ATTEMPTS:
                    raise RuntimeError(f"no working recipe after {MAX_ATTEMPTS} attempts") from e
                ob["status"] = "revising"
                prof = profiles.validate(profiles.revise(ws, system_map, prof, err))
                say("revised the recipe", profile=prof)
                continue
            pages = _pages(copy)
            fid = copy.fidelity()
            ob["attempts"].append({"attempt": attempt, "ok": True})
            prof["baseline_sql"] = list(system_map["schema_files"])
            store.set_project_meta(pid, "profile_draft", prof)
            ob |= {"status": "ready", "pages": pages, "fidelity": fid, "finished_at": time.time()}
            say(f"copy booted · fidelity {fid['score']}% · {sum(1 for p in pages if isinstance(p['status'], int) and p['status'] < 400)}/{len(pages)} pages answer")
            return
    except Exception as e:  # noqa: BLE001
        log.error("onboarding %s failed:\n%s", pid, traceback.format_exc())
        ob |= {"status": "failed", "error": str(e)[-3000:], "finished_at": time.time()}
        say(f"onboarding failed: {e}")
    finally:
        if copy:
            copy.down()


def approve(store: Store, pid: str, who: str) -> dict[str, Any]:
    draft = store.get_project_meta(pid, "profile_draft")
    if not draft:
        raise ValueError("nothing to approve: onboard the project first")
    store.set_project_meta(pid, "profile", draft)
    store.set_project_meta(pid, "profile_approved_by", {"who": who, "at": time.time()})
    return state(store, pid)
