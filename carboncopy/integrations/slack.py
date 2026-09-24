"""Slack alerts through an incoming webhook (SLACK_WEBHOOK_URL).

Sends: failed/blocked runs (failed test, failed step, what the AI tester saw), runs waiting at a
human gate ("needs your approval"), and passed/shipped runs only when SLACK_NOTIFY_SUCCESS=1.
"""
import logging
import os

import httpx

from . import GATES, headline, step_text, summarize

log = logging.getLogger("carboncopy.integrations.slack")
FAIL = {"failed", "blocked"}
OK = {"passed", "shipped"}
GATE_TEXT = {"awaiting_requirements": "Requirements are ready for review", "awaiting_ship": "The change passed every gate and is ready to ship"}


def _cut(s: str, n: int = 2900) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def _section(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": _cut(text)}}


def _button(text: str, url: str, style: str | None = None) -> dict:
    b = {"type": "button", "text": {"type": "plain_text", "text": text}, "url": url}
    if style:
        b["style"] = style
    return b


def build_message(run: dict) -> dict | None:
    """Block Kit payload for this run, or None when nothing should be sent."""
    s = summarize(run)
    status = s["status"]
    what = "Change" if s["kind"] == "change" else "Test run"
    title = s["title"] or s["id"]
    trig = f" · {s['trigger']}" if s.get("trigger") else ""
    ctx = {"type": "context", "elements": [{"type": "mrkdwn", "text": f"Run `{s['id']}` · project `{s['project_id']}`{trig}"}]}

    if status in GATES:
        state = run.get("state") or {}
        blocks = [_section(f":raised_hand: *Needs your approval* · {what}: *{title}*\n{GATE_TEXT[status]}.")]
        reqs = state.get("requirements") or {}
        if status == "awaiting_requirements" and reqs.get("acceptance_criteria"):
            blocks.append(_section("\n".join(f"• {a}" for a in reqs["acceptance_criteria"][:8])))
        if status == "awaiting_ship" and (state.get("agent") or {}).get("summary"):
            blocks.append(_section(f"*What the agent changed:* {state['agent']['summary']}"))
        blocks += [{"type": "actions", "elements": [_button("Review and approve", s["url"], "primary")]}, ctx]
        return {"text": f"Carbon Copy: {title} needs your approval", "blocks": blocks}

    if status in FAIL:
        head = headline(s)
        blocks = [_section(f":x: *{what} {'blocked' if status == 'blocked' else 'failed'}* · *{title}*\n{head}")]
        for t in [t for t in s["tests"] if not t["passed"]][:5]:
            text = f"*Failed test:* {t['name']}"
            if fs := t.get("failed_step"):
                text += f"\n*Failed at:* {step_text(fs)}"
                if fs["why"]:
                    text += f"\n*Trying to:* {fs['why']}"
                if fs["error"]:
                    text += f"\n*What the agent saw:*\n```{_cut(fs['error'], 1200)}```"
            blocks.append(_section(text))
        bad_db = [d for d in s["db_checks"] if not d["passed"]]
        if bad_db:
            blocks.append(_section("\n".join(f"*DB check failed:* {d['description']} (expected `{d['expect']}`, got `{d['got']}`)" for d in bad_db[:5])))
        bad_final = [c for c in s["final_checks"] if not c["passed"]]
        if bad_final:
            blocks.append(_section("\n".join(f"*Gate failed:* {c['name']}: {c['summary']}" for c in bad_final[:5])))
        if s["error"]:
            blocks.append(_section(f"*Error:*\n```{_cut(str(s['error']), 1500)}```"))
        buttons = [_button("Open run", s["url"], "danger")]
        if s["report_url"]:
            buttons.append(_button("Proof report", s["report_url"]))
        blocks += [{"type": "actions", "elements": buttons}, ctx]
        return {"text": f"Carbon Copy: {title} failed ({head})", "blocks": blocks}

    if status in OK and os.environ.get("SLACK_NOTIFY_SUCCESS") == "1":
        verb = "shipped" if status == "shipped" else "passed"
        return {"text": f"Carbon Copy: {title} {verb}", "blocks": [
            _section(f":white_check_mark: *{what} {verb}* · {title} · {headline(s)} · <{s['url']}|open run>"), ctx]}
    return None


def notify(run: dict) -> None:
    url = os.environ.get("SLACK_WEBHOOK_URL")
    if not url:
        log.info("slack: SLACK_WEBHOOK_URL not set; skipping run %s", run.get("id"))
        return
    msg = build_message(run)
    if not msg:
        return
    r = httpx.post(url, json=msg, timeout=10)
    if r.status_code >= 400:
        raise RuntimeError(f"Slack webhook -> {r.status_code}: {r.text[:200]}")
