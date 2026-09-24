"""MCP server for coding agents (Claude Code, Cursor, Codex) at /mcp.

Transport: MCP Streamable HTTP, stateless, JSON responses only (no SSE stream, no sessions):
POST a JSON-RPC message or batch → `application/json`; notifications/responses → 202; GET → 405.
Hand-rolled rather than the `mcp` SDK because the SDK's transport wants its own ASGI app and
lifespan (session manager task group), which does not mount cleanly inside the server's APIRouter.

Data access (one rule): reads and run_tests go through the Store (`carboncopy.server.store`);
writes that the HTTP API owns (create_test, request_change, approve) are sent in-process to the
same FastAPI app's /api routes via httpx.ASGITransport, so validation, queueing and the audit
trail are exactly what the dashboard gets. No network hop, no dependence on CCOPY_PUBLIC_URL.
If CCOPY_API_TOKEN is set, /mcp requires `Authorization: Bearer <token>` and forwards it.
"""
import json
import logging
import os

import httpx
from fastapi import APIRouter, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from . import GATES, headline, public_url, step_text, summarize

log = logging.getLogger("carboncopy.integrations.mcp")
router = APIRouter()

VERSIONS = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]
SERVER_INFO = {"name": "carboncopy", "title": "Carbon Copy", "version": "0.1.0"}
INSTRUCTIONS = (
    "Carbon Copy tests code changes and plain-English browser tests on a working copy of production. "
    "Typical loop: list_projects → run_tests (or request_change) → poll get_run every ~15s until status is "
    "passed/failed/shipped/blocked or awaiting_* → read failed steps and errors. Gates (awaiting_requirements, "
    "awaiting_ship) are for humans; only call approve when the user explicitly told you to."
)


def _store():
    from carboncopy.server.store import get_store
    return get_store()


class ToolError(Exception):
    pass


def _s(v, default=""):
    return v if isinstance(v, str) else default


TOOLS = [
    {"name": "list_projects", "title": "List projects", "description": "List Carbon Copy projects with their id and latest run status.",
     "inputSchema": {"type": "object", "properties": {}}, "annotations": {"readOnlyHint": True}},
    {"name": "list_tests", "title": "List saved tests", "description": "List a project's saved plain-English browser tests.",
     "inputSchema": {"type": "object", "properties": {"project_id": {"type": "string"}}, "required": ["project_id"]},
     "annotations": {"readOnlyHint": True}},
    {"name": "create_test", "title": "Create a test", "description": "Save a plain-English browser test, e.g. instructions 'Sign up, add an item to the cart, check out, and see the order confirmation'.",
     "inputSchema": {"type": "object", "properties": {"project_id": {"type": "string"}, "name": {"type": "string"},
                                                       "instructions": {"type": "string", "description": "what a human QA tester would do and expect to see"}},
                     "required": ["project_id", "name", "instructions"]}},
    {"name": "run_tests", "title": "Run tests", "description": "Run saved tests on a fresh copy of production. Returns a run id; poll get_run for the result (runs take minutes).",
     "inputSchema": {"type": "object", "properties": {"project_id": {"type": "string"},
                                                       "test_ids": {"type": "array", "items": {"type": "string"}, "description": "omit to run all saved tests"}},
                     "required": ["project_id"]}},
    {"name": "get_run", "title": "Get run", "description": "Status and results of a run: per-test pass/fail, the failed step and the error the AI tester saw, and report links.",
     "inputSchema": {"type": "object", "properties": {"run_id": {"type": "string"}}, "required": ["run_id"]},
     "annotations": {"readOnlyHint": True}},
    {"name": "request_change", "title": "Request a change", "description": "Ask Carbon Copy's agent to make a code change on a copy of production. It writes requirements, then pauses for human approval.",
     "inputSchema": {"type": "object", "properties": {"project_id": {"type": "string"}, "request": {"type": "string"}}, "required": ["project_id", "request"]}},
    {"name": "approve", "title": "Approve a gate", "description": "Approve a human gate on a change run: 'requirements' (awaiting_requirements) or 'ship' (awaiting_ship). Only when the user explicitly asked.",
     "inputSchema": {"type": "object", "properties": {"run_id": {"type": "string"}, "gate": {"type": "string", "enum": ["requirements", "ship"]},
                                                       "who": {"type": "string", "description": "name recorded in the audit trail"}},
                     "required": ["run_id", "gate"]},
     "annotations": {"destructiveHint": True}},
]


def _tools() -> list[dict]:
    """Approving a human gate over MCP is off unless the operator opts in (CCOPY_MCP_ALLOW_APPROVE=1)."""
    if os.environ.get("CCOPY_MCP_ALLOW_APPROVE") == "1":
        return TOOLS
    return [t for t in TOOLS if t["name"] != "approve"]


async def _api(request: Request, method: str, path: str, body: dict | None = None) -> dict | list:
    headers = {}
    if tok := os.environ.get("CCOPY_API_TOKEN"):
        headers["Authorization"] = f"Bearer {tok}"
    transport = httpx.ASGITransport(app=request.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://carboncopy.internal", headers=headers, timeout=60) as c:
        r = await c.request(method, path, json=body)
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail", r.text)
        except ValueError:
            detail = r.text
        raise ToolError(f"{method} {path} failed ({r.status_code}): {detail}")
    return r.json()


def run_summary(run: dict) -> dict:
    s = summarize(run)
    out = {k: s[k] for k in ("id", "project_id", "kind", "status", "trigger", "title", "passed", "error", "url", "report_url")}
    out["headline"] = headline(s) if s["status"] not in GATES else f"waiting for a human at {s['status']}"
    out["tests"] = [{"name": t["name"], "passed": t["passed"]} | (
        {"failed_step": step_text(t["failed_step"]), "error": t["failed_step"]["error"], "trying_to": t["failed_step"]["why"]}
        if t.get("failed_step") else {}) for t in s["tests"]]
    out["db_checks"] = s["db_checks"]
    if s["final_checks"]:
        out["final_checks"] = s["final_checks"]
    state = run.get("state") or {}
    if s["status"] == "awaiting_requirements" and state.get("requirements"):
        out["requirements"] = state["requirements"]
    if state.get("agent"):
        out["agent"] = {k: state["agent"].get(k) for k in ("summary", "edited_files")}
    if state.get("patch"):
        out["patch_url"] = f"{public_url()}/api/runs/{s['id']}/patch"
    return out


async def call_tool(request: Request, name: str, args: dict):
    st = lambda fn, *a, **kw: run_in_threadpool(fn, *a, **kw)  # noqa: E731
    if name == "list_projects":
        store = _store()
        return [{"id": p["id"], "name": p.get("name"), "git_url": p.get("git_url"),
                 "last_run": ({k: (p.get("last_run") or {}).get(k) for k in ("id", "status", "passed", "kind")} if p.get("last_run") else None)}
                for p in await st(store.list_projects)]
    if name == "list_tests":
        store = _store()
        pid = _s(args.get("project_id"))
        if not await st(store.get_project, pid):
            raise ToolError(f"no project {pid!r}")
        return [{"id": t["id"], "name": t["name"], "instructions": t["instructions"]} for t in await st(store.list_saved_tests, pid)]
    if name == "create_test":
        return await _api(request, "POST", f"/api/projects/{_s(args.get('project_id'))}/tests",
                          {"name": _s(args.get("name")), "instructions": _s(args.get("instructions"))})
    if name == "run_tests":
        store = _store()
        pid = _s(args.get("project_id"))
        if not await st(store.get_project, pid):
            raise ToolError(f"no project {pid!r}")
        ids = args.get("test_ids") or None
        run = await st(store.create_test_run, pid, "mcp", ids)
        return {"run_id": run["id"], "status": run.get("status"), "url": f"{public_url()}/run?id={run['id']}",
                "next": "call get_run with this run_id until status is passed or failed"}
    if name == "get_run":
        rid = _s(args.get("run_id"))
        store = _store()
        try:
            run = await st(store.get_run, rid, state=True)
        except TypeError:  # a store whose get_run always includes state
            run = await st(store.get_run, rid)
        if not run:
            raise ToolError(f"no run {rid!r}")
        return run_summary(run)
    if name == "request_change":
        run = await _api(request, "POST", f"/api/projects/{_s(args.get('project_id'))}/changes", {"request": _s(args.get("request"))})
        return {"run_id": run["id"], "status": run.get("status"), "url": f"{public_url()}/run?id={run['id']}",
                "next": "poll get_run; it pauses at awaiting_requirements for human approval"}
    if name == "approve":
        gate = args.get("gate")
        if gate not in ("requirements", "ship"):
            raise ToolError("gate must be 'requirements' or 'ship'")
        path = "approve-requirements" if gate == "requirements" else "approve-ship"
        run = await _api(request, "POST", f"/api/runs/{_s(args.get('run_id'))}/{path}", {"who": _s(args.get("who"), "") or "mcp-agent"})
        return {"run_id": run["id"], "status": run.get("status")}
    raise KeyError(name)


def _err(id_, code: int, msg: str) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": msg}}


async def handle(request: Request, msg) -> dict | None:
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return _err(None, -32600, "invalid request")
    method, id_ = msg.get("method"), msg.get("id")
    if method is None:  # a response from the client; we never send requests
        return None
    is_note = "id" not in msg
    params = msg.get("params") or {}
    try:
        if method == "initialize":
            want = params.get("protocolVersion")
            result = {"protocolVersion": want if want in VERSIONS else VERSIONS[0],
                      "capabilities": {"tools": {"listChanged": False}}, "serverInfo": SERVER_INFO, "instructions": INSTRUCTIONS}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": _tools()}
        elif method == "tools/call":
            name, args = params.get("name"), params.get("arguments") or {}
            if name not in {t["name"] for t in _tools()}:
                return None if is_note else _err(id_, -32602, f"unknown tool: {name}")
            try:
                data = await call_tool(request, name, args)
                structured = data if isinstance(data, dict) else {"items": data}
                result = {"content": [{"type": "text", "text": json.dumps(data, indent=2, default=str)}],
                          "structuredContent": structured, "isError": False}
            except ToolError as e:
                result = {"content": [{"type": "text", "text": str(e)}], "isError": True}
            except Exception as e:  # noqa: BLE001
                log.exception("mcp tool %s failed", name)
                result = {"content": [{"type": "text", "text": f"{name} failed: {type(e).__name__}: {e}"}], "isError": True}
        elif method.startswith("notifications/"):
            return None
        else:
            return None if is_note else _err(id_, -32601, f"method not found: {method}")
    except Exception as e:  # noqa: BLE001
        log.exception("mcp %s failed", method)
        return None if is_note else _err(id_, -32603, str(e))
    return None if is_note else {"jsonrpc": "2.0", "id": id_, "result": result}


def _authorized(request: Request) -> bool:
    tok = os.environ.get("CCOPY_API_TOKEN")
    return not tok or request.headers.get("Authorization", "") == f"Bearer {tok}"


@router.post("")
@router.post("/", include_in_schema=False)
async def mcp_post(request: Request):
    if not _authorized(request):
        return JSONResponse({"detail": "unauthorized"}, status_code=401, headers={"WWW-Authenticate": "Bearer"})
    try:
        body = json.loads(await request.body() or b"null")
    except ValueError:
        return JSONResponse(_err(None, -32700, "parse error"), status_code=400)
    if isinstance(body, list):
        if not body:
            return JSONResponse(_err(None, -32600, "empty batch"), status_code=400)
        out = [r for r in [await handle(request, m) for m in body] if r is not None]
        return JSONResponse(out) if out else Response(status_code=202)
    r = await handle(request, body)
    return JSONResponse(r) if r is not None else Response(status_code=202)


@router.get("")
@router.get("/", include_in_schema=False)
async def mcp_get():
    # No server-initiated stream: spec says return 405 when SSE on GET is not offered.
    return Response(status_code=405, headers={"Allow": "POST"})


@router.delete("")
@router.delete("/", include_in_schema=False)
async def mcp_delete():
    return Response(status_code=405, headers={"Allow": "POST"})
