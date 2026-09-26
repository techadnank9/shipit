"""AI browser tester: plain-English test -> steps -> real Chromium clicking through the copy,
then checks the copy's database behind the screen. Records video + a screenshot per step."""
import base64
import json
import re
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
        "value": {"type": "string", "description": "text to type, or text expected on screen, or seconds to wait. For long repeated text write {repeat:CHARS:N}, e.g. {repeat:a:500}"},
        "why": {"type": "string"},
    },
}
PLAN = {"type": "object", "additionalProperties": False, "required": ["steps"], "properties": {"steps": {"type": "array", "items": STEP}}}

SYSTEM = """You turn a plain-English QA instruction into browser steps for Playwright.
Use only the actions in the schema. Targets must match a button text, input label or placeholder listed in
the CURRENT PAGE you are given. Start with goto to the page the instruction names ("/" if it names none).
You will see each new page when you get there, so plan steps only as far as the current page lets you
know the labels; stop after the step that leads to a new page. After each important action add an
ASSERT step. Text checks are case-sensitive; expect_no_text matches whole words only. An expect_no_text must name text that
would appear only if the behaviour were wrong (an error message, the text you just tried to send), never
text that is always on the page such as names, headings or navigation. expect_text checks text a person can read on screen: button names that come only from aria-labels
(icon buttons such as "Zoom in") are not visible text, so check a visible result of using them instead.
Browser-native form validation
(required fields, type=email) shows no text on the page: check instead that the page did not move on."""

JS_DIGEST = """() => {
  const vis = e => e.offsetParent !== null || e.getClientRects().length > 0;
  const name = e => (e.getAttribute('aria-label') || (e.labels && e.labels[0] && e.labels[0].innerText) ||
    e.getAttribute('placeholder') || e.innerText || e.value || e.name || '').trim().replace(/\\s+/g, ' ').slice(0, 80);
  const q = s => Array.from(document.querySelectorAll(s)).filter(vis);
  return {
    buttons: q('button, [role=button], input[type=submit], input[type=button]').map(e => name(e) + (e.disabled ? ' [disabled]' : '')).filter(Boolean).slice(0, 60),
    inputs: q('input:not([type=hidden]):not([type=submit]):not([type=button]), textarea, select').map(e =>
      `${e.tagName.toLowerCase()}${e.type ? '/' + e.type : ''} label="${name(e)}" placeholder="${e.getAttribute('placeholder') || ''}"`).slice(0, 60),
    links: q('a[href]').map(a => `${(a.innerText || '').trim().slice(0, 40)} -> ${a.getAttribute('href')}`).slice(0, 80),
  };
}"""


def digest(page: Page) -> str:
    """What a tester sees: URL, visible text, and every control with the label a locator can use."""
    try:
        d = page.evaluate(JS_DIGEST)
        text = page.inner_text("body")[:5000]
    except Exception:  # noqa: BLE001
        return f"URL: {page.url}\n(page not readable)"
    return (f"URL: {page.url}\nBUTTONS: {d['buttons']}\nINPUTS: {d['inputs']}\nLINKS: {d['links']}\n"
            f"VISIBLE TEXT:\n{text}")


def plan(instructions: str, page_html: str, done: list[dict] | None = None, error: str | None = None) -> list[dict]:
    """Steps from here. `done` and `error` turn this into a re-plan from the page the tester is on."""
    if ai_mode() == "standin":
        return [] if done else standin.browser_plan(instructions)
    prompt = f"INSTRUCTION:\n{instructions}\n\nCURRENT PAGE:\n{page_html}"
    if done is not None:
        prompt += ("\n\nSTEPS ALREADY DONE (do not repeat them):\n"
                   + "\n".join(f"- {s['kind']} {s['action']} {s['target']!r} {s['value'][:60]!r} -> {'ok' if s['ok'] else s['error']}" for s in done)
                   + (f"\n\nTHE LAST ACTION FAILED: {error}\nFind the right control on the current page." if error else "")
                   + "\n\nReturn only the remaining steps (an empty list if the instruction is complete).")
    return ask_json(prompt, PLAN, SYSTEM, budget_usd=0.5)["steps"]


def _scope(page: Page, target: str) -> Any:
    """The element an expect_* step looks inside: by id, data-testid or aria-label; else the page."""
    t = target.strip().lstrip("#")
    if t:
        for loc in (page.locator(f"[id={json.dumps(t)}]"), page.get_by_test_id(t), page.get_by_label(t, exact=True)):
            if loc.count() > 0:
                return loc.first
    return page


REPEAT = re.compile(r"\{repeat:(.+?):(\d{1,5})\}")


def _expand(v: str) -> str:
    return REPEAT.sub(lambda m: m[1] * int(m[2]), v)


def _text(v: str, whole_word: bool = False) -> re.Pattern:
    """Case-sensitive. Negative checks match whole words, so "Send" is not found in "send a message"."""
    return re.compile(rf"(?<!\w){re.escape(v)}(?!\w)" if whole_word else re.escape(v))


def _rendered(page: Page, target: str) -> str:
    scope = _scope(page, target)
    try:
        return (scope.locator("body") if scope is page else scope).inner_text(timeout=2000)
    except Exception:  # noqa: BLE001 - page mid-navigation: nothing readable yet
        return ""


def _do(page: Page, base: str, s: dict) -> None:
    a, t, v = s["action"], s["target"], _expand(s["value"])
    if a == "goto":
        page.goto(base + (t if t.startswith("/") else "/" + t))
        settle(page)
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
        # Rendered text (what a person reads, after CSS such as text-transform), polled for up to 5s.
        rx = _text(v)
        deadline = time.time() + 5
        while not rx.search(_rendered(page, t)):
            if time.time() > deadline:
                raise AssertionError(f"'{v}' is not on the screen{f' in {t}' if t else ''}")
            page.wait_for_timeout(250)
    elif a == "expect_no_text":
        page.wait_for_timeout(500)
        if _text(v, whole_word=True).search(_rendered(page, t)):
            raise AssertionError(f"'{v}' is on the screen{f' in {t}' if t else ''} but should not be")
    elif a == "wait":
        page.wait_for_timeout(int(float(v or 1) * 1000))


def db_check(inv: dict, db_url: str = DB_URL_HOST) -> dict:
    try:
        with psycopg.connect(db_url, autocommit=True) as c:
            row = c.execute(inv["sql"]).fetchone()
        got = "" if row is None else str(row[0])
        return {"description": inv["description"], "sql": inv["sql"], "expect": inv["expect"], "got": got, "passed": got == str(inv["expect"])}
    except Exception as e:  # noqa: BLE001 - report any DB error as a failed check
        return {"description": inv["description"], "sql": inv["sql"], "expect": inv["expect"], "got": f"error: {e}", "passed": False}


def settle(page: Page, timeout: int = 15000) -> None:
    """Wait for the page to go quiet, but never fail on apps that poll or stream forever."""
    try:
        page.wait_for_load_state("networkidle", timeout=timeout)
    except Exception:  # noqa: BLE001
        page.wait_for_load_state("load")


MAX_REPLANS = 8


def _replan(test: dict, page: Page, done: list[dict], error: str | None) -> list[dict]:
    try:
        settle(page, timeout=5000)
        return plan(test["instructions"], digest(page), done=[d for d in done if d.get("action") != "plan"], error=error)
    except Exception:  # noqa: BLE001 - no new plan: the loop ends and the record stands as is
        return []


def run_one(browser: Any, base_url: str, test: dict, video_dir: Path) -> dict:
    """Plan and run one plain-English test in a fresh browser context, a screenshot per step."""
    ctx = browser.new_context(record_video_dir=str(video_dir), viewport={"width": 1100, "height": 720})
    page = ctx.new_page()
    record = {"name": test["name"], "instructions": test["instructions"], "steps": [], "passed": True}
    try:
        page.goto(base_url + test.get("start", "/"))
        settle(page)
        steps = plan(test["instructions"], digest(page))
    except Exception as e:  # noqa: BLE001 - an unreadable test fails, the run continues
        steps = []
        record["passed"] = False
        record["steps"].append({"kind": "ACT", "action": "plan", "target": "", "value": "", "why": "", "ok": False,
                                "error": f"{type(e).__name__}: {e}"[:400], "ms": 0, "screenshot": base64.b64encode(page.screenshot()).decode()})
    replans = 0
    while steps:
        s = steps.pop(0)
        before = page.url
        t0 = time.time()
        err = None
        try:
            _do(page, base_url, s)
        except Exception as e:  # noqa: BLE001
            err = (str(e).splitlines() or [type(e).__name__])[0][:300]
        shot = page.screenshot()
        # A control that could not be found gets one fresh look at the page; a failed check never does.
        if err and s["kind"] == "ACT" and replans < MAX_REPLANS:
            replans += 1
            record["steps"].append({**s, "ok": False, "error": err, "retried": True, "ms": int((time.time() - t0) * 1000),
                                    "screenshot": base64.b64encode(shot).decode()})
            steps = _replan(test, page, record["steps"], err)
            continue
        record["steps"].append({**s, "ok": err is None, "error": err, "ms": int((time.time() - t0) * 1000),
                                "screenshot": base64.b64encode(shot).decode()})
        if err:
            record["passed"] = False
            break
        # Landed on a new page, or the plan ran out after an action: look at the page and plan onwards.
        if s["kind"] == "ACT" and replans < MAX_REPLANS and (not steps or page.url != before):
            replans += 1
            steps = _replan(test, page, record["steps"], None)
    if record["passed"] and not any(x["kind"] == "ASSERT" and x["ok"] for x in record["steps"]):
        record["passed"] = False
        record["steps"].append({"kind": "ASSERT", "action": "check", "target": "", "value": "", "why": "", "ok": False,
                                "error": "the test never checked anything", "ms": 0, "screenshot": ""})
    video = page.video.path() if page.video else None
    ctx.close()
    record["video"] = str(video) if video else None
    return record


def run(base_url: str, tests: list[dict], invariants: list[dict], out_dir: Path, on_event=None, db_url: str = DB_URL_HOST) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    results = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for n, test in enumerate(tests):
            record = run_one(browser, base_url, test, out_dir / f"video-{n}")
            results.append(record)
            if on_event:
                on_event("browser", {"test": record["name"], "passed": record["passed"]})
        browser.close()
    db = [db_check(i, db_url) for i in invariants]
    return {"tests": results, "db_checks": db, "passed": all(r["passed"] for r in results) and all(d["passed"] for d in db)}


def summarize(res: dict) -> str:
    return json.dumps({"tests": [(t["name"], t["passed"]) for t in res["tests"]], "db": [(d["description"], d["passed"], d["got"]) for d in res["db_checks"]]})
