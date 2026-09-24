"""Scheduled test runs: a daemon thread checks project schedules every 30s (cron, UTC).

The contract names no Store methods for schedules; this uses the server's Store:
  store.list_schedules() -> [{project_id, cron, next_run_at}]
  store.mark_schedule_ran(pid, now)   advances next_run_at to the next slot after `now`
  store.set_schedule(pid, cron)       (re)computes next_run_at when it is missing
with a fallback to get_schedule(pid) per project if list_schedules is absent.
"""
import logging
import os
import threading
import time
from datetime import datetime, timezone

from croniter import croniter

log = logging.getLogger("carboncopy.integrations.schedule")
INTERVAL = float(os.environ.get("CCOPY_SCHEDULER_INTERVAL", "30"))
_started = threading.Event()


def valid(cron: str) -> bool:
    return bool(cron) and croniter.is_valid(cron)


def next_run(cron: str, after: float | None = None) -> float:
    """Next fire time (epoch seconds, UTC) strictly after `after` (default: now). Raises ValueError on a bad cron."""
    if not valid(cron):
        raise ValueError(f"invalid cron expression: {cron!r}")
    base = datetime.fromtimestamp(time.time() if after is None else after, tz=timezone.utc)
    return croniter(cron, base).get_next(float)


def _schedules(store) -> list[dict]:
    if hasattr(store, "list_schedules"):
        return list(store.list_schedules() or [])
    out = []
    for p in store.list_projects() or []:
        s = store.get_schedule(p["id"]) if hasattr(store, "get_schedule") else None
        if s:
            out.append(s)
    return out


def _advance(store, pid: str, cron: str, now: float) -> None:
    """Move the schedule to its first slot after `now`."""
    if hasattr(store, "mark_schedule_ran"):
        store.mark_schedule_ran(pid, now)
    elif hasattr(store, "set_schedule_next_run"):
        store.set_schedule_next_run(pid, next_run(cron, now))
    else:
        store.set_schedule(pid, cron)  # recomputes next_run_at from the current time


def tick(store, now: float | None = None) -> list[str]:
    """One pass: fire every due schedule once and move it to its next slot. Returns started run ids.
    Missed slots (server was down) collapse into a single run, not a burst."""
    now = time.time() if now is None else now
    started = []
    for s in _schedules(store):
        pid, cron, due = s.get("project_id"), s.get("cron"), s.get("next_run_at")
        if not pid or not cron:
            continue
        try:
            if not valid(cron):
                log.warning("schedule: project %s has invalid cron %r", pid, cron)
                continue
            if due is None:
                store.set_schedule(pid, cron)
                continue
            if float(due) <= now:
                _advance(store, pid, cron, now)  # advance first: never double-fire
                run = store.create_test_run(pid, "schedule")
                started.append(run["id"])
                log.info("schedule: project %s (%s) -> run %s", pid, cron, run["id"])
        except Exception:  # noqa: BLE001
            log.exception("schedule: project %s failed", pid)
    return started


def start_scheduler(store) -> None:
    """Start the background scheduler once per process."""
    if _started.is_set():
        return
    _started.set()

    def loop():
        while True:
            try:
                tick(store)
            except Exception:  # noqa: BLE001
                log.exception("schedule: tick failed")
            time.sleep(INTERVAL)

    threading.Thread(target=loop, name="ccopy-scheduler", daemon=True).start()
    log.info("schedule: scheduler started (every %ss)", INTERVAL)
