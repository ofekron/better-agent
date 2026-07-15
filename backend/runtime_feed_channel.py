"""Runtime→BFF canonical feed advance channel.

Fans out "canonical journal advanced for root X" facts to attached
server-to-server subscribers (the BFF's chat feed client). Publishes
facts, never commands: subscribers decide whether and what to pull.

Loss-proof by design: each subscriber holds a dirty-set of root ids,
not a frame queue, so bursts coalesce and nothing can overflow. A
subscriber that drains the set pulls each root's feed to head, which
makes any coalescing invisible.
"""
from __future__ import annotations

import asyncio
import threading


class FeedSubscriber:
    def __init__(self) -> None:
        self.dirty: set[str] = set()
        self.wake = asyncio.Event()
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


runtime_feed_channel = RuntimeFeedChannel()
