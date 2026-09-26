"""Copy builder: system map + copy profile -> a running carbon copy of the customer's system.

Laptop mode: an idle `net` container owns one network namespace and every service joins it, so
the app finds Postgres on localhost:5432, ClickHouse on localhost:8123, LocalStack AWS on
localhost:4566 and SMTP on localhost:1025, as on a developer machine. `net` publishes the few
ports Carbon Copy itself needs (app, Postgres for DB checks, mail UI) on the worker's 127.0.0.1.

LocalStack 4.14 is the last image that runs without an account. Commercial use needs a paid
LocalStack license: set LOCALSTACK_AUTH_TOKEN and CCOPY_AWS_IMAGE=localstack/localstack:latest.
"""
import os
import subprocess
import time
from pathlib import Path

import httpx
import yaml

from . import profile as profiles

AWS_IMAGE = os.environ.get("CCOPY_AWS_IMAGE", "localstack/localstack:4.14")
CLICKHOUSE_IMAGE = os.environ.get("CCOPY_CLICKHOUSE_IMAGE", "clickhouse/clickhouse-server:25.8")
NODE_IMAGE = os.environ.get("CCOPY_NODE_IMAGE", "node:22-bookworm-slim")
APP_PORT = 18000
DB_PORT = 15432
MAIL_UI_PORT = 18025
DB_URL_HOST = f"postgresql://postgres:copy@127.0.0.1:{DB_PORT}/postgres"

NODE_DOCKERFILE = f"""FROM {NODE_IMAGE}
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \\
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY package*.json ./
RUN if [ -f package-lock.json ]; then npm ci --no-audit --no-fund; else npm install --no-audit --no-fund; fi
COPY . .
ENV NEXT_TELEMETRY_DISABLED=1 HOSTNAME=0.0.0.0
"""


class Copy:
    def __init__(self, workspace: Path, system_map: dict, name: str, profile: dict | None = None):
        self.ws = workspace.resolve()
        self.map = system_map
        self.name = name  # docker compose project name
        self.profile = profile or profiles.detect(self.ws, system_map)
        self.dir = self.ws / ".ccopy" / "copy"
        self.compose_file = self.dir / "docker-compose.yml"

    @property
    def app_url(self) -> str:
        return f"http://127.0.0.1:{APP_PORT}"

    @property
    def db_url_host(self) -> str:
        return f"postgresql://postgres:copy@127.0.0.1:{DB_PORT}/{self.profile['postgres_db']}"

    def _has(self, service: str) -> bool:
        return service in self.profile["services"]

    def _buckets(self) -> list[str]:
        return [r["attrs"].get("bucket", r["name"]) for r in self.map["infrastructure"] if r["type"] == "aws_s3_bucket"]

    def _infra(self) -> list[str]:
        return [s for s, on in (("db", self._has("postgres")), ("clickhouse", self._has("clickhouse")),
                                ("aws", self._has("aws")), ("mail", self._has("mail"))) if on]

    def write(self) -> Path:
        self.dir.mkdir(parents=True, exist_ok=True)
        p = self.profile
        joined = {"network_mode": "service:net"}
        ports = [f"127.0.0.1:{APP_PORT}:{p['port']}", f"127.0.0.1:{DB_PORT}:5432", f"127.0.0.1:{MAIL_UI_PORT}:8025"]
        services: dict = {"net": {"image": "alpine:3.22", "command": ["sleep", "infinity"], "ports": ports, "init": True}}
        if self._has("postgres"):
            services["db"] = {
                "image": "postgres:17-alpine", **joined,
                "environment": {"POSTGRES_PASSWORD": "copy", "POSTGRES_DB": p["postgres_db"]},
                "tmpfs": ["/var/lib/postgresql/data"],
                "healthcheck": {"test": ["CMD", "pg_isready", "-U", "postgres", "-h", "localhost"], "interval": "1s", "retries": 60},
            }
        if self._has("clickhouse"):
            services["clickhouse"] = {
                "image": CLICKHOUSE_IMAGE, **joined,
                "environment": {"CLICKHOUSE_SKIP_USER_SETUP": "1"},
                "tmpfs": ["/var/lib/clickhouse"],
                "healthcheck": {"test": ["CMD", "wget", "-qO-", "http://localhost:8123/ping"], "interval": "1s", "retries": 90},
            }
        if self._has("aws"):
            services["aws"] = {
                "image": AWS_IMAGE, **joined,
                "environment": {"LOCALSTACK_AUTH_TOKEN": os.environ.get("LOCALSTACK_AUTH_TOKEN", ""), "AWS_DEFAULT_REGION": "us-west-1"},
                "healthcheck": {"test": ["CMD", "curl", "-sf", "http://localhost:4566/_localstack/health"], "interval": "2s", "retries": 60},
            }
        if self._has("mail"):
            services["mail"] = {"image": "axllent/mailpit:v1.27", **joined}
        if p["build"] == "node":
            (self.dir / "Dockerfile.node").write_text(NODE_DOCKERFILE)
            (self.dir / "Dockerfile.node.dockerignore").write_text(".ccopy\n.git\nnode_modules\n.next\n.env*\n")
            build = {"context": str(self.ws), "dockerfile": str(self.dir / "Dockerfile.node")}
        else:
            build = {"context": str(self.ws)}
        app = {"build": build, **joined, "environment": dict(p["env"]),
               "depends_on": {s: {"condition": "service_healthy"} for s in self._infra() if s != "mail"}}
        if p.get("start"):
            app["command"] = ["sh", "-c", p["start"]]
        services["app"] = app
        compose = {"name": self.name, "services": services}
        self.compose_file.write_text(yaml.safe_dump(compose, sort_keys=False))
        return self.compose_file

    def _dc(self, *args: str, check: bool = True, capture: bool = True, stdin: str | None = None, timeout: int | None = None) -> subprocess.CompletedProcess:
        r = subprocess.run(["docker", "compose", "-f", str(self.compose_file), *args], capture_output=capture, text=True, input=stdin, timeout=timeout)
        if check and r.returncode != 0:
            raise RuntimeError(f"docker compose {args[0]} failed: {(r.stderr or r.stdout or '').strip()[-1500:]}")
        return r

    def _step(self, step: dict) -> None:
        kind, value = step["kind"], step["value"]
        db = self.profile["postgres_db"]
        if kind in ("postgres", "clickhouse"):
            path = self.ws / value
            if not path.exists():
                raise RuntimeError(f"setup step {kind} {value}: file not found")
            sql = path.read_text()
            if kind == "postgres":
                r = self._dc("exec", "-T", "db", "psql", "-v", "ON_ERROR_STOP=1", "-q", "-U", "postgres", "-d", db, check=False, stdin=sql)
            else:
                r = self._dc("exec", "-T", "clickhouse", "clickhouse-client", "--multiquery", check=False, stdin=sql)
        elif kind == "postgres_sql":
            r = self._dc("exec", "-T", "db", "psql", "-v", "ON_ERROR_STOP=1", "-q", "-U", "postgres", "-d", db, "-c", value, check=False)
        else:
            env = [a for k, v in step.get("env", {}).items() for a in ("-e", f"{k}={v}")]
            r = self._dc("run", "--rm", "--no-deps", *env, "app", "sh", "-c", value, check=False, timeout=900)
        if r.returncode != 0:
            raise RuntimeError(f"setup step failed: {kind} {value}\n{(r.stderr or r.stdout or '').strip()[-2500:]}")

    def _new_schema_files(self) -> list[dict]:
        """Schema files the agent added: applied after the recipe so its migrations take effect."""
        listed = {s["value"] for s in self.profile["setup"] if s["kind"] in ("postgres", "clickhouse")}
        baseline = set(self.profile.get("baseline_sql", listed))
        out = []
        for f, k in self.map.get("sql", {}).items():
            if k["kind"] == "schema" and f not in listed and f not in baseline:
                engine = k["engine"] if self._has(k["engine"]) or k["engine"] == "postgres" else "postgres"
                out.append({"kind": engine, "value": f, "env": {}})
        return out

    def up(self, timeout: int = 300, on_step=None) -> None:
        self.write()
        infra = ["net", *self._infra()]
        self._dc("up", "-d", "--wait", "--wait-timeout", str(timeout), *infra)
        if self._has("aws") and self._buckets():
            for b in self._buckets():
                self._dc("exec", "-T", "aws", "awslocal", "s3", "mb", f"s3://{b}", check=False)
        self._dc("build", "app")
        for step in [*self.profile["setup"], *self._new_schema_files()]:
            if on_step:
                on_step(step)
            self._step(step)
        self._dc("up", "-d", "--no-deps", "app")
        deadline = time.time() + timeout
        health = self.app_url + self.profile.get("health", "/")
        while time.time() < deadline:
            try:
                if httpx.get(health, timeout=10).status_code < 500:
                    return
            except httpx.HTTPError:
                pass
            if self._dc("ps", "--status", "exited", "-q", "app", check=False).stdout.strip():
                break
            time.sleep(2)
        raise RuntimeError(f"copy app never became healthy at {health}:\n{self.logs()}")

    def rebuild_app(self) -> None:
        """Pick up code changes: rebuild the app and reset data to the seed."""
        self.down()
        self.up()

    def run_tests(self) -> tuple[bool, str]:
        cmd = self.profile.get("tests", "")
        if not cmd:
            return True, "no automated tests in this repo (advisory)"
        r = self._dc("run", "--rm", "--no-deps", "-e", f"API_URL=http://localhost:{self.profile['port']}", "app",
                     "sh", "-c", cmd, check=False, timeout=900)
        return r.returncode == 0, (r.stdout + r.stderr)[-6000:]

    def logs(self, service: str = "app") -> str:
        return self._dc("logs", "--tail", "80", service, check=False).stdout

    def down(self) -> None:
        if self.compose_file.exists():
            self._dc("down", "-v", "--remove-orphans", check=False)

    def fidelity(self) -> dict:
        """How closely the copy matches what the system declares it needs."""
        p = self.profile
        declared = set(self.map["env_vars"])
        handled = set(p["env"]) | set(p["env_not_needed"]) | {"API_URL"}
        emulated = {"s3", "sqs", "sns", "dynamodb", "lambda", "secretsmanager", "ses", "kms", "iam"}
        aws = set(self.map["aws_services"])
        covered = len(declared & handled) + len(aws & emulated)
        total = len(declared) + len(aws) or 1
        return {"score": round(100 * covered / total), "missing_env": sorted(declared - handled),
                "unavailable": dict(p["env_unavailable"]), "unemulated_aws": sorted(aws - emulated)}
