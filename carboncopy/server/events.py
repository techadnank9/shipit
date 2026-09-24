"""In-memory pub/sub of run events for SSE. Per process: in SQS mode worker events do not reach the API."""
from __future__ import annotations

import asyncio
import threading
from collections import deque
from typing import Any

Event = tuple[str, dict[str, Any]]


class Broker:
    def __init__(self, history: int = 500):
        self._lock = threading.Lock()
        self._subs: dict[str, set[tuple[asyncio.AbstractEventLoop, asyncio.Queue[Event]]]] = {}
        self._history: dict[str, deque[Event]] = {}
        self._max = history

    def publish(self, rid: str, event: str, data: dict[str, Any]) -> None:
        with self._lock:
            if event != "status":
                self._history.setdefault(rid, deque(maxlen=self._max)).append((event, data))
            subs = list(self._subs.get(rid, ()))
        for loop, q in subs:
            try:
                loop.call_soon_threadsafe(q.put_nowait, (event, data))
            except RuntimeError:
                pass

    def subscribe(self, rid: str) -> tuple[asyncio.Queue[Event], list[Event]]:
        q: asyncio.Queue[Event] = asyncio.Queue()
        with self._lock:
            self._subs.setdefault(rid, set()).add((asyncio.get_running_loop(), q))
            return q, list(self._history.get(rid, ()))

    def unsubscribe(self, rid: str, q: asyncio.Queue[Event]) -> None:
        with self._lock:
            subs = self._subs.get(rid, set())
            subs.difference_update({s for s in subs if s[1] is q})
            if not subs:
                self._subs.pop(rid, None)


broker = Broker()
