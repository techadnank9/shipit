"""AI browser tester: plain-English test -> steps -> real Chromium clicking through the copy,
then checks the copy's database behind the screen. Records video + a screenshot per step."""
import base64
import json
import time
from pathlib import Path
from typing import Any

import psycopg
from playwright.sync_api import Page, sync_playwright

from .copier import DB_URL_HOST
from . import standin
from .llm import ai_mode, ask_json

STEP = {
    "type": "object",
    "additionalProperties": False,
    "required": ["kind", "action", "target", "value", "why"],
    "properties": {
        "kind": {"type": "string", "enum": ["ACT", "ASSERT"]},
        "action": {"type": "string", "enum": ["goto", "click", "double_click", "fill", "expect_text", "expect_no_text", "wait"]},
        "target": {"type": "string", "description": "URL path for goto; visible button text or input label for click/fill; for expect_text/expect_no_text the id, data-testid or aria-label of the element to look inside, or empty for the whole page"},
        "value": {"type": "string", "description": "text to type, or text expected on screen, or seconds to wait"},
        "why": {"type": "string"},
    },
}
PLAN = {"type": "object", "additionalProperties": False, "required": ["steps"], "properties": {"steps": {"type": "array", "items": STEP}}}

SYSTEM = """You turn a plain-English QA instruction into browser steps for Playwright.
Use only the actions in the schema. Targets must match visible button text or input aria-labels in the
page HTML you are given. Always start with goto "/". After each important action add an ASSERT step."""


def plan(instructions: str, page_html: str) -> list[dict]:
    if ai_mode() == "standin":
        return standin.browser_plan(instructions)
    return ask_json(f"INSTRUCTION:\n{instructions}\n\nPAGE HTML:\n{page_html[:12000]}", PLAN, SYSTEM, budget_usd=0.5)["steps"]


def _scope(page: Page, target: str) -> Any:
    """The element an expect_* step looks inside: by id, data-testid or aria-label; else the page."""
    t = target.strip().lstrip("#")
    if t:
        for loc in (page.locator(f"[id={json.dumps(t)}]"), page.get_by_test_id(t), page.get_by_label(t, exact=True)):
            if loc.count() > 0:
                return loc.first
    return page


def _do(page: Page, base: str, s: dict) -> None:
    a, t, v = s["action"], s["target"], s["value"]
    if a == "goto":
        page.goto(base + (t if t.startswith("/") else "/" + t))
        page.wait_for_load_state("networkidle")
    elif a in ("click", "double_click"):
        loc = page.get_by_role("button", name=t)
        if loc.count() == 0:
            loc = page.get_by_text(t, exact=False)
        (loc.first.dblclick if a == "double_click" else loc.first.click)(timeout=5000)
        page.wait_for_timeout(700)
    elif a == "fill":
        loc = page.get_by_label(t)
        if loc.count() == 0:
            loc = page.get_by_placeholder(t)
        loc.first.fill(v, timeout=5000)
    elif a == "expect_text":
        _scope(page, t).get_by_text(v, exact=False).first.wait_for(state="visible", timeout=5000)
    elif a == "expect_no_text":
        page.wait_for_timeout(500)
        if _scope(page, t).get_by_text(v, exact=False).count() > 0:
            raise AssertionError(f"'{v}' is on the screen{f' in {t}' if t else ''} but should not be")
    elif a == "wait":
        page.wait_for_timeout(int(float(v or 1) * 1000))


def db_check(inv: dict) -> dict:
    try:
        with psycopg.connect(DB_URL_HOST, autocommit=True) as c:
            row = c.execute(inv["sql"]).fetchone()
        got = "" if row is None else str(row[0])
        return {"description": inv["description"], "sql": inv["sql"], "expect": inv["expect"], "got": got, "passed": got == str(inv["expect"])}
    except Exception as e:  # noqa: BLE001 - report any DB error as a failed check
        return {"description": inv["description"], "sql": inv["sql"], "expect": inv["expect"], "got": f"error: {e}", "passed": False}


def run(base_url: str, tests: list[dict], invariants: list[dict], out_dir: Path, on_event=None) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    results = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for n, test in enumerate(tests):
            ctx = browser.new_context(record_video_dir=str(out_dir / f"video-{n}"), viewport={"width": 1100, "height": 720})
            page = ctx.new_page()
            page.goto(base_url + "/")
            page.wait_for_load_state("networkidle")
            record = {"name": test["name"], "instructions": test["instructions"], "steps": [], "passed": True}
            try:
                steps = plan(test["instructions"], page.content())
            except Exception as e:  # noqa: BLE001 - an unreadable test fails, the run continues
                steps = []
                record["passed"] = False
                record["steps"].append({"kind": "ACT", "action": "plan", "target": "", "value": "", "why": "", "ok": False,
                                        "error": str(e)[:400], "ms": 0, "screenshot": base64.b64encode(page.screenshot()).decode()})
            for i, s in enumerate(steps):
                t0 = time.time()
                err = None
                try:
                    _do(page, base_url, s)
                except Exception as e:  # noqa: BLE001
                    err = str(e).splitlines()[0][:300]
                shot = page.screenshot()
                record["steps"].append({**s, "ok": err is None, "error": err, "ms": int((time.time() - t0) * 1000),
                                        "screenshot": base64.b64encode(shot).decode()})
                if err:
                    record["passed"] = False
                    break
            video = page.video.path() if page.video else None
            ctx.close()
            record["video"] = str(video) if video else None
            results.append(record)
            if on_event:
                on_event("browser", {"test": record["name"], "passed": record["passed"]})
        browser.close()
    db = [db_check(i) for i in invariants]
    return {"tests": results, "db_checks": db, "passed": all(r["passed"] for r in results) and all(d["passed"] for d in db)}


def summarize(res: dict) -> str:
    return json.dumps({"tests": [(t["name"], t["passed"]) for t in res["tests"]], "db": [(d["description"], d["passed"], d["got"]) for d in res["db_checks"]]})
