"""Shared bounded-outbox / slow-consumer-disconnect WebSocket transport.

A per-connection send queue with a bounded size: once full, a brief grace
period (`enqueue_timeout_s`) is given for the client to drain before the
connection is treated as a slow consumer and closed — protects the process
from unbounded memory growth when a client can't keep up with live event
fan-out, independent of which route is doing the sending.

Originally lived only in `ws_chat.py` (the legacy `/ws/chat` transport).
Moved here so `adapter_api.py`'s `/ws/v2/surface` route gets the identical
bounded-queue/slow-consumer protection via the SAME class rather than a
second parallel implementation — both routes sit outside
`backend/adapters/`'s import boundary (see
`backend/scripts/test_adapter_boundaries.py`), so a shared sibling module
is architecturally clean. `ws_chat.py` keeps its own `_WebSocketOutbox`
name as a local alias onto this module's class (`from backend.ws_outbox
import WebSocketOutbox as _WebSocketOutbox`) — every existing behavior/
perf-instrumentation/test here is an unmodified move, not a rewrite.
"""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import logging
import time

import perf
import lag_watchdog
from ws_serialization import SerializedWebSocketFrame, dumps_ws_json, metric_event_type

logger = logging.getLogger(__name__)

WS_OUTBOX_MAX_ITEMS = 256
WS_OUTBOX_ENQUEUE_TIMEOUT_SECONDS = 2.0
WS_OUTBOX_CLOSE_TIMEOUT_SECONDS = 1.0

_WS_FRAME_IDS = itertools.count(1)


class WebSocketOutbox:
    def __init__(
        self,
        websocket,
        *,
        on_close,
        max_items: int = WS_OUTBOX_MAX_ITEMS,
        enqueue_timeout_s: float = WS_OUTBOX_ENQUEUE_TIMEOUT_SECONDS,
        close_timeout_s: float = WS_OUTBOX_CLOSE_TIMEOUT_SECONDS,
    ) -> None:
        self._websocket = websocket
        self._on_close = on_close
        self._queue: asyncio.Queue[
            tuple[
                int,
                float,
                str,
                dict | None,
                int,
                SerializedWebSocketFrame | None,
                bytes | None,
            ] | None
        ] = perf.LaggedQueue(
            maxsize=max_items,
            _perf_name="ws.outbox",
        )
        self._enqueue_timeout_s = enqueue_timeout_s
        self._close_timeout_s = close_timeout_s
        self._closed = False
        self._closed_event = asyncio.Event()
        self._connection_id = hashlib.blake2s(
            str(id(websocket)).encode("ascii"),
            digest_size=4,
        ).hexdigest()
        self._writer_task = asyncio.create_task(self._writer())

    async def send(
        self,
        event_dict: dict,
        serialized: SerializedWebSocketFrame | None = None,
    ) -> bool:
        event_type = event_dict.get("type") if isinstance(event_dict, dict) else None
        return await self._enqueue(
            event_type=event_type if isinstance(event_type, str) else "unknown",
            event_dict=event_dict,
            serialized=serialized,
            binary=None,
        )

    async def send_binary(self, payload: bytes, *, event_type: str) -> bool:
        if not isinstance(payload, bytes) or not payload:
            raise ValueError("binary WebSocket payload must be non-empty bytes")
        return await self._enqueue(
            event_type=event_type,
            event_dict=None,
            serialized=None,
            binary=payload,
        )

    async def _enqueue(
        self,
        *,
        event_type: str,
        event_dict: dict | None,
        serialized: SerializedWebSocketFrame | None,
        binary: bytes | None,
    ) -> bool:
        if self._closed:
            perf.record_count("ws.outbox.rejected_closed")
            return False
        perf.record_count("ws.outbox.enqueue_depth", self._queue.qsize())
        queued_item = (
            next(_WS_FRAME_IDS),
            time.perf_counter(),
            event_type,
            event_dict,
            self._queue.qsize(),
            serialized,
            binary,
        )
        try:
            self._queue.put_nowait(queued_item)
            return True
        except asyncio.QueueFull:
            pass

        wait_started = time.perf_counter()
        put_task = asyncio.create_task(self._queue.put(queued_item))
        close_task = asyncio.create_task(self._closed_event.wait())
        try:
            done, _ = await asyncio.wait(
                (put_task, close_task),
                timeout=self._enqueue_timeout_s,
                return_when=asyncio.FIRST_COMPLETED,
            )
            perf.record(
                "ws.outbox.enqueue_wait",
                (time.perf_counter() - wait_started) * 1000.0,
            )
            if close_task in done or self._closed:
                perf.record_count("ws.outbox.rejected_closed")
                return False
            if put_task in done:
                return True
            perf.record_count("ws.outbox.rejected_timeout")
            logger.warning(
                "closing slow WebSocket: outbox enqueue timeout type=%s depth=%d",
                event_type,
                self._queue.qsize(),
            )
            await self.close()
            return False
        finally:
            for task in (put_task, close_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(put_task, close_task, return_exceptions=True)

    async def close(self) -> None:
        await self._close(cancel_writer=True)

    async def _close(self, *, cancel_writer: bool) -> None:
        if self._closed:
            return
        self._closed = True
        self._closed_event.set()
        try:
            await self._on_close()
        except Exception:
            logger.debug("WebSocket outbox unregister failed", exc_info=True)
        try:
            await asyncio.wait_for(
                self._websocket.close(),
                timeout=self._close_timeout_s,
            )
        except asyncio.TimeoutError:
            logger.debug("WebSocket close timed out")
        except Exception:
            pass
        if cancel_writer and self._writer_task is not asyncio.current_task():
            self._writer_task.cancel()

    async def wait_closed(self) -> None:
        try:
            await self._writer_task
        except asyncio.CancelledError:
            pass

    async def _writer(self) -> None:
        while True:
            queued_item = await self._queue.get()
            if queued_item is None:
                return
            (
                frame_id,
                queued_at,
                event_type,
                event_dict,
                enqueue_depth,
                serialized,
                binary,
            ) = queued_item
            writer_start_ms = (time.perf_counter() - queued_at) * 1000.0
            perf.record("ws.outbox.writer_start", writer_start_ms)
            metric_type = metric_event_type({"type": event_type})
            perf.record(
                f"ws.outbox.writer_start.type.{metric_type}",
                writer_start_ms,
            )
            if binary is not None:
                await self._write_binary(
                    binary,
                    event_type=event_type,
                    frame_id=frame_id,
                    queued_at=queued_at,
                    writer_dequeued_at=time.perf_counter(),
                    enqueue_depth=enqueue_depth,
                )
                if self._closed:
                    return
                continue
            if event_dict is None:
                raise RuntimeError("text WebSocket queue item is missing its event")
            await self._write_one(
                event_dict,
                frame_id=frame_id,
                queued_at=queued_at,
                writer_dequeued_at=time.perf_counter(),
                enqueue_depth=enqueue_depth,
                serialized=serialized,
            )
            if self._closed:
                return

    async def _write_binary(
        self,
        payload: bytes,
        *,
        event_type: str,
        frame_id: int,
        queued_at: float,
        writer_dequeued_at: float,
        enqueue_depth: int,
    ) -> None:
        send_t = time.perf_counter()
        try:
            perf.record_count("ws.phase.payload_bytes", len(payload))
            wire_t = time.perf_counter()
            perf.record("ws.phase.writer_dequeue_wire_start", (
                wire_t - writer_dequeued_at
            ) * 1000.0)
            await self._websocket.send_bytes(payload)
            wire_ms = (time.perf_counter() - wire_t) * 1000.0
            wire_end_at = time.perf_counter()
            self._record_lag_overlap(queued_at, wire_end_at)
            perf.record("ws.phase.wire_start_resume", wire_ms)
            perf.record("ws.send_binary.wire", wire_ms)
            timeline_total = (
                writer_dequeued_at - queued_at
                + wire_t - writer_dequeued_at
                + wire_end_at - wire_t
            )
            perf.record("ws.phase.timeline_total", timeline_total * 1000.0)
            perf.record("ws.phase.timeline_elapsed", (
                wire_end_at - queued_at
            ) * 1000.0)
            if wire_ms > 250.0:
                logger.warning(
                    "slow binary WebSocket wire type=%s elapsed_ms=%.1f bytes=%d "
                    "conn=%s frame=%d enqueue_depth=%d current_depth=%d",
                    event_type,
                    wire_ms,
                    len(payload),
                    self._connection_id,
                    frame_id,
                    enqueue_depth,
                    self._queue.qsize(),
                )
        except Exception as exc:
            logger.debug(
                "Binary WebSocket send failed type=%s error=%s",
                event_type,
                exc,
            )
            await self._close(cancel_writer=False)
            return
        perf.record("ws.send_binary", (time.perf_counter() - send_t) * 1000.0)

    @staticmethod
    def _record_lag_overlap(queued_at: float, finished_at: float) -> None:
        evidence = lag_watchdog._LAG_LOOP_EVIDENCE
        sentinel_at = evidence.get("sentinel_at")
        if isinstance(sentinel_at, (int, float)) and queued_at <= sentinel_at <= finished_at:
            perf.record_count("ws.phase.lag_overlap")

    async def _write_one(
        self,
        event_dict: dict,
        *,
        frame_id: int,
        queued_at: float,
        writer_dequeued_at: float,
        enqueue_depth: int,
        serialized: SerializedWebSocketFrame | None,
    ) -> None:
        event_type = event_dict.get("type") if isinstance(event_dict, dict) else None
        send_t = time.perf_counter()
        payload_bytes = 0
        wire_t: float | None = None

        try:
            serialize_t = time.perf_counter()
            serialized_task = getattr(event_dict, "_bc_serialized_json_task", None)
            serializer_await_start_at = time.perf_counter()
            if serialized is not None:
                text = serialized
            elif serialized_task is not None:
                text = await serialized_task
            else:
                text = await dumps_ws_json(event_dict)
            serializer_await_resume_at = time.perf_counter()
            serializer_submit_at = getattr(text, "submit_at", serialize_t)
            serializer_start_at = getattr(text, "start_at", serializer_submit_at)
            serializer_done_at = getattr(text, "done_at", serializer_await_resume_at)
            if not (
                writer_dequeued_at <= serializer_await_start_at <= serializer_await_resume_at
                and serializer_submit_at <= serializer_start_at <= serializer_done_at
                and serializer_done_at <= serializer_await_resume_at
            ):
                raise RuntimeError("invalid WebSocket phase timestamp ordering")
            perf.record(
                "ws.phase.serializer_submit_start",
                (serializer_start_at - serializer_submit_at) * 1000.0,
            )
            perf.record(
                "ws.phase.serializer_start_done",
                (serializer_done_at - serializer_start_at) * 1000.0,
            )
            perf.record("ws.phase.writer_dequeue_await_start", (
                serializer_await_start_at - writer_dequeued_at
            ) * 1000.0)
            if serializer_done_at <= writer_dequeued_at:
                perf.record("ws.phase.serializer_done_writer_dequeue", (
                    writer_dequeued_at - serializer_done_at
                ) * 1000.0)
                perf.record("ws.phase.serializer_await_start_resume", (
                    serializer_await_resume_at - serializer_await_start_at
                ) * 1000.0)
            else:
                if serializer_submit_at >= serializer_await_start_at:
                    perf.record("ws.phase.serializer_await_start_submit", (
                        serializer_submit_at - serializer_await_start_at
                    ) * 1000.0)
                perf.record("ws.phase.serializer_done_await_resume", (
                    serializer_await_resume_at - serializer_done_at
                ) * 1000.0)
            payload_bytes = len(text.encode("utf-8"))
            perf.record_count("ws.phase.payload_bytes", payload_bytes)
            perf.record(
                "ws.send_json.serialize_off_loop",
                (time.perf_counter() - serialize_t) * 1000.0,
            )
            wire_t = time.perf_counter()
            perf.record("ws.phase.serializer_resume_wire_start", (
                wire_t - serializer_await_resume_at
            ) * 1000.0)
            await self._websocket.send_text(text)
            wire_ms = (time.perf_counter() - wire_t) * 1000.0
            wire_end_at = time.perf_counter()
            self._record_lag_overlap(queued_at, wire_end_at)
            perf.record("ws.phase.wire_start_resume", wire_ms)
            perf.record("ws.send_json.wire", wire_ms)
            if serializer_done_at <= writer_dequeued_at:
                timeline_origin = serializer_submit_at
                timeline_total = (
                    serializer_start_at - serializer_submit_at
                    + serializer_done_at - serializer_start_at
                    + writer_dequeued_at - serializer_done_at
                    + serializer_await_start_at - writer_dequeued_at
                    + serializer_await_resume_at - serializer_await_start_at
                    + wire_t - serializer_await_resume_at
                    + wire_end_at - wire_t
                )
            elif serializer_submit_at >= serializer_await_start_at:
                timeline_origin = writer_dequeued_at
                timeline_total = (
                    serializer_await_start_at - writer_dequeued_at
                    + serializer_submit_at - serializer_await_start_at
                    + serializer_start_at - serializer_submit_at
                    + serializer_done_at - serializer_start_at
                    + serializer_await_resume_at - serializer_done_at
                    + wire_t - serializer_await_resume_at
                    + wire_end_at - wire_t
                )
            else:
                timeline_origin = serializer_submit_at
                timeline_total = (
                    serializer_start_at - serializer_submit_at
                    + serializer_done_at - serializer_start_at
                    + serializer_await_resume_at - serializer_done_at
                    + wire_t - serializer_await_resume_at
                    + wire_end_at - wire_t
                )
            perf.record("ws.phase.timeline_total", timeline_total * 1000.0)
            perf.record("ws.phase.timeline_elapsed", (
                wire_end_at - timeline_origin
            ) * 1000.0)
            if wire_ms > 250.0:
                logger.warning(
                    "slow WebSocket wire type=%s elapsed_ms=%.1f bytes=%d "
                    "conn=%s frame=%d enqueue_depth=%d current_depth=%d",
                    event_type,
                    wire_ms,
                    payload_bytes,
                    self._connection_id,
                    frame_id,
                    enqueue_depth,
                    self._queue.qsize(),
                )
        except Exception as exc:
            logger.debug(
                "WebSocket send failed type=%s error=%s",
                event_type,
                exc,
            )
            await self._close(cancel_writer=False)
            return
        elapsed_ms = (time.perf_counter() - send_t) * 1000.0
        perf.record("ws.send_json", elapsed_ms)
        if elapsed_ms > 250.0:
            logger.warning(
                "slow WebSocket send type=%s elapsed_ms=%.1f",
                event_type,
                elapsed_ms,
            )
