"""QA sweep: go through every page like a manual tester.

For each page: open it in a real browser, record console errors, uncaught exceptions and failed
requests, take a screenshot, have the model review what a user sees there, write plain-English
tests for the page's main flows, and run them. Pages come from the system map (static routes)
plus a crawl of same-origin links, which also supplies real URLs for routes with parameters
(/people/:id -> /people/41).
"""
from __future__ import annotations

import base64
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from playwright.sync_api import sync_playwright

from .browser import run_one, settle
from .llm import ai_mode, ask_json

MAX_PAGES = 40
PER_ROUTE = 2  # concrete examples per parameterised route

REVIEW = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "issues", "tests"],
    "properties": {
        "summary": {"type": "string", "description": "one sentence: what this page is for"},
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["severity", "title", "detail"],
                "properties": {
                    "severity": {"type": "string", "enum": ["high", "medium", "low"]},
                    "title": {"type": "string"},
                    "detail": {"type": "string"},
                },
            },
        },
        "tests": {
            "type": "array",
            "maxItems": 3,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "instructions"],
                "properties": {"name": {"type": "string"}, "instructions": {"type": "string"}},
            },
        },
    },
}

SYSTEM = """You are a senior manual QA tester going through a web app page by page. For the page you are
given (its visible text, its controls, and what the browser recorded), report only real problems a user
or engineer would care about: errors in the console or failed requests, raw error text, "undefined",
"NaN" or "null" on screen, broken or empty sections that should have content, controls that cannot work.
Do not report style opinions. Then write up to 3 plain-English tests of the main things a user does on
this page. Each test starts with "Open <path>.", describes the actions with visible labels, and ends with
"Expected: ..." naming what must be visible. This is a disposable copy: tests may create data."""

JS_CONTROLS = """() => {
  const label = el => (el.getAttribute('aria-label') || el.innerText || el.getAttribute('placeholder') || el.name || '').trim().slice(0, 60);
  const q = s => Array.from(document.querySelectorAll(s)).filter(e => e.offsetParent !== null);
  return {
    buttons: q('button, [role=button], input[type=submit]').map(label).filter(Boolean).slice(0, 40),
    inputs: q('input:not([type=hidden]), textarea, select').map(label).filter(Boolean).slice(0, 40),
    links: q('a[href]').map(a => a.getAttribute('href')).slice(0, 200),
  };
}"""


def _pattern(path: str) -> re.Pattern:
    return re.compile("^" + re.sub(r":[^/]+", "[^/]+", re.escape(path).replace("\\:", ":")) + "$")


def _static_pages(routes: list[dict]) -> list[str]:
    pages = [r["path"] for r in routes if r["method"] in ("PAGE", "GET") and ":" not in r["path"] and not r["path"].startswith("/api")]
    return list(dict.fromkeys(pages or ["/"]))


def _inspect(browser: Any, base_url: str, path: str) -> dict:
    ctx = browser.new_context(viewport={"width": 1280, "height": 800})
    page = ctx.new_page()
    console, crashes, failed = [], [], []
    origin = urlparse(base_url).netloc
    page.on("console", lambda m: console.append(m.text[:300]) if m.type == "error" else None)
    page.on("pageerror", lambda e: crashes.append(str(e)[:300]))
    page.on("response", lambda r: failed.append(f"{r.status} {r.request.method} {urlparse(r.url).path}")
            if r.status >= 400 and urlparse(r.url).netloc == origin else None)
    page.on("requestfailed", lambda r: failed.append(f"failed {r.method} {urlparse(r.url).path}")
            if urlparse(r.url).netloc == origin else None)
    t0 = time.time()
    status: int | str
    try:
        resp = page.goto(base_url + path, timeout=60000)
        status = resp.status if resp else "no response"
        settle(page)
        page.wait_for_timeout(800)  # let client-side data land
    except Exception as e:  # noqa: BLE001
        status = f"error: {str(e).splitlines()[0][:200]}"
    obs = {
        "path": path, "final_path": urlparse(page.url).path, "status": status, "ms": int((time.time() - t0) * 1000),
        "console_errors": list(dict.fromkeys(console))[:15], "page_errors": list(dict.fromkeys(crashes))[:15],
        "failed_requests": list(dict.fromkeys(failed))[:15],
    }
    try:
        obs["text"] = page.inner_text("body")[:6000]
        obs["controls"] = page.evaluate(JS_CONTROLS)
        obs["screenshot"] = base64.b64encode(page.screenshot(type="jpeg", quality=70)).decode()
    except Exception:  # noqa: BLE001
        obs |= {"text": "", "controls": {"buttons": [], "inputs": [], "links": []}, "screenshot": ""}
    ctx.close()
    return obs


def _review(obs: dict) -> dict:
    if ai_mode() == "standin":
        issues = [{"severity": "high", "title": "Page failed to load", "detail": str(obs["status"])}] if not isinstance(obs["status"], int) or obs["status"] >= 500 else []
        issues += [{"severity": "medium", "title": "Console error", "detail": e} for e in obs["console_errors"] + obs["page_errors"]]
        issues += [{"severity": "medium", "title": "Failed request", "detail": f} for f in obs["failed_requests"]]
        return {"summary": "", "issues": issues, "tests": []}
    facts = {k: obs[k] for k in ("path", "final_path", "status", "console_errors", "page_errors", "failed_requests")}
    prompt = (f"PAGE: {obs['path']}\nBROWSER RECORDED: {facts}\nCONTROLS: {obs['controls'] | {'links': obs['controls']['links'][:40]}}\n\n"
              f"VISIBLE TEXT:\n{obs['text']}")
    return ask_json(prompt, REVIEW, SYSTEM, budget_usd=0.5)


def sweep(base_url: str, routes: list[dict], out_dir: Path, on_event=None, run_tests: bool = True, max_pages: int = MAX_PAGES) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    queue = _static_pages(routes)
    param_routes = {r["path"]: _pattern(r["path"]) for r in routes if r["method"] in ("PAGE", "GET") and ":" in r["path"] and not r["path"].startswith("/api")}
    examples: dict[str, int] = {}
    seen, pages = set(), []
    origin = urlparse(base_url).netloc
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        while queue and len(pages) < max_pages:
            path = queue.pop(0)
            if path in seen:
                continue
            seen.add(path)
            obs = _inspect(browser, base_url, path)
            for href in obs["controls"]["links"]:  # crawl: real URLs for /people/:id and friends
                u = urlparse(urljoin(base_url + path, href))
                if u.netloc != origin or u.path in seen or u.path.startswith("/api"):
                    continue
                for route, rx in param_routes.items():
                    if rx.match(u.path) and examples.get(route, 0) < PER_ROUTE:
                        examples[route] = examples.get(route, 0) + 1
                        queue.append(u.path)
            try:
                review = _review(obs)
            except Exception as e:  # noqa: BLE001 - a failed review is reported, the sweep continues
                review = {"summary": "", "issues": [{"severity": "low", "title": "Review failed", "detail": str(e)[:300]}], "tests": []}
            record = {**{k: v for k, v in obs.items() if k != "controls"}, **review, "results": []}
            if run_tests:
                for n, t in enumerate(review["tests"]):
                    record["results"].append(run_one(browser, base_url, {**t, "start": path}, out_dir / f"video-{len(pages)}-{n}"))
            pages.append(record)
            if on_event:
                on_event("page", {"path": path, "issues": len(review["issues"]),
                                  "tests": [r["passed"] for r in record["results"]]})
        browser.close()
    issues = [i for p in pages for i in p["issues"]]
    results = [r for p in pages for r in p["results"]]
    return {
        "pages": pages,
        "unvisited_routes": sorted(set(param_routes) - set(examples)),
        "totals": {
            "pages": len(pages),
            "issues": {s: sum(1 for i in issues if i["severity"] == s) for s in ("high", "medium", "low")},
            "tests": len(results), "tests_passed": sum(1 for r in results if r["passed"]),
        },
    }
