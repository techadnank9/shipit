"""Gate checks: tests on the copy, infrastructure policy (OPA), IaC scan (Checkov), secret scan."""
import json
import re
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .copier import Copy
from .reader import scan

POLICY_DIR = Path(__file__).resolve().parent.parent / "policies"
SECRET_PATTERNS = {
    "AWS access key": re.compile(r"AKIA[0-9A-Z]{16}"),
    "Private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "Anthropic key": re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    "Generic secret": re.compile(r"(?i)(password|secret|token)\s*=\s*['\"][^'\"]{12,}['\"]"),
}


@dataclass
class Check:
    name: str
    passed: bool
    summary: str
    details: list[str] = field(default_factory=list)


def tests_on_copy(copy: Copy) -> Check:
    ok, out = copy.run_tests()
    tail = [l for l in out.splitlines() if l.strip()][-25:]
    summary = next((l for l in reversed(tail) if "passed" in l or "failed" in l or "error" in l), "no test output")
    return Check("Tests on the copy", ok, summary.strip(" ="), tail)


def policy(workspace: Path) -> Check:
    resources = scan(workspace)["infrastructure"]
    r = subprocess.run(
        ["opa", "eval", "--format", "json", "--stdin-input", "-d", str(POLICY_DIR), "data.carboncopy.aws.deny"],
        input=json.dumps({"resources": resources}), capture_output=True, text=True,
    )
    if r.returncode != 0:
        return Check("Infrastructure policy (OPA)", False, "policy engine error", [r.stderr[-500:]])
    result = json.loads(r.stdout)["result"][0]["expressions"][0]["value"]
    return Check("Infrastructure policy (OPA)", not result,
                 f"{len(result)} violation(s)" if result else f"{len(resources)} resources, 0 violations", sorted(result))


def checkov(workspace: Path) -> Check:
    tf_dirs = sorted({p.parent for p in workspace.rglob("*.tf") if ".ccopy" not in p.parts})
    if not tf_dirs:
        return Check("IaC scan (Checkov)", True, "no Terraform found")
    findings = []
    for d in tf_dirs:
        r = subprocess.run(["checkov", "-d", str(d), "-o", "json", "--quiet", "--compact"], capture_output=True, text=True)
        try:
            data = json.loads(r.stdout or "{}")
        except json.JSONDecodeError:
            continue
        for rep in data if isinstance(data, list) else [data]:
            for f in rep.get("results", {}).get("failed_checks", []):
                findings.append(f"{f['check_id']} {f['check_name']} ({f['resource']})")
    # Checkov is advisory in the MVP: it reports, OPA blocks.
    return Check("IaC scan (Checkov, advisory)", True, f"{len(findings)} advisory finding(s)", findings[:15])


def secrets(workspace: Path) -> Check:
    hits = []
    for p in workspace.rglob("*"):
        if p.is_file() and not {".git", ".ccopy", ".venv", "node_modules"} & set(p.parts) and p.stat().st_size < 500_000:
            try:
                text = p.read_text()
            except (UnicodeDecodeError, OSError):
                continue
            for label, rx in SECRET_PATTERNS.items():
                if rx.search(text):
                    hits.append(f"{p.relative_to(workspace)}: {label}")
    return Check("Secret scan", not hits, f"{len(hits)} secret(s) found" if hits else "clean", hits)


def run_all(copy: Copy, workspace: Path) -> list[Check]:
    return [tests_on_copy(copy), policy(workspace), checkov(workspace), secrets(workspace)]


def as_dicts(checks: list[Check]) -> list[dict]:
    return [asdict(c) for c in checks]
