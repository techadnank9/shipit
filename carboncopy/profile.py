"""Copy profile: the recipe for booting one customer's system inside a carbon copy.

Every copy runs in "laptop mode": the app and its services share one network namespace, so
Postgres is on localhost:5432, ClickHouse on localhost:8123, AWS (LocalStack) on localhost:4566
and SMTP on localhost:1025, exactly like a developer machine. Apps written for local
development boot unchanged.

`detect` guesses a profile from the repo. `draft` asks Claude to refine that guess from the
README and code, and `revise` asks Claude to repair it after a failed boot. A person approves
the profile once when the project is onboarded; every run then boots from it.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

from .llm import ai_mode, ask_json

PG_URL = "postgresql://postgres:copy@localhost:5432/{db}"
AWS_ENV = {
    "AWS_ENDPOINT_URL": "http://localhost:4566",
    "S3_ENDPOINT": "http://localhost:4566",
    "AWS_ACCESS_KEY_ID": "copy",
    "AWS_SECRET_ACCESS_KEY": "copy",
    "AWS_DEFAULT_REGION": "us-west-1",
}
SMTP_ENV = {"SMTP_HOST": "localhost", "SMTP_PORT": "1025"}
SERVICES = ("postgres", "clickhouse", "aws", "mail")
STEP_KINDS = ("postgres", "clickhouse", "postgres_sql", "run")

SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["build", "start", "port", "health", "postgres_db", "services", "env", "env_not_needed",
                 "env_unavailable", "setup", "tests", "notes"],
    "properties": {
        "build": {"type": "string", "enum": ["dockerfile", "node"], "description": "dockerfile: the repo's own Dockerfile; node: Carbon Copy builds a Node image"},
        "start": {"type": "string", "description": "node only: command that starts the app (e.g. npm run dev); empty for dockerfile"},
        "port": {"type": "integer", "description": "port the app listens on"},
        "health": {"type": "string", "description": "path that answers once the app is up"},
        "postgres_db": {"type": "string", "description": "database name the app connects to"},
        "services": {"type": "array", "items": {"type": "string", "enum": list(SERVICES)}},
        "env": {"type": "object", "additionalProperties": {"type": "string"}, "description": "environment for the app; services are on localhost"},
        "env_not_needed": {"type": "object", "additionalProperties": {"type": "string"}, "description": "variables the code reads that must stay unset in the copy, with the reason"},
        "env_unavailable": {"type": "object", "additionalProperties": {"type": "string"}, "description": "variables for external services the copy cannot provide (third-party APIs), with the reason"},
        "setup": {
            "type": "array",
            "description": "ordered steps after services are healthy and before the app starts",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["kind", "value", "env"],
                "properties": {
                    "kind": {"type": "string", "enum": list(STEP_KINDS), "description": "postgres/clickhouse: apply a .sql file; postgres_sql: one SQL statement; run: shell command in the app image"},
                    "value": {"type": "string"},
                    "env": {"type": "object", "additionalProperties": {"type": "string"}},
                },
            },
        },
        "tests": {"type": "string", "description": "command that runs the repo's automated tests against API_URL; empty if there are none"},
        "notes": {"type": "string", "description": "anything a person should know when approving this profile"},
    },
}


def _git_first_seen(root: Path, files: list[str]) -> dict[str, float]:
    """When each file was first committed, so migrations apply in the order they were written."""
    out: dict[str, float] = {}
    for f in files:
        r = subprocess.run(["git", "log", "--diff-filter=A", "--follow", "--format=%at", "--", f], cwd=root, capture_output=True, text=True)
        stamps = [float(x) for x in r.stdout.split()] if r.returncode == 0 else []
        out[f] = min(stamps) if stamps else float("inf")
    return out


def _base_first(files: list[str], root: Path) -> list[str]:
    seen = _git_first_seen(root, files)
    base = lambda f: 0 if re.search(r"(^|/)(schema|init|postgres|clickhouse|base)\.sql$", f) else 1
    return sorted(files, key=lambda f: (base(f), seen[f], f))


def detect(root: Path, system_map: dict) -> dict:
    """A first guess from the repo alone. Good enough for simple apps; `draft` refines it."""
    sql = system_map.get("sql", {})
    schema = [f for f, k in sql.items() if k["kind"] == "schema"]
    pg = _base_first([f for f in schema if sql[f]["engine"] == "postgres"], root)
    ch = _base_first([f for f in schema if sql[f]["engine"] == "clickhouse"], root)
    aws = bool(system_map.get("aws_services"))
    declared = set(system_map.get("env_vars", []))
    services = ["postgres"] + (["clickhouse"] if ch else []) + ["aws", "mail"]
    env: dict[str, str] = {}
    steps = [{"kind": "postgres", "value": f, "env": {}} for f in pg[:1]] + [{"kind": "clickhouse", "value": f, "env": {}} for f in ch]

    if system_map.get("has_dockerfile") or not system_map.get("has_package_json"):
        db = "postgres"
        env |= {"DATABASE_URL": PG_URL.format(db=db), **AWS_ENV, **SMTP_ENV}
        env |= {k: "copy" for k in declared if k.endswith("_BUCKET") and k not in env}
        buckets = [r["attrs"].get("bucket", r["name"]) for r in system_map.get("infrastructure", []) if r["type"] == "aws_s3_bucket"]
        env |= {k: b for k in declared if k.endswith("_BUCKET") for b in buckets[:1]}
        steps += [{"kind": "postgres", "value": f, "env": {}} for f in pg[1:]]
        return {
            "build": "dockerfile", "start": "", "port": 8000, "health": "/health", "postgres_db": db,
            "services": services, "env": env, "env_not_needed": {"API_URL": "set by Carbon Copy when tests run"},
            "env_unavailable": {}, "setup": steps,
            "tests": "python -m pytest -q -p no:cacheprovider tests" if (root / "tests").is_dir() else "",
            "notes": "Detected: Dockerfile app.",
        }

    pkg = json.loads((root / "package.json").read_text())
    scripts = pkg.get("scripts", {})
    start = "npm run dev" if "dev" in scripts else "npm start"
    port = int(m[1]) if (m := re.search(r"(?:-p|--port)\s+(\d+)", scripts.get("dev", "") + " " + scripts.get("start", ""))) else 3000
    code = "\n".join((root / c["file"]).read_text(errors="replace") for c in system_map.get("code", []) if (root / c["file"]).exists())
    db = m[1] if (m := re.search(r"database:\s*[\"'](\w+)[\"']", code)) else "postgres"
    env |= {"PGHOST": "localhost", "PGPORT": "5432", "PGUSER": "postgres", "PGPASSWORD": "copy", "PGDATABASE": db}
    if "DATABASE_URL" in declared:
        env["DATABASE_URL"] = PG_URL.format(db=db)
    if aws:
        env |= AWS_ENV
    seed = "npm run seed" if "seed" in scripts else next((f"npx tsx {f}" for f in ("seed/seed.ts", "scripts/seed.ts", "prisma/seed.ts") if (root / f).exists()), "")
    if seed:
        steps.append({"kind": "run", "value": seed, "env": {}})
    steps += [{"kind": "postgres", "value": f, "env": {}} for f in pg[1:]]
    tests = "npm test" if "test" in scripts and "no test specified" not in scripts["test"] else ""
    return {
        "build": "node", "start": start, "port": port, "health": "/", "postgres_db": db,
        "services": services, "env": env, "env_not_needed": {}, "env_unavailable": {}, "setup": steps,
        "tests": tests, "notes": "Detected: Node app without a Dockerfile.",
    }


def _context(root: Path, system_map: dict) -> str:
    parts = []
    for name in ("README.md", "readme.md", "package.json", "Dockerfile", "docker-compose.yml", ".env.example"):
        if (root / name).exists():
            parts.append(f"=== {name}\n{(root / name).read_text(errors='replace')[:6000]}")
    for f, k in system_map.get("sql", {}).items():
        parts.append(f"=== {f} ({k['engine']} {k['kind']})\n{(root / f).read_text(errors='replace')[:1500]}")
    for c in system_map.get("code", []):
        if c["env_vars"] or re.search(r"(^|/)(db|database|seed|config)", c["file"]):
            parts.append(f"=== {c['file']}\n{(root / c['file']).read_text(errors='replace')[:3000]}")
    return "\n\n".join(parts)[:60000]


SYSTEM = """You write the boot recipe (copy profile) that lets Carbon Copy run a customer's app inside an
isolated copy. Everything shares localhost: Postgres localhost:5432 (user postgres, password copy),
ClickHouse localhost:8123 (default user, no password), LocalStack AWS localhost:4566, SMTP localhost:1025.
Never point the app at real hosted services. Prefer the app's local-development defaults over hosted
URLs; if setting a variable would switch the app to a hosted mode (TLS, cloud targets), put it in
env_not_needed instead. Third-party APIs the copy cannot provide go in env_unavailable.
Order setup steps so every file applies cleanly on an empty database: base schema, then seed, then
later migrations in the order they were written. Seeds must be small (seconds, not minutes): set any
size knob the seed exposes. Keep every schema .sql file in setup; saved query files are not setup."""


def draft(root: Path, system_map: dict) -> dict:
    guess = detect(root, system_map)
    if ai_mode() == "standin":
        return guess
    prompt = f"FIRST GUESS:\n{json.dumps(guess, indent=2)}\n\nREPO:\n{_context(root, system_map)}"
    return ask_json(prompt, SCHEMA, SYSTEM, budget_usd=1.0)


def revise(root: Path, system_map: dict, profile: dict, error: str) -> dict:
    if ai_mode() == "standin":
        raise RuntimeError(error)
    prompt = (f"This profile failed to boot the copy.\n\nPROFILE:\n{json.dumps(profile, indent=2)}\n\n"
              f"ERROR:\n{error[-4000:]}\n\nREPO:\n{_context(root, system_map)}\n\n"
              "Return the corrected profile. Fix the cause, not the symptom: if a file fails because of the "
              "order, reorder; if the repo itself has a bug that breaks a clean setup, add the smallest "
              "postgres_sql step that works around it and say so in notes.")
    return ask_json(prompt, SCHEMA, SYSTEM, budget_usd=1.0)


def validate(profile: dict) -> dict:
    missing = [k for k in SCHEMA["required"] if k not in profile]
    if missing:
        raise ValueError(f"profile is missing {', '.join(missing)}")
    bad = [s for s in profile["services"] if s not in SERVICES] + [s["kind"] for s in profile["setup"] if s.get("kind") not in STEP_KINDS]
    if bad:
        raise ValueError(f"unknown service or step kind: {', '.join(bad)}")
    return profile
