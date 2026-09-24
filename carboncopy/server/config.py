"""Server settings from environment variables (see INTERFACES.md)."""
import os
from pathlib import Path

TERMINAL = frozenset({"shipped", "blocked", "rejected", "failed", "passed"})
AWAITING = frozenset({"awaiting_requirements", "awaiting_ship"})


def data_dir() -> Path:
    p = Path(os.environ.get("CCOPY_DATA_DIR", "./.ccopy-server")).expanduser().resolve()
    p.mkdir(parents=True, exist_ok=True)
    return p


def runs_dir() -> Path:
    (p := data_dir() / "runs").mkdir(exist_ok=True)
    return p


def repos_dir() -> Path:
    (p := data_dir() / "repos").mkdir(exist_ok=True)
    return p


def database_url() -> str:
    return os.environ.get("DATABASE_URL", "sqlite:///./.ccopy-server/ccopy.db")


def ai_mode() -> str:
    return os.environ.get("CCOPY_AI", "standin")


def queue_kind() -> str:
    return os.environ.get("CCOPY_QUEUE", "local")


def sqs_url() -> str | None:
    return os.environ.get("CCOPY_SQS_URL")


def api_token() -> str | None:
    return os.environ.get("CCOPY_API_TOKEN") or None


def passed_for(status: str) -> bool | None:
    if status in ("passed", "shipped", "awaiting_ship"):
        return True
    if status in ("failed", "blocked"):
        return False
    return None
