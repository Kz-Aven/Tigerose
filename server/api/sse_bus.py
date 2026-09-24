"""In-process SSE pub/sub."""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from typing import Any


class EventBus:
    def __init__(self) -> None:
        self._subs: dict[str, list[asyncio.Queue]] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def subscribe(self, channel: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        async with self._lock:
            self._subs[channel].append(q)
        return q

    async def unsubscribe(self, channel: str, q: asyncio.Queue) -> None:
        async with self._lock:
            subs = self._subs.get(channel, [])
            if q in subs:
                subs.remove(q)

    async def publish(self, channel: str, event_type: str, data: Any) -> None:
        payload = {"type": event_type, "data": data}
        async with self._lock:
            subs = list(self._subs.get(channel, []))
        for q in subs:
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                pass

    def publish_threadsafe(self, loop: asyncio.AbstractEventLoop, channel: str, event_type: str, data: Any) -> None:
        asyncio.run_coroutine_threadsafe(self.publish(channel, event_type, data), loop)


bus = EventBus()


def format_sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
