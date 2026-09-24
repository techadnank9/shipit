"""Requirements writer: plain-English request + system map -> structured, testable requirements."""
import json

from . import standin
from .llm import ai_mode, ask_json

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["title", "summary", "risk", "acceptance_criteria", "api_tests", "browser_tests", "db_invariants", "infra_changes"],
    "properties": {
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "risk": {"type": "string", "enum": ["low", "medium", "high"]},
        "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
        "api_tests": {"type": "array", "items": {"type": "string"}, "description": "pytest cases the coding agent must add"},
        "browser_tests": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "instructions"],
                "properties": {"name": {"type": "string"}, "instructions": {"type": "string"}},
            },
        },
        "db_invariants": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["description", "sql", "expect"],
                "properties": {"description": {"type": "string"}, "sql": {"type": "string"}, "expect": {"type": "string"}},
            },
        },
        "infra_changes": {"type": "array", "items": {"type": "string"}},
    },
}

SYSTEM = """You are the requirements architect inside Carbon Copy, a system that tests AI-made changes on a
working copy of production. Turn a business request into precise, testable requirements.
Rules:
- Every acceptance criterion must be checkable by an automated test.
- browser_tests: plain-English instructions a human QA tester could follow on the app's web page,
  starting from "/". Mention exact button labels you can see in the code. Include the expected outcome.
- db_invariants: SQL that returns ONE value, plus the exact expected value as a string, checked after the
  browser tests run on freshly seeded data. Only use tables and columns that exist.
- infra_changes: include fixes for every listed policy violation.
- Keep scope tight: only what the request needs."""


def write(request: str, system_map: dict, policy_violations: list[str], sources: dict[str, str]) -> dict:
    if ai_mode() == "standin":
        return standin.requirements(request, system_map)
    prompt = f"""REQUEST:
{request}

SYSTEM MAP:
{json.dumps({k: system_map[k] for k in ("routes", "tables", "env_vars", "aws_services", "infrastructure", "schema_files")}, indent=1, default=str)}

CURRENT POLICY VIOLATIONS (must be fixed before anything ships):
{json.dumps(policy_violations) if policy_violations else "none"}

SOURCE FILES:
""" + "\n".join(f"--- {name} ---\n{text}" for name, text in sources.items())
    return ask_json(prompt, SCHEMA, SYSTEM, budget_usd=1.0)
