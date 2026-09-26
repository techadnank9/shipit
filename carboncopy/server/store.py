"""Store: projects, saved tests, DB checks, runs and schedules (SQLAlchemy Core on SQLite or Postgres)."""
from __future__ import annotations

import json
import secrets
import threading
import time
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from croniter import croniter

from . import config

Row = dict[str, Any]

md = sa.MetaData()
projects = sa.Table(
    "projects", md,
    sa.Column("id", sa.String(32), primary_key=True),
    sa.Column("name", sa.String(200), nullable=False),
    sa.Column("repo_path", sa.Text),
    sa.Column("git_url", sa.Text, index=True),
    sa.Column("created_at", sa.Float, nullable=False),
)
saved_tests = sa.Table(
    "saved_tests", md,
    sa.Column("id", sa.String(32), primary_key=True),
    sa.Column("project_id", sa.String(32), nullable=False, index=True),
    sa.Column("name", sa.String(200), nullable=False),
    sa.Column("instructions", sa.Text, nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
)
db_checks = sa.Table(
    "db_checks", md,
    sa.Column("id", sa.String(32), primary_key=True),
    sa.Column("project_id", sa.String(32), nullable=False, index=True),
    sa.Column("description", sa.Text, nullable=False),
    sa.Column("sql", sa.Text, nullable=False),
    sa.Column("expect", sa.JSON),
    sa.Column("created_at", sa.Float, nullable=False),
)
runs = sa.Table(
    "runs", md,
    sa.Column("id", sa.String(64), primary_key=True),
    sa.Column("project_id", sa.String(32), nullable=False),
    sa.Column("kind", sa.String(16), nullable=False),
    sa.Column("status", sa.String(32), nullable=False),
    sa.Column("trigger", sa.String(16), nullable=False),
    sa.Column("title", sa.Text, nullable=False),
    sa.Column("passed", sa.Boolean),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.Column("updated_at", sa.Float, nullable=False),
    sa.Column("run_dir", sa.Text, nullable=False),
    sa.Index("ix_runs_project_created", "project_id", "created_at"),
)
run_meta = sa.Table(
    "run_meta", md,
    sa.Column("run_id", sa.String(64), primary_key=True),
    sa.Column("key", sa.String(100), primary_key=True),
    sa.Column("value", sa.JSON),
)
project_meta = sa.Table(
    "project_meta", md,
    sa.Column("project_id", sa.String(32), primary_key=True),
    sa.Column("key", sa.String(100), primary_key=True),
    sa.Column("value", sa.JSON),
)
schedules = sa.Table(
    "schedules", md,
    sa.Column("project_id", sa.String(32), primary_key=True),
    sa.Column("cron", sa.String(100)),
    sa.Column("next_run_at", sa.Float),
    sa.Column("last_run_at", sa.Float),
)

RUN_FIELDS = ("id", "project_id", "kind", "status", "trigger", "title", "created_at", "updated_at", "passed")


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(5)}"


def _engine(url: str) -> sa.Engine:
    if url.startswith("postgres://"):
        url = "postgresql://" + url.removeprefix("postgres://")
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url.removeprefix("postgresql://")
    if url.startswith("sqlite:///"):
        path = url.removeprefix("sqlite:///")
        if path and path != ":memory:":
            Path(path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        return sa.create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})
    return sa.create_engine(url, pool_pre_ping=True)


def next_cron(cron: str, after: float | None = None) -> float:
    return float(croniter(cron, after or time.time()).get_next(float))


class Store:
    def __init__(self, url: str | None = None):
        self.engine = _engine(url or config.database_url())
        md.create_all(self.engine)

    def _one(self, q) -> Row | None:
        with self.engine.connect() as c:
            r = c.execute(q).mappings().first()
        return dict(r) if r else None

    def _all(self, q) -> list[Row]:
        with self.engine.connect() as c:
            return [dict(r) for r in c.execute(q).mappings()]

    def _exec(self, q) -> int:
        with self.engine.begin() as c:
            return c.execute(q).rowcount

    # projects
    def create_project(self, name: str, repo_path: str | None = None, git_url: str | None = None) -> Row:
        row = {"id": new_id("p"), "name": name, "repo_path": repo_path, "git_url": git_url, "created_at": time.time()}
        self._exec(projects.insert().values(**row))
        return self.get_project(row["id"])  # type: ignore[return-value]

    def _with_last_run(self, p: Row) -> Row:
        last = self._one(runs.select().where(runs.c.project_id == p["id"]).order_by(runs.c.created_at.desc()).limit(1))
        return p | {"last_run": self._run_out(last) if last else None}

    def get_project(self, pid: str) -> Row | None:
        p = self._one(projects.select().where(projects.c.id == pid))
        return self._with_last_run(p) if p else None

    def list_projects(self) -> list[Row]:
        return [self._with_last_run(p) for p in self._all(projects.select().order_by(projects.c.created_at))]

    def delete_project(self, pid: str) -> None:
        self._exec(projects.delete().where(projects.c.id == pid))

    def project_by_git_url(self, url: str) -> Row | None:
        def norm(u: str) -> str:
            u = u.strip().rstrip("/").removesuffix(".git").lower()
            if u.startswith("git@"):
                u = "https://" + u[4:].replace(":", "/", 1)
            return u.split("://", 1)[-1].split("@", 1)[-1]

        want = norm(url)
        for p in self._all(projects.select().where(projects.c.git_url.is_not(None))):
            if norm(p["git_url"]) == want:
                return self._with_last_run(p)
        return None

    # saved tests and DB checks
    def add_saved_test(self, project_id: str, name: str, instructions: str) -> Row:
        row = {"id": new_id("t"), "project_id": project_id, "name": name, "instructions": instructions, "created_at": time.time()}
        self._exec(saved_tests.insert().values(**row))
        return row

    def list_saved_tests(self, project_id: str, ids: list[str] | None = None) -> list[Row]:
        q = saved_tests.select().where(saved_tests.c.project_id == project_id)
        if ids is not None:
            q = q.where(saved_tests.c.id.in_(ids))
        return self._all(q.order_by(saved_tests.c.created_at))

    def delete_saved_test(self, tid: str) -> bool:
        return self._exec(saved_tests.delete().where(saved_tests.c.id == tid)) > 0

    def add_db_check(self, project_id: str, description: str, sql: str, expect: Any) -> Row:
        row = {"id": new_id("c"), "project_id": project_id, "description": description, "sql": sql, "expect": expect, "created_at": time.time()}
        self._exec(db_checks.insert().values(**row))
        return row

    def list_db_checks(self, project_id: str) -> list[Row]:
        return self._all(db_checks.select().where(db_checks.c.project_id == project_id).order_by(db_checks.c.created_at))

    # runs
    def insert_run(self, *, id: str, project_id: str, kind: str, status: str, trigger: str, title: str, run_dir: str) -> Row:
        now = time.time()
        self._exec(runs.insert().values(id=id, project_id=project_id, kind=kind, status=status, trigger=trigger, title=title,
                                        passed=None, created_at=now, updated_at=now, run_dir=run_dir))
        return self.get_run(id)  # type: ignore[return-value]

    def update_run(self, rid: str, **fields: Any) -> None:
        self._exec(runs.update().where(runs.c.id == rid).values(updated_at=time.time(), **fields))

    def run_row(self, rid: str) -> Row | None:
        return self._one(runs.select().where(runs.c.id == rid))

    @staticmethod
    def _run_out(row: Row, state: bool = False) -> Row:
        out = {k: row[k] for k in RUN_FIELDS}
        if state:
            f = Path(row["run_dir"]) / "state.json"
            try:
                out["state"] = json.loads(f.read_text())
            except (OSError, ValueError):
                out["state"] = None
        return out

    def get_run(self, rid: str, state: bool = False) -> Row | None:
        row = self.run_row(rid)
        return self._run_out(row, state) if row else None

    def list_runs(self, project_id: str | None = None, kind: str | None = None, status: str | None = None, limit: int = 50) -> list[Row]:
        q = runs.select()
        if project_id:
            q = q.where(runs.c.project_id == project_id)
        if kind:
            q = q.where(runs.c.kind == kind)
        if status:
            q = q.where(runs.c.status.in_(status.split(",")))
        return [self._run_out(r) for r in self._all(q.order_by(runs.c.created_at.desc()).limit(max(1, min(limit, 500))))]

    def runs_with_status(self, statuses: set[str] | frozenset[str], exclude: bool = False) -> list[Row]:
        col = runs.c.status
        return self._all(runs.select().where(col.not_in(statuses) if exclude else col.in_(statuses)))

    def create_test_run(self, project_id: str, trigger: str, test_ids: list[str] | None = None, ref: str | None = None) -> Row:
        from .runs import start_test_run

        return start_test_run(self, project_id, trigger, test_ids, ref=ref)

    def create_change_run(self, project_id: str, request: str, trigger: str = "manual", ref: str | None = None) -> Row:
        from .runs import start_change_run

        return start_change_run(self, project_id, request, trigger, ref=ref)

    def set_run_meta(self, run_id: str, key: str, value: Any) -> None:
        with self.engine.begin() as c:
            n = c.execute(run_meta.update().where(run_meta.c.run_id == run_id, run_meta.c.key == key).values(value=value)).rowcount
            if not n:
                c.execute(run_meta.insert().values(run_id=run_id, key=key, value=value))

    def get_run_meta(self, run_id: str, key: str) -> Any:
        r = self._one(sa.select(run_meta.c.value).where(run_meta.c.run_id == run_id, run_meta.c.key == key))
        return r["value"] if r else None

    def set_project_meta(self, project_id: str, key: str, value: Any) -> None:
        with self.engine.begin() as c:
            n = c.execute(project_meta.update().where(project_meta.c.project_id == project_id, project_meta.c.key == key).values(value=value)).rowcount
            if not n:
                c.execute(project_meta.insert().values(project_id=project_id, key=key, value=value))

    def get_project_meta(self, project_id: str, key: str) -> Any:
        r = self._one(sa.select(project_meta.c.value).where(project_meta.c.project_id == project_id, project_meta.c.key == key))
        return r["value"] if r else None

    # schedules
    def get_schedule(self, project_id: str) -> Row:
        r = self._one(schedules.select().where(schedules.c.project_id == project_id))
        return {"project_id": project_id, "cron": r["cron"] if r else None, "next_run_at": r["next_run_at"] if r else None}

    def set_schedule(self, project_id: str, cron: str | None) -> Row:
        nxt = next_cron(cron) if cron else None
        with self.engine.begin() as c:
            n = c.execute(schedules.update().where(schedules.c.project_id == project_id).values(cron=cron, next_run_at=nxt)).rowcount
            if not n:
                c.execute(schedules.insert().values(project_id=project_id, cron=cron, next_run_at=nxt))
        return self.get_schedule(project_id)

    def list_schedules(self) -> list[Row]:
        return self._all(schedules.select().where(schedules.c.cron.is_not(None)))

    def due_schedules(self, now: float | None = None) -> list[Row]:
        return self._all(schedules.select().where(schedules.c.cron.is_not(None), schedules.c.next_run_at <= (now or time.time())))

    def set_schedule_next_run(self, project_id: str, ts: float) -> None:
        self._exec(schedules.update().where(schedules.c.project_id == project_id).values(next_run_at=ts, last_run_at=time.time()))

    def mark_schedule_ran(self, project_id: str, now: float | None = None) -> Row:
        now = now or time.time()
        s = self.get_schedule(project_id)
        if s["cron"]:
            self._exec(schedules.update().where(schedules.c.project_id == project_id).values(last_run_at=now, next_run_at=next_cron(s["cron"], now)))
        return self.get_schedule(project_id)


_store: Store | None = None
_lock = threading.Lock()


def get_store() -> Store:
    global _store
    with _lock:
        if _store is None:
            _store = Store()
        return _store
