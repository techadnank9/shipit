"""Coding agent: edits code inside the workspace; the ONLY way it can execute anything is the
run_checks tool, which rebuilds the carbon copy and runs the gate checks there."""
import asyncio
import json
import os
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    create_sdk_mcp_server,
    query,
    tool,
)

from . import checks
from .copier import Copy
from . import standin
from .llm import MODEL, ai_mode

SYSTEM = """You are the coding agent inside Carbon Copy. You work in a copy of the customer's repository.
You cannot run shell commands. To execute code you call run_checks, which rebuilds a working copy of the
customer's system (app + its databases + AWS emulator) and returns test, policy and secret-scan results.
Process:
1. Read the relevant files.
2. Make the smallest correct change that satisfies every acceptance criterion.
3. If the repo has automated tests, add cases for the new behaviour in its existing style (API tests call API_URL).
   Schema changes go in a .sql file under the repo's schema folder; new files are applied after the existing ones.
4. Fix every infrastructure policy violation in Terraform.
5. Call run_checks. If anything fails, fix it and call run_checks again.
Stop when run_checks passes. Finish with a 3-5 line summary of what you changed and why."""


AGENT_TIMEOUT_S = float(os.environ.get("CCOPY_AGENT_TIMEOUT_S", "1800"))


def _layout(system_map: dict) -> str:
    pages = sorted({f"{r['path']} -> {r['file']}" for r in system_map["routes"] if r["method"] == "PAGE"})
    api = sorted({f"{r['method']} {r['path']} -> {r['file']}" for r in system_map["routes"] if r["method"] != "PAGE"})
    schema = [f"{f} ({k['engine']} {k['kind']})" for f, k in system_map.get("sql", {}).items()]
    tests = sorted({c["file"] for c in system_map["code"] if "test" in c["file"].lower()})
    return "\n".join([
        "Pages: " + ("; ".join(pages) or "none detected"),
        "API routes: " + ("; ".join(api[:60]) or "none detected"),
        "SQL files: " + ("; ".join(schema) or "none"),
        "Test files: " + ("; ".join(tests) or "none (the repo has no automated tests)"),
    ])


def _round(workspace: Path, copy: Copy, n: int) -> dict:
    copy.rebuild_app()
    results = checks.run_all(copy, workspace)
    return {"round": n, "passed": all(c.passed for c in results), "checks": checks.as_dicts(results)}


def run(workspace: Path, copy: Copy, reqs: dict, on_event=None, max_rounds: int = 5, budget_usd: float = 4.0) -> dict:
    rounds: list[dict] = []
    if ai_mode() == "standin":
        def scripted_round():
            rounds.append(_round(workspace, copy, len(rounds) + 1))
            if on_event:
                on_event("round", rounds[-1])
            return rounds[-1]
        return standin.agent_run(workspace, copy.map, scripted_round, on_event)

    @tool("run_checks", "Rebuild the carbon copy with the current code and run all gate checks", {})
    async def run_checks(_args):
        if len(rounds) >= max_rounds:
            return {"content": [{"type": "text", "text": "Round limit reached. Stop and summarize."}]}
        rounds.append(await asyncio.to_thread(_round, workspace, copy, len(rounds) + 1))
        passed = rounds[-1]["passed"]
        if on_event:
            on_event("round", rounds[-1])
        report = "\n".join(f"[{'PASS' if c['passed'] else 'FAIL'}] {c['name']}: {c['summary']}\n  " + "\n  ".join(c['details'][-12:]) for c in rounds[-1]["checks"])
        return {"content": [{"type": "text", "text": ("ALL CHECKS PASSED\n" if passed else "CHECKS FAILED\n") + report}]}

    server = create_sdk_mcp_server("copy", tools=[run_checks])
    prompt = f"""Implement these approved requirements:

{json.dumps(reqs, indent=1)}

Where things are in this repo:
{_layout(copy.map)}"""

    async def go() -> dict:
        options = ClaudeAgentOptions(
            system_prompt=SYSTEM,
            model=MODEL,
            cwd=str(workspace),
            tools=["Read", "Edit", "Write", "Glob", "Grep"],
            allowed_tools=["Read", "Edit", "Write", "Glob", "Grep", "mcp__copy__run_checks"],
            disallowed_tools=["Bash", "WebFetch", "WebSearch"],
            mcp_servers={"copy": server},
            permission_mode="acceptEdits",
            setting_sources=[],  # never load the host's personal settings, plugins or hooks
            max_turns=60,
            max_budget_usd=budget_usd,
        )
        summary, cost, edits = "", 0.0, []
        async for msg in query(prompt=prompt, options=options):
            if isinstance(msg, AssistantMessage):
                for b in msg.content:
                    if isinstance(b, TextBlock) and b.text.strip():
                        summary = b.text.strip()
                    elif isinstance(b, ToolUseBlock):
                        target = b.input.get("file_path") or b.input.get("pattern") or ""
                        if b.name in ("Edit", "Write"):
                            edits.append(target)
                        if on_event:
                            on_event("tool", {"tool": b.name, "target": str(target).replace(str(workspace) + "/", "")})
            elif isinstance(msg, ResultMessage):
                cost = msg.total_cost_usd or 0.0
        return {"summary": summary, "cost_usd": cost, "rounds": rounds, "edited_files": sorted(set(edits))}

    return asyncio.run(asyncio.wait_for(go(), timeout=AGENT_TIMEOUT_S))
