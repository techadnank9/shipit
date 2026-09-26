"""FastAPI app: the HTTP API from INTERFACES.md, SSE run events, optional integrations and the static dashboard."""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config, runs
from .events import broker
from .queue import get_queue
from .repos import RepoError, prepare_repo
from .store import Row, get_store, next_cron

log = logging.getLogger("ccopy.server")
WEB_OUT = Path(__file__).resolve().parents[2] / "web" / "out"
HEARTBEAT = 15.0


class ProjectIn(BaseModel):
    name: str = Field(min_length=1)
    repo_path: str | None = None
    git_url: str | None = None


class TestIn(BaseModel):
    name: str = Field(min_length=1)
    instructions: str = Field(min_length=1)


class CheckIn(BaseModel):
    description: str = Field(min_length=1)
    sql: str = Field(min_length=1)
    expect: Any = None


class ChangeIn(BaseModel):
    request: str = Field(min_length=1)


class TestRunIn(BaseModel):
    trigger: str = "manual"
    test_ids: list[str] | None = None


class WhoIn(BaseModel):
    who: str = "api"


class RejectIn(BaseModel):
    who: str = "api"
    reason: str = ""


class ScheduleIn(BaseModel):
    cron: str | None = None


def _start_scheduler() -> None:
    try:
        from carboncopy.integrations.schedule import start_scheduler
    except ImportError:
        return
    try:
        start_scheduler(get_store())
    except Exception:
        log.exception("scheduler failed to start")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    store = get_store()
    q = get_queue()
    if config.queue_kind() == "local":
        runs.recover(store)
    q.start()
    _start_scheduler()
    yield


app = FastAPI(title="Carbon Copy", version="0.1.0", lifespan=lifespan)


@app.exception_handler(runs.ApiError)
async def _api_error(request: Request, exc: runs.ApiError) -> JSONResponse:
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


@app.middleware("http")
async def _auth(request: Request, call_next):  # type: ignore[no-untyped-def]
    token = config.api_token()
    path = request.url.path
    if token and path.startswith("/api/") and path != "/api/health" and request.method != "OPTIONS":
        header = request.headers.get("authorization", "")
        given = header[7:] if header.lower().startswith("bearer ") else request.query_params.get("token", "")
        if not hmac.compare_digest(given.encode(), token.encode()):
            return JSONResponse({"detail": "missing or invalid bearer token"}, status_code=401)
    return await call_next(request)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _project(pid: str) -> Row:
    p = get_store().get_project(pid)
    if not p:
        raise HTTPException(404, "project not found")
    return p


def _run_row(rid: str) -> Row:
    r = get_store().run_row(rid)
    if not r:
        raise HTTPException(404, "run not found")
    return r


def _run_file(rid: str, key: str, default: str) -> Path:
    row = _run_row(rid)
    run_dir = Path(row["run_dir"]).resolve()
    try:
        name = json.loads((run_dir / "state.json").read_text()).get(key) or default
    except (OSError, ValueError):
        name = default
    f = (run_dir / name).resolve()
    if not f.is_relative_to(run_dir) or not f.is_file():
        raise HTTPException(404, f"{default} not available for this run")
    return f


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"ok": True, "ai": config.ai_mode()}


@app.get("/api/projects")
def list_projects() -> list[Row]:
    return get_store().list_projects()


@app.post("/api/projects")
def create_project(body: ProjectIn) -> Row:
    if bool(body.repo_path) == bool(body.git_url):
        raise HTTPException(400, "give exactly one of repo_path or git_url")
    store = get_store()
    if body.repo_path:
        path = Path(body.repo_path).expanduser().resolve()
        if not path.is_dir():
            raise HTTPException(400, f"repo_path is not a directory: {path}")
        return store.create_project(body.name, repo_path=str(path))
    p = store.create_project(body.name, git_url=body.git_url)
    try:
        prepare_repo(p)
    except RepoError as e:
        store.delete_project(p["id"])
        raise HTTPException(400, str(e)) from e
    return p


@app.get("/api/projects/{pid}")
def get_project(pid: str) -> Row:
    return _project(pid)


@app.get("/api/projects/{pid}/map")
def project_map(pid: str) -> dict[str, Any]:
    from carboncopy.reader import scan

    try:
        repo = prepare_repo(_project(pid), fresh=False)
    except RepoError as e:
        raise HTTPException(400, str(e)) from e
    return scan(repo)


@app.get("/api/projects/{pid}/tests")
def list_tests(pid: str) -> list[Row]:
    _project(pid)
    return get_store().list_saved_tests(pid)


@app.post("/api/projects/{pid}/tests")
def add_test(pid: str, body: TestIn) -> Row:
    _project(pid)
    return get_store().add_saved_test(pid, body.name, body.instructions)


@app.delete("/api/tests/{tid}")
def delete_test(tid: str) -> dict[str, bool]:
    if not get_store().delete_saved_test(tid):
        raise HTTPException(404, "test not found")
    return {"ok": True}


@app.get("/api/projects/{pid}/checks")
def list_checks(pid: str) -> list[Row]:
    _project(pid)
    return get_store().list_db_checks(pid)


@app.post("/api/projects/{pid}/checks")
def add_check(pid: str, body: CheckIn) -> Row:
    _project(pid)
    return get_store().add_db_check(pid, body.description, body.sql, body.expect)


@app.post("/api/projects/{pid}/changes")
def create_change(pid: str, body: ChangeIn) -> Row:
    return runs.start_change_run(get_store(), pid, body.request)


@app.post("/api/projects/{pid}/test-runs")
def create_test_run(pid: str, body: TestRunIn) -> Row:
    if body.trigger not in ("manual", "pr", "schedule", "ci", "mcp"):
        raise HTTPException(400, "trigger must be manual, pr, schedule, ci or mcp")
    return runs.start_test_run(get_store(), pid, body.trigger, body.test_ids)


@app.get("/api/runs")
def list_runs(project_id: str | None = None, kind: str | None = None, status: str | None = None, limit: int = 50) -> list[Row]:
    return get_store().list_runs(project_id=project_id, kind=kind, status=status, limit=limit)


@app.get("/api/runs/{rid}")
def get_run(rid: str) -> Row:
    run = get_store().get_run(rid, state=True)
    if not run:
        raise HTTPException(404, "run not found")
    return run


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


@app.get("/api/runs/{rid}/events")
async def run_events(rid: str, request: Request) -> StreamingResponse:
    status = _run_row(rid)["status"]
    q, history = broker.subscribe(rid)

    async def stream() -> AsyncIterator[str]:
        try:
            yield _sse("status", {"status": status})
            for event, data in history:
                yield _sse(event, data)
            while not await request.is_disconnected():
                try:
                    event, data = await asyncio.wait_for(q.get(), HEARTBEAT)
                except TimeoutError:
                    yield ": heartbeat\n\n"
                    continue
                yield _sse(event, data)
        finally:
            broker.unsubscribe(rid, q)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/projects/{pid}/onboarding")
def get_onboarding(pid: str) -> dict[str, Any]:
    from . import onboarding

    _project(pid)
    return onboarding.state(get_store(), pid)


@app.post("/api/projects/{pid}/onboard")
def start_onboarding(pid: str) -> dict[str, Any]:
    from . import onboarding
    from .queue import enqueue

    _project(pid)
    store = get_store()
    current = onboarding.state(store, pid)["onboarding"]
    if current["status"] in ("queued", "drafting", "booting", "revising", "sweeping"):
        raise HTTPException(409, "onboarding is already running")
    store.set_project_meta(pid, "onboarding", {"status": "queued", "log": [], "attempts": []})
    enqueue("project.onboard", "", project_id=pid)
    return onboarding.state(store, pid)


@app.post("/api/projects/{pid}/sweep")
def start_sweep(pid: str) -> dict[str, Any]:
    from . import onboarding
    from .queue import enqueue

    _project(pid)
    store = get_store()
    ob = onboarding.state(store, pid)["onboarding"]
    if ob["status"] in ("queued", "drafting", "booting", "revising", "sweeping"):
        raise HTTPException(409, "onboarding or a sweep is already running")
    if not (store.get_project_meta(pid, "profile") or store.get_project_meta(pid, "profile_draft")):
        raise HTTPException(409, "onboard the project first")
    ob["status"] = "sweeping"
    store.set_project_meta(pid, "onboarding", ob)
    enqueue("project.sweep", "", project_id=pid)
    return onboarding.state(store, pid)


@app.get("/api/projects/{pid}/sweep")
def get_sweep(pid: str) -> Any:
    from . import onboarding

    _project(pid)
    f = onboarding.sweep_path(pid)
    if not f.exists():
        raise HTTPException(404, "no QA sweep yet")
    return JSONResponse(json.loads(f.read_text()))


@app.post("/api/projects/{pid}/profile/approve")
def approve_profile(pid: str, body: WhoIn) -> dict[str, Any]:
    from . import onboarding

    _project(pid)
    try:
        return onboarding.approve(get_store(), pid, body.who)
    except ValueError as e:
        raise HTTPException(409, str(e)) from e


@app.put("/api/projects/{pid}/profile")
def edit_profile(pid: str, body: dict[str, Any]) -> dict[str, Any]:
    from carboncopy.profile import validate

    from . import onboarding

    _project(pid)
    try:
        get_store().set_project_meta(pid, "profile_draft", validate(body))
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return onboarding.state(get_store(), pid)


@app.post("/api/runs/{rid}/approve-requirements")
def approve_requirements(rid: str, body: WhoIn) -> Row:
    return runs.approve_requirements(get_store(), rid, body.who)


@app.post("/api/runs/{rid}/reject")
def reject(rid: str, body: RejectIn) -> Row:
    return runs.reject(get_store(), rid, body.who, body.reason)


@app.post("/api/runs/{rid}/approve-ship")
def approve_ship(rid: str, body: WhoIn) -> Row:
    return runs.approve_ship(get_store(), rid, body.who)


@app.get("/api/runs/{rid}/report", response_class=HTMLResponse)
def run_report(rid: str) -> HTMLResponse:
    return HTMLResponse(_run_file(rid, "report", "report.html").read_text())


@app.get("/api/runs/{rid}/patch", response_class=PlainTextResponse)
def run_patch(rid: str) -> PlainTextResponse:
    return PlainTextResponse(_run_file(rid, "patch", "change.patch").read_text())


@app.get("/api/runs/{rid}/audit")
def run_audit(rid: str) -> dict[str, Any]:
    from carboncopy.audit import AuditLog

    audit = AuditLog(Path(_run_row(rid)["run_dir"]) / "audit.jsonl")
    try:
        return {"verified": audit.verify(), "entries": audit.entries()}
    except (ValueError, KeyError):
        return {"verified": False, "entries": []}


@app.get("/api/projects/{pid}/schedule")
def get_schedule(pid: str) -> Row:
    _project(pid)
    return get_store().get_schedule(pid)


@app.put("/api/projects/{pid}/schedule")
def put_schedule(pid: str, body: ScheduleIn) -> Row:
    _project(pid)
    cron = (body.cron or "").strip() or None
    if cron:
        try:
            next_cron(cron)
        except (ValueError, KeyError) as e:
            raise HTTPException(400, f"invalid cron expression: {cron}") from e
    return get_store().set_schedule(pid, cron)


def _mount_integrations() -> None:
    try:
        from carboncopy.integrations.github import router as github_router

        app.include_router(github_router, prefix="/webhooks/github")
    except ImportError:
        pass
    try:
        from carboncopy.integrations.mcp_server import router as mcp_router
    except ImportError:
        return
    if isinstance(mcp_router, APIRouter):
        app.include_router(mcp_router, prefix="/mcp")
    else:
        app.mount("/mcp", mcp_router)


def _page(name: str) -> Path | None:
    for f in (WEB_OUT / f"{name}.html", WEB_OUT / name / "index.html"):
        if f.is_file():
            return f
    return None


def _mount_dashboard() -> None:
    if not WEB_OUT.is_dir():
        return
    for name in ("project", "run", "onboard"):
        if page := _page(name):
            app.add_api_route(f"/{name}", lambda page=page: FileResponse(page), methods=["GET"], include_in_schema=False)
    app.mount("/", StaticFiles(directory=WEB_OUT, html=True), name="dashboard")


_mount_integrations()
_mount_dashboard()
