from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timezone

from .contracts import DashboardEvent


class EventHub:
    def __init__(self, replay_size: int = 2_000):
        self._sequence = 0
        self._replay: deque[DashboardEvent] = deque(maxlen=replay_size)
        self._subscribers: set[asyncio.Queue[DashboardEvent]] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind(self) -> None:
        self._loop = asyncio.get_running_loop()

    def publish(self, event_type: str, meeting_id: str | None, payload: dict) -> DashboardEvent:
        self._sequence += 1
        event = DashboardEvent(
            sequence=self._sequence,
            event_type=event_type,
            meeting_id=meeting_id,
            created_at=datetime.now(timezone.utc),
            payload=payload,
        )
        self._replay.append(event)
        for queue in tuple(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                self._subscribers.discard(queue)
        return event

    def publish_threadsafe(self, event_type: str, meeting_id: str | None, payload: dict) -> None:
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self.publish, event_type, meeting_id, payload)

    def subscribe(self, since: int = 0) -> asyncio.Queue[DashboardEvent]:
        queue: asyncio.Queue[DashboardEvent] = asyncio.Queue(maxsize=500)
        for event in self._replay:
            if event.sequence > since:
                queue.put_nowait(event)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[DashboardEvent]) -> None:
        self._subscribers.discard(queue)
