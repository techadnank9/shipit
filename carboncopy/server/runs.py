"""Run lifecycle: create engine runs, keep the runs table in sync with them, call integration hooks."""
from __future__ import annotations

import inspect
import logging
import os
from pathlib import Path
from types import ModuleType
from typing import Any, Callable

from . import config
from .events import broker
from .queue import enqueue
from .repos import RepoError, prepare_repo
from .store import Row, Store

log = logging.getLogger("ccopy.runs")
Emit = Callable[[str, dict[str, Any]], None]


class ApiError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code, self.detail = status_code, detail


def engine() -> ModuleType:
    from carboncopy import pipeline

    if not hasattr(pipeline, "ChangeRun") or not hasattr(pipeline, "TestRun"):
        raise ApiError(503, "engine not available: carboncopy.pipeline has no ChangeRun/TestRun yet")
    return pipeline


def status_of(run: Any) -> str:
    s = run.status
    return str(getattr(s, "value", s))


def make_emit(store: Store, holder: dict[str, str]) -> Emit:
    """Engine emit callback: publish to SSE subscribers and mirror `status` events into the runs table."""

    def emit(event: str, data: dict[str, Any]) -> None:
        rid = holder.get("id")
        if not rid:
            return
        data = dict(data or {})
        if event == "status" and "status" in data:
            status = str(getattr(data["status"], "value", data["status"]))
            data["status"] = status
            try:
                store.update_run(rid, status=status, passed=config.passed_for(status))
            except Exception:
                log.exception("status update failed for %s", rid)
        broker.publish(rid, event, data)

    return emit


def load_run(store: Store, row: Row) -> Any:
    eng = engine()
    cls = eng.ChangeRun if row["kind"] == "change" else eng.TestRun
    emit = make_emit(store, {"id": row["id"]})
    if "emit" in inspect.signature(cls.load).parameters:
        return cls.load(Path(row["run_dir"]), emit=emit)
    run = cls.load(Path(row["run_dir"]))
    run.emit = emit
    return run


def notify(store: Store, rid: str) -> None:
    """Call integrations.on_run_finished once per terminal/awaiting status. Never raises."""
    run = store.get_run(rid, state=True)
    if not run or run["status"] not in config.TERMINAL | config.AWAITING:
        return
    key = f"notified:{run['status']}"
    try:
        if store.get_run_meta(rid, key):
            return
        store.set_run_meta(rid, key, True)
        from carboncopy.integrations import on_run_finished
    except ImportError:
        return
    except Exception:
        log.exception("notify bookkeeping failed for %s", rid)
        return
    public = os.environ.get("CCOPY_PUBLIC_URL", "http://127.0.0.1:8080").rstrip("/")
    try:
        on_run_finished(run | {"url": f"{public}/run?id={rid}"})
    except Exception:
        log.exception("on_run_finished failed for %s", rid)


def sync(store: Store, rid: str, run: Any) -> Row:
    status = status_of(run)
    store.update_run(rid, status=status, passed=config.passed_for(status))
    broker.publish(rid, "status", {"status": status})
    notify(store, rid)
    return store.get_run(rid, state=True)  # type: ignore[return-value]


def fail(store: Store, rid: str, message: str) -> None:
    store.update_run(rid, status="failed", passed=False)
    store.set_run_meta(rid, "error", message)
    broker.publish(rid, "log", {"message": f"error: {message}"})
    broker.publish(rid, "status", {"status": "failed"})
    notify(store, rid)


def _project(store: Store, pid: str) -> Row:
    p = store.get_project(pid)
    if not p:
        raise ApiError(404, "project not found")
    return p


def _repo(p: Row, ref: str | None) -> Path:
    try:
        return prepare_repo(p, ref=ref)
    except RepoError as e:
        raise ApiError(400, str(e)) from e


def _register(store: Store, run: Any, holder: dict[str, str], pid: str, kind: str, trigger: str, title: str) -> Row:
    holder["id"] = run.id
    return store.insert_run(id=run.id, project_id=pid, kind=kind, status="queued", trigger=trigger, title=title, run_dir=str(Path(run.dir).resolve()))


def start_change_run(store: Store, pid: str, request: str, trigger: str = "manual", ref: str | None = None) -> Row:
    p = _project(store, pid)
    eng = engine()
    holder: dict[str, str] = {}
    run = eng.ChangeRun.create(_repo(p, ref), request, config.runs_dir(), emit=make_emit(store, holder), profile=store.get_project_meta(pid, "profile"))
    title = " ".join(request.split())
    row = _register(store, run, holder, pid, "change", trigger, title if len(title) <= 80 else title[:79] + "…")
    if ref:
        store.set_run_meta(run.id, "ref", ref)
    enqueue("change.prepare", run.id)
    return row


def start_test_run(store: Store, pid: str, trigger: str = "manual", test_ids: list[str] | None = None, ref: str | None = None) -> Row:
    p = _project(store, pid)
    tests = store.list_saved_tests(pid, test_ids)
    checks = store.list_db_checks(pid)
    if not tests and not checks:
        raise ApiError(400, "no saved tests or DB checks to run")
    n = len(tests)
    title = f"{n} test{'' if n == 1 else 's'} · {trigger}" if n else f"{len(checks)} DB checks · {trigger}"
    eng = engine()
    holder: dict[str, str] = {}
    run = eng.TestRun.create(
        _repo(p, ref),
        [{"name": t["name"], "instructions": t["instructions"]} for t in tests],
        [{"description": c["description"], "sql": c["sql"], "expect": c["expect"]} for c in checks],
        config.runs_dir(),
        emit=make_emit(store, holder),
        title=title,
        profile=store.get_project_meta(pid, "profile"),
    )
    row = _register(store, run, holder, pid, "test", trigger, title)
    if ref:
        store.set_run_meta(run.id, "ref", ref)
    enqueue("test.execute", run.id)
    return row


def _change(store: Store, rid: str) -> tuple[Row, Any]:
    row = store.run_row(rid)
    if not row:
        raise ApiError(404, "run not found")
    if row["kind"] != "change":
        raise ApiError(409, "only change runs have approval gates")
    return row, load_run(store, row)


def approve_requirements(store: Store, rid: str, who: str) -> Row:
    row, run = _change(store, rid)
    if status_of(run) != "awaiting_requirements" or store.get_run_meta(rid, "requirements_approved"):
        raise ApiError(409, f"run is {status_of(run)}, not awaiting requirements approval")
    run.approve_requirements(who)
    store.set_run_meta(rid, "requirements_approved", who)
    broker.publish(rid, "log", {"message": f"requirements approved by {who}"})
    out = sync(store, rid, run)
    enqueue("change.execute", rid)
    return out


def reject(store: Store, rid: str, who: str, reason: str = "") -> Row:
    row, run = _change(store, rid)
    if status_of(run) not in config.AWAITING:
        raise ApiError(409, f"run is {status_of(run)}, nothing to reject")
    run.reject(who, reason=reason)
    return sync(store, rid, run)


def approve_ship(store: Store, rid: str, who: str) -> Row:
    row, run = _change(store, rid)
    if status_of(run) != "awaiting_ship":
        raise ApiError(409, f"run is {status_of(run)}, not awaiting ship approval")
    run.approve_ship(who)
    return sync(store, rid, run)


def recover(store: Store) -> None:
    """After a restart of a local-queue server: jobs in memory are gone, so in-flight runs failed."""
    for row in store.runs_with_status(config.TERMINAL | config.AWAITING, exclude=True):
        log.warning("run %s was %s when the server stopped; marking failed", row["id"], row["status"])
        fail(store, row["id"], f"server restarted while run was {row['status']}")
