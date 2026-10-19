"""Sequence-numbered, bounded, coalescing event log.

Every signal the browser emits lands here: console, page errors, network,
navigation, dialogs, downloads and tab lifecycle. Agents subscribe with a
cursor and receive only what happened since; high-frequency signals coalesce so
a chatty page cannot flood the stream.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Any

Event = dict[str, Any]


class EventBus:
    def __init__(self, capacity: int = 4000, coalesce_window_ms: int = 400) -> None:
        self.capacity = max(1, capacity)
        self.coalesce_window_ms = max(0, coalesce_window_ms)
        self.seq = 0
        self.buffer: deque[Event] = deque(maxlen=self.capacity)
        self.dropped_through = 0
        self._last_by_key: dict[str, Event] = {}
        self._subscribers: set[asyncio.Queue[tuple[Event, dict[str, Any]]]] = set()

    # -- writing -----------------------------------------------------------

    def emit(
        self,
        event_type: str,
        data: dict[str, Any],
        *,
        key: str | None = None,
        summary: str | None = None,
    ) -> Event:
        now = time.time()
        if key and self.coalesce_window_ms:
            previous = self._last_by_key.get(key)
            if previous is not None and (now - previous["ts"]) * 1000 < self.coalesce_window_ms:
                previous["data"] = data
                previous["ts"] = now
                previous["count"] = previous.get("count", 1) + 1
                if summary is not None:
                    previous["summary"] = summary
                self._notify(previous, coalesced=True)
                return previous

        if len(self.buffer) == self.capacity:
            evicted = self.buffer[0]
            self.dropped_through = max(self.dropped_through, int(evicted["seq"]))
            if evicted.get("key") and self._last_by_key.get(evicted["key"]) is evicted:
                self._last_by_key.pop(evicted["key"], None)

        self.seq += 1
        event: Event = {
            "seq": self.seq,
            "ts": now,
            "type": event_type,
            "data": data,
            "summary": summary,
            "key": key,
            "count": 1,
        }
        if key:
            self._last_by_key[key] = event
        self.buffer.append(event)
        self._notify(event, coalesced=False)
        return event

    def _notify(self, event: Event, *, coalesced: bool) -> None:
        if not self._subscribers:
            return
        meta = {"coalesced": coalesced}
        for queue in list(self._subscribers):
            try:
                queue.put_nowait((event, meta))
            except asyncio.QueueFull:
                # Slow client: drop its oldest frame, never block the browser.
                try:
                    queue.get_nowait()
                    queue.put_nowait((event, meta))
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass

    # -- reading -----------------------------------------------------------

    def since(self, cursor: int = 0) -> tuple[list[Event], int, bool]:
        events = [e for e in self.buffer if int(e["seq"]) > cursor]
        last_seq = int(events[-1]["seq"]) if events else cursor
        dropped = cursor < self.dropped_through
        return events, last_seq, dropped

    def mark(self) -> int:
        return self.seq

    def subscribe(self, maxsize: int = 2000) -> asyncio.Queue[tuple[Event, dict[str, Any]]]:
        queue: asyncio.Queue[tuple[Event, dict[str, Any]]] = asyncio.Queue(maxsize=maxsize)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[tuple[Event, dict[str, Any]]]) -> None:
        self._subscribers.discard(queue)


# ---------------------------------------------------------------------------
# Frames and digests
# ---------------------------------------------------------------------------


def frame(event: Event, *, coalesced: bool = False) -> dict[str, Any]:
    """The wire shape of one event.

    Metadata comes last so a stray field in the event's own data cannot
    clobber the envelope (for example a network `type` overriding the event
    type).
    """
    payload = {
        **event["data"],
        "seq": event["seq"],
        "ts": event["ts"],
        "type": event["type"],
    }
    if event.get("count", 1) > 1:
        payload["count"] = event["count"]
    if coalesced:
        payload["coalesced"] = True
    return payload


def summarize(events: list[Event]) -> dict[str, Any]:
    by_type: dict[str, int] = {}
    highlights: list[str] = []
    for event in events:
        by_type[event["type"]] = by_type.get(event["type"], 0) + 1
        data = event["data"]
        if event["type"] == "console" and data.get("level") in ("error", "warning"):
            highlights.append(f"[{data['level']}] {data.get('text', '')}")
        elif event["type"] == "pageerror":
            highlights.append(f"[pageerror] {data.get('message', '')}")
        elif event["type"] == "requestfailed":
            highlights.append(
                f"[netfail] {data.get('method', '')} "
                f"{data.get('url', '')} {data.get('failure', '')}"
            )
        elif event["type"] == "response" and int(data.get("status") or 0) >= 400:
            highlights.append(
                f"[http {data['status']}] {data.get('method', '')} {data.get('url', '')}"
            )
        elif event["type"] == "navigation":
            highlights.append(f"[nav] {data.get('url', '')}")
        elif event["type"] == "dialog":
            highlights.append(f"[dialog {data.get('kind', '')}] {data.get('message', '')}")
        elif event["type"] == "download":
            highlights.append(f"[download] {data.get('filename') or data.get('url', '')}")
    return {"byType": by_type, "highlights": highlights[:40], "total": len(events)}
