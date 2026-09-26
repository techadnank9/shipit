"""Worker: runs queued engine jobs. `python -m carboncopy.server.worker` drains the SQS queue."""
from __future__ import annotations

import logging
import traceback
from typing import Any

from .queue import Job, SqsQueue, get_queue
from .runs import fail, load_run, status_of, sync
from .store import get_store

log = logging.getLogger("ccopy.worker")

STEPS = {
    "change.prepare": ("prepare", {"queued"}),
    "change.execute": ("execute", {"baseline_testing"}),
    "change.ship": ("approve_ship", {"awaiting_ship"}),
    "test.execute": ("execute", {"queued"}),
}


def handle_job(job: Job) -> None:
    store = get_store()
    if job.get("type") == "project.onboard":
        from . import onboarding

        log.info("project.onboard %s", job.get("project_id"))
        onboarding.run(store, job.get("project_id", ""))
        return
    rid, kind = job.get("run_id", ""), job.get("type", "")
    row = store.run_row(rid)
    if not row or kind not in STEPS:
        log.error("dropping job %s", job)
        return
    method, allowed = STEPS[kind]
    try:
        run = load_run(store, row)
        if status_of(run) not in allowed or (kind == "change.execute" and not store.get_run_meta(rid, "requirements_approved")):
            log.warning("skipping %s for %s: run is %s", kind, rid, status_of(run))
            return
        log.info("%s %s", kind, rid)
        args: list[Any] = [job.get("who", "api")] if method == "approve_ship" else []
        getattr(run, method)(*args)
        sync(store, rid, run)
    except Exception as e:
        log.error("%s %s failed:\n%s", kind, rid, traceback.format_exc())
        fail(store, rid, f"{type(e).__name__}: {e}")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    q = get_queue()
    if not isinstance(q, SqsQueue):
        raise SystemExit("worker process is for CCOPY_QUEUE=sqs; the local queue runs inside `ccopy serve`")
    log.info("polling %s", q.url)
    q.poll(handle_job)


if __name__ == "__main__":
    main()
