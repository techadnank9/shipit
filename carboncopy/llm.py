"""One door for every Claude call.

Locally this uses the logged-in Claude Code CLI. On AWS set CLAUDE_CODE_USE_BEDROCK=1 and
AWS_REGION=us-west-1 and the same code calls Claude through Amazon Bedrock.
"""
import asyncio
import concurrent.futures
import json
import os
from typing import Any

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock, query

MODEL = os.environ.get("CCOPY_MODEL", "claude-sonnet-5")


def ai_mode() -> str:
    """`standin` (scripted, default until Claude is connected) or `claude`."""
    return os.environ.get("CCOPY_AI", "standin").lower()


def ask_json(prompt: str, schema: dict[str, Any], system: str, budget_usd: float = 1.0) -> dict:
    """Single model call that must return JSON matching `schema`."""

    async def run() -> dict:
        options = ClaudeAgentOptions(
            system_prompt=system,
            model=MODEL,
            tools=[],
            max_turns=2,
            max_budget_usd=budget_usd,
            setting_sources=None,
            output_format={"type": "json_schema", "schema": schema},
        )
        text, structured = "", None
        async for msg in query(prompt=prompt, options=options):
            if isinstance(msg, AssistantMessage):
                text += "".join(b.text for b in msg.content if isinstance(b, TextBlock))
            elif isinstance(msg, ResultMessage):
                structured = getattr(msg, "structured_output", None)
                text = text or (msg.result or "")
        if structured:
            return structured
        return _extract_json(text)

    return _run_sync(run())


def _run_sync(coro: Any) -> Any:
    """asyncio.run, also from threads that already run a loop (Playwright's sync API does)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _extract_json(text: str) -> dict:
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"model did not return JSON: {text[:300]}")
    return json.loads(text[start : end + 1])
