#!/usr/bin/env python3
"""Container entrypoint: resolve secrets into environment variables, then exec the command.

CCOPY_SECRETS is a comma-separated list of ENV_NAME=<secret-arn>[#json-key]. Each secret is
read once from AWS Secrets Manager; with #key the SecretString is parsed as JSON and that key
is used. Variables already set in the environment win (handy for local runs). A secret that
has no value yet (placeholder never filled in) is skipped with a warning, so optional
integrations (Slack, GitHub App, LocalStack token) stay off until configured.

    CCOPY_SECRETS="DATABASE_URL=arn:...:secret:ccopy/prod/db-AbC#url,SLACK_WEBHOOK_URL=arn:..."
    bootstrap.py uvicorn carboncopy.server.app:app --port 8080
"""
from __future__ import annotations

import json
import os
import sys


def _log(msg: str) -> None:
    print(f"bootstrap: {msg}", file=sys.stderr, flush=True)


def resolve(spec: str) -> dict[str, str]:
    pairs = []
    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue
        name, sep, ref = item.partition("=")
        if not sep or not name or not ref:
            _log(f"ignoring malformed entry {item!r}")
            continue
        if os.environ.get(name):
            continue
        arn, _, key = ref.partition("#")
        pairs.append((name, arn, key))
    if not pairs:
        return {}

    import boto3  # installed with the app; imported lazily so local runs need no AWS

    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-west-1"
    client = boto3.client("secretsmanager", region_name=region)
    cache: dict[str, str | None] = {}
    out: dict[str, str] = {}
    for name, arn, key in pairs:
        if arn not in cache:
            try:
                cache[arn] = client.get_secret_value(SecretId=arn).get("SecretString")
            except Exception as exc:  # noqa: BLE001 - any failure means "not configured"
                _log(f"{name}: secret not available ({type(exc).__name__}); leaving unset")
                cache[arn] = None
        raw = cache[arn]
        if raw is None:
            continue
        value: object = raw
        if key:
            try:
                value = json.loads(raw).get(key)
            except (ValueError, AttributeError):
                value = None
            if value is None:
                _log(f"{name}: key {key!r} missing in secret; leaving unset")
                continue
        value = str(value)
        if value.strip():
            out[name] = value
    return out


def main() -> None:
    argv = sys.argv[1:]
    if not argv:
        sys.exit("usage: bootstrap.py <command> [args...]")
    os.environ.update(resolve(os.environ.get("CCOPY_SECRETS", "")))
    os.execvp(argv[0], argv)


if __name__ == "__main__":
    main()
