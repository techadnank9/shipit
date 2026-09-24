"""Run engine: change runs (Avcel-style loop with two human gates) and test runs (saved plain-English
tests on a fresh copy). State lives in <run_dir>/state.json so a run survives process restarts and
can pause for hours at a gate. See INTERFACES.md."""
import hashlib
import json
import shutil
import subprocess
import time
import traceback
import uuid
from enum import StrEnum
from pathlib import Path
from typing import Callable

from . import agent, browser, checks, report, requirements
from .audit import AuditLog
from .copier import Copy
from .reader import scan

Emit = Callable[[str, dict], None]
IGNORE = shutil.ignore_patterns(".ccopy", ".git", ".venv", "__pycache__", "node_modules", ".pytest_cache")


class RunStatus(StrEnum):
    QUEUED = "queued"
    MAPPING = "mapping"
    COPYING = "copying"
    WRITING_REQUIREMENTS = "writing_requirements"
    AWAITING_REQUIREMENTS = "awaiting_requirements"
    BASELINE_TESTING = "baseline_testing"
    AGENT_WORKING = "agent_working"
    FINAL_GATE = "final_gate"
    AWAITING_SHIP = "awaiting_ship"
    SHIPPED = "shipped"
    BLOCKED = "blocked"
    REJECTED = "rejected"
    FAILED = "failed"
    TESTING = "testing"
    PASSED = "passed"


TERMINAL = {RunStatus.SHIPPED, RunStatus.BLOCKED, RunStatus.REJECTED, RunStatus.FAILED, RunStatus.PASSED}


def _new_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]


def _sources(ws: Path, system_map: dict) -> dict[str, str]:
    files = [c["file"] for c in system_map["code"]] + [r["file"] for r in system_map["infrastructure"]] + system_map["schema_files"]
    return {f: (ws / f).read_text() for f in dict.fromkeys(files) if (ws / f).exists()}


class _Run:
    kind = ""

    def __init__(self, run_dir: Path, state: dict, emit: Emit | None = None):
        self.dir = run_dir
        self.state = state
        self.emit_fn = emit
        self.audit = AuditLog(run_dir / "audit.jsonl")

    # --- state -------------------------------------------------------------------------------
    @property
    def id(self) -> str:
        return self.state["id"]

    @property
    def status(self) -> RunStatus:
        return RunStatus(self.state["status"])

    @property
    def workspace(self) -> Path:
        return self.dir / "workspace"

    def _save(self) -> None:
        self.state["updated_at"] = time.time()
        tmp = self.dir / "state.json.tmp"
        tmp.write_text(json.dumps(self.state, indent=1, default=str))
        tmp.replace(self.dir / "state.json")

    def _emit(self, event: str, data: dict) -> None:
        if self.emit_fn:
            try:
                self.emit_fn(event, data)
            except Exception:  # noqa: BLE001 - a broken listener must never break a run
                pass

    def _set(self, status: RunStatus, **fields) -> None:
        self.state.update(fields, status=str(status))
        self._save()
        self._emit("status", {"status": str(status)})

    def log(self, message: str) -> None:
        self._emit("log", {"message": message})

    def _log_db(self, res: dict) -> None:
        for d in res["db_checks"]:
            self.log(f"DB check {'PASS' if d['passed'] else 'FAIL'} {d['description']} (expected {d['expect']}, got {d['got']})")

    @classmethod
    def _create(cls, repo: Path, runs_dir: Path, emit: Emit | None, **extra) -> "_Run":
        repo = repo.resolve()
        run_id = _new_id()
        run_dir = (runs_dir / run_id).resolve()
        shutil.copytree(repo, run_dir / "workspace", ignore=IGNORE)
        ws = run_dir / "workspace"
        git = ["git", "-c", "user.email=ccopy@local", "-c", "user.name=ccopy"]
        subprocess.run(["git", "init", "-q"], cwd=ws, check=True)
        subprocess.run(["git", "add", "-A"], cwd=ws, check=True)
        subprocess.run([*git, "commit", "-qm", "baseline", "--allow-empty"], cwd=ws, check=True)
        now = time.time()
        state = {
            "id": run_id, "kind": cls.kind, "status": str(RunStatus.QUEUED), "repo": str(repo),
            "request": extra.pop("request", ""), "created_at": now, "updated_at": now,
            "requirements": None, "fidelity": None, "before": None, "after": None, "final_checks": None,
            "rounds": [], "agent": None, "error": None, "report": None, "patch": None, **extra,
        }
        run = cls(run_dir, state, emit)
        run._save()
        run.audit.record("run.created", who="carboncopy", kind=cls.kind, repo=str(repo), request=state["request"])
        return run

    @classmethod
    def load(cls, run_dir: Path, emit: Emit | None = None):
        state = json.loads((run_dir / "state.json").read_text())
        klass = {"change": ChangeRun, "test": TestRun}[state["kind"]]
        return klass(run_dir, state, emit)

    def _copy(self, system_map: dict) -> Copy:
        return Copy(self.workspace, system_map, f"ccopy-{self.id.lower()}")

    def _fail(self, exc: BaseException) -> None:
        self.audit.record("run.failed", who="carboncopy", error=str(exc)[:500])
        self._set(RunStatus.FAILED, error=f"{type(exc).__name__}: {exc}"[:2000])
        (self.dir / "error.log").write_text(traceback.format_exc())

    def _write_report(self) -> None:
        entries = self.audit.entries()
        for e in entries:
            e["who"] = e["data"].get("who", "")
        s = self.state
        reqs = s["requirements"] or {"title": s.get("title") or "Test run", "summary": "", "risk": "n/a", "acceptance_criteria": []}
        report.write(
            self.dir / "report.html", run_id=self.id, when=time.strftime("%Y-%m-%d %H:%M %Z"),
            request=s["request"] or reqs["title"], reqs=reqs, shipped=s["status"] in ("shipped", "passed"),
            before=s["before"], after=s["after"], final_checks=s["final_checks"] or [], rounds=s["rounds"],
            agent=s["agent"] or {"summary": "No code change in a test run.", "edited_files": []},
            diff=(self.dir / "change.patch").read_text() if (self.dir / "change.patch").exists() else "",
            fidelity=s["fidelity"] or {"score": 0, "missing_env": []}, cost=(s["agent"] or {}).get("cost_usd", 0.0),
            duration=f"{int(s['updated_at'] - s['created_at'])}s", audit=entries, audit_ok=self.audit.verify(),
        )
        self.state["report"] = "report.html"
        self._save()


class ChangeRun(_Run):
    kind = "change"

    @classmethod
    def create(cls, repo: Path, request: str, runs_dir: Path, emit: Emit | None = None) -> "ChangeRun":
        return cls._create(repo, runs_dir, emit, request=request)

    def prepare(self) -> None:
        """Map → copy → requirements. Ends at AWAITING_REQUIREMENTS."""
        copy = None
        try:
            self._set(RunStatus.MAPPING)
            system_map = scan(self.workspace)
            self.log(f"{len(system_map['routes'])} routes · tables {system_map['tables']} · AWS {system_map['aws_services']}")
            self.audit.record("system.mapped", who="reader", routes=len(system_map["routes"]), tables=system_map["tables"])

            self._set(RunStatus.COPYING)
            copy = self._copy(system_map)
            copy.up()
            fid = copy.fidelity()
            self.audit.record("copy.up", who="copier", fidelity=fid)
            self.log(f"carbon copy running · fidelity {fid['score']}%")

            self._set(RunStatus.WRITING_REQUIREMENTS, fidelity=fid)
            violations = checks.policy(self.workspace).details
            reqs = requirements.write(self.state["request"], system_map, violations, _sources(self.workspace, system_map))
            (self.dir / "requirements.json").write_text(json.dumps(reqs, indent=2))
            self.audit.record("requirements.written", who="ai", title=reqs["title"], risk=reqs["risk"])
            self._set(RunStatus.AWAITING_REQUIREMENTS, requirements=reqs, title=reqs["title"])
        except Exception as e:  # noqa: BLE001
            self._fail(e)
        finally:
            if copy:
                copy.down()  # free the machine while a human reviews

    def approve_requirements(self, who: str) -> None:
        self._require(RunStatus.AWAITING_REQUIREMENTS)
        self.audit.record("requirements.approved", who=who)
        self._set(RunStatus.BASELINE_TESTING)

    def reject(self, who: str, reason: str = "") -> None:
        if self.status in TERMINAL:
            raise ValueError(f"run is already {self.status}")
        self.audit.record("run.rejected", who=who, reason=reason)
        self._set(RunStatus.REJECTED, error=reason or None)
        self._write_report()

    def execute(self) -> None:
        """Baseline AI test → agent → final gate. Ends at AWAITING_SHIP or BLOCKED."""
        if self.status == RunStatus.AWAITING_REQUIREMENTS:
            raise ValueError("requirements are not approved yet")
        self._require(RunStatus.BASELINE_TESTING)
        reqs = self.state["requirements"]
        copy = None
        try:
            system_map = scan(self.workspace)
            copy = self._copy(system_map)
            copy.up()
            before = browser.run(copy.app_url, reqs["browser_tests"], reqs["db_invariants"], self.dir / "before", self._emit)
            self._log_db(before)
            self.audit.record("baseline.tested", who="ai-tester", passed=before["passed"])
            self._set(RunStatus.AGENT_WORKING, before=before)

            def on_event(kind, data):
                if kind == "round":
                    self.state["rounds"].append(data)
                    self._save()
                self._emit(kind, data)

            result = agent.run(self.workspace, copy, reqs, on_event=on_event)
            self.state["rounds"] = result["rounds"]
            agent_info = {k: result[k] for k in ("summary", "cost_usd", "edited_files")}
            self.audit.record("agent.finished", who="ai-agent", rounds=len(result["rounds"]), cost_usd=result["cost_usd"], files=result["edited_files"])
            self._set(RunStatus.FINAL_GATE, agent=agent_info)

            copy.map = scan(self.workspace)
            copy.rebuild_app()
            final = checks.run_all(copy, self.workspace)
            after = browser.run(copy.app_url, reqs["browser_tests"], reqs["db_invariants"], self.dir / "after", self._emit)
            self._log_db(after)
            passed = all(c.passed for c in final) and after["passed"]
            subprocess.run(["git", "add", "-A", "--", ".", ":(exclude).ccopy"], cwd=self.workspace, check=True)
            diff = subprocess.run(["git", "diff", "--cached"], cwd=self.workspace, capture_output=True, text=True).stdout
            (self.dir / "change.patch").write_text(diff)
            self.audit.record("gate.evaluated", who="carboncopy", passed=passed, checks={c.name: c.passed for c in final}, browser=after["passed"])
            status = RunStatus.AWAITING_SHIP if passed else RunStatus.BLOCKED
            if not passed:
                self.audit.record("change.blocked", who="carboncopy")
            self._set(status, after=after, final_checks=checks.as_dicts(final), patch="change.patch")
            self._write_report()
        except Exception as e:  # noqa: BLE001
            self._fail(e)
        finally:
            if copy:
                copy.down()

    def approve_ship(self, who: str) -> None:
        self._require(RunStatus.AWAITING_SHIP)
        patch = (self.dir / "change.patch").read_bytes()
        self.audit.record("change.approved", who=who, patch_sha256=hashlib.sha256(patch).hexdigest())
        self._set(RunStatus.SHIPPED)
        self._write_report()

    def _require(self, status: RunStatus) -> None:
        if self.status != status:
            raise ValueError(f"run is {self.status}, expected {status}")


class TestRun(_Run):
    kind = "test"
    __test__ = False  # not a pytest class

    @classmethod
    def create(cls, repo: Path, tests: list[dict], db_checks: list[dict], runs_dir: Path, emit: Emit | None = None, title: str = "") -> "TestRun":
        return cls._create(repo, runs_dir, emit, tests=tests, db_checks=db_checks, title=title or f"{len(tests)} tests")

    def execute(self) -> None:
        copy = None
        try:
            self._set(RunStatus.MAPPING)
            system_map = scan(self.workspace)
            self._set(RunStatus.COPYING)
            copy = self._copy(system_map)
            copy.up()
            fid = copy.fidelity()
            self._set(RunStatus.TESTING, fidelity=fid)
            res = browser.run(copy.app_url, self.state["tests"], self.state["db_checks"], self.dir / "after", self._emit)
            self._log_db(res)
            self.audit.record("tests.run", who="ai-tester", passed=res["passed"], tests=len(res["tests"]))
            self._set(RunStatus.PASSED if res["passed"] else RunStatus.FAILED, after=res)
            self._write_report()
        except Exception as e:  # noqa: BLE001
            self._fail(e)
        finally:
            if copy:
                copy.down()
