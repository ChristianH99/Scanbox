"""In-process pub/sub for Server-Sent Events.

Each subscriber gets a bounded queue. Events carry an audience:
``user_id`` (only that user), ``"all"`` (every signed-in user) or ``"admin"``.
Slow consumers are dropped (the browser reconnects and refetches state).
"""

from __future__ import annotations

import asyncio
import itertools
import json
from dataclasses import dataclass, field
from typing import Any

import structlog

log = structlog.get_logger(__name__)

ALL = "all"
ADMIN = "admin"


@dataclass
class Event:
    type: str
    data: dict[str, Any]
    audience: int | str = ALL
    id: int = 0

    def encode(self) -> str:
        return f"id: {self.id}\nevent: {self.type}\ndata: {json.dumps(self.data, default=str)}\n\n"


@dataclass(eq=False)
class Subscriber:
    user_id: int
    is_admin: bool
    queue: asyncio.Queue[Event] = field(default_factory=lambda: asyncio.Queue(maxsize=100))
    dropped: bool = False

    def wants(self, ev: Event) -> bool:
        if ev.audience == ALL:
            return True
        if ev.audience == ADMIN:
            return self.is_admin
        return ev.audience == self.user_id


class EventBus:
    def __init__(self) -> None:
        self._subs: set[Subscriber] = set()
        self._ids = itertools.count(1)
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    @property
    def subscriber_count(self) -> int:
        return len(self._subs)

    def subscribe(self, user_id: int, is_admin: bool) -> Subscriber:
        sub = Subscriber(user_id, is_admin)
        self._subs.add(sub)
        return sub

    def unsubscribe(self, sub: Subscriber) -> None:
        self._subs.discard(sub)

    def publish(self, type_: str, data: dict[str, Any], audience: int | str | None = ALL) -> None:
        """Thread-safe: may be called from worker threads (asyncio.to_thread)."""
        if audience is None:  # e.g. inbox document without owner → everyone
            audience = ALL
        ev = Event(type_, data, audience, next(self._ids))
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if self._loop is not None and running is not self._loop:
            if self._loop.is_closed():
                return
            self._loop.call_soon_threadsafe(self._dispatch, ev)
        else:
            self._dispatch(ev)

    def _dispatch(self, ev: Event) -> None:
        for sub in list(self._subs):
            if not sub.wants(ev):
                continue
            try:
                sub.queue.put_nowait(ev)
            except asyncio.QueueFull:
                sub.dropped = True
                self._subs.discard(sub)
                log.info("sse.subscriber_dropped", user_id=sub.user_id)


bus = EventBus()
