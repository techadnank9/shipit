"""Append-only, hash-chained audit log. Editing any past line breaks every hash after it."""
import hashlib
import json
import time
from pathlib import Path


class AuditLog:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def _last_hash(self) -> str:
        if not self.path.exists():
            return "0" * 64
        lines = self.path.read_text().strip().splitlines()
        return json.loads(lines[-1])["hash"] if lines else "0" * 64

    def record(self, event: str, **data) -> dict:
        entry = {"ts": time.time(), "event": event, "data": data, "prev": self._last_hash()}
        body = json.dumps(entry, sort_keys=True, default=str)
        entry["hash"] = hashlib.sha256(body.encode()).hexdigest()
        with self.path.open("a") as f:
            f.write(json.dumps(entry, default=str) + "\n")
        return entry

    def entries(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(l) for l in self.path.read_text().splitlines() if l.strip()]

    def verify(self) -> bool:
        prev = "0" * 64
        for e in self.entries():
            stored = e.pop("hash")
            if e["prev"] != prev:
                return False
            if hashlib.sha256(json.dumps(e, sort_keys=True, default=str).encode()).hexdigest() != stored:
                return False
            prev = stored
        return True
