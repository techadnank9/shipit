"""Project checkouts: a local repo_path as-is, or a shallow clone of git_url under CCOPY_DATA_DIR/repos."""
from __future__ import annotations

import re
import subprocess
import threading
from pathlib import Path
from typing import Any

from . import config

_locks: dict[str, threading.Lock] = {}
_guard = threading.Lock()


class RepoError(RuntimeError):
    pass


def _git(*args: str, cwd: Path | None = None) -> None:
    r = subprocess.run(["git", "-c", "advice.detachedHead=false", *args], cwd=cwd, capture_output=True, text=True, timeout=600)
    if r.returncode:
        raise RepoError(f"git {args[0]} failed: {(r.stderr or r.stdout).strip()[-500:]}")


def prepare_repo(project: dict[str, Any], ref: str | None = None, fresh: bool = True) -> Path:
    """Return a checkout for the project. For git projects, clone once, then refresh (or check out `ref`)."""
    if project.get("repo_path"):
        return Path(project["repo_path"])
    url = project.get("git_url")
    if not url:
        raise RepoError("project has neither repo_path nor git_url")
    name = project["id"] + (f"@{re.sub(r'[^A-Za-z0-9._-]+', '_', ref)[:80]}" if ref else "")
    dest = config.repos_dir() / name
    with _guard:
        lock = _locks.setdefault(str(dest), threading.Lock())
    with lock:
        cloned = (dest / ".git").exists()
        if not cloned:
            _git("clone", "--depth", "1", url, str(dest))
        if ref:
            _git("fetch", "--depth", "1", "origin", ref, cwd=dest)
            _git("checkout", "--force", "FETCH_HEAD", cwd=dest)
        elif cloned and fresh:
            _git("fetch", "--depth", "1", "origin", "HEAD", cwd=dest)
            _git("reset", "--hard", "FETCH_HEAD", cwd=dest)
    return dest
