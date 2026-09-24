"""Job queue: `local` (one in-process worker thread) or `sqs` (AWS SQS, drained by `python -m carboncopy.server.worker`)."""
from __future__ import annotations

import json
import logging
import queue as stdqueue
import threading
from typing import Any, Callable, Protocol

from . import config

log = logging.getLogger("ccopy.queue")
Job = dict[str, Any]


class JobQueue(Protocol):
    def enqueue(self, job: Job) -> None: ...
    def start(self) -> None: ...


class LocalQueue:
    """One job at a time: copies bind fixed host ports."""

    def __init__(self, handler: Callable[[Job], None] | None = None):
        self._q: stdqueue.Queue[Job] = stdqueue.Queue()
        self._handler = handler
        self._thread: threading.Thread | None = None

    def enqueue(self, job: Job) -> None:
        self._q.put(job)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        if self._handler is None:
            from .worker import handle_job

            self._handler = handle_job
        self._thread = threading.Thread(target=self._loop, name="ccopy-worker", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while True:
            job = self._q.get()
            try:
                self._handler(job)  # type: ignore[misc]
            except Exception:
                log.exception("job failed: %s", job)


class SqsQueue:
    def __init__(self, url: str):
        import boto3

        self.url = url
        host = url.split("://", 1)[-1].split("/", 1)[0]
        region = host.split(".")[1] if host.startswith("sqs.") else None
        self.sqs = boto3.client("sqs", region_name=region)

    def enqueue(self, job: Job) -> None:
        self.sqs.send_message(QueueUrl=self.url, MessageBody=json.dumps(job))

    def start(self) -> None:
        log.info("sqs queue: jobs run in `python -m carboncopy.server.worker`")

    def poll(self, handler: Callable[[Job], None]) -> None:
        """At-most-once: a message is deleted before its (long, non-idempotent) job runs."""
        while True:
            resp = self.sqs.receive_message(QueueUrl=self.url, MaxNumberOfMessages=1, WaitTimeSeconds=20)
            for m in resp.get("Messages", []):
                self.sqs.delete_message(QueueUrl=self.url, ReceiptHandle=m["ReceiptHandle"])
                try:
                    handler(json.loads(m["Body"]))
                except Exception:
                    log.exception("job failed: %s", m.get("Body"))


_queue: JobQueue | None = None
_lock = threading.Lock()


def get_queue() -> JobQueue:
    global _queue
    with _lock:
        if _queue is None:
            if config.queue_kind() == "sqs":
                url = config.sqs_url()
                if not url:
                    raise RuntimeError("CCOPY_QUEUE=sqs needs CCOPY_SQS_URL")
                _queue = SqsQueue(url)
            else:
                _queue = LocalQueue()
        return _queue


def enqueue(job_type: str, run_id: str, **extra: Any) -> None:
    get_queue().enqueue({"type": job_type, "run_id": run_id, **extra})
