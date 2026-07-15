"""Runtime→BFF canonical feed advance channel.

Fans out "canonical journal advanced for root X" facts to attached
server-to-server subscribers (the BFF's chat feed client). Publishes
facts, never commands: subscribers decide whether and what to pull.

Two independent delivery paths share one connection:

  - `dirty` / `publish_advance` — coalesced settled-fact signal. Each
    subscriber holds a dirty-set of root ids, not a frame queue, so
    bursts coalesce and nothing can overflow; a drainer pulls each
    dirty root's feed to head, making any coalescing invisible.
  - `raw` / `publish_raw_event` — an ORDERED per-subscriber FIFO of
    in-flight raw event frames (live typing deltas). These must NOT
    coalesce: every frame is delivered in production order so the BFF's
    current-turn cache renders the same token sequence the provider
    emitted. Only the in-scope agent-render event types are forwarded.
"""
from __future__ import annotations

import asyncio
import threading

# Event types whose raw frames the BFF owns rendering for. Anything else
# (sidebar/session metadata, approvals, provider/node infra) is NOT
# forwarded on the raw path. Kept in sync with the plan's "moves to BFF"
# list; each string is a verified `ws_callback`/tailer-sent type.
_RAW_FORWARD_TYPES = frozenset({
    "agent_message",
    "manager_event",
    "messages_replay",
    "messages_delta",
    "worker_event",
    "worker_start",
    "worker_complete",
    "worker_prep_start",
    "worker_prep_event",
    "worker_prep_complete",
    "worker_prep_cancelled",
    "todos_snapshot",
    "todos_updated",
    "supervisor_event",
    "turn_start",
    "turn_complete",
    "turn_stopped",
    "turn_detached",
})


class FeedSubscriber:
    def __init__(self) -> None:
        self.dirty: set[str] = set()
        self.wake = asyncio.Event()
        self.raw: asyncio.Queue[dict] = asyncio.Queue()
        self.closed = False

    async def wait_drain(self) -> set[str]:
        await self.wake.wait()
        self.wake.clear()
        if self.closed:
            raise ConnectionError("feed channel closed")
        roots = self.dirty
        self.dirty = set()
        return roots


class RuntimeFeedChannel:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._subscribers: set[FeedSubscriber] = set()

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        with self._lock:
            self._loop = loop

    def unbind(self) -> None:
        with self._lock:
            loop = self._loop
            subscribers = list(self._subscribers)
            self._loop = None
            self._subscribers.clear()
        if loop is None:
            return

        def _close_all() -> None:
            for subscriber in subscribers:
                subscriber.closed = True
                subscriber.wake.set()

        try:
            loop.call_soon_threadsafe(_close_all)
        except RuntimeError:
            pass

    def attach(self) -> FeedSubscriber:
        subscriber = FeedSubscriber()
        with self._lock:
            self._subscribers.add(subscriber)
        return subscriber

    def detach(self, subscriber: FeedSubscriber) -> None:
        with self._lock:
            self._subscribers.discard(subscriber)

    def publish_advance(self, root_id: str, journal_seq: int) -> None:
        """Thread-safe: called from the journal writer's worker threads."""
        if not isinstance(root_id, str) or not root_id:
            return
        with self._lock:
            loop = self._loop
            subscribers = list(self._subscribers)
        if loop is None or not subscribers:
            return

        def _fanout() -> None:
            for subscriber in subscribers:
                subscriber.dirty.add(root_id)
                subscriber.wake.set()

        try:
            loop.call_soon_threadsafe(_fanout)
        except RuntimeError:
            pass

    def publish_raw_event(
        self,
        root_id: str,
        event_type: str,
        data: object,
        *,
        seq: int,
        sid: str | None = None,
        source: str | None = None,
        msg_id: str | None = None,
    ) -> None:
        """Thread-safe: called from the wire tailer's run loop.

        Enqueues one ordered raw frame onto every attached subscriber's
        FIFO, but only for the in-scope agent-render event types. Out-of-
        scope types are dropped here so they never reach the BFF's raw
        path.
        """
        if not isinstance(root_id, str) or not root_id:
            return
        if event_type not in _RAW_FORWARD_TYPES:
            return
        with self._lock:
            loop = self._loop
            subscribers = list(self._subscribers)
        if loop is None or not subscribers:
            return
        frame = {
            "root_id": root_id,
            "event_type": event_type,
            "data": data,
            "seq": seq,
            "sid": sid,
            "source": source,
            "msg_id": msg_id,
        }

        def _fanout() -> None:
            for subscriber in subscribers:
                subscriber.raw.put_nowait(frame)

        try:
            loop.call_soon_threadsafe(_fanout)
        except RuntimeError:
            pass


runtime_feed_channel = RuntimeFeedChannel()
