"""Coding agent: edits code inside the workspace; the ONLY way it can execute anything is the
run_checks tool, which rebuilds the carbon copy and runs the gate checks there."""
import asyncio
import json
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
from .llm import MODEL

SYSTEM = """You are the coding agent inside Carbon Copy. You work in a copy of the customer's repository.
You cannot run shell commands. To execute code you call run_checks, which rebuilds a working copy of the
customer's system (app + Postgres + AWS emulator) and returns test, policy and secret-scan results.
Process:
1. Read the relevant files.
2. Make the smallest correct change that satisfies every acceptance criterion.
3. Add the requested pytest cases to tests/ (they call the running API at API_URL).
4. Fix every infrastructure policy violation in Terraform.
5. Call run_checks. If anything fails, fix it and call run_checks again.
Stop when run_checks passes. Finish with a 3-5 line summary of what you changed and why."""


def run(workspace: Path, copy: Copy, reqs: dict, on_event=None, max_rounds: int = 5, budget_usd: float = 4.0) -> dict:
    rounds: list[dict] = []

    @tool("run_checks", "Rebuild the carbon copy with the current code and run all gate checks", {})
    async def run_checks(_args):
        if len(rounds) >= max_rounds:
            return {"content": [{"type": "text", "text": "Round limit reached. Stop and summarize."}]}
        await asyncio.to_thread(copy.rebuild_app)
        results = await asyncio.to_thread(checks.run_all, copy, workspace)
        passed = all(c.passed for c in results)
        rounds.append({"round": len(rounds) + 1, "passed": passed, "checks": checks.as_dicts(results)})
        if on_event:
            on_event("round", rounds[-1])
        report = "\n".join(f"[{'PASS' if c.passed else 'FAIL'}] {c.name}: {c.summary}\n  " + "\n  ".join(c.details[-12:]) for c in results)
        return {"content": [{"type": "text", "text": ("ALL CHECKS PASSED\n" if passed else "CHECKS FAILED\n") + report}]}

    server = create_sdk_mcp_server("copy", tools=[run_checks])
    prompt = f"""Implement these approved requirements:

{json.dumps(reqs, indent=1)}

The web page is served from app/main.py (PAGE). Tests live in tests/."""

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
            setting_sources=None,
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

    return asyncio.run(go())
