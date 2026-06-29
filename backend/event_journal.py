"""Single runtime owner for events.jsonl writes and reads.

EventJournalWriter routes facts to per-root single-thread executors — each
session gets its own thread, so different sessions process concurrently while
per-session writes remain serialized. EventJournalReader owns runtime reads,
live observation, and the expanded message LRU. EventIngester remains the
private low-level storage engine.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import threading
import uuid
from bisect import bisect_right
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional, Union

import perf
from event_bus import BusEvent, EventBus, bus
from event_ingester import event_ingester

EVENT_JOURNAL_EVENT = "event_journal.event"
EVENT_JOURNAL_TURN_MESSAGE_SET = "event_journal.turn_message_set"
EVENT_JOURNAL_TURN_FINISHED = "event_journal.turn_finished"
EVENT_JOURNAL_WRITTEN = "event_journal.written"
EVENT_JOURNAL_WRITE_FAILED = "event_journal.write_failed"
RENDER_EVENT_TYPES = frozenset({
    "agent_message",
    "manager_event",
    "model_switched",
    "steer_prompt",
    "worker_event",
})
# Worker-fork tailer backup rows. Owned by the worker panel, never by a
# message: excluded from ownership resolution and from every render /
# message-read attachment path. MUST stay a value no legacy writer ever
# used — pre-fork-identity disks hold "claude_tailer" rows that are the
# sole copy of PRIMARY content and must keep rendering.
FORK_BACKUP_SOURCE = "fork_backup"
_event_journal_loop: Optional[asyncio.AbstractEventLoop] = None

@dataclass(frozen=True)
class MessageOwnership:
    msg_id: str


@dataclass(frozen=True)
class RootOwnership:
    reason: str = ""


@dataclass(frozen=True)
class MetadataOwnership:
    msg_id: Optional[str] = None


EventOwnership = Union[MessageOwnership, RootOwnership, MetadataOwnership]


@dataclass(frozen=True)
class ResolvedEvent:
    """Resolved write shape internal to EventJournalWriter.

    Producers submit Event.  This shape exists so the
    writer can keep ownership resolution separate from the append mechanics.
    """
    root_id: str
    sid: str
    event_type: str
    data: dict
    source: str
    ownership: EventOwnership
    run_id: Optional[str] = None
    event_id: Optional[str] = None
    cwd_override: Optional[str] = None
    dedupe_by_uid_only: bool = False


@dataclass(frozen=True)
class Event:
    """Producer-facing event.

    Producers report what happened plus correlation facts they know.  The
    writer decides whether that becomes message-owned, root-owned, or metadata.
    """
    root_id: str
    sid: str
    event_type: str
    data: dict
    source: str
    run_id: Optional[str] = None
    event_id: Optional[str] = None
    turn_id: Optional[str] = None
    message_id: Optional[str] = None
    cwd_override: Optional[str] = None
    dedupe_by_uid_only: bool = False


@dataclass(frozen=True)
class EventWritten:
    root_id: str
    sid: str
    event_type: str
    seq: int
    msg_id: Optional[str]
    event_id: Optional[str] = None
    data: Optional[dict] = None
    source: Optional[str] = None


@dataclass(frozen=True, order=True)
class TurnBoundary:
    source_ts: datetime
    turn_id: str
    msg_id: str


class EventJournalWriteError(RuntimeError):
    pass


def bind_event_journal_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Bind the backend loop used by off-loop synchronous publishers."""
    global _event_journal_loop
    _event_journal_loop = loop


async def publish_event(
    *,
    session_id: str,
    event_type: str,
    data: dict,
    source: str,
    context_id: Optional[str] = None,
    message_id: Optional[str] = None,
    turn_id: Optional[str] = None,
    run_id: Optional[str] = None,
    event_id: Optional[str] = None,
    bus_instance: EventBus = bus,
) -> EventWritten:
    """Publish an event fact and wait for the journal writer acknowledgement."""
    resolved_event_id = event_id or str(uuid.uuid4())
    if bus_instance is bus:
        return await event_journal_writer.submit_event_async(Event(
            root_id=session_id,
            sid=context_id or session_id,
            event_type=event_type,
            data=data,
            source=source,
            message_id=message_id,
            turn_id=turn_id,
            run_id=run_id,
            event_id=resolved_event_id,
        ))

    loop = asyncio.get_running_loop()
    result: asyncio.Future[EventWritten] = loop.create_future()
    subscriber_name = f"event_journal_ack_{resolved_event_id}_{uuid.uuid4()}"

    async def _on_written(event: BusEvent) -> None:
        payload = event.payload
        if payload.get("event_id") != resolved_event_id or result.done():
            return
        result.set_result(EventWritten(
            root_id=event.root_id,
            sid=event.sid,
            event_type=str(payload.get("event_type") or event_type),
            seq=int(payload.get("seq") or 0),
            msg_id=event.msg_id,
            event_id=resolved_event_id,
            data=payload.get("data"),
            source=payload.get("source"),
        ))

    async def _on_failed(event: BusEvent) -> None:
        payload = event.payload
        if payload.get("event_id") != resolved_event_id or result.done():
            return
        result.set_exception(EventJournalWriteError(
            f"{payload.get('error_class')}: {payload.get('error_message')}",
        ))

    bus_instance.subscribe(
        EVENT_JOURNAL_WRITTEN,
        _on_written,
        name=subscriber_name,
    )
    bus_instance.subscribe(
        EVENT_JOURNAL_WRITE_FAILED,
        _on_failed,
        name=subscriber_name,
    )
    payload = {
        "event_type": event_type,
        "data": data,
        "source": source,
        "event_id": resolved_event_id,
    }
    if message_id:
        payload["message_id"] = message_id
    if turn_id:
        payload["turn_id"] = turn_id
    try:
        await bus_instance.publish(BusEvent(
            type=EVENT_JOURNAL_EVENT,
            root_id=session_id,
            sid=context_id or session_id,
            payload=payload,
            run_id=run_id,
            persist=False,
        ))
        if not result.done():
            raise EventJournalWriteError(
                f"event journal writer did not acknowledge {resolved_event_id}",
            )
        return await result
    finally:
        bus_instance.unsubscribe(subscriber_name)


def publish_event_sync(
    *,
    session_id: str,
    event_type: str,
    data: dict,
    source: str,
    context_id: Optional[str] = None,
    message_id: Optional[str] = None,
    turn_id: Optional[str] = None,
    run_id: Optional[str] = None,
    event_id: Optional[str] = None,
    cwd_override: Optional[str] = None,
    dedupe_by_uid_only: bool = False,
    timeout: float = 30.0,
) -> Optional[EventWritten]:
    """Queue a fact and block until the dedicated writer thread commits it."""
    if cwd_override is None:
        from session_manager import manager as session_manager

        session = session_manager.get_lite(context_id or session_id) or {}
        cwd = session.get("cwd")
        cwd_override = cwd if isinstance(cwd, str) else ""
    event = Event(
        root_id=session_id,
        sid=context_id or session_id,
        event_type=event_type,
        data=data,
        source=source,
        message_id=message_id,
        turn_id=turn_id,
        run_id=run_id,
        event_id=event_id or str(uuid.uuid4()),
        cwd_override=cwd_override,
        dedupe_by_uid_only=dedupe_by_uid_only,
    )
    return event_journal_writer.submit_event_sync(event, timeout=timeout)


class _ShardedExecutor:
    """Fixed pool of single-thread executors, sharded by root_id.

    N executors with max_workers=1 each.  root_id is hashed to a bucket so
    the same root always lands on the same executor (same thread), giving
    per-root serialization.  Different roots on different buckets run
    concurrently.  Total thread count is bounded at *pool_size*.
    """

    def __init__(self, pool_size: int = 8, thread_name_prefix: str = "ejw") -> None:
        self._pool = [
            ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix=f"{thread_name_prefix}-{i}",
            )
            for i in range(pool_size)
        ]
        for i, ex in enumerate(self._pool):
            perf.register_queue(
                f"{thread_name_prefix}.shard{i}",
                lambda ex=ex: ex._work_queue.qsize(),
            )

    def executor(self, root_id: str) -> ThreadPoolExecutor:
        return self._pool[hash(root_id) % len(self._pool)]

    def submit(self, root_id: str, fn, *args, **kwargs):
        return self.executor(root_id).submit(fn, *args, **kwargs)

    def shutdown(self, wait: bool = True) -> None:
        for ex in self._pool:
            ex.shutdown(wait=wait)


class EventJournalWriter:
    """Ownership-aware write facade for events.jsonl."""

    def __init__(self) -> None:
        self._bus = None
        self._closed = False
        self._executor = _ShardedExecutor(
            pool_size=8,
            thread_name_prefix="ejw",
        )
        self._turn_messages: dict[tuple[str, str], str] = {}
        self._turn_boundaries: dict[tuple[str, str], list[TurnBoundary]] = {}
        self._event_messages: dict[tuple[str, str], str] = {}
        self._tool_messages: dict[tuple[str, str], str] = {}
        self._delegate_messages: dict[tuple[str, str], str] = {}
        self._pending_events: dict[str, dict[int, Event]] = {}
        self._ownership_hydrated_roots: set[str] = set()

    def register(self, bus_instance, *, priority: int = 10) -> None:
        """Register this writer as the handler for journal Event traffic."""
        self._bus = bus_instance
        bus_instance.unsubscribe("event_journal_writer")
        bus_instance.subscribe(
            EVENT_JOURNAL_EVENT,
            self._on_bus_event,
            priority=priority,
            name="event_journal_writer",
        )
        bus_instance.unsubscribe("event_journal_turn_message_set")
        bus_instance.subscribe(
            EVENT_JOURNAL_TURN_MESSAGE_SET,
            self._on_turn_message_set,
            priority=priority,
            name="event_journal_turn_message_set",
        )
        bus_instance.unsubscribe("event_journal_turn_finished")
        bus_instance.subscribe(
            EVENT_JOURNAL_TURN_FINISHED,
            self._on_turn_finished,
            priority=priority,
            name="event_journal_turn_finished",
        )

    async def _on_turn_message_set(self, bus_event: BusEvent) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            self._executor.executor(bus_event.root_id),
            self._set_turn_message,
            bus_event,
        )

    def _set_turn_message(self, bus_event: BusEvent) -> None:
        payload = bus_event.payload or {}
        if not isinstance(payload, dict):
            return
        turn_id = payload.get("turn_id")
        message_id = payload.get("message_id")
        if not (
            isinstance(turn_id, str) and turn_id
            and isinstance(message_id, str) and message_id
        ):
            return
        self._turn_messages[(bus_event.root_id, turn_id)] = message_id

    async def _on_turn_finished(self, bus_event: BusEvent) -> None:
        """Serialize the lifecycle fact without closing ownership.

        A provider may flush late events after turn completion. The next
        turn_started boundary, not turn_finished, closes the timestamp interval.
        """
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            self._executor.executor(bus_event.root_id),
            self._finish_turn,
            bus_event,
        )

    def _finish_turn(self, bus_event: BusEvent) -> None:
        _ = bus_event

    async def _on_bus_event(self, bus_event: BusEvent) -> None:
        try:
            loop = asyncio.get_running_loop()
            written = await loop.run_in_executor(
                self._executor.executor(bus_event.root_id),
                self._write_bus_event,
                bus_event,
            )
        except Exception as exc:
            await self._publish_failed(bus_event, exc)
            return
        await self._publish_written(written, run_id=bus_event.run_id)

    def _write_bus_event(self, bus_event: BusEvent) -> EventWritten:
        event = self._event_from_bus(bus_event)
        return self._append_event(event)

    def submit_event_sync(self, event: Event, *, timeout: float = 30.0) -> Optional[EventWritten]:
        """Queue a fact and block until the dedicated writer thread commits it.

        timeout=0 means fire-and-forget: the write is submitted to the
        per-root executor but the caller does not block. The executor
        still serializes writes in order — only the caller unblocks
        immediately. Returns None on fire-and-forget.
        """
        if self._closed:
            raise EventJournalWriteError("event journal writer is closed")
        future = self._executor.submit(event.root_id, self._append_event, event)
        if timeout == 0:
            # Fire-and-forget: schedule written/failed callbacks on
            # completion so downstream bookkeeping still fires.
            def _on_done(fut: "concurrent.futures.Future[EventWritten]") -> None:
                try:
                    written = fut.result()
                    self._schedule_written(written, run_id=event.run_id)
                except Exception as exc:
                    self._schedule_failed(event, exc)
            future.add_done_callback(_on_done)
            return None
        try:
            written = future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise TimeoutError("timed out waiting for event journal acknowledgement")
        except Exception as exc:
            self._schedule_failed(event, exc)
            raise
        self._schedule_written(written, run_id=event.run_id)
        return written

    async def submit_event_async(self, event: Event) -> EventWritten:
        """Queue a fact without requiring event-bus startup wiring."""
        if self._closed:
            raise EventJournalWriteError("event journal writer is closed")
        future = self._executor.submit(event.root_id, self._append_event, event)
        try:
            written = await asyncio.wrap_future(future)
        except Exception as exc:
            await self._publish_failed_event(event, exc)
            raise
        if self._bus is not None:
            await self._publish_written(written, run_id=event.run_id)
        return written

    def barrier_sync(self, root_id: str, *, timeout: float = 30.0) -> int:
        """Wait for prior queued writes and return their durable high-water mark."""
        future = self._executor.submit(root_id, event_ingester.cursor, root_id)
        try:
            return int(future.result(timeout=timeout))
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise TimeoutError("timed out waiting for event journal barrier")

    async def barrier(self, root_id: str) -> int:
        """Async writer barrier; writes queued before this call are durable."""
        future = self._executor.submit(root_id, event_ingester.cursor, root_id)
        return int(await asyncio.wrap_future(future))

    def reconcile_ownership_sync(
        self, root_id: str, *, timeout: float = 30.0,
    ) -> int:
        """Resolve durable unattached rows using all currently known facts."""
        future = self._executor.submit(root_id, self._reconcile_ownership, root_id)
        try:
            return int(future.result(timeout=timeout))
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise TimeoutError("timed out reconciling event journal ownership")

    async def prepare_read(self, root_id: str) -> int:
        """Single-thread-hop consolidation of barrier + reconcile_through.

        Runs cursor + session_manager.reconcile_through sequentially on the
        per-root shard thread. Returns the barrier seq.

        Ownership reconciliation is NOT included here — it already runs on
        every write (_append_event calls _resolve_pending_events), so the
        read path only needs to ensure writes are durable (cursor/barrier)
        and projected into the cache (reconcile_through).
        """
        from session_manager import manager as session_manager

        def _run() -> int:
            barrier_seq = int(event_ingester.cursor(root_id))
            session_manager.reconcile_through(root_id, barrier_seq)
            return barrier_seq

        future = self._executor.submit(root_id, _run)
        return int(await asyncio.wrap_future(future))

    def _reconcile_ownership(self, root_id: str) -> int:
        self._ensure_ownership_hydrated(root_id)
        return self._resolve_pending_events(root_id)

    def _schedule_written(
        self, written: EventWritten, *, run_id: Optional[str] = None,
    ) -> None:
        if self._bus is None:
            return
        loop = _event_journal_loop
        if loop is None or loop.is_closed():
            return
        coroutine = self._publish_written(written, run_id=run_id)
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        if running_loop is loop:
            loop.create_task(coroutine)
        else:
            asyncio.run_coroutine_threadsafe(coroutine, loop)

    def _schedule_failed(self, event: Event, exc: Exception) -> None:
        if self._bus is None:
            return
        loop = _event_journal_loop
        if loop is None or loop.is_closed():
            return
        coroutine = self._publish_failed_event(event, exc)
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        if running_loop is loop:
            loop.create_task(coroutine)
        else:
            asyncio.run_coroutine_threadsafe(coroutine, loop)

    async def _publish_failed_event(self, event: Event, exc: Exception) -> None:
        if self._bus is None:
            return
        payload = {
            "error_class": type(exc).__name__,
            "error_message": str(exc),
        }
        if event.event_id:
            payload["event_id"] = event.event_id
        await self._bus.publish(BusEvent(
            type=EVENT_JOURNAL_WRITE_FAILED,
            root_id=event.root_id,
            sid=event.sid,
            payload=payload,
            run_id=event.run_id,
            persist=False,
        ))

    def close(self) -> None:
        """Drain queued writes and stop all writer threads."""
        self._closed = True
        self._executor.shutdown(wait=True)

    async def _publish_written(
        self, written: EventWritten, *, run_id: Optional[str] = None,
    ) -> None:
        if self._bus is None:
            return
        payload: dict = {
            "event_type": written.event_type,
            "seq": written.seq,
        }
        if written.event_id:
            payload["event_id"] = written.event_id
        if written.data is not None:
            payload["data"] = written.data
        if written.source is not None:
            payload["source"] = written.source
        await self._bus.publish(BusEvent(
            type=EVENT_JOURNAL_WRITTEN,
            root_id=written.root_id,
            sid=written.sid,
            payload=payload,
            msg_id=written.msg_id,
            run_id=run_id,
            persist=False,
        ))

    async def _publish_failed(self, bus_event: BusEvent, exc: Exception) -> None:
        if self._bus is None:
            return
        payload = bus_event.payload if isinstance(bus_event.payload, dict) else {}
        event_id = payload.get("event_id")
        fail_payload = {
            "error_class": type(exc).__name__,
            "error_message": str(exc),
        }
        if isinstance(event_id, str) and event_id:
            fail_payload["event_id"] = event_id
        await self._bus.publish(BusEvent(
            type=EVENT_JOURNAL_WRITE_FAILED,
            root_id=bus_event.root_id,
            sid=bus_event.sid,
            payload=fail_payload,
            run_id=bus_event.run_id,
            persist=False,
        ))

    def _event_from_bus(self, bus_event: BusEvent) -> Event:
        payload = bus_event.payload or {}
        if not isinstance(payload, dict):
            raise ValueError("event_journal.event payload must be a dict")
        event_type = payload.get("event_type")
        data = payload.get("data")
        source = payload.get("source")
        if not isinstance(event_type, str) or not event_type:
            raise ValueError("event_journal.event requires payload.event_type")
        if not isinstance(data, dict):
            raise ValueError("event_journal.event requires payload.data")
        if not isinstance(source, str) or not source:
            raise ValueError("event_journal.event requires payload.source")
        event_id = payload.get("event_id")
        if not isinstance(event_id, str) or not event_id:
            event_id = data.get("uuid")
        if not isinstance(event_id, str) or not event_id:
            event_id = None
        turn_id = payload.get("turn_id")
        if not isinstance(turn_id, str) or not turn_id:
            turn_id = None
        message_id = payload.get("message_id")
        if not isinstance(message_id, str) or not message_id:
            message_id = None
        return Event(
            root_id=bus_event.root_id,
            sid=bus_event.sid,
            event_type=event_type,
            data=data,
            source=source,
            run_id=bus_event.run_id,
            event_id=event_id,
            turn_id=turn_id,
            message_id=message_id,
            cwd_override=payload.get("cwd_override"),
            dedupe_by_uid_only=bool(payload.get("dedupe_by_uid_only")),
        )

    @perf.timed_fn("ejw.append")
    def _append_event(self, event: Event) -> EventWritten:
        """Resolve a producer event into journal ownership and append it."""
        self._ensure_ownership_hydrated(event.root_id)
        resolved = self._resolve_event(event)
        written = self._append_resolved(resolved)
        duplicate_facts_changed = False
        if (
            written.seq == -1
            and written.msg_id
            and event.event_type in RENDER_EVENT_TYPES
        ):
            duplicate_facts_changed = self._resolve_matching_pending_duplicate(
                event, written.msg_id,
            )
        ownership_facts_changed = self._record_resolved_event(
            event,
            written.msg_id,
            journal_seq=written.seq if written.seq > 0 else None,
        )
        if duplicate_facts_changed or ownership_facts_changed:
            self._resolve_pending_events(event.root_id)
        return written

    def _resolve_event(self, event: Event) -> ResolvedEvent:
        if event.event_type == "turn_started":
            message_id = self._turn_started_message_id(event)
            ownership: EventOwnership = MetadataOwnership(msg_id=message_id)
        elif event.event_type in RENDER_EVENT_TYPES:
            message_id = self._resolve_render_message_id(event)
            if isinstance(message_id, str) and message_id:
                ownership = MessageOwnership(message_id)
            else:
                ownership = RootOwnership(reason="render_event_without_msg")
        else:
            ownership = MetadataOwnership(msg_id=event.message_id)
        return ResolvedEvent(
            root_id=event.root_id,
            sid=event.sid,
            event_type=event.event_type,
            data=event.data,
            source=event.source,
            ownership=ownership,
            run_id=event.run_id,
            event_id=event.event_id,
            cwd_override=event.cwd_override,
            dedupe_by_uid_only=event.dedupe_by_uid_only,
        )

    def _resolve_render_message_id(self, event: Event) -> Optional[str]:
        """Resolve ownership from strongest fact to weakest inference."""
        if event.source == FORK_BACKUP_SOURCE:
            return None
        message_id = event.message_id
        if isinstance(message_id, str) and message_id:
            return message_id
        causal_keys = self._causal_keys(event.data)
        for key in causal_keys:
            owner = self._owner_for_causal_key(event.root_id, key)
            if owner:
                return owner
        if causal_keys:
            return None
        message_id = self._message_id_for_turn(event)
        if message_id:
            return message_id
        source_ts = self._source_ts(event.data)
        if source_ts is not None:
            return self._message_id_for_source_ts(event.root_id, event.sid, source_ts)
        return None

    def _turn_started_message_id(self, event: Event) -> Optional[str]:
        message_id = event.message_id or event.data.get("message_id")
        return message_id if isinstance(message_id, str) and message_id else None

    def _record_resolved_event(
        self,
        event: Event,
        msg_id: Optional[str],
        *,
        journal_seq: Optional[int] = None,
    ) -> bool:
        if event.source == FORK_BACKUP_SOURCE:
            # Fork backup rows are not ownership facts: never pending
            # (unresolvable by design) and never causal-key donors —
            # recording their worker uuids/tool ids would let later rows
            # resolve onto a parent message.
            return False
        changed = False
        if event.event_type == "turn_started":
            changed = self._record_turn_started(event, msg_id) or changed
        if event.event_type == "event_ownership_resolved":
            event_seq = event.data.get("event_seq")
            if isinstance(event_seq, int) and event_seq > 0 and msg_id:
                pending = self._pending_events.get(event.root_id, {}).pop(event_seq, None)
                if pending is not None:
                    changed = self._record_resolved_event(pending, msg_id) or changed
            return changed
        provider_event_id = self._provider_event_id(event.data)
        if (
            event.event_type in RENDER_EVENT_TYPES
            and not msg_id
            and isinstance(journal_seq, int)
            and journal_seq > 0
        ):
            self._pending_events.setdefault(event.root_id, {})[journal_seq] = event
        if not msg_id:
            return changed
        if provider_event_id:
            key = (event.root_id, provider_event_id)
            if self._event_messages.get(key) != msg_id:
                self._event_messages[key] = msg_id
                changed = True
        for tool_use_id in self._tool_use_ids(event.data):
            key = (event.root_id, tool_use_id)
            if self._tool_messages.get(key) != msg_id:
                self._tool_messages[key] = msg_id
                changed = True
        delegate_id = self._delegate_id(event.data)
        if delegate_id:
            key = (event.root_id, delegate_id)
            if self._delegate_messages.get(key) != msg_id:
                self._delegate_messages[key] = msg_id
                changed = True
        return changed

    def _resolve_pending_events(self, root_id: str) -> int:
        pending = self._pending_events.get(root_id)
        if not pending:
            return 0
        resolved_count = 0
        while True:
            resolved_any = False
            for event_seq, event in list(pending.items()):
                msg_id = self._resolve_render_message_id(event)
                if not msg_id:
                    continue
                self._append_ownership_resolution(
                    event,
                    event_seq,
                    msg_id,
                    reason="ownership_facts_available",
                )
                pending.pop(event_seq, None)
                self._record_resolved_event(event, msg_id)
                resolved_any = True
                resolved_count += 1
            if not resolved_any:
                break
        return resolved_count

    def _resolve_matching_pending_duplicate(
        self, event: Event, msg_id: str,
    ) -> bool:
        pending = self._pending_events.get(event.root_id, {})
        changed = False
        for event_seq, pending_event in list(pending.items()):
            if (
                pending_event.event_type != event.event_type
                or pending_event.data != event.data
            ):
                continue
            self._append_ownership_resolution(
                pending_event,
                event_seq,
                msg_id,
                reason="stronger_fact_on_duplicate_event",
            )
            pending.pop(event_seq, None)
            changed = self._record_resolved_event(pending_event, msg_id) or changed
        return changed

    def _append_ownership_resolution(
        self,
        event: Event,
        event_seq: int,
        msg_id: str,
        *,
        reason: str,
    ) -> None:
        event_id = self._provider_event_id(event.data)
        seq = self._append_metadata_event(
            event.root_id,
            sid=event.sid,
            event_type="event_ownership_resolved",
            data={
                "event_seq": event_seq,
                **({"event_id": event_id} if event_id else {}),
                "message_id": msg_id,
                "reason": reason,
            },
            source="event_journal_writer",
            msg_id=msg_id,
            cwd_override=event.cwd_override,
        )
        if seq == -1:
            return
        self._schedule_written(EventWritten(
            root_id=event.root_id,
            sid=event.sid,
            event_type="event_ownership_resolved",
            seq=seq,
            msg_id=msg_id,
        ))

    def _record_turn_started(self, event: Event, msg_id: Optional[str]) -> bool:
        turn_id = event.data.get("turn_id") or event.turn_id or event.run_id
        source_ts = self._source_ts(event.data)
        if not (
            isinstance(turn_id, str) and turn_id
            and isinstance(msg_id, str) and msg_id
            and source_ts is not None
        ):
            return False
        changed = False
        turn_key = (event.root_id, turn_id)
        if self._turn_messages.get(turn_key) != msg_id:
            self._turn_messages[turn_key] = msg_id
            changed = True
        key = (event.root_id, event.sid)
        boundaries = self._turn_boundaries.setdefault(key, [])
        boundary = TurnBoundary(source_ts, turn_id, msg_id)
        if boundary not in boundaries:
            boundaries.insert(bisect_right(boundaries, boundary), boundary)
            changed = True
        return changed

    def _ensure_ownership_hydrated(self, root_id: str) -> None:
        if root_id in self._ownership_hydrated_roots:
            return
        ownership_facts_changed = self._hydrate_snapshot_turn_boundaries(root_id)
        after_seq = 0
        while True:
            rows, next_seq, has_more = event_ingester.read_events(
                root_id,
                after_seq=after_seq,
                limit=10_000,
            )
            for row in rows:
                replay = Event(
                    root_id=root_id,
                    sid=str(row.get("sid") or root_id),
                    event_type=str(row.get("type") or "unknown"),
                    data=row.get("data") if isinstance(row.get("data"), dict) else {},
                    source=str(row.get("source") or "journal"),
                    run_id=row.get("run_id"),
                    message_id=row.get("msg_id"),
                )
                ownership_facts_changed = self._record_resolved_event(
                    replay,
                    row.get("msg_id"),
                    journal_seq=int(row.get("seq") or 0) or None,
                ) or ownership_facts_changed
            if not has_more or not rows:
                break
            after_seq = int(rows[-1].get("seq") or after_seq)
        self._ownership_hydrated_roots.add(root_id)
        if ownership_facts_changed:
            self._resolve_pending_events(root_id)

    def _hydrate_snapshot_turn_boundaries(self, root_id: str) -> bool:
        """Seed historical turn starts from the collapsed session snapshot.

        Native-file tailing can replay provider rows before a live
        ``turn_started`` fact exists. Assistant scaffold timestamps are the
        durable historical start boundaries needed to own those rows on their
        first append.
        """
        import session_store

        root = session_store.get_root_tree(root_id)
        if not isinstance(root, dict):
            return False
        changed = False
        nodes = [root, *session_store._walk_forks(root)]
        for node in nodes:
            sid = node.get("id")
            if not isinstance(sid, str) or not sid:
                continue
            boundaries = self._turn_boundaries.setdefault((root_id, sid), [])
            for msg in node.get("messages") or []:
                msg_id = msg.get("id") if isinstance(msg, dict) else None
                if (
                    not isinstance(msg, dict)
                    or msg.get("role") != "assistant"
                    or not isinstance(msg_id, str)
                    or not msg_id
                ):
                    continue
                source_ts = self._source_ts({"timestamp": msg.get("timestamp")})
                if source_ts is None:
                    continue
                boundary = TurnBoundary(
                    source_ts,
                    f"snapshot:{msg_id}",
                    msg_id,
                )
                if boundary not in boundaries:
                    boundaries.insert(bisect_right(boundaries, boundary), boundary)
                    changed = True
        return changed

    def _owner_for_causal_key(
        self, root_id: str, key: tuple[str, str],
    ) -> Optional[str]:
        kind, value = key
        if kind == "event":
            return self._event_messages.get((root_id, value))
        if kind == "tool":
            return self._tool_messages.get((root_id, value))
        if kind == "delegate":
            return self._delegate_messages.get((root_id, value))
        return None

    @classmethod
    def _causal_keys(cls, data: dict) -> list[tuple[str, str]]:
        payload = cls._provider_payload(data)
        keys: list[tuple[str, str]] = []
        parent_event_id = payload.get("parent_event_id")
        if not parent_event_id and payload.get("isSidechain") is True:
            parent_event_id = payload.get("parentUuid")
        if isinstance(parent_event_id, str) and parent_event_id:
            keys.append(("event", parent_event_id))
        parent_tool_use_id = payload.get("parent_tool_use_id")
        if isinstance(parent_tool_use_id, str) and parent_tool_use_id:
            keys.append(("tool", parent_tool_use_id))
        delegate_id = cls._delegate_id(data)
        if delegate_id:
            keys.append(("delegate", delegate_id))
        return keys

    @classmethod
    def _provider_payload(cls, data: dict) -> dict:
        if not isinstance(data, dict):
            return {}
        event = data.get("event")
        if isinstance(event, dict) and isinstance(event.get("data"), dict):
            return event["data"]
        inner = data.get("data")
        if isinstance(inner, dict) and inner.get("uuid"):
            return inner
        return data

    @classmethod
    def _provider_event_id(cls, data: dict) -> Optional[str]:
        value = cls._provider_payload(data).get("uuid")
        return value if isinstance(value, str) and value else None

    @classmethod
    def _tool_use_ids(cls, data: dict) -> list[str]:
        payload = cls._provider_payload(data)
        message = payload.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            return []
        return [
            block["id"]
            for block in content
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_use"
                and isinstance(block.get("id"), str)
                and block.get("id")
            )
        ]

    @staticmethod
    def _delegate_id(data: dict) -> Optional[str]:
        if not isinstance(data, dict):
            return None
        value = data.get("delegation_id") or data.get("delegate_id")
        return value if isinstance(value, str) and value else None

    @classmethod
    def _source_ts(cls, data: dict) -> Optional[datetime]:
        payload = cls._provider_payload(data)
        value = payload.get("source_ts") or payload.get("timestamp")
        if not isinstance(value, str) or not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        # Claude native JSONL and session.json use naive local wall time.
        # datetime.astimezone() intentionally interprets a naive value in the
        # host timezone, including the historical DST offset for that date.
        return parsed.astimezone(timezone.utc)

    def _message_id_for_source_ts(
        self, root_id: str, sid: str, source_ts: datetime,
    ) -> Optional[str]:
        boundaries = self._turn_boundaries.get((root_id, sid), [])
        if not boundaries:
            return None
        probe = TurnBoundary(source_ts, "\uffff", "\uffff")
        index = bisect_right(boundaries, probe) - 1
        return boundaries[index].msg_id if index >= 0 else None

    def _message_id_for_turn(self, event: Event) -> Optional[str]:
        turn_id = event.turn_id or event.run_id
        if not isinstance(turn_id, str) or not turn_id:
            return None
        return self._turn_messages.get((event.root_id, turn_id))

    def _append_resolved(self, event: ResolvedEvent) -> EventWritten:
        """Append an internally-resolved event to events.jsonl."""
        ownership = event.ownership
        if isinstance(ownership, MessageOwnership):
            seq = self._append_message_event(
                event.root_id,
                sid=event.sid,
                event_type=event.event_type,
                data=event.data,
                source=event.source,
                msg_id=ownership.msg_id,
                run_id=event.run_id,
                cwd_override=event.cwd_override,
                dedupe_by_uid_only=event.dedupe_by_uid_only,
            )
            msg_id: Optional[str] = ownership.msg_id
        elif isinstance(ownership, RootOwnership):
            seq = self._append_root_event(
                event.root_id,
                sid=event.sid,
                event_type=event.event_type,
                data=event.data,
                source=event.source,
                run_id=event.run_id,
                cwd_override=event.cwd_override,
                dedupe_by_uid_only=event.dedupe_by_uid_only,
            )
            msg_id = None
        elif isinstance(ownership, MetadataOwnership):
            seq = self._append_metadata_event(
                event.root_id,
                sid=event.sid,
                event_type=event.event_type,
                data=event.data,
                source=event.source,
                run_id=event.run_id,
                msg_id=ownership.msg_id,
                cwd_override=event.cwd_override,
                dedupe_by_uid_only=event.dedupe_by_uid_only,
            )
            msg_id = ownership.msg_id
        else:
            raise TypeError(f"unsupported event ownership: {ownership!r}")
        return EventWritten(
            root_id=event.root_id,
            sid=event.sid,
            event_type=event.event_type,
            seq=seq,
            msg_id=msg_id,
            event_id=event.event_id,
            data=event.data,
            source=event.source,
        )

    def _append_message_event(
        self,
        root_id: str,
        *,
        sid: str,
        event_type: str,
        data: dict,
        source: str,
        msg_id: str,
        run_id: Optional[str] = None,
        cwd_override: Optional[str] = None,
        dedupe_by_uid_only: bool = False,
    ) -> int:
        """Append an event owned by a chat message.

        Render events should use this path.  A missing message id is a
        programming error; callers that really mean "root/unattached" must use
        append_root_event explicitly.
        """
        if not isinstance(msg_id, str) or not msg_id:
            raise ValueError("_append_message_event requires a non-empty msg_id")
        return event_ingester.ingest(
            root_id,
            sid=sid,
            event_type=event_type,
            data=data,
            source=source,
            run_id=run_id,
            msg_id=msg_id,
            cwd_override=cwd_override,
            dedupe_by_uid_only=dedupe_by_uid_only,
        )

    def _append_root_event(
        self,
        root_id: str,
        *,
        sid: str,
        event_type: str,
        data: dict,
        source: str,
        run_id: Optional[str] = None,
        cwd_override: Optional[str] = None,
        dedupe_by_uid_only: bool = False,
    ) -> int:
        """Append an explicitly unattached/root event."""
        return event_ingester.ingest(
            root_id,
            sid=sid,
            event_type=event_type,
            data=data,
            source=source,
            run_id=run_id,
            msg_id=None,
            cwd_override=cwd_override,
            dedupe_by_uid_only=dedupe_by_uid_only,
        )

    def _append_metadata_event(
        self,
        root_id: str,
        *,
        sid: str,
        event_type: str,
        data: dict,
        source: str,
        run_id: Optional[str] = None,
        msg_id: Optional[str] = None,
        cwd_override: Optional[str] = None,
        dedupe_by_uid_only: bool = False,
    ) -> int:
        """Append a non-render/control event.

        Metadata may be message-correlated or session/root-scoped, so msg_id is
        optional here by design.
        """
        return event_ingester.ingest(
            root_id,
            sid=sid,
            event_type=event_type,
            data=data,
            source=source,
            run_id=run_id,
            msg_id=msg_id,
            cwd_override=cwd_override,
            dedupe_by_uid_only=dedupe_by_uid_only,
        )


@dataclass
class _MessageCacheEntry:
    events: list[dict]
    frontend_events: Optional[list[dict]]
    byte_start: int
    byte_end: int
    seq_end: int
    res_version: int


class EventJournalReader:
    """Runtime source of truth for journal reads and cached projections."""

    def __init__(self, *, message_cache_size: int = 20) -> None:
        self._message_cache_size = message_cache_size
        self._message_cache: OrderedDict[
            tuple[str, str, str], _MessageCacheEntry
        ] = OrderedDict()
        self._message_cache_lock = threading.RLock()

    @staticmethod
    def _context_id(
        session_id: str,
        *,
        fork_id: Optional[str] = None,
        worker_id: Optional[str] = None,
        delegate_id: Optional[str] = None,
    ) -> str:
        provided = [
            value for value in (fork_id, worker_id, delegate_id)
            if isinstance(value, str) and value
        ]
        if len(provided) > 1:
            raise ValueError("provide only one of fork_id, worker_id, delegate_id")
        return provided[0] if provided else session_id

    def read_session_events(
        self,
        session_id: str,
        *,
        after_seq: int = 0,
        limit: int = 500,
        fork_id: Optional[str] = None,
        worker_id: Optional[str] = None,
        delegate_id: Optional[str] = None,
        message_id: Optional[str] = None,
    ) -> tuple[list[dict], int, bool]:
        context_id = self._context_id(
            session_id,
            fork_id=fork_id,
            worker_id=worker_id,
            delegate_id=delegate_id,
        )
        return self.read_events(
            session_id,
            after_seq=after_seq,
            limit=limit,
            sid_filter=context_id,
            msg_id_filter=message_id,
        )

    def read_message_events(
        self,
        session_id: str,
        message_id: str,
        *,
        limit: int = 10_000,
        fork_id: Optional[str] = None,
        worker_id: Optional[str] = None,
        delegate_id: Optional[str] = None,
    ) -> list[dict]:
        cached = self._ensure_message_cache(
            session_id,
            message_id,
            fork_id=fork_id,
            worker_id=worker_id,
            delegate_id=delegate_id,
        )
        if cached is None:
            return []
        return list(cached.events[:limit])

    def _ensure_message_cache(
        self,
        session_id: str,
        message_id: str,
        *,
        fork_id: Optional[str] = None,
        worker_id: Optional[str] = None,
        delegate_id: Optional[str] = None,
    ) -> Optional[_MessageCacheEntry]:
        context_id = self._context_id(
            session_id,
            fork_id=fork_id,
            worker_id=worker_id,
            delegate_id=delegate_id,
        )
        summaries = self.message_event_summaries(
            session_id,
            sid_filter=context_id,
        )
        summary = summaries.get(message_id)
        if not summary:
            return None
        resolutions = event_ingester.ownership_resolutions(session_id)
        res_version = len(resolutions)
        key = (session_id, context_id, message_id)
        # Effective bounds: summary byte_start/byte_end already span the
        # message's own contiguous run UNION any resolved-in orphan
        # ranges (folded in event_ingester). A single range read + an
        # effective-owner filter reconstructs the message — no full scan.
        byte_start = int(summary.get("byte_start") or 0)
        byte_end = int(summary.get("byte_end") or byte_start)
        seq_end = int(summary.get("seq_end") or 0)
        with self._message_cache_lock:
            cached = self._message_cache.get(key)
            grow_only = (
                cached is not None
                and cached.res_version == res_version
                and cached.byte_start == byte_start
                and cached.byte_end <= byte_end
            )
            if grow_only and cached.byte_end < byte_end:
                # Hot streaming path: same span start, no new resolution
                # — only append the new tail and filter it.
                cached.events.extend(self._read_owned_range(
                    session_id, message_id,
                    cached.byte_end, byte_end,
                    resolutions=resolutions,
                ))
                cached.byte_end = byte_end
                cached.seq_end = seq_end
                cached.frontend_events = None
            elif not grow_only:
                # Cold, shrunk, start moved earlier, or a new resolution
                # landed (which can reassign a mid-span row) — full
                # effective re-read.
                events = self._read_owned_range(
                    session_id, message_id,
                    byte_start, byte_end,
                    resolutions=resolutions,
                )
                cached = _MessageCacheEntry(
                    events=events,
                    frontend_events=None,
                    byte_start=byte_start,
                    byte_end=byte_end,
                    seq_end=seq_end,
                    res_version=res_version,
                )
                self._message_cache[key] = cached
            self._message_cache.move_to_end(key)
            while len(self._message_cache) > self._message_cache_size:
                self._message_cache.popitem(last=False)
            return cached

    def read_message_frontend_events(
        self,
        session_id: str,
        message_id: str,
        *,
        fork_id: Optional[str] = None,
        worker_id: Optional[str] = None,
        delegate_id: Optional[str] = None,
    ) -> list[dict]:
        cached = self._ensure_message_cache(
            session_id,
            message_id,
            fork_id=fork_id,
            worker_id=worker_id,
            delegate_id=delegate_id,
        )
        if cached is None:
            return []
        with self._message_cache_lock:
            if cached.frontend_events is None:
                cached.frontend_events = self._to_frontend_events(cached.events)
            return list(cached.frontend_events)

    def _read_owned_range(
        self,
        session_id: str,
        message_id: str,
        byte_start: int,
        byte_end: int,
        *,
        resolutions: dict[int, str],
    ) -> list[dict]:
        """Read `[byte_start, byte_end)` and keep only rows whose
        EFFECTIVE owner is `message_id` — `resolutions[seq]` when a
        write-time fact reassigned the row, else its on-disk `msg_id`.
        Reassigned rows are stamped with the resolved owner. Drops
        `event_ownership_resolved` facts (metadata, not render rows).

        No sid filter: ownership is decided by effective msg_id alone. A
        resolved-in orphan may carry a different on-disk `sid` than the
        owning message's context, so filtering by `context_id` here would
        wrongly drop it (matches the former resolved-branch semantics,
        which filtered by msg_id only)."""
        raw = self._read_raw_range(
            session_id, byte_start, byte_end, context_id=None,
        )
        out: list[dict] = []
        for row in raw:
            if row.get("type") == "event_ownership_resolved":
                continue
            if row.get("source") == FORK_BACKUP_SOURCE:
                continue
            seq = row.get("seq")
            owner = resolutions.get(seq) if isinstance(seq, int) else None
            if owner is None:
                owner = row.get("msg_id")
            if owner != message_id:
                continue
            if row.get("msg_id") != message_id:
                row = dict(row)
                row["msg_id"] = message_id
            out.append(row)
        return out

    def _read_raw_range(
        self,
        session_id: str,
        byte_start: int,
        byte_end: int,
        *,
        context_id: Optional[str] = None,
        message_id: Optional[str] = None,
    ) -> list[dict]:
        from paths import ba_home

        path = ba_home() / "sessions" / session_id / "events.jsonl"
        if not path.exists() or byte_end <= byte_start:
            return []
        events: list[dict] = []
        with path.open("rb") as file:
            file.seek(byte_start)
            while file.tell() < byte_end:
                raw = file.readline()
                if not raw:
                    break
                try:
                    entry = json.loads(raw.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    continue
                if context_id and entry.get("sid") != context_id:
                    continue
                if message_id and entry.get("msg_id") != message_id:
                    continue
                events.append(entry)
        return events

    async def watch_entries(
        self,
        session_id: str,
        stop_event: asyncio.Event,
        on_entry: Callable[[dict], Awaitable[None]],
        *,
        poll_interval: float = 0.05,
    ) -> None:
        """Read appended journal blocks and deliver entries in file order."""
        import logging as _logging
        _wl = _logging.getLogger(__name__)
        offset = 0
        consecutive_read_failures = 0
        _MAX_CONSECUTIVE_READ_FAILURES = 10
        while not stop_event.is_set():
            try:
                entries, offset = await asyncio.to_thread(
                    self._read_appended_entries,
                    session_id,
                    offset,
                )
                consecutive_read_failures = 0
            except Exception:
                consecutive_read_failures += 1
                _wl.exception(
                    "watch_entries: _read_appended_entries failed for %s (%d/%d)",
                    session_id[:8],
                    consecutive_read_failures, _MAX_CONSECUTIVE_READ_FAILURES,
                )
                if consecutive_read_failures >= _MAX_CONSECUTIVE_READ_FAILURES:
                    _wl.critical(
                        "watch_entries: giving up after %d consecutive read failures for %s",
                        consecutive_read_failures, session_id[:8],
                    )
                    raise
                await asyncio.sleep(0.5)
                continue
            for entry in entries:
                try:
                    await on_entry(entry)
                except Exception:
                    _wl.exception(
                        "watch_entries: on_entry failed for entry seq=%s in %s",
                        entry.get("seq"), session_id[:8],
                    )
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=poll_interval)
            except asyncio.TimeoutError:
                pass

    def _read_appended_entries(
        self, session_id: str, byte_offset: int,
    ) -> tuple[list[dict], int]:
        from paths import ba_home

        path = ba_home() / "sessions" / session_id / "events.jsonl"
        if not path.exists():
            return [], byte_offset
        if path.stat().st_size < byte_offset:
            byte_offset = 0
        entries: list[dict] = []
        with path.open("rb") as file:
            file.seek(byte_offset)
            while True:
                line_start = file.tell()
                raw = file.readline()
                if not raw:
                    break
                if not raw.endswith(b"\n"):
                    return entries, line_start
                try:
                    entry = json.loads(raw.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    continue
                if isinstance(entry, dict):
                    entries.append(entry)
            return entries, file.tell()

    @staticmethod
    def _to_frontend_events(rows: list[dict]) -> list[dict]:
        # Mirror the write-path render gate (`_RENDER_TREE_ETYPES` in
        # orchs/base.py): only event types the frontend renders belong in
        # a message's events list. Framing/control rows (`turn_start`,
        # `turn_started`, `turn_complete`, `trace_step`) and lifecycle rows
        # are stamped with a msg_id in events.jsonl for ownership/recovery
        # but MUST NOT surface as renderable events — otherwise a native
        # reload diverges from the live render tree (e.g. the frontend
        # shows "unknown event: event.turn_started").
        #
        # UUID dedup: streaming writes multiple rows for the same UUID
        # (progressively longer text).  The render tree keeps one entry per
        # UUID (latest wins).  Without this dedup the native-only path
        # would return every snapshot and the frontend would render
        # triplicated text bubbles.
        events: list[dict] = []
        uuid_idx: dict[str, int] = {}
        for entry in rows:
            event_type = entry.get("type")
            if event_type not in RENDER_EVENT_TYPES:
                continue
            data = entry.get("data", {})
            if event_type == "manager_event" and isinstance(data, dict):
                inner = data.get("event")
                if isinstance(inner, dict):
                    ev = inner
                else:
                    continue
            else:
                ev = {"type": event_type, "data": data}
            uid = (ev.get("data") or {}).get("uuid") if isinstance(ev, dict) else None
            if uid and uid in uuid_idx:
                events[uuid_idx[uid]] = ev
            else:
                if uid:
                    uuid_idx[uid] = len(events)
                events.append(ev)
        return events

    def read_unattached_events(
        self,
        session_id: str,
        *,
        after_seq: int = 0,
        render_only: bool = False,
        fork_id: Optional[str] = None,
        worker_id: Optional[str] = None,
        delegate_id: Optional[str] = None,
    ) -> list[dict]:
        context_id = self._context_id(
            session_id,
            fork_id=fork_id,
            worker_id=worker_id,
            delegate_id=delegate_id,
        )
        rows = [
            e for e in self.read_orphan_events(session_id, after_seq=after_seq)
            if e.get("sid") == context_id
        ]
        if render_only:
            rows = [e for e in rows if e.get("type") in RENDER_EVENT_TYPES]
        return rows

    def read_frontend_events(
        self,
        session_id: str,
        *,
        fork_id: Optional[str] = None,
        worker_id: Optional[str] = None,
        delegate_id: Optional[str] = None,
        message_id: Optional[str] = None,
    ) -> list[dict]:
        context_id = self._context_id(
            session_id,
            fork_id=fork_id,
            worker_id=worker_id,
            delegate_id=delegate_id,
        )
        if message_id:
            return self.read_message_frontend_events(
                session_id,
                message_id,
                fork_id=fork_id,
                worker_id=worker_id,
                delegate_id=delegate_id,
            )
        return self.read_ws_events(session_id, sid_filter=context_id)

    def read_events(
        self,
        root_id: str,
        *,
        after_seq: int = 0,
        limit: int = 500,
        sid_filter: Optional[str] = None,
        msg_id_filter: Optional[str] = None,
    ) -> tuple[list[dict], int, bool]:
        resolutions = self._ownership_resolutions(root_id)
        if resolutions:
            rows, _, _ = event_ingester.read_events(
                root_id,
                after_seq=after_seq,
                limit=999_999,
                sid_filter=sid_filter,
            )
            rows = self._apply_ownership_resolutions(rows, resolutions)
            if msg_id_filter:
                rows = [
                    row for row in rows
                    if row.get("msg_id") == msg_id_filter
                ]
            total = len(rows)
            return rows[:limit], total, total > limit
        return event_ingester.read_events(
            root_id,
            after_seq=after_seq,
            limit=limit,
            sid_filter=sid_filter,
            msg_id_filter=msg_id_filter,
        )

    def read_orphan_events(self, root_id: str, *, after_seq: int = 0) -> list[dict]:
        if self._ownership_resolutions(root_id):
            rows, _, _ = self.read_events(
                root_id,
                after_seq=after_seq,
                limit=999_999,
            )
            return [
                row for row in rows
                if (
                    not row.get("msg_id")
                    and row.get("type") != "event_ownership_resolved"
                )
            ]
        return event_ingester.read_orphan_events(root_id, after_seq=after_seq)

    @staticmethod
    def _ownership_resolutions(root_id: str) -> dict[tuple[str, object], str]:
        """Seq-keyed ownership resolution map for the in-memory remap
        path. Backed by the ingester's incrementally-maintained
        write-time fact map (no full scan). Current `event_journal`
        always stamps `event_seq` on resolution rows, so the legacy
        ("event", event_id) key is no longer produced."""
        return {
            ("seq", seq): msg_id
            for seq, msg_id in event_ingester.ownership_resolutions(root_id).items()
        }

    @staticmethod
    def _apply_ownership_resolutions(
        rows: list[dict], resolutions: dict[tuple[str, object], str],
    ) -> list[dict]:
        resolved_rows: list[dict] = []
        for row in rows:
            if row.get("type") == "event_ownership_resolved":
                resolved_rows.append(row)
                continue
            data = row.get("data")
            event_id = (
                EventJournalWriter._provider_event_id(data)
                if isinstance(data, dict)
                else None
            )
            message_id = resolutions.get(("event", event_id)) if event_id else None
            event_seq = row.get("seq")
            if isinstance(event_seq, int):
                message_id = resolutions.get(("seq", event_seq), message_id)
            if message_id and row.get("msg_id") != message_id:
                row = dict(row)
                row["msg_id"] = message_id
            resolved_rows.append(row)
        return resolved_rows

    def read_ws_events(
        self,
        root_id: str,
        *,
        sid_filter: Optional[str] = None,
        msg_id_filter: Optional[str] = None,
    ) -> list[dict]:
        if msg_id_filter:
            rows = self.read_message_events(
                root_id,
                msg_id_filter,
                fork_id=(
                    sid_filter
                    if isinstance(sid_filter, str) and sid_filter != root_id
                    else None
                ),
            )
            return self._to_frontend_events(rows)
        return event_ingester.read_ws_events(
            root_id,
            sid_filter=sid_filter,
            msg_id_filter=msg_id_filter,
        )

    def read_ws_events_range(
        self,
        root_id: str,
        byte_start: int,
        byte_end: int,
    ) -> list[dict]:
        return event_ingester.read_ws_events_range(root_id, byte_start, byte_end)

    def message_event_summaries(
        self,
        root_id: str,
        *,
        sid_filter: Optional[str] = None,
        tail: int = 3,
    ) -> dict[str, dict]:
        return event_ingester.message_event_summaries(
            root_id,
            sid_filter=sid_filter,
            tail=tail,
        )

    def current_seq(self, root_id: str) -> Optional[int]:
        return event_ingester.current_seq(root_id)

    def read_through(self, root_id: str, required_seq: int) -> int:
        """Verify the journal is readable through a writer commit barrier."""
        cursor = event_ingester.cursor(root_id)
        if cursor < required_seq:
            raise RuntimeError(
                f"journal {root_id} readable through {cursor}, "
                f"required {required_seq}",
            )
        return cursor

    def cursor(self, root_id: str) -> int:
        return event_ingester.cursor(root_id)

    def max_seq_by_context(self, session_id: str) -> dict[str, int]:
        return event_ingester.max_seq_by_sid(session_id)

    def render_seq_by_context(self, session_id: str) -> dict[str, int]:
        return event_ingester.render_seq_by_sid(session_id)


event_journal_writer = EventJournalWriter()
event_journal_reader = EventJournalReader()
