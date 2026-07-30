"""WebSocket chat surface: the /ws/chat protocol and its per-connection outbox."""

import asyncio
import hashlib
import itertools
import json
import logging
import os
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

import browser_trust
import auth
import config_store
import extension_store
import lag_watchdog
import perf
import recovery
import session_search
import user_prefs
import virtual_session_prompt_handlers
from capability_contexts import normalize_capability_contexts
from env_compat import dual_env
from i18n import t
from offline_actions_api import _start_prompt_handoff
from orchestrator import build_semantic_alter_prompt
from session_detail_api import (
    _build_messages_replay_delta,
    _fallback_ws_send_mode_after_failed_steer,
    _floor_events_from_seq,
    _node_offline_error,
    _normalize_ws_send_mode_for_turn_state,
    _parse_ws_disabled_builtin_extensions,
    _parse_ws_disallowed_tools,
    _rewind_latest_user_for_alter,
)
from session_manager import manager as session_manager
from stores import pending_approvals
from user_msg_lifecycle import (
    emit_failed,
    emit_queued,
    emit_requested,
    new_lifecycle_msg_id,
    queued_payload,
)
from ws_serialization import (
    SerializedWebSocketFrame,
    dumps_ws_json,
    metric_event_type,
)
from ws_snapshot_binary import SNAPSHOT_BINARY_SUBPROTOCOL

logger = logging.getLogger(__name__)

router = APIRouter()

coordinator: Any = None


def configure(*, coordinator: Any) -> None:
    globals()["coordinator"] = coordinator


_WS_OUTBOX_MAX_ITEMS = 256
_WS_OUTBOX_ENQUEUE_TIMEOUT_SECONDS = 2.0
_WS_OUTBOX_CLOSE_TIMEOUT_SECONDS = 1.0

_WS_FRAME_IDS = itertools.count(1)


class _WebSocketOutbox:
    def __init__(
        self,
        websocket,
        *,
        on_close,
        max_items: int = _WS_OUTBOX_MAX_ITEMS,
        enqueue_timeout_s: float = _WS_OUTBOX_ENQUEUE_TIMEOUT_SECONDS,
        close_timeout_s: float = _WS_OUTBOX_CLOSE_TIMEOUT_SECONDS,
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


def _ws_queued_prompt_is_user_visible(kind: str) -> bool:
    return kind != "send"


async def _send_snapshot_refresh_roots(scope, refresh_id, send) -> bool:
    authority = await asyncio.to_thread(_snapshot_refresh_authority, scope)
    if authority is None:
        return await send({
            "type": "snapshot_refresh_complete",
            "data": {
                "refresh_id": refresh_id,
                "success": False,
                "root_ids": [],
            },
        })
    ordered_root_ids = sorted(authority)
    for root_id in ordered_root_ids:
        if not await send({
            "type": "session_reconciled",
            "data": {
                "root_id": root_id,
                "scope_sids": authority[root_id],
                "snapshot_refresh_id": refresh_id,
            },
        }):
            return False
    return await send({
        "type": "snapshot_refresh_complete",
        "data": {
            "refresh_id": refresh_id,
            "success": True,
            "root_ids": ordered_root_ids,
        },
    })


_SNAPSHOT_REFRESH_MAX_SCOPE_SIDS = 512
_SNAPSHOT_REFRESH_MAX_SCOPE_BYTES = 128 * 1024


def _snapshot_refresh_authority(scope):
    authority = {}
    all_scope_sids = set()
    session_ids = sorted({session_id for session_id, _message_id in scope})
    for session_id in session_ids:
        root_id = session_manager._root_id_for(session_id) or session_id
        scope_sids = session_manager.subtree_ids(root_id) or {root_id}
        scope_sids.add(root_id)
        ordered_scope = sorted(scope_sids)
        if (
            len(ordered_scope) > _SNAPSHOT_REFRESH_MAX_SCOPE_SIDS
            or any(
                not isinstance(sid, str) or not sid or len(sid) > 256
                for sid in ordered_scope
            )
            or sum(len(sid.encode("utf-8")) for sid in ordered_scope)
            > _SNAPSHOT_REFRESH_MAX_SCOPE_BYTES
        ):
            return None
        authority[root_id] = ordered_scope
        all_scope_sids.update(ordered_scope)
        if (
            len(all_scope_sids) > _SNAPSHOT_REFRESH_MAX_SCOPE_SIDS
            or sum(len(sid.encode("utf-8")) for sid in all_scope_sids)
            > _SNAPSHOT_REFRESH_MAX_SCOPE_BYTES
        ):
            return None
    return authority or None


@router.websocket("/ws/chat")
async def websocket_chat(websocket: WebSocket):
    # Auth gate. SessionMiddleware populates `websocket.session` on
    # the scope from the cookie sent in the upgrade request (same-
    # origin via Vite proxy in dev, single-port backend in prod —
    # both see the better_agent_session cookie).
    #
    # We MUST accept() before sending a close-with-code: closing
    # pre-accept terminates the handshake with HTTP 403 and the
    # browser never sees a WebSocket close frame (it surfaces as
    # CloseEvent code 1006 "abnormal" — indistinguishable from a
    # backend restart, triggering useWebSocket's reconnect loop).
    # Accepting first means the client gets a real close frame with
    # code 1008, which useWebSocket maps to <Login /> swap.
    if not browser_trust.validate_websocket(websocket):
        await websocket.close(code=1008)
        return
    await _accept_ws_if_needed(websocket)
    user = websocket.session.get("user")
    if not user:
        # Bearer-token fallback for native clients — same rationale as
        # the REST middleware. WS headers are not generally writable
        # from JS in browsers, so we accept the token as a query param
        # too.
        tok = websocket.query_params.get("token")
        tok_user = auth.verify_token(tok) if tok else None
        if tok_user:
            websocket.session["user"] = tok_user
            user = tok_user
    if not user:
        await websocket.close(code=1008)
        return
    logger.info("WebSocket connected")
    outbox: _WebSocketOutbox | None = None

    from ws_snapshot_transport import SnapshotTransport

    snapshot_transport: SnapshotTransport | None = None

    async def _send_prepared(event_dict, serialized=None):
        if outbox is None:
            return False
        return await outbox.send(event_dict, serialized)

    async def _send_binary(payload: bytes):
        if outbox is None:
            return False
        return await outbox.send_binary(payload, event_type="snapshot_chunk")

    async def _refresh_snapshot_roots(scope, refresh_id):
        return await _send_snapshot_refresh_roots(
            scope,
            refresh_id,
            _send_prepared,
        )

    async def ws_callback(event_dict):
        if snapshot_transport is None:
            return False
        return await snapshot_transport.send_event(event_dict)

    # Per-connection token so subscription bookkeeping in the coordinator
    # keys on a value that is unique per WS connection and NEVER reused
    # (unlike `id(ws_callback)`, which CPython recycles once the closure is
    # GC'd, letting a stale leaked entry from a dead connection collide with
    # a fresh one and dedupe its re-subscribe away — starving the focused
    # session of live events until a manual switch). Cleanup on disconnect
    # goes through `coordinator.unregister_all_ws`, which drops EVERY session
    # this socket subscribed to (a single socket subscribes to many panes;
    # the old single-`current_app_session_id` cleanup leaked the rest).
    ws_callback._bc_conn_token = uuid.uuid4().hex  # type: ignore[attr-defined]

    async def _close_ws_connection() -> None:
        await asyncio.to_thread(coordinator.unregister_all_ws, ws_callback)

    outbox = _WebSocketOutbox(websocket, on_close=_close_ws_connection)
    snapshot_transport = SnapshotTransport(
        principal=json.dumps(user, sort_keys=True, default=str),
        send=_send_prepared,
        send_binary=_send_binary,
        binary=_snapshot_binary_enabled(websocket),
        refresh=_refresh_snapshot_roots,
    )
    coordinator.register_global_ws(ws_callback)

    def _register(sid: str, *, from_seq: int = 0) -> None:
        coordinator.register_ws(sid, ws_callback, from_seq=from_seq)

    def _unregister(sid: str) -> None:
        coordinator.unregister_ws(sid, ws_callback)

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await ws_callback({"type": "error", "data": {"error": t("error.ws_invalid_json")}})
                continue

            msg_type = msg.get("type")

            if msg_type == "ping":
                # Application-level heartbeat reply. Neither the ASGI
                # server nor a reverse proxy in this stack configures a WS
                # idle timeout, so a connection killed silently by a
                # mobile network transition (OS-suspended background
                # sockets, WiFi<->cellular handoff, carrier NAT idle-drop)
                # would otherwise sit open on both ends forever. The
                # client's heartbeat watchdog (useWebSocket.ts) uses the
                # absence of this reply to detect and repair that.
                await _send_prepared({"type": "pong"})
                continue
            if msg_type == "snapshot_ack":
                await snapshot_transport.acknowledge(msg)
                continue
            if msg_type == "snapshot_resume":
                await snapshot_transport.resume(msg)
                continue
            if msg_type == "snapshot_refresh":
                await snapshot_transport.refresh(msg)
                continue

            # Lightweight viewing-without-prompting hook: lets a client
            # tell the backend "I am viewing this session now; register
            # my ws_callback for it." `BetterAgentJsonlTailer` (started
            # by `register_ws`) is the sole live-event WS producer; any
            # worker fan-out from `/api/internal/ask-fork` also reaches
            # this socket via the same callback registry.
            if msg_type == "subscribe":
                sub_sid = msg.get("app_session_id")
                if sub_sid:
                    try:
                        import startup_recovery_gate
                        sub_sid_text = str(sub_sid)
                        startup_recovery_gate.request_session_priority(sub_sid_text)
                        asyncio.create_task(
                            recovery._promote_recovered_session(sub_sid_text),
                            name=f"recover-selected-{sub_sid_text[:8]}",
                        )
                    except Exception:
                        logger.debug("startup recovery priority request failed", exc_info=True)
                    # `events_from_seq` is the watermark from the REST
                    # snapshot's `max_seq_by_sid`. The wire tailer drains
                    # `events_from_seq+1..cursor` to this WS before live
                    # events flow — gap-free, dup-free.
                    try:
                        events_from_seq = int(msg.get("events_from_seq") or 0)
                    except (TypeError, ValueError):
                        events_from_seq = 0
                    events_cursor_known = msg.get("events_cursor_known") is True
                    events_from_seq = await asyncio.to_thread(
                        _floor_events_from_seq,
                        sub_sid,
                        events_from_seq,
                        cursor_known=events_cursor_known,
                    )
                    _register(sub_sid, from_seq=events_from_seq)
                    # Sequence-cursor replay. The frontend hands us the
                    # highest seq it has already applied; we send back
                    # every persisted message with `seq >= since_seq` so
                    # reconnects (and cold loads with since_seq=0)
                    # converge on the canonical state without needing
                    # a separate REST refetch path. Includes the live
                    # in-flight assistant message if one is mid-stream
                    # — its in-memory state may be a few ms ahead of
                    # the on-disk snapshot.
                    try:
                        since_seq = int(msg.get("since_seq") or 0)
                    except (TypeError, ValueError):
                        since_seq = 0
                    try:
                        # Unified projection (INV-15 / ADR-1, originally
                        # DIV-1 / OQ-15): WS replay reads the SAME
                        # session_manager cache REST reads. The fold
                        # (SessionProjectionDrainer) keeps that cache
                        # projected from events.jsonl as rows are written —
                        # no inline reconcile here. Cap replay at the same
                        # `msg_limit` REST uses so cold-hop `since_seq=0`
                        # doesn't ship the entire history; frontend
                        # upsert-by-id makes the overlap with REST
                        # harmless.
                        asyncio.create_task(
                            asyncio.to_thread(
                                coordinator.turn_manager.tick_running_state,
                                sub_sid,
                            )
                        )
                        replay_start = time.perf_counter()
                        delta = await asyncio.to_thread(
                            _build_messages_replay_delta,
                            sub_sid,
                            since_seq,
                            limit=50,
                        )
                        replay_delta_ms = (time.perf_counter() - replay_start) * 1000
                        replay_build_ms = replay_delta_ms
                        if delta is not None:
                            replay_post_start = time.perf_counter()
                            replay_msgs = delta["messages"]
                            in_flight = delta.get("in_flight")
                            replay_post_ms = (time.perf_counter() - replay_post_start) * 1000
                            replay_build_ms = replay_delta_ms + replay_post_ms
                            perf.record("ws.replay.delta", replay_delta_ms)
                            perf.record("ws.replay.post", replay_post_ms)
                            send_start = time.perf_counter()
                            await ws_callback({
                                "type": "messages_replay",
                                "data": {
                                    "app_session_id": sub_sid,
                                    "since_seq": since_seq,
                                    "next_seq": delta["next_seq"],
                                    "messages": replay_msgs,
                                },
                            })
                            send_ms = (time.perf_counter() - send_start) * 1000
                            if logger.isEnabledFor(logging.DEBUG):
                                replay_asst = [
                                    m for m in replay_msgs
                                    if m.get("role") == "assistant"
                                ]
                                last_asst_evts = replay_asst[-1].get("events") if replay_asst else None
                                last_asst_stub = replay_asst[-1].get("stub") if replay_asst else None
                                logger.debug(
                                    "WS replay %s: since_seq=%d next_seq=%d msgs=%d "
                                    "inflight=%s last_asst_evts=%s "
                                    "last_asst_stub=%s build=%.1fms delta=%.1fms post=%.1fms send=%.1fms",
                                    sub_sid[:8],
                                    since_seq,
                                    delta["next_seq"],
                                    len(replay_msgs),
                                    in_flight is not None,
                                    len(last_asst_evts) if last_asst_evts else None,
                                    last_asst_stub.get("event_count") if last_asst_stub else None,
                                    replay_build_ms,
                                    replay_delta_ms,
                                    replay_post_ms,
                                    send_ms,
                                )
                            elif replay_build_ms >= 100 or send_ms >= 100:
                                logger.info(
                                    "WS replay %s timings build=%.1fms send=%.1fms",
                                    sub_sid[:8], replay_build_ms, send_ms,
                                )
                    except Exception:
                        logger.exception("messages_replay on subscribe failed")
                    # Push current run_state snapshot so the freshly
                    # subscribed client knows what's running for this
                    # session right now (no waiting for the next
                    # transition).
                    try:
                        _sub_runs = coordinator.turn_manager.get_run_state(sub_sid)
                        if logger.isEnabledFor(logging.DEBUG):
                            logger.debug(
                                "RUNSTATE_DBG[subscribe] sid=%s serves runs=%s",
                                sub_sid[:8],
                                [
                                    f"{(r.get('run_id') or '?')[:8]}|{r.get('kind')}"
                                    f"|pid={r.get('pid')}|started={r.get('started_at')}"
                                    for r in _sub_runs
                                ],
                            )
                        await ws_callback({
                            "type": "run_state",
                            "data": {
                                "app_session_id": sub_sid,
                                "runs": _sub_runs,
                            },
                        })
                    except Exception:
                        logger.exception("run_state replay on subscribe failed")
                    # Re-emit pending fresh-worker approvals so a
                    # frontend that reconnected mid-wait sees the inline
                    # card without depending on the REST rehydration
                    # race. Gate on `approval_waiters` so only
                    # approvals the backend is ACTIVELY waiting on get
                    # re-emitted — orphan disk records (resolved while
                    # the user was disconnected, or stranded by a
                    # crash before the runner re-retried) don't
                    # resurrect dismissed cards.
                    try:
                        active_dids = set(coordinator.approval_waiters.keys())
                        for rec in pending_approvals.list_pending(cwd=None):
                            if rec.get("app_session_id") != sub_sid:
                                continue
                            if rec.get("delegation_id") not in active_dids:
                                continue
                            await ws_callback({
                                "type": "worker_creation_requested",
                                "data": rec,
                            })
                    except Exception:
                        logger.exception("re-emit pending approvals on subscribe failed")
                    # Stale-queue cleanup: if the frontend has a stale
                    # queuedBySession entry for this session (consumed while
                    # unsubscribed), tell it to clear it now. The live
                    # queue_consumed event only reaches subscribers at emit
                    # time, so this re-emit covers the gap.
                    try:
                        import session_queue_projection
                        persisted_queued = await asyncio.to_thread(
                            session_queue_projection.queued_prompts,
                            sub_sid,
                            storage_identity=(
                                session_manager._root_repository.storage_identity()
                            ),
                        )
                        if (
                            not coordinator.has_queued_prompts(sub_sid)
                            and not persisted_queued
                        ):
                            await ws_callback({
                                "type": "queue_consumed",
                                "data": {
                                    "app_session_id": sub_sid,
                                    "queued_id": None,
                                },
                            })
                    except Exception:
                        logger.debug("queue_consumed re-emit on subscribe failed", exc_info=True)
                continue
            if msg_type == "unsubscribe":
                sub_sid = msg.get("app_session_id")
                if sub_sid:
                    _unregister(sub_sid)
                continue

            if msg_type == "send_message":
                prompt = msg.get("prompt", "").strip()
                images = msg.get("images") or []
                files = msg.get("files") or []
                async def _send_message_error(error: str) -> None:
                    data = {
                        "error": error,
                        "app_session_id": msg.get("app_session_id"),
                        "session_id": msg.get("app_session_id"),
                        "client_id": msg.get("client_id"),
                    }
                    await ws_callback({"type": "error", "data": data})
                if not prompt and not images and not files:
                    await _send_message_error(t("error.ws_empty_prompt"))
                    continue

                # Validate file attachments — reject oversized or malformed entries.
                MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB per file
                _file_error = None
                for f in files:
                    if (
                        not isinstance(f, dict)
                        or not isinstance(f.get("data"), str)
                        or not isinstance(f.get("name"), str)
                        or not f.get("name")
                        or not isinstance(f.get("media_type"), str)
                        or not isinstance(f.get("size"), int)
                        or f.get("size") < 0
                    ):
                        _file_error = "Malformed file attachment"
                        break
                    if f["size"] > MAX_FILE_SIZE:
                        _file_error = f"File \"{f.get('name', '?')}\" exceeds 10 MB limit"
                        break
                if _file_error:
                    await _send_message_error(_file_error)
                    continue

                model = msg.get("model")
                cwd = msg.get("cwd", os.path.expanduser("~"))
                app_session_id = msg.get("app_session_id")
                orchestration_mode = msg.get("orchestration_mode")
                send_mode = msg.get("send_mode") or await asyncio.to_thread(
                    user_prefs.get_send_mode,
                )
                logger.info("Received message: prompt=%s, images=%d, files=%d, send_mode=%s", prompt[:50], len(images), len(files), send_mode)

                if not app_session_id:
                    await _send_message_error(t("error.ws_no_session_selected"))
                    continue

                _offline_session = await asyncio.to_thread(
                    session_manager.get_lite,
                    app_session_id,
                )
                _offline_err = await _node_offline_error(_offline_session)
                if _offline_err:
                    await _send_message_error(_offline_err)
                    continue
                _provider_id_for_send = (_offline_session or {}).get("provider_id")
                if _provider_id_for_send and config_store.provider_suspended(_provider_id_for_send):
                    await _send_message_error(t("error.provider_suspended", action="run turns"))
                    continue
                if _offline_session:
                    model = _offline_session.get("model") or model
                    cwd = _offline_session.get("cwd") or cwd
                    orchestration_mode = (
                        _offline_session.get("orchestration_mode")
                        or orchestration_mode
                    )
                if orchestration_mode == "manager":
                    orchestration_mode = "team"
                if orchestration_mode == "team":
                    team_not_ready = extension_store.runtime_not_ready_message(
                        extension_store.extension_id_for_role('team-orchestration')
                    )
                    if team_not_ready is not None:
                        await _send_message_error(team_not_ready)
                        continue

                # Ask-singleton entry point: when the user sends a prompt
                # into the singleton session via WS, wrap the prompt with
                # the per-call session index + JSON contract instructions
                # so the LLM emits structured `{session_ids, reasoning}`
                # the frontend AskPicker can render. v1 trade-off: the
                # wrapped prompt persists as user_msg.content on the
                # singleton (the Ask UI hides scrollback so this is not
                # user-visible). Documented in session_search.py.
                cli_prompt = msg.get("cli_prompt")
                try:
                    disallowed_tools = _parse_ws_disallowed_tools(msg.get("disallowed_tools"))
                    disabled_builtin_extensions = _parse_ws_disabled_builtin_extensions(
                        msg.get("disabled_builtin_extensions")
                    )
                except ValueError as e:
                    await _send_message_error(str(e))
                    continue
                backend_url = msg.get("backend_url")
                if backend_url is not None:
                    if not isinstance(backend_url, str) or not (
                        backend_url.startswith("http://127.0.0.1:")
                        or backend_url.startswith("http://localhost:")
                    ):
                        await _send_message_error("backend_url must be a loopback HTTP URL")
                        continue
                    backend_url = backend_url.rstrip("/")
                    os.environ.update(dual_env("BETTER_CLAUDE_BACKEND_URL", backend_url))
                    await asyncio.to_thread(
                        session_manager.set_backend_url,
                        app_session_id,
                        backend_url,
                    )
                # NOTE: the Ask session runs NO claude turn of its own. A
                # prompt sent into it is an Ask search, orchestrated after
                # `_register` below via `session_search.search()` (appends
                # user+assistant turns, runs an ephemeral search worker,
                # stamps the picker).
                known_worker_registry_cwds = msg.get("known_worker_registry_cwds")
                if known_worker_registry_cwds is not None:
                    if not isinstance(known_worker_registry_cwds, dict):
                        await _send_message_error("known_worker_registry_cwds must be an object")
                        continue
                    parsed_worker_registry_cwds: dict[str, str] = {}
                    registry_error = None
                    for key, value in known_worker_registry_cwds.items():
                        if not isinstance(key, str) or not key:
                            registry_error = "known_worker_registry_cwds keys must be non-empty strings"
                            break
                        if not isinstance(value, str) or not value.strip():
                            registry_error = "known_worker_registry_cwds values must be non-empty strings"
                            break
                        expanded = Path(value).expanduser()
                        if not expanded.is_absolute():
                            registry_error = "known_worker_registry_cwds values must be absolute paths"
                            break
                        parsed_worker_registry_cwds[key] = str(expanded.resolve())
                    if registry_error:
                        await _send_message_error(registry_error)
                        continue
                    known_worker_registry_cwds = parsed_worker_registry_cwds or None
                try:
                    capability_contexts = normalize_capability_contexts(msg.get("capability_contexts"))
                except ValueError as e:
                    await _send_message_error(str(e))
                    continue
                harness_profile_id = str(msg.get("harness_profile_id") or "").strip()

                # Register this WS so /api/internal/ask-fork can fan out worker events.
                _register(app_session_id)

                # Ask search: orchestrate entirely via session_search.search()
                # (appends a user turn + an assistant turn with the picker on
                # the stable Ask session, driven by an ephemeral search
                # worker). No claude turn runs on the Ask session, so skip
                # the normal submit/queue path — the broadcasts from search()
                # drive the UI over this registered WS.
                if app_session_id == session_search.ASK_SINGLETON_ID:
                    not_ready_msg = extension_store.runtime_not_ready_message(
                        extension_store.BUILTIN_ASK_EXTENSION_ID
                    )
                    if not_ready_msg is not None:
                        await _send_message_error(not_ready_msg)
                        continue
                    ask_client_id = msg.get("client_id")
                    already_done = await asyncio.to_thread(
                        session_search.find_user_message_by_client_id,
                        ask_client_id,
                    )
                    if already_done:
                        await ws_callback({
                            "type": "user_message_persisted",
                            "data": {
                                "session_id": app_session_id,
                                "user_message": already_done,
                            },
                        })
                        continue

                    async def _ack_ask_user_message(user_message: dict) -> None:
                        await ws_callback({
                            "type": "user_message_persisted",
                            "data": {
                                "session_id": app_session_id,
                                "user_message": user_message,
                            },
                        })

                    asyncio.create_task(
                        session_search.search(
                            prompt,
                            client_id=ask_client_id,
                            lifecycle_msg_id=new_lifecycle_msg_id(),
                            on_user_message=_ack_ask_user_message,
                        )
                    )
                    continue
                virtual_prompt_client_id = msg.get("client_id")
                virtual_prompt_handled = await virtual_session_prompt_handlers.handle(
                    app_session_id,
                    prompt=prompt,
                    cwd=cwd,
                    client_id=(
                        virtual_prompt_client_id
                        if isinstance(virtual_prompt_client_id, str)
                        else None
                    ),
                    lifecycle_msg_id=new_lifecycle_msg_id(),
                    dispatch_ws=ws_callback,
                )
                if virtual_prompt_handled:
                    continue

                # Dedup: if client_id is present, check whether this prompt
                # was already processed or is already queued.  Prevents
                # duplicate turns when the frontend re-flushes its offline
                # backlog after a WS reconnect or page reload.
                _cid = msg.get("client_id")
                if _cid:
                    import session_queue_projection
                    _sess = await asyncio.to_thread(
                        session_queue_projection.get,
                        app_session_id,
                        storage_identity=(
                            session_manager._root_repository.storage_identity()
                        ),
                    )
                    if _sess:
                        # Check if user message with this client_id exists.
                        _already_done = None
                        for _m in _sess.get("user_messages", []):
                            if _m.get("client_id") == _cid:
                                _already_done = _m
                                break
                        if _already_done:
                            await ws_callback({
                                "type": "user_message_persisted",
                                "data": {
                                    "session_id": app_session_id,
                                    "user_message": _already_done,
                                },
                            })
                            continue

                        # Check if prompt with this client_id is already queued.
                        _already_queued = None
                        for _qp in _sess.get("queued_prompts", []):
                            if _qp.get("client_id") == _cid:
                                _already_queued = _qp
                                break
                        if _already_queued:
                            _already_lifecycle_msg_id = _already_queued.get("lifecycle_msg_id")
                            _already_kind = _already_queued.get("kind") or "queued_behind"
                            if _already_lifecycle_msg_id:
                                await ws_callback({
                                    "type": "user_message_queued",
                                    "data": {
                                        "app_session_id": app_session_id,
                                        **queued_payload(
                                            lifecycle_msg_id=_already_lifecycle_msg_id,
                                            content=_already_queued.get("content", ""),
                                            kind=_already_kind,
                                            queue_position=coordinator.get_queued_count(app_session_id),
                                            client_id=_cid,
                                            images_count=int(_already_queued.get("images_count") or 0),
                                            orchestration_mode=_already_queued.get("orchestration_mode"),
                                        ),
                                    },
                                })
                            if _ws_queued_prompt_is_user_visible(_already_kind):
                                await ws_callback({
                                    "type": "prompt_queued",
                                    "data": {
                                        "app_session_id": app_session_id,
                                        "queued_id": _already_queued.get("id"),
                                        "prompt_preview": _already_queued.get("content", ""),
                                        "send_mode": send_mode,
                                        "queue_position": coordinator.get_queued_count(app_session_id),
                                        "client_id": _cid,
                                    },
                                })
                            continue

                    _already_active = coordinator.active_prompt_for_client_id(
                        app_session_id,
                        _cid,
                    )
                    if _already_active:
                        _already_lifecycle_msg_id = _already_active.get("lifecycle_msg_id")
                        if _already_lifecycle_msg_id:
                            await ws_callback({
                                "type": "user_message_queued",
                                "data": {
                                    "app_session_id": app_session_id,
                                    **queued_payload(
                                        lifecycle_msg_id=_already_lifecycle_msg_id,
                                        content=prompt,
                                        kind="send",
                                        queue_position=0,
                                        client_id=_cid,
                                        images_count=len(images),
                                        orchestration_mode=orchestration_mode,
                                    ),
                                },
                            })
                        continue

                # has_active_runs (not has_active_turn): also covers the
                # dequeue→cancel_events gap and recovered live runs, so
                # an interrupt can't silently degrade to a plain queue.
                is_queued = (
                    coordinator.turn_manager.has_active_turn(app_session_id)
                    or coordinator.turn_manager.has_active_runs(app_session_id)
                )

                lifecycle_msg_id = new_lifecycle_msg_id()
                alter_rewind_latest = False
                if send_mode == "alter":
                    _alter_session = await asyncio.to_thread(
                        session_manager.get_lite,
                        app_session_id,
                    )
                    queued_prompts = (_alter_session or {}).get("queued_prompts") or []
                    latest_queued = queued_prompts[-1] if queued_prompts else None
                    if latest_queued:
                        lifecycle_msg_id = (
                            latest_queued.get("lifecycle_msg_id")
                            or lifecycle_msg_id
                        )
                        queued_id = await coordinator.update_latest_queued(
                            app_session_id,
                            prompt,
                            cli_prompt,
                            msg.get("client_id"),
                            lifecycle_msg_id,
                            capability_contexts,
                        )
                        if not queued_id:
                            await _send_message_error(t("error.ws_no_queued_prompt"))
                            continue
                        await asyncio.to_thread(
                            session_manager.update_queued_prompt,
                            app_session_id,
                            queued_id,
                            {
                                "content": prompt,
                                "cli_prompt": cli_prompt,
                                "client_id": msg.get("client_id"),
                                "lifecycle_msg_id": lifecycle_msg_id,
                                "capability_contexts": capability_contexts,
                            },
                        )
                        await ws_callback({
                            "type": "steer_prompt_persisted",
                            "data": {
                                "app_session_id": app_session_id,
                                "client_id": msg.get("client_id"),
                                "lifecycle_msg_id": lifecycle_msg_id,
                            },
                        })
                        continue

                    if is_queued:
                        alter_rewind_latest = True
                        send_mode = "queue"
                    else:
                        try:
                            rewind_data = await _rewind_latest_user_for_alter(app_session_id)
                        except HTTPException as e:
                            await _send_message_error(str(e.detail))
                            continue
                        model = rewind_data.get("retry_model") or model
                        cwd = rewind_data.get("retry_cwd") or cwd
                        orchestration_mode = (
                            rewind_data.get("retry_orchestration_mode")
                            or orchestration_mode
                        )
                        previous_prompt = rewind_data.get("semantic_alter_previous_prompt")
                        if previous_prompt is not None:
                            cli_prompt = build_semantic_alter_prompt(
                                str(previous_prompt),
                                cli_prompt or prompt,
                            )
                        send_mode = "queue"

                send_mode = _normalize_ws_send_mode_for_turn_state(
                    send_mode, is_queued,
                )
                if send_mode == "steer":
                    # Steer injects into the live turn and never reaches the
                    # claim below, so dedup it here: a concurrent same-client_id
                    # steer (offline re-dispatch after a reconnect) must not be
                    # injected twice. One-shot — release right after.
                    _steer_cid = msg.get("client_id")
                    _steer_item = str(uuid.uuid4())
                    if _steer_cid and coordinator.try_claim_prompt_client_id(
                        app_session_id, _steer_item, _steer_cid,
                    ):
                        continue
                    try:
                        _steered = await coordinator.steer_active_turn(
                            app_session_id=app_session_id,
                            prompt=cli_prompt or prompt,
                            display_prompt=prompt,
                            images=images if images else None,
                            client_id=msg.get("client_id"),
                            lifecycle_msg_id=lifecycle_msg_id,
                        )
                    finally:
                        if _steer_cid:
                            coordinator._forget_active_prompt_item(_steer_item)
                    if _steered:
                        continue
                    root_id = (
                        await asyncio.to_thread(
                            session_manager._root_id_for,
                            app_session_id,
                        )
                        or app_session_id
                    )
                    await coordinator.turn_manager.lifecycle.publish(
                        "lifecycle.steer_fallback_queued",
                        root_id=root_id,
                        session_id=app_session_id,
                        message_id=lifecycle_msg_id,
                        payload={"reason": "steer_rejected"},
                    )
                    send_mode = _fallback_ws_send_mode_after_failed_steer(
                        send_mode,
                    )

                if alter_rewind_latest:
                    lifecycle_kind = "interrupt"
                elif send_mode == "interrupt" and is_queued:
                    lifecycle_kind = "interrupt"
                elif is_queued:
                    lifecycle_kind = "queued_behind"
                else:
                    lifecycle_kind = "send"
                queue_position = coordinator.get_queued_count(app_session_id)
                interrupts_msg_id = (
                    coordinator.user_prompt_manager.get_in_flight_lifecycle_msg_id(app_session_id)
                    if lifecycle_kind == "interrupt" else None
                )
                lifecycle_queued_payload = queued_payload(
                    lifecycle_msg_id=lifecycle_msg_id,
                    content=prompt,
                    kind=lifecycle_kind,
                    queue_position=queue_position,
                    client_id=msg.get("client_id"),
                    interrupts_msg_id=interrupts_msg_id,
                    images_count=len(images),
                    orchestration_mode=orchestration_mode,
                )

                item_id = str(uuid.uuid4())
                # Atomic admission gate: claim the client_id BEFORE
                # persisting/emitting anything. A concurrent same-client_id
                # send (offline re-dispatch after a reconnect) that arrives
                # while this turn is in flight would otherwise slip past the
                # read-only dedup checks above and broadcast a phantom
                # queued bubble before submit_prompt deduped it. Claiming
                # here makes the dedup authoritative under concurrency.
                _claim_cid = msg.get("client_id")
                if _claim_cid:
                    _dup_of = coordinator.try_claim_prompt_client_id(
                        app_session_id, item_id, _claim_cid,
                    )
                    if _dup_of:
                        _dup_lifecycle = (
                            coordinator.user_prompt_manager
                            .get_in_flight_lifecycle_msg_id(app_session_id)
                        )
                        if _dup_lifecycle:
                            await ws_callback({
                                "type": "user_message_queued",
                                "data": {
                                    "app_session_id": app_session_id,
                                    **queued_payload(
                                        lifecycle_msg_id=_dup_lifecycle,
                                        content=prompt,
                                        kind="send",
                                        queue_position=0,
                                        client_id=_claim_cid,
                                        images_count=len(images),
                                        orchestration_mode=orchestration_mode,
                                    ),
                                },
                            })
                        continue
                await emit_requested(
                    app_session_id=app_session_id,
                    lifecycle_msg_id=lifecycle_msg_id,
                    content=prompt,
                    kind=lifecycle_kind,
                    queue_position=queue_position,
                    client_id=msg.get("client_id"),
                    interrupts_msg_id=interrupts_msg_id,
                    images_count=len(images),
                    orchestration_mode=orchestration_mode,
                )
                # Sample the active provider after the acceptance fact so
                # environment refresh cannot delay the running projection.
                # This remains before durable admission and CLI spawn.
                await asyncio.to_thread(config_store.apply_env_vars)
                params = {
                    "prompt": prompt,
                    "app_session_id": app_session_id,
                    "model": model,
                    "cwd": cwd,
                    "ws_callback": ws_callback,
                    "images": images if images else None,
                    "files": files if files else None,
                    "orchestration_mode": orchestration_mode,
                    "send_target": msg.get("send_target"),
                    "client_id": msg.get("client_id"),
                    "lifecycle_msg_id": lifecycle_msg_id,
                    "cli_prompt": cli_prompt,
                    "disallowed_tools": disallowed_tools,
                    "disabled_builtin_extensions": disabled_builtin_extensions,
                    "known_worker_registry_cwds": known_worker_registry_cwds,
                    "capability_contexts": capability_contexts,
                    "harness_profile_id": harness_profile_id,
                    "_queued_id": item_id,
                    "_client_id_claimed": bool(_claim_cid),
                }

                # From the claim above until submit_prompt hands the claim's
                # release to turn-end, any failure must release the claim —
                # otherwise the offline backlog's same-client_id re-dispatch
                # would be deduped and the prompt silently lost. Fail closed
                # toward NOT losing user intent.
                def _release_claim_on_failure() -> None:
                    if _claim_cid:
                        coordinator._forget_active_prompt_item(item_id)

                if send_mode == "interrupt" and is_queued:
                    params["_interrupt"] = True
                    # Cancel the current turn so the processor picks this up sooner.
                    # Pass the incoming lifecycle id so the displaced
                    # turn's done event carries the cross-ref.
                    try:
                        await coordinator.turn_manager.cancel_turn(
                            app_session_id,
                            interrupted_by_msg_id=lifecycle_msg_id,
                        )
                    except Exception as exc:
                        _release_claim_on_failure()
                        await emit_failed(
                            app_session_id=app_session_id,
                            lifecycle_msg_id=lifecycle_msg_id,
                            reason="interrupt_failed",
                            error=str(exc),
                        )
                        raise
                elif alter_rewind_latest:
                    params["_alter_rewind_latest"] = True
                    try:
                        await coordinator.turn_manager.cancel_turn(
                            app_session_id,
                            interrupted_by_msg_id=lifecycle_msg_id,
                        )
                    except Exception as exc:
                        _release_claim_on_failure()
                        await emit_failed(
                            app_session_id=app_session_id,
                            lifecycle_msg_id=lifecycle_msg_id,
                            reason="alter_interrupt_failed",
                            error=str(exc),
                        )
                        raise

                # Hand off to the coordinator-owned per-session
                # processor. It survives this WebSocket's lifetime — a
                # disconnect deregisters the ws_callback below but the
                # processor keeps running and the detached runner keeps
                # writing events into the persisted session JSON, so a
                # reconnect+refetch shows the same content as live.
                queued_prompt = {
                    "id": item_id,
                    "lifecycle_msg_id": lifecycle_msg_id,
                    "content": prompt,
                    "kind": lifecycle_kind,
                    "queue_position": queue_position,
                    "images_count": len(images),
                    "files_count": len(files),
                    "images": images if images else None,
                    "files": files if files else None,
                    "orchestration_mode": orchestration_mode,
                    "send_target": msg.get("send_target"),
                    "cli_prompt": cli_prompt,
                    "disallowed_tools": disallowed_tools,
                    "disabled_builtin_extensions": disabled_builtin_extensions,
                    "client_id": msg.get("client_id"),
                    "alter_rewind_latest": alter_rewind_latest,
                    "capability_contexts": capability_contexts,
                    "harness_profile_id": harness_profile_id,
                    "created_at": datetime.now().isoformat(),
                }
                try:
                    admission = await _start_prompt_handoff(
                        app_session_id,
                        queued_prompt,
                        params,
                    )
                except Exception as exc:
                    _release_claim_on_failure()
                    await emit_failed(
                        app_session_id=app_session_id,
                        lifecycle_msg_id=lifecycle_msg_id,
                        reason="durable_admission_failed",
                        error=str(exc),
                    )
                    raise
                if not admission.get("session"):
                    _release_claim_on_failure()
                    await emit_failed(
                        app_session_id=app_session_id,
                        lifecycle_msg_id=lifecycle_msg_id,
                        reason="session_not_found",
                    )
                    await _send_message_error(t("error.session_not_found_retry"))
                    continue
                existing_user_message = admission.get("existing_user_message")
                if existing_user_message:
                    _release_claim_on_failure()
                    await emit_failed(
                        app_session_id=app_session_id,
                        lifecycle_msg_id=lifecycle_msg_id,
                        reason="duplicate_user_message",
                    )
                    await ws_callback({
                        "type": "user_message_persisted",
                        "data": {
                            "session_id": app_session_id,
                            "user_message": existing_user_message,
                        },
                    })
                    continue
                existing_queued_prompt = admission.get("existing_queued_prompt")
                if existing_queued_prompt:
                    _release_claim_on_failure()
                    await emit_failed(
                        app_session_id=app_session_id,
                        lifecycle_msg_id=lifecycle_msg_id,
                        reason="duplicate_queued_prompt",
                    )
                    existing_lifecycle_msg_id = existing_queued_prompt.get("lifecycle_msg_id")
                    existing_kind = existing_queued_prompt.get("kind") or "queued_behind"
                    if existing_lifecycle_msg_id:
                        await ws_callback({
                            "type": "user_message_queued",
                            "data": {
                                "app_session_id": app_session_id,
                                **queued_payload(
                                    lifecycle_msg_id=existing_lifecycle_msg_id,
                                    content=existing_queued_prompt.get("content", ""),
                                    kind=existing_kind,
                                    queue_position=coordinator.get_queued_count(app_session_id),
                                    client_id=msg.get("client_id"),
                                    images_count=int(existing_queued_prompt.get("images_count") or 0),
                                    orchestration_mode=existing_queued_prompt.get("orchestration_mode"),
                                ),
                            },
                        })
                    if _ws_queued_prompt_is_user_visible(existing_kind):
                        await ws_callback({
                            "type": "prompt_queued",
                            "data": {
                                "app_session_id": app_session_id,
                                "queued_id": existing_queued_prompt.get("id"),
                                "prompt_preview": existing_queued_prompt.get("content", ""),
                                "send_mode": send_mode,
                                "queue_position": coordinator.get_queued_count(app_session_id),
                                "client_id": msg.get("client_id"),
                            },
                        })
                    continue
                if not is_queued:
                    try:
                        await emit_queued(
                            app_session_id=app_session_id,
                            lifecycle_msg_id=lifecycle_msg_id,
                            content=prompt,
                            kind=lifecycle_kind,
                            queue_position=queue_position,
                            client_id=msg.get("client_id"),
                            interrupts_msg_id=interrupts_msg_id,
                            images_count=len(images),
                            orchestration_mode=orchestration_mode,
                        )
                    except Exception:
                        logger.exception("lifecycle: emit_queued failed")
                await ws_callback({
                    "type": "user_message_queued",
                    "data": {
                        "app_session_id": app_session_id,
                        **lifecycle_queued_payload,
                    },
                })
                if is_queued:
                    try:
                        await emit_queued(
                            app_session_id=app_session_id,
                            lifecycle_msg_id=lifecycle_msg_id,
                            content=prompt,
                            kind=lifecycle_kind,
                            queue_position=queue_position,
                            client_id=msg.get("client_id"),
                            interrupts_msg_id=interrupts_msg_id,
                            images_count=len(images),
                            orchestration_mode=orchestration_mode,
                        )
                    except Exception:
                        logger.exception("lifecycle: emit_queued failed")

                # Notify the frontend about queue state
                if is_queued:
                    await ws_callback({
                        "type": "prompt_queued",
                        "data": {
                            "app_session_id": app_session_id,
                            "queued_id": item_id,
                            "prompt_preview": prompt,
                            "send_mode": send_mode,
                            "queue_position": queue_position,
                            "client_id": msg.get("client_id"),
                        },
                    })

            elif msg_type == "stop_message":
                app_session_id = msg.get("app_session_id")
                if app_session_id:
                    cancelled = await coordinator.turn_manager.cancel_turn(app_session_id)
                    if not cancelled:
                        await ws_callback({"type": "error", "data": {"error": t("error.ws_no_active_turn_to_stop")}})

            elif msg_type == "promote_queued":
                app_session_id = msg.get("app_session_id")
                if app_session_id:
                    action = msg.get("action")
                    if action not in ("interrupt", "steer"):
                        await _send_message_error(t("error.ws_invalid_send_mode"))
                        continue
                    queued_ids_raw = msg.get("queued_ids")
                    queued_ids = (
                        [qid for qid in queued_ids_raw if isinstance(qid, str)]
                        if isinstance(queued_ids_raw, list)
                        else None
                    )
                    promoted = await coordinator.promote_queued(
                        app_session_id,
                        action=action,
                        queued_id=msg.get("queued_id"),
                        queued_ids=queued_ids,
                    )
                    if not promoted:
                        await ws_callback({"type": "error", "data": {"error": t("error.ws_no_queued_prompt")}})

            elif msg_type == "cancel_queued":
                app_session_id = msg.get("app_session_id")
                if app_session_id:
                    queued_id = msg.get("queued_id")
                    coordinator.cancel_queued(
                        app_session_id,
                        queued_id if isinstance(queued_id, str) else None,
                    )
                    await asyncio.to_thread(
                        session_manager.remove_queued_prompt,
                        app_session_id,
                        queued_id if isinstance(queued_id, str) else None,
                    )

            elif msg_type == "update_queued":
                app_session_id = msg.get("app_session_id")
                queued_id = msg.get("queued_id")
                content = msg.get("content")
                if (
                    isinstance(app_session_id, str)
                    and isinstance(queued_id, str)
                    and isinstance(content, str)
                ):
                    await coordinator.update_queued(
                        app_session_id, queued_id, content,
                    )
                    await asyncio.to_thread(
                        session_manager.update_queued_prompt,
                        app_session_id, queued_id, {"content": content},
                    )
                    coordinator.finish_queued_edit(app_session_id, queued_id)

            elif msg_type == "begin_queued_edit":
                app_session_id = msg.get("app_session_id")
                queued_id = msg.get("queued_id")
                if isinstance(app_session_id, str) and isinstance(queued_id, str):
                    coordinator.begin_queued_edit(app_session_id, queued_id)

            elif msg_type == "finish_queued_edit":
                app_session_id = msg.get("app_session_id")
                queued_id = msg.get("queued_id")
                if isinstance(app_session_id, str) and isinstance(queued_id, str):
                    coordinator.finish_queued_edit(app_session_id, queued_id)

    except WebSocketDisconnect:
        logger.info("WebSocket disconnected")
    except RuntimeError as e:
        if "not connected" in str(e):
            logger.info(f"WebSocket connection issue: {e}")
        else:
            logger.exception(f"WebSocket RuntimeError: {e}")
    except Exception as e:
        logger.exception(f"WebSocket error: {e}")
    finally:
        # WS disconnect: just stop fanning events to this socket. Do NOT
        # cancel the coordinator-owned prompt processor — its turn
        # continues, the detached runner keeps producing events, and the
        # session JSON keeps being updated. The next connect+refetch
        # picks up where this socket left off.
        #
        # Unregister EVERY session this socket subscribed to — not just the
        # last one. Leaving non-last subscriptions registered leaks their
        # `ws_callbacks` / `_subscriber_index` entries; on reconnect the
        # stale entry blocks a fresh re-subscribe, starving the focused
        # session of live events until a manual switch.
        await asyncio.to_thread(coordinator.unregister_all_ws, ws_callback)
        if snapshot_transport is not None:
            await snapshot_transport.close()
        if outbox is not None:
            await outbox.close()
            await outbox.wait_closed()


def _snapshot_binary_offered(websocket: WebSocket) -> bool:
    raw = websocket.headers.get("sec-websocket-protocol", "")
    if not isinstance(raw, str) or len(raw) > 1024:
        return False
    return SNAPSHOT_BINARY_SUBPROTOCOL in {
        value.strip() for value in raw.split(",") if value.strip()
    }


def _snapshot_binary_enabled(websocket: WebSocket) -> bool:
    return websocket.scope.get("better_agent_snapshot_binary_v1") is True


async def _accept_ws_if_needed(websocket: WebSocket) -> None:
    if websocket.application_state == WebSocketState.CONNECTED:
        logger.warning("WebSocket entered /ws/chat already accepted")
        return
    binary = _snapshot_binary_offered(websocket)
    await websocket.accept(
        subprotocol=SNAPSHOT_BINARY_SUBPROTOCOL if binary else None,
    )
    websocket.scope["better_agent_snapshot_binary_v1"] = binary


@router.websocket("/{_unknown_ws_path:path}")
async def unknown_websocket(websocket: WebSocket, _unknown_ws_path: str):
    await websocket.close(code=1008)
