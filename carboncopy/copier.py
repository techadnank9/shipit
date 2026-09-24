"""Copy builder: system map -> a running carbon copy (app + Postgres + LocalStack AWS + mail catcher).

LocalStack 4.14 is the last image that runs without an account. Commercial use needs a paid
LocalStack license: set LOCALSTACK_AUTH_TOKEN and CCOPY_AWS_IMAGE=localstack/localstack:latest.
"""
import os
import subprocess
import time
from pathlib import Path

import httpx
import yaml

AWS_IMAGE = os.environ.get("CCOPY_AWS_IMAGE", "localstack/localstack:4.14")
AWS_PORT = 4566
APP_PORT = 18000
DB_PORT = 15432
MAIL_UI_PORT = 18025
DB_URL_INTERNAL = "postgresql://postgres:copy@db:5432/postgres"
DB_URL_HOST = f"postgresql://postgres:copy@127.0.0.1:{DB_PORT}/postgres"


class Copy:
    def __init__(self, workspace: Path, system_map: dict, name: str):
        self.ws = workspace.resolve()
        self.map = system_map
        self.name = name  # docker compose project name
        self.dir = self.ws / ".ccopy" / "copy"
        self.compose_file = self.dir / "docker-compose.yml"

    @property
    def app_url(self) -> str:
        return f"http://127.0.0.1:{APP_PORT}"

    def _buckets(self) -> list[str]:
        return [r["attrs"].get("bucket", r["name"]) for r in self.map["infrastructure"] if r["type"] == "aws_s3_bucket"]

    def write(self) -> Path:
        self.dir.mkdir(parents=True, exist_ok=True)
        aws_env = {
            "AWS_ENDPOINT_URL": f"http://aws:{AWS_PORT}",
            "S3_ENDPOINT": f"http://aws:{AWS_PORT}",
            "AWS_ACCESS_KEY_ID": "copy",
            "AWS_SECRET_ACCESS_KEY": "copy",
            "AWS_DEFAULT_REGION": "us-west-1",
        }
        init_sql = [f"{self.ws / f}:/docker-entrypoint-initdb.d/{i:02d}-{Path(f).name}:ro" for i, f in enumerate(self.map["schema_files"])]
        make_buckets = " && ".join(f"curl -sf -X PUT http://aws:{AWS_PORT}/{b}" for b in self._buckets()) or "true"
        compose = {
            "name": self.name,
            "services": {
                "db": {
                    "image": "postgres:17-alpine",
                    "environment": {"POSTGRES_PASSWORD": "copy"},
                    "volumes": init_sql,
                    "tmpfs": ["/var/lib/postgresql/data"],
                    "ports": [f"127.0.0.1:{DB_PORT}:5432"],
                    "healthcheck": {"test": ["CMD", "pg_isready", "-U", "postgres"], "interval": "1s", "retries": 30},
                },
                "aws": {
                    "image": AWS_IMAGE,
                    "environment": {"LOCALSTACK_AUTH_TOKEN": os.environ.get("LOCALSTACK_AUTH_TOKEN", ""), "AWS_DEFAULT_REGION": "us-west-1"},
                    "healthcheck": {"test": ["CMD", "curl", "-sf", f"http://localhost:{AWS_PORT}/_localstack/health"], "interval": "2s", "retries": 60},
                },
                "aws-init": {
                    "image": "curlimages/curl:8.16.0",
                    "command": ["sh", "-c", make_buckets],
                    "depends_on": {"aws": {"condition": "service_healthy"}},
                },
                "mail": {"image": "axllent/mailpit:v1.27", "ports": [f"127.0.0.1:{MAIL_UI_PORT}:8025"]},
                "app": {
                    "build": {"context": str(self.ws)},
                    "environment": {"DATABASE_URL": DB_URL_INTERNAL, "SMTP_HOST": "mail", "SMTP_PORT": "1025", **aws_env},
                    "ports": [f"127.0.0.1:{APP_PORT}:8000"],
                    "depends_on": {"db": {"condition": "service_healthy"}, "aws-init": {"condition": "service_completed_successfully"}},
                },
            },
        }
        self.compose_file.write_text(yaml.safe_dump(compose, sort_keys=False))
        return self.compose_file

    def _dc(self, *args: str, check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["docker", "compose", "-f", str(self.compose_file), *args],
            check=check, capture_output=capture, text=True,
        )

    def up(self, timeout: int = 180) -> None:
        self.write()
        self._dc("up", "-d", "--build", "--wait", "--wait-timeout", str(timeout), "db", "aws", "mail")
        self._dc("up", "-d", "--build", "app")
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if httpx.get(f"{self.app_url}/health", timeout=2).status_code < 500:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(1)
        logs = self._dc("logs", "--tail", "40", "app", check=False).stdout
        raise RuntimeError(f"copy app never became healthy:\n{logs}")

    def rebuild_app(self) -> None:
        """Pick up code changes: rebuild the app and reset data to the seed."""
        self.down()
        self.up()

    def run_tests(self, path: str = "tests") -> tuple[bool, str]:
        r = self._dc("run", "--rm", "--no-deps", "-e", "API_URL=http://app:8000", "app",
                     "python", "-m", "pytest", "-q", "-p", "no:cacheprovider", path, check=False)
        return r.returncode == 0, (r.stdout + r.stderr)[-6000:]

    def logs(self, service: str = "app") -> str:
        return self._dc("logs", "--tail", "80", service, check=False).stdout

    def down(self) -> None:
        if self.compose_file.exists():
            self._dc("down", "-v", "--remove-orphans", check=False)

    def fidelity(self) -> dict:
        """How closely the copy matches what the system declares it needs."""
        declared = set(self.map["env_vars"]) - {"API_URL"}
        provided = {"DATABASE_URL", "S3_ENDPOINT", "AWS_ENDPOINT_URL", "RECEIPTS_BUCKET", "SMTP_HOST"}
        emulated = {"s3", "sqs", "sns", "dynamodb", "lambda", "secretsmanager", "ses", "kms", "iam"}
        aws = set(self.map["aws_services"])
        covered = len(declared & provided) + len(aws & emulated)
        total = len(declared) + len(aws) or 1
        return {"score": round(100 * covered / total), "missing_env": sorted(declared - provided), "unemulated_aws": sorted(aws - emulated)}
