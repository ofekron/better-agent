"""Better Agent — FastAPI backend with WebSocket streaming and REST APIs."""

import asyncio
import collections
import contextvars
import copy
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
import faulthandler
import hashlib
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
from html import escape

# Build version: first 5 chars of git HEAD SHA.
try:
    _GIT_SHA = subprocess.check_output(
        ["git", "rev-parse", "--short=5", "HEAD"],
        stderr=subprocess.DEVNULL,
    ).decode().strip()
except Exception:
    _GIT_SHA = "dev"
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import Response
from pathlib import Path

# Assert an adequate fd limit before any backend module opens handles.
from fd_limits import raise_fd_limit
raise_fd_limit()

from capability_contexts import normalize_capability_contexts
from communication_modes import (
    ASK_MODE_CONTINUE_AND_EXPECT_MSSG_BACK_ASYNC,
    normalize_ask_mode,
)
from backend_instance_lock import (
    acquire_backend_instance_lock,
    release_backend_instance_lock,
)
from env_compat import dual_env, get_env
from event_bus import BusEvent, bus as event_bus
import browser_trust
import hook_store
from event_shape import (
    has_assistant_text,
    project_content_snapshot,
    strip_synthetic_events,
)
from paths import ba_home
from i18n import t
from provisioning.prompts import render_prompt
from reasoning_effort import normalize_reasoning_effort
from user_msg_lifecycle import emit_queued, new_lifecycle_msg_id, queued_payload
import session_store
import session_organization_store
import functools
import synthetic_messages
import virtual_session_store
import perf
import provider_setup
import itertools
from requirements_query_runner import (
    PROCESSOR_RESULT_TIMEOUT_SECONDS,
    REQUIREMENTS_PROCESSOR_EXECUTOR,
    REQUIREMENTS_SEARCH_EXECUTOR,
    run_requirements_processor_query,
    run_requirements_query,
)
import user_input_store
import file_panel_drafts
import file_preview_urls
import mobile_bundle_ticket
from secret_redaction import install_access_log_redaction, redact_secrets
from ws_serialization import (
    SerializedWebSocketFrame,
    dumps_ws_json,
    metric_event_type,
    reopen_ws_json_executor,
    shutdown_ws_json_executor,
)

_WS_OUTBOX_MAX_ITEMS = 256
_WS_OUTBOX_SEND_TIMEOUT_SECONDS = 2.0
_WS_OUTBOX_ENQUEUE_TIMEOUT_SECONDS = 2.0
_WS_OUTBOX_CLOSE_TIMEOUT_SECONDS = 1.0
import requirements_async_jobs

install_access_log_redaction()

_WS_FRAME_IDS = itertools.count(1)


class _WebSocketOutbox:
    def __init__(
        self,
        websocket,
        *,
        on_close,
        max_items: int = _WS_OUTBOX_MAX_ITEMS,
        send_timeout_s: float = _WS_OUTBOX_SEND_TIMEOUT_SECONDS,
        enqueue_timeout_s: float = _WS_OUTBOX_ENQUEUE_TIMEOUT_SECONDS,
        close_timeout_s: float = _WS_OUTBOX_CLOSE_TIMEOUT_SECONDS,
    ) -> None:
        self._websocket = websocket
        self._on_close = on_close
        self._queue: asyncio.Queue[
            tuple[int, float, dict, int, SerializedWebSocketFrame | None] | None
        ] = perf.LaggedQueue(
            maxsize=max_items,
            _perf_name="ws.outbox",
        )
        self._send_timeout_s = send_timeout_s
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
        if self._closed:
            perf.record_count("ws.outbox.rejected_closed")
            return False
        perf.record_count("ws.outbox.enqueue_depth", self._queue.qsize())
        queued_item = (
            next(_WS_FRAME_IDS), time.perf_counter(), event_dict, self._queue.qsize(), serialized,
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
            event_type = event_dict.get("type") if isinstance(event_dict, dict) else None
            perf.record_count("ws.outbox.rejected_timeout")
            _warning_off_loop(
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
            frame_id, queued_at, event_dict, enqueue_depth, serialized = queued_item
            writer_start_ms = (time.perf_counter() - queued_at) * 1000.0
            perf.record("ws.outbox.writer_start", writer_start_ms)
            metric_type = metric_event_type(event_dict)
            perf.record(
                f"ws.outbox.writer_start.type.{metric_type}",
                writer_start_ms,
            )
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

        def record_lag_overlap(finished_at: float) -> None:
            evidence = globals().get("_LAG_LOOP_EVIDENCE")
            if not isinstance(evidence, dict):
                return
            sentinel_at = evidence.get("sentinel_at")
            if isinstance(sentinel_at, (int, float)) and queued_at <= sentinel_at <= finished_at:
                perf.record_count("ws.phase.lag_overlap")
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
            await asyncio.wait_for(
                self._websocket.send_text(text),
                timeout=self._send_timeout_s,
            )
            wire_ms = (time.perf_counter() - wire_t) * 1000.0
            wire_end_at = time.perf_counter()
            record_lag_overlap(wire_end_at)
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
                _warning_off_loop(
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
        except asyncio.TimeoutError:
            record_lag_overlap(time.perf_counter())
            wire_ms = (
                (time.perf_counter() - wire_t) * 1000.0
                if wire_t is not None
                else 0.0
            )
            if wire_t is not None:
                perf.record("ws.send_json.wire", wire_ms)
            _warning_off_loop(
                "closing slow WebSocket: send timeout type=%s wire_ms=%.1f "
                "bytes=%d conn=%s frame=%d enqueue_depth=%d current_depth=%d",
                event_type,
                wire_ms,
                payload_bytes,
                self._connection_id,
                frame_id,
                enqueue_depth,
                self._queue.qsize(),
            )
            await self._close(cancel_writer=False)
            return
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
            _warning_off_loop(
                "slow WebSocket send type=%s elapsed_ms=%.1f",
                event_type,
                elapsed_ms,
            )

# Resolved once at import time — stable for the process lifetime.
_GIT_HASH: str | None = None
try:
    _GIT_HASH = subprocess.check_output(
        ["git", "rev-parse", "--short", "HEAD"], stderr=subprocess.DEVNULL
    ).decode().strip()
except Exception:
    pass

# True when the user explicitly requests shutdown (Ctrl+C / SIGINT).
# Uvicorn reload sends SIGTERM, not SIGINT, so we use this to avoid
# killing runner processes during a hot reload — they'll be re-attached
# by run_recovery on the next startup.
_intentional_shutdown = False
_uvicorn_sigint_handler = None
# Whether `on_shutdown` will kill runner subprocesses. The fail-safe
# baseline is False: restarts and ambiguous shutdowns must leave runners
# alive for run_recovery to re-attach on the next boot. Only an explicit
# affirmative prompt answer or supervisor kill flag flips this to True.
_kill_runners_on_shutdown = False
# Set by `_sigint_flag_handler` only on the SECOND (or later) SIGINT.
# `on_shutdown`'s "kill? [y/N]" prompt races against this so a second
# Ctrl+C means "stop waiting — interpret as 'n' (don't kill)" without
# requiring any I/O inside the signal handler. The runs survive; the
# next backend start picks them up via run_recovery. threading.Event
# because the prompt is awaited via
# `asyncio.to_thread(sys.stdin.readline)` — the wait happens on a
# worker thread.
_second_sigint_event = threading.Event()
# SIGINT count for the current process lifetime. The first SIGINT only
# flags intentional shutdown; only subsequent ones arm
# `_second_sigint_event`. Signal handlers run on the main thread
# between bytecodes, so a plain int suffices — no lock needed.
_sigint_count = 0

_session_event_meta_cache: dict[
    str,
    tuple[tuple[int, int], bool, int, dict[str, int]],
] = {}
_session_organization_refresh_task: asyncio.Task | None = None
_session_organization_refresh_pending = False
_session_event_meta_warm_inflight: set[str] = set()
_SESSION_EVENT_META_WARM_LIMIT = 20
_sessions_list_response_cache: dict[
    tuple,
    tuple[float, bytes, tuple[int, int, int]],
] = {}
_session_summaries_response_cache: dict[
    tuple,
    tuple[float, bytes, tuple[int, int, int]],
] = {}
_sidebar_payload_cache: dict[int, tuple[str, dict]] = {}
_sidebar_decorated_cache: dict[tuple, dict] = {}
_sidebar_state_snapshot_cache: tuple[
    tuple[int, int, int],
    tuple[set[str], dict[str, str], dict[str, int], dict[str, int]],
] | None = None
_remote_sessions_cache: dict[str, tuple[float, list[dict]]] = {}
_remote_sessions_cache_lock = threading.Lock()
_remote_sessions_refresh_tasks: set[str] = set()
_remote_sessions_cache_version = 0
_virtual_sessions_recent_refresh_task: asyncio.Task | None = None
_session_list_user_prefs_cache: tuple[float, tuple[bool, str, bool]] | None = None
_local_visible_order_cache: dict[
    tuple[str, str | None, int, int, int],
    tuple[list[str], int],
] = {}
_session_detail_response_cache: collections.OrderedDict[tuple, bytes] = (
    collections.OrderedDict()
)
_session_detail_response_cache_latest: dict[tuple[str, int, Optional[int]], tuple] = {}
_SESSIONS_LIST_RESPONSE_TTL_SECONDS = 15.0
_REMOTE_SESSIONS_CACHE_TTL_SECONDS = 2.0
_SESSION_LIST_USER_PREFS_TTL_SECONDS = 1.0
_SESSION_DETAIL_RESPONSE_CACHE_MAX = 64
_SESSION_LIST_CONTENT_SEARCH_MAX_WAIT_SECONDS = 0.05
_SESSION_LIST_SEARCH_MIN_CANDIDATES = 200
_SESSION_LIST_SUMMARY_WARM_WAIT_SECONDS = 0.08
_SESSION_LIST_SUMMARY_WARM_MIN_PUBLISHED = 50
_SIDEBAR_PAYLOAD_CACHE_MAX = 4096
_SIDEBAR_DECORATED_CACHE_MAX = 1024
_machine_nodes_enabled_cache: tuple[float, bool] | None = None
_machine_nodes_enabled_refresh_task: asyncio.Task | None = None
_MACHINE_NODES_ENABLED_TTL_SECONDS = 2.0
_HOT_PATH_EXECUTOR = ThreadPoolExecutor(
    max_workers=8,
    thread_name_prefix="hot-path",
)
_SESSION_DETAIL_EXECUTOR = ThreadPoolExecutor(
    max_workers=4,
    thread_name_prefix="session-detail",
)
_SESSION_LIST_EXECUTOR = ThreadPoolExecutor(
    max_workers=4,
    thread_name_prefix="session-list",
)


async def _run_hot_path(name: str, fn, /, *args, **kwargs):
    queued_at = time.perf_counter()
    ctx = contextvars.copy_context()

    def _call():
        perf.record(f"{name}.queue_wait", (time.perf_counter() - queued_at) * 1000)
        return ctx.run(fn, *args, **kwargs)

    start = time.perf_counter()
    try:
        return await asyncio.get_running_loop().run_in_executor(
            _HOT_PATH_EXECUTOR,
            _call,
        )
    finally:
        perf.record(name, (time.perf_counter() - start) * 1000)


async def _run_session_detail_hot_path(name: str, fn, /, *args, **kwargs):
    queued_at = time.perf_counter()
    ctx = contextvars.copy_context()

    def _call():
        perf.record(f"{name}.queue_wait", (time.perf_counter() - queued_at) * 1000)
        return ctx.run(fn, *args, **kwargs)

    start = time.perf_counter()
    try:
        return await asyncio.get_running_loop().run_in_executor(
            _SESSION_DETAIL_EXECUTOR,
            _call,
        )
    finally:
        perf.record(name, (time.perf_counter() - start) * 1000)


async def _run_session_list_hot_path(name: str, fn, /, *args, **kwargs):
    queued_at = time.perf_counter()
    ctx = contextvars.copy_context()

    def _call():
        perf.record(f"{name}.queue_wait", (time.perf_counter() - queued_at) * 1000)
        return ctx.run(fn, *args, **kwargs)

    start = time.perf_counter()
    try:
        return await asyncio.get_running_loop().run_in_executor(
            _SESSION_LIST_EXECUTOR,
            _call,
        )
    finally:
        perf.record(name, (time.perf_counter() - start) * 1000)


def _streaming_assistant_message_id(session: dict) -> Optional[str]:
    messages = session.get("messages") if isinstance(session, dict) else None
    if not isinstance(messages, list):
        return None
    for msg in reversed(messages):
        if isinstance(msg, dict) and msg.get("role") == "assistant" and msg.get("isStreaming"):
            msg_id = msg.get("id")
            return msg_id if isinstance(msg_id, str) and msg_id else None
    return None


def _append_selector_change_anchor(session_id: str) -> Optional[str]:
    now = datetime.now(timezone.utc).isoformat()
    msg_id = f"model-switch-{uuid.uuid4()}"
    anchor = {
        "id": msg_id,
        "role": "assistant",
        "content": "",
        "events": [],
        "timestamp": now,
        "isStreaming": False,
        "completed_at": now,
        "source": "selector_change",
    }
    if session_manager.append_assistant_msg(session_id, anchor) is None:
        return None
    return msg_id


def _record_model_switched_event(
    session_id: str,
    before: dict,
    after: dict,
    updates: dict,
) -> None:
    keys = ("model", "provider_id", "reasoning_effort")
    changed = [
        key for key in keys
        if key in updates and before.get(key) != after.get(key)
    ]
    if not changed:
        return
    msg_id = _streaming_assistant_message_id(after)
    root_id = session_manager._root_id_for(session_id)
    if not root_id:
        return
    if not msg_id:
        if not after.get("messages"):
            return
        msg_id = _append_selector_change_anchor(session_id)
        if not msg_id:
            return

    provider = config_store.get_provider(after.get("provider_id"))
    previous_provider = config_store.get_provider(before.get("provider_id"))
    data = {
        "uuid": f"model-switch-{uuid.uuid4()}",
        "model": after.get("model"),
        "provider_id": after.get("provider_id"),
        "provider_name": (provider or {}).get("name"),
        "provider_kind": (provider or {}).get("kind"),
        "reasoning_effort": after.get("reasoning_effort"),
        "previous_model": before.get("model"),
        "previous_provider_id": before.get("provider_id"),
        "previous_provider_name": (previous_provider or {}).get("name"),
        "previous_provider_kind": (previous_provider or {}).get("kind"),
        "previous_reasoning_effort": before.get("reasoning_effort"),
        "changed": changed,
        "app_session_id": session_id,
        "msg_id": msg_id,
    }
    event = {"type": "model_switched", "data": data}
    session_manager.append_native_event(session_id, msg_id, event)
    from event_journal import publish_event_sync
    publish_event_sync(
        session_id=root_id,
        context_id=session_id,
        event_type="model_switched",
        data=data,
        source="selector_change",
        message_id=msg_id,
        timeout=30,
    )


def _session_event_file_fingerprint(root_id: str) -> tuple[int, int]:
    path = event_ingester._events_path(root_id)
    try:
        st = path.stat()
    except FileNotFoundError:
        return (0, 0)
    return (int(st.st_mtime_ns), int(st.st_size))


def _session_event_meta(root_id: str) -> tuple[bool, int, dict[str, int]]:
    fingerprint = _session_event_file_fingerprint(root_id)
    cached = _session_event_meta_cache.get(root_id)
    if cached is not None and cached[0] == fingerprint:
        return cached[1], cached[2], dict(cached[3])

    has_events, barrier_seq, max_context = event_ingester.session_event_meta(root_id)
    _session_event_meta_cache[root_id] = (
        fingerprint,
        has_events,
        barrier_seq,
        dict(max_context),
    )
    return has_events, barrier_seq, dict(max_context)


def _session_detail_watermarks(
    root_id: str,
    has_events: bool,
    barrier_seq: int,
    max_context: dict[str, int],
) -> dict[str, int]:
    if max_context:
        return dict(max_context)
    if has_events and barrier_seq > 0:
        return {root_id: barrier_seq}
    return {}


def _session_event_meta_cache_fresh(root_id: str) -> bool:
    cached = _session_event_meta_cache.get(root_id)
    return cached is not None and cached[0] == _session_event_file_fingerprint(root_id)


def _session_event_meta_roots_for_page(page: list[dict]) -> list[str]:
    root_ids: list[str] = []
    seen: set[str] = set()
    for session in page:
        if len(root_ids) >= _SESSION_EVENT_META_WARM_LIMIT:
            break
        if session.get("node_id") not in (None, "primary"):
            continue
        root_id = session.get("id")
        if not isinstance(root_id, str) or not root_id or root_id in seen:
            continue
        if int(session.get("message_count") or 0) <= 0:
            continue
        seen.add(root_id)
        root_ids.append(root_id)
    return root_ids


async def _warm_session_event_meta_roots(root_ids: list[str]) -> None:
    pending: list[str] = []
    for root_id in root_ids:
        if root_id in _session_event_meta_warm_inflight:
            continue
        _session_event_meta_warm_inflight.add(root_id)
        pending.append(root_id)
    if not pending:
        return

    try:
        await asyncio.to_thread(_warm_session_event_meta_roots_sync, pending)
    finally:
        for root_id in pending:
            _session_event_meta_warm_inflight.discard(root_id)


def _warm_session_event_meta_roots_sync(root_ids: list[str]) -> None:
    for root_id in root_ids:
        try:
            _session_event_meta(root_id)
        except Exception:
            logger.debug("session event meta warm failed for %s", root_id, exc_info=True)


def _schedule_session_event_meta_warm(page: list[dict]) -> None:
    root_ids = _session_event_meta_roots_for_page(page)
    if root_ids:
        task = asyncio.create_task(_warm_session_event_meta_roots(root_ids))
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)


def _machine_nodes_enabled_cached() -> bool:
    global _machine_nodes_enabled_cache, _machine_nodes_enabled_refresh_task
    now = time.monotonic()
    cached = _machine_nodes_enabled_cache
    if (
        cached is not None
        and now - cached[0] <= _MACHINE_NODES_ENABLED_TTL_SECONDS
    ):
        return cached[1]
    if cached is not None:
        if _machine_nodes_enabled_refresh_task is None or _machine_nodes_enabled_refresh_task.done():
            async def _refresh() -> None:
                global _machine_nodes_enabled_cache
                try:
                    enabled = await asyncio.to_thread(
                        _builtin_extension_runtime_ready,
                        extension_store.extension_id_for_role('machine-nodes'),
                    )
                except Exception:
                    logger.debug("machine nodes enabled refresh failed", exc_info=True)
                    return
                _machine_nodes_enabled_cache = (time.monotonic(), enabled)

            _machine_nodes_enabled_refresh_task = asyncio.create_task(_refresh())
        return cached[1]
    enabled = _builtin_extension_runtime_ready(
        extension_store.extension_id_for_role('machine-nodes'),
    )
    _machine_nodes_enabled_cache = (now, enabled)
    return enabled


def _sessions_list_response(content: bytes) -> Response:
    return Response(content=content, media_type="application/json")


def _json_bytes_response(value: dict) -> Response:
    content = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return Response(content=content, media_type="application/json")


async def _json_bytes_response_async(value: dict) -> Response:
    content = await asyncio.to_thread(
        json.dumps,
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )
    return Response(content=content.encode("utf-8"), media_type="application/json")


def _sessions_list_response_maybe_cache(
    cache_key: tuple,
    value: dict,
    *,
    cache_response: bool,
) -> Response:
    if cache_response:
        return _sessions_list_cache_put(cache_key, value)
    return _sessions_list_response(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _sessions_snapshot_payload(value: dict) -> dict:
    snapshot_complete = session_store.summary_index_snapshot_complete()
    index_warming = (
        not snapshot_complete
        and session_store.summary_index_has_roots_on_disk()
    )
    return {
        **value,
        "snapshot_complete": snapshot_complete or not index_warming,
        "index_warming": index_warming,
    }


def _session_detail_cache_get(key: tuple) -> Response | None:
    content = _session_detail_response_cache.get(key)
    if content is None:
        return None
    _session_detail_response_cache.move_to_end(key)
    return Response(content=content, media_type="application/json")


def _session_detail_cache_has(key: tuple) -> bool:
    return key in _session_detail_response_cache


def _session_detail_cache_put(key: tuple, value: dict) -> Response:
    while len(_session_detail_response_cache) >= _SESSION_DETAIL_RESPONSE_CACHE_MAX:
        old_key, _ = _session_detail_response_cache.popitem(last=False)
        old_simple = _session_detail_simple_cache_key_from_full(old_key)
        if (
            old_simple is not None
            and _session_detail_response_cache_latest.get(old_simple) == old_key
        ):
            _session_detail_response_cache_latest.pop(old_simple, None)
    content = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    _session_detail_response_cache[key] = content
    simple_key = _session_detail_simple_cache_key_from_full(key)
    if simple_key is not None:
        _session_detail_response_cache_latest[simple_key] = key
    return Response(content=content, media_type="application/json")


async def _session_detail_cache_put_async(key: tuple, value: dict) -> Response:
    content_text = await asyncio.to_thread(
        json.dumps,
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )
    while len(_session_detail_response_cache) >= _SESSION_DETAIL_RESPONSE_CACHE_MAX:
        old_key, _ = _session_detail_response_cache.popitem(last=False)
        old_simple = _session_detail_simple_cache_key_from_full(old_key)
        if (
            old_simple is not None
            and _session_detail_response_cache_latest.get(old_simple) == old_key
        ):
            _session_detail_response_cache_latest.pop(old_simple, None)
    content = content_text.encode("utf-8")
    _session_detail_response_cache[key] = content
    simple_key = _session_detail_simple_cache_key_from_full(key)
    if simple_key is not None:
        _session_detail_response_cache_latest[simple_key] = key
    return Response(content=content, media_type="application/json")


def _session_detail_simple_cache_key_from_full(
    key: tuple,
) -> tuple[str, int, Optional[int]] | None:
    if (
        len(key) >= 2
        and isinstance(key[0], str)
        and isinstance(key[1], tuple)
        and len(key[1]) >= 3
        and isinstance(key[1][1], int)
        and (key[1][2] is None or isinstance(key[1][2], int))
    ):
        return (key[0], key[1][1], key[1][2])
    return None


def _sessions_list_transient_state_version() -> tuple[int, int, int]:
    return (
        coordinator.turn_manager.cached_state_version(),
        session_manager.unread_counts_version(),
        user_input_store.pending_counts_version_loaded(),
    )


def _sessions_list_cache_get(key: tuple) -> Response | None:
    cached = _sessions_list_response_cache.get(key)
    if cached is None:
        return None
    if time.monotonic() - cached[0] > _SESSIONS_LIST_RESPONSE_TTL_SECONDS:
        _sessions_list_response_cache.pop(key, None)
        return None
    if cached[2] != _sessions_list_transient_state_version():
        _sessions_list_response_cache.pop(key, None)
        return None
    return _sessions_list_response(cached[1])


def _sessions_list_cache_put(key: tuple, value: dict) -> Response:
    if len(_sessions_list_response_cache) >= 64:
        oldest = min(
            _sessions_list_response_cache,
            key=lambda item: _sessions_list_response_cache[item][0],
        )
        _sessions_list_response_cache.pop(oldest, None)
    content = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    _sessions_list_response_cache[key] = (
        time.monotonic(),
        content,
        _sessions_list_transient_state_version(),
    )
    return _sessions_list_response(content)


def _session_summaries_cache_get(key: tuple) -> Response | None:
    cached = _session_summaries_response_cache.get(key)
    if cached is None:
        return None
    if time.monotonic() - cached[0] > _SESSIONS_LIST_RESPONSE_TTL_SECONDS:
        _session_summaries_response_cache.pop(key, None)
        return None
    return _sessions_list_response(cached[1])


def _session_summaries_cache_put(key: tuple, value: dict) -> Response:
    if len(_session_summaries_response_cache) >= 64:
        oldest = min(
            _session_summaries_response_cache,
            key=lambda item: _session_summaries_response_cache[item][0],
        )
        _session_summaries_response_cache.pop(oldest, None)
    content = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    _session_summaries_response_cache[key] = (
        time.monotonic(),
        content,
        0,
    )
    return _sessions_list_response(content)


def _sessions_list_cache_version(search_query: str, search_fields: set[str]) -> tuple[int, int | None] | int:
    if search_query:
        content_generation = None
        if session_store.SEARCH_FIELD_CONTENT in search_fields:
            import session_search_index
            content_generation = session_search_index.generation()
        return (session_store.search_metadata_version(), content_generation)
    return (session_store.summary_version(), virtual_session_store.version_token())


def _sessions_list_content_search_ready(
    search_query: str,
    search_fields: set[str],
    *,
    offset: int,
    limit: int,
) -> bool:
    if (
        not search_query
        or session_store.SEARCH_FIELD_CONTENT not in search_fields
    ):
        return True
    import session_search_index
    return session_search_index.has_cached_result(
        search_query,
        _session_search_candidate_limit(offset, limit),
    )


def _remote_sessions_cache_version_snapshot() -> int:
    with _remote_sessions_cache_lock:
        return _remote_sessions_cache_version


def _copy_remote_sessions(sessions: list[dict], *, limit: int | None = None) -> list[dict]:
    out: list[dict] = []
    for session in sessions:
        if isinstance(session, dict):
            out.append(dict(session))
            if limit is not None and len(out) >= limit:
                break
    return out


def _remote_sessions_cache_get(
    node_id: str,
    *,
    limit: int | None = None,
) -> tuple[list[dict] | None, bool, int]:
    with _remote_sessions_cache_lock:
        cached = _remote_sessions_cache.get(node_id)
    if cached is None:
        return None, False, 0
    age = time.monotonic() - cached[0]
    sessions = cached[1]
    return (
        _copy_remote_sessions(sessions, limit=limit),
        age <= _REMOTE_SESSIONS_CACHE_TTL_SECONDS,
        len(sessions),
    )


def _remote_sessions_cache_put(node_id: str, sessions: list[dict]) -> None:
    global _remote_sessions_cache_version
    clean = _copy_remote_sessions(sessions)
    with _remote_sessions_cache_lock:
        existing = _remote_sessions_cache.get(node_id)
        if existing is not None and existing[1] == clean:
            _remote_sessions_cache[node_id] = (time.monotonic(), clean)
            return
        _remote_sessions_cache[node_id] = (time.monotonic(), clean)
        _remote_sessions_cache_version += 1


async def _fetch_remote_sessions_live(node_id: str) -> list[dict]:
    import node_link as _nl

    resp = await _nl.rpc_call(
        node_id,
        "list_sessions",
        {},
        timeout=REMOTE_SESSION_MERGE_TIMEOUT_SECONDS,
    )
    sessions = (resp or {}).get("sessions", [])
    return _copy_remote_sessions(sessions if isinstance(sessions, list) else [])


def _schedule_remote_sessions_refresh(node_id: str) -> None:
    with _remote_sessions_cache_lock:
        if node_id in _remote_sessions_refresh_tasks:
            return
        _remote_sessions_refresh_tasks.add(node_id)

    async def _refresh() -> None:
        try:
            sessions = await _fetch_remote_sessions_live(node_id)
            _remote_sessions_cache_put(node_id, sessions)
        except Exception:
            logger.debug("get_sessions: cached remote refresh from %s failed", node_id, exc_info=True)
        finally:
            with _remote_sessions_cache_lock:
                _remote_sessions_refresh_tasks.discard(node_id)

    asyncio.create_task(_refresh())


async def _remote_sessions_for_sidebar(node_id: str) -> list[dict]:
    cached, fresh, _total = _remote_sessions_cache_get(node_id)
    if cached is not None:
        if fresh:
            perf.record("sessions.list.remote_cache.hit", 1.0)
        else:
            perf.record("sessions.list.remote_cache.stale", 1.0)
            _schedule_remote_sessions_refresh(node_id)
        return cached
    perf.record("sessions.list.remote_cache.miss", 1.0)
    sessions = await _fetch_remote_sessions_live(node_id)
    _remote_sessions_cache_put(node_id, sessions)
    return sessions


def _remote_sessions_for_sidebar_cached(
    node_id: str,
    *,
    limit: int | None = None,
) -> tuple[list[dict], int] | None:
    cached, fresh, total = _remote_sessions_cache_get(node_id, limit=limit)
    if cached is None:
        perf.record("sessions.list.remote_cache.deferred_miss", 1.0)
        _schedule_remote_sessions_refresh(node_id)
        return None
    if fresh:
        perf.record("sessions.list.remote_cache.deferred_hit", 1.0)
    else:
        perf.record("sessions.list.remote_cache.deferred_stale", 1.0)
        _schedule_remote_sessions_refresh(node_id)
    return cached, total


def _schedule_virtual_sessions_recent_refresh(limit: int) -> None:
    global _virtual_sessions_recent_refresh_task
    existing = _virtual_sessions_recent_refresh_task
    if existing is not None and not existing.done():
        return

    async def _refresh() -> None:
        await asyncio.to_thread(
            virtual_session_store.list_recent,
            limit,
            exclude_id=session_search.ASK_SINGLETON_ID,
        )

    _virtual_sessions_recent_refresh_task = asyncio.create_task(_refresh())


def _session_list_user_prefs() -> tuple[bool, str, bool]:
    global _session_list_user_prefs_cache
    now = time.monotonic()
    cached = _session_list_user_prefs_cache
    if cached is not None and now - cached[0] <= _SESSION_LIST_USER_PREFS_TTL_SECONDS:
        return cached[1]
    prefs = user_prefs.get_all()
    folder_view_enabled = prefs.get(
        "folder_view_enabled",
        user_prefs.DEFAULT_FOLDER_VIEW_ENABLED,
    )
    session_sort = prefs.get("session_sort", user_prefs.DEFAULT_SESSION_SORT)
    if session_sort not in user_prefs.SESSION_SORT_VALUES:
        session_sort = user_prefs.DEFAULT_SESSION_SORT
    session_status_sort = prefs.get(
        "session_status_sort",
        user_prefs.DEFAULT_SESSION_STATUS_SORT,
    )
    resolved = (
        bool(folder_view_enabled),
        session_sort,
        bool(session_status_sort),
    )
    _session_list_user_prefs_cache = (now, resolved)
    return resolved


def _invalidate_session_list_user_prefs_cache() -> None:
    global _session_list_user_prefs_cache
    _session_list_user_prefs_cache = None


_GIT_STATUS_TTL_SECONDS = 60.0
_GIT_STATUS_STARTUP_WARM_LIMIT = 8
_git_status_cache: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
_git_status_inflight: dict[tuple[str, str], asyncio.Task] = {}
_git_status_cache_lock = asyncio.Lock()


def _clear_git_status_cache(node_id: str | None = None, cwd: str | None = None) -> None:
    if node_id is None and cwd is None:
        _git_status_cache.clear()
        return
    for key in list(_git_status_cache):
        key_node, key_cwd = key
        if node_id is not None and key_node != node_id:
            continue
        if cwd is not None and key_cwd != cwd:
            continue
        _git_status_cache.pop(key, None)


async def _cached_git_status(node_id: str, cwd: str) -> dict:
    key = (node_id, cwd)
    now = time.monotonic()
    async with _git_status_cache_lock:
        cached = _git_status_cache.get(key)
        if cached and now - cached[0] <= _GIT_STATUS_TTL_SECONDS:
            return dict(cached[1])
        task = _git_status_inflight.get(key)
        if cached:
            if task is None:
                task = asyncio.create_task(_file_op(node_id, "get_git_status", {"cwd": cwd}))
                _git_status_inflight[key] = task
                task.add_done_callback(
                    lambda done, key=key: asyncio.create_task(
                        _store_git_status_refresh(key, done),
                    ),
                )
            return dict(cached[1])
        if task is None:
            task = asyncio.create_task(_file_op(node_id, "get_git_status", {"cwd": cwd}))
            _git_status_inflight[key] = task

    try:
        result = await task
    finally:
        async with _git_status_cache_lock:
            if _git_status_inflight.get(key) is task:
                _git_status_inflight.pop(key, None)

    if isinstance(result, dict):
        async with _git_status_cache_lock:
            _git_status_cache[key] = (time.monotonic(), dict(result))
        return dict(result)
    return result


async def _store_git_status_refresh(
    key: tuple[str, str],
    task: asyncio.Task,
) -> None:
    try:
        result = task.result()
    except Exception:
        result = None
    async with _git_status_cache_lock:
        if _git_status_inflight.get(key) is task:
            _git_status_inflight.pop(key, None)
        if isinstance(result, dict):
            _git_status_cache[key] = (time.monotonic(), dict(result))


async def _warm_recent_git_statuses() -> None:
    try:
        projects = await asyncio.to_thread(project_store.list_projects)
    except Exception:
        logger.debug("git-status startup warm: project list failed", exc_info=True)
        return
    warmed = 0
    seen: set[tuple[str, str]] = set()
    for project in projects:
        node_id = str(project.get("node_id") or "primary")
        cwd = str(project.get("path") or "")
        if node_id != "primary" or not cwd:
            continue
        key = (node_id, cwd)
        if key in seen:
            continue
        seen.add(key)
        try:
            await _cached_git_status(node_id, cwd)
        except Exception:
            logger.debug("git-status startup warm failed cwd=%s", cwd, exc_info=True)
        warmed += 1
        if warmed >= _GIT_STATUS_STARTUP_WARM_LIMIT:
            break


def _shutdown_kill_runners_flag() -> Path:
    return ba_home() / "kill_runners_requested"


def _consume_shutdown_kill_runners_flag() -> bool:
    flag = _shutdown_kill_runners_flag()
    if not flag.exists():
        return False
    try:
        flag.unlink()
    except OSError:
        pass
    return True


def _sigint_flag_handler(signum, frame):
    """Signal-safe: mutate flags + chain to uvicorn. NEVER block here.

    The interactive "kill running Claude/Gemini processes? [y/N]" prompt
    runs inside `on_shutdown` (off the signal frame, on the event loop)
    via `asyncio.to_thread`. Doing the prompt in the signal handler
    would (1) freeze the event loop while readline blocks, (2) re-enter
    `sys.stdin.readline()` on the second Ctrl+C (uvloop redelivers
    SIGINT through `_invoke_signals`, which can fire the handler again
    while the outer readline is parked → `RuntimeError: reentrant call
    inside <BufferedReader>`), and (3) interleave its prompt bytes with
    concurrent logger output on the same stderr stream.
    """
    global _intentional_shutdown, _sigint_count
    _intentional_shutdown = True
    _sigint_count += 1
    # Only the SECOND+ SIGINT arms the abort event — the first must
    # leave it clear so `_prompt_kill_runners` can actually show the
    # prompt (sidecar: "MUST prompt the user" on Ctrl+C in an
    # interactive terminal). Once armed, `_prompt_kill_runners`
    # treats the abort as an "n" answer (don't kill subprocesses).
    # The user's intent on a
    # double-tap is "stop now without nuking my running tasks".
    if _sigint_count >= 2:
        _second_sigint_event.set()
    if callable(_uvicorn_sigint_handler):
        _uvicorn_sigint_handler(signum, frame)


async def _prompt_kill_runners() -> None:
    """Ask the user whether to kill runner subprocesses, off the signal
    frame. Sets `_kill_runners_on_shutdown` based on the answer.

    Decision matrix (TTY only — non-TTY defaults to leaving runners alive):
    - explicit "y"/"yes"             → kill
    - Enter / empty / anything else  → don't kill
    - explicit "n"/"no"              → don't kill
    - **second Ctrl+C** during prompt → don't kill (treated as "n")

    The second-Ctrl+C-as-"n" shortcut exists because users impatiently
    double-tap Ctrl+C and previously lost their long-running Claude
    runs; the safer interpretation of "I just want this to stop NOW"
    is "leave the runs alone — recovery picks them up on next start".
    See requirements-main.py.md.
    """
    global _kill_runners_on_shutdown
    if not (sys.stdin.isatty() and sys.stderr.isatty()):
        # Non-interactive (desktop .app SIGINT, `kill -INT`, containers):
        # can't ask the user, so leave runners alive — they're detached and
        # run_recovery re-attaches them on the next boot.
        _kill_runners_on_shutdown = False
        return
    if _second_sigint_event.is_set():
        # Second Ctrl+C arrived before we could even render the prompt
        # → treat as "n", don't kill the runners.
        _kill_runners_on_shutdown = False
        return
    try:
        sys.stderr.write(
            "\n^C — kill running Claude/Gemini processes too? "
            "[y/N]  (Ctrl+C again = n): "
        )
        sys.stderr.flush()
    except OSError:
        return
    # Race the readline against a second SIGINT. Both waits run on
    # worker threads — cancelling the asyncio Future doesn't unblock the
    # underlying thread, but the process is shutting down so leaked
    # threads are harmless.
    read_task = asyncio.create_task(asyncio.to_thread(sys.stdin.readline))
    abort_task = asyncio.create_task(asyncio.to_thread(_second_sigint_event.wait))
    try:
        done, _ = await asyncio.wait(
            {read_task, abort_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if abort_task in done and read_task not in done:
            # Second Ctrl+C interrupted the prompt → "n".
            _kill_runners_on_shutdown = False
        elif read_task in done and read_task.exception() is None:
            answer = (read_task.result() or "").strip().lower()
            _kill_runners_on_shutdown = answer in ("y", "yes")
    finally:
        # task.cancel() only unwraps the asyncio Future; the underlying
        # thread keeps blocking. Set the event so abort_task's
        # `_second_sigint_event.wait` returns and its executor thread
        # exits — otherwise ThreadPoolExecutor's atexit join blocks
        # process exit forever after uvicorn prints "Finished server
        # process". The stdin thread already returned (we have an
        # answer) so it doesn't need a kick.
        _second_sigint_event.set()
        for task in (read_task, abort_task):
            if not task.done():
                task.cancel()

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, Header, HTTPException, Body, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from starlette.websockets import WebSocketState
from fastapi.responses import FileResponse

import config_store
import pre_send_advisory
import shortcut_picker
import user_prefs
import auto_restart_on_idle
import ui_selection

# Apply saved auth env vars at import time so any code path that still
# reads `os.environ` directly (e.g. `runner.py` jsonl-path resolution)
# sees the active provider's `CLAUDE_CONFIG_DIR`. Subprocess spawns now
# go through `Provider.build_env()` and don't depend on this — but
# in-process fallbacks still might.
#
# `warm_keyring_cache()` BEFORE `apply_env_vars()`: this pulls every
# api_key provider's secret into the in-process cache so subsequent
# request-hot-path callers (`get_default_provider`, `_strip`'s
# `has_api_key` probe, every `apply_env_vars` reapply at prompt-send)
# hit the cache instead of macOS Keychain. The 2s-timeout-per-call risk
# in `_keyring_call` is paid ONCE at startup, off the event loop. See
# the comment over `_api_key_cache` in `config_store.py`.
config_store.warm_keyring_cache()
config_store.apply_env_vars()

from pydantic import BaseModel

from provider import default_provider, load_all_providers, recover_all_in_flight, known_providers
from orchestrator import Coordinator, build_semantic_alter_prompt
from run_recovery import integrate_recovered_runs, shutdown_recovery_lease_executor
from event_ingester import event_ingester
from session_manager import manager as session_manager
from session_manager import (
    IncompatibleOrchestrationMode,
    DelegateForkParentMissing,
    reopen_reconciles,
    session_matches_project,
    shutdown_reconciles,
    strip_link_marker_syntax,
)
from session_store import _session_path
import session_migrate
import runs_dir
import file_browser
import analytics
import project_store
import project_mapping_store
from stores import pending_approvals
import tool_approval
import prompt_engineer
import file_editor
import project_config
import session_search
import session_bridge
import assistant_ui
import coordination
import project_update_store
import project_structure_edit_session
import virtual_session_prompt_handlers
import extension_store

# Log directory is intentionally captured at module-load. The
# "no module-load Path caching" rule (CLAUDE.md, A12) applies to
# STATE storage (sessions, traces) — observability
# is configured exactly once at process boot and the `FileHandler`
# below binds a single Path into logging's machinery regardless of
# how we resolve it here. Tests that need isolated logs must set
# `BETTER_CLAUDE_HOME` BEFORE importing `main`.
_log_dir = ba_home() / "logs"
_log_dir.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(_log_dir / "backend.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)
_LOG_WRITE_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="log-write")
_project_match_executor: ProcessPoolExecutor | None = None
_project_match_ready = False
_project_match_warm_task: asyncio.Task | None = None


def _ensure_project_match_executor() -> ProcessPoolExecutor:
    global _project_match_executor, _project_match_ready
    if _project_match_executor is None:
        _project_match_executor = ProcessPoolExecutor(max_workers=1)
        _project_match_ready = False
    return _project_match_executor


try:
    faulthandler.enable()
    faulthandler.register(signal.SIGUSR1, file=sys.stderr, all_threads=True, chain=False)
except Exception:
    logger.debug("faulthandler enable failed", exc_info=True)
frontend_logger = logging.getLogger("frontend")
frontend_logger.setLevel(logging.DEBUG)
frontend_logger.propagate = False
if not frontend_logger.handlers:
    _frontend_handler = logging.FileHandler(_log_dir / "frontend.log", encoding="utf-8")
    _frontend_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-5s %(message)s"))
    frontend_logger.addHandler(_frontend_handler)


def _warning_off_loop(message: str, *args: Any) -> None:
    _LOG_WRITE_EXECUTOR.submit(logger.warning, message, *args)


def _frontend_log_off_loop(level: int, line: str) -> None:
    _LOG_WRITE_EXECUTOR.submit(frontend_logger.log, level, line)


def create_api_app() -> FastAPI:
    """Create the FastAPI API app before frontend static files are mounted.

    Route registration in this module still happens via decorators on the
    module-level `app`; tests that need API-only import set
    `BETTER_CLAUDE_API_ONLY=1` before importing `main`.
    """
    return FastAPI(title="Better Agent")


app = create_api_app()
REMOTE_SESSION_MERGE_TIMEOUT_SECONDS = 0.75

# CORS, auth_gate, SessionMiddleware, and ingest_command_received are
# registered AFTER `coordinator` is created (we need its internal_token
# in auth_gate). See the block below `coordinator = Coordinator()`.


@app.exception_handler(IncompatibleOrchestrationMode)
async def _incompatible_orchestration_mode_handler(_request, exc):
    """Layer-2 capability gate raised inside `session_manager.create`
    surfaces here as a 400 instead of a 500. Catches HTTP `POST
    /api/sessions` + Team Orchestration worker creation + any future route that mints
    a session through the manager. CLI / tests still see the raw
    exception (no FastAPI middleware in those paths)."""
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=400, content={"detail": str(exc)})


_COMMAND_METHODS = {"POST", "PATCH", "PUT", "DELETE"}
_COMMAND_JOURNAL_EXCLUDED_SUFFIXES = ("/draft",)


def _extract_command_sid(path: str) -> Optional[str]:
    """Return the session id this request mutates, or None if the path
    is not a per-session mutation route.

    Covers two prefixes the frontend actually uses for session-scoped
    state changes: `/api/sessions/<sid>/...` and `/api/file-editor/<sid>/...`.
    A bare `/api/sessions` (the create-session endpoint) returns None —
    the new session's existence is captured in `session_store`; the
    follow-up state-mutating calls each carry sid.
    """
    if path.endswith(_COMMAND_JOURNAL_EXCLUDED_SUFFIXES):
        return None
    parts = path.split("/")
    # ["", "api", <root>, <sid>, ...]
    if len(parts) >= 5 and parts[1] == "api" and parts[2] in {"sessions", "file-editor"}:
        sid = parts[3]
        if sid:
            return sid
    return None


@app.middleware("http")
async def perf_timing(request, call_next):
    # INVARIANT: this is the innermost middleware so the recorded
    # duration is handler-only (auth/session/CORS overhead is not
    # included). Route template is only populated on `request.scope`
    # AFTER routing inside `call_next`, so it's looked up post-call.
    t0 = time.perf_counter()
    try:
        return await call_next(request)
    finally:
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        # Three populated-scope cases after routing:
        #   (a) FastAPI `APIRoute` → `scope["route"].path` is the template.
        #   (b) Starlette `Mount` (e.g. SPA static files) → `scope["route"]`
        #       is NOT set; `scope["endpoint"]` is the mounted app. Without
        #       this branch every static asset hit would bucket as
        #       `unmatched`, polluting the 404 signal.
        #   (c) No match → both None → `unmatched`.
        route = request.scope.get("route")
        template = getattr(route, "path", None)
        if template is None:
            endpoint = request.scope.get("endpoint")
            if endpoint is not None:
                template = f"mount:{type(endpoint).__name__}"
            else:
                template = "unmatched"
        perf.record(f"rest.{request.method}.{template}", elapsed_ms)


@app.middleware("http")
async def ingest_command_received(request, call_next):
    """Persist every inbound state-mutating REST request as a
    `command_received` event in the target session's events.jsonl,
    BEFORE the handler runs. This is the structural guardrail that
    makes the durable log a complete record of frontend → backend
    inputs (without it, only the downstream worker/manager effects
    appear; "user clicked rewind to seq=42" is invisible).

    Body re-injection pattern: Starlette consumes the request body
    via a single `receive` callable; reading it here exhausts the
    stream. We capture the bytes, ingest, then patch `_receive` so
    downstream Pydantic/`Body(...)` parsing sees the bytes again.
    """
    if request.method not in _COMMAND_METHODS:
        return await call_next(request)
    sid = _extract_command_sid(request.url.path)
    if not sid:
        return await call_next(request)
    body_bytes = await request.body()
    payload: Any
    if body_bytes:
        try:
            payload = json.loads(body_bytes)
        except Exception:
            payload = {"_raw": body_bytes.decode("utf-8", errors="replace")}
    else:
        payload = {}
    try:
        from event_journal import publish_event
        root_id = await asyncio.to_thread(session_manager._root_id_for, sid) or sid
        await publish_event(
            session_id=root_id,
            context_id=sid,
            event_type="command_received",
            data={
                "method": request.method,
                "path": request.url.path,
                "sid": sid,
                "payload": payload,
                "uuid": str(uuid.uuid4()),
            },
            source="rest",
        )
    except Exception:
        logger.exception(
            "command_received ingest failed sid=%s path=%s",
            sid, request.url.path,
        )

    async def receive():
        return {"type": "http.request", "body": body_bytes, "more_body": False}

    request._receive = receive
    return await call_next(request)

coordinator = Coordinator()
from scheduler import Scheduler, broadcast_schedules
from stores import schedule_store
schedule_ticker = Scheduler(coordinator)
# A10 TOCTOU closure: wire the active-run gate at module-load time,
# BEFORE any HTTP route is mounted, so the very first PATCH that
# arrives can't skip the inside-the-lock recheck via the "gate not
# bound yet" path. Keeping this here (not inside on_startup) shrinks
# the gate-binding window to zero.
session_manager.bind_active_run_gate(coordinator.turn_manager.has_active_runs)
# Source of truth for the "Running…" indicator: coordinator walks
# `_run_state[sid]` and checks pid liveness per entry. session_manager
# only keeps the last-broadcast value per sid for WS dedup.
# Bound at module load (same window as the active-run gate) so the
# first request can't race a None check.
session_manager.bind_running_check(coordinator.turn_manager.is_running)
session_manager.bind_monitoring_check(coordinator.turn_manager.monitoring_state)
# Pin predicate for LRU root eviction: never evict a root the
# orchestrator still references (active turn / WS subscriber / live
# tailer). Bound at module load so an early load can't enforce the cap
# with the predicate still None (which fails closed → nothing evicted).
session_manager.bind_pin_predicate(coordinator.is_root_in_use)

# ============================================================================
# Auth — keychain-backed credentials, session-cookie gate.
# ----------------------------------------------------------------------------
# Middleware source-order below determines the wrapping (Starlette docs:
# last-added = outermost = runs first). Runtime order is:
#   CORS  →  SessionMiddleware  →  auth_gate  →  ingest_command_received
#         →  handler / router
# CORS outermost so OPTIONS preflight returns without auth-401ing it.
# SessionMiddleware before auth_gate so the latter can read the session.
# auth_gate before ingest so unauth requests can't write
# `command_received` events to disk.
# ============================================================================

import auth                                                       # noqa: E402
import auth_routes                                                # noqa: E402
from starlette.middleware.sessions import SessionMiddleware       # noqa: E402

app.include_router(auth_routes.router)

import provider_config_sync_api  # noqa: E402
app.include_router(provider_config_sync_api.router)
import capability_api  # noqa: E402
app.include_router(capability_api.router)
import extension_api  # noqa: E402
app.include_router(extension_api.router)
import extension_storage_api  # noqa: E402
app.include_router(extension_storage_api.router)
import testape_api  # noqa: E402
app.include_router(testape_api.router)


def _builtin_extension_enabled(extension_id: str) -> bool:
    return extension_store.is_builtin_feature_enabled(extension_id)


def _builtin_extension_runtime_ready(extension_id: str) -> bool:
    return extension_store.is_extension_runtime_ready(extension_id)


async def _builtin_extension_runtime_ready_fast(extension_id: str, *, timeout_s: float = 0.05) -> bool:
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_builtin_extension_runtime_ready, extension_id),
            timeout=timeout_s,
        )
    except Exception:
        return False


def _require_builtin_extension(extension_id: str) -> None:
    if not _builtin_extension_enabled(extension_id):
        raise HTTPException(status_code=404, detail="Extension is not installed")


def _require_builtin_runtime_extension(extension_id: str) -> None:
    not_ready_msg = extension_store.runtime_not_ready_message(extension_id)
    if not_ready_msg is not None:
        raise HTTPException(status_code=404, detail=not_ready_msg)


# Auth routes reachable without credentials (you authenticate TO reach
# them). /api/auth/me is excluded — see auth_gate below.
_AUTH_PUBLIC_ROUTES = frozenset({
    "/api/auth/login",
    "/api/auth/setup",
    "/api/auth/needs_setup",
    # QR / refresh-token external access. qr_grant self-gates to
    # loopback-or-authed inside the handler (see auth_routes.py).
    "/api/auth/qr_grant",
    "/api/auth/qr_redeem",
    "/api/auth/refresh",
    # OTA bundle download for the Capacitor updater. The native HTTP GET
    # cannot carry our dynamic bearer header, so the handler validates a
    # `token` query param (same pattern as the WS endpoints) and fails
    # closed on an invalid/missing token.
    "/api/mobile/bundle/download",
})
_AUTH_PUBLIC_PREFIXES = (
    "/api/desktop/updates/",
    "/api/download/desktop/",
    # HTML preview files. The route self-gates via an HMAC-signed,
    # expiring, directory-scoped token minted by the authed
    # /api/file/preview-url endpoint (which this prefix does NOT match).
    # The preview iframe is an opaque origin that cannot send the
    # session cookie, so the token is the credential.
    "/api/file/preview/",
)
_AUTH_PUBLIC_ARTIFACT_ROUTES = frozenset({
    "/api/desktop/status",
})

# Extension UI bundles are static JS/CSS assets, served to the client the
# same way the SPA shell is. They are loaded by dynamic `import()` of a
# backend-served URL. On the Capacitor native shell the page origin
# (http://localhost / capacitor://localhost) differs from the API origin,
# so the import is cross-origin — and `import()` can neither carry the
# SameSite=Lax session cookie nor set an Authorization header (the bearer
# interceptor only wraps window.fetch, not the module loader). An auth
# requirement here therefore 401s the module request and surfaces in the
# WebView as "Failed to fetch dynamically imported module". Treat these
# static assets as public, exactly like the frontend shell.
_EXTENSION_FRONTEND_ASSET_RE = re.compile(r"^/api/extensions/[^/]+/frontend/")


def _is_extension_frontend_asset(path: str) -> bool:
    return bool(_EXTENSION_FRONTEND_ASSET_RE.match(path))


@app.middleware("http")
async def auth_gate(request, call_next):
    """Gate every /api/* request except the pre-auth auth routes
    (`_AUTH_PUBLIC_ROUTES`) and /api/internal/* (the latter uses the
    existing X-Internal-Token pattern that worker subprocesses already
    send — see main.py handlers using `Header(..., alias="X-Internal-Token")`).
    Note /api/auth/me IS gated — native clients authenticate to it via
    the bearer fallback, since their session cookie can't cross origins.

    `BETTER_CLAUDE_TEST_AUTH_BYPASS` is intentionally ignored; tests
    authenticate normally or use internal tokens."""
    from fastapi.responses import JSONResponse

    path = request.url.path
    if (
        path.startswith("/api/")
        and path not in _AUTH_PUBLIC_ARTIFACT_ROUTES
        and not _is_extension_frontend_asset(path)
    ):
        try:
            browser_trust.validate_http_request(request)
        except HTTPException as exc:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    # Pre-auth auth routes must stay reachable without credentials (you
    # log in to reach them). /api/auth/me is intentionally NOT here: it
    # requires auth like every other /api/ route, so native Capacitor
    # clients authenticate via the bearer fallback below. Otherwise the
    # SameSite=Lax session cookie can't cross origins from the WebView
    # (http://localhost) to the backend, /me 401s, and the just-logged-in
    # user bounces straight back to <Login /> with no error shown.
    if (
        path in _AUTH_PUBLIC_ROUTES
        or path in _AUTH_PUBLIC_ARTIFACT_ROUTES
        or any(path.startswith(prefix) for prefix in _AUTH_PUBLIC_PREFIXES)
        or _is_extension_frontend_asset(path)
    ):
        return await call_next(request)
    if path.startswith("/api/internal/"):
        token = request.headers.get("X-Internal-Token")
        # Authn: accept the core/runner token OR a registered per-extension
        # token. Identity (which extension) is derived from the token by the
        # per-endpoint gates — never from a self-asserted X-Extension-Id.
        principal = coordinator.resolve_principal(token)
        if principal is None:
            from fastapi.responses import JSONResponse
            return JSONResponse({"detail": "invalid internal token"}, status_code=403)
        if principal[0] == "extension" and path != "/api/internal/capabilities/invoke":
            record = extension_store.get_extension(str(principal[1] or ""))
            if not record or not extension_store.has_permission(record, "internal_loopback"):
                return JSONResponse(
                    {"detail": "internal route requires internal_loopback permission"},
                    status_code=403,
                )
        return await call_next(request)
    if not path.startswith("/api/"):
        # Frontend static files and any non-API path are public — the
        # frontend SPA handles redirecting to <Login /> when /api/auth/me
        # comes back 401.
        return await call_next(request)
    user = request.session.get("user") if "session" in request.scope else None
    if not user:
        # Fall back to Bearer-token auth for cross-origin native clients
        # (Capacitor WebView) where the session cookie can't make it
        # across origins. See auth.verify_token for the contract.
        auth_header = request.headers.get("authorization") or ""
        if auth_header.lower().startswith("bearer "):
            tok_user = auth.verify_token(auth_header.split(" ", 1)[1].strip())
            if tok_user:
                if "session" in request.scope:
                    request.session["user"] = tok_user
                user = tok_user
    if not user:
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": "unauthenticated"}, status_code=401)
    return await call_next(request)


app.add_middleware(
    SessionMiddleware,
    secret_key=auth.get_session_secret(),
    max_age=30 * 86400,  # 30 days; matches the documented cookie lifetime
    same_site="lax",
    https_only=False,    # LAN HTTP for now; flip when fronted by TLS
    session_cookie="better_agent_session",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "capacitor://localhost",
        "http://localhost",
        "http://localhost:3000",
        "http://localhost:8000",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:8000",
        "http://[::1]:3000",
        "http://[::1]:8000",
    ],
    allow_origin_regex=(
        r"^https?://("
        r"localhost|127\.0\.0\.1|\[::1\]|"
        r"10(?:\.\d{1,3}){3}|"
        r"192\.168(?:\.\d{1,3}){2}|"
        r"172\.(?:1[6-9]|2\d|3[0-1])(?:\.\d{1,3}){2}|"
        r"100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])(?:\.\d{1,3}){2}|"
        r"[a-z0-9-]+(?:\.[a-z0-9-]+)*\.ts\.net"
        r"):(?:3000|8000)$"
    ),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# SessionManager change events fan out as global session metadata WS frames.
from session_ws_broadcaster import SessionWSBroadcaster  # noqa: E402
ws_broadcaster = SessionWSBroadcaster(coordinator)
from event_bus_subscribers import bind_session_ws_broadcaster
bind_session_ws_broadcaster(ws_broadcaster)

from event_bus_subscribers import (
    bind_post_turn_hooks,
    bind_pre_turn_hooks,
    bind_worker_fanout_cleanup,
)
bind_worker_fanout_cleanup(coordinator.broadcast_workers_changed)
bind_post_turn_hooks()
bind_pre_turn_hooks()
import task_assessor
task_assessor.bind(coordinator)

# Rebuild the declarative tag-rule registry from every enabled extension
# so styling/markers apply from boot, not only after the periodic
# instruction reconcile.
try:
    import extension_applied_config

    extension_applied_config.reconcile_all()
except Exception:
    logger.exception("startup: extension_applied_config.reconcile_all failed")

# Publish the desired supervisor-daemon set for the platform daemon host and
# start backend-lifecycle extension daemons.
try:
    import extension_daemons

    extension_daemons.reconcile()
except Exception:
    logger.exception("startup: extension_daemons.reconcile failed")

# Native-CLI-jsonl tailing is owned by native_files_manager: it folds
# tail targets (session.agent_sid_set / native_files.fork_target) and
# demand (native_files.demand) off the bus, and reconciles the
# OwnedClaudeJsonlTailers. The orchestrator only publishes demand.
from native_files_manager import native_files
native_files.bind()

# Working-mode owners subscribe to `session.parent_deleted` and route by
# `working_mode`; cascade-delete only publishes the fact.
import working_mode as _working_mode_mod
prompt_engineer.register_bus_subscribers()
_working_mode_mod.register_bus_subscribers()

# Mount the node_link WS endpoint (only meaningful in primary mode —
# topology.yaml is loaded lazily on first hit). Importing
# `provider_remote` here also wires its inbound dispatchers into
# node_link as a side-effect of the module import. Both imports are
# behind a try so a misconfigured topology.yaml doesn't break the
# primary's startup — node_link itself will refuse connections later
# with a clear error.
if (
    extension_store.extension_id_for_role('machine-nodes') is None
    or extension_store.is_extension_runtime_ready(
        extension_store.extension_id_for_role('machine-nodes')
    )
):
    try:
        import node_link
        import node_store
        import provider_remote  # noqa: F401 — wires dispatchers as side effect
        app.include_router(node_link.router)

        async def _on_node_state_changed(node_id: str, new_state: str) -> None:
            """Fan node up/down transitions out to every open WS client so
            the frontend can render node-status badges without polling.

            Carries the backend-owned `last_seen` so the frontend never
            has to invent a timestamp from its own clock (CLAUDE.md state-
            ownership: compute server-side, reflect on the frontend)."""
            conn = node_store.get_connection(node_id)
            payload = {
                "node_id": node_id,
                "state": new_state,
                "last_seen": conn.last_seen if conn else None,
                "app_commit_sha": conn.app_commit_sha if conn else "",
                "app_dirty": conn.app_dirty if conn else False,
                "primary_commit_sha": node_store.app_version.current_commit_sha(),
                "primary_dirty": node_store.app_version.current_dirty(),
                "version_status": (
                    node_store.connection_version_status(conn)
                    if conn else "unknown"
                ),
            }
            try:
                await coordinator.broadcast_global("node_state_changed", payload)
            except Exception:
                logger.exception("node_state_changed broadcast failed")

        node_store.add_listener(_on_node_state_changed)

        async def _on_node_connected_recover(node_id: str, new_state: str) -> None:
            """When a node (re)connects, reconcile every pending remote run
            dir it owns — finalize completed/dead runs, rehook alive ones.
            Background task: recovery RPC round-trips must not block the
            node handshake path that fires this listener."""
            if new_state != "connected":
                return
            import run_recovery
            asyncio.get_running_loop().create_task(
                run_recovery.integrate_remote_runs_for_node(node_id),
                name=f"remote-recovery-{node_id}",
            )

        node_store.add_listener(_on_node_connected_recover)

        import node_extension_sync
        # A (re)connecting worker gets the current extension state pushed so
        # it never runs a stale projection after downtime.
        node_store.add_listener(node_extension_sync.on_node_state)
        import run_recovery as _run_recovery_mod
        _run_recovery_mod.set_remote_recovery_coordinator(coordinator)

        async def _on_node_registration(event_type: str, payload: dict) -> None:
            """Fan node registration-lifecycle events
            (`node_registration_requested` / `node_registration_resolved`) out
            to every open browser so the approval popup appears/dismisses
            without polling. node_link calls this via set_registration_listener
            to avoid importing the coordinator (circular import)."""
            try:
                await coordinator.broadcast_global(event_type, payload)
            except Exception:
                logger.exception("node registration broadcast failed (%s)", event_type)

        node_link.set_registration_listener(_on_node_registration)
        logger.info("multi-machine: node_link WS endpoint mounted")
    except Exception:
        logger.exception("multi-machine: node_link mount failed at startup")


# ============================================================================
# REST Endpoints
# ============================================================================

class ProviderPayload(BaseModel):
    name: str = ""
    kind: str = "claude"  # "claude" | "gemini" — selects the Provider impl
    mode: str = "subscription"  # "subscription" | "api_key"
    api_key: str = ""
    base_url: str = ""
    config_dir: str = ""
    custom_models: list[str] = []
    default_model: str = ""
    runner: str = ""
    default_reasoning_effort: str = ""
    default_permission: dict = {}
    capabilities: dict[str, bool] | None = None
    suspended: bool = False


class ProviderPatch(BaseModel):
    """All fields optional — only the supplied ones are written."""
    name: str | None = None
    kind: str | None = None
    mode: str | None = None
    api_key: str | None = None  # "__keep__" preserves the existing key
    base_url: str | None = None
    config_dir: str | None = None
    custom_models: list[str] | None = None
    default_model: str | None = None
    runner: str | None = None
    default_reasoning_effort: str | None = None
    default_permission: dict | None = None
    capabilities: dict[str, bool] | None = None
    suspended: bool | None = None


class ProviderSetupInstallPayload(BaseModel):
    kind: str


async def _broadcast_provider_changed():
    state = await asyncio.to_thread(config_store.list_providers)
    await coordinator.broadcast_global(
        "provider_changed",
        state,
    )


def _provider_not_suspended(provider_id: str | None, *, action: str = "use provider") -> None:
    if provider_id and config_store.provider_suspended(provider_id):
        raise HTTPException(
            status_code=409,
            detail=t("error.provider_suspended", action=action),
        )


async def _broadcast_install(event_type: str, data: dict) -> None:
    """Fan-out for streaming provider-CLI installs. provider_setup calls
    this per stdout/stderr line and on completion."""
    await coordinator.broadcast_global(event_type, data)


async def _record_last_model(provider_id: str | None, model: str | None) -> None:
    """Remember the model the user chose for a provider so pickers can
    pre-choose it on the next provider switch. Broadcasts only on an
    actual change so the prefs write doesn't spam refetches."""
    if not provider_id or not model:
        return
    changed = await asyncio.to_thread(user_prefs.set_last_model, provider_id, model)
    if changed:
        await _broadcast_provider_changed()


async def _record_last_reasoning_effort(
    provider_id: str | None, reasoning_effort: str | None,
) -> None:
    if not provider_id or not reasoning_effort:
        return
    changed = await asyncio.to_thread(
        user_prefs.set_last_reasoning_effort,
        provider_id,
        reasoning_effort,
    )
    if changed:
        await _broadcast_provider_changed()


async def _model_for_provider_switch(provider_id: str, provider_record: dict) -> str:
    """Pick a valid model when the user switches provider without an
    explicit model. Prefer the user's remembered model for that provider,
    then the provider default, then the first cached active model. Never
    leave the old provider's model attached to the session."""
    import models as models_mod

    last_models = await asyncio.to_thread(user_prefs.get_last_models)
    candidates: list[str] = []
    for value in (
        last_models.get(provider_id),
        provider_record.get("default_model"),
    ):
        model = str(value or "").strip()
        if model and model not in candidates:
            candidates.append(model)
    try:
        available = await asyncio.to_thread(models_mod.available_models, provider_id)
    except Exception:
        available = []
    for value in available:
        model = str(value or "").strip()
        if model and model not in candidates:
            candidates.append(model)

    for model in candidates:
        try:
            await asyncio.to_thread(_validate_provider_model, provider_id, model, True)
            return model
        except HTTPException:
            continue

    name = provider_record.get("name") or provider_id
    raise HTTPException(
        status_code=400,
        detail=f"{name} has no known models; cannot switch provider without a model",
    )


def _api_reasoning_effort(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return ""
    effort = normalize_reasoning_effort(value)
    if effort is None:
        raise HTTPException(
            status_code=400,
            detail="reasoning_effort must be one of: none, minimal, low, medium, high, xhigh",
        )
    return effort


def _api_optional_provision_prompt(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(status_code=400, detail="provision_prompt must be a non-empty string")
    return value


_REQUIREMENTS_PROCESSOR_PROFILE = "requirements_processor"


def _api_optional_provisioned_tool_profile(value: object, body: dict | None = None) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise HTTPException(status_code=400, detail="provisioned_tool_profile must be a string")
    profile = value.strip()
    if not profile:
        return profile
    if profile == _REQUIREMENTS_PROCESSOR_PROFILE:
        if _is_authorized_provisioned_tool_profile(body, profile):
            return profile
        raise HTTPException(
            status_code=400,
            detail="requirements_processor profile is reserved for get-requirements processor dispatch",
        )
    raise HTTPException(status_code=400, detail="unsupported provisioned_tool_profile")


def _is_authorized_provisioned_tool_profile(body: dict | None, profile: str) -> bool:
    if not isinstance(body, dict):
        return False
    client_delegation_id = body.get("client_delegation_id")
    if not isinstance(client_delegation_id, str):
        return False
    from provisioning.dispatch import is_authorized_tool_profile_dispatch
    return is_authorized_tool_profile_dispatch(client_delegation_id, profile)


def _provider_reasoning_effort(
    provider_id: str | None, effort: str | None,
) -> str | None:
    if effort is None:
        return None
    if not effort:
        return ""
    record = config_store.get_provider(provider_id) if provider_id else None
    if record is None:
        active = config_store.get_default_provider()
        record = config_store.get_provider(active["id"]) if active else None
    options = (record or {}).get("reasoning_effort_options") or []
    if effort not in options:
        name = (record or {}).get("name") or provider_id or "active provider"
        raise HTTPException(
            status_code=400,
            detail=f"{name} does not support reasoning_effort={effort!r}",
        )
    return effort


def _api_permission(value: object) -> dict | None:
    """Coerce an incoming permission body value. None = absent (no change /
    inherit at creation). Empty object/string = explicit "inherit default"
    (clear any per-session override). Otherwise must be a dict."""
    if value is None:
        return None
    if (isinstance(value, str) and not value.strip()) or value == {}:
        return {}
    if not isinstance(value, dict):
        raise HTTPException(status_code=400, detail="permission must be an object")
    return value


def _provider_permission(
    provider_id: str | None, value: dict | None,
) -> dict | None:
    """Strictly validate a permission dict against the provider's native
    options. Returns normalized dict, {} (inherit default), or None (absent)."""
    if value is None:
        return None
    if not value:
        return {}
    record = config_store.get_provider(provider_id) if provider_id else None
    if record is None:
        active = config_store.get_default_provider()
        record = config_store.get_provider(active["id"]) if active else None
    name = (record or {}).get("name") or provider_id or "active provider"
    options = (record or {}).get("permission_options") or {}
    if not options:
        raise HTTPException(
            status_code=400, detail=f"{name} has no permission options"
        )
    norm: dict[str, str] = {}
    for axis, allowed in options.items():
        raw = value.get(axis)
        if not isinstance(raw, str) or raw not in allowed:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{name} permission {axis}={raw!r} is not one of: "
                    f"{', '.join(allowed)}"
                ),
            )
        norm[axis] = raw
    return norm


def _provider_for_required_model(provider_id: str | None) -> dict:
    provider = config_store.get_provider(provider_id) if provider_id else config_store.get_default_provider()
    if provider_id and not provider:
        if config_store.provider_suspended(provider_id):
            raise HTTPException(
                status_code=409,
                detail=t("error.provider_suspended", action="create sessions"),
            )
        raise HTTPException(status_code=404, detail="provider not found")
    if not provider:
        raise HTTPException(status_code=400, detail="no active provider configured")
    _provider_not_suspended(provider.get("id"), action="create sessions")
    return provider


def _required_model_from_body_or_provider(body: dict, provider: dict) -> str:
    import models as models_mod

    model = str(body.get("model") or "").strip()
    if model:
        _validate_provider_model(str(provider.get("id") or "").strip() or None, model)
        return model
    provider_id = str(provider.get("id") or "").strip() or None
    available = models_mod.available_models(provider_id)
    default_model = str(provider.get("default_model") or "").strip()
    name = provider.get("name") or provider.get("id") or "provider"
    if not default_model:
        raise HTTPException(status_code=400, detail=f"{name} has no default model configured")
    if default_model and default_model in available:
        return default_model
    for candidate in available:
        candidate = str(candidate or "").strip()
        if candidate:
            return candidate
    raise HTTPException(status_code=400, detail=f"{name} has no default model configured")


def _validate_provider_model(
    provider_id: str | None, model: str, include_retired: bool = False,
) -> None:
    if not model:
        return
    import models as models_mod
    available = set(
        models_mod.available_models_including_retired(provider_id)
        if include_retired
        else models_mod.available_models(provider_id)
    )
    if model in available:
        return
    provider = (
        config_store.get_provider(provider_id)
        if provider_id
        else config_store.get_default_provider()
    ) or {}
    name = provider.get("name") or provider_id
    if not available:
        raise HTTPException(
            status_code=400,
            detail=f"{name} has no known models; explicit model={model!r} is not allowed",
        )
    raise HTTPException(
        status_code=400,
        detail=f"{name} does not support model={model!r}",
    )


async def _resolve_provider_id_ref(provider_ref: str) -> str:
    ref = str(provider_ref or "").strip()
    if not ref:
        return ""
    try:
        provider = await asyncio.to_thread(config_store.resolve_provider_ref, ref)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not provider:
        raise HTTPException(status_code=400, detail="provider_id does not exist")
    return str(provider.get("id") or "").strip()


async def _resolve_auto_search_provider_id(
    body: dict,
    caller_session_id: str,
) -> str:
    requested = str((body or {}).get("provider_id") or "").strip()
    if requested.upper() == "ANY":
        return ""
    if requested:
        return await _resolve_provider_id_ref(requested)
    caller = await _session_lite(caller_session_id)
    return str((caller or {}).get("provider_id") or "").strip()


async def _validate_optional_run_selector(
    sender_session_id: str,
    provider_id: str,
    model: str,
) -> None:
    if not provider_id and not model:
        return
    sender = await _session_lite(sender_session_id)
    resolved_provider_id = (
        provider_id
        or str((sender or {}).get("provider_id") or "").strip()
        or None
    )
    resolved_model = model
    if not resolved_model and provider_id:
        provider = await asyncio.to_thread(config_store.get_provider, provider_id) or {}
        resolved_model = str(provider.get("default_model") or "").strip()
        if not resolved_model:
            name = provider.get("name") or provider_id
            raise HTTPException(
                status_code=400,
                detail=f"{name} has no default model configured",
            )
    if not resolved_model:
        resolved_model = str((sender or {}).get("model") or "").strip()
    await asyncio.to_thread(_validate_provider_model, resolved_provider_id, resolved_model)


def _validate_provider_default_reasoning_effort(
    provider_record: dict, effort: str | None,
) -> str:
    parsed = _api_reasoning_effort(effort)
    if not parsed:
        return ""
    options = config_store.reasoning_effort_options_for_provider(provider_record)
    if parsed not in options:
        name = provider_record.get("name") or provider_record.get("kind") or "provider"
        raise HTTPException(
            status_code=400,
            detail=f"{name} does not support reasoning_effort={parsed!r}",
        )
    return parsed


async def _broadcast_models_catalog_changed(provider_id: str, diff: dict) -> None:
    """Per-provider catalog delta. Four disjoint transition sets:
    newly_added / became_active / went_retired / truly_removed.
    Frontend `useModelsCatalogChanged` refetches `/api/models` on receipt."""
    await coordinator.broadcast_global(
        "models_catalog_changed",
        {
            "provider_id": provider_id,
            "newly_added": diff.get("newly_added", []),
            "became_active": diff.get("became_active", []),
            "went_retired": diff.get("went_retired", []),
            "truly_removed": diff.get("truly_removed", []),
        },
    )


@app.get("/api/startup_tasks")
async def get_startup_tasks():
    """Snapshot of in-flight + recent-history backend startup tasks.
    Frontend banner reads this on mount for first paint, then
    subscribes to `startup_task_changed` WS events for live deltas.
    Authoritative state lives in `startup_task_registry` (in-memory)."""
    from startup_tasks import startup_task_registry
    return startup_task_registry.list()


def _require_machine_nodes_internal(x_internal_token: str) -> None:
    if not coordinator.is_internal_caller(x_internal_token):
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    _require_builtin_runtime_extension(extension_store.extension_id_for_role('machine-nodes'))


@app.post("/api/internal/machine-nodes/list")
async def internal_get_nodes(
    body: dict | None = None,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    _require_machine_nodes_internal(x_internal_token)
    """Snapshot of the multi-machine topology + live connection state.
    Returns an empty list (no topology configured) for single-machine
    deployments rather than raising. Frontend uses this to render
    node-status badges and the per-worker node selector."""
    try:
        import node_store
        return await asyncio.to_thread(node_store.snapshot)
    except Exception:
        logger.exception("get_nodes failed")
        return []


@app.get("/api/providers")
async def get_providers():
    state, last_models, last_efforts = await asyncio.gather(
        asyncio.to_thread(config_store.list_providers),
        asyncio.to_thread(user_prefs.get_last_models),
        asyncio.to_thread(user_prefs.get_last_reasoning_efforts),
    )
    for record in state.get("providers", []):
        last = last_models.get(record.get("id"))
        if last:
            record["last_model"] = last
        last_effort = last_efforts.get(record.get("id"))
        if last_effort and last_effort in (record.get("reasoning_effort_options") or []):
            record["last_reasoning_effort"] = last_effort
    return state


@app.post("/api/providers")
async def create_provider(payload: ProviderPayload):
    body = payload.model_dump()
    body["default_reasoning_effort"] = _validate_provider_default_reasoning_effort(
        body, body.get("default_reasoning_effort"),
    )
    try:
        record = await asyncio.to_thread(config_store.add_provider, body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await _broadcast_provider_changed()
    return record


@app.patch("/api/providers/{provider_id}")
async def patch_provider(provider_id: str, payload: ProviderPatch):
    body = {k: v for k, v in payload.model_dump().items() if v is not None}
    if "default_reasoning_effort" in body:
        current = await asyncio.to_thread(config_store.get_provider, provider_id)
        if current is None:
            raise HTTPException(status_code=404, detail=t("error.provider_not_found"))
        candidate = dict(current)
        candidate.update(body)
        body["default_reasoning_effort"] = _validate_provider_default_reasoning_effort(
            candidate, body.get("default_reasoning_effort"),
        )
    try:
        record = await asyncio.to_thread(config_store.update_provider, provider_id, body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if record is None:
        raise HTTPException(status_code=404, detail=t("error.provider_not_found"))
    if body.get("suspended") is True:
        try:
            from provider import cancel_provider_runs
            cancelled = await asyncio.to_thread(cancel_provider_runs, provider_id)
            if cancelled:
                logger.info("provider %s suspended; cancelled %d run(s)", provider_id, cancelled)
        except Exception:
            logger.exception("failed to cancel runs for suspended provider %s", provider_id)
    await _broadcast_provider_changed()
    return record


@app.post("/api/providers/{provider_id}/suspended")
async def set_provider_suspended(provider_id: str, body: dict = Body(default={})):
    suspended = bool((body or {}).get("suspended", True))
    state = await asyncio.to_thread(config_store.set_provider_suspended, provider_id, suspended)
    if state is None:
        raise HTTPException(status_code=404, detail=t("error.provider_not_found"))
    cancelled = 0
    if suspended:
        try:
            from provider import cancel_provider_runs
            cancelled = await asyncio.to_thread(cancel_provider_runs, provider_id)
        except Exception:
            logger.exception("failed to cancel runs for suspended provider %s", provider_id)
    await _broadcast_provider_changed()
    return {"suspended": suspended, "cancelled_runs": cancelled, **state}


@app.delete("/api/providers/{provider_id}")
async def remove_provider(provider_id: str):
    deleted, reason = await asyncio.to_thread(config_store.delete_provider, provider_id)
    if not deleted:
        if reason == "missing":
            raise HTTPException(status_code=404, detail=t("error.provider_not_found"))
        if reason == "default":
            raise HTTPException(
                status_code=409,
                detail=t("error.cannot_delete_default_provider"),
            )
        raise HTTPException(status_code=400, detail=reason)
    await _broadcast_provider_changed()
    return {"deleted": True}


@app.post("/api/providers/{provider_id}/set-default")
async def set_default_provider(provider_id: str):
    try:
        state = await asyncio.to_thread(config_store.set_default_provider, provider_id)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=409,
            detail=t("error.provider_suspended", action="set as default"),
        ) from exc
    if state is None:
        raise HTTPException(status_code=404, detail=t("error.provider_not_found"))
    await _broadcast_provider_changed()
    return state


@app.post("/api/providers/default/custom_models")
async def add_custom_model(body: dict):
    """Append a custom model to the currently-active provider. Used by the
    ModelSelector's '+ custom' button so the frontend doesn't need to know
    which provider is active locally."""
    name = (body or {}).get("name", "").strip()
    if not name:
        raise HTTPException(status_code=400, detail=t("error.name_required"))
    record = await asyncio.to_thread(config_store.add_custom_model_to_default, name)
    if record is None:
        raise HTTPException(status_code=400, detail=t("error.no_default_provider"))
    await _broadcast_provider_changed()
    return record


@app.post("/api/native-import")
async def start_native_import(body: dict):
    """Start a background job that ingests every native CLI session of
    the given providers (all configured providers when `provider_ids` is
    omitted) into Better Agent sessions. Single-flight. Returns current
    job status. `limit` caps the number of NEW sessions imported."""
    provider_ids = body.get("provider_ids") if isinstance(body, dict) else None
    if provider_ids is not None and not isinstance(provider_ids, list):
        raise HTTPException(status_code=400, detail="provider_ids must be a list of ids or omitted")
    import native_import
    return await asyncio.to_thread(
        native_import.start_import,
        provider_ids,
        _parse_native_import_limit(body),
        _parse_native_import_project_paths(body),
    )


@app.get("/api/native-import/status")
async def native_import_status():
    import native_import
    return native_import.get_status()


@app.get("/api/native-import/summary")
async def native_import_summary(
    provider_ids: Optional[str] = None,
    all_projects: bool = False,
):
    """Counts-only preview of importable native sessions, grouped by
    provider. Read-only; does not start a job. Returns grouped counts
    instead of one row per session — a full Claude+Codex history is tens
    of thousands of sessions and the row dump reached hundreds of MB."""
    import native_import
    ids = provider_ids.split(",") if provider_ids else None
    project_paths = None if all_projects else native_import.loaded_project_paths()
    return await native_import.count_native_sessions_async(ids, project_paths)


def _parse_native_import_limit(body: dict):
    raw = body.get("limit") if isinstance(body, dict) else None
    if raw is None:
        return None
    try:
        limit = int(raw)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="limit must be an integer")
    if limit < 0:
        raise HTTPException(status_code=400, detail="limit must be >= 0")
    return limit or None


def _parse_native_import_project_paths(body: dict) -> Optional[list[str]]:
    import native_import
    if isinstance(body, dict) and body.get("all_projects") is True:
        return None
    raw = body.get("project_paths") if isinstance(body, dict) else None
    if raw is None:
        return native_import.loaded_project_paths()
    if not isinstance(raw, list):
        raise HTTPException(status_code=400, detail="project_paths must be a list of paths or omitted")
    return [str(p) for p in raw if isinstance(p, str) and p]


@app.post("/api/internal/native-import")
async def internal_start_native_import(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    """Internal-token-gated trigger for the native import. Runs the
    import INSIDE the backend process, which is the only safe way when
    the backend is live — a separate process writing session.json races
    the backend's in-memory cache (it re-persists and clobbers the
    render tree). Used by the CLI/import scripts."""
    if not coordinator.is_internal_caller(x_internal_token):
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    import native_import
    provider_ids = body.get("provider_ids") if isinstance(body, dict) else None
    if provider_ids is not None and not isinstance(provider_ids, list):
        raise HTTPException(status_code=400, detail="provider_ids must be a list of ids or omitted")
    return await asyncio.to_thread(
        native_import.start_import,
        provider_ids,
        _parse_native_import_limit(body),
        _parse_native_import_project_paths(body),
    )


@app.get("/api/internal/native-import/status")
async def internal_native_import_status(
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    if not coordinator.is_internal_caller(x_internal_token):
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    import native_import
    return await asyncio.to_thread(native_import.get_status)


@app.get("/api/models")
async def get_models():
    """Disk-only read. NEVER makes a provider HTTP call. Catalog
    population is owned by `_models_catalog_refresher` — see
    `backend/models.py` for the cache schema. Frontend reads
    `last_fetch_state` to render warming/failing UI."""
    import models as models_mod
    return models_mod.models_catalog()


@app.post("/api/models/refresh")
async def refresh_active_models_endpoint():
    """Manual refresh for the active provider. Bounded ~10s by
    `_fetch_api_models` timeout; two back-to-back clicks serialize
    behind the per-provider lock — also bounded ~10s. Not worth a
    202 + WS-when-done complication for that window."""
    import models as models_mod
    active = await asyncio.to_thread(config_store.get_default_provider)
    if active is None:
        raise HTTPException(status_code=400, detail=t("error.no_default_provider"))
    diff = await models_mod.refresh_one(active["id"])
    if diff:
        await _broadcast_models_catalog_changed(active["id"], diff)
    return models_mod.models_catalog(active["id"])


@app.post("/api/providers/{provider_id}/models/refresh")
async def refresh_provider_models_endpoint(provider_id: str):
    """Per-provider manual refresh — parity with `GET /api/providers/{id}/models`.
    Same lock, same bounded await as the active endpoint."""
    import models as models_mod
    record = await asyncio.to_thread(config_store.get_provider, provider_id)
    if record is None:
        raise HTTPException(status_code=404, detail=t("error.provider_not_found"))
    diff = await models_mod.refresh_one(provider_id)
    if diff:
        await _broadcast_models_catalog_changed(provider_id, diff)
    return models_mod.models_catalog(provider_id)


@app.get("/api/providers/{provider_id}/models")
async def get_provider_models(provider_id: str):
    """Models for a specific provider — used by the ProviderForm dropdown
    so the user can pick a default_model without activating the provider
    first. Disk-only read (no provider HTTP call); the daily refresher
    owns network I/O."""
    import models as models_mod
    record = await asyncio.to_thread(config_store.get_provider, provider_id)
    if record is None:
        raise HTTPException(status_code=404, detail=t("error.provider_not_found"))
    return models_mod.models_catalog(provider_id)


@app.get("/api/provider-setup/status")
async def get_provider_setup_status():
    results = await asyncio.gather(
        *[
            provider_setup.provider_setup_status(kind)
            for kind in provider_setup.supported_provider_kinds()
        ]
    )
    return {"providers": results}


@app.get("/api/provider-setup/installs")
async def get_provider_setup_installs():
    """Snapshot of in-memory install runs for first paint; live deltas
    arrive via `provider_install_progress` / `provider_install_finished`
    WS events."""
    return {"runs": provider_setup.get_install_runs()}


@app.post("/api/provider-setup/install")
async def install_provider_setup(payload: ProviderSetupInstallPayload):
    try:
        return await provider_setup.start_install(payload.kind, _broadcast_install)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---- User preferences ----

@app.get("/api/user-prefs")
async def get_user_prefs(request: Request):
    login_username = (request.session.get("user") or {}).get("username")
    return await asyncio.to_thread(user_prefs.get_all, login_username)


@app.patch("/api/user-prefs")
async def patch_user_prefs(request: Request, body: dict = Body(...)):
    login_username = (request.session.get("user") or {}).get("username")

    def _patch_user_prefs_sync() -> dict:
        if "user_display_name" in body:
            user_prefs.set_user_display_name(body["user_display_name"])
        if "send_mode" in body:
            user_prefs.set_send_mode(body["send_mode"])
        if "language" in body:
            user_prefs.set_language(body["language"])
        if "shortcut_responses" in body:
            user_prefs.set_shortcut_responses(body["shortcut_responses"])
        if "cross_session_delegate_auto" in body:
            val = body["cross_session_delegate_auto"]
            if not isinstance(val, bool):
                raise ValueError("cross_session_delegate_auto must be a boolean")
            user_prefs.set_cross_session_delegate_auto(val)
        if "context_strategy" in body:
            user_prefs.set_context_strategy(body["context_strategy"])
        if "session_auto_delete_days" in body:
            val = body["session_auto_delete_days"]
            if val is not None and (
                isinstance(val, bool) or not isinstance(val, int) or val < 1
            ):
                raise ValueError("session_auto_delete_days must be null or a positive integer")
            user_prefs.set_session_auto_delete_days(val)
        if "font_family" in body:
            val = body["font_family"]
            if val not in ("system", "serif", "mono", "inter"):
                raise ValueError("font_family must be system, serif, mono, or inter")
            user_prefs.set_font_family(val)
        if "font_size" in body:
            val = body["font_size"]
            if (
                isinstance(val, bool)
                or not isinstance(val, int)
                or val < user_prefs.MIN_FONT_SIZE
                or val > user_prefs.MAX_FONT_SIZE
            ):
                raise ValueError(
                    f"font_size must be an integer between "
                    f"{user_prefs.MIN_FONT_SIZE} and {user_prefs.MAX_FONT_SIZE}"
                )
            user_prefs.set_font_size(val)
        if "first_run_wizard_done" in body:
            val = body["first_run_wizard_done"]
            if not isinstance(val, bool):
                raise ValueError("first_run_wizard_done must be a boolean")
            user_prefs.set_first_run_wizard_done(val)
        if "network_bind_address" in body:
            val = body["network_bind_address"]
            if val not in ("127.0.0.1", "0.0.0.0"):
                raise ValueError("network_bind_address must be 127.0.0.1 or 0.0.0.0")
            user_prefs.set_network_bind_address(val)
        if "folder_view_enabled" in body:
            val = body["folder_view_enabled"]
            if not isinstance(val, bool):
                raise ValueError("folder_view_enabled must be a boolean")
            user_prefs.set_folder_view_enabled(val)
        if "session_sort" in body:
            val = body["session_sort"]
            if not isinstance(val, str):
                raise ValueError("session_sort must be a string")
            user_prefs.set_session_sort(val)
        if "session_status_sort" in body:
            val = body["session_status_sort"]
            if not isinstance(val, bool):
                raise ValueError("session_status_sort must be a boolean")
            user_prefs.set_session_status_sort(val)
        if "sessions_tabs_sort" in body:
            val = body["sessions_tabs_sort"]
            if not isinstance(val, str):
                raise ValueError("sessions_tabs_sort must be a string")
            user_prefs.set_session_tabs_sort(val)
        if "sessions_tabs_visible" in body:
            val = body["sessions_tabs_visible"]
            if not isinstance(val, bool):
                raise ValueError("sessions_tabs_visible must be a boolean")
            user_prefs.set_session_tabs_visible(val)
        if "voice_close_on_background" in body:
            val = body["voice_close_on_background"]
            if not isinstance(val, bool):
                raise ValueError("voice_close_on_background must be a boolean")
            user_prefs.set_voice_close_on_background(val)
        if "auto_restart_on_idle" in body:
            val = body["auto_restart_on_idle"]
            if not isinstance(val, bool):
                raise ValueError("auto_restart_on_idle must be a boolean")
            user_prefs.set_auto_restart_on_idle(val)
        return user_prefs.get_all(login_username)

    try:
        prefs = await asyncio.to_thread(_patch_user_prefs_sync)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _invalidate_session_list_user_prefs_cache()
    await coordinator.broadcast_global("user_prefs_changed", prefs)
    return prefs


# ---- UI selection (per-machine navigation restore) ----

@app.get("/api/ui-selection")
async def get_ui_selection():
    return await _run_hot_path("ui_selection.get_all", ui_selection.get_all)


@app.patch("/api/ui-selection")
async def patch_ui_selection(body: dict = Body(...)):
    def _patch_sync() -> dict:
        if "selected_project" in body:
            sel = body["selected_project"]
            if sel is None:
                ui_selection.set_selected_project("")
            elif isinstance(sel, dict):
                path = sel.get("path")
                if not isinstance(path, str):
                    raise ValueError("selected_project.path must be a string")
                node_id = sel.get("node_id", ui_selection.DEFAULT_NODE_ID)
                if not isinstance(node_id, str):
                    raise ValueError("selected_project.node_id must be a string")
                ui_selection.set_selected_project(path, node_id)
            else:
                raise ValueError("selected_project must be an object or null")
        if "remembered_session" in body:
            rem = body["remembered_session"]
            if not isinstance(rem, dict):
                raise ValueError("remembered_session must be an object")
            path = rem.get("path")
            session_id = rem.get("session_id")
            node_id = rem.get("node_id", ui_selection.DEFAULT_NODE_ID)
            if not isinstance(path, str) or not path:
                raise ValueError("remembered_session.path must be a non-empty string")
            if not isinstance(session_id, str) or not session_id:
                raise ValueError("remembered_session.session_id must be a non-empty string")
            if not isinstance(node_id, str):
                raise ValueError("remembered_session.node_id must be a string")
            ui_selection.set_remembered_session(path, node_id, session_id)
        if "open_session_tab_ids" in body:
            open_ids = body["open_session_tab_ids"]
            if not isinstance(open_ids, list):
                raise ValueError("open_session_tab_ids must be a list")
            if any(not isinstance(sid, str) or not sid for sid in open_ids):
                raise ValueError("open_session_tab_ids entries must be non-empty strings")
            ui_selection.set_open_session_tab_ids(open_ids)
        if "open_session_tab_joined_at" in body:
            joined_at = body["open_session_tab_joined_at"]
            if not isinstance(joined_at, dict):
                raise ValueError("open_session_tab_joined_at must be an object")
            if any(
                not isinstance(sid, str)
                or not sid
                or not isinstance(value, str)
                or not value
                for sid, value in joined_at.items()
            ):
                raise ValueError("open_session_tab_joined_at entries must be non-empty strings")
            ui_selection.set_open_session_tab_joined_at(joined_at)
        return ui_selection.get_all()

    try:
        snapshot = await _run_hot_path("ui_selection.patch", _patch_sync)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await coordinator.broadcast_global("ui_selection_changed", snapshot)
    return snapshot


# ---- Shortcut responses ----


@app.post("/api/shortcuts/pick")
async def pick_shortcuts(body: dict = Body(...)):
    assistant_text = body.get("assistant_text", "")
    shortcuts = await shortcut_picker.pick_shortcuts(assistant_text)
    return {"shortcuts": shortcuts}


# ---- File browsing ----
#
# INVARIANT: every endpoint takes an optional `node_id` (default
# "primary" — the sentinel for the local node). They funnel through
# `_file_op`, which dispatches locally OR via `node_link.rpc_call` for
# remote nodes. Single-machine deploys never see a topology lookup on
# this path.

@app.get("/api/files")
async def get_files(
    path: str = Query(...),
    node_id: str = Query("primary"),
    max_depth: int = Query(1, ge=0, le=5),
):
    return await _file_op(node_id, "get_file_tree", {"root": path, "max_depth": max_depth})


@app.get("/api/browse")
async def browse_dir(
    path: str = Query(""),
    node_id: str = Query("primary"),
):
    return await _file_op(node_id, "list_directories", {"path": path})


@app.get("/api/files/search")
async def search_files(
    root: str = Query(...),
    q: str = Query(""),
    kind: str = Query("file"),
    methods: str = Query("path"),
    node_id: str = Query("primary"),
):
    if kind not in ("file", "dir"):
        kind = "file"
    sel = [m for m in (s.strip() for s in methods.split(",")) if m in ("path", "name", "symbols")]
    return await _file_op(
        node_id,
        "search_tree",
        {"root": root, "query": q, "kind": kind, "methods": sel},
    )


@app.post("/api/files/create")
async def create_file_entry(body: dict = Body(...)):
    path = body.get("path")
    kind = body.get("kind")
    node_id = body.get("node_id") or "primary"
    if not isinstance(path, str) or not path.strip():
        raise HTTPException(status_code=400, detail="path must be a non-empty string")
    if not isinstance(node_id, str) or not node_id.strip():
        raise HTTPException(status_code=400, detail="node_id must be a non-empty string")
    if kind not in ("file", "directory"):
        raise HTTPException(status_code=400, detail="kind must be file or directory")
    method = "create_file" if kind == "file" else "create_directory"
    return await _file_op(node_id, method, {"path": path})


def _require_session(session_id: str) -> dict:
    """Fetch session by id or raise 404 with the standard
    `session_not_found_retry` detail. Replaces the 2-line guard
    duplicated across every route that mutates a session by id.

    Returns a `get_lite()` snapshot — caller MUST NOT read
    `msg.events` / `msg.workers[*].events`
    from the returned dict (they will be empty lists). Callers that
    need events should call `session_manager.get(sid)` explicitly."""
    session = session_manager.get_lite(session_id)
    if session is None:
        raise HTTPException(
            status_code=404, detail=t("error.session_not_found_retry"),
        )
    return session


async def _session_exists(session_id: str) -> bool:
    return await asyncio.to_thread(session_manager.exists, session_id)


async def _session_lite(session_id: str) -> dict | None:
    return await asyncio.to_thread(session_manager.get_lite, session_id)


async def _require_session_async(session_id: str) -> dict:
    session = await _session_lite(session_id)
    if session is None:
        raise HTTPException(
            status_code=404, detail=t("error.session_not_found_retry"),
        )
    return session


async def _broadcast_projects_changed() -> None:
    """Single source for the projects_changed fan-out frame. Any
    mutation to the projects list (CRUD or auto-add-from-session) ends
    with this broadcast so open clients refresh the sidebar picker.
    Also rebuilds project mappings since project data changed."""
    _invalidate_project_aggregates()
    await coordinator.broadcast_global("projects_changed", {})
    # Rebuild mappings in background — non-blocking.
    projects = await asyncio.to_thread(project_store.list_projects)
    await asyncio.to_thread(project_mapping_store.rebuild_and_save, projects)
    await coordinator.broadcast_global("project_mappings_changed", {})


async def _broadcast_session_organization_changed(session_ids: list[str] | None = None) -> None:
    if session_ids:
        await asyncio.to_thread(session_store.refresh_organization_projection, session_ids)
        await coordinator.broadcast_global("session_organization_changed", {})
        return

    global _session_organization_refresh_pending, _session_organization_refresh_task
    _session_organization_refresh_pending = True
    if _session_organization_refresh_task is not None and not _session_organization_refresh_task.done():
        return

    async def _refresh_loop() -> None:
        global _session_organization_refresh_pending, _session_organization_refresh_task
        try:
            while _session_organization_refresh_pending:
                _session_organization_refresh_pending = False
                await asyncio.to_thread(session_store.refresh_organization_projection)
                await coordinator.broadcast_global("session_organization_changed", {})
        except Exception:
            logger.warning("session organization projection refresh failed", exc_info=True)
        finally:
            _session_organization_refresh_task = None
            if _session_organization_refresh_pending:
                _session_organization_refresh_task = asyncio.create_task(_refresh_loop())

    _session_organization_refresh_task = asyncio.create_task(_refresh_loop())


async def _apply_initial_session_folder(session_id: str | None, folder_id: str | None) -> None:
    """Assign a folder chosen at creation time. Best-effort: a deleted
    folder or stale id (e.g. an offline-queued create replayed after the
    folder was removed) must never fail the session creation itself."""
    if not session_id or not folder_id:
        return
    try:
        await asyncio.to_thread(
            session_organization_store.set_session_folder,
            session_id,
            folder_id,
        )
        await _broadcast_session_organization_changed([session_id])
    except ValueError as e:
        logger.warning("initial folder assignment failed for %s: %s", session_id[:8], e)


async def _forward_requirement_tags_refreshed(event: BusEvent) -> None:
    await _broadcast_session_organization_changed()


def _local_session_summaries_for_sidebar() -> list[dict]:
    # Hide ephemeral working-mode sessions from the sidebar.
    import working_mode as _wm
    with perf.timed("sessions.list.local.summary_warm_wait"):
        session_store.wait_for_summary_index(
            _SESSION_LIST_SUMMARY_WARM_WAIT_SECONDS,
            min_published=_SESSION_LIST_SUMMARY_WARM_MIN_PUBLISHED,
        )
    with perf.timed("sessions.list.local.session_manager"):
        summaries = session_manager.list()
    with perf.timed("sessions.list.local.hide_filter"):
        return [s for s in summaries if not _wm.should_hide_from_sidebar(s)]


def _local_session_summaries_by_ids_for_sidebar(session_ids: list[str]) -> list[dict]:
    import working_mode as _wm
    with perf.timed("sessions.list.search_summary_lookup"):
        summaries = session_store.get_session_summaries_by_ids(session_ids)
    with perf.timed("sessions.list.search_hide_filter"):
        return [s for s in summaries if not _wm.should_hide_from_sidebar(s)]


def _local_session_summaries_by_ids(session_ids: list[str]) -> list[dict]:
    with perf.timed("sessions.list.summary_lookup_by_ids"):
        return session_store.get_session_summaries_by_ids(session_ids)


def _can_page_default_local_visible_order(
    *,
    project_path: str | None,
    search: str | None,
    show_archived: bool,
    file_edit_mode: bool | None,
    folder_ids: set[str],
    tag_ids: set[str],
    provider_ids: set[str],
    model_ids: set[str],
    modes: set[str],
    sources: set[str],
    content_scores: dict[str, int],
) -> bool:
    return (
        not (search or "").strip()
        and not show_archived
        and file_edit_mode is None
        and not folder_ids
        and not tag_ids
        and not provider_ids
        and not model_ids
        and not modes
        and not sources
        and not content_scores
    )


def _local_visible_order_page_ids(
    sort_by: str,
    project_path: str | None,
    offset: int,
    limit: int,
    expected_summary_index_version: int,
) -> tuple[list[str], int] | None:
    import working_mode as _wm
    key = (sort_by, project_path, offset, limit, expected_summary_index_version)
    cached = _local_visible_order_cache.get(key)
    if cached is not None:
        perf.record("sessions.list.local.visible_order_cache.hit", 1.0)
        return cached
    perf.record("sessions.list.local.visible_order_cache.miss", 1.0)
    ordered_ids = session_manager.ordered_summary_ids(sort_by)
    page_ids: list[str] = []
    total = 0
    end = offset + limit
    with perf.timed("sessions.list.local.visible_order_build"):
        for ordered_id in ordered_ids:
            summary = session_store.get_indexed_session_summary_if_current(
                ordered_id,
                expected_summary_index_version,
            )
            if summary is None:
                return None
            if project_path is not None and not session_matches_project(summary, project_path):
                continue
            if summary.get("archived") or _wm.should_hide_from_sidebar(summary):
                continue
            if offset <= total < end:
                sid = summary.get("id")
                if sid:
                    page_ids.append(str(sid))
            total += 1
    if len(_local_visible_order_cache) >= 8:
        _local_visible_order_cache.pop(next(iter(_local_visible_order_cache)), None)
    cached = (page_ids, total)
    _local_visible_order_cache[key] = cached
    return cached


def _local_session_page_for_sidebar_preserving_order(
    *,
    sort_by: str,
    offset: int,
    limit: int,
    project_path: str | None,
    search: str | None,
    show_archived: bool,
    file_edit_mode: bool | None,
    folder_ids: set[str],
    tag_ids: set[str],
    provider_ids: set[str],
    model_ids: set[str],
    modes: set[str],
    sources: set[str],
    content_scores: dict[str, int],
) -> tuple[list[dict], int]:
    import working_mode as _wm
    if _can_page_default_local_visible_order(
        project_path=project_path,
        search=search,
        show_archived=show_archived,
        file_edit_mode=file_edit_mode,
        folder_ids=folder_ids,
        tag_ids=tag_ids,
        provider_ids=provider_ids,
        model_ids=model_ids,
        modes=modes,
        sources=sources,
        content_scores=content_scores,
    ):
        with perf.timed("sessions.list.local.visible_order_page"):
            expected_summary_index_version = session_store.summary_index_version()
            visible_page = _local_visible_order_page_ids(
                sort_by,
                project_path,
                offset,
                limit,
                expected_summary_index_version,
            )
            if visible_page is None:
                perf.record("sessions.list.local.visible_order_page.indexed_miss", 1.0)
            else:
                page_ids, total = visible_page
                indexed_page = session_store.get_indexed_session_summaries_by_ids_if_current(
                    page_ids,
                    expected_summary_index_version,
                )
                if indexed_page is not None:
                    perf.record("sessions.list.local.visible_order_page.indexed_hit", 1.0)
                    return indexed_page, total
                perf.record("sessions.list.local.visible_order_page.indexed_miss", 1.0)
                return session_store.get_session_summaries_by_ids(page_ids), total
    with perf.timed("sessions.list.local.ordered_ids"):
        ordered_ids = session_manager.ordered_summary_ids(sort_by)
    page_ids: list[str] = []
    total = 0
    end = offset + limit
    with perf.timed("sessions.list.local.ordered_filter"):
        for session in session_store.get_indexed_session_summaries_by_ids(ordered_ids):
            if _wm.should_hide_from_sidebar(session):
                continue
            if not _session_matches_list_filters(
                session,
                project_path=project_path,
                search=search,
                show_archived=show_archived,
                file_edit_mode=file_edit_mode,
                folder_ids=folder_ids,
                tag_ids=tag_ids,
                provider_ids=provider_ids,
                model_ids=model_ids,
                modes=modes,
                sources=sources,
                content_scores=content_scores,
            ):
                continue
            if offset <= total < end:
                sid = session.get("id")
                if sid:
                    page_ids.append(str(sid))
            total += 1
    with perf.timed("sessions.list.local.ordered_page_lookup"):
        return session_store.get_session_summaries_by_ids(page_ids), total


def _root_session_file_path(session_id: str) -> str:
    return f"{_root_sessions_dir_path()}/{session_id}.json"


_root_sessions_dir_path_cache: tuple[str, str] | None = None


def _root_sessions_dir_path() -> str:
    global _root_sessions_dir_path_cache
    home = str(ba_home())
    cached = _root_sessions_dir_path_cache
    if cached is not None and cached[0] == home:
        return cached[1]
    sessions_dir = str(Path(home) / "sessions")
    _root_sessions_dir_path_cache = (home, sessions_dir)
    return sessions_dir


_SIDEBAR_WORKING_MODE_META_KEYS = {
    "project_cwd",
    "file_paths",
    "temp_file_path",
    "parent_session_id",
    "mode",
    "persistent",
}


def _sidebar_session_payload(session: dict) -> dict:
    sid = session.get("id")
    cache_key = id(session)
    if isinstance(sid, str):
        cached = _sidebar_payload_cache.get(cache_key)
        if cached is not None and cached[0] == sid:
            return cached[1]
    payload = {
        key: value
        for key, value in session.items()
        if key != "first_prompt"
    }
    meta = payload.get("working_mode_meta")
    if isinstance(meta, dict):
        payload["working_mode_meta"] = {
            key: meta[key]
            for key in _SIDEBAR_WORKING_MODE_META_KEYS
            if key in meta
        }
    if isinstance(sid, str):
        if len(_sidebar_payload_cache) >= _SIDEBAR_PAYLOAD_CACHE_MAX:
            _sidebar_payload_cache.pop(next(iter(_sidebar_payload_cache)), None)
        _sidebar_payload_cache[cache_key] = (sid, payload)
    return payload


def _sidebar_state_snapshot() -> tuple[set[str], dict[str, str], dict[str, int], dict[str, int]]:
    global _sidebar_state_snapshot_cache
    version = _sessions_list_transient_state_version()
    cached = _sidebar_state_snapshot_cache
    if cached is not None and cached[0] == version:
        return cached[1]
    running_sids, monitoring_by_sid = coordinator.turn_manager.cached_state_snapshot()
    unread_by_sid = session_manager.unread_counts_snapshot()
    pending_input_by_sid = user_input_store.pending_counts_by_session()
    snapshot = running_sids, monitoring_by_sid, unread_by_sid, pending_input_by_sid
    _sidebar_state_snapshot_cache = (
        _sessions_list_transient_state_version(),
        snapshot,
    )
    return snapshot


def _decorate_local_sidebar_sessions(
    sessions: list[dict],
    state_snapshot: tuple[set[str], dict[str, str], dict[str, int], dict[str, int]] | None = None,
) -> list[dict]:
    local: list[dict] = []
    with perf.timed("sessions.list.local.decorate"):
        with perf.timed("sessions.list.local.decorate.state"):
            if state_snapshot is None:
                running_sids, monitoring_by_sid, unread_by_sid, pending_input_by_sid = (
                    _sidebar_state_snapshot()
                )
            else:
                running_sids, monitoring_by_sid, unread_by_sid, pending_input_by_sid = (
                    state_snapshot
                )
            sessions_dir = _root_sessions_dir_path()
            summary_version = session_store.summary_index_version()
        for s in sessions:
            with perf.timed("sessions.list.local.decorate.payload"):
                sidebar_session = _sidebar_session_payload(s)
            node_id = s.get("node_id") or "primary"
            if node_id != "primary" or s.get("source") == "virtual":
                local.append(sidebar_session)
                continue
            sid = s.get("id")
            if not sid:
                local.append(sidebar_session)
                continue
            running = sid in running_sids
            monitoring_state = monitoring_by_sid.get(sid, "stopped")
            unread_count = unread_by_sid.get(sid, 0)
            pending_user_input_count = pending_input_by_sid.get(sid, 0)
            has_error = bool(s.get("unseen_error"))
            file_path = f"{sessions_dir}/{sid}.json"
            decorated_cache_key = (
                sid,
                summary_version,
                running,
                monitoring_state,
                unread_count,
                pending_user_input_count,
                has_error,
                file_path,
            )
            cached_decorated = _sidebar_decorated_cache.get(decorated_cache_key)
            if cached_decorated is not None:
                perf.record("sessions.list.local.decorate.row_cache.hit", 1.0)
                local.append(cached_decorated)
                continue
            perf.record("sessions.list.local.decorate.row_cache.miss", 1.0)
            # Enrich with transient running flag + lazy-hydrated unread.
            # `peek_unread_count` returns None on cache miss — we surface
            # 0 in that case so the sidebar renders immediately.
            # `is_running_cached` / `monitoring_state_cached` read from
            # the background-tick cache — no os.kill PID probing on the
            # event loop. Cache is refreshed every 2 s by the background
            # tick thread; stale by up to 2 s, acceptable for badges.
            decorated = {
                **sidebar_session,
                "is_running": running,
                "monitoring_state": monitoring_state,
                "unread_count": unread_count,
                "pending_user_input_count": pending_user_input_count,
                "has_error": has_error,
                "file_path": f"{sessions_dir}/{sid}.json",
            }
            if len(_sidebar_decorated_cache) >= _SIDEBAR_DECORATED_CACHE_MAX:
                _sidebar_decorated_cache.pop(next(iter(_sidebar_decorated_cache)), None)
            _sidebar_decorated_cache[decorated_cache_key] = decorated
            local.append(decorated)
    return local


def _sidebar_stats_payload(session: dict) -> dict:
    return {
        "token_usage_total": session.get("token_usage_total"),
        "token_usage_last": session.get("token_usage_last"),
        "context_window": session.get("context_window"),
    }


def _local_sessions_for_sidebar() -> list[dict]:
    return _decorate_local_sidebar_sessions(_local_session_summaries_for_sidebar())


_project_aggregates_cache: dict[tuple[str, str], dict[str, int]] = {}
_project_aggregates_gen = 0
_session_org_facets_cache: dict[
    tuple[str | None, int, tuple[int, int] | None],
    dict[str, Any],
] = {}


def _project_aggregates() -> dict[tuple[str, str], dict[str, int]]:
    """Compute per-project (cwd, node_id) → counts for status badges.

    Cached: recompute only when the generation counter bumps (set by
    session mutation events via `_invalidate_project_aggregates`).
    Reads from the background-tick running-state cache — no PID
    probing on the event loop."""
    global _project_aggregates_cache, _project_aggregates_gen
    if _project_aggregates_gen > 0 and _project_aggregates_cache:
        return _project_aggregates_cache
    import working_mode as _wm
    running_sids, _ = coordinator.turn_manager.cached_state_snapshot()
    unread_by_sid = session_manager.unread_counts_snapshot()
    agg: dict[tuple[str, str], dict[str, int]] = {}
    for s in session_manager.list():
        if _wm.should_hide_from_sidebar(s):
            continue
        sid = s.get("id")
        cwd = s.get("cwd") or ""
        if not sid or not cwd:
            continue
        key = (cwd, s.get("node_id") or "primary")
        slot = agg.setdefault(
            key, {"running_count": 0, "unread_session_count": 0}
        )
        if sid in running_sids:
            slot["running_count"] += 1
        if unread_by_sid.get(sid, 0) > 0:
            slot["unread_session_count"] += 1
    _project_aggregates_cache = agg
    _project_aggregates_gen += 1
    return agg


def _invalidate_project_aggregates() -> None:
    """Bump the generation counter so the next _project_aggregates call
    recomputes. Called from session mutation broadcast paths."""
    global _project_aggregates_gen
    _project_aggregates_gen = 0


@app.get("/api/projects")
async def get_projects():
    aggs = await asyncio.to_thread(_project_aggregates)
    out: list[dict] = []
    for p in await asyncio.to_thread(project_store.list_projects):
        key = (p.get("path") or "", p.get("node_id") or "primary")
        slot = aggs.get(key, {"running_count": 0, "unread_session_count": 0})
        out.append({
            **p,
            "running_count": slot["running_count"],
            "unread_session_count": slot["unread_session_count"],
        })
    return {"projects": out}


@app.post("/api/projects")
async def create_project(body: dict):
    record = await asyncio.to_thread(
        project_store.add_project,
        path=body.get("path", ""),
        name=body.get("name") or None,
        node_id=body.get("node_id") or "primary",
    )
    if not record:
        raise HTTPException(status_code=400, detail=t("error.invalid_path"))
    await _broadcast_projects_changed()
    return record


@app.delete("/api/projects")
async def delete_project(
    path: str = Query(...),
    node_id: str = Query("primary"),
):
    deleted = await asyncio.to_thread(
        project_store.remove_project,
        path,
        node_id=node_id,
    )
    if deleted:
        await _broadcast_projects_changed()
    return {"deleted": deleted}


@app.post("/api/projects/touch")
async def touch_project(body: dict):
    await asyncio.to_thread(
        project_store.touch_project,
        body.get("path", ""),
        node_id=body.get("node_id") or "primary",
    )
    await _broadcast_projects_changed()
    return {"status": "ok"}


# ── Project mappings ───────────────────────────────────────────


async def _broadcast_mappings_changed() -> None:
    await coordinator.broadcast_global("project_mappings_changed", {})


@app.get("/api/project-mappings")
async def get_project_mappings():
    return {"groups": await asyncio.to_thread(project_mapping_store.list_mappings)}


@app.post("/api/project-mappings/rebuild")
async def rebuild_project_mappings():
    projects = await asyncio.to_thread(project_store.list_projects)
    groups = await asyncio.to_thread(project_mapping_store.rebuild_and_save, projects)
    await _broadcast_mappings_changed()
    return {"groups": groups}


@app.patch("/api/project-mappings/{group_id}")
async def update_project_mapping(group_id: str, body: dict):
    result = await asyncio.to_thread(
        project_mapping_store.update_group,
        group_id,
        label=body.get("label"),
        members=body.get("members"),
    )
    if not result:
        raise HTTPException(status_code=404, detail="Mapping group not found")
    await _broadcast_mappings_changed()
    return result


@app.delete("/api/project-mappings/{group_id}")
async def delete_project_mapping(group_id: str):
    deleted = await asyncio.to_thread(project_mapping_store.remove_group, group_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Mapping group not found")
    await _broadcast_mappings_changed()
    return {"deleted": True}


# ── Project structure updates ──────────────────────────────────


def _require_project_structure_internal(x_internal_token: str) -> None:
    principal = coordinator.resolve_principal(x_internal_token)
    if principal is None:
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    _require_builtin_runtime_extension(extension_store.extension_id_for_role('project-structure'))
    if (
        principal[0] != "extension"
        or principal[1] != extension_store.extension_id_for_role('project-structure')
    ):
        raise HTTPException(status_code=403, detail="project-structure extension is required")


async def _require_project_structure_internal_async(x_internal_token: str) -> None:
    _require_project_structure_internal(x_internal_token)


def _require_project_updates_internal(x_internal_token: str) -> None:
    principal = coordinator.resolve_principal(x_internal_token)
    if principal is None:
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    _require_builtin_extension(extension_store.extension_id_for_role('project-structure'))
    if (
        principal[0] != "extension"
        or principal[1] != extension_store.extension_id_for_role('project-structure')
    ):
        raise HTTPException(status_code=403, detail="project-structure extension is required")


async def _require_project_updates_internal_async(x_internal_token: str) -> None:
    _require_project_updates_internal(x_internal_token)


def _require_capabilities_internal(x_internal_token: str) -> None:
    """Capabilities are managed by the Better Agent builtin MCP that runs inside
    the runner and calls back over the internal loopback. Gate on a valid
    internal token only."""
    if not coordinator.is_internal_caller(x_internal_token):
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))


def _capabilities_snapshot(sess: dict) -> dict:
    active = [
        str(c) for c in (sess.get("active_capability_ids") or []) if str(c or "").strip()
    ]
    catalog = []
    for cap_id, descriptor in extension_store.capability_catalog().items():
        catalog.append({
            "id": cap_id,
            "scope": descriptor.get("scope"),
            "bare_allowed": bool(descriptor.get("bare_allowed")),
            "scope_gate": descriptor.get("scope_gate", "internal"),
            "release": descriptor.get("release") or {},
            "loaded": cap_id in active,
        })
    catalog.sort(key=lambda c: c["id"])
    return {"capabilities": catalog, "active_capability_ids": active}


@app.post("/api/internal/sessions/{sid}/capabilities")
async def internal_session_capabilities(
    sid: str,
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    """List/load/release scoped capabilities for a session. Core owns the write
    (session_manager.active_capability_ids); callers are internal loopback only
    (the Better Agent capabilities builtin MCP). Delivery follows on the next
    turn — the capability's MCP self-gates on the active set, skills merge at
    assembly."""
    _require_capabilities_internal(x_internal_token)
    action = str((body or {}).get("action") or "").strip()
    if action not in ("list", "load", "release"):
        raise HTTPException(status_code=400, detail="action must be list, load or release")
    sess = await asyncio.to_thread(session_manager.get, sid)
    if not sess:
        raise HTTPException(status_code=404, detail="unknown session")
    if action == "list":
        return {"ok": True, **_capabilities_snapshot(sess)}
    capability_id = str((body or {}).get("capability_id") or "").strip()
    if not capability_id:
        raise HTTPException(status_code=400, detail="capability_id is required")
    if not extension_store.get_capability(capability_id):
        raise HTTPException(status_code=404, detail="unknown capability")
    if action == "load":
        updated = session_manager.add_active_capability(sid, capability_id)
    else:
        updated = session_manager.remove_active_capability(sid, capability_id)
    return {
        "ok": True,
        "active_capability_ids": (updated or {}).get("active_capability_ids") or [],
    }


@app.post("/api/internal/session-control/selectors")
async def internal_session_control_selectors(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    """`switch_model` tool backing. Applies model / provider_id /
    reasoning_effort to the CALLER'S OWN session (`app_session_id` from the
    loopback) through the SAME validation path as the public /selectors
    endpoint. The change takes effect on the next turn via the
    selector-change continuation (fresh provider subprocess, same session)."""
    _require_builtin_runtime_extension(extension_store.BUILTIN_SESSION_CONTROL_EXTENSION_ID)
    if not coordinator.is_internal_caller(x_internal_token):
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    sid = str((body or {}).get("app_session_id") or "").strip()
    if not sid:
        raise HTTPException(status_code=400, detail="app_session_id is required")
    updates = await _resolve_selector_updates(sid, body or {})
    before = await asyncio.to_thread(session_manager.get, sid)
    session = await asyncio.to_thread(
        session_manager.set_selectors, sid, **updates,
    )
    if not session:
        raise HTTPException(status_code=404, detail=t("error.session_not_found_retry"))
    _record_model_switched_event(sid, before or {}, session, updates)
    if "model" in updates:
        await _record_last_model(session.get("provider_id"), updates["model"])
    if updates.get("reasoning_effort"):
        await _record_last_reasoning_effort(
            session.get("provider_id"), updates["reasoning_effort"],
        )
    return {"id": sid, "updates": updates}


@app.post("/api/internal/session-control/continue-fresh")
async def internal_session_control_continue_fresh(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    """`continue_in_fresh_context` tool backing. Sets the agent-requested
    continuation flag on the CALLER'S OWN session.

    `when="next_turn"` (default): the current turn completes normally, then a
    fresh provider subprocess starts under the SAME session running the queued
    prompt. `when="now"`: abort the in-flight run immediately and start that
    fresh subprocess right away (same session, continuation_chain extended).

    The agent can only continue its own session."""
    _require_builtin_runtime_extension(extension_store.BUILTIN_SESSION_CONTROL_EXTENSION_ID)
    if not coordinator.is_internal_caller(x_internal_token):
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    sid = str((body or {}).get("app_session_id") or "").strip()
    prompt = str((body or {}).get("prompt") or "").strip()
    when = str((body or {}).get("when") or "next_turn").strip()
    if not sid:
        raise HTTPException(status_code=400, detail="app_session_id is required")
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt is required")
    if when == "now":
        landed = coordinator.turn_manager.request_immediate_continuation(sid, prompt)
        # If no live turn is running to abort, fall back to next-turn semantics
        # so the request is never silently lost.
        if landed:
            return {"ok": True, "session_id": sid, "when": "now"}
        when = "next_turn"
    if when == "next_turn":
        session_manager.set_continuation_requested(sid, prompt, when="next_turn")
        return {"ok": True, "session_id": sid, "when": "next_turn"}
    raise HTTPException(status_code=400, detail="when must be 'next_turn' or 'now'")


@app.post("/api/internal/project-updates/count")
async def internal_project_update_count(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    await _require_project_updates_internal_async(x_internal_token)
    from paths import encode_cwd

    project_id = encode_cwd((body or {}).get("cwd") or os.getcwd())
    count = await asyncio.to_thread(project_update_store.unseen_count, project_id)
    return {"project_id": project_id, "count": count}


@app.post("/api/internal/project-updates/total")
async def internal_project_update_total(
    body: dict | None = None,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    await _require_project_updates_internal_async(x_internal_token)
    with perf.timed("internal.project_updates.total"):
        count = project_update_store.peek_total_unseen()
        if count is None:
            count = await asyncio.to_thread(project_update_store.total_unseen)
        return {"count": count}


@app.post("/api/internal/project-updates/counts-batch")
async def internal_project_update_counts_batch(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    await _require_project_updates_internal_async(x_internal_token)
    cwds = (body or {}).get("cwds")
    if not isinstance(cwds, list) or any(not isinstance(cwd, str) for cwd in cwds):
        raise HTTPException(status_code=400, detail="cwds must be a list of strings")
    from paths import encode_cwd

    with perf.timed("internal.project_updates.counts_batch"):
        project_ids = [encode_cwd(cwd) for cwd in cwds]
        counts = project_update_store.peek_unseen_counts(project_ids)
        if counts is None:
            counts = await asyncio.to_thread(project_update_store.unseen_counts, project_ids)
        return counts


@app.post("/api/internal/project-updates/unseen")
async def internal_project_updates_unseen(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    await _require_project_updates_internal_async(x_internal_token)
    from paths import encode_cwd

    project_id = encode_cwd((body or {}).get("cwd") or os.getcwd())
    return await asyncio.to_thread(project_update_store.list_unseen, project_id)


@app.post("/api/internal/project-updates/capture")
async def capture_project_update(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    await _require_project_updates_internal_async(x_internal_token)
    text = body.get("text", "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")
    from paths import encode_cwd
    project_id = encode_cwd(body.get("cwd", "") or os.getcwd())
    entry, unseen_count = await asyncio.to_thread(
        lambda: (
            project_update_store.append(project_id, text),
            project_update_store.unseen_count(project_id),
        )
    )
    await coordinator.broadcast_global(
        "project_updates_changed",
        {"project_id": project_id, "unseen_count": unseen_count},
    )
    return entry


@app.post("/api/internal/provisioned-sessions")
async def internal_provisioned_session(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    """Extension-facing primitive (Better Agent SDK): run one provisioned-session
    fork for a registered spec or a validated extension-owned inline spec."""
    if not coordinator.is_internal_caller(x_internal_token):
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    extension_id = coordinator.principal_extension_id(x_internal_token) or ""
    record = extension_store.get_extension(extension_id) if extension_id else None
    if (
        record is None
        or not extension_store.is_extension_active(extension_id)
        or not extension_store.has_permission(record, "spawn_runs")
    ):
        raise HTTPException(status_code=403, detail="extension lacks spawn_runs permission")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")
    raw_spec_key = body.get("spec_key")
    if raw_spec_key is None:
        spec_key = ""
    elif isinstance(raw_spec_key, str):
        spec_key = raw_spec_key.strip()
    else:
        raise HTTPException(status_code=400, detail="spec_key must be a string")
    inline_spec = body.get("inline_spec")
    query = body.get("query", "")
    if query is None:
        query = ""
    ctx = body.get("ctx", {})
    if ctx is None:
        ctx = {}
    if bool(spec_key) == (inline_spec is not None):
        raise HTTPException(status_code=400, detail="exactly one of spec_key or inline_spec is required")
    if not isinstance(query, str):
        raise HTTPException(status_code=400, detail="query must be a string")
    if not isinstance(ctx, dict):
        raise HTTPException(status_code=400, detail="ctx must be an object")
    import provisioning

    if inline_spec is not None:
        try:
            spec = provisioning.inline_spec_from_payload(
                inline_spec,
                extension_id=extension_id,
                allowed_task_keys=set(extension_store.extension_provisioned_internal_llm_tasks(record)),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    else:
        try:
            spec = provisioning.get(spec_key)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"unknown provisioned-session spec: {spec_key}")
    try:
        result = await provisioning.run(spec, query, ctx)
    except Exception as exc:
        return {"success": False, "error": str(exc)}
    return {
        "success": True,
        "text": result.text,
        "value": result.value,
        "base_session_id": result.base_session_id,
    }


@app.post("/api/internal/extension-settings")
async def internal_extension_settings(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    """Better Agent SDK: read an extension's own declared settings. Secrets
    are resolved from the OS keychain server-side and never placed in the
    subprocess environment. The caller may read only its own settings."""
    if not coordinator.is_internal_caller(x_internal_token):
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    extension_id = coordinator.principal_extension_id(x_internal_token) or ""
    if not extension_id or not extension_store.is_extension_active(extension_id):
        raise HTTPException(status_code=403, detail="extension not active")
    key = str(body.get("key") or "").strip() if isinstance(body, dict) else ""
    try:
        resolved = extension_store.resolve_all_settings(extension_id)
    except extension_store.ExtensionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if key:
        return {"success": True, "value": resolved.get(key)}
    return {"success": True, "settings": resolved}


@app.post("/api/internal/extension-internal-llm/resolve")
async def internal_extension_internal_llm_resolve(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    if not coordinator.is_internal_caller(x_internal_token):
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    extension_id = coordinator.principal_extension_id(x_internal_token) or ""
    record = extension_store.get_extension(extension_id) if extension_id else None
    if record is None or not extension_store.is_extension_active(extension_id):
        raise HTTPException(status_code=403, detail="extension not active")
    task_key = str((body or {}).get("task_key") or "").strip()
    allowed = {
        *extension_store.extension_internal_llm_tasks(record),
        *extension_store.extension_provisioned_internal_llm_tasks(record),
    }
    if task_key not in allowed:
        raise HTTPException(status_code=403, detail="internal LLM task is not owned by this extension")
    resolved = await asyncio.to_thread(config_store.resolve_internal_llm, task_key)
    return {"success": True, "task_key": task_key, "resolved": resolved}


@app.get("/api/internal/provisioned-sessions/specs")
async def internal_provisioned_specs(
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    """SDK: list the provisioned-session spec types an extension may invoke."""
    if not coordinator.is_internal_caller(x_internal_token):
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    import provisioning

    specs = [
        {"key": spec.key, "name": spec.name, "version": spec.version}
        for spec in provisioning.all_specs()
    ]
    return {"specs": specs}


@app.post("/api/internal/broadcast-session")
async def internal_broadcast_session(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    """SDK: let an active extension emit a per-session WebSocket event.

    The event is persisted to events.jsonl via ``coordinator.broadcast_session``
    and fanned to the session's WS subscribers by the tailer. ``source`` is
    pinned to the calling extension id so emitted events are auditable and one
    extension cannot impersonate another."""
    if not coordinator.is_internal_caller(x_internal_token):
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    extension_id = coordinator.principal_extension_id(x_internal_token) or ""
    if not extension_id or not extension_store.is_extension_active(extension_id):
        raise HTTPException(status_code=403, detail="extension is not active")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")
    app_session_id = str(body.get("session_id") or body.get("app_session_id") or "").strip()
    event_type = str(body.get("event_type") or "").strip()
    data = body.get("data") or {}
    if not app_session_id:
        raise HTTPException(status_code=400, detail="session_id is required")
    if not event_type or len(event_type) > 128 or "\n" in event_type:
        raise HTTPException(status_code=400, detail="event_type must be a short single-line string")
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="data must be an object")
    await coordinator.broadcast_session(
        app_session_id, event_type, data, source=f"extension:{extension_id}",
    )
    return {"success": True, "event_type": event_type, "source": f"extension:{extension_id}"}


@app.post("/api/internal/project-updates/list")
async def internal_project_updates_list(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    await _require_project_updates_internal_async(x_internal_token)
    from paths import encode_cwd

    project_id = encode_cwd((body or {}).get("cwd") or os.getcwd())
    unseen_count, unseen_updates = await asyncio.to_thread(
        lambda: (
            project_update_store.unseen_count(project_id),
            project_update_store.list_unseen(project_id),
        )
    )
    return {
        "project_id": project_id,
        "unseen_count": unseen_count,
        "unseen_updates": unseen_updates,
    }


@app.post("/api/internal/project-updates/mark-seen")
async def internal_project_updates_mark_seen(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    await _require_project_updates_internal_async(x_internal_token)
    from paths import encode_cwd

    project_id = encode_cwd((body or {}).get("cwd") or os.getcwd())
    entry_ids = body.get("entry_ids") or []
    if not isinstance(entry_ids, list) or not all(isinstance(x, str) for x in entry_ids):
        raise HTTPException(status_code=400, detail="entry_ids must be a list of strings")
    marked, unseen_count = await asyncio.to_thread(
        lambda: (
            project_update_store.mark_seen(project_id, entry_ids),
            project_update_store.unseen_count(project_id),
        )
    )
    await coordinator.broadcast_global(
        "project_updates_changed",
        {"project_id": project_id, "unseen_count": unseen_count},
    )
    return {"success": True, "marked": marked}


@app.post("/api/internal/extension-call")
async def internal_extension_call(
    request: Request,
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    """SDK inter-extension call: let one active extension invoke another active
    extension's exposed backend surface. Extensions expose their own SDKs
    (feature-specific capabilities live in per-extension surfaces); core only
    routes the call and never bakes in feature logic."""
    if not coordinator.is_internal_caller(x_internal_token):
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    caller = coordinator.principal_extension_id(x_internal_token) or ""
    if not caller or not extension_store.is_extension_active(caller):
        raise HTTPException(status_code=403, detail="calling extension is not active")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")
    target = str(body.get("target_extension_id") or "").strip()
    path = str(body.get("path") or "").strip()
    method = str(body.get("method") or "POST").strip().upper()
    inner = body.get("body")
    if not target or not path:
        raise HTTPException(status_code=400, detail="target_extension_id and path are required")
    if method not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
        raise HTTPException(status_code=400, detail="unsupported method")
    if inner is not None and not isinstance(inner, dict):
        raise HTTPException(status_code=400, detail="body must be an object")
    if target == caller:
        raise HTTPException(status_code=400, detail="inter-extension call is for other extensions")
    if not extension_store.is_extension_active(target):
        raise HTTPException(status_code=404, detail="target extension is not active")
    import json as _json
    import extension_backend_loader
    body_bytes = _json.dumps(inner).encode("utf-8") if inner is not None else b""
    return await extension_backend_loader.invoke_extension_backend(
        target,
        path,
        method=method,
        body_bytes=body_bytes,
        base_url=str(request.base_url).rstrip("/"),
    )


def _validate_processed_requirements_body(body: dict) -> dict[str, Any]:
    _require_builtin_runtime_extension(extension_store.extension_id_for_role('requirements'))
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")

    query = body.get("query")
    if not isinstance(query, str) or not query.strip():
        raise HTTPException(status_code=400, detail="query must be a non-empty string")
    cwd = body.get("cwd", "")
    if not isinstance(cwd, str):
        raise HTTPException(status_code=400, detail="cwd must be a string")
    cwds = body.get("cwds")
    if cwds is not None and (
        not isinstance(cwds, list) or any(not isinstance(item, str) for item in cwds)
    ):
        raise HTTPException(status_code=400, detail="cwds must be a list of strings")
    all_projects = body.get("all_projects", False)
    if not isinstance(all_projects, bool):
        raise HTTPException(status_code=400, detail="all_projects must be a boolean")
    return {
        "query": query,
        "cwd": cwd,
        "cwds": cwds,
        "all_projects": all_projects,
    }


def _requirements_query_debug_fields(payload: dict[str, Any]) -> dict[str, Any]:
    query = payload.get("query") if isinstance(payload.get("query"), str) else ""
    query_bytes = query.encode("utf-8", "surrogatepass")
    return {
        "query_sha256": hashlib.sha256(query_bytes).hexdigest()[:16],
        "query_len": len(query),
        "cwd": payload.get("cwd") or "",
        "cwds_count": len(payload.get("cwds") or []),
        "all_projects": bool(payload.get("all_projects")),
    }


async def _run_processed_requirements_payload(
    payload: dict[str, Any],
    *,
    request_id: str = "",
    queue_admission: bool = False,
) -> dict[str, Any]:
    import requirement_context
    debug_fields = _requirements_query_debug_fields(payload)
    await run_requirements_query(
        "requirements.processed.prepare",
        requirement_context.prepare_requirements_local_read_context,
        executor=REQUIREMENTS_SEARCH_EXECUTOR,
    )
    try:
        # Poll-driven jobs queue for a worker instead of failing admission at
        # the sync hot-path budget. wait=True fires and the sync endpoint keep
        # the short budget so the caller's own timeout stays authoritative.
        admission_kwargs = (
            {"admission_timeout_seconds": PROCESSOR_RESULT_TIMEOUT_SECONDS}
            if queue_admission
            else {}
        )
        processed = await run_requirements_processor_query(
            "requirements.processed.processor",
            requirement_context._run_requirements_processor,
            executor=REQUIREMENTS_PROCESSOR_EXECUTOR,
            **admission_kwargs,
            **payload,
            debug_request_id=request_id,
        )
    except TimeoutError as exc:
        if request_id:
            logger.warning(
                "requirements_async_processor_timeout request_id=%s query_sha256=%s "
                "query_len=%s cwd=%s cwds_count=%s all_projects=%s",
                request_id,
                debug_fields["query_sha256"],
                debug_fields["query_len"],
                debug_fields["cwd"],
                debug_fields["cwds_count"],
                debug_fields["all_projects"],
            )
        processed = requirement_context.processor_failure_result(exc)
    return await run_requirements_query(
        "requirements.processed.finalize",
        requirement_context.build_processed_requirements_response,
        executor=REQUIREMENTS_SEARCH_EXECUTOR,
        **payload,
        processed=processed,
    )


@app.post("/api/internal/get-requirements")
async def internal_get_requirements(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    if not coordinator.is_internal_caller(x_internal_token):
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    payload = _validate_processed_requirements_body(body)
    return await _run_processed_requirements_payload(payload)


@app.post("/api/internal/get-requirements/fire")
async def internal_fire_get_requirements(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    if not coordinator.is_internal_caller(x_internal_token):
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    payload = _validate_processed_requirements_body(body)
    wait = body.get("wait", False)
    if not isinstance(wait, bool):
        raise HTTPException(status_code=400, detail="wait must be a boolean")

    requirements_async_jobs.cleanup()
    request_id = uuid.uuid4().hex
    debug_fields = _requirements_query_debug_fields(payload)
    logger.info(
        "requirements_async_fire request_id=%s query_sha256=%s query_len=%s "
        "cwd=%s cwds_count=%s all_projects=%s wait=%s",
        request_id,
        debug_fields["query_sha256"],
        debug_fields["query_len"],
        debug_fields["cwd"],
        debug_fields["cwds_count"],
        debug_fields["all_projects"],
        wait,
    )
    task = requirements_async_jobs.fire(
        request_id,
        payload,
        functools.partial(_run_processed_requirements_payload, queue_admission=not wait),
    )
    if not wait:
        return {"success": True, "id": request_id, "status": "running"}
    try:
        result = await asyncio.shield(task)
    except Exception as exc:
        return {"success": False, "id": request_id, "status": "failed", "ready": True, "error": str(exc)}
    return {"success": True, "id": request_id, "status": "complete", "ready": True, "result": result}


@app.post("/api/internal/get-requirements/results")
async def internal_get_requirements_results(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    if not coordinator.is_internal_caller(x_internal_token):
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    _require_builtin_runtime_extension(extension_store.extension_id_for_role('requirements'))
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")
    request_id = body.get("id")
    if not isinstance(request_id, str) or not request_id.strip():
        raise HTTPException(status_code=400, detail="id is required")
    wait = body.get("wait", 0.0)
    if not isinstance(wait, (int, float)) or isinstance(wait, bool) or wait < 0:
        raise HTTPException(status_code=400, detail="wait must be a non-negative number of seconds")

    requirements_async_jobs.cleanup()
    request_id = request_id.strip()
    found = requirements_async_jobs.get_or_resume(
        request_id,
        functools.partial(_run_processed_requirements_payload, queue_admission=True),
    )
    if found is None:
        return {"success": False, "error": "unknown id"}
    if isinstance(found, dict):
        return found
    task = found
    try:
        result = await asyncio.wait_for(asyncio.shield(task), timeout=float(wait))
    except asyncio.TimeoutError:
        return {"success": True, "id": request_id, "status": "running", "ready": False}
    except Exception as exc:
        return {"success": False, "id": request_id, "status": "failed", "ready": True, "error": str(exc)}
    return {"success": True, "id": request_id, "status": "complete", "ready": True, "result": result}


@app.post("/api/internal/get-requirements/unit-fts")
async def internal_requirements_unit_fts(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    _require_builtin_runtime_extension(extension_store.extension_id_for_role('requirements'))
    if not coordinator.is_internal_caller(x_internal_token):
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    payload = _validate_processed_requirements_body(body)
    fields = body.get("fields")
    if fields is not None and (
        not isinstance(fields, list) or any(not isinstance(field, str) for field in fields)
    ):
        raise HTTPException(status_code=400, detail="fields must be a list of strings")
    include_all_fields = body.get("include_all_fields", False)
    if not isinstance(include_all_fields, bool):
        raise HTTPException(status_code=400, detail="include_all_fields must be a boolean")

    import requirement_context
    return await run_requirements_query(
        "requirements.unit_fts",
        requirement_context.search_requirement_units_fts,
        executor=REQUIREMENTS_SEARCH_EXECUTOR,
        query=payload["query"],
        cwd=payload["cwd"],
        cwds=payload["cwds"],
        all_projects=payload["all_projects"],
        fields=fields,
        include_all_fields=include_all_fields,
    )


@app.post("/api/internal/get-requirements/unit-vector")
async def internal_requirements_unit_vector(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    _require_builtin_runtime_extension(extension_store.extension_id_for_role('requirements'))
    if not coordinator.is_internal_caller(x_internal_token):
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    payload = _validate_processed_requirements_body(body)
    fields = body.get("fields")
    if fields is not None and (
        not isinstance(fields, list) or any(not isinstance(field, str) for field in fields)
    ):
        raise HTTPException(status_code=400, detail="fields must be a list of strings")
    include_all_fields = body.get("include_all_fields", False)
    if not isinstance(include_all_fields, bool):
        raise HTTPException(status_code=400, detail="include_all_fields must be a boolean")

    import requirement_context
    return await run_requirements_query(
        "requirements.unit_vector",
        requirement_context.search_requirement_units_vector,
        executor=REQUIREMENTS_SEARCH_EXECUTOR,
        query=payload["query"],
        cwd=payload["cwd"],
        cwds=payload["cwds"],
        all_projects=payload["all_projects"],
        fields=fields,
        include_all_fields=include_all_fields,
    )


@app.post("/api/internal/get-requirements/index-sql")
async def internal_requirements_index_sql(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    _require_builtin_runtime_extension(extension_store.extension_id_for_role('requirements'))
    if not coordinator.is_internal_caller(x_internal_token):
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")
    sql = body.get("sql")
    if not isinstance(sql, str) or not sql.strip():
        raise HTTPException(status_code=400, detail="sql must be a non-empty string")

    import requirement_context
    return await run_requirements_query(
        "requirements.index_sql",
        requirement_context.run_native_index_sql,
        executor=REQUIREMENTS_SEARCH_EXECUTOR,
        sql=sql,
    )


def _latest_user_task_text(session: dict) -> str:
    return _latest_message_text(session, "user")


def _latest_assistant_response_text(session: dict) -> str:
    return _latest_message_text(session, "assistant")


def _latest_message_text(session: dict, role: str) -> str:
    """Latest message text for `role`, read from an already-fetched session
    snapshot. Callers MUST pass a session dict (e.g. one fetched off-loop via
    `get_lite`) — this MUST NOT fetch on the event loop: a full `get()`
    deepcopy here blocked the loop for ~1.8s on large sessions (auto-tagging
    `current-task`), and `internal_auto_tagging` already fetches the session
    off-loop before calling this."""
    for msg in reversed((session or {}).get("messages") or []):
        if msg.get("role") != role:
            continue
        return _message_text(msg)
    return ""


def _message_text(msg: dict) -> str:
    content = msg.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts).strip()
    text = msg.get("text")
    return text.strip() if isinstance(text, str) else ""


def _session_auto_tagging_eligible(session: dict) -> bool:
    if not session:
        return False
    if session.get("parent_session_id") or session.get("bare_config"):
        return False
    return session.get("source") not in {"internal", "provisioning", "subprocess_agent"}


def _auto_tagging_selector_module():
    """Lazy-load the auto-tagging extension's provisioned tag-selector
    package (tagging_selector) so the worker spec registers in-process."""
    import extension_package_loader
    extension_package_loader.ensure_package_importable(
        "ofek-dev.auto-tagging", "tagging_selector"
    )
    import importlib
    return importlib.import_module("tagging_selector")


_TAG_SOURCE_OWNERS = {
    session_organization_store.TAG_SOURCE_AUTO_TAGGING: "ofek-dev.auto-tagging",
    session_organization_store.TAG_SOURCE_REQUIREMENT_ANALYSIS: extension_store.extension_id_for_role('requirements'),
}


def _require_tag_source_owner(source: object, token: str) -> None:
    source_name = str(source or session_organization_store.TAG_SOURCE_MANUAL).strip()
    owner = _TAG_SOURCE_OWNERS.get(source_name)
    if owner and coordinator.principal_extension_id(token) != owner:
        raise HTTPException(status_code=403, detail=f"{source_name} tag source is owned by {owner}")


@app.post("/api/internal/auto-tagging")
async def internal_auto_tagging(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    if coordinator.principal_extension_id(x_internal_token) != "ofek-dev.auto-tagging":
        raise HTTPException(status_code=403, detail="auto-tagging extension is required")
    not_ready = extension_store.runtime_not_ready_message("ofek-dev.auto-tagging")
    if not_ready:
        raise HTTPException(status_code=403, detail=not_ready)
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")
    action = str(body.get("action") or "").strip()
    try:
        if action == "current-task":
            session_id = str(body.get("session_id") or "").strip()
            if not session_id:
                raise ValueError("session_id is required")
            session = await asyncio.to_thread(
                session_manager.get_lite, session_id,
            ) or {}
            return {
                "success": True,
                "task": _latest_user_task_text(session),
                "last_response": _latest_assistant_response_text(session),
                "cwd": str(session.get("cwd") or ""),
                "eligible": _session_auto_tagging_eligible(session),
            }
        if action == "select-tags":
            task = str(body.get("task") or "").strip()
            if not task:
                raise ValueError("task is required")
            evidence = body.get("evidence")
            if not isinstance(evidence, list) or not evidence:
                raise ValueError("evidence must be a non-empty list")
            existing_tags = body.get("existing_tags")
            if not isinstance(existing_tags, list):
                raise ValueError("existing_tags must be a list")
            max_tags = body.get("max_tags")
            if not isinstance(max_tags, int) or isinstance(max_tags, bool) or max_tags <= 0:
                raise ValueError("max_tags must be a positive integer")
            cwd = body.get("cwd") or ""
            if not isinstance(cwd, str):
                raise ValueError("cwd must be a string")
            selector = _auto_tagging_selector_module()
            try:
                labels = await asyncio.to_thread(
                    selector.select_labels,
                    task=task,
                    evidence=evidence,
                    existing_tags=existing_tags,
                    max_tags=max_tags,
                    cwd=cwd,
                )
            except Exception as exc:
                raise HTTPException(
                    status_code=502, detail=f"tag-selector worker failed: {exc}"
                )
            return {"success": True, "labels": labels}
        if action == "snapshot":
            return {
                "success": True,
                "organization": await asyncio.to_thread(
                    session_organization_store.snapshot,
                    body.get("project_id"),
                ),
            }
        if action == "ensure-tag":
            tag = await asyncio.to_thread(
                session_organization_store.ensure_tag,
                name=body.get("name"),
                project_id=body.get("project_id"),
                color=body.get("color"),
            )
            await _broadcast_session_organization_changed()
            return {"success": True, "tag": tag}
        if action == "sync-session-tags":
            session_id = str(body.get("session_id") or "").strip()
            if not await _session_exists(session_id):
                raise HTTPException(status_code=404, detail=t("error.session_not_found_retry"))
            source = str(body.get("source") or session_organization_store.TAG_SOURCE_AUTO_TAGGING).strip()
            if source != session_organization_store.TAG_SOURCE_AUTO_TAGGING:
                raise ValueError("sync-session-tags source must be auto_tagging")
            merge = body.get("merge", False)
            if not isinstance(merge, bool):
                raise ValueError("merge must be a boolean")
            org = await asyncio.to_thread(
                session_organization_store.sync_session_tags_by_source,
                session_id,
                tag_ids=body.get("tag_ids"),
                source=source,
                merge=merge,
            )
            await _broadcast_session_organization_changed([session_id])
            return {"success": True, "session_id": session_id, "organization": org}
        if action == "tags-sql":
            sql = body.get("sql")
            if not isinstance(sql, str) or not sql.strip():
                raise ValueError("sql must be a non-empty string")
            result = await asyncio.to_thread(session_organization_store.run_tags_readonly_sql, sql)
            return {"success": "error" not in result, **result}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    raise HTTPException(status_code=400, detail="unknown auto-tagging action")


@app.post("/api/internal/get-requirements/search")
async def internal_search_requirements(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    _require_builtin_runtime_extension(extension_store.extension_id_for_role('requirements'))
    if not coordinator.is_internal_caller(x_internal_token):
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")

    rg_args = body.get("rg_args")
    query = body.get("query", "")
    if rg_args is not None and (
        not isinstance(rg_args, list) or any(not isinstance(arg, str) for arg in rg_args)
    ):
        raise HTTPException(status_code=400, detail="rg_args must be a list of strings")
    if not isinstance(query, str):
        raise HTTPException(status_code=400, detail="query must be a string")
    if rg_args is not None and query.strip():
        raise HTTPException(status_code=400, detail="provide either rg_args or query, not both")
    if rg_args is None and not query.strip():
        raise HTTPException(status_code=400, detail="rg_args or query is required")
    cwd = body.get("cwd", "")
    if not isinstance(cwd, str):
        raise HTTPException(status_code=400, detail="cwd must be a string")
    cwds = body.get("cwds")
    if cwds is not None and (
        not isinstance(cwds, list) or any(not isinstance(item, str) for item in cwds)
    ):
        raise HTTPException(status_code=400, detail="cwds must be a list of strings")
    all_projects = body.get("all_projects", False)
    if not isinstance(all_projects, bool):
        raise HTTPException(status_code=400, detail="all_projects must be a boolean")
    fields = body.get("fields")
    if fields is not None and (
        not isinstance(fields, list) or any(not isinstance(field, str) for field in fields)
    ):
        raise HTTPException(status_code=400, detail="fields must be a list of strings")
    include_all_fields = body.get("include_all_fields", False)
    if not isinstance(include_all_fields, bool):
        raise HTTPException(status_code=400, detail="include_all_fields must be a boolean")
    include_unprocessed_prompts = body.get("include_unprocessed_prompts", False)
    if not isinstance(include_unprocessed_prompts, bool):
        raise HTTPException(status_code=400, detail="include_unprocessed_prompts must be a boolean")
    provider_native_only = body.get("provider_native_only", True)
    if not isinstance(provider_native_only, bool):
        raise HTTPException(status_code=400, detail="provider_native_only must be a boolean")
    compare = body.get("compare", False)
    if not isinstance(compare, bool):
        raise HTTPException(status_code=400, detail="compare must be a boolean")
    max_matches = body.get("max_matches")
    if max_matches is not None and (
        not isinstance(max_matches, int) or isinstance(max_matches, bool) or max_matches <= 0
    ):
        raise HTTPException(status_code=400, detail="max_matches must be a positive integer when provided")

    import requirement_context
    return await run_requirements_query(
        "requirements.search",
        requirement_context.search_requirements,
        executor=REQUIREMENTS_SEARCH_EXECUTOR,
        rg_args=rg_args,
        query=query,
        cwd=cwd,
        cwds=cwds,
        all_projects=all_projects,
        fields=fields,
        include_all_fields=include_all_fields,
        include_unprocessed_prompts=include_unprocessed_prompts,
        provider_native_only=provider_native_only,
        compare=compare,
        max_matches=max_matches,
    )


# ── Project structure edit session ──────────────────────────────


@app.post("/api/internal/project-structure-edit/status")
async def internal_project_structure_edit_status(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    await _require_project_structure_internal_async(x_internal_token)
    cwd = (body or {}).get("cwd") or os.getcwd()
    return await asyncio.to_thread(project_structure_edit_session.get_edit_status, cwd)


@app.post("/api/internal/project-structure-edit/ensure")
async def internal_project_structure_edit_ensure(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    await _require_project_structure_internal_async(x_internal_token)
    cwd = (body or {}).get("cwd") or os.getcwd()
    prompt_result = await project_structure_edit_session.submit_review_prompt(cwd)
    return {
        "session_id": project_structure_edit_session.EDIT_SINGLETON_ID,
        **prompt_result,
    }


@app.get("/api/project-config")
async def get_project_config(
    cwd: str = Query(...),
    node_id: str = Query("primary"),
):
    result = await _file_op(node_id, "scan_project_configs", {"cwd": cwd})
    # rpc handler returns the raw scan dict; legacy endpoint shape
    # wraps it as {"files": ...}. Preserve that envelope.
    return {"files": result}


@app.get("/api/file")
async def get_file(
    path: str = Query(...),
    node_id: str = Query("primary"),
):
    return await _file_op(node_id, "get_file_content", {"path": path})


@app.get("/api/file/metadata")
async def get_file_metadata(
    path: str = Query(...),
    node_id: str = Query("primary"),
):
    return await _file_op(node_id, "get_file_metadata", {"path": path})


@app.get("/api/file/draft")
async def get_file_draft(
    path: str = Query(...),
    node_id: str = Query("primary"),
):
    return file_panel_drafts.read_draft(path, node_id)


@app.post("/api/file/draft")
async def save_file_draft(body: dict):
    node_id = body.get("node_id") or "primary"
    return file_panel_drafts.write_draft(
        path=body["path"],
        node_id=node_id,
        content=body["content"],
        base_identity=body.get("base_identity"),
    )


@app.delete("/api/file/draft")
async def delete_file_draft(
    path: str = Query(...),
    node_id: str = Query("primary"),
):
    return file_panel_drafts.delete_draft(path, node_id)


@app.get("/api/file/raw")
async def get_raw_file(
    request: Request,
    path: str = Query(...),
    node_id: str = Query("primary"),
):
    """Serve a binary file (PDF, video, audio) with correct Content-Type.
    Supports HTTP Range requests for video seeking. For sessions hosted
    on a worker-node the bytes are pulled over the node WS in base64
    chunks (`read_file_raw_range`); local files stream straight off disk."""
    return await _serve_raw_file(request, path, node_id)


async def _serve_raw_file(
    request: Request,
    path: str,
    node_id: str,
    allow_preview_types: bool = False,
):
    """Shared raw-file streamer for /api/file/raw and the signed preview
    route. `allow_preview_types` widens the extension allowlist to web
    assets and is only ever set by the token-gated preview route — it is
    deliberately not a query param on /api/file/raw."""
    try:
        from topology import local_node_id as _lid
        is_local = node_id in ("primary", _lid())
    except Exception:
        is_local = node_id == "primary"

    info = await _file_op(
        node_id, "get_raw_file_info",
        {"path": path, "allow_preview_types": allow_preview_types},
    )
    size = info["size"]
    mime = info["mime_type"]
    file_path = Path(info["path"])

    start, end, status = 0, size - 1, 200
    range_header = request.headers.get("range") if hasattr(request, "headers") else None
    if range_header:
        import re as _re
        match = _re.match(r"bytes=(\d+)-(\d*)", range_header)
        if match:
            start = int(match.group(1))
            end = int(match.group(2)) if match.group(2) else size - 1
            end = min(end, size - 1)
            status = 206
    content_length = end - start + 1

    if is_local and status == 200:
        from starlette.responses import FileResponse
        return FileResponse(
            file_path, media_type=mime, filename=file_path.name,
        )

    if is_local:
        async def _sender():
            with open(file_path, "rb") as f:
                f.seek(start)
                remaining = content_length
                while remaining > 0:
                    chunk = f.read(min(65536, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk
    else:
        async def _sender():
            import base64
            offset = start
            remaining = content_length
            while remaining > 0:
                res = await _file_op(
                    node_id, "read_file_raw_range",
                    {"path": path, "start": offset,
                     "length": min(4 * 1024 * 1024, remaining),
                     "allow_preview_types": allow_preview_types},
                )
                chunk = base64.b64decode(res["data_b64"])
                if not chunk:
                    break
                offset += len(chunk)
                remaining -= len(chunk)
                yield chunk

    headers = {
        "Content-Length": str(content_length),
        "Accept-Ranges": "bytes",
        "Content-Type": mime,
    }
    if status == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"

    from starlette.responses import StreamingResponse
    return StreamingResponse(_sender(), status_code=status, headers=headers)


@app.get("/api/file/preview-url")
async def get_file_preview_url(
    path: str = Query(...),
    node_id: str = Query("primary"),
):
    """Mint a signed, expiring, directory-scoped preview URL (authed).
    The preview iframe runs in an opaque origin that cannot send the
    session cookie, so the URL's signature is the credential for the
    /api/file/preview/ route."""
    try:
        return {"url": file_preview_urls.mint(path, node_id)}
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid preview path")


@app.get("/api/file/preview/{token}/{node_id}/{file_path:path}")
async def preview_file(request: Request, token: str, node_id: str, file_path: str):
    """Serve a file for in-panel/new-tab HTML preview. Path-based routing
    (unlike the query-based /api/file/raw) so relative asset URLs inside
    the page resolve to sibling files naturally. Gated by the signed
    token, confined to the signed directory tree. HTML/SVG responses
    carry a CSP sandbox — scripts run in an opaque origin and cannot
    reach Better Agent's origin, cookies, or DOM."""
    try:
        norm_path = file_preview_urls.verify(token, node_id, f"/{file_path}")
    except ValueError:
        raise HTTPException(status_code=403, detail="invalid preview token")
    response = await _serve_raw_file(
        request, norm_path, node_id, allow_preview_types=True,
    )
    response.headers["content-disposition"] = "inline"
    mime = response.headers.get("content-type", "")
    if mime.startswith("text/html") or mime.startswith("image/svg"):
        response.headers["content-security-policy"] = (
            "sandbox allow-scripts allow-popups allow-forms allow-modals allow-downloads"
        )
        response.headers["x-content-type-options"] = "nosniff"
    return response


@app.post("/api/file")
async def save_file(body: dict):
    return await _file_op(
        body.get("node_id") or "primary",
        "write_file_content",
        {"path": body["path"], "content": body["content"]},
    )


@app.post("/api/file-before-edit")
async def get_file_before_edit(body: dict):
    return await _file_op(
        body.get("node_id") or "primary",
        "reconstruct_before_edit",
        {
            "file_path": body.get("file_path", ""),
            "old_string": body.get("old_string", ""),
            "new_string": body.get("new_string", ""),
        },
    )


@app.get("/api/git-status")
async def get_git_status(
    cwd: str = Query(...),
    node_id: str = Query("primary"),
):
    return await _cached_git_status(node_id, cwd)


@app.get("/api/git-diff")
async def get_git_diff(
    path: str = Query(...),
    cwd: str = Query(...),
    node_id: str = Query("primary"),
):
    # rpc handler returns {"diff": str|None}; pass through unchanged.
    return await _file_op(
        node_id, "get_file_diff", {"file_path": path, "cwd": cwd},
    )


@app.post("/api/git-commit")
async def post_git_commit(body: dict):
    node_id = body.get("node_id") or "primary"
    cwd = body.get("cwd", "")
    _clear_git_status_cache(node_id, cwd)
    result = await _file_op(
        node_id,
        "git_commit",
        {"cwd": cwd, "message": body.get("message", "")},
    )
    _clear_git_status_cache(node_id, cwd)
    return result


@app.post("/api/git-commit-and-push")
async def post_git_commit_and_push(body: dict):
    node_id = body.get("node_id") or "primary"
    cwd = body.get("cwd", "")
    _clear_git_status_cache(node_id, cwd)
    result = await _file_op(
        node_id,
        "git_commit_and_push",
        {"cwd": cwd, "message": body.get("message", "")},
    )
    _clear_git_status_cache(node_id, cwd)
    return result


@app.get("/api/hooks")
async def get_hooks():
    try:
        return {"hooks": await asyncio.to_thread(hook_store.list_hooks)}
    except hook_store.HookConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.put("/api/hooks")
async def put_hooks(body: dict):
    hooks = body.get("hooks")
    if not isinstance(hooks, list):
        raise HTTPException(status_code=400, detail="hooks must be a list")
    try:
        return {"hooks": await asyncio.to_thread(hook_store.replace_hooks, hooks)}
    except hook_store.HookConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/hooks")
async def post_hook(body: dict):
    try:
        return {"hook": await asyncio.to_thread(hook_store.upsert_hook, body)}
    except hook_store.HookConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.delete("/api/hooks/{hook_id}")
async def delete_hook(hook_id: str):
    try:
        deleted = await asyncio.to_thread(hook_store.delete_hook, hook_id)
    except hook_store.HookConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not deleted:
        raise HTTPException(status_code=404, detail="hook not found")
    return {"ok": True}


def _parse_session_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = f"{raw[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        return parsed.astimezone().replace(tzinfo=None)
    return parsed


async def _delete_session_tree(session_id: str) -> bool:
    # Per-step timing so a slow delete repro names the offending step
    # (cancel / cascade / store / run-dirs / task-refs / fanout). Logged
    # once at the end; the wrapping endpoint also logs the handler total.
    import time as _time
    _dt0 = _time.perf_counter()
    _dsteps: list[tuple[str, float]] = []

    def _dmark(label: str, t0: float) -> None:
        _dsteps.append((label, (_time.perf_counter() - t0) * 1000.0))

    _t = _time.perf_counter()
    await coordinator.cancel_session(session_id)
    _dmark("cancel_session", _t)
    _t = _time.perf_counter()
    try:
        # The list() summary already carries `working_mode` and
        # `working_mode_meta` (session_store._build_summary_for_root), so we
        # can find this session's working-mode children with a pure in-memory
        # scan. Calling `_session_lite` here used to load + deepcopy the full
        # session tree per working-mode session (~778 of them), which was the
        # dominant delete cost after run-dir reaping was indexed.
        for s in await asyncio.to_thread(session_manager.list):
            if not s.get("working_mode"):
                continue
            meta = s.get("working_mode_meta") or {}
            if meta.get("parent_session_id") != session_id:
                continue
            child_id = s["id"]
            await coordinator.cancel_session(child_id)
            await event_bus.publish(BusEvent(
                type="session.parent_deleted",
                root_id=child_id,
                sid=child_id,
                payload={
                    "parent_session_id": session_id,
                    "child_session_id": child_id,
                    "working_mode": s.get("working_mode"),
                },
                persist=False,
            ))
    except Exception:
        logger.exception("cascade working-mode cleanup failed during session delete")
    _dmark("working_mode_cascade", _t)

    _t = _time.perf_counter()
    removed_sids = await asyncio.to_thread(session_manager.subtree_ids, session_id)
    ok = await asyncio.to_thread(session_manager.delete, session_id)
    _dmark("session_delete", _t)
    if ok:
        _t = _time.perf_counter()
        try:
            await asyncio.to_thread(runs_dir.delete_runs_for_sessions, removed_sids)
        except Exception:
            logger.exception("run-dir cleanup failed during session delete")
        _dmark("runs_cleanup", _t)
        # Drop any task deep-link breadcrumbs / singleton bindings that
        # pointed at deleted sessions, so the Routines tab never links to a
        # gone session. Best-effort, store-only; safe (no-op) when the
        # routines extension isn't installed (empty store).
        _t = _time.perf_counter()
        try:
            from stores import task_store as _task_store
            for _removed in removed_sids:
                if await asyncio.to_thread(_task_store.drop_session_references, _removed):
                    # A reference changed — ping tabs to refetch. cwd/node
                    # are unknown here; a null-cwd ping invalidates broadly,
                    # like worker fan-out's cross-cwd broadcast.
                    await coordinator.broadcast_global(
                        "tasks_changed", {"cwd": None, "node_id": "primary"},
                    )
        except Exception:
            logger.debug("task reference cleanup failed during session delete", exc_info=True)
        _dmark("task_ref_cleanup", _t)
    _t = _time.perf_counter()
    await _publish_worker_fanout_required(
        session_id,
        op_label="session delete",
        caller_scope=True,
        remove_worker=True,
        outer_log_msg="worker fan-out failed during session delete",
    )
    _dmark("worker_fanout", _t)
    logger.info(
        "delete_session_tree sid=%s total=%.0fms steps=[%s]",
        session_id,
        (_time.perf_counter() - _dt0) * 1000.0,
        ", ".join(f"{n}={ms:.0f}ms" for n, ms in _dsteps),
    )
    return ok


async def _auto_delete_expired_sessions() -> None:
    days = await asyncio.to_thread(user_prefs.get_session_auto_delete_days)
    if days is None:
        return
    cutoff = datetime.now() - timedelta(days=days)
    summaries = await asyncio.to_thread(session_manager.list)
    for summary in list(summaries):
        sid = summary.get("id")
        if not sid:
            continue
        updated_at = _parse_session_timestamp(summary.get("updated_at"))
        if updated_at is None or updated_at >= cutoff:
            continue
        if coordinator.turn_manager.is_running_cached(sid):
            continue
        try:
            deleted = await _delete_session_tree(sid)
            if deleted:
                logger.info(
                    "auto_delete_expired_session sid=%s days=%s updated_at=%s",
                    sid, days, summary.get("updated_at"),
                )
        except Exception:
            logger.exception("auto-delete expired session failed sid=%s", sid)


# Attention-marker tags emitted by the user-attention extension. The tag
# rides the marker projection (see file_ref_resolver.detect_markers); we
# read it here rather than match on color/tooltip (which drift).
_MARKER_TAG_NEEDS_DECISION = "NEEDS_USER_DECISION"
_MARKER_TAG_ALL_TASKS_DONE = "ALL_TASKS__DONE"
_RUNNING_STATES = ("active", "waiting_on_background")


def _has_open_work_items(session: dict) -> bool:
    items = (
        list(session.get("current_todos") or [])
        + list(session.get("current_tasks") or [])
    )
    return any(
        (item or {}).get("status") != "completed"
        for item in items
        if isinstance(item, dict)
    )


def _session_status_rank(
    session: dict,
    monitoring_by_sid: dict[str, str],
    unread_by_sid: dict[str, int],
    pending_input_by_sid: dict[str, int] | None = None,
) -> int:
    """Status bucket for the status-sort option. Higher sorts first."""
    sid = session.get("id") or ""
    # Snapshot wins for local rows (their summary has no monitoring_state at
    # sort time); fall back to the row's own fields for remote-node rows that
    # aren't in the local snapshot.
    state = monitoring_by_sid.get(sid) or session.get("monitoring_state") or "stopped"
    pending_inputs = None
    if pending_input_by_sid is not None:
        pending_inputs = pending_input_by_sid.get(sid)
    if pending_inputs is None:
        pending_inputs = session.get("pending_user_input_count", 0)
    try:
        pending_input_count = max(0, int(pending_inputs or 0))
    except (TypeError, ValueError):
        pending_input_count = 0
    if session.get("has_error") or session.get("unseen_error"):
        return 6
    markers = session.get("markers") or {}
    tags = {
        (m or {}).get("tag")
        for m in markers.values()
        if isinstance(m, dict)
    }
    if (
        state == "blocked_on_user"
        or pending_input_count > 0
        or _MARKER_TAG_NEEDS_DECISION in tags
    ):
        return 5
    unread = unread_by_sid.get(sid)
    if unread is None:
        unread = session.get("unread_count", 0)
    if (unread or 0) > 0 and state not in _RUNNING_STATES:
        return 4
    if _has_open_work_items(session):
        return 3
    if state in _RUNNING_STATES:
        return 2
    if _MARKER_TAG_ALL_TASKS_DONE in tags:
        return 1
    return 0


def _session_list_sort_key(
    session: dict,
    folder_view: bool,
    sort_by: str,
    *,
    status_sort: bool = False,
    monitoring_by_sid: dict[str, str] | None = None,
    unread_by_sid: dict[str, int] | None = None,
    pending_input_by_sid: dict[str, int] | None = None,
) -> tuple:
    # empty-new and pinned stay above status; status is the strongest key
    # below them, time the tie-break: (isEmpty, pinned, [status], ts).
    inner: tuple = (
        int(session.get("message_count", 0) or 0) == 0,
        bool(session.get("pinned", False)),
    )
    if status_sort:
        inner += (
            _session_status_rank(
                session,
                monitoring_by_sid or {},
                unread_by_sid or {},
                pending_input_by_sid or {},
            ),
        )
    inner += (session_store.timestamp_sort_value(session.get(sort_by)),)
    if not folder_view:
        return inner
    # folderized sessions first when folder view is on
    return (bool(session.get("folder_id")),) + inner


def _split_session_filter(value: str | None) -> set[str]:
    if not value:
        return set()
    return {item.strip() for item in value.split(",") if item.strip()}


def _split_session_search_fields(value: str | None) -> set[str]:
    if value is None:
        return set(session_store.DEFAULT_SEARCH_FIELDS)
    return {
        item
        for item in (part.strip() for part in value.split(","))
        if item in session_store.SEARCH_FIELDS
    }


def _session_filter_list_from_body(body: dict, key: str) -> set[str]:
    value = body.get(key)
    if value is None:
        return set()
    if not isinstance(value, list):
        raise HTTPException(status_code=400, detail=f"{key} must be a list")
    out: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            raise HTTPException(status_code=400, detail=f"{key} must contain strings")
        stripped = item.strip()
        if stripped:
            out.add(stripped)
    return out


def _session_filter_str_from_body(body: dict, key: str) -> str | None:
    value = body.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise HTTPException(status_code=400, detail=f"{key} must be a string")
    stripped = value.strip()
    return stripped or None


def _session_filter_bool_from_body(body: dict, key: str) -> bool:
    value = body.get(key, False)
    if not isinstance(value, bool):
        raise HTTPException(status_code=400, detail=f"{key} must be a boolean")
    return value


def _session_filter_optional_bool_from_body(body: dict, key: str) -> bool | None:
    value = body.get(key)
    if value is None:
        return None
    if not isinstance(value, bool):
        raise HTTPException(status_code=400, detail=f"{key} must be a boolean")
    return value


def _session_list_filter_args_from_body(body: dict | None) -> dict[str, Any]:
    if body is None:
        body = {}
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")
    allowed = {
        "project_path",
        "search",
        "show_archived",
        "file_edit_mode",
        "folder_id",
        "tag_ids",
        "provider_ids",
        "model_ids",
        "modes",
        "sources",
    }
    unknown = set(body) - allowed
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"unexpected fields: {', '.join(sorted(unknown))}",
        )
    return {
        "project_path": _session_filter_str_from_body(body, "project_path"),
        "search": _session_filter_str_from_body(body, "search"),
        "show_archived": _session_filter_bool_from_body(body, "show_archived"),
        "file_edit_mode": _session_filter_optional_bool_from_body(body, "file_edit_mode"),
        "folder_id": _session_filter_str_from_body(body, "folder_id"),
        "tag_ids": _session_filter_list_from_body(body, "tag_ids"),
        "provider_ids": _session_filter_list_from_body(body, "provider_ids"),
        "model_ids": _session_filter_list_from_body(body, "model_ids"),
        "modes": _session_filter_list_from_body(body, "modes"),
        "sources": _session_filter_list_from_body(body, "sources"),
    }


def _session_matches_list_filters(
    session: dict,
    *,
    project_path: str | None,
    search: str | None,
    show_archived: bool,
    file_edit_mode: bool | None,
    folder_ids: set[str],
    tag_ids: set[str],
    provider_ids: set[str],
    model_ids: set[str],
    modes: set[str],
    sources: set[str],
    content_scores: dict[str, int] | None = None,
) -> bool:
    if not show_archived and session.get("archived"):
        return False
    if file_edit_mode is not None:
        is_file_edit_mode = session.get("working_mode") == "file_editing"
        if is_file_edit_mode != file_edit_mode:
            return False
    if not session_matches_project(session, project_path):
        return False
    if folder_ids and (session.get("folder_id") or "") not in folder_ids:
        return False
    if provider_ids and (session.get("provider_id") or "") not in provider_ids:
        return False
    if model_ids and (session.get("model") or "") not in model_ids:
        return False
    if modes and (session.get("orchestration_mode") or "team") not in modes:
        return False
    if sources:
        source = session.get("source") or "web"
        user_aware_bucket = "user" if session.get("user_initiated") else "system"
        if source not in sources and user_aware_bucket not in sources:
            return False
    if tag_ids:
        filter_ids = session.get("tag_filter_ids")
        if not isinstance(filter_ids, list):
            filter_ids = _session_tag_filter_ids(session)
        if not tag_ids.issubset(filter_ids):
            return False
    q = (search or "").strip().lower()
    if q:
        if not (content_scores and session.get("id") in content_scores):
            return False
    return True


def _session_tag_filter_ids(session: dict) -> set[str]:
    ids: set[str] = set()
    for tag in session.get("session_tags") or []:
        if isinstance(tag, dict) and isinstance(tag.get("id"), str):
            ids.add(tag["id"])
    for tag in session.get("requirement_tags") or []:
        if not isinstance(tag, dict):
            continue
        kind = tag.get("kind")
        tag_id = tag.get("id")
        if isinstance(kind, str) and isinstance(tag_id, str):
            ids.add(f"req:{kind}:{tag_id}")
    return ids


def _session_filtered_sort_key(
    session: dict,
    *,
    folder_view: bool,
    search: str | None,
    content_scores: dict[str, int],
    sort_by: str,
    status_sort: bool = False,
    monitoring_by_sid: dict[str, str] | None = None,
    unread_by_sid: dict[str, int] | None = None,
    pending_input_by_sid: dict[str, int] | None = None,
) -> tuple:
    # In search mode relevance dominates; status only breaks ties below the
    # search score: (pinned, score>0, score, [status], ts).
    search_score = content_scores.get(str(session.get("id") or ""), 0)
    inner: tuple = (
        bool(session.get("pinned", False)),
        search_score > 0,
        search_score,
    )
    if status_sort:
        inner += (
            _session_status_rank(
                session,
                monitoring_by_sid or {},
                unread_by_sid or {},
                pending_input_by_sid or {},
            ),
        )
    inner += (session_store.timestamp_sort_value(session.get(sort_by)),)
    if not folder_view:
        return inner
    # folderized sessions first when folder view is on
    return (bool(session.get("folder_id")),) + inner


def _filter_sort_sessions_for_list(
    sessions: list[dict],
    *,
    project_path: str | None,
    search: str | None,
    show_archived: bool,
    file_edit_mode: bool | None,
    folder_ids: set[str],
    folder_view: bool,
    tag_ids: set[str],
    provider_ids: set[str],
    model_ids: set[str],
    modes: set[str],
    sources: set[str],
    content_scores: dict[str, int],
    sort_by: str,
    status_sort: bool = False,
    state_snapshot: tuple[set[str], dict[str, str], dict[str, int], dict[str, int]] | None = None,
) -> list[dict]:
    out = [
        session for session in sessions
        if _session_matches_list_filters(
            session,
            project_path=project_path,
            search=search,
            show_archived=show_archived,
            file_edit_mode=file_edit_mode,
            folder_ids=folder_ids,
            tag_ids=tag_ids,
            provider_ids=provider_ids,
            model_ids=model_ids,
            modes=modes,
            sources=sources,
            content_scores=content_scores,
        )
    ]
    # Snapshots read ONCE per request (not per-session) — the same cheap
    # caches the decorate step uses. monitoring is the 2s background-tick
    # cache; the frontend's live registry rank is the authoritative interim
    # view between fetches (see useSession debounced refetch).
    monitoring_by_sid: dict[str, str] = {}
    unread_by_sid: dict[str, int] = {}
    pending_input_by_sid: dict[str, int] = {}
    if status_sort:
        if state_snapshot is None:
            state_snapshot = _sidebar_state_snapshot()
        _, monitoring_by_sid, unread_by_sid, pending_input_by_sid = state_snapshot
    out.sort(
        key=(
            (lambda session: _session_filtered_sort_key(
                session,
                folder_view=folder_view,
                search=search,
                content_scores=content_scores,
                sort_by=sort_by,
                status_sort=status_sort,
                monitoring_by_sid=monitoring_by_sid,
                unread_by_sid=unread_by_sid,
                pending_input_by_sid=pending_input_by_sid,
            ))
            if search and search.strip()
            else (lambda session: _session_list_sort_key(
                session,
                folder_view,
                sort_by,
                status_sort=status_sort,
                monitoring_by_sid=monitoring_by_sid,
                unread_by_sid=unread_by_sid,
                pending_input_by_sid=pending_input_by_sid,
            ))
        ),
        reverse=True,
    )
    return out


def _filter_sort_page_for_list(
    sessions: list[dict],
    *,
    offset: int,
    limit: int,
    project_path: str | None,
    search: str | None,
    show_archived: bool,
    file_edit_mode: bool | None,
    folder_ids: set[str],
    folder_view: bool,
    tag_ids: set[str],
    provider_ids: set[str],
    model_ids: set[str],
    modes: set[str],
    sources: set[str],
    content_scores: dict[str, int],
    sort_by: str,
    status_sort: bool = False,
    state_snapshot: tuple[set[str], dict[str, str], dict[str, int], dict[str, int]] | None = None,
) -> tuple[list[dict], int]:
    import heapq

    monitoring_by_sid: dict[str, str] = {}
    unread_by_sid: dict[str, int] = {}
    pending_input_by_sid: dict[str, int] = {}
    if status_sort:
        if state_snapshot is None:
            state_snapshot = _sidebar_state_snapshot()
        _, monitoring_by_sid, unread_by_sid, pending_input_by_sid = state_snapshot

    def _sort_key(session: dict) -> tuple:
        if search and search.strip():
            return _session_filtered_sort_key(
                session,
                folder_view=folder_view,
                search=search,
                content_scores=content_scores,
                sort_by=sort_by,
                status_sort=status_sort,
                monitoring_by_sid=monitoring_by_sid,
                unread_by_sid=unread_by_sid,
                pending_input_by_sid=pending_input_by_sid,
            )
        return _session_list_sort_key(
            session,
            folder_view,
            sort_by,
            status_sort=status_sort,
            monitoring_by_sid=monitoring_by_sid,
            unread_by_sid=unread_by_sid,
            pending_input_by_sid=pending_input_by_sid,
        )

    total = 0
    end = offset + limit
    selected: list[tuple[tuple, int, dict]] = []
    for idx, session in enumerate(sessions):
        if not _session_matches_list_filters(
            session,
            project_path=project_path,
            search=search,
            show_archived=show_archived,
            file_edit_mode=file_edit_mode,
            folder_ids=folder_ids,
            tag_ids=tag_ids,
            provider_ids=provider_ids,
            model_ids=model_ids,
            modes=modes,
            sources=sources,
            content_scores=content_scores,
        ):
            continue
        total += 1
        item = (_sort_key(session), -idx, session)
        if 0 < end <= len(selected):
            if (item[0], item[1]) > (selected[0][0], selected[0][1]):
                heapq.heapreplace(selected, item)
        else:
            heapq.heappush(selected, item)

    selected.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [session for _, __, session in selected[offset:end]], total


def _filter_sessions_for_list_preserving_order(
    sessions: list[dict],
    *,
    project_path: str | None,
    search: str | None,
    show_archived: bool,
    file_edit_mode: bool | None,
    folder_ids: set[str],
    tag_ids: set[str],
    provider_ids: set[str],
    model_ids: set[str],
    modes: set[str],
    sources: set[str],
    content_scores: dict[str, int],
) -> list[dict]:
    return [
        session for session in sessions
        if _session_matches_list_filters(
            session,
            project_path=project_path,
            search=search,
            show_archived=show_archived,
            file_edit_mode=file_edit_mode,
            folder_ids=folder_ids,
            tag_ids=tag_ids,
            provider_ids=provider_ids,
            model_ids=model_ids,
            modes=modes,
            sources=sources,
            content_scores=content_scores,
        )
    ]


def _filter_page_for_list_preserving_order(
    sessions: list[dict],
    *,
    offset: int,
    limit: int,
    project_path: str | None,
    search: str | None,
    show_archived: bool,
    file_edit_mode: bool | None,
    folder_ids: set[str],
    tag_ids: set[str],
    provider_ids: set[str],
    model_ids: set[str],
    modes: set[str],
    sources: set[str],
    content_scores: dict[str, int],
) -> tuple[list[dict], int]:
    page: list[dict] = []
    total = 0
    end = offset + limit
    for session in sessions:
        if not _session_matches_list_filters(
            session,
            project_path=project_path,
            search=search,
            show_archived=show_archived,
            file_edit_mode=file_edit_mode,
            folder_ids=folder_ids,
            tag_ids=tag_ids,
            provider_ids=provider_ids,
            model_ids=model_ids,
            modes=modes,
            sources=sources,
            content_scores=content_scores,
        ):
            continue
        if offset <= total < end:
            page.append(session)
        total += 1
    return page, total


def _can_preserve_summary_order(
    *,
    search_query: str,
    appended_virtual_sessions: bool,
    folder_view: bool,
    sort_by: str,
    status_sort: bool,
) -> bool:
    return (
        not search_query
        and not appended_virtual_sessions
        and not folder_view
        and sort_by in {"updated_at", "last_user_prompt_at", "last_opened_at"}
        and not status_sort
    )


def _can_page_local_summary_order(
    *,
    search_query: str,
    folder_view: bool,
    sort_by: str,
    status_sort: bool,
) -> bool:
    return (
        not search_query
        and not folder_view
        and sort_by in {"updated_at", "last_user_prompt_at", "last_opened_at"}
        and not status_sort
    )


def _can_page_default_updated_at_with_virtual(
    *,
    search_query: str,
    project_path: str | None,
    show_archived: bool,
    file_edit_mode: bool | None,
    folder_ids: set[str],
    folder_view: bool,
    tag_ids: set[str],
    provider_ids: set[str],
    model_ids: set[str],
    modes: set[str],
    sources: set[str],
    sort_by: str,
    status_sort: bool,
) -> bool:
    return (
        not search_query
        and project_path is None
        and not show_archived
        and file_edit_mode is None
        and not folder_ids
        and not folder_view
        and not tag_ids
        and not provider_ids
        and not model_ids
        and not modes
        and not sources
        and sort_by == "updated_at"
        and not status_sort
    )


def _merge_updated_at_page(
    local_sessions: list[dict],
    secondary_sessions: list[dict],
    *,
    offset: int,
    limit: int,
) -> tuple[list[dict], int]:
    page: list[dict] = []
    total = 0
    local_index = 0
    virtual_index = 0
    end = offset + limit
    while local_index < len(local_sessions) or virtual_index < len(secondary_sessions):
        if local_index >= len(local_sessions):
            session = secondary_sessions[virtual_index]
            virtual_index += 1
        elif virtual_index >= len(secondary_sessions):
            session = local_sessions[local_index]
            local_index += 1
        else:
            local_session = local_sessions[local_index]
            virtual_session = secondary_sessions[virtual_index]
            if (
                session_store.timestamp_sort_value(local_session.get("updated_at"))
                >= session_store.timestamp_sort_value(virtual_session.get("updated_at"))
            ):
                session = local_session
                local_index += 1
            else:
                session = virtual_session
                virtual_index += 1
        if session.get("archived"):
            continue
        if offset <= total < end:
            page.append(session)
        total += 1
    return page, total


def _session_filters_may_include_virtual(
    *,
    file_edit_mode: bool | None,
    folder_ids: set[str],
    tag_ids: set[str],
    modes: set[str],
    sources: set[str],
) -> bool:
    if file_edit_mode is True:
        return False
    if folder_ids or tag_ids:
        return False
    if modes and "virtual" not in modes:
        return False
    if sources and not ({"extension", "system"} & sources):
        return False
    return True


def _can_page_local_search_scores(
    *,
    project_path: str | None,
    show_archived: bool,
    file_edit_mode: bool | None,
    folder_ids: set[str],
    folder_view: bool,
    tag_ids: set[str],
    provider_ids: set[str],
    model_ids: set[str],
    modes: set[str],
    sources: set[str],
    sort_by: str,
    status_sort: bool,
    connected: tuple[str, ...],
) -> bool:
    return (
        project_path is None
        and not show_archived
        and file_edit_mode is None
        and not folder_ids
        and not tag_ids
        and not provider_ids
        and not model_ids
        and not modes
        and not sources
        and sort_by in {"updated_at", "last_user_prompt_at", "last_opened_at"}
        and not status_sort
        and not connected
    )


def _build_local_search_page_for_sidebar(
    *,
    offset: int,
    limit: int,
    search_query: str,
    search_fields: str | None,
    sort_by: str,
    folder_view: bool,
) -> tuple[list[dict], int, dict[str, int]]:
    selected_search_fields = _split_session_search_fields(search_fields)
    content_max_wait_seconds = (
        _SESSION_LIST_CONTENT_SEARCH_MAX_WAIT_SECONDS
        if session_store.SEARCH_FIELD_CONTENT in selected_search_fields
        else None
    )
    with perf.timed("sessions.list.search_score_page"):
        score_page, total = session_store.grep_session_score_page(
            search_query,
            selected_search_fields,
            offset=offset,
            limit=limit,
            sort_by=sort_by,
            folder_view=folder_view,
            content_limit=_session_search_candidate_limit(offset, limit),
            content_max_wait_seconds=content_max_wait_seconds,
        )
    scores = dict(score_page)
    page_source = _local_session_summaries_by_ids_for_sidebar(
        [sid for sid, _score in score_page]
    )
    return page_source, total, scores


def _build_local_sessions_page_for_list(
    *,
    offset: int,
    limit: int,
    project_path: str | None,
    search: str | None,
    show_archived: bool,
    file_edit_mode: bool | None,
    folder_ids: set[str],
    folder_view: bool,
    tag_ids: set[str],
    provider_ids: set[str],
    model_ids: set[str],
    modes: set[str],
    sources: set[str],
    search_fields: str | None,
    sort_by: str,
    status_sort: bool = False,
) -> tuple[list[dict], int]:
    content_scores: dict[str, int] = {}
    state_snapshot = _sidebar_state_snapshot() if status_sort else None
    search_query = (search or "").strip()
    appended_virtual_sessions = False
    default_virtual_page = _can_page_default_updated_at_with_virtual(
        search_query=search_query,
        project_path=project_path,
        show_archived=show_archived,
        file_edit_mode=file_edit_mode,
        folder_ids=folder_ids,
        folder_view=folder_view,
        tag_ids=tag_ids,
        provider_ids=provider_ids,
        model_ids=model_ids,
        modes=modes,
        sources=sources,
        sort_by=sort_by,
        status_sort=status_sort,
    )
    can_page_local_order = _can_page_local_summary_order(
        search_query=search_query,
        folder_view=folder_view,
        sort_by=sort_by,
        status_sort=status_sort,
    )
    may_include_virtual = _session_filters_may_include_virtual(
        file_edit_mode=file_edit_mode,
        folder_ids=folder_ids,
        tag_ids=tag_ids,
        modes=modes,
        sources=sources,
    )
    if can_page_local_order and (not may_include_virtual or sort_by == "last_user_prompt_at"):
        with perf.timed("sessions.list.local_order_page"):
            page_source, local_total = _local_session_page_for_sidebar_preserving_order(
                sort_by=sort_by,
                offset=offset,
                limit=limit,
                project_path=project_path,
                search=search,
                show_archived=show_archived,
                file_edit_mode=file_edit_mode,
                folder_ids=folder_ids,
                tag_ids=tag_ids,
                provider_ids=provider_ids,
                model_ids=model_ids,
                modes=modes,
                sources=sources,
                content_scores=content_scores,
            )
        virtual_total = 0
        if may_include_virtual and sort_by == "last_user_prompt_at":
            with perf.timed("sessions.list.virtual_count"):
                cached_virtual = virtual_session_store.list_recent_cached(
                    1,
                    exclude_id=session_search.ASK_SINGLETON_ID,
                )
                if cached_virtual is None:
                    _virtual_page, virtual_total = virtual_session_store.list_recent(
                        1,
                        exclude_id=session_search.ASK_SINGLETON_ID,
                    )
                else:
                    _virtual_page, virtual_total = cached_virtual
        if len(page_source) >= limit or not may_include_virtual:
            total = local_total + virtual_total
            with perf.timed("sessions.list.page_decorate"):
                page = _decorate_local_sidebar_sessions(page_source, state_snapshot)
            return page, total
    if search_query:
        if _can_page_local_search_scores(
            project_path=project_path,
            show_archived=show_archived,
            file_edit_mode=file_edit_mode,
            folder_ids=folder_ids,
            folder_view=folder_view,
            tag_ids=tag_ids,
            provider_ids=provider_ids,
            model_ids=model_ids,
            modes=modes,
            sources=sources,
            sort_by=sort_by,
            status_sort=status_sort,
            connected=(),
        ):
            page_source, total, content_scores = _build_local_search_page_for_sidebar(
                offset=offset,
                limit=limit,
                search_query=search_query,
                search_fields=search_fields,
                sort_by=sort_by,
                folder_view=folder_view,
            )
            with perf.timed("sessions.list.page_decorate"):
                page = _decorate_local_sidebar_sessions(page_source, state_snapshot)
            if content_scores:
                page = [
                    {**session, "search_score": content_scores.get(str(session.get("id") or ""), 0)}
                    for session in page
                ]
            return page, total
        selected_search_fields = _split_session_search_fields(search_fields)
        content_max_wait_seconds = (
            _SESSION_LIST_CONTENT_SEARCH_MAX_WAIT_SECONDS
            if session_store.SEARCH_FIELD_CONTENT in selected_search_fields
            else None
        )
        with perf.timed("sessions.list.search_scores"):
            content_scores = session_store.grep_session_scores(
                search_query,
                selected_search_fields,
                content_limit=_session_search_candidate_limit(offset, limit),
                content_max_wait_seconds=content_max_wait_seconds,
            )
        with perf.timed("sessions.list.search_local"):
            out = _local_session_summaries_by_ids_for_sidebar(list(content_scores))
    else:
        if may_include_virtual:
            with perf.timed("sessions.list.virtual"):
                if default_virtual_page:
                    with perf.timed("sessions.list.local_order_page"):
                        out, local_total = _local_session_page_for_sidebar_preserving_order(
                            sort_by=sort_by,
                            offset=0,
                            limit=max(offset + limit, 1),
                            project_path=project_path,
                            search=search,
                            show_archived=show_archived,
                            file_edit_mode=file_edit_mode,
                            folder_ids=folder_ids,
                            tag_ids=tag_ids,
                            provider_ids=provider_ids,
                            model_ids=model_ids,
                            modes=modes,
                            sources=sources,
                            content_scores=content_scores,
                        )
                    virtual_limit = max(offset + limit, 1)
                    cached_virtual = virtual_session_store.list_recent_cached(
                        virtual_limit,
                        exclude_id=session_search.ASK_SINGLETON_ID,
                    )
                    if cached_virtual is None:
                        virtual_sessions, virtual_total = virtual_session_store.list_recent(
                            virtual_limit,
                            exclude_id=session_search.ASK_SINGLETON_ID,
                        )
                    else:
                        virtual_sessions, virtual_total = cached_virtual
                else:
                    with perf.timed("sessions.list.local"):
                        out = _local_session_summaries_for_sidebar()
                    virtual_sessions = virtual_session_store.list_all()
                    virtual_total = len([
                        session for session in virtual_sessions
                        if session.get("id") != session_search.ASK_SINGLETON_ID
                    ])
            virtual_sidebar_sessions = [
                session
                for session in virtual_sessions
                if session.get("id") != session_search.ASK_SINGLETON_ID
            ]
            if default_virtual_page:
                with perf.timed("sessions.list.default_virtual_merge"):
                    page_source, _merged_count = _merge_updated_at_page(
                        out,
                        virtual_sidebar_sessions,
                        offset=offset,
                        limit=limit,
                    )
                total = local_total + virtual_total
                with perf.timed("sessions.list.page_decorate"):
                    page = _decorate_local_sidebar_sessions(page_source, state_snapshot)
                return page, total
            if virtual_sidebar_sessions:
                out.extend(virtual_sidebar_sessions)
                appended_virtual_sessions = True
        else:
            with perf.timed("sessions.list.local"):
                out = _local_session_summaries_for_sidebar()
            perf.record("sessions.list.virtual.skipped", 1.0)
    with perf.timed("sessions.list.filter_sort"):
        if search_query:
            page_source, total = _filter_sort_page_for_list(
                out,
                offset=offset,
                limit=limit,
                project_path=project_path,
                search=search,
                show_archived=show_archived,
                file_edit_mode=file_edit_mode,
                folder_ids=folder_ids,
                folder_view=folder_view,
                tag_ids=tag_ids,
                provider_ids=provider_ids,
                model_ids=model_ids,
                modes=modes,
                sources=sources,
                content_scores=content_scores,
                sort_by=sort_by,
                status_sort=status_sort,
                state_snapshot=state_snapshot,
            )
            with perf.timed("sessions.list.page_decorate"):
                page = _decorate_local_sidebar_sessions(page_source, state_snapshot)
            if content_scores:
                page = [
                    {**session, "search_score": content_scores.get(str(session.get("id") or ""), 0)}
                    for session in page
                ]
            return page, total
        if _can_preserve_summary_order(
            search_query=search_query,
            appended_virtual_sessions=appended_virtual_sessions,
            folder_view=folder_view,
            sort_by=sort_by,
            status_sort=status_sort,
        ):
            page_source, total = _filter_page_for_list_preserving_order(
                out,
                offset=offset,
                limit=limit,
                project_path=project_path,
                search=search,
                show_archived=show_archived,
                file_edit_mode=file_edit_mode,
                folder_ids=folder_ids,
                tag_ids=tag_ids,
                provider_ids=provider_ids,
                model_ids=model_ids,
                modes=modes,
                sources=sources,
                content_scores=content_scores,
            )
            with perf.timed("sessions.list.page_decorate"):
                page = _decorate_local_sidebar_sessions(page_source, state_snapshot)
            return page, total
        else:
            out = _filter_sort_sessions_for_list(
                out,
                project_path=project_path,
                search=search,
                show_archived=show_archived,
                file_edit_mode=file_edit_mode,
                folder_ids=folder_ids,
                folder_view=folder_view,
                tag_ids=tag_ids,
                provider_ids=provider_ids,
                model_ids=model_ids,
                modes=modes,
                sources=sources,
                content_scores=content_scores,
                sort_by=sort_by,
                status_sort=status_sort,
                state_snapshot=state_snapshot,
            )
    total = len(out)
    end = offset + limit
    with perf.timed("sessions.list.page_decorate"):
        page = _decorate_local_sidebar_sessions(out[offset:end], state_snapshot)
    if content_scores:
        page = [
            {**session, "search_score": content_scores.get(str(session.get("id") or ""), 0)}
            for session in page
        ]
    return page, total


async def _sidebar_search_scores(
    search_query: str,
    search_fields: str | None,
    *,
    content_limit: int,
) -> dict[str, int]:
    selected_search_fields = _split_session_search_fields(search_fields)
    content_max_wait_seconds = (
        _SESSION_LIST_CONTENT_SEARCH_MAX_WAIT_SECONDS
        if session_store.SEARCH_FIELD_CONTENT in selected_search_fields
        else None
    )
    return await asyncio.to_thread(
        session_store.grep_session_scores,
        search_query,
        selected_search_fields,
        content_limit=content_limit,
        content_max_wait_seconds=content_max_wait_seconds,
    )


def _session_search_candidate_limit(offset: int, limit: int) -> int:
    return max(offset + limit, _SESSION_LIST_SEARCH_MIN_CANDIDATES)


@app.get("/api/sessions")
async def get_sessions(
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    project_path: str | None = Query(None),
    search: str | None = Query(None),
    show_archived: bool = Query(False),
    file_edit_mode: bool | None = Query(None),
    folder_ids: str | None = Query(None),
    folder_view: bool | None = Query(None),
    tag_ids: str | None = Query(None),
    provider_ids: str | None = Query(None),
    model_ids: str | None = Query(None),
    modes: str | None = Query(None),
    sources: str | None = Query(None),
    search_fields: str | None = Query(None),
    sort_by: str | None = Query(None),
):
    search_query = (search or "").strip()
    connected_version = 0
    connected: tuple[str, ...] = ()
    if not search_query:
        with perf.timed("sessions.list.connected_nodes"):
            try:
                import node_store as _ns
                connected_version, connected = _ns.connected_worker_node_ids_snapshot()
            except Exception:
                logger.debug("get_sessions: connected node snapshot failed", exc_info=True)
    if connected:
        with perf.timed("sessions.list.nodes_ready"):
            if not _machine_nodes_enabled_cached():
                connected = ()
    with perf.timed("sessions.list.filters"):
        (
            default_folder_view,
            default_sort_by,
            effective_status_sort,
        ) = _session_list_user_prefs()
        effective_folder_view = (
            folder_view if folder_view is not None else default_folder_view
        )
        effective_sort_by = (
            sort_by if sort_by in user_prefs.SESSION_SORT_VALUES
            else default_sort_by
        )
        effective_search_fields = _split_session_search_fields(search_fields)
        filters = {
            "offset": offset,
            "limit": limit,
            "project_path": project_path,
            "search": search,
            "show_archived": show_archived,
            "file_edit_mode": file_edit_mode,
            "folder_ids": _split_session_filter(folder_ids),
            "folder_view": effective_folder_view,
            "tag_ids": _split_session_filter(tag_ids),
            "provider_ids": _split_session_filter(provider_ids),
            "model_ids": _split_session_filter(model_ids),
            "modes": _split_session_filter(modes),
            "sources": _split_session_filter(sources),
            "search_fields": search_fields,
            "sort_by": effective_sort_by,
            "status_sort": effective_status_sort,
        }
    cache_key = (
        offset,
        limit,
        project_path,
        search_query,
        show_archived,
        file_edit_mode,
        tuple(sorted(filters["folder_ids"])),
        effective_folder_view,
        tuple(sorted(filters["tag_ids"])),
        tuple(sorted(filters["provider_ids"])),
        tuple(sorted(filters["model_ids"])),
        tuple(sorted(filters["modes"])),
        tuple(sorted(filters["sources"])),
        tuple(sorted(effective_search_fields)),
        effective_sort_by,
        effective_status_sort,
        connected_version,
        connected,
        _remote_sessions_cache_version_snapshot() if connected else 0,
        _sessions_list_cache_version(search_query, effective_search_fields),
    )
    cached_response = _sessions_list_cache_get(cache_key)
    if cached_response is not None:
        perf.record("sessions.list.response_cache.hit", 1.0)
        return cached_response
    perf.record("sessions.list.response_cache.miss", 1.0)
    cache_response = _sessions_list_content_search_ready(
        search_query,
        effective_search_fields,
        offset=offset,
        limit=limit,
    )
    if search_query and _can_page_local_search_scores(
        project_path=project_path,
        show_archived=show_archived,
        file_edit_mode=file_edit_mode,
        folder_ids=filters["folder_ids"],
        folder_view=effective_folder_view,
        tag_ids=filters["tag_ids"],
        provider_ids=filters["provider_ids"],
        model_ids=filters["model_ids"],
        modes=filters["modes"],
        sources=filters["sources"],
        sort_by=effective_sort_by,
        status_sort=effective_status_sort,
        connected=(),
    ):
        page_source, total, content_scores = await _run_session_list_hot_path(
            "sessions.list.search_local_page.worker",
            _build_local_search_page_for_sidebar,
            offset=offset,
            limit=limit,
            search_query=search_query,
            search_fields=search_fields,
            sort_by=effective_sort_by,
            folder_view=effective_folder_view,
        )
        state_snapshot = None
        with perf.timed("sessions.list.page_decorate"):
            page = await _run_session_list_hot_path(
                "sessions.list.page_decorate.worker",
                _decorate_local_sidebar_sessions,
                page_source,
                state_snapshot,
            )
        if content_scores:
            page = [
                {**session, "search_score": content_scores.get(str(session.get("id") or ""), 0)}
                for session in page
            ]
        _schedule_session_event_meta_warm(page)
        response_payload = _sessions_snapshot_payload(
            {
                "sessions": page,
                "offset": offset,
                "limit": limit,
                "total": total,
                "has_more": offset + limit < total,
                "sort_by": effective_sort_by,
                "status_sort": effective_status_sort,
            }
        )
        return _sessions_list_response_maybe_cache(
            cache_key,
            response_payload,
            cache_response=cache_response and response_payload.get("snapshot_complete") is True,
        )
    if not connected:
        page, total = await _run_session_list_hot_path(
            "sessions.list.local_page_thread",
            _build_local_sessions_page_for_list,
            **filters,
        )
        _schedule_session_event_meta_warm(page)
        response_payload = _sessions_snapshot_payload({
            "sessions": page,
            "offset": offset,
            "limit": limit,
            "total": total,
            "has_more": offset + limit < total,
            "sort_by": effective_sort_by,
            "status_sort": effective_status_sort,
        })
        return _sessions_list_response_maybe_cache(
            cache_key,
            response_payload,
            cache_response=cache_response and response_payload.get("snapshot_complete") is True,
        )

    content_scores: dict[str, int] = {}
    appended_virtual_sessions = False
    appended_remote_sessions = False
    handled_virtual_sessions = False
    handled_remote_sessions = False
    deferred_sidebar_projection = False
    local_total: int | None = None
    local_page_candidates: list[dict] | None = None
    projected_first_page_sessions: list[dict] = []
    can_page_remote_local_order = _can_page_local_summary_order(
        search_query=search_query,
        folder_view=effective_folder_view,
        sort_by=effective_sort_by,
        status_sort=effective_status_sort,
    )
    may_include_virtual = _session_filters_may_include_virtual(
        file_edit_mode=file_edit_mode,
        folder_ids=filters["folder_ids"],
        tag_ids=filters["tag_ids"],
        modes=filters["modes"],
        sources=filters["sources"],
    )
    default_projected_first_page = _can_page_default_updated_at_with_virtual(
        search_query=search_query,
        project_path=project_path,
        show_archived=show_archived,
        file_edit_mode=file_edit_mode,
        folder_ids=filters["folder_ids"],
        folder_view=effective_folder_view,
        tag_ids=filters["tag_ids"],
        provider_ids=filters["provider_ids"],
        model_ids=filters["model_ids"],
        modes=filters["modes"],
        sources=filters["sources"],
        sort_by=effective_sort_by,
        status_sort=effective_status_sort,
    )
    if search_query:
        with perf.timed("sessions.list.search_scores"):
            content_scores = await _sidebar_search_scores(
                search_query,
                search_fields,
                content_limit=_session_search_candidate_limit(offset, limit),
            )
        with perf.timed("sessions.list.search_local"):
            out = await asyncio.to_thread(
                _local_session_summaries_by_ids_for_sidebar,
                list(content_scores),
            )
    else:
        if can_page_remote_local_order:
            with perf.timed("sessions.list.remote.local_order_candidates"):
                out, local_total = await _run_session_list_hot_path(
                    "sessions.list.remote.local_order_candidates.worker",
                    _local_session_page_for_sidebar_preserving_order,
                    sort_by=effective_sort_by,
                    offset=0,
                    limit=max(offset + limit, 1),
                    project_path=project_path,
                    search=search,
                    show_archived=show_archived,
                    file_edit_mode=file_edit_mode,
                    folder_ids=filters["folder_ids"],
                    tag_ids=filters["tag_ids"],
                    provider_ids=filters["provider_ids"],
                    model_ids=filters["model_ids"],
                    modes=filters["modes"],
                    sources=filters["sources"],
                    content_scores=content_scores,
                )
                local_page_candidates = out
        else:
            with perf.timed("sessions.list.local"):
                out = await _run_session_list_hot_path(
                    "sessions.list.local.worker",
                    _local_session_summaries_for_sidebar,
                )
        if (
            can_page_remote_local_order
            and local_total is not None
            and len(out) >= max(offset + limit, 1)
        ):
            if may_include_virtual:
                with perf.timed("sessions.list.virtual.cached_first_page"):
                    cached_virtual = await asyncio.to_thread(
                        virtual_session_store.list_recent_cached,
                        max(offset + limit, 1),
                        exclude_id=session_search.ASK_SINGLETON_ID,
                    )
                handled_virtual_sessions = True
                if cached_virtual is None:
                    deferred_sidebar_projection = True
                    _schedule_virtual_sessions_recent_refresh(max(offset + limit, 1))
                else:
                    virtual_sessions, virtual_total = cached_virtual
                    virtual_sidebar_sessions = [
                        session
                        for session in virtual_sessions
                        if session.get("id") != session_search.ASK_SINGLETON_ID
                    ]
                    if virtual_sidebar_sessions:
                        out.extend(virtual_sidebar_sessions)
                        projected_first_page_sessions.extend(virtual_sidebar_sessions)
                        appended_virtual_sessions = True
                    local_total += virtual_total
            with perf.timed("sessions.list.remote.cached_first_page"):
                for nid in connected:
                    cached_remote = _remote_sessions_for_sidebar_cached(
                        nid,
                        limit=max(offset + limit, 1),
                    )
                    if cached_remote is None:
                        deferred_sidebar_projection = True
                        continue
                    remote, remote_total = cached_remote
                    for rs in remote:
                        rs["node_id"] = nid
                        rs.setdefault("is_running", False)
                        rs.setdefault("unread_count", 0)
                        rs.setdefault("monitoring_state", "idle")
                        out.append(rs)
                        projected_first_page_sessions.append(rs)
                        appended_remote_sessions = True
                    local_total += remote_total
            handled_remote_sessions = True
            if deferred_sidebar_projection and not appended_virtual_sessions and not appended_remote_sessions:
                end = offset + limit
                with perf.timed("sessions.list.page_decorate"):
                    page = await _run_session_list_hot_path(
                        "sessions.list.page_decorate.worker",
                        _decorate_local_sidebar_sessions,
                        out[offset:end],
                        None,
                    )
                _schedule_session_event_meta_warm(page)
                return _sessions_list_response(
                    json.dumps(
                        {
                            "sessions": page,
                            "offset": offset,
                            "limit": limit,
                            "total": local_total,
                            "has_more": end < local_total,
                            "sort_by": effective_sort_by,
                            "status_sort": effective_status_sort,
                        },
                        ensure_ascii=False,
                        allow_nan=False,
                        separators=(",", ":"),
                    ).encode("utf-8")
                )
        if may_include_virtual:
            if handled_virtual_sessions:
                pass
            else:
                with perf.timed("sessions.list.virtual"):
                    if can_page_remote_local_order:
                        virtual_sessions, virtual_total = await asyncio.to_thread(
                            virtual_session_store.list_recent,
                            max(offset + limit, 1),
                            exclude_id=session_search.ASK_SINGLETON_ID,
                        )
                    else:
                        virtual_sessions = await asyncio.to_thread(virtual_session_store.list_all)
                        virtual_total = len([
                            session for session in virtual_sessions
                            if session.get("id") != session_search.ASK_SINGLETON_ID
                        ])
                virtual_sidebar_sessions = [
                    session
                    for session in virtual_sessions
                    if session.get("id") != session_search.ASK_SINGLETON_ID
                ]
                if virtual_sidebar_sessions:
                    out.extend(virtual_sidebar_sessions)
                    appended_virtual_sessions = True
                if local_total is not None:
                    local_total += virtual_total
        else:
            perf.record("sessions.list.virtual.skipped", 1.0)

    if not handled_remote_sessions:
        try:
            with perf.timed("sessions.list.remote"):
                remote_results = await asyncio.gather(
                    *(
                        asyncio.wait_for(
                            _remote_sessions_for_sidebar(nid),
                            timeout=REMOTE_SESSION_MERGE_TIMEOUT_SECONDS + 0.05,
                        )
                        for nid in connected
                    ),
                    return_exceptions=True,
                )
            for nid, result in zip(connected, remote_results):
                if isinstance(result, Exception):
                    logger.warning("get_sessions: remote node merge timed out")
                    continue
                remote = result
                for rs in remote:
                    rs["node_id"] = nid
                    rs.setdefault("is_running", False)
                    rs.setdefault("unread_count", 0)
                    rs.setdefault("monitoring_state", "idle")
                    out.append(rs)
                    projected_first_page_sessions.append(rs)
                    appended_remote_sessions = True
                if local_total is not None:
                    local_total += len(remote)
        except Exception:
            logger.debug("get_sessions: node merge failed", exc_info=True)

    if (
        default_projected_first_page
        and local_page_candidates is not None
        and projected_first_page_sessions
        and local_total is not None
    ):
        end = offset + limit
        with perf.timed("sessions.list.projected_first_page_merge"):
            projected_first_page_sessions.sort(
                key=lambda session: session_store.timestamp_sort_value(session.get("updated_at")),
                reverse=True,
            )
            page_source, _merged_count = _merge_updated_at_page(
                local_page_candidates,
                projected_first_page_sessions,
                offset=offset,
                limit=limit,
            )
        with perf.timed("sessions.list.page_decorate"):
            page = await _run_session_list_hot_path(
                "sessions.list.page_decorate.worker",
                _decorate_local_sidebar_sessions,
                page_source,
                None,
            )
        _schedule_session_event_meta_warm(page)
        response_payload = _sessions_snapshot_payload({
            "sessions": page,
            "offset": offset,
            "limit": limit,
            "total": local_total,
            "has_more": end < local_total,
            "sort_by": effective_sort_by,
            "status_sort": effective_status_sort,
        })
        return _sessions_list_response_maybe_cache(
            cache_key,
            response_payload,
            cache_response=cache_response and response_payload.get("snapshot_complete") is True,
        )

    if (
        can_page_remote_local_order
        and not appended_virtual_sessions
        and not appended_remote_sessions
        and local_total is not None
    ):
        end = offset + limit
        with perf.timed("sessions.list.page_decorate"):
            page = await _run_session_list_hot_path(
                "sessions.list.page_decorate.worker",
                _decorate_local_sidebar_sessions,
                out[offset:end],
                None,
            )
        _schedule_session_event_meta_warm(page)
        response_payload = _sessions_snapshot_payload({
            "sessions": page,
            "offset": offset,
            "limit": limit,
            "total": local_total,
            "has_more": end < local_total,
            "sort_by": effective_sort_by,
            "status_sort": effective_status_sort,
        })
        return _sessions_list_response_maybe_cache(
            cache_key,
            response_payload,
            cache_response=cache_response and response_payload.get("snapshot_complete") is True,
        )

    state_snapshot = (
        await asyncio.to_thread(_sidebar_state_snapshot)
        if effective_status_sort
        else None
    )
    page_source: list[dict] | None = None
    filtered_total: int | None = None
    with perf.timed("sessions.list.filter_sort"):
        if can_page_remote_local_order or search_query:
            page_source, filtered_total = await asyncio.to_thread(
                _filter_sort_page_for_list,
                out,
                offset=offset,
                limit=limit,
                project_path=project_path,
                search=search,
                show_archived=show_archived,
                file_edit_mode=file_edit_mode,
                folder_ids=filters["folder_ids"],
                folder_view=effective_folder_view,
                tag_ids=filters["tag_ids"],
                provider_ids=filters["provider_ids"],
                model_ids=filters["model_ids"],
                modes=filters["modes"],
                sources=filters["sources"],
                content_scores=content_scores,
                sort_by=effective_sort_by,
                status_sort=effective_status_sort,
                state_snapshot=state_snapshot,
            )
        elif _can_preserve_summary_order(
            search_query=search_query,
            appended_virtual_sessions=appended_virtual_sessions,
            folder_view=effective_folder_view,
            sort_by=effective_sort_by,
            status_sort=effective_status_sort,
        ):
            out = await asyncio.to_thread(
                _filter_sessions_for_list_preserving_order,
                out,
                project_path=project_path,
                search=search,
                show_archived=show_archived,
                file_edit_mode=file_edit_mode,
                folder_ids=filters["folder_ids"],
                tag_ids=filters["tag_ids"],
                provider_ids=filters["provider_ids"],
                model_ids=filters["model_ids"],
                modes=filters["modes"],
                sources=filters["sources"],
                content_scores=content_scores,
            )
        else:
            out = await asyncio.to_thread(
                _filter_sort_sessions_for_list,
                out,
                project_path=project_path,
                search=search,
                show_archived=show_archived,
                file_edit_mode=file_edit_mode,
                folder_ids=filters["folder_ids"],
                folder_view=effective_folder_view,
                tag_ids=filters["tag_ids"],
                provider_ids=filters["provider_ids"],
                model_ids=filters["model_ids"],
                modes=filters["modes"],
                sources=filters["sources"],
                content_scores=content_scores,
                sort_by=effective_sort_by,
                status_sort=effective_status_sort,
                state_snapshot=state_snapshot,
            )
    total = (
        local_total
        if local_total is not None
        else (filtered_total if filtered_total is not None else len(out))
    )
    end = offset + limit
    if page_source is None:
        page_source = out[offset:end]
    with perf.timed("sessions.list.page_decorate"):
        page = await _run_session_list_hot_path(
            "sessions.list.page_decorate.worker",
            _decorate_local_sidebar_sessions,
            page_source,
            state_snapshot,
        )
    if content_scores:
        page = [
            {**session, "search_score": content_scores.get(str(session.get("id") or ""), 0)}
            for session in page
        ]
    _schedule_session_event_meta_warm(page)
    response_payload = _sessions_snapshot_payload({
        "sessions": page,
        "offset": offset,
        "limit": limit,
        "total": total,
        "has_more": end < total,
        "sort_by": effective_sort_by,
        "status_sort": effective_status_sort,
    })
    if deferred_sidebar_projection:
        return _sessions_list_response(
            json.dumps(
                response_payload,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
    return _sessions_list_response_maybe_cache(
        cache_key,
        response_payload,
        cache_response=cache_response and response_payload.get("snapshot_complete") is True,
    )


@app.post("/api/sessions/search-content")
async def search_session_content(body: dict = Body(default={})):
    """Grep-based session content search.

    Scans session JSON files for substring matches, counts occurrences
    per session, and returns results sorted by score descending.
    Used by the sidebar filter for "search in session content".

    Body: `{"query": "...", "limit"?: int, "fields"?: ["content"|"title"|"first_prompt"]}`.
    Returns: `{"results": [{"session_id": "...", "score": N}, ...]}`.
    """
    query = (body.get("query") or "").strip()
    if not query:
        return {"results": []}
    raw_fields = body.get("fields")
    if raw_fields is None:
        fields = set(session_store.DEFAULT_SEARCH_FIELDS)
    elif isinstance(raw_fields, list):
        fields = {
            field
            for field in raw_fields
            if isinstance(field, str) and field in session_store.SEARCH_FIELDS
        }
    else:
        raise HTTPException(status_code=400, detail="fields must be a list")
    limit = body.get("limit")
    if not isinstance(limit, int) or limit <= 0:
        limit = 50
    with perf.timed("sessions.search_content.query"):
        results = await asyncio.to_thread(session_store.grep_sessions, query, limit, fields)
    return {"results": results}


def _session_organization_snapshot_with_facets(project_id: str | None) -> dict:
    """Org snapshot plus the model filter universe for the project.

    Folder/provider/mode/source universes are known client-side (org
    folders, configured providers, static enums); models are open-ended,
    so the backend supplies the distinct models across ALL the project's
    sessions regardless of the active filter, keeping the filter options
    stable instead of collapsing to whatever the current page contains.
    """
    org_token = session_organization_store.version_token()
    cache_key = (project_id, session_store.summary_version(), org_token)
    cached = _session_org_facets_cache.get(cache_key)
    if cached is not None:
        return cached
    snapshot = session_organization_store.snapshot(project_id)
    models: set[str] = set()
    for session in _local_session_summaries_for_sidebar():
        if not session_matches_project(session, project_id):
            continue
        model = (session.get("model") or "").strip()
        if model:
            models.add(model)
    snapshot["models"] = sorted(models)
    if len(_session_org_facets_cache) >= 16:
        _session_org_facets_cache.pop(next(iter(_session_org_facets_cache)))
    _session_org_facets_cache[cache_key] = snapshot
    return snapshot


@app.get("/api/session-organization")
async def get_session_organization(project_id: str | None = Query(default=None)):
    try:
        return await asyncio.to_thread(
            _session_organization_snapshot_with_facets,
            project_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/session-organization/query")
async def query_session_organization(body: dict = Body(default={})):
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="request body must be an object")
    try:
        sessions = await asyncio.to_thread(_local_sessions_for_sidebar)
        results = await asyncio.to_thread(
            session_organization_store.query_sessions,
            sessions,
            body,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"sessions": results}


@app.post("/api/session-folders")
async def create_session_folder(body: dict = Body(default={})):
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="request body must be an object")
    try:
        folder = await asyncio.to_thread(
            session_organization_store.create_folder,
            project_id=body.get("project_id"),
            name=body.get("name"),
            parent_folder_id=body.get("parent_folder_id"),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await _broadcast_session_organization_changed()
    return {"folder": folder}


@app.patch("/api/session-folders/{folder_id}")
async def update_session_folder(folder_id: str, body: dict = Body(default={})):
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="request body must be an object")
    try:
        folder = await asyncio.to_thread(
            session_organization_store.update_folder,
            folder_id,
            body,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="folder not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await _broadcast_session_organization_changed()
    return {"folder": folder}


@app.delete("/api/session-folders/{folder_id}")
async def delete_session_folder(folder_id: str, mode: str | None = Query(None)):
    try:
        preview = await asyncio.to_thread(
            session_organization_store.folder_delete_preview,
            folder_id,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="folder not found")
    if preview["session_count"] > 0 and mode is None:
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "folder_contains_sessions",
                **preview,
            },
        )
    delete_mode = mode or "unassign"
    if delete_mode == "delete_sessions":
        for session_id in preview["session_ids"]:
            await _delete_session_tree(session_id)
    try:
        deleted = await asyncio.to_thread(
            session_organization_store.delete_folder,
            folder_id,
            mode=delete_mode,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not deleted:
        raise HTTPException(status_code=404, detail="folder not found")
    await _broadcast_session_organization_changed()
    return {"deleted": True, **preview}


@app.post("/api/session-tags")
async def create_session_tag(body: dict = Body(default={})):
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="request body must be an object")
    try:
        tag = await asyncio.to_thread(
            session_organization_store.create_tag,
            project_id=body.get("project_id"),
            name=body.get("name"),
            color=body.get("color"),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await _broadcast_session_organization_changed()
    return {"tag": tag}


@app.patch("/api/session-tags/{tag_id}")
async def update_session_tag(tag_id: str, body: dict = Body(default={})):
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="request body must be an object")
    try:
        tag = await asyncio.to_thread(
            session_organization_store.update_tag,
            tag_id,
            body,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="tag not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await _broadcast_session_organization_changed()
    return {"tag": tag}


@app.delete("/api/session-tags/{tag_id}")
async def delete_session_tag(tag_id: str):
    deleted = await asyncio.to_thread(
        session_organization_store.delete_tag,
        tag_id,
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="tag not found")
    await _broadcast_session_organization_changed()
    return {"deleted": True}


@app.patch("/api/sessions/{session_id}/organization")
async def update_session_organization(session_id: str, body: dict = Body(default={})):
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="request body must be an object")
    if not await _session_exists(session_id):
        raise HTTPException(status_code=404, detail=t("error.session_not_found_retry"))
    allowed = {"folder_id", "tag_ids", "add_tag_ids", "remove_tag_ids"}
    unknown = set(body) - allowed
    if unknown:
        raise HTTPException(status_code=400, detail="unknown organization field")
    try:
        if "folder_id" in body:
            org = await asyncio.to_thread(
                session_organization_store.set_session_folder,
                session_id,
                body.get("folder_id"),
            )
        if "tag_ids" in body:
            org = await asyncio.to_thread(
                session_organization_store.set_session_tags,
                session_id,
                body.get("tag_ids"),
            )
        if "add_tag_ids" in body or "remove_tag_ids" in body:
            org = await asyncio.to_thread(
                session_organization_store.patch_session_tags,
                session_id,
                add=body.get("add_tag_ids"),
                remove=body.get("remove_tag_ids"),
            )
        if not body:
            org = await asyncio.to_thread(
                session_organization_store.organization_for_session,
                session_id,
            )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await _broadcast_session_organization_changed([session_id])
    return {"session_id": session_id, "organization": org}


@app.post("/api/internal/session-organization/snapshot")
async def internal_session_organization_snapshot(body: dict = Body(default={})):
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="request body must be an object")
    try:
        return await asyncio.to_thread(
            session_organization_store.snapshot,
            body.get("project_id"),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/internal/session-organization/query")
async def internal_session_organization_query(body: dict = Body(default={})):
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="request body must be an object")
    try:
        sessions = await asyncio.to_thread(_local_sessions_for_sidebar)
        results = await asyncio.to_thread(
            session_organization_store.query_sessions,
            sessions,
            body,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"sessions": results}


@app.post("/api/internal/session-organization/create-folder")
async def internal_session_organization_create_folder(body: dict = Body(default={})):
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="request body must be an object")
    try:
        folder = await asyncio.to_thread(
            session_organization_store.create_folder,
            project_id=body.get("project_id"),
            name=body.get("name"),
            parent_folder_id=body.get("parent_folder_id"),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await _broadcast_session_organization_changed()
    return {"folder": folder}


@app.post("/api/internal/session-organization/update-folder")
async def internal_session_organization_update_folder(body: dict = Body(default={})):
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="request body must be an object")
    try:
        folder = await asyncio.to_thread(
            session_organization_store.update_folder,
            body.get("folder_id"),
            body.get("patch") or {},
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="folder not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await _broadcast_session_organization_changed()
    return {"folder": folder}


@app.post("/api/internal/session-organization/delete-folder")
async def internal_session_organization_delete_folder(body: dict = Body(default={})):
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="request body must be an object")
    folder_id = body.get("folder_id")
    try:
        preview = await asyncio.to_thread(
            session_organization_store.folder_delete_preview,
            folder_id,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="folder not found")
    mode = body.get("mode")
    if preview["session_count"] > 0 and mode is None:
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "folder_contains_sessions",
                **preview,
            },
        )
    delete_mode = mode or "unassign"
    if delete_mode == "delete_sessions":
        for session_id in preview["session_ids"]:
            await _delete_session_tree(session_id)
    try:
        deleted = await asyncio.to_thread(
            session_organization_store.delete_folder,
            folder_id,
            mode=delete_mode,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not deleted:
        raise HTTPException(status_code=404, detail="folder not found")
    await _broadcast_session_organization_changed()
    return {"deleted": True, **preview}


@app.post("/api/internal/session-organization/create-tag")
async def internal_session_organization_create_tag(body: dict = Body(default={})):
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="request body must be an object")
    try:
        tag = await asyncio.to_thread(
            session_organization_store.create_tag,
            project_id=body.get("project_id"),
            name=body.get("name"),
            color=body.get("color"),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await _broadcast_session_organization_changed()
    return {"tag": tag}


@app.post("/api/internal/session-organization/update-tag")
async def internal_session_organization_update_tag(body: dict = Body(default={})):
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="request body must be an object")
    try:
        tag = await asyncio.to_thread(
            session_organization_store.update_tag,
            body.get("tag_id"),
            body.get("patch") or {},
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="tag not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await _broadcast_session_organization_changed()
    return {"tag": tag}


@app.post("/api/internal/session-organization/delete-tag")
async def internal_session_organization_delete_tag(body: dict = Body(default={})):
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="request body must be an object")
    try:
        deleted = await asyncio.to_thread(
            session_organization_store.delete_tag,
            body.get("tag_id"),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not deleted:
        raise HTTPException(status_code=404, detail="tag not found")
    await _broadcast_session_organization_changed()
    return {"deleted": True}


@app.post("/api/internal/session-organization/update-session")
async def internal_session_organization_update_session(
    body: dict = Body(default={}),
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    if not coordinator.is_internal_caller(x_internal_token):
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="request body must be an object")
    session_id = str(body.get("session_id") or "").strip()
    if not await _session_exists(session_id):
        raise HTTPException(status_code=404, detail=t("error.session_not_found_retry"))
    allowed = {"session_id", "folder_id", "tag_ids", "add_tag_ids", "remove_tag_ids", "tag_source", "sync_tag_source"}
    unknown = set(body) - allowed
    if unknown:
        raise HTTPException(status_code=400, detail="unknown organization field")
    if ("tag_source" in body or "sync_tag_source" in body) and not any(
        key in body for key in ("tag_ids", "add_tag_ids")
    ):
        raise HTTPException(status_code=400, detail="tag source requires tag_ids or add_tag_ids")
    if "tag_source" in body:
        _require_tag_source_owner(body.get("tag_source"), x_internal_token)
    if "sync_tag_source" in body:
        _require_tag_source_owner(body.get("sync_tag_source"), x_internal_token)
    try:
        if "folder_id" in body:
            org = await asyncio.to_thread(
                session_organization_store.set_session_folder,
                session_id,
                body.get("folder_id"),
            )
        if "tag_ids" in body:
            if body.get("sync_tag_source"):
                org = await asyncio.to_thread(
                    session_organization_store.sync_session_tags_by_source,
                    session_id,
                    tag_ids=body.get("tag_ids"),
                    source=body.get("sync_tag_source"),
                )
            else:
                org = await asyncio.to_thread(
                    session_organization_store.set_session_tags,
                    session_id,
                    body.get("tag_ids"),
                    source=body.get("tag_source") or session_organization_store.TAG_SOURCE_MANUAL,
                )
        if "add_tag_ids" in body or "remove_tag_ids" in body:
            org = await asyncio.to_thread(
                session_organization_store.patch_session_tags,
                session_id,
                add=body.get("add_tag_ids"),
                remove=body.get("remove_tag_ids"),
                add_source=body.get("tag_source") or session_organization_store.TAG_SOURCE_MANUAL,
            )
        if not any(k in body for k in ("folder_id", "tag_ids", "add_tag_ids", "remove_tag_ids")):
            org = await asyncio.to_thread(
                session_organization_store.organization_for_session,
                session_id,
            )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await _broadcast_session_organization_changed([session_id])
    return {"session_id": session_id, "organization": org}


def _require_ask_internal(x_internal_token: str) -> None:
    if not coordinator.is_internal_caller(x_internal_token):
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    _require_builtin_runtime_extension(extension_store.BUILTIN_ASK_EXTENSION_ID)


@app.post("/api/internal/ask-ui/search")
async def internal_ask_ui_search(
    body: dict = Body(default={}),
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    _require_ask_internal(x_internal_token)
    body = body or {}
    query = (body.get("query") or "").strip()
    if not query:
        raise HTTPException(
            status_code=400, detail="query must be a non-empty string",
        )
    max_results = body.get("max_results")
    timeout = body.get("timeout")
    kwargs: dict = {}
    if isinstance(max_results, int) and max_results > 0:
        kwargs["max_results"] = max_results
    if isinstance(timeout, (int, float)) and timeout > 0:
        kwargs["timeout"] = float(timeout)
    for key in ("provider_id", "model", "reasoning_effort", "node_id"):
        val = body.get(key)
        if isinstance(val, str) and val.strip():
            kwargs[key] = val.strip()
    return await session_search.search(query, **kwargs)


@app.post("/api/internal/ask-ui/search-sessions")
async def internal_ask_ui_search_sessions(
    body: dict = Body(default={}),
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    _require_ask_internal(x_internal_token)
    body = body or {}
    query = (body.get("query") or "").strip()
    if not query:
        raise HTTPException(
            status_code=400, detail="query must be a non-empty string",
        )
    max_results = body.get("max_results")
    timeout = body.get("timeout")
    kwargs: dict = {}
    if isinstance(max_results, int) and max_results > 0:
        kwargs["max_results"] = max_results
    if isinstance(timeout, (int, float)) and timeout > 0:
        kwargs["timeout"] = float(timeout)
    for key in ("provider_id", "model", "reasoning_effort", "node_id"):
        val = body.get(key)
        if isinstance(val, str) and val.strip():
            kwargs[key] = val.strip()
    result = await session_search.run_search_sessions_session(query, **kwargs)
    return await asyncio.to_thread(
        session_search.canonical_search_response, result,
    )


@app.post("/api/internal/ask-ui/ensure")
async def internal_ask_ui_ensure(
    body: dict | None = None,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    _require_ask_internal(x_internal_token)
    return await session_search.ensure_ask_session()


def _require_assistant_internal(x_internal_token: str) -> None:
    if not coordinator.is_internal_caller(x_internal_token):
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    _require_builtin_runtime_extension(extension_store.extension_id_for_role('assistant'))


@app.post("/api/internal/assistant-ui/ensure")
async def internal_assistant_ui_ensure(
    body: dict | None = None,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    _require_assistant_internal(x_internal_token)
    board_preamble = None
    if isinstance(body, dict) and "board_preamble" in body:
        board_preamble = str(body.get("board_preamble") or "")
    sess = await asyncio.to_thread(assistant_ui.ensure_singleton, board_preamble)
    return {"id": sess["id"], "name": sess.get("name"), "cwd": sess.get("cwd")}


@app.post("/api/internal/assistant-ui/ensure-monitor")
async def internal_assistant_ui_ensure_monitor(
    body: dict | None = None,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    _require_assistant_internal(x_internal_token)
    board_preamble = None
    if isinstance(body, dict) and "board_preamble" in body:
        board_preamble = str(body.get("board_preamble") or "")
    sess = await asyncio.to_thread(assistant_ui.ensure_monitor, board_preamble)
    return {"id": sess["id"], "name": sess.get("name"), "cwd": sess.get("cwd")}


@app.post("/api/internal/assistant-ui/search")
async def internal_assistant_ui_search(
    body: dict = Body(default={}),
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    _require_assistant_internal(x_internal_token)
    return await assistant_ui.search(
        str(body.get("query") or ""),
        max_results=int(body.get("max_results") or 10),
    )


@app.post("/api/internal/assistant-ui/resolve-ba-session")
async def internal_assistant_ui_resolve_ba_session(
    body: dict = Body(default={}),
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    _require_assistant_internal(x_internal_token)
    return await assistant_ui.resolve_ba_session(str(body.get("session_id") or ""))


@app.post("/api/internal/assistant-ui/adopt-native-session")
async def internal_assistant_ui_adopt_native_session(
    body: dict = Body(default={}),
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    _require_assistant_internal(x_internal_token)
    return await assistant_ui.adopt_native_session(
        str(body.get("session_id") or ""),
        transcript_path=str(body.get("transcript_path") or ""),
    )


@app.post("/api/internal/assistant-ui/delegate")
async def internal_assistant_ui_delegate(
    body: dict = Body(default={}),
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    _require_assistant_internal(x_internal_token)
    target = str(body.get("target_session_id") or "").strip()
    prompt = str(body.get("prompt") or "").strip()
    if not target or not prompt:
        raise HTTPException(status_code=400, detail="target_session_id and prompt are required")
    return await assistant_ui.delegate(target, prompt)


@app.post("/api/internal/assistant-ui/last-turn")
async def internal_assistant_ui_last_turn(
    body: dict = Body(default={}),
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    _require_assistant_internal(x_internal_token)
    sid = str(body.get("session_id") or "").strip()
    if not sid:
        raise HTTPException(status_code=400, detail="session_id is required")
    return await asyncio.to_thread(assistant_ui.last_turn, sid)


def _tree_has_loaded_events(tree: dict) -> bool:
    stack = [tree]
    while stack:
        node = stack.pop()
        for m in node.get("messages") or []:
            events = m.get("events")
            if isinstance(events, list) and events:
                return True
            for worker in m.get("workers") or []:
                if not isinstance(worker, dict):
                    continue
                events = worker.get("events")
                if isinstance(events, list) and events:
                    return True
        stack.extend(node.get("forks", []) or [])
    return False


def _strip_synthetic_events_from_tree(tree: dict) -> None:
    """Walk every node in the tree and strip SDK continuation markers
    (model="<synthetic>", "No response requested.") from both
    `msg.events` and `msg.content`. Pure data hygiene — runs on every
    REST snapshot.

    (Was bundled with the zombie-streaming reaper until the streaming-
    source-of-truth refactor moved `isStreaming` ownership onto runner
    registration — runner add/remove now drives the flag via the hook
    in `coordinator.turn_manager.run_state_add` / `_run_state_set_target` /
    `run_state_remove`, eliminating both the start-path race and the
    in-recovery false-positive that the gated reaper compensated for.)
    """

    def _visit(node: dict) -> None:
        sid = node.get("id")
        for m in node.get("messages", []):
            if m.get("role") != "assistant":
                continue
            events = m.get("events")
            if isinstance(events, list) and events:
                cleaned = strip_synthetic_events(events)
                if len(cleaned) != len(events):
                    session_manager.set_native_events(sid, m["id"], cleaned)
                    m["events"] = cleaned
                    # Invalidate uid_idx — direct list assignment bypassed
                    # the mutator's invalidation (same-length-different-
                    # uuids would survive the lazy len check in
                    # `orchs.base._uid_idx_for`). Pop forces a rebuild on
                    # the next apply_event.
                    m.pop("_uid_idx", None)
                    # Re-extract content without synthetic text. Semantics:
                    # a message left with NO assistant text blanks (it was
                    # synthetic-only); a message whose remaining real events
                    # merely end on a tool/thinking boundary keeps its
                    # current snapshot (project_content_snapshot guard).
                    if has_assistant_text(cleaned):
                        new_content = project_content_snapshot(
                            cleaned, m.get("content"),
                        )
                    else:
                        new_content = ""
                    if new_content != (m.get("content") or ""):
                        session_manager.update_running_content(
                            sid, m["id"], new_content,
                        )
                        m["content"] = new_content
        for f in node.get("forks", []):
            _visit(f)

    _visit(tree)


def _strip_synthetic_events_from_message(msg: dict) -> None:
    """Pure per-message synthetic-event strip for the lazy-expand fetch.
    Operates on a DEEPCOPY returned by `get_message_full` — mutates only
    the copy (never live state, unlike `_strip_synthetic_events_from_tree`)
    and covers every events list (native, manager, worker panels)."""
    for owner in (msg, *(msg.get("workers") or [])):
        if not isinstance(owner, dict):
            continue
        events = owner.get("events")
        if isinstance(events, list) and events:
            owner["events"] = strip_synthetic_events(events)


def _now_iso() -> str:
    from datetime import datetime
    return datetime.now().isoformat()


# `_claude_projects_root` lives in `paths.claude_projects_root_for_session`.
from paths import claude_projects_root_for_session as _claude_projects_root


# ============================================================================
# Render-tree reconcile from events.jsonl
# ============================================================================
# msg.events is the canonical render shape, maintained at runtime by the
# strategy's apply_event. If for any reason an assistant message's
# events list lags behind events.jsonl (e.g. crash between
# session_store.flush and the next persist), this safety net re-applies
# any missing events deterministically — events.jsonl entries carry
# `msg_id` so there's no heuristic turn-grouping to get wrong.

def _reconcile_msg_events_from_jsonl(tree: dict, *, on_historical_change=None) -> None:
    """Thin delegate to `render_tree_hydrate.reconcile_msg_events_from_jsonl`.

    The implementation was moved out of `main.py` so both this REST/WS
    call site AND `session_manager._load_root` can call into it without
    creating a `main → session_manager → main` import cycle. See
    `backend/render_tree_hydrate.py` for the full body."""
    from render_tree_hydrate import reconcile_msg_events_from_jsonl
    reconcile_msg_events_from_jsonl(tree, on_historical_change=on_historical_change)


def _reconcile_root_by_id(root_id: str, *, after_seq: int = 0) -> list[dict]:
    """Warm-path reconcile body bound via `bind_reconcile_fn`.

    Delegates to the single bracketing/hydration implementation in
    `render_tree_hydrate` (the same body cold load runs), so warm and
    cold render trees cannot diverge. `after_seq` is the warm-reconcile
    cursor: cold load hydrates the full stream, while reconcile projects
    only rows appended since the last successful cursor.

    Returns stub_invalidated payloads for historical msgs whose expanded
    timeline changed. The payload remains the small current collapsed
    stub; outside-tail changes still need the ping to bust frontend full-
    message caches."""
    import copy as _copy
    from event_journal import event_journal_reader
    from render_stub import build_stub
    from render_tree_hydrate import reconcile_msg_events_from_jsonl

    current = event_journal_reader.current_seq(root_id)
    if current is not None and after_seq >= current:
        return []

    changes: list[dict] = []

    def _on_historical_change(sid: str, msg_id: str, m: dict) -> None:
        stub = build_stub(m)
        changes.append({
            "app_session_id": sid,
            "msg_id": msg_id,
            "stub": {
                "event_count": stub["event_count"],
                "last_events": _copy.deepcopy(stub["last_events"]),
            },
        })

    session_manager.hydrate_root_prepared(
        root_id,
        after_seq=after_seq,
        on_historical_change=_on_historical_change,
    )
    return changes


def _fire_and_forget(coro) -> None:
    """Schedule a coroutine on the running event loop, logging any
    exception instead of silently swallowing it.  Replaces bare
    ``loop.create_task(coro)`` patterns that dropped errors (e.g.
    broadcast_global ValueError from a missing allowlist entry)."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        coro.close()
        return

    async def _wrapped():
        try:
            await coro
        except Exception:
            logger.exception("fire-and-forget task failed")

    loop.create_task(_wrapped())


def _emit_session_processing(root_id: str, kind: str) -> None:
    """Wrapper called by `session_manager._async_reconcile_with_progress`
    on the event loop thread. Emits `session_processing_started` /
    `session_processing_finished` to every connected WS so the
    frontend can render a "reconciling…" badge for slow reconciles
    (>0.3s)."""
    payload = {"root_id": root_id}
    coro = coordinator.broadcast_global(f"session_processing_{kind}", payload)
    _fire_and_forget(coro)


def _emit_session_reconciled(root_id: str) -> None:
    """Called by `session_manager._async_reconcile_with_progress` after
    every reconcile completes (fast or slow). Frontend silently
    refetches the session if the user is viewing it, replacing any
    stale cache served by the initial GET."""
    gen = session_manager._reconcile_gen.get(root_id, 0)
    logger.info(
        "_emit_session_reconciled %s: broadcasting gen=%d",
        root_id[:8], gen,
    )
    coro = coordinator.broadcast_global("session_reconciled", {"root_id": root_id})
    _fire_and_forget(coro)


_STUB_INVALIDATED_COALESCE_SECONDS = 0.05
_stub_invalidated_pending: list[dict] = []
_stub_invalidated_flush_scheduled = False
_stub_invalidated_flush_handle: asyncio.TimerHandle | None = None


def _flush_stub_invalidated() -> None:
    global _stub_invalidated_flush_scheduled, _stub_invalidated_flush_handle
    _stub_invalidated_flush_scheduled = False
    _stub_invalidated_flush_handle = None
    if not _stub_invalidated_pending:
        return
    changes = list(_stub_invalidated_pending)
    _stub_invalidated_pending.clear()
    try:
        coro = coordinator.broadcast_global("stub_invalidated", {"changes": changes})
    except Exception:
        logger.exception("stub_invalidated broadcast scheduling failed")
        return
    _fire_and_forget(coro)


def _emit_stub_invalidated(changes: list[dict]) -> None:
    """Called by `session_manager._async_reconcile_with_progress` on the
    event-loop thread after a reconcile. Emits one batched `stub_invalidated`
    global ping for non-latest historical msgs whose stubs went stale, so
    any client with that turn collapsed swaps in the fresh stub (and an
    expanded turn re-fetches). Not persisted — authoritative events live
    in the render tree / events.jsonl."""
    global _stub_invalidated_flush_scheduled, _stub_invalidated_flush_handle
    if not changes:
        return
    _stub_invalidated_pending.extend(changes)
    if _stub_invalidated_flush_scheduled:
        return
    _stub_invalidated_flush_scheduled = True
    loop = asyncio.get_running_loop()
    _stub_invalidated_flush_handle = loop.call_later(
        _STUB_INVALIDATED_COALESCE_SECONDS,
        _flush_stub_invalidated,
    )


def _reconcile_catchup_state(sub_sid: str) -> tuple[str | None, bool]:
    sub_root_id = session_manager._root_id_for(sub_sid)
    if not isinstance(sub_root_id, str):
        return None, False
    session_manager.schedule_reconcile_if_needed(sub_root_id)
    with session_manager._lock_for_root(sub_root_id):
        return sub_root_id, session_manager.is_reconcile_in_flight(sub_root_id)


def _session_reconcile_snapshot_and_schedule(root_id: str) -> tuple[bool, bool, int]:
    dirty = session_manager.is_reconcile_dirty(root_id)
    hydrated = root_id in session_manager._event_hydrated_roots
    gen_after = session_manager._reconcile_gen.get(root_id, 0)
    session_manager.schedule_reconcile_if_needed(root_id)
    return dirty, hydrated, gen_after


def _session_detail_snapshot_sync(
    session_id: str,
    *,
    msg_limit: int,
    exchange_count: Optional[int],
    include_cache_key: bool = False,
) -> Optional[dict]:
    root_id_start = time.perf_counter()
    root_id = session_manager._root_id_for(session_id)
    root_id_ms = (time.perf_counter() - root_id_start) * 1000
    perf.record("sessions.detail.root_id", root_id_ms)
    barrier_seq = 0
    get_start = time.perf_counter()
    max_seq_ms = 0.0
    tree_ms = 0.0
    strip_ms = 0.0
    max_context_ms = 0.0
    has_events = False
    if isinstance(root_id, str):
        max_seq_start = time.perf_counter()
        has_events, barrier_seq, max_context = _session_event_meta(root_id)
        max_seq_ms = (time.perf_counter() - max_seq_start) * 1000
        perf.record("sessions.detail.event_meta", max_seq_ms)
    else:
        max_context = {}
    gen_before = session_manager._reconcile_gen.get(root_id or "", 0) if root_id else 0
    tree_start = time.perf_counter()
    detail_cache_key = None
    if include_cache_key:
        built = session_manager.get_root_tree_stubbed_with_cache_key(
            session_id,
            msg_limit=msg_limit,
            exchange_count=exchange_count,
            known_root_id=root_id if isinstance(root_id, str) else None,
        )
        if built is None:
            tree = None
        else:
            tree, tree_key = built
            detail_cache_key = (session_id, tree_key)
    else:
        tree = session_manager.get_root_tree_stubbed(
            session_id, msg_limit=msg_limit, exchange_count=exchange_count,
        )
    tree_ms = (time.perf_counter() - tree_start) * 1000
    perf.record("sessions.detail.tree", tree_ms)
    if not tree:
        return None

    strip_start = time.perf_counter()
    if _tree_has_loaded_events(tree):
        _strip_synthetic_events_from_tree(tree)
    strip_ms = (time.perf_counter() - strip_start) * 1000
    perf.record("sessions.detail.strip_synthetic", strip_ms)
    root_id = tree.get("id")
    if isinstance(root_id, str):
        if has_events:
            reconcile_start = time.perf_counter()
            dirty, hydrated, gen_after = _session_reconcile_snapshot_and_schedule(root_id)
            reconcile_ms = (time.perf_counter() - reconcile_start) * 1000
            perf.record("sessions.detail.reconcile_snapshot", reconcile_ms)
            msg_count = len(tree.get("messages", []))
            assistant_msgs = [m for m in tree.get("messages", []) if m.get("role") == "assistant"]
            last_events = assistant_msgs[-1].get("events") if assistant_msgs else None
            last_stub = assistant_msgs[-1].get("stub") if assistant_msgs else None
            logger.info(
                "GET session %s: dirty=%s hydrated=%s gen=%d->%d barrier=%d "
                "msgs=%d queued=%d draft_len=%d last_asst_evts=%s "
                "last_asst_stub=%s timings="
                "max_seq=%.1fms barrier=%.1fms tree=%.1fms strip=%.1fms",
                root_id[:8], dirty, hydrated, gen_before, gen_after, barrier_seq,
                msg_count,
                len(tree.get("queued_prompts") or []),
                len(tree.get("draft_input") or ""),
                len(last_events) if last_events else None,
                last_stub.get("event_count") if last_stub else None,
                max_seq_ms, 0.0, tree_ms, strip_ms,
            )
        if has_events:
            max_context_start = time.perf_counter()
            tree["max_seq_by_sid"] = _session_detail_watermarks(
                root_id, has_events, barrier_seq, max_context,
            )
            max_context_ms = (time.perf_counter() - max_context_start) * 1000
            perf.record("sessions.detail.max_context_copy", max_context_ms)
        else:
            tree["max_seq_by_sid"] = {}
        total_ms = (time.perf_counter() - get_start) * 1000
        perf.record("sessions.detail.total", total_ms)
        if total_ms >= 50 or root_id_ms >= 20 or max_context_ms >= 20 or strip_ms >= 20:
            logger.info(
                "GET session %s timings total=%.1fms root_id=%.1fms max_context=%.1fms strip=%.1fms has_events=%s",
                root_id[:8], total_ms, root_id_ms, max_context_ms, strip_ms, has_events,
            )
        file_path_start = time.perf_counter()
        tree["file_path"] = str(_session_path(root_id))
        perf.record("sessions.detail.file_path", (time.perf_counter() - file_path_start) * 1000)
    if detail_cache_key is not None:
        cache_marker_start = time.perf_counter()
        tree["_detail_response_cache_key_parts"] = (
            detail_cache_key[0],
            detail_cache_key[1],
            has_events,
            tuple(sorted(_session_detail_watermarks(
                root_id,
                has_events,
                barrier_seq,
                max_context,
            ).items())),
        )
        perf.record("sessions.detail.cache_marker", (time.perf_counter() - cache_marker_start) * 1000)
    return tree


def _session_detail_response_cache_key_sync(
    session_id: str,
    *,
    msg_limit: int,
    exchange_count: Optional[int],
    known_root_id: str | None = None,
) -> tuple | None:
    root_id = known_root_id or session_manager._root_id_for(session_id)
    if not isinstance(root_id, str):
        return None
    has_events, barrier_seq, max_context = _session_event_meta(root_id)
    watermarks = _session_detail_watermarks(
        root_id, has_events, barrier_seq, max_context,
    )
    if known_root_id:
        tree_key = session_manager.root_tree_stub_cache_key_for_root(
            root_id,
            msg_limit=msg_limit,
            exchange_count=exchange_count,
        )
    else:
        tree_key = session_manager.root_tree_stub_cache_key(
            session_id,
            msg_limit=msg_limit,
            exchange_count=exchange_count,
        )
    if tree_key is None:
        return None
    return (
        session_id,
        tree_key,
        has_events,
        tuple(sorted(watermarks.items())),
    )


def _session_detail_cached_key_still_current(
    key: tuple,
    session_id: str,
    *,
    msg_limit: int,
    exchange_count: Optional[int],
) -> bool:
    if len(key) != 4 or key[0] != session_id:
        return False
    cached_tree_key = key[1]
    root_id = (
        cached_tree_key[0]
        if isinstance(cached_tree_key, tuple)
        and cached_tree_key
        and isinstance(cached_tree_key[0], str)
        else None
    )
    if not isinstance(root_id, str):
        return False
    tree_key = session_manager.root_tree_stub_cache_key_for_root(
        root_id,
        msg_limit=msg_limit,
        exchange_count=exchange_count,
    )
    if tree_key is None or key[1] != tree_key:
        return False
    has_events, barrier_seq, max_context = _session_event_meta(root_id)
    watermarks = _session_detail_watermarks(
        root_id, has_events, barrier_seq, max_context,
    )
    return key[2] == has_events and key[3] == tuple(sorted(watermarks.items()))


def _floor_events_from_seq(
    app_session_id: str,
    requested_from_seq: int,
    *,
    cursor_known: bool,
) -> int:
    if cursor_known:
        return max(0, requested_from_seq)
    root_id = session_manager._root_id_for(app_session_id)
    if not isinstance(root_id, str):
        return 0
    # Render-projection head, not the raw journal head: flooring to the
    # raw head would push the resume cursor past the rendered tail and
    # drop a still-streaming turn (same defect as the REST watermark).
    floor = event_ingester.render_seq_for_sid(root_id, app_session_id)
    return max(0, floor)


def _total_replay_events(msg: dict) -> int:
    n = len(msg.get("events") or [])
    for w in msg.get("workers") or []:
        n += len(w.get("events") or [])
    return n


def _merge_in_flight_replay(
    replay_msgs: list[dict],
    in_flight: Optional[dict],
    *,
    app_session_id: str,
) -> list[dict]:
    if in_flight is None:
        return replay_msgs
    in_flight_id = in_flight.get("id")
    in_flight_evts = _total_replay_events(in_flight)
    new_replay = []
    for m in replay_msgs:
        if m.get("id") != in_flight_id:
            new_replay.append(m)
            continue
        cache_evts = _total_replay_events(m)
        if in_flight_evts >= cache_evts:
            new_replay.append(in_flight)
            continue
        merged = dict(m)
        merged["isStreaming"] = in_flight.get("isStreaming", True)
        merged["isStale"] = in_flight.get("isStale")
        logger.info(
            "WS replay %s: cache over in-flight (%d>%d evts), stamping isStreaming",
            app_session_id[:8], cache_evts, in_flight_evts,
        )
        new_replay.append(merged)
    return new_replay


def _build_messages_replay_delta(
    app_session_id: str,
    since_seq: int,
    *,
    limit: int,
    get_messages_since=session_manager.get_messages_since,
    get_in_flight=coordinator.turn_manager.get_in_flight_assistant_msg,
) -> Optional[dict]:
    initial_in_flight = get_in_flight(app_session_id)
    used_exclusive = since_seq > 0 and initial_in_flight is None
    effective_since_seq = since_seq + 1 if used_exclusive else since_seq
    delta = get_messages_since(app_session_id, effective_since_seq, limit=limit)
    if delta is None:
        return None
    final_in_flight = get_in_flight(app_session_id)
    if used_exclusive and final_in_flight is not None:
        delta = get_messages_since(app_session_id, since_seq, limit=limit)
        if delta is None:
            return None
    replay_msgs = _merge_in_flight_replay(
        delta["messages"],
        final_in_flight,
        app_session_id=app_session_id,
    )
    return {
        "messages": replay_msgs,
        "next_seq": delta["next_seq"],
        "in_flight": final_in_flight,
    }


@app.get("/api/sessions/topbar-pinned")
async def get_topbar_pinned_sessions():
    sessions = await asyncio.to_thread(session_manager.list)
    pinned = [
        session
        for session in sessions
        if session.get("topbar_pinned")
    ]
    pinned.sort(
        key=lambda session: (
            session.get("topbar_pinned_at") or "",
            session.get("id") or "",
        ),
        reverse=True,
    )
    return {"sessions": pinned}


@app.get("/api/sessions/summaries")
async def get_session_summaries(ids: str = Query("")):
    requested_ids = [
        sid.strip()
        for sid in ids.split(",")
        if sid.strip()
    ]
    if not requested_ids:
        return {"sessions": []}
    cache_key = (
        tuple(requested_ids),
        session_store.summary_index_version(),
    )
    cached_response = _session_summaries_cache_get(cache_key)
    if cached_response is not None:
        perf.record("sessions.summaries.response_cache.hit", 1.0)
        return cached_response
    perf.record("sessions.summaries.response_cache.miss", 1.0)
    summaries = await asyncio.to_thread(
        _local_session_summaries_by_ids,
        requested_ids,
    )
    by_id = {str(session.get("id")): session for session in summaries if session.get("id")}
    ordered = [by_id[sid] for sid in requested_ids if sid in by_id]
    page = await _run_hot_path(
        "sessions.summaries.decorate.worker",
        _decorate_local_sidebar_sessions,
        ordered,
        None,
    )
    final_cache_key = (
        tuple(requested_ids),
        session_store.summary_index_version(),
    )
    return _session_summaries_cache_put(final_cache_key, {"sessions": page})


@app.get("/api/sessions/{session_id}/stats")
async def get_session_stats(session_id: str):
    if session_id == session_search.ASK_SINGLETON_ID:
        session = await session_search.ensure_ask_session()
    else:
        session = await asyncio.to_thread(virtual_session_store.get, session_id)
    if not session:
        session = await asyncio.to_thread(session_manager.get, session_id)
    if not session:
        raise HTTPException(status_code=404, detail=t("error.session_not_found"))
    return _sidebar_stats_payload(session)


@app.get("/api/communications")
async def get_communications(
    session_id: str | None = Query(None),
    limit: int = Query(default=200, ge=1, le=500),
):
    import communication_log

    return await asyncio.to_thread(
        communication_log.list_communications,
        session_id=session_id or "",
        limit=limit,
    )


@app.post("/api/chats/{chat_id}/messages")
async def post_chat_message(chat_id: str, body: dict = Body(default={})):
    import chat_store

    sender_session_id = str(body.get("sender_session_id") or "").strip()
    message = str(body.get("message") or "").strip()
    if not sender_session_id:
        raise HTTPException(status_code=400, detail="sender_session_id is required")
    if not message:
        raise HTTPException(status_code=400, detail="message is required")

    session = await asyncio.to_thread(session_manager.get, sender_session_id)
    if not session:
        raise HTTPException(status_code=404, detail=t("error.session_not_found"))

    try:
        return await asyncio.to_thread(
            chat_store.post_and_read,
            chat_id=chat_id,
            reader_id=sender_session_id,
            message=message,
        )
    except chat_store.ChatStoreError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/sessions/{session_id}")
async def get_session(
    session_id: str,
    msg_limit: int = Query(default=50, ge=1, le=200),
    exchange_count: Optional[int] = Query(default=None, ge=1, le=100),
):
    """Return the root tree containing `session_id`, with messages
    paginated to the latest `msg_limit` per node (or `exchange_count`
    user→assistant exchanges when provided).

    `session_id` may be a root or any fork inside one — both resolve
    to the same root tree. The frontend uses this to load every pane
    of a split-fork view in one call (root + embedded forks all carry
    their own `messages`, `claude_session_id`s, drafts, etc., so the
    UI has all the data it needs to render every pane). For backward
    compat the response also exposes the requested `id` in `id` so
    callers that expect "the session they asked for" still work — but
    the meaningful content is at the root level.

    Each node carries a ``pagination`` dict: ``{total_messages,
    oldest_loaded_seq, has_older}``. Older messages can be loaded via
    ``GET /api/sessions/{id}/messages?before_seq=N``."""
    if session_id == session_search.ASK_SINGLETON_ID:
        virtual = await session_search.ensure_ask_session()
    else:
        virtual = await asyncio.to_thread(virtual_session_store.get, session_id)
    if virtual:
        return virtual

    simple_cache_key = (session_id, msg_limit, exchange_count)
    cached_full_key = _session_detail_response_cache_latest.get(simple_cache_key)
    cache_key = None
    if cached_full_key is not None:
        still_current = await asyncio.to_thread(
            _session_detail_cached_key_still_current,
            cached_full_key,
            session_id,
            msg_limit=msg_limit,
            exchange_count=exchange_count,
        )
        if still_current:
            cache_key = cached_full_key
        else:
            _session_detail_response_cache_latest.pop(simple_cache_key, None)
    if cache_key is not None:
        cached = _session_detail_cache_get(cache_key)
        if cached is not None:
            root_id = cache_key[1][0]
            if cache_key[3] and isinstance(root_id, str):
                await asyncio.to_thread(_session_reconcile_snapshot_and_schedule, root_id)
            perf.record("sessions.detail.response_cache.hit", 1.0)
            return cached
    perf.record("sessions.detail.response_cache.miss", 1.0)

    worker_start = time.perf_counter()
    tree = await _run_session_detail_hot_path(
        "sessions.detail.worker",
        _session_detail_snapshot_sync,
        session_id,
        msg_limit=msg_limit,
        exchange_count=exchange_count,
        include_cache_key=True,
    )
    perf.record("sessions.detail.worker.total", (time.perf_counter() - worker_start) * 1000)
    if not tree:
        raise HTTPException(status_code=404, detail=t("error.session_not_found"))
    if cache_key is None:
        cache_key_parts = tree.pop("_detail_response_cache_key_parts", None)
        if isinstance(cache_key_parts, tuple) and len(cache_key_parts) == 4:
            cache_key = cache_key_parts
    else:
        tree.pop("_detail_response_cache_key_parts", None)
    if cache_key is not None:
        return await _session_detail_cache_put_async(cache_key, tree)
    return await _json_bytes_response_async(tree)


@app.get("/api/sessions/{session_id}/messages")
async def get_older_messages(
    session_id: str,
    before_seq: int = Query(...),
    limit: int = Query(default=50, ge=1, le=200),
    exchange_count: Optional[int] = Query(default=None, ge=1, le=100),
):
    """Load messages older than ``before_seq`` for a session node.
    When ``exchange_count`` is provided, pages by user→assistant exchanges.
    Returns ``{messages, has_older, oldest_loaded_seq, total_messages}``."""
    virtual = await asyncio.to_thread(virtual_session_store.get, session_id)
    if virtual:
        return {
            "messages": [],
            "has_older": False,
            "oldest_loaded_seq": None,
            "total_messages": len(virtual.get("messages") or []),
        }
    result = await asyncio.to_thread(
        session_manager.get_messages_before,
        session_id, before_seq, limit, exchange_count=exchange_count,
    )
    if result is None:
        raise HTTPException(status_code=404, detail=t("error.session_not_found"))
    return result


@app.get("/api/sessions/{session_id}/messages/{message_id}/events")
async def get_message_events(session_id: str, message_id: str):
    """Lazy-expand fetch: return ONE message with its FULL events lists
    (Tier-1 lazy event fetch). The heavy load paths ship completed,
    non-latest assistant messages as stubs (`msg.stub`, empty events);
    the frontend calls this when the user expands such a turn.

    Reads the in-memory render tree (single source of truth), so
    `stub.event_count` matches the primary-stream renderable count of
    the returned message."""
    msg = await asyncio.to_thread(
        session_manager.get_message_full, session_id, message_id,
    )
    if msg is None:
        raise HTTPException(status_code=404, detail=t("error.session_not_found"))
    _strip_synthetic_events_from_message(msg)
    return msg


def _resolve_session_node_id(body: dict) -> str:
    """Validate the optional `node_id` on a session-create request.

    INVARIANT: `"primary"` is always accepted without touching
    topology.yaml so single-machine deploys (no topology configured)
    keep working. Any other id MUST appear in topology AND, if the
    spec declares `cwd_roots`, the requested `cwd` MUST sit under one
    of them — otherwise the worker would crash at delegation time on
    a directory that doesn't exist on the remote node's filesystem.

    file_editing mode now runs on any node — the file_editor pipeline
    routes baseline reads through `node_link.rpc_call` when the
    session targets a non-local node (see file_editor.start).
    """
    node_id = body.get("node_id") or "primary"
    if node_id == "primary":
        return node_id
    from topology import TopologyError, load_topology
    spec = None
    try:
        spec = load_topology().get(node_id)
    except (TopologyError, KeyError):
        pass
    if spec is None:
        from node_registry_store import to_spec as registry_to_spec
        spec = registry_to_spec(node_id)
    if spec is None:
        raise HTTPException(
            status_code=400,
            detail=f"node_id {node_id!r} is unknown — not in topology.yaml or approved nodes",
        )
    cwd = body.get("cwd") or ""
    if spec.cwd_roots:
        if not cwd.startswith("/"):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"cwd {cwd!r} must be an absolute path when targeting "
                    f"node {node_id!r}"
                ),
            )
        if not any(
            cwd == root or cwd.startswith(root.rstrip("/") + "/")
            for root in spec.cwd_roots
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"cwd {cwd!r} is not under any of node {node_id!r}'s "
                    f"cwd_roots: {list(spec.cwd_roots)}"
                ),
            )
    return node_id


def _node_offline_error(session: Optional[dict]) -> Optional[str]:
    """Fail-closed gate for sends into a node-hosted session: a prompt
    accepted while the node is down would sit queued and error minutes
    later — reject upfront with a clear message instead."""
    node_id = (session or {}).get("node_id") or "primary"
    if node_id == "primary":
        return None
    nodes_not_ready = extension_store.runtime_not_ready_message(
        extension_store.extension_id_for_role('machine-nodes')
    )
    if nodes_not_ready is not None:
        return nodes_not_ready
    try:
        from topology import local_node_id
        if node_id == local_node_id():
            return None
    except Exception:
        pass
    import node_store
    conn = node_store.get_connection(node_id)
    if conn is None:
        return (
            f"node {node_id!r} is offline — this session runs there. "
            f"Reconnect the node and resend."
        )
    if node_store.connection_version_blocks_work(conn):
        primary_sha = node_store.app_version.current_commit_sha()
        node_sha = conn.app_commit_sha or "unknown"
        return (
            f"node {node_id!r} is running app commit {node_sha}, "
            f"but primary is running {primary_sha}. Update or restart the node "
            f"onto the primary version and resend."
        )
    return None


async def _file_op(node_id: str, method: str, params: dict):
    """REST-endpoint wrapper around `node_rpc_handlers.call_local_or_remote`
    that translates plain exceptions to FastAPI HTTPExceptions.

    Error translation:
      `RuntimeError("...topology.yaml...")` → 400 (caller asked for a
        non-primary node but no topology is configured)
      NodeOffline          → 503
      asyncio.TimeoutError → 504
      RuntimeError         → 502 (remote handler raised)
      FileNotFoundError    → 404
      FileExistsError      → 409
      PermissionError      → 403
      ValueError           → 400
    """
    if node_id != "primary":
        nodes_not_ready = extension_store.runtime_not_ready_message(
            extension_store.extension_id_for_role('machine-nodes')
        )
        if nodes_not_ready is not None:
            raise HTTPException(status_code=404, detail=nodes_not_ready)
    import node_link
    from node_rpc_handlers import call_local_or_remote
    try:
        return await call_local_or_remote(node_id, method, params)
    except HTTPException:
        raise
    except node_link.NodeOffline as e:
        raise HTTPException(status_code=503, detail=str(e))
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=504,
            detail=f"node {node_id!r} did not respond within timeout",
        )
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except FileExistsError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        # RuntimeErrors come from the remote handler → 502.
        raise HTTPException(status_code=502, detail=str(e))


def _local_node_id_or_primary() -> str:
    """Resolve the local node's id without raising. Single-machine
    deploys (no topology.yaml) get the legacy `"primary"` sentinel."""
    try:
        from topology import local_node_id as _lid
        return _lid()
    except Exception:
        return "primary"


@app.post("/api/internal/machine-nodes/local-node-id")
async def internal_get_local_node_id(
    body: dict | None = None,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    _require_machine_nodes_internal(x_internal_token)
    """Tells the frontend which node snapshot entry corresponds
    to "this backend's host" — used to render the "(host)"
    badge in pickers and to compute `is_local` for default-pick rules."""
    return {"node_id": _local_node_id_or_primary()}


@app.post("/api/sessions")
async def create_session(body: Any = Body(default=None)):
    if body is None:
        body = {}
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="request body must be an object")
    client_session_id = body.get("client_session_id")
    if client_session_id is not None:
        if not isinstance(client_session_id, str):
            logger.warning("create_session rejected: client_session_id not a string type=%s", type(client_session_id).__name__)
            raise HTTPException(status_code=400, detail="client_session_id must be a UUID string")
        try:
            parsed_client_session_id = uuid.UUID(client_session_id)
        except ValueError:
            logger.warning("create_session rejected: client_session_id not a valid UUID value=%r", client_session_id[:80])
            raise HTTPException(status_code=400, detail="client_session_id must be a valid UUID")
        if str(parsed_client_session_id) != client_session_id:
            logger.warning("create_session rejected: client_session_id not canonical value=%r", client_session_id[:80])
            raise HTTPException(status_code=400, detail="client_session_id must be a canonical UUID")
        existing = await asyncio.to_thread(session_manager.get, client_session_id)
        if existing is not None:
            return existing

    requested_source = body.get("source", "web")
    if requested_source not in ("web", "cli"):
        requested_source = "web"
    worker_creation_policy = str(body.get("worker_creation_policy") or "ask")
    if worker_creation_policy not in ("ask", "approve", "deny"):
        raise HTTPException(status_code=400, detail="worker_creation_policy must be ask, approve, or deny")
    requested_orchestration_mode = str(body.get("orchestration_mode") or "").strip()
    if requested_orchestration_mode == "manager":
        requested_orchestration_mode = "team"
    if not requested_orchestration_mode:
        requested_orchestration_mode = (
            "team"
            if _builtin_extension_runtime_ready(
                extension_store.extension_id_for_role('team-orchestration')
            )
            else "native"
        )
    if requested_orchestration_mode == "team":
        team_not_ready = extension_store.runtime_not_ready_message(
            extension_store.extension_id_for_role('team-orchestration')
        )
        if team_not_ready is not None:
            raise HTTPException(status_code=404, detail=team_not_ready)
    bare_config = body.get("bare_config", False)
    if not isinstance(bare_config, bool):
        raise HTTPException(status_code=400, detail="bare_config must be a boolean")
    try:
        capability_contexts = normalize_capability_contexts(body.get("capability_contexts"))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    requested_provider_id = body.get("provider_id")
    provider_record = await asyncio.to_thread(
        _provider_for_required_model,
        requested_provider_id,
    )
    model = _required_model_from_body_or_provider(body, provider_record)
    requested_effort = await asyncio.to_thread(
        _provider_reasoning_effort,
        requested_provider_id,
        _api_reasoning_effort(body.get("reasoning_effort")),
    )
    requested_permission = await asyncio.to_thread(
        _provider_permission,
        requested_provider_id,
        _api_permission(body.get("permission")),
    )
    node_id = _resolve_session_node_id(body)
    requested_folder_id = body.get("folder_id")
    if requested_folder_id is not None and not isinstance(requested_folder_id, str):
        raise HTTPException(status_code=400, detail="folder_id must be a string")
    if isinstance(requested_folder_id, str):
        requested_folder_id = requested_folder_id.strip() or None
    file_edit_path = body.get("file_edit_path")
    raw_file_edit_enabled = body.get("file_edit_enabled", False)
    if not isinstance(raw_file_edit_enabled, bool):
        raise HTTPException(status_code=400, detail="file_edit_enabled must be a boolean")
    file_edit_enabled = raw_file_edit_enabled or bool(file_edit_path)
    if file_edit_enabled:
        try:
            if file_edit_path:
                result = await file_editor.start(
                    file_edit_path,
                    cwd=body.get("cwd", ""),
                    model=model,
                    provider_id=requested_provider_id,
                    reasoning_effort=requested_effort,
                    persistent=True,
                    node_id=node_id,
                )
            else:
                result = await file_editor.start_empty(
                    cwd=body.get("cwd", ""),
                    model=model,
                    provider_id=requested_provider_id,
                    reasoning_effort=requested_effort,
                    persistent=True,
                    node_id=node_id,
                )
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        session = await _session_lite(result["session_id"]) or {}
        if capability_contexts:
            session = await asyncio.to_thread(
                session_manager.set_capability_contexts,
                result["session_id"], capability_contexts,
            ) or session
        await asyncio.to_thread(config_store.apply_env_vars)
        if result.get("user_ask") is not None:
            await synthetic_messages.append_assistant_message(
                result["session_id"],
                content=result["user_ask"],
                source="file_editor",
            )
            session = await _session_lite(result["session_id"]) or session
        elif result.get("meta_prompt") is not None:
            await synthetic_messages.inject(
                coordinator,
                result["session_id"],
                prompt=result["meta_prompt"],
                model=session.get("model") or "",
                cwd=session.get("cwd") or "",
                orchestration_mode=session.get("orchestration_mode") or "native",
                source="file_editor",
                capability_contexts=capability_contexts,
            )
        if session_store.should_auto_register_project(session):
            try:
                await asyncio.to_thread(
                    project_store.add_project,
                    session["cwd"],
                    node_id=session.get("node_id") or "primary",
                )
                await _broadcast_projects_changed()
            except Exception as e:
                logger.warning("auto add_project failed: %s", e)
        logger.info("create_session %s mode=file_editing(persistent)", result["session_id"][:8])
        await _apply_initial_session_folder(session.get("id"), requested_folder_id)
        return session
    browser_harness_enabled = (
        body.get("browser_harness_enabled", True)
        and _builtin_extension_runtime_ready(
            extension_store.extension_id_for_role('browser-harness')
        )
    )
    session = await asyncio.to_thread(
        session_manager.create,
        name=body.get("name", ""),
        model=model,
        cwd=body.get("cwd", ""),
        orchestration_mode=requested_orchestration_mode,
        source=requested_source,
        provider_id=requested_provider_id,
        reasoning_effort=requested_effort,
        permission=requested_permission or None,
        browser_harness_enabled=browser_harness_enabled,
        browser_harness_headless=body.get("browser_harness_headless", True),
        node_id=node_id,
        worker_creation_policy=worker_creation_policy,
        bare_config=bare_config,
        # The UI/CLI `POST /api/sessions` is always an explicit user
        # action — the user is aware of (and owns) this session.
        user_initiated=True,
        capability_contexts=capability_contexts,
        id=client_session_id,
    )
    backend_url = body.get("backend_url")
    if isinstance(backend_url, str) and (
        backend_url.startswith("http://127.0.0.1:")
        or backend_url.startswith("http://localhost:")
    ):
        session = await asyncio.to_thread(
            session_manager.set_backend_url,
            session["id"],
            backend_url.rstrip("/"),
        ) or session
    if session_store.should_auto_register_project(session):
        try:
            await asyncio.to_thread(
                project_store.add_project,
                session["cwd"],
                node_id=session.get("node_id") or "primary",
            )
            await _broadcast_projects_changed()
        except Exception as e:
            logger.warning("auto add_project failed: %s", e)
    if body.get("model"):
        await _record_last_model(session.get("provider_id"), session.get("model"))
    if requested_effort:
        await _record_last_reasoning_effort(
            session.get("provider_id"), session.get("reasoning_effort"),
        )
    logger.info("create_session %s mode=%s", session["id"][:8], session.get("orchestration_mode"))
    await _apply_initial_session_folder(session.get("id"), requested_folder_id)
    return session


@app.post("/api/sessions/{session_id}/fork")
async def fork_session_endpoint(session_id: str, body: dict = Body(default={})):
    try:
        return await asyncio.to_thread(
            session_manager.fork,
            session_id,
            name=(body or {}).get("name"),
        )
    except KeyError:
        raise HTTPException(status_code=404, detail=t("error.session_not_found"))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/sessions/{session_id}/fork_and_send")
async def fork_and_send(session_id: str, body: dict = Body(default={})):
    """Atomic fork-then-submit. Forks `session_id` off its current claude
    head, then enqueues `prompt` against the new child via the same
    coordinator path the WS send_message handler uses. The processor
    replaces ws_callback at dequeue time with a registry-based dispatcher,
    so any client that subscribes to the child's session id over WS will
    receive its live events."""
    body = body or {}
    prompt = (body.get("prompt") or "").strip()
    images = body.get("images") or []
    if not prompt and not images:
        raise HTTPException(status_code=400, detail=t("error.prompt_required"))
    try:
        child = await asyncio.to_thread(session_manager.fork, session_id, name=body.get("name"))
    except KeyError:
        raise HTTPException(status_code=404, detail=t("error.session_not_found"))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Sample provider env so the new fork's CLI spawn inherits the right
    # ANTHROPIC_API_KEY / BASE_URL / CONFIG_DIR. Mirrors the WS handler.
    await asyncio.to_thread(config_store.apply_env_vars)

    child_id = child["id"]
    images = body.get("images") or None
    await coordinator.submit_prompt_async(child_id, {
        "prompt": prompt,
        "app_session_id": child_id,
        "model": body.get("model") or child.get("model"),
        "cwd": body.get("cwd") or child.get("cwd"),
        "ws_callback": None,
        "images": images,
        "orchestration_mode": body.get("orchestration_mode") or child.get("orchestration_mode"),
        "client_id": body.get("client_id"),
    })
    return {
        "child": child,
        "fork_point_seq": child.get("fork_point_seq"),
    }


@app.post("/api/sessions/{session_id}/close_fork")
async def close_fork(session_id: str):
    sess = await asyncio.to_thread(session_manager.set_fork_closed, session_id, True)
    if sess is None:
        raise HTTPException(status_code=404, detail=t("error.session_not_found"))
    return {"id": session_id, "fork_closed": True}


@app.post("/api/sessions/{session_id}/reopen_fork")
async def reopen_fork(session_id: str):
    """Inverse of /close_fork — flips `fork_closed` back to false so the
    pane becomes focusable and able to receive new prompts again. The
    pane was never destroyed; it just gets unlocked."""
    sess = await asyncio.to_thread(session_manager.set_fork_closed, session_id, False)
    if sess is None:
        raise HTTPException(status_code=404, detail=t("error.session_not_found"))
    return {"id": session_id, "fork_closed": False}


@app.post("/api/internal/supervisor/default-prompt")
async def internal_supervisor_default_prompt(_body: dict | None = Body(default=None)):
    _require_builtin_runtime_extension(extension_store.extension_id_for_role('supervisor'))
    from orchs.supervisor._verdict import DEFAULT_SUPERVISOR_CUSTOM_PROMPT
    return {"prompt": DEFAULT_SUPERVISOR_CUSTOM_PROMPT}


@app.post("/api/internal/supervisor/toggle")
async def internal_supervisor_toggle(body: dict = Body(...)):
    _require_builtin_runtime_extension(extension_store.extension_id_for_role('supervisor'))
    session_id = str(body.get("session_id") or "").strip()
    if not session_id:
        return {"success": False, "status": 400, "error": "session_id is required"}
    enabled = bool(body.get("enabled", False))
    custom_prompt = body.get("custom_prompt")
    if custom_prompt is not None and not isinstance(custom_prompt, str):
        return {"success": False, "status": 400, "error": "custom_prompt must be a string"}
    session = await asyncio.to_thread(
        session_manager.set_supervisor_enabled,
        session_id,
        enabled,
        custom_prompt=custom_prompt if enabled else None,
    )
    if session is None:
        return {"success": False, "status": 404, "error": t("error.session_not_found")}
    return {"success": True, "session": session}


@app.post("/api/internal/supervisor/review-last-work")
async def internal_supervisor_review_last_work(body: dict = Body(...)):
    _require_builtin_runtime_extension(extension_store.extension_id_for_role('supervisor'))
    session_id = str(body.get("session_id") or "").strip()
    if not session_id:
        return {"success": False, "status": 400, "error": "session_id is required"}
    session = await _session_lite(session_id)
    if not session:
        return {"success": False, "status": 404, "error": t("error.session_not_found")}
    if not session.get("supervisor_enabled"):
        return {"success": False, "status": 409, "error": t("error.ws_review_supervisor_only")}
    await asyncio.to_thread(config_store.apply_env_vars)
    try:
        queued_id = await coordinator.submit_prompt_async(session_id, {
            "_review": True,
            "app_session_id": session_id,
        })
    except RuntimeError as e:
        return {"success": False, "status": 409, "error": str(e)}
    return {"success": True, "queued_id": queued_id}


@app.post("/api/internal/supervisor/separate")
async def internal_supervisor_separate(body: dict = Body(...)):
    _require_builtin_runtime_extension(extension_store.extension_id_for_role('supervisor'))
    session_id = str(body.get("session_id") or "").strip()
    if not session_id:
        return {"success": False, "status": 400, "error": "session_id is required"}
    if not await _session_exists(session_id):
        return {"success": False, "status": 404, "error": t("error.session_not_found")}
    if coordinator.turn_manager.has_active_runs(session_id):
        return {
            "success": False,
            "status": 409,
            "error": "cannot separate supervisor while a turn is queued or in flight",
        }
    try:
        new_session = await asyncio.to_thread(session_manager.separate_supervisor, session_id)
    except KeyError:
        return {"success": False, "status": 404, "error": t("error.session_not_found")}
    except ValueError as e:
        msg = str(e)
        if "in flight" in msg or "queued" in msg:
            return {"success": False, "status": 409, "error": msg}
        return {"success": False, "status": 422, "error": msg}
    return {
        "success": True,
        "new_session_id": new_session["id"],
        "session": new_session,
    }


async def _publish_worker_fanout_required(
    session_id: str,
    *,
    op_label: str,
    caller_scope: bool,
    remove_worker: bool,
    outer_log_msg: str,
) -> None:
    await event_bus.publish(BusEvent(
        type="session.worker_fanout_required",
        root_id=session_id,
        sid=session_id,
        payload={
            "session_id": session_id,
            "op_label": op_label,
            "caller_scope": caller_scope,
            "remove_worker": remove_worker,
            "outer_log_msg": outer_log_msg,
        },
        persist=False,
    ))


@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str):
    # Per-step timing instrumentation. The handler chains 6 steps
    # serially; user-perceived "delete hangs forever" needs us to
    # identify WHICH step is the offender on the next reproduction.
    # Logs a single line per step with elapsed ms — emitted even on
    # exception so a hang followed by Ctrl+C still surfaces the
    # last-completed step.
    import time as _time
    _steps: list[tuple[str, float]] = []
    _t_total = _time.perf_counter()

    def _mark(label: str, t0: float) -> None:
        _steps.append((label, (_time.perf_counter() - t0) * 1000.0))

    try:
        virtual = await asyncio.to_thread(virtual_session_store.get, session_id)
        if virtual:
            try:
                deleted = await asyncio.to_thread(
                    virtual_session_store.delete,
                    str(virtual.get("extension_id") or ""),
                    session_id,
                )
            except PermissionError:
                raise HTTPException(status_code=403, detail="extension does not own this virtual session")
            if deleted:
                await coordinator.broadcast_global(
                    "session_deleted",
                    {"session_id": session_id},
                )
            return {"deleted": deleted}
        _t = _time.perf_counter()
        ok = await _delete_session_tree(session_id)
        _mark("delete_session_tree", _t)
        return {"deleted": ok}
    finally:
        total_ms = (_time.perf_counter() - _t_total) * 1000.0
        per_step = ", ".join(f"{name}={ms:.0f}ms" for name, ms in _steps)
        logger.info(
            "delete_session sid=%s total=%.0fms steps=[%s]",
            session_id, total_ms, per_step,
        )


@app.put("/api/sessions/{session_id}/rename")
async def rename_session(session_id: str, body: dict):
    new_name = strip_link_marker_syntax((body or {}).get("name", "").strip())
    if not new_name:
        return {"error": t("error.name_is_required_rename")}, 400
    virtual = await asyncio.to_thread(virtual_session_store.get, session_id)
    if virtual:
        virtual["name"] = new_name
        try:
            session = await asyncio.to_thread(
                virtual_session_store.upsert,
                str(virtual.get("extension_id") or ""),
                virtual,
            )
        except PermissionError:
            raise HTTPException(status_code=403, detail="extension does not own this virtual session")
        await coordinator.broadcast_global(
            "session_metadata_updated",
            {"session_id": session_id, "patch": {"name": session.get("name")}},
        )
        return {"id": session_id, "name": new_name}
    session = await asyncio.to_thread(session_manager.rename, session_id, new_name)
    if not session:
        return {"error": t("error.session_not_found_rename")}, 404
    # rename() refuses locked sessions (e.g. the assistant singleton) and
    # returns the unchanged record; surface that as a clear 403, not a silent
    # success that would let the UI believe the rename took.
    if session.get("name_locked") and session.get("name") != new_name:
        return {"error": "session name is locked", "name_locked": True}, 403
    return {"id": session_id, "name": new_name}


@app.put("/api/sessions/{session_id}/pin")
async def pin_session(session_id: str, body: dict):
    pinned = bool((body or {}).get("pinned", True))
    session = await asyncio.to_thread(session_manager.set_pinned, session_id, pinned)
    if not session:
        raise HTTPException(status_code=404, detail=t("error.session_not_found_retry"))
    return {"id": session_id, "pinned": pinned}


@app.post("/api/sessions/{session_id}/move-to-project")
async def move_session_to_project(session_id: str, body: dict):
    """Move a session to another project continuation-style: create a NEW
    session in the target project's cwd whose first turn gets a continuation
    handoff prompt pointing at the old session's provider-native transcript,
    stamp moved_to/moved_from pointers on both, and archive the old session.
    The old session's data is never physically moved."""
    target_cwd = str((body or {}).get("cwd") or "").strip()
    if not target_cwd:
        raise HTTPException(status_code=400, detail="cwd is required")
    expanded_cwd = os.path.realpath(os.path.expanduser(target_cwd))
    if not os.path.isdir(expanded_cwd):
        raise HTTPException(status_code=400, detail="cwd must be an existing directory")
    old = await asyncio.to_thread(session_manager.get, session_id)
    if not old:
        raise HTTPException(status_code=404, detail=t("error.session_not_found_retry"))
    if old.get("id") != session_id:
        raise HTTPException(status_code=400, detail="only root sessions can be moved")
    if old.get("moved_to_session_id"):
        raise HTTPException(status_code=409, detail="session was already moved")
    old_cwd = os.path.realpath(os.path.expanduser(str(old.get("cwd") or "")))
    if old_cwd == expanded_cwd:
        raise HTTPException(status_code=400, detail="session is already in that project")
    if coordinator.turn_manager.has_active_runs(session_id):
        raise HTTPException(
            status_code=409,
            detail="cannot move a session while a turn is running",
        )
    try:
        new_session = await asyncio.to_thread(
            session_manager.create,
            name=old.get("name") or "",
            model=old.get("model"),
            cwd=expanded_cwd,
            orchestration_mode=old.get("orchestration_mode") or "native",
            source=old.get("source") or "web",
            provider_id=old.get("provider_id"),
            reasoning_effort=old.get("reasoning_effort"),
            permission=old.get("permission"),
            browser_harness_enabled=bool(old.get("browser_harness_enabled", False)),
            browser_harness_headless=bool(old.get("browser_harness_headless", True)),
            node_id=old.get("node_id") or "primary",
            worker_creation_policy=old.get("worker_creation_policy") or "ask",
            bare_config=bool(old.get("bare_config", False)),
            user_initiated=True,
            capability_contexts=old.get("capability_contexts") or None,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    new_sid = new_session["id"]
    chain = [
        item.strip()
        for item in (old.get("continuation_chain") or [])
        if isinstance(item, str) and item.strip()
    ]
    old_agent_sid = str(old.get("agent_session_id") or "").strip()
    if old_agent_sid:
        chain.append(old_agent_sid)
    if chain:
        await asyncio.to_thread(
            session_manager.set_continuation_chain, new_sid, chain,
        )
    # Physically carry the source's render tree and summary snapshot into the
    # new session so the moved session shows its history instead of starting
    # blank. The first turn still gets a moved_project continuation handoff
    # (provider starts fresh; the handoff points it at the source transcript).
    await asyncio.to_thread(
        session_migrate.migrate_session_content, session_id, new_sid,
    )
    await asyncio.to_thread(
        session_manager.apply_migrated_fields, new_sid, old,
    )
    await asyncio.to_thread(session_manager.set_moved_from, new_sid, session_id)
    await asyncio.to_thread(session_manager.set_moved_to, session_id, new_sid)
    await asyncio.to_thread(session_manager.set_archived, session_id, True)
    await _broadcast_projects_changed()
    return await _session_lite(new_sid) or new_session


@app.put("/api/sessions/{session_id}/topbar-pin")
async def pin_session_to_topbar(session_id: str, body: dict):
    pinned = bool((body or {}).get("pinned", True))
    session = await asyncio.to_thread(session_manager.set_topbar_pinned, session_id, pinned)
    if not session:
        raise HTTPException(status_code=404, detail=t("error.session_not_found_retry"))
    return {
        "id": session_id,
        "topbar_pinned": pinned,
        "topbar_pinned_at": session.get("topbar_pinned_at"),
    }


@app.post("/api/sessions/{session_id}/opened")
async def mark_session_opened(session_id: str):
    """Stamp `last_opened_at` after a client opens this session's chat view.
    Server-generated timestamp; does not bump `updated_at`."""
    at = datetime.now().isoformat()
    session = await _run_hot_path(
        "session.opened.set_last_opened_at",
        session_manager.set_last_opened_at,
        session_id,
        at,
        return_session=False,
    )
    if not session:
        raise HTTPException(status_code=404, detail=t("error.session_not_found_retry"))
    return {"id": session_id, "last_opened_at": at}


@app.post("/api/sessions/{session_id}/unpin-others")
async def unpin_other_sessions(session_id: str, body: dict = Body(default={})):
    unpinned_ids = await asyncio.to_thread(session_manager.unpin_others, session_id)
    if unpinned_ids is None:
        raise HTTPException(status_code=404, detail=t("error.session_not_found_retry"))
    return {
        "id": session_id,
        "unpinned_ids": unpinned_ids,
        "count": len(unpinned_ids),
    }


@app.put("/api/sessions/{session_id}/agent_rename_allowed")
async def set_agent_rename_allowed(session_id: str, body: dict):
    value = bool((body or {}).get("agent_rename_allowed", False))
    session = await asyncio.to_thread(
        session_manager.set_agent_rename_allowed, session_id, value,
    )
    if not session:
        raise HTTPException(status_code=404, detail=t("error.session_not_found_retry"))
    return {"id": session_id, "agent_rename_allowed": value}


@app.put("/api/sessions/{session_id}/worker_eligible")
async def set_worker_eligible(session_id: str, body: dict):
    _require_builtin_runtime_extension(extension_store.extension_id_for_role('team-orchestration'))
    value = bool((body or {}).get("worker_eligible", True))
    session = await asyncio.to_thread(session_manager.set_worker_eligible, session_id, value)
    if not session:
        raise HTTPException(status_code=404, detail=t("error.session_not_found_retry"))
    return {"id": session_id, "worker_eligible": value}


@app.put("/api/sessions/{session_id}/worker_creation_policy")
async def set_worker_creation_policy(session_id: str, body: dict):
    _require_builtin_runtime_extension(extension_store.extension_id_for_role('team-orchestration'))
    policy = str((body or {}).get("worker_creation_policy") or "ask")
    if policy not in ("ask", "approve", "deny"):
        raise HTTPException(status_code=400, detail="worker_creation_policy must be ask, approve, or deny")
    session = await asyncio.to_thread(
        session_manager.set_worker_creation_policy,
        session_id,
        policy,
    )
    if not session:
        raise HTTPException(status_code=404, detail=t("error.session_not_found_retry"))
    return {"id": session_id, "worker_creation_policy": policy}


@app.put("/api/sessions/{session_id}/archive")
async def archive_session(session_id: str, body: dict):
    archived = bool((body or {}).get("archived", True))
    session = await asyncio.to_thread(session_manager.set_archived, session_id, archived)
    if not session:
        raise HTTPException(status_code=404, detail=t("error.session_not_found_retry"))
    return {"id": session_id, "archived": archived}


async def _resolve_selector_updates(session_id: str, body: dict) -> dict:
    """Parse a selectors payload into a validated `updates` dict (model,
    provider_id, reasoning_effort, permission, cwd). Shared by the public
    /selectors endpoint and the internal session-control tool so the agent
    applies selectors through the SAME validation + fail-closed path."""
    body = body or {}
    updates: dict = {}
    requested_provider_id = (
        body.get("provider_id").strip()
        if isinstance(body.get("provider_id"), str) and body.get("provider_id").strip()
        else None
    )
    provider_record = None
    if "provider_id" in body:
        if not requested_provider_id:
            raise HTTPException(status_code=400, detail="provider_id is required")
        provider_record = await asyncio.to_thread(
            config_store.get_provider,
            requested_provider_id,
        )
        if not provider_record:
            if config_store.provider_suspended(requested_provider_id):
                raise HTTPException(
                    status_code=409,
                    detail=t("error.provider_suspended", action="select for a session"),
                )
            raise HTTPException(status_code=400, detail="Unknown provider")
        _provider_not_suspended(requested_provider_id, action="select for a session")
        updates["provider_id"] = requested_provider_id
    if "model" in body and isinstance(body["model"], str) and body["model"].strip():
        requested_model = body["model"].strip()
        provider_for_model = (
            requested_provider_id
            or ((await _session_lite(session_id)) or {}).get("provider_id")
        )
        # Fail closed: with no resolvable provider, `available_models(None)`
        # would validate against the DEFAULT provider — letting a foreign
        # model (e.g. glm-5.2 onto a Claude session) slip through. Reject.
        if not provider_for_model:
            raise HTTPException(
                status_code=400, detail=t("error.session_not_found_retry"),
            )
        await asyncio.to_thread(
            _validate_provider_model, provider_for_model, requested_model, True,
        )
        updates["model"] = requested_model
    elif "provider_id" in updates:
        updates["model"] = await _model_for_provider_switch(
            requested_provider_id,
            provider_record or {},
        )
    if "reasoning_effort" in body:
        requested_effort = _api_reasoning_effort(body.get("reasoning_effort"))
        if requested_effort is None:
            raise HTTPException(status_code=400, detail="reasoning_effort is required")
        provider_for_effort = (
            requested_provider_id
            or ((await _session_lite(session_id)) or {}).get("provider_id")
        )
        updates["reasoning_effort"] = _provider_reasoning_effort(
            provider_for_effort, requested_effort,
        )
    if "permission" in body:
        requested_permission = _api_permission(body.get("permission"))
        if requested_permission is None:
            raise HTTPException(status_code=400, detail="permission is required")
        provider_for_permission = (
            requested_provider_id
            or ((await _session_lite(session_id)) or {}).get("provider_id")
        )
        updates["permission"] = _provider_permission(
            provider_for_permission, requested_permission,
        )
    if "cwd" in body and isinstance(body["cwd"], str) and body["cwd"].strip():
        updates["cwd"] = body["cwd"].strip()
    if requested_provider_id:
        if "reasoning_effort" not in updates:
            updates["reasoning_effort"] = (
                (provider_record or {}).get("default_reasoning_effort") or ""
            )
        if "permission" not in updates:
            updates["permission"] = (
                (provider_record or {}).get("default_permission") or {}
            )
    if "orchestration_mode" in body:
        raise HTTPException(
            status_code=409,
            detail=t("error.orchestration_mode_frozen"),
        )
    if not updates:
        raise HTTPException(status_code=400, detail=t("error.no_recognized_selector"))
    return updates


@app.patch("/api/sessions/{session_id}/selectors")
async def update_session_selectors(session_id: str, body: dict):
    """Persist selector changes (model, cwd, provider_id) to the
    session record.

    `orchestration_mode` is FROZEN at session creation time and cannot
    be changed here — flipping modes mid-session orphans the claude
    session under the old mode.

    `provider_id` IS mutable at any time. If it changes while a turn is
    in flight, the current run keeps using the provider instance that
    already owns that run; the changed selector is applied lazily to the
    next prompt via continuation. `set_selectors` re-validates
    `orchestration_mode` against the new provider's capability (e.g.
    Claude→Gemini on a manager-mode session fails here with
    `IncompatibleOrchestrationMode` → 400)."""
    body = body or {}
    updates = await _resolve_selector_updates(session_id, body)
    client_id = body.get("client_id") if isinstance(body.get("client_id"), str) else None
    before = await asyncio.to_thread(session_manager.get, session_id)
    session = await asyncio.to_thread(
        session_manager.set_selectors,
        session_id, client_id=client_id, **updates,
    )
    if not session:
        raise HTTPException(status_code=404, detail=t("error.session_not_found_retry"))
    _record_model_switched_event(session_id, before or {}, session, updates)
    if "model" in updates:
        await _record_last_model(session.get("provider_id"), updates["model"])
    if "reasoning_effort" in updates and updates["reasoning_effort"]:
        await _record_last_reasoning_effort(
            session.get("provider_id"), updates["reasoning_effort"],
        )
    return {"id": session_id, "updates": updates}


@app.post("/api/sessions/{session_id}/project-suggestion")
async def project_suggestion(session_id: str, body: dict):
    """Read-only: does this prompt look like it belongs to a different
    project than the session's current cwd? Returns a switch suggestion
    or null. Called pre-send on a fresh session so the user can move it
    to the right project before the first turn spawns a CLI in the wrong
    cwd (cheap to move before it starts, costly after)."""
    body = body or {}
    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise HTTPException(status_code=400, detail=t("error.ws_empty_prompt"))
    session = await _session_lite(session_id)
    if not session:
        raise HTTPException(status_code=404, detail=t("error.session_not_found_retry"))
    current_cwd = session.get("cwd")
    if not current_cwd:
        return {"suggestion": None}
    if not _project_match_ready or _project_match_executor is None:
        _ensure_project_match_warm_task()
        return {"suggestion": None}
    from project_match.worker import suggest_project_payload

    try:
        sugg = await asyncio.get_running_loop().run_in_executor(
            _project_match_executor,
            suggest_project_payload,
            prompt.strip(),
            current_cwd,
        )
    except Exception:
        logger.exception("project_match: suggestion failed")
        return {"suggestion": None}
    if not sugg:
        return {"suggestion": None}
    return {
        "suggestion": {
            "target_cwd": sugg["target_cwd"],
            "score": sugg["score"],
            "margin": sugg["margin"],
        }
    }


async def _project_match_warm_loop():
    """Build the project-match index once at startup (model load ~34s),
    then rebuild every 15 min so suggestions track new prompts. Runs in a
    dedicated process so model load/indexing cannot starve uvicorn's event loop."""
    global _project_match_ready
    from project_match.worker import rebuild_index

    fingerprint = None
    while True:
        try:
            if _project_match_executor is None:
                logger.warning("project_match: executor unavailable")
                return
            result = await asyncio.get_running_loop().run_in_executor(
                _project_match_executor,
                rebuild_index,
                fingerprint,
            )
            fingerprint = result.get("fingerprint") if isinstance(result, dict) else None
            _project_match_ready = True
            if isinstance(result, dict) and result.get("rebuilt") is False:
                logger.info("project_match: index unchanged; rebuild skipped")
            else:
                logger.info("project_match: index rebuilt")
        except Exception:
            _project_match_ready = False
            logger.exception("project_match: index rebuild failed")
        await asyncio.sleep(15 * 60)


def _ensure_project_match_warm_task() -> None:
    global _project_match_warm_task
    if _project_match_warm_task is not None and not _project_match_warm_task.done():
        return
    _ensure_project_match_executor()
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _project_match_warm_task = loop.create_task(
        _project_match_warm_loop(),
        name="project-match-warm",
    )


@app.get("/api/processes")
async def list_processes():
    """List all active runner processes across every loaded provider."""
    from provider import known_providers
    runs: list[dict] = []
    for prov in known_providers():
        runs.extend(prov.active_runs())
    return {"processes": runs}


@app.get("/api/sessions/{session_id}/runs/{run_id}/details")
async def get_run_details(session_id: str, run_id: str):
    """Diagnostic snapshot for one in-flight run — answers "why is this
    still considered running?".

    Returns the run_state entry the orchestrator holds for this run
    (kind, started_at, last_event_at, target_message_id, pid) plus the
    full descendant process tree (pid/ppid/state/CPU%/RSS/elapsed/cmd)
    walked from the runner PID. Also includes provider-side metadata
    (jsonl_path, run_dir, cancelled, popen_alive) when the provider
    that owns ``run_id`` still has bookkeeping for it.

    Computed on demand — heavy (forks ps/pgrep), so the frontend pulls
    on modal open + an explicit refresh, never on WS.
    """
    from provider import known_providers
    from process_inspect import inspect_process_tree

    runs = coordinator.turn_manager.get_run_state(session_id)
    entry = next((r for r in runs if r.get("run_id") == run_id), None)
    if entry is None:
        raise HTTPException(
            status_code=404, detail="run not found for this session",
        )
    pid = entry.get("pid")

    # Provider-side metadata — search every loaded provider for this
    # run_id. Each provider keeps its own RunState shape, so we read
    # defensively.
    provider_info: Optional[dict] = None
    for prov in known_providers():
        rs = getattr(prov, "_runs", {}).get(run_id)
        if rs is None:
            continue
        popen = getattr(rs, "popen", None)
        jsonl_path = getattr(rs, "jsonl_path", None)
        run_dir = getattr(rs, "run_dir", None)
        provider_info = {
            "provider_kind": getattr(prov, "KIND", None),
            "mode": getattr(rs, "mode", None),
            "session_id": getattr(rs, "session_id", None),
            "jsonl_path": str(jsonl_path) if jsonl_path else None,
            "run_dir": str(run_dir) if run_dir else None,
            "cancelled": getattr(rs, "cancelled", False),
            "popen_alive": (
                popen.poll() is None if popen is not None else None
            ),
            "popen_pid": getattr(popen, "pid", None) if popen else None,
        }
        break

    processes = await asyncio.to_thread(inspect_process_tree, pid, run_id)

    return {
        "run_id": run_id,
        "app_session_id": session_id,
        "kind": entry.get("kind"),
        "target_message_id": entry.get("target_message_id"),
        "delegation_id": entry.get("delegation_id"),
        "pid": pid,
        "started_at": entry.get("started_at"),
        "last_event_at": entry.get("last_event_at"),
        "provider": provider_info,
        "processes": processes,
    }


@app.post("/api/sessions/{session_id}/stop")
async def stop_session_turn(session_id: str):
    cancelled = await coordinator.turn_manager.cancel_turn(session_id)
    if not cancelled:
        raise HTTPException(
            status_code=409,
            detail=t("error.ws_no_active_turn_to_stop"),
        )
    return {"stopped": True}


@app.get("/api/sessions/{session_id}/details")
async def get_session_details(session_id: str):
    """Session-level "Details" snapshot for the menu panel: the live
    monitoring state, the provenance log (what ran + WHY), and the
    escape-proof process tree across all of the session's runs.

    Computed on demand (forks ps); the frontend pulls on panel open and
    refetches on the `session_monitoring_changed` /
    `session_provenance_changed` WS pings.
    """
    from process_inspect import inspect_process_tree
    from stores import provenance_store
    from containment import containment

    runs = coordinator.turn_manager.get_run_state(session_id)
    trees = []
    for entry in runs:
        rid, pid = entry.get("run_id"), entry.get("pid")
        procs = await asyncio.to_thread(inspect_process_tree, pid, rid)
        trees.append({
            "run_id": rid,
            "kind": entry.get("kind"),
            "pid": pid,
            "started_at": entry.get("started_at"),
            "processes": procs,
        })
    return {
        "session_id": session_id,
        "monitoring_state": coordinator.turn_manager.monitoring_state(session_id),
        "tracking_guaranteed": containment().guaranteed,
        "provenance": provenance_store.read(session_id, limit=500),
        "runs": trees,
    }


@app.get("/api/sessions/{session_id}/changes")
async def get_session_changes(session_id: str):
    """Every file edit made in this session + the reasoning that preceded
    each, projected from the provenance log and grouped by the user→assistant
    turn that produced them. Backend owns the filter (file-edit detection
    across providers) and the turn grouping (it owns the render tree); the
    Changes right-panel just renders. Refetched on the
    ``session_provenance_changed`` WS ping."""
    from stores import provenance_store

    def _build():
        changes = provenance_store.read_file_changes(session_id)
        sess = session_manager.get(session_id) or {}
        messages = sess.get("messages") or []
        return provenance_store.group_changes_by_turn(messages, changes)

    turns = await asyncio.to_thread(_build)
    return {"session_id": session_id, "turns": turns}


@app.post("/api/sessions/{session_id}/seen")
async def mark_session_seen(session_id: str, body: Optional[dict] = None):
    """Frontend ack: the user has viewed this session. Persists
    `last_seen_event_uid` on the Session record and resets the
    transient unread counter to 0. Fires `session_unread_changed`
    (broadcast_global) so every open client (multi-tab) converges.

    Body: `{"uid": "<event-uuid>" | null}`. `null` / missing → ack the
    current head (mutator picks the most recent UUID found by walking
    msg.events). Idempotent — a re-ack with the same uid simply re-fires
    the WS frame.
    """
    uid = (body or {}).get("uid")
    sess = await asyncio.to_thread(
        session_manager.mark_seen,
        session_id,
        uid if isinstance(uid, str) and uid else None,
    )
    if sess is None:
        raise HTTPException(status_code=404, detail=t("error.session_not_found"))
    return {
        "session_id": session_id,
        "last_seen_event_uid": sess.get("last_seen_event_uid"),
        "unread_count": 0,
    }


@app.post("/api/sessions/{session_id}/unread")
async def mark_session_unread(session_id: str):
    """Frontend action: force this session into the "has new" state.
    Clears `last_seen_event_uid` and recomputes the unread set so the
    sidebar badge appears, persisting it. Fires `session_unread_changed`
    (broadcast_global) so every open client converges.
    """
    sess = await asyncio.to_thread(session_manager.mark_unread, session_id)
    if sess is None:
        raise HTTPException(status_code=404, detail=t("error.session_not_found"))
    return {
        "session_id": session_id,
        "last_seen_event_uid": sess.get("last_seen_event_uid"),
        "unread_count": session_manager.get_unread_count(session_id),
    }


@app.get("/healthz")
async def healthz():
    """Liveness probe used by the frontend to poll for backend availability
    after triggering /api/admin/restart. Intentionally tiny — no I/O, no
    session/provider touches — so it answers the moment the event loop is
    up after a self-restart.
    """
    return {"ok": True}


@app.get("/api/build-info")
async def build_info():
    """Returns backend version and the latest supervised refresh result."""
    def _read_refresh_result() -> dict | None:
        result_path = ba_home() / "refresh_result.json"
        try:
            return json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    refresh_result = await asyncio.to_thread(_read_refresh_result)
    return {"git_hash": _GIT_HASH, "refresh_result": refresh_result}


def _valid_refresh_request_id(request_id: str) -> bool:
    return (
        1 <= len(request_id) <= 100
        and all(char.isalnum() or char in "-_" for char in request_id)
    )


def _refresh_acceptance_path() -> Path:
    return ba_home() / "refresh_request_accepted.json"


def _read_refresh_result_for(request_id: str) -> dict | None:
    result_path = ba_home() / "refresh_result.json"
    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if result.get("request_id") != request_id:
        return None
    return result


def _read_refresh_acceptance_for(request_id: str) -> dict | None:
    try:
        accepted = json.loads(_refresh_acceptance_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if accepted.get("request_id") != request_id:
        return None
    return accepted


@app.get("/api/admin/restart-status/{request_id}")
async def admin_restart_status(request_id: str):
    if not _valid_refresh_request_id(request_id):
        raise HTTPException(status_code=400, detail="Invalid restart request id.")
    accepted = await asyncio.to_thread(_read_refresh_acceptance_for, request_id)
    result = await asyncio.to_thread(_read_refresh_result_for, request_id)
    return {
        "request_id": request_id,
        "accepted": accepted is not None or result is not None,
        "refresh_result": result,
    }


_FRONTEND_LOG_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warn": logging.WARNING,
    "error": logging.ERROR,
}
_FRONTEND_LOG_MAX = 16384


def _clip(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return value[:limit]


@app.post("/api/logs/frontend")
async def frontend_log(request: Request):
    try:
        body = await request.json()
    except Exception:
        return {"ok": True, "dropped": True}
    if not isinstance(body, dict):
        return {"ok": True, "dropped": True}

    level = body.get("level")
    log_level = _FRONTEND_LOG_LEVELS.get(level if isinstance(level, str) else "", logging.ERROR)
    source = _clip(body.get("source"), 128) or "unknown"
    message = redact_secrets(_clip(body.get("message"), _FRONTEND_LOG_MAX))
    stack = redact_secrets(_clip(body.get("stack"), _FRONTEND_LOG_MAX))
    url = redact_secrets(_clip(body.get("url"), 2048))

    line = f"[{source}] {message}"
    if url:
        line += f" | url={url}"
    if stack:
        line += f"\n{stack}"
    _frontend_log_off_loop(log_level, line)
    return {"ok": True}


@app.get("/api/mobile/bundle/manifest")
async def mobile_bundle_manifest():
    """Current web-bundle version for the Capacitor OTA updater. Gated by
    the normal auth middleware (the JS caller sends the bearer header)."""
    import mobile_bundle
    info = await asyncio.to_thread(mobile_bundle.build_bundle, frontend_dist_dir())
    if not info:
        raise HTTPException(status_code=503, detail="web bundle unavailable")
    return {
        "version": info["version"],
        "checksum": info["checksum"],
        "download_path": (
            "/api/mobile/bundle/download?ticket="
            + mobile_bundle_ticket.create_ticket(info["version"], info["checksum"])
        ),
    }


@app.get("/api/mobile/bundle/download")
async def mobile_bundle_download(ticket: str = Query(default="")):
    """Serve the current web bundle as a zip for the Capacitor updater.

    Public-listed because the native GET cannot send Authorization. Access is
    limited by a short-lived capability bound to the exact bundle bytes."""
    import mobile_bundle
    info = await asyncio.to_thread(mobile_bundle.build_bundle, frontend_dist_dir())
    if not info:
        raise HTTPException(status_code=503, detail="web bundle unavailable")
    if not ticket or not mobile_bundle_ticket.verify_ticket(
        ticket, info["version"], info["checksum"],
    ):
        raise HTTPException(status_code=401, detail="invalid bundle ticket")
    return FileResponse(
        info["path"],
        media_type="application/zip",
        filename=f"{info['version']}.zip",
    )


@app.post("/api/admin/restart")
async def admin_restart(body: dict | None = None):
    """Ask the run.sh supervisor to rebuild the frontend and restart.

    The request id is persisted in the restart flag. run.sh starts the new
    backend and waits until it is healthy, then builds the frontend atomically
    and records success/failure for the reloaded UI.

    Runner processes survive: SIGTERM (not SIGINT) leaves the
    `_intentional_shutdown` flag false, so `on_shutdown` skips
    `provider.cancel_all` and run_recovery re-attaches the still-alive
    runners on the next boot.
    """
    if get_env("BETTER_CLAUDE_RUN_SH_SUPERVISOR") != "1":
        raise HTTPException(
            status_code=409,
            detail="In-app refresh requires the run.sh supervisor.",
        )

    raw_request_id = (body or {}).get("request_id")
    request_id = str(raw_request_id) if raw_request_id is not None else ""
    if not _valid_refresh_request_id(request_id):
        request_id = str(uuid.uuid4())

    mode = str((body or {}).get("mode") or "now")
    if mode not in {"now", "idle"}:
        raise HTTPException(status_code=400, detail="Invalid restart mode.")

    if mode == "idle":
        await _wait_for_all_agents_idle()

    restarted_nodes = await _restart_connected_worker_nodes()
    await _trigger_supervisor_restart(request_id)
    return {
        "status": "rebuilding",
        "request_id": request_id,
        "restarted_nodes": restarted_nodes,
    }


@app.post("/api/internal/switch-restart")
async def internal_switch_restart(
    body: dict | None = None,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    """Restart trigger for control-plane extensions (line switching).

    Same supervisor contract as /api/admin/restart, but authenticated with an
    extension internal token. Fail closed: only an active extension that was
    consented as a supervisor-daemon owner may restart the backend."""
    if not coordinator.is_internal_caller(x_internal_token):
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    caller = coordinator.principal_extension_id(x_internal_token) or ""
    if not caller or not extension_store.is_extension_active(caller):
        raise HTTPException(status_code=403, detail="calling extension is not active")
    manifest = (extension_store.get_extension(caller) or {}).get("manifest") or {}
    if (manifest.get("permissions") or {}).get("daemons") != "supervisor":
        raise HTTPException(status_code=403, detail="extension lacks supervisor daemon consent")
    if get_env("BETTER_CLAUDE_RUN_SH_SUPERVISOR") != "1":
        raise HTTPException(
            status_code=409,
            detail="Line switching requires the run.sh supervisor.",
        )
    raw_request_id = (body or {}).get("request_id")
    request_id = str(raw_request_id) if raw_request_id is not None else ""
    if not _valid_refresh_request_id(request_id):
        request_id = str(uuid.uuid4())
    restarted_nodes = await _restart_connected_worker_nodes()
    await _trigger_supervisor_restart(request_id)
    return {"status": "rebuilding", "request_id": request_id, "restarted_nodes": restarted_nodes}


async def _trigger_supervisor_restart(request_id: str) -> None:
    """Write the restart flag and SIGTERM uvicorn so the run.sh supervisor
    rebuilds the frontend and restarts the backend. Caller is responsible
    for the supervisor-env guard (`BETTER_CLAUDE_RUN_SH_SUPERVISOR=1`) —
    restarting without the supervisor would just kill the server with
    nothing to respawn it.

    Runner processes survive: SIGTERM (not SIGINT) leaves
    `_intentional_shutdown` false, so `on_shutdown` skips
    `provider.cancel_all` and run_recovery re-attaches the still-alive
    runners on the next boot."""
    accepted_payload = {
        "request_id": request_id,
        "accepted_at": datetime.now().astimezone().isoformat(),
    }
    await asyncio.to_thread(
        _refresh_acceptance_path().write_text,
        json.dumps(accepted_payload),
        "utf-8",
    )

    flag = ba_home() / "restart_requested"
    await asyncio.to_thread(flag.write_text, request_id, encoding="utf-8")
    pid = os.getpid()

    async def _restart():
        # Give uvicorn time to flush the JSON response before terminating.
        await asyncio.sleep(0.3)
        os.kill(pid, signal.SIGTERM)

    asyncio.create_task(_restart())


async def _wait_for_all_agents_idle() -> None:
    while True:
        await asyncio.to_thread(coordinator.turn_manager._refresh_cache)
        if not _has_restart_blocking_agent_work():
            return
        await asyncio.sleep(1.0)


def _has_restart_blocking_agent_work() -> bool:
    if session_manager.has_any_queued_prompts():
        return True
    if requirements_async_jobs.has_active_jobs():
        return True

    active_sids = set(coordinator.turn_manager.active_run_ids.keys())
    active_sids.update(getattr(coordinator, "_in_flight_prompts", {}).keys())
    active_sids.update(getattr(coordinator, "_prompt_queues", {}).keys())
    active_sids.update(coordinator.turn_manager._run_state.keys())
    return any(coordinator.turn_manager.has_active_runs(sid) for sid in active_sids)


def _system_busy_for_auto_restart() -> bool:
    """Fresh snapshot of "is any agent work running right now", for the
    auto-restart-on-idle monitor. Refreshes the turn-manager cache first so
    dead PIDs are pruned before the check."""
    coordinator.turn_manager._refresh_cache()
    return _has_restart_blocking_agent_work()


_auto_restart_on_idle_monitor = auto_restart_on_idle.AutoRestartOnIdleMonitor(
    is_busy=_system_busy_for_auto_restart,
    trigger_restart=_trigger_supervisor_restart,
    is_enabled=user_prefs.get_auto_restart_on_idle,
)


async def _restart_connected_worker_nodes() -> list[str]:
    import node_link
    import node_store

    restarted: list[str] = []
    for node in node_store.snapshot():
        if node.get("role") != "worker_node" or node.get("state") != "connected":
            continue
        node_id = node.get("id")
        if not isinstance(node_id, str) or not node_id:
            continue
        try:
            await node_link.send_restart(node_id)
        except node_link.NodeOffline:
            continue
        restarted.append(node_id)
    return restarted


@app.post("/api/sessions/{session_id}/rewind")
async def rewind_session(session_id: str, body: dict):
    message_id = (body or {}).get("message_id")
    if not message_id:
        raise HTTPException(status_code=400, detail=t("error.message_id_required"))
    try:
        result = await coordinator.rewind_files(session_id, message_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    # Rewind moves the Better Agent session's agent_sid lineage. Any per-pair
    # forks pointing at this session as a worker are now stale (their
    # parent has shifted) — drop them so the next delegation re-forks
    # from the rewound head.
    await _publish_worker_fanout_required(
        session_id,
        op_label="rewind",
        caller_scope=False,
        remove_worker=False,
        outer_log_msg="clear_forks_for_worker_everywhere failed during rewind",
    )
    return result


@app.post("/api/sessions/{session_id}/rewind_and_retry")
async def rewind_and_retry(session_id: str, body: dict):
    """Discard a stopped/failed turn and atomically retry it server-side.

    Body: `{"assistant_message_id": <id>, "client_id"?: <id>}`. The caller
    points at the failed assistant bubble; this endpoint locates the user
    message immediately preceding it, DURABLY re-enqueues its prompt (with
    images/model/cwd/orchestration context) through the normal queued-prompt
    path, then REWINDS the session to before that user message — removing
    the failed user+assistant pair (and any worker forks) — and submits the
    queued prompt as a fresh turn. The durable enqueue commits before the
    rewind, so a crash or a disconnected client can never lose the prompt:
    startup re-enqueue recovers any admitted-but-unprocessed prompt.

    The retried turn flows through the canonical prompt path, so
    persistence, validation, the `user_message_persisted` WS emit (echoing
    `client_id` for optimistic-bubble resolution), and turn start behave
    exactly like a fresh user message. The response only acks the enqueue —
    the client never resends the prompt itself.

    When the failed user message has a provider rewind anchor
    (`agent_message_uuid`) the provider CLI is rewound too; otherwise the
    prompt never committed there, so only the render tree is truncated.
    """
    body = body or {}
    asst_id = body.get("assistant_message_id")
    if not asst_id:
        raise HTTPException(status_code=400, detail=t("error.assistant_message_id_required"))

    sess = await _session_lite(session_id)
    if not sess:
        raise HTTPException(status_code=404, detail=t("error.session_not_found_retry"))

    msgs = sess.get("messages") or []
    asst_idx = next(
        (i for i, m in enumerate(msgs) if m.get("id") == asst_id and m.get("role") == "assistant"),
        None,
    )
    if asst_idx is None:
        raise HTTPException(status_code=404, detail=t("error.assistant_message_not_found"))

    user_idx = next(
        (i for i in range(asst_idx - 1, -1, -1) if msgs[i].get("role") == "user"),
        None,
    )
    if user_idx is None:
        raise HTTPException(
            status_code=400,
            detail=t("error.no_preceding_user_message"),
        )

    user_msg = msgs[user_idx]
    retry_prompt = user_msg.get("content") or ""
    retry_images = []
    for img in user_msg.get("images") or []:
        if not isinstance(img, dict):
            continue
        filename = img.get("filename")
        media_type = img.get("media_type")
        if not isinstance(filename, str) or not isinstance(media_type, str):
            continue
        img_path = resolve_session_image_path(session_id, filename)
        if not img_path.exists():
            raise HTTPException(status_code=500, detail=t("error.image_not_found"))
        import base64
        raw = await asyncio.to_thread(img_path.read_bytes)
        retry_images.append({
            "data": base64.b64encode(raw).decode("ascii"),
            "media_type": media_type,
        })

    client_id = body.get("client_id") if isinstance(body.get("client_id"), str) else None
    lifecycle_msg_id = new_lifecycle_msg_id()
    orchestration_mode = sess.get("orchestration_mode") or "team"
    qp_id = str(uuid.uuid4())
    queued_prompt = {
        "id": qp_id,
        "lifecycle_msg_id": lifecycle_msg_id,
        "content": retry_prompt,
        "kind": "send",
        "queue_position": 0,
        "images_count": len(retry_images),
        "files_count": 0,
        "images": retry_images or None,
        "files": None,
        "orchestration_mode": orchestration_mode,
        "send_target": None,
        "cli_prompt": None,
        "disallowed_tools": None,
        "disabled_builtin_extensions": None,
        "client_id": client_id,
        "alter_rewind_latest": False,
        "capability_contexts": [],
        "created_at": datetime.now().isoformat(),
    }
    # Durable enqueue FIRST: once admitted, the prompt survives any crash
    # (startup re-enqueue picks it up), so the rewind below can never open
    # a loss window for the user's intent.
    admission = await asyncio.to_thread(
        session_manager.admit_queued_prompt, session_id, queued_prompt,
    )
    if not admission.get("session"):
        raise HTTPException(status_code=404, detail=t("error.session_not_found_retry"))
    if admission.get("existing_user_message") or admission.get("existing_queued_prompt"):
        # Same-client_id retry already admitted/persisted — idempotent ack.
        return {
            "ok": True,
            "enqueued": False,
            "duplicate": True,
            "client_id": client_id,
        }

    try:
        try:
            await coordinator.rewind_files(session_id, user_msg["id"])
        except ValueError:
            # No provider rewind anchor (failed before commit) or rewind
            # unsupported — drop the failed pair from the render tree only
            # so the retry replaces the prompt instead of duplicating it.
            await coordinator.rewind_files(
                session_id, user_msg["id"], provider_rewind=False
            )
    except (ValueError, RuntimeError) as e:
        # Rewind failed — the failed pair is still in place, so drop the
        # queued retry to avoid running a duplicate of the prompt.
        await asyncio.to_thread(
            session_manager.remove_queued_prompt, session_id, qp_id,
        )
        raise HTTPException(status_code=500, detail=str(e))

    params = {
        "prompt": retry_prompt,
        "app_session_id": session_id,
        "model": sess.get("model"),
        "cwd": sess.get("cwd"),
        "ws_callback": None,
        "images": retry_images or None,
        "files": None,
        "orchestration_mode": orchestration_mode,
        "send_target": None,
        "client_id": client_id,
        "lifecycle_msg_id": lifecycle_msg_id,
        "cli_prompt": None,
        "disallowed_tools": None,
        "disabled_builtin_extensions": None,
        "capability_contexts": [],
        "_queued_id": qp_id,
    }
    try:
        await coordinator.submit_prompt_async(session_id, params)
    except HTTPException:
        raise
    except Exception as e:
        # Keep the durable queued prompt — startup re-enqueue recovers it —
        # but surface the submit failure to the caller.
        raise HTTPException(status_code=500, detail=str(e))

    return {
        "ok": True,
        "enqueued": True,
        "client_id": client_id,
        "lifecycle_msg_id": lifecycle_msg_id,
    }


@app.post("/api/sessions/{session_id}/pre-send-advisories")
async def pre_send_advisories_endpoint(session_id: str, body: dict):
    """Collect pre-send advisories from extensions declaring the
    ``pre_send_advisory`` hook. Advisories are signals shown to the user
    before a prompt is sent; collection failures never block sending."""
    body = body or {}
    sess = await _session_lite(session_id)
    if not sess:
        raise HTTPException(status_code=404, detail=t("error.session_not_found_retry"))
    provider_id = str(body.get("provider_id") or "").strip()
    provider = config_store.get_provider(provider_id) if provider_id else None
    provider_kind = str((provider or {}).get("kind") or "").strip()
    config_dir = str((provider or {}).get("config_dir") or "").strip()
    model = str(body.get("model") or "").strip()
    advisories = await pre_send_advisory.collect_pre_send_advisories(
        session_id,
        provider_id,
        provider_kind,
        config_dir,
        model,
        provider_mode=str((provider or {}).get("mode") or "").strip(),
        provider_base_url=str((provider or {}).get("base_url") or "").strip(),
        provider_name=str((provider or {}).get("name") or "").strip(),
    )
    return {"advisories": advisories}


@app.post("/api/sessions/{session_id}/rate-limit/continue")
async def continue_rate_limited_turn(session_id: str, body: dict):
    body = body or {}
    asst_id = body.get("assistant_message_id")
    if not asst_id:
        raise HTTPException(status_code=400, detail=t("error.assistant_message_id_required"))

    sess = await _session_lite(session_id)
    if not sess:
        raise HTTPException(status_code=404, detail=t("error.session_not_found_retry"))

    msgs = sess.get("messages") or []
    asst_idx = next(
        (i for i, m in enumerate(msgs) if m.get("id") == asst_id and m.get("role") == "assistant"),
        None,
    )
    if asst_idx is None:
        raise HTTPException(status_code=404, detail=t("error.assistant_message_not_found"))
    assistant_msg = msgs[asst_idx]
    if not assistant_msg.get("retrying_until"):
        raise HTTPException(status_code=409, detail="assistant message is not waiting on a rate limit")

    user_idx = next(
        (i for i in range(asst_idx - 1, -1, -1) if msgs[i].get("role") == "user"),
        None,
    )
    if user_idx is None:
        raise HTTPException(
            status_code=400,
            detail=t("error.no_preceding_user_message"),
        )

    updates = await _resolve_selector_updates(session_id, body)
    before = await asyncio.to_thread(session_manager.get, session_id)
    session = await asyncio.to_thread(
        session_manager.set_selectors,
        session_id,
        client_id=body.get("client_id") if isinstance(body.get("client_id"), str) else None,
        **updates,
    )
    if not session:
        raise HTTPException(status_code=404, detail=t("error.session_not_found_retry"))
    _record_model_switched_event(session_id, before or {}, session, updates)
    if "model" in updates:
        await _record_last_model(session.get("provider_id"), updates["model"])
    if updates.get("reasoning_effort"):
        await _record_last_reasoning_effort(
            session.get("provider_id"), updates["reasoning_effort"],
        )

    prompt = msgs[user_idx].get("content") or ""
    landed = coordinator.turn_manager.request_immediate_continuation(
        session_id,
        prompt,
        reason="rate_limit_provider_switch",
    )
    if not landed:
        session_manager.set_continuation_requested(
            session_id,
            prompt,
            reason="rate_limit_provider_switch",
            when="next_turn",
        )
    return {"ok": True, "session_id": session_id, "updates": updates, "when": "now" if landed else "next_turn"}


def _latest_user_message_index(messages: list[dict]) -> Optional[int]:
    for idx in range(len(messages) - 1, -1, -1):
        if messages[idx].get("role") == "user":
            return idx
    return None


def _normalize_ws_send_mode_for_turn_state(send_mode: str, is_queued: bool) -> str:
    if send_mode == "steer" and not is_queued:
        return "queue"
    return send_mode


def _fallback_ws_send_mode_after_failed_steer(send_mode: str) -> str:
    if send_mode == "steer":
        return "queue"
    return send_mode


def _parse_ws_disallowed_tools(value: object) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValueError("disallowed_tools must be an array")
    parsed = []
    for tool in value:
        if not isinstance(tool, str) or not tool.strip():
            raise ValueError("disallowed_tools entries must be non-empty strings")
        parsed.append(tool.strip())
    return parsed


def _parse_ws_disabled_builtin_extensions(value: object) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValueError("disabled_builtin_extensions must be an array")
    parsed = []
    for extension_id in value:
        if not isinstance(extension_id, str) or not extension_id.strip():
            raise ValueError("disabled_builtin_extensions entries must be non-empty strings")
        parsed.append(extension_id.strip())
    return parsed


async def _rewind_latest_user_for_alter(session_id: str) -> dict:
    sess = await _session_lite(session_id)
    if not sess:
        raise HTTPException(status_code=404, detail=t("error.session_not_found_retry"))
    messages = sess.get("messages") or []
    user_idx = _latest_user_message_index(messages)
    if user_idx is None:
        raise HTTPException(status_code=400, detail=t("error.no_preceding_user_message"))
    user_msg = messages[user_idx]
    try:
        rewind_result = await coordinator.rewind_files(
            session_id,
            user_msg["id"],
            semantic_alter=True,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    await _publish_worker_fanout_required(
        session_id,
        op_label="alter_latest",
        caller_scope=False,
        remove_worker=False,
        outer_log_msg="clear_forks_for_worker_everywhere failed during alter_latest",
    )
    return {
        "messages": rewind_result.get("messages") or [],
        "workers": rewind_result.get("workers"),
        "semantic_alter_previous_prompt": rewind_result.get("semantic_alter_previous_prompt"),
        "retry_model": sess.get("model"),
        "retry_cwd": sess.get("cwd"),
        "retry_orchestration_mode": sess.get("orchestration_mode") or "team",
    }


@app.post("/api/sessions/{session_id}/tags")
async def add_inline_tag(session_id: str, body: dict):
    await _require_session_async(session_id)
    tag = {
        "id": body["id"],
        "messageId": body["messageId"],
        "selectedText": body["selectedText"],
        "comment": body["comment"],
        "timestamp": body["timestamp"],
    }
    # File-anchored tags carry an extra `fileAnchor`. Two flavors:
    #   - Monaco selection (eng overlay or FileViewer Monaco view) →
    #     line:col fields present.
    #   - Rendered-DOM selection (FileViewer markdown / CSV / TSV) →
    #     line:col absent; only `filePath` + `selectedText` carry the
    #     positional info.
    file_anchor = body.get("fileAnchor")
    if isinstance(file_anchor, dict):
        anchor: dict = {"filePath": str(file_anchor.get("filePath", ""))}
        for key in ("startLine", "endLine", "startCol", "endCol"):
            val = file_anchor.get(key)
            if isinstance(val, (int, float)):
                anchor[key] = int(val)
        tag["fileAnchor"] = anchor
    await asyncio.to_thread(
        session_manager.add_tag,
        session_id, tag, client_id=body.get("client_id"),
    )
    return tag


@app.patch("/api/sessions/{session_id}/tags/{tag_id}")
async def update_inline_tag(
    session_id: str, tag_id: str,
    body: dict = Body(default={}),
    client_id: str = Query(None),
):
    await _require_session_async(session_id)
    comment = body.get("comment")
    if not isinstance(comment, str):
        return {"updated": False, "error": "comment must be a string"}
    await asyncio.to_thread(
        session_manager.update_tag,
        session_id, tag_id, {"comment": comment}, client_id=client_id,
    )
    return {"updated": True}


@app.delete("/api/sessions/{session_id}/tags/{tag_id}")
async def remove_inline_tag(
    session_id: str, tag_id: str, client_id: str = Query(None)
):
    await _require_session_async(session_id)
    await asyncio.to_thread(
        session_manager.remove_tag,
        session_id,
        tag_id,
        client_id=client_id,
    )
    return {"deleted": True}


@app.delete("/api/sessions/{session_id}/tags")
async def clear_inline_tags(session_id: str, client_id: str = Query(None)):
    await _require_session_async(session_id)
    await asyncio.to_thread(session_manager.clear_tags, session_id, client_id=client_id)
    return {"cleared": True}


# ── Notes ──────────────────────────────────────────────────────────

@app.post("/api/sessions/{session_id}/notes")
async def add_note(session_id: str, body: dict):
    await _require_session_async(session_id)
    text = body.get("text", "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Note text is required")
    sess = await asyncio.to_thread(
        session_manager.add_note,
        session_id, text, client_id=body.get("client_id"),
    )
    if not sess:
        raise HTTPException(status_code=404, detail=t("error.session_not_found_retry"))
    return {"notes": sess.get("notes", [])}


@app.delete("/api/sessions/{session_id}/notes/{note_id}")
async def remove_note(session_id: str, note_id: str, client_id: str = Query(None)):
    await _require_session_async(session_id)
    await asyncio.to_thread(
        session_manager.remove_note,
        session_id,
        note_id,
        client_id=client_id,
    )
    return {"deleted": True}


@app.patch("/api/sessions/{session_id}/notes/{note_id}")
async def update_note(session_id: str, note_id: str, body: dict):
    await _require_session_async(session_id)
    text = body.get("text", "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Note text is required")
    sess = await asyncio.to_thread(
        session_manager.update_note,
        session_id, note_id, text, client_id=body.get("client_id"),
    )
    if not sess:
        raise HTTPException(status_code=404, detail=t("error.session_not_found_retry"))
    return {"notes": sess.get("notes", [])}


# ── Right panel ────────────────────────────────────────────────────

# Single source for tab validation across the public PATCH and the
# internal POST endpoints. Add new tab ids here, not at each handler.
_VALID_RIGHT_PANEL_TABS = {"files", "notes", "canvas", "comments", "todos", "screen", "changes", "communications", "board"}
_VALID_RIGHT_PANEL_AUTO_REASONS = {"files", "notes", "canvas", "comments", "todos", "navigate", "screen", "communications", "board"}


def _optional_positive_int(body: dict, key: str) -> int | None:
    if key not in body:
        return None
    value = body.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise HTTPException(status_code=400, detail=f"{key} must be a positive integer")
    return value


def _right_panel_patch_from_body(body: dict) -> dict:
    patch: dict = {}
    if "open" in body:
        if not isinstance(body.get("open"), bool):
            raise HTTPException(status_code=400, detail="open must be a boolean")
        patch["open"] = body["open"]
    if "tab" in body:
        tab_val = body.get("tab")
        if tab_val is not None and tab_val not in _VALID_RIGHT_PANEL_TABS:
            raise HTTPException(status_code=400, detail=f"Invalid tab: {tab_val!r}")
        patch["tab"] = tab_val
        patch["tab_set"] = True
    width = _optional_positive_int(body, "width")
    if width is not None:
        patch["width"] = width
    mobile_height = _optional_positive_int(body, "mobile_height")
    if mobile_height is not None:
        patch["mobile_height"] = mobile_height
    if "todos_dismissed" in body:
        if not isinstance(body.get("todos_dismissed"), bool):
            raise HTTPException(status_code=400, detail="todos_dismissed must be a boolean")
        patch["todos_dismissed"] = body["todos_dismissed"]
    if "auto_opened_by" in body:
        reasons = body.get("auto_opened_by")
        if not isinstance(reasons, list) or any(
            not isinstance(reason, str) or reason not in _VALID_RIGHT_PANEL_AUTO_REASONS
            for reason in reasons
        ):
            raise HTTPException(status_code=400, detail="auto_opened_by contains an invalid reason")
        patch["auto_opened_by"] = list(dict.fromkeys(reasons))
    if "sidebar_minimized" in body:
        if not isinstance(body.get("sidebar_minimized"), bool):
            raise HTTPException(status_code=400, detail="sidebar_minimized must be a boolean")
        patch["sidebar_minimized"] = body["sidebar_minimized"]
    if not patch:
        raise HTTPException(status_code=400, detail="At least one right-panel field must be present")
    return patch


def _right_panel_response(sess: dict) -> dict:
    return {
        "right_panel_open": sess.get("right_panel_open"),
        "right_panel_active_tab": sess.get("right_panel_active_tab"),
        "right_panel_width": sess.get("right_panel_width"),
        "right_panel_mobile_height": sess.get("right_panel_mobile_height"),
        "right_panel_todos_dismissed": sess.get("right_panel_todos_dismissed"),
        "right_panel_auto_opened_by": list(sess.get("right_panel_auto_opened_by") or []),
        "sidebar_minimized": sess.get("sidebar_minimized"),
    }


@app.patch("/api/sessions/{session_id}/right-panel")
async def patch_right_panel(session_id: str, body: dict):
    """Update right-panel UI state (open/closed + active tab).

    Body: `{open?: bool, tab?: 'files'|'notes'|'canvas'|'comments',
    client_id: str}`. At least one of open/tab must be present.
    Echoes via `session_metadata_updated` (kind: right_panel_set);
    originating tab drops its own echo via `client_id` match."""
    await _require_session_async(session_id)
    patch = _right_panel_patch_from_body(body)
    sess = await asyncio.to_thread(
        session_manager.set_right_panel,
        session_id,
        **patch,
        client_id=body.get("client_id"),
    )
    if not sess:
        raise HTTPException(
            status_code=404, detail=t("error.session_not_found_retry"),
        )
    return _right_panel_response(sess)


@app.post("/api/internal/sessions/{session_id}/right-panel")
async def internal_set_right_panel(
    session_id: str,
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    """Internal-token-authed twin of PATCH /api/sessions/{id}/right-panel.

    Lets extensions (via better_agent_sdk.Client.set_right_panel) open
    the right panel and switch its active tab without holding a user
    cookie. Same validation rules and same broadcast as the public
    endpoint. The ``client_id`` echoed on the broadcast is pinned to the
    calling extension id — extensions cannot suppress another tab's
    echo by spoofing client_id."""
    if not coordinator.is_internal_caller(x_internal_token):
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    extension_id = coordinator.principal_extension_id(x_internal_token) or ""
    if not extension_id or not extension_store.is_extension_active(extension_id):
        raise HTTPException(status_code=403, detail="extension is not active")
    await _require_session_async(session_id)
    patch = _right_panel_patch_from_body(body)
    sess = await asyncio.to_thread(
        session_manager.set_right_panel,
        session_id,
        **patch,
        client_id=f"ext:{extension_id}",
    )
    if not sess:
        raise HTTPException(
            status_code=404, detail=t("error.session_not_found_retry"),
        )
    return _right_panel_response(sess)


@app.post("/api/sessions/{session_id}/adv_sync")
async def start_adv_sync_endpoint(session_id: str, body: dict = Body(default={})):
    """Kick off an adversarial-sync ping-pong for the selected text.

    Body: `{message_id, selected_text}`. Returns the created overlay
    record immediately; the ping-pong runs as a background task and
    publishes progress via `session_metadata_updated` WS frames on
    this session id."""
    await _require_session_async(session_id)
    body = body or {}
    message_id = (body.get("message_id") or "").strip()
    selected_text = body.get("selected_text") or ""
    if not message_id:
        raise HTTPException(status_code=400, detail="message_id required")
    if not selected_text.strip():
        raise HTTPException(status_code=400, detail="selected_text required")
    from orchs.adv_sync import start_adv_sync
    # Inherit the active provider env so the new forks' first CLI
    # spawn picks up the right ANTHROPIC_API_KEY/BASE_URL/CONFIG_DIR.
    await asyncio.to_thread(config_store.apply_env_vars)
    try:
        overlay = await start_adv_sync(
            coordinator,
            parent_session_id=session_id,
            message_id=message_id,
            selected_text=selected_text,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return overlay


@app.post("/api/sessions/{session_id}/adv_sync/{overlay_id}/cancel")
async def cancel_adv_sync_endpoint(session_id: str, overlay_id: str):
    await _require_session_async(session_id)
    from orchs.adv_sync import cancel_adv_sync
    cancelled = await cancel_adv_sync(
        coordinator,
        parent_session_id=session_id,
        overlay_id=overlay_id,
    )
    return {"cancelled": cancelled}


def _sanitize_file_panel(raw: dict) -> dict:
    """Build a persisted open-file-panel dict from request input.

    Shape: {id, path, focus?: {startLine,endLine},
    selection?: {startLine,endLine}}. `path` is required; focus /
    selection are optional integer line ranges (the agent-/user-
    requested scroll + highlight — NOT the user's live viewport,
    which stays frontend-transient)."""
    path = str(raw.get("path") or "").strip()
    if not path:
        raise HTTPException(status_code=400, detail=t("error.file_panel_path_required"))

    def _range(val) -> Optional[dict]:
        if not isinstance(val, dict):
            return None
        s, e = val.get("startLine"), val.get("endLine")
        if not isinstance(s, (int, float)) or not isinstance(e, (int, float)):
            return None
        return {"startLine": int(s), "endLine": int(e)}

    return {
        "id": str(raw.get("id") or uuid.uuid4().hex[:12]),
        "path": path,
        "focus": _range(raw.get("focus")),
        "selection": _range(raw.get("selection")),
    }


@app.post("/api/sessions/{session_id}/file-panels")
async def add_file_panel(session_id: str, body: dict):
    await _require_session_async(session_id)
    panel = _sanitize_file_panel(body)
    await asyncio.to_thread(
        session_manager.add_open_file_panel,
        session_id, panel, client_id=body.get("client_id"),
    )
    return panel


@app.delete("/api/sessions/{session_id}/file-panels/{panel_id}")
async def remove_file_panel(
    session_id: str, panel_id: str, client_id: str = Query(None)
):
    await _require_session_async(session_id)
    await asyncio.to_thread(
        session_manager.remove_open_file_panel,
        session_id,
        panel_id,
        client_id=client_id,
    )
    return {"deleted": True}


@app.put("/api/sessions/{session_id}/file-panels")
async def set_file_panels(session_id: str, body: dict):
    """Replace the full ordered panel list (covers reorder + clear)."""
    await _require_session_async(session_id)
    raw_panels = body.get("panels")
    if not isinstance(raw_panels, list):
        raise HTTPException(status_code=400, detail=t("error.file_panels_list_required"))
    panels = [_sanitize_file_panel(p) for p in raw_panels]
    await asyncio.to_thread(
        session_manager.set_open_file_panels,
        session_id, panels, client_id=body.get("client_id"),
    )
    return {"panels": panels}


def _sanitize_config_panel(raw: dict) -> dict:
    """Build a persisted open-config-panel dict from request input.

    Shape: {id, capability_id, scope, cwd}. `capability_id` is required;
    `scope` is 'global' | 'project' (default 'project'); `cwd` is the
    project path for project-scope panels (empty for global)."""
    capability_id = str(raw.get("capability_id") or "").strip()
    if not capability_id:
        raise HTTPException(status_code=400, detail="capability_id is required")
    scope = str(raw.get("scope") or "project").strip()
    if scope not in ("global", "project"):
        scope = "project"
    return {
        "id": str(raw.get("id") or uuid.uuid4().hex[:12]),
        "capability_id": capability_id,
        "scope": scope,
        "cwd": str(raw.get("cwd") or "").strip(),
    }


@app.post("/api/sessions/{session_id}/config-panels")
async def add_config_panel(session_id: str, body: dict):
    await _require_session_async(session_id)
    panel = _sanitize_config_panel(body)
    await asyncio.to_thread(
        session_manager.add_open_config_panel,
        session_id, panel, client_id=body.get("client_id"),
    )
    return panel


@app.delete("/api/sessions/{session_id}/config-panels/{panel_id}")
async def remove_config_panel(
    session_id: str, panel_id: str, client_id: str = Query(None)
):
    await _require_session_async(session_id)
    await asyncio.to_thread(
        session_manager.remove_open_config_panel,
        session_id,
        panel_id,
        client_id=client_id,
    )
    return {"deleted": True}


@app.put("/api/sessions/{session_id}/config-panels")
async def set_config_panels(session_id: str, body: dict):
    """Replace the full ordered config-panel list (covers reorder + clear)."""
    await _require_session_async(session_id)
    raw_panels = body.get("panels")
    if not isinstance(raw_panels, list):
        raise HTTPException(status_code=400, detail="panels must be a list")
    panels = [_sanitize_config_panel(p) for p in raw_panels]
    await asyncio.to_thread(
        session_manager.set_open_config_panels,
        session_id, panels, client_id=body.get("client_id"),
    )
    return {"panels": panels}


def _require_prompt_engineer_internal(x_internal_token: str) -> None:
    if not coordinator.is_internal_caller(x_internal_token):
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    _require_builtin_runtime_extension(extension_store.extension_id_for_role('prompt-engineer'))


@app.post("/api/internal/prompt-engineer/start")
async def internal_prompt_engineering_start(
    body: dict = Body(default={}),
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    _require_prompt_engineer_internal(x_internal_token)
    body = body or {}
    session_id = str(body.get("session_id") or "").strip()
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id is required")
    # Empty drafts are valid — claude starts the prompt from scratch.
    draft = body.get("draft") or ""
    mode = body.get("mode") or "fork"
    if mode not in ("fork", "new"):
        raise HTTPException(status_code=400, detail=t("error.mode_must_be_fork_or_new"))
    try:
        result = await prompt_engineer.start(session_id, draft, mode)
    except KeyError:
        raise HTTPException(status_code=404, detail=t("error.parent_session_not_found"))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Sample provider env so the eng-session CLI spawn inherits the right
    # ANTHROPIC_API_KEY / BASE_URL / CONFIG_DIR. Mirrors fork_and_send.
    await asyncio.to_thread(config_store.apply_env_vars)

    eng_id = result["eng_session_id"]
    eng_session = result["session"] or {}
    # Only fire the meta-prompt on a fresh start. On resume the eng
    # session already had its turn 1 (and possibly many subsequent ones)
    # — re-firing would tell claude "you are refining a prompt" again
    # mid-conversation and corrupt the thread.
    if result.get("meta_prompt") is not None:
        await coordinator.submit_prompt_async(eng_id, {
            "prompt": result["meta_prompt"],
            "app_session_id": eng_id,
            "model": eng_session.get("model"),
            "cwd": eng_session.get("cwd"),
            "ws_callback": None,
            "images": None,
            "orchestration_mode": eng_session.get("orchestration_mode"),
            "client_id": body.get("client_id"),
        })

    return {
        "eng_session_id": eng_id,
        "temp_file_path": result["temp_file_path"],
        "original_content": result["original_content"],
        "session": eng_session,
        "resumed": bool(result.get("resumed", False)),
    }


@app.post("/api/internal/prompt-engineer/get")
async def internal_prompt_engineering_get(
    body: dict = Body(default={}),
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    _require_prompt_engineer_internal(x_internal_token)
    session_id = str((body or {}).get("session_id") or "").strip()
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id is required")
    payload = prompt_engineer.get_for_parent(session_id)
    if payload is None:
        raise HTTPException(
            status_code=404, detail=t("error.no_live_eng_session"),
        )
    return {
        "eng_session_id": payload["eng_session_id"],
        "temp_file_path": payload["temp_file_path"],
        "original_content": payload["original_content"],
        "session": payload["session"],
        "resumed": True,
    }


@app.post("/api/internal/prompt-engineer/comment")
async def internal_prompt_engineering_comment(
    body: dict = Body(default={}),
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    _require_prompt_engineer_internal(x_internal_token)
    session_id = str((body or {}).get("session_id") or "").strip()
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id is required")
    if not prompt_engineer.is_eng_session(session_id):
        raise HTTPException(status_code=404, detail=t("error.not_eng_session"))
    body = body or {}
    try:
        file_path = body["file_path"]
        start_line = int(body["start_line"])
        end_line = int(body["end_line"])
        start_col = int(body["start_col"])
        end_col = int(body["end_col"])
        comment = body["comment"]
    except (KeyError, TypeError, ValueError) as e:
        raise HTTPException(status_code=400, detail=t("error.missing_invalid_field", e=str(e)))

    message = prompt_engineer.format_comment(
        file_path, start_line, end_line, start_col, end_col, comment,
    )
    eng_session = await _session_lite(session_id) or {}
    await asyncio.to_thread(config_store.apply_env_vars)
    await coordinator.submit_prompt_async(session_id, {
        "prompt": message,
        "app_session_id": session_id,
        "model": eng_session.get("model"),
        "cwd": eng_session.get("cwd"),
        "ws_callback": None,
        "images": None,
        "orchestration_mode": eng_session.get("orchestration_mode"),
        "client_id": body.get("client_id"),
    })
    return {"submitted": True}


@app.post("/api/internal/prompt-engineer/result")
async def internal_prompt_engineering_result(
    body: dict = Body(default={}),
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    _require_prompt_engineer_internal(x_internal_token)
    session_id = str((body or {}).get("session_id") or "").strip()
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id is required")
    if not prompt_engineer.is_eng_session(session_id):
        raise HTTPException(status_code=404, detail=t("error.not_eng_session"))
    try:
        content = await prompt_engineer.finalize(session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=t("error.session_not_found_retry"))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=t("error.temp_file_missing"))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    sess = await _session_lite(session_id) or {}
    meta = sess.get("working_mode_meta") or {}
    return {
        "content": content,
        "parent_session_id": meta.get("parent_session_id"),
        "original_content": meta.get("original_content", ""),
    }


@app.post("/api/internal/prompt-engineer/cleanup")
async def internal_prompt_engineering_cleanup(
    body: dict = Body(default={}),
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    _require_prompt_engineer_internal(x_internal_token)
    session_id = str((body or {}).get("session_id") or "").strip()
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id is required")
    if not prompt_engineer.is_eng_session(session_id):
        raise HTTPException(status_code=404, detail=t("error.not_eng_session"))
    await coordinator.cancel_session(session_id)
    ok = await prompt_engineer.cleanup(session_id)
    await _publish_worker_fanout_required(
        session_id,
        op_label="prompt-eng cleanup",
        caller_scope=True,
        remove_worker=True,
        outer_log_msg="worker fan-out failed during prompt-eng cleanup",
    )
    return {"deleted": ok}


# ── File editing mode ──────────────────────────────────────────────


@app.post("/api/file-editor")
async def start_file_editor(body: dict = Body(default={})):
    """Start (or join) the file-editing session for a project cwd and
    ensure the file is in its set.

    Body: { file_path: str, cwd: str, model?: str }
    """
    file_path = body.get("file_path")
    if not file_path:
        raise HTTPException(status_code=400, detail=t("error.file_path_required"))
    try:
        reasoning_effort = await asyncio.to_thread(
            _provider_reasoning_effort,
            body.get("provider_id"),
            _api_reasoning_effort(body.get("reasoning_effort")),
        )
        default_model = await asyncio.to_thread(config_store.default_session_model)
        result = await file_editor.start(
            file_path,
            cwd=body.get("cwd", ""),
            model=body.get("model") or default_model,
            provider_id=body.get("provider_id"),
            reasoning_effort=reasoning_effort,
            node_id=body.get("node_id") or "primary",
        )
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    editor_session = await _session_lite(result["session_id"]) or {}
    await asyncio.to_thread(config_store.apply_env_vars)

    if result.get("meta_prompt") is not None:
        await coordinator.submit_prompt_async(result["session_id"], {
            "prompt": result["meta_prompt"],
            "app_session_id": result["session_id"],
            "model": editor_session.get("model"),
            "cwd": editor_session.get("cwd"),
            "ws_callback": None,
        })

    return {
        "session_id": result["session_id"],
        "file_paths": result["file_paths"],
        "original_contents": result["original_contents"],
        "session": editor_session,
        "resumed": bool(result.get("resumed", False)),
    }


@app.post("/api/file-editor/{session_id}/comment")
async def add_file_editor_comment(session_id: str, body: dict):
    """Send a file-anchored comment to the file-editor session."""
    if not file_editor.is_file_editor_session(session_id):
        raise HTTPException(status_code=404, detail=t("error.not_file_editor_session"))
    body = body or {}
    try:
        file_path = body["file_path"]
        start_line = int(body["start_line"])
        end_line = int(body["end_line"])
        start_col = int(body["start_col"])
        end_col = int(body["end_col"])
        comment = body["comment"]
    except (KeyError, TypeError, ValueError) as e:
        raise HTTPException(status_code=400, detail=t("error.missing_invalid_field", e=str(e)))

    message = working_mode.format_file_comment(
        file_path, start_line, end_line, start_col, end_col, comment,
    )
    editor_session = await _session_lite(session_id) or {}
    await asyncio.to_thread(config_store.apply_env_vars)
    await coordinator.submit_prompt_async(session_id, {
        "prompt": message,
        "app_session_id": session_id,
        "model": editor_session.get("model"),
        "cwd": editor_session.get("cwd"),
        "ws_callback": None,
        "client_id": body.get("client_id"),
    })
    return {"submitted": True}


@app.post("/api/file-editor/{session_id}/discussions")
async def start_file_editor_discussion(session_id: str, body: dict):
    if not file_editor.is_file_editor_session(session_id):
        raise HTTPException(status_code=404, detail=t("error.not_file_editor_session"))
    try:
        discussion = file_editor.start_discussion(
            session_id,
            file_path=str(body.get("file_path") or "").strip(),
            line=int(body.get("line")),
            title=str(body.get("title") or ""),
            opened_by="user",
            client_id=body.get("client_id"),
        )
    except (TypeError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"discussion": discussion}


@app.patch("/api/file-editor/{session_id}/discussions/{discussion_id}")
async def patch_file_editor_discussion(session_id: str, discussion_id: str, body: dict):
    if not file_editor.is_file_editor_session(session_id):
        raise HTTPException(status_code=404, detail=t("error.not_file_editor_session"))
    try:
        discussion = file_editor.patch_discussion(
            session_id,
            discussion_id,
            body or {},
            client_id=(body or {}).get("client_id"),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"discussion": discussion}


@app.post("/api/file-editor/{session_id}/discussions/{discussion_id}/messages")
async def send_file_editor_discussion_message(session_id: str, discussion_id: str, body: dict):
    if not file_editor.is_file_editor_session(session_id):
        raise HTTPException(status_code=404, detail=t("error.not_file_editor_session"))
    prompt = str((body or {}).get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail=t("error.prompt_required"))
    try:
        discussion = file_editor.get_discussion(session_id, discussion_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    editor_session = await _session_lite(session_id) or {}
    await asyncio.to_thread(config_store.apply_env_vars)
    client_id = (body or {}).get("client_id")
    await coordinator.submit_prompt_async(session_id, {
        "prompt": prompt,
        "cli_prompt": file_editor.format_discussion_prompt(discussion, prompt),
        "app_session_id": session_id,
        "model": editor_session.get("model"),
        "cwd": editor_session.get("cwd"),
        "ws_callback": None,
        "client_id": client_id,
        "file_discussion_id": discussion_id,
    })
    return {"submitted": True, "client_id": client_id}


@app.delete("/api/file-editor/{session_id}")
async def cleanup_file_editor(session_id: str):
    """Tear down a file-editor session."""
    if not file_editor.is_file_editor_session(session_id):
        raise HTTPException(status_code=404, detail=t("error.not_file_editor_session"))
    await coordinator.cancel_session(session_id)
    ok = file_editor.cleanup(session_id)
    await _publish_worker_fanout_required(
        session_id,
        op_label="file-editor cleanup",
        caller_scope=True,
        remove_worker=True,
        outer_log_msg="worker fan-out failed during file-editor cleanup",
    )
    return {"deleted": ok}


@app.patch("/api/sessions/{session_id}/draft")
async def set_session_draft(session_id: str, body: dict):
    """Persist the in-progress chat input for this session. Called on a
    debounced cadence from every keystroke. `bump_updated_at=False` so
    typing doesn't reorder the sidebar.

    Stale-write guard: the body MUST carry `client_seq` (the client's
    monotonic timestamp at PATCH-send time, e.g. Date.now()). If
    `client_seq <= stored draft_input_seq` the PATCH is dropped — this
    prevents a slow-network typing-PATCH from arriving AFTER a
    send-PATCH that cleared the field and resurrecting stale text on
    disk. Returns the canonical state either way (so the caller can
    self-heal if rejected)."""
    session = await asyncio.to_thread(session_manager.get_lite, session_id)
    if not session:
        raise HTTPException(status_code=404, detail=t("error.session_not_found_retry"))
    draft = body.get("draft_input")
    if not isinstance(draft, str):
        raise HTTPException(status_code=400, detail=t("error.draft_input_must_be_string"))
    client_seq = body.get("client_seq")
    if not isinstance(client_seq, (int, float)):
        raise HTTPException(status_code=400, detail=t("error.client_seq_must_be_number"))
    client_seq = int(client_seq)
    stored_seq = int(session.get("draft_input_seq") or 0)
    if client_seq <= stored_seq:
        return {
            "draft_input": session.get("draft_input", ""),
            "draft_input_seq": stored_seq,
            "rejected": True,
        }
    draft_images = body.get("draft_images")
    if draft_images is not None and not isinstance(draft_images, list):
        raise HTTPException(status_code=400, detail="draft_images must be an array")
    await asyncio.to_thread(
        session_manager.set_draft,
        session_id,
        draft,
        client_seq,
        images=draft_images,
        client_id=body.get("client_id"),
    )
    result = {"draft_input": draft, "draft_input_seq": client_seq}
    if draft_images is not None:
        result["draft_images"] = draft_images
    return result


async def _re_enqueue_queued_prompts() -> None:
    """Re-enqueue accepted prompts that have not become user messages."""
    import session_queue_projection
    import team_messaging

    rebuilt = await asyncio.to_thread(session_queue_projection.ensure_current_or_rebuild)
    await asyncio.to_thread(session_manager.rebuild_queued_prompt_counts)
    logger.info(
        "re-enqueue: queue projection %s; scanning projected queued records",
        "rebuilt" if rebuilt else "current",
    )

    for session in await asyncio.to_thread(session_queue_projection.list_queued_records):
        sid = session.get("id")
        if not sid:
            continue
        try:
            queued = session.get("queued_prompts", [])
            if not queued:
                continue

            existing_client_ids = set(session.get("user_client_ids") or [])
            existing_lifecycle_ids = set(session.get("user_lifecycle_msg_ids") or [])

            for qp in list(queued):
                qp_id = qp.get("id")
                client_id = qp.get("client_id")
                lifecycle_msg_id = qp.get("lifecycle_msg_id")
                if not lifecycle_msg_id:
                    lifecycle_msg_id = new_lifecycle_msg_id()
                    await asyncio.to_thread(
                        session_manager.update_queued_prompt,
                        sid,
                        qp_id,
                        {"lifecycle_msg_id": lifecycle_msg_id},
                    )

                if (
                    (client_id and client_id in existing_client_ids)
                    or (lifecycle_msg_id and lifecycle_msg_id in existing_lifecycle_ids)
                ):
                    await asyncio.to_thread(session_manager.remove_queued_prompt, sid, qp_id)
                    logger.info(
                        "re-enqueue: skipping already-processed queued "
                        "prompt %s for session %s",
                        qp_id, sid,
                    )
                    continue

                team_message = team_messaging.team_message_from_queue_payload(
                    qp,
                    target_session_id=sid,
                )
                params = {
                    "prompt": qp.get("content", ""),
                    "app_session_id": sid,
                    "model": session.get("model"),
                    "cwd": session.get("cwd"),
                    "ws_callback": None,
                    "images": qp.get("images"),
                    "files": qp.get("files"),
                    "orchestration_mode": qp.get("orchestration_mode"),
                    "send_target": qp.get("send_target"),
                    "client_id": client_id,
                    "lifecycle_msg_id": lifecycle_msg_id,
                    "cli_prompt": qp.get("cli_prompt"),
                    "source": qp.get("source"),
                    "team_message": team_message,
                    "disallowed_tools": qp.get("disallowed_tools"),
                    "disabled_builtin_extensions": qp.get("disabled_builtin_extensions"),
                    "capability_contexts": qp.get("capability_contexts") or [],
                    "_alter_rewind_latest": bool(qp.get("alter_rewind_latest")),
                    "collapse_key": qp.get("collapse_key") or "",
                    "collapse_policy": qp.get("collapse_policy") or "",
                    "_queued_id": qp_id,
                }
                item_id = await coordinator.submit_prompt_async(sid, params)
                logger.info(
                    "re-enqueue: re-submitted queued prompt %s -> %s "
                    "for session %s",
                    qp_id, item_id, sid,
                )
        except Exception:
            logger.exception(
                "re-enqueue: failed for session %s, skipping", sid,
            )


async def _recover_in_flight_task() -> None:
    """Composite body for the `recover_in_flight` startup task: scan
    run dirs on a worker thread (sync FS I/O), then integrate the
    descriptors asynchronously. The startup gate opens after scan and
    classification; replay/finalization is reactive background work and
    must not block normal prompt start."""
    import startup_recovery_gate
    gate_open = False
    try:
        loop = asyncio.get_running_loop()
        recovered = await asyncio.to_thread(recover_all_in_flight, loop)
        startup_recovery_gate.mark_recovery_done()
        gate_open = True
        if recovered:
            logger.info("recover_all_in_flight: %d run(s) recovered", len(recovered))
            live = [r for r in recovered if bool(r.get("alive"))]
            cold = [r for r in recovered if not bool(r.get("alive"))]
            if live:
                logger.info("recover_all_in_flight: integrating %d live run(s)", len(live))
                await integrate_recovered_runs(coordinator, live)
            if cold:
                _enqueue_recovered_cold_runs(cold)
        # Re-enqueue persisted queued prompts after recovery is complete.
        await _re_enqueue_queued_prompts()
        # Resume a native-session import that a restart interrupted. Spawns
        # its own background thread; the idempotency registry makes resume
        # duplicate-free. Best-effort — must never block startup.
        try:
            import native_import
            native_import.resume_if_interrupted()
        except Exception:
            logger.exception("native_import: resume-on-startup failed")
    except Exception as e:
        if not gate_open:
            startup_recovery_gate.mark_recovery_failed(str(e))
        else:
            logger.exception("recover_all_in_flight: background integration failed")
        raise


_RECOVERED_COLD_RUN_WORKER_TASK: Optional[asyncio.Task] = None
_RECOVERED_COLD_RUN_QUEUE: "asyncio.Queue[list[dict]]" = asyncio.Queue()
_RECOVERED_COLD_RUN_BATCH_MAX = 8


def _enqueue_recovered_cold_runs(recovered: list[dict]) -> None:
    """Queue completed/stale recovered runs for low-priority integration.

    Live recovered runs are integrated first in `_recover_in_flight_task`.
    Cold runs no longer wait a fixed 120 seconds; they enter a bounded
    single-worker background queue immediately, in small batches, so stale
    completed output converges quickly without competing with live reattach.
    """
    if not recovered:
        return
    for index in range(0, len(recovered), _RECOVERED_COLD_RUN_BATCH_MAX):
        _RECOVERED_COLD_RUN_QUEUE.put_nowait(
            recovered[index:index + _RECOVERED_COLD_RUN_BATCH_MAX],
        )
    _ensure_recovered_cold_run_worker()
    logger.info(
        "recover_all_in_flight: queued %d completed/stale run(s) for "
        "low-priority integration",
        len(recovered),
    )


def _ensure_recovered_cold_run_worker() -> None:
    global _RECOVERED_COLD_RUN_WORKER_TASK
    if (
        _RECOVERED_COLD_RUN_WORKER_TASK is not None
        and not _RECOVERED_COLD_RUN_WORKER_TASK.done()
    ):
        return
    _RECOVERED_COLD_RUN_WORKER_TASK = asyncio.create_task(
        _recovered_cold_run_worker(),
        name="startup-recover-cold-runs",
    )


async def _recovered_cold_run_worker() -> None:
    while True:
        batch = await _RECOVERED_COLD_RUN_QUEUE.get()
        try:
            # Low priority: yield once before each batch so live recovery,
            # re-enqueue, WS, and REST work scheduled by startup can run first.
            await asyncio.sleep(0)
            started = time.monotonic()
            await integrate_recovered_runs(coordinator, batch)
            logger.info(
                "recover_all_in_flight: integrated cold batch of %d run(s) "
                "in %.3fs",
                len(batch),
                time.monotonic() - started,
            )
        except Exception:
            logger.exception("recovered cold-run integration failed")
        finally:
            _RECOVERED_COLD_RUN_QUEUE.task_done()


async def _housekeeping_task() -> None:
    """Load all providers and prune old runs/approvals."""
    # 1. Load all providers so known_providers() is complete.
    await asyncio.to_thread(load_all_providers)

    # 2. Prune old run directories.
    try:
        ap = default_provider()
        await asyncio.to_thread(ap.prune_old_runs)
    except Exception:
        logger.exception("housekeeping: prune_old_runs failed")

    # 3. Prune old pending approvals.
    try:
        from stores import pending_approvals
        n = await asyncio.to_thread(pending_approvals.prune_old)
        if n:
            logger.info("housekeeping: pruned %d old approval records", n)
    except Exception:
        logger.exception("housekeeping: pending_approvals.prune_old failed")

    # 3b. Prune old pending node-registration requests.
    try:
        from stores import pending_node_registrations
        n = await asyncio.to_thread(pending_node_registrations.prune_old)
        if n:
            logger.info("housekeeping: pruned %d old node-registration records", n)
    except Exception:
        logger.exception("housekeeping: pending_node_registrations.prune_old failed")

    # 4. Best-effort extension auto-update for refreshable install sources.
    try:
        result = await asyncio.to_thread(extension_store.update_installed_extensions)
        if result.get("updated"):
            logger.info(
                "housekeeping: auto-updated %d extension(s)",
                result["updated"],
            )
            await coordinator.broadcast_global("extensions_changed", {})
            import node_extension_sync
            node_extension_sync.notify_extensions_changed()
    except Exception:
        logger.exception("housekeeping: update_installed_extensions failed")

    # 5. Self-heal extension instruction blocks: re-apply enabled extensions,
    #    purge disabled/uninstalled ones from provider config files.
    try:
        swept = await asyncio.to_thread(extension_store.reconcile_all_instructions)
        if swept:
            logger.info("housekeeping: swept %d orphan instruction block(s)", swept)
    except Exception:
        logger.exception("housekeeping: reconcile_all_instructions failed")

    # 6. Self-heal extension runtime skills: install enabled extension skills
    #    into ~/.agents/skills and remove disabled/uninstalled extension-owned copies.
    try:
        changed = await asyncio.to_thread(extension_store.reconcile_runtime_skills)
        if changed:
            logger.info("housekeeping: reconciled %d extension runtime skill item(s)", changed)
    except Exception:
        logger.exception("housekeeping: reconcile_runtime_skills failed")

    # 7. Self-heal extension native MCP entries through Provider Config Sync.
    try:
        changed = await asyncio.to_thread(extension_store.reconcile_native_mcp_servers)
        if changed:
            logger.info("housekeeping: reconciled %d extension native MCP item(s)", changed)
    except Exception:
        logger.exception("housekeeping: reconcile_native_mcp_servers failed")

    # 8. Pre-mint per-extension internal-loopback tokens so out-of-process
    #    native MCP launchers never race to create one.
    try:
        await asyncio.to_thread(extension_store.reconcile_extension_tokens)
    except Exception:
        logger.exception("housekeeping: reconcile_extension_tokens failed")

    # 9. Grandfather consent for extensions enabled before the consent feature.
    try:
        grandfathered = await asyncio.to_thread(extension_store.reconcile_extension_consent)
        if grandfathered:
            logger.info("housekeeping: grandfathered consent for %d extension(s)", grandfathered)
    except Exception:
        logger.exception("housekeeping: reconcile_extension_consent failed")


# --- Event-loop lag watchdog -----------------------------------------------
# The lag monitor coroutine cannot run while its callback is delayed. This
# daemon samples evidence while its heartbeat is stale without asserting a
# cause: ready-queue starvation, a synchronous stack, and OS descheduling can
# all produce the same stale heartbeat. The backend's stderr is
# not captured in bundled/detached runs, so the old sys.stderr dumps were
# being lost — dumps now go to ba_home/logs/backend-faulthandler.log.
# Output-equivalent to the old dump: adds diagnostics only, no control-flow
# or state change.
_LAG_HEARTBEAT: list[float] = [time.monotonic()]
_LAG_LAST_DUMP: list[float] = [0.0]
_LAG_LOOP_EVIDENCE: dict[str, object] = {
    "sentinel_at": time.monotonic(),
    "sentinel_latency_ms": 0.0,
    "ready_depth": 0,
    "last_sentinel_callback": "startup",
    "monitor_task": "startup",
    "monitor_task_duration_ms": 0.0,
    "last_sentinel_duration_ms": 0.0,
}
_ASSISTANT_EXTENSION_ID = "ofek-dev.assistant"
_LAG_REPORT_BODY_LIMIT_BYTES = 18_000
_LAG_REPORT_MAX_EVIDENCE_LINES = 120
_LAG_REPORT_MAX_LINE_CHARS = 512
_LAG_REPORT_TRUNCATED = "[diagnostic evidence truncated]"


def _lag_watchdog_issue_ref(evidence: str) -> str:
    digest = hashlib.sha256(evidence.encode("utf-8")).hexdigest()[:16]
    return f"bug:lag-watchdog:{digest}"


def _lag_report_safe_path(path: Path) -> str:
    value = str(path.expanduser())
    home = str(Path.home())
    if value == home:
        return "~"
    if value.startswith(home + os.sep):
        return "~" + value[len(home):]
    return Path(value).name


def _lag_report_evidence(value: str) -> str:
    redacted = redact_secrets(value)
    lines = redacted.splitlines()[:_LAG_REPORT_MAX_EVIDENCE_LINES]
    return "\n".join(line[:_LAG_REPORT_MAX_LINE_CHARS] for line in lines)


def _serialize_lag_report(payload: dict[str, object]) -> bytes:
    def encode(candidate: dict[str, object]) -> bytes:
        return json.dumps(candidate, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

    body = encode(payload)
    if len(body) <= _LAG_REPORT_BODY_LIMIT_BYTES:
        return body
    evidence = str(payload.get("evidence") or "")
    marker = _LAG_REPORT_TRUNCATED
    low, high = 0, len(evidence)
    best: bytes | None = None
    while low <= high:
        keep = (low + high) // 2
        candidate = dict(payload)
        candidate["evidence"] = evidence[:keep].rstrip() + "\n" + marker
        encoded = encode(candidate)
        if len(encoded) <= _LAG_REPORT_BODY_LIMIT_BYTES:
            best = encoded
            low = keep + 1
        else:
            high = keep - 1
    if best is None:
        raise ValueError("lag report metadata exceeds body limit")
    return best


def _safe_extension_error_detail(status: int, content: bytes) -> str:
    del content
    reasons = {
        400: "invalid request",
        401: "authentication required",
        403: "request forbidden",
        404: "endpoint not found",
        409: "request conflict",
        413: "request too large",
        422: "invalid request",
        429: "rate limited",
    }
    reason = reasons.get(status)
    if reason is None:
        reason = "request rejected" if 400 <= status < 500 else "extension backend failed"
    return redact_secrets(reason)


def _report_lag_watchdog_issue(
    *,
    label: str,
    heartbeat_age: float,
    dump_path: Path,
    evidence: str,
    stack_names: list[str],
) -> None:
    safe_evidence = _lag_report_evidence(evidence)
    safe_dump_path = _lag_report_safe_path(dump_path)
    payload = {
        "requirement_ref": _lag_watchdog_issue_ref(evidence),
        "summary": f"Event loop lag: {label} ~{heartbeat_age:.1f}s",
        "assistant_message": (
            "The backend event-loop lag watchdog captured a slowness incident "
            f"and wrote the full traceback dump to {safe_dump_path}."
        ),
        "evidence": safe_evidence,
        "source": "lag_watchdog",
        "severity": "high",
        "dump_path": safe_dump_path,
        "lag_label": label,
        "lag_seconds": heartbeat_age,
        "stack_names": [redact_secrets(str(name))[:120] for name in stack_names[:16]],
    }
    try:
        import extension_backend_loader
        status, content = extension_backend_loader.invoke_extension_backend_sync(
            _ASSISTANT_EXTENSION_ID,
            "assistant/bug-report",
            body_bytes=_serialize_lag_report(payload),
            base_url=os.environ.get("BETTER_CLAUDE_BACKEND_URL", "http://localhost:8000"),
        )
    except Exception:
        logger.exception("lag-watchdog: assistant board bug report dispatch failed")
        return
    if status >= 400:
        detail = _safe_extension_error_detail(status, content)
        logger.warning(
            "lag-watchdog: assistant board bug report failed status=%s detail=%s",
            status,
            detail,
        )


def _schedule_lag_sentinel(loop: asyncio.AbstractEventLoop) -> None:
    scheduled_at = time.monotonic()

    def sentinel() -> None:
        started = time.monotonic()
        _LAG_LOOP_EVIDENCE.update({
            "sentinel_at": started,
            "sentinel_latency_ms": (started - scheduled_at) * 1000.0,
            "ready_depth": len(getattr(loop, "_ready", ())),
            "last_sentinel_callback": "lag-sentinel",
        })
        _LAG_LOOP_EVIDENCE["last_sentinel_duration_ms"] = (
            time.monotonic() - started
        ) * 1000.0

    loop.call_soon(sentinel)


def _start_lag_watchdog(threshold: float = 1.5, cooldown: float = 5.0) -> None:
    dump_path = ba_home() / "logs" / "backend-faulthandler.log"
    try:
        dump_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        logger.exception("lag-watchdog: cannot create logs dir")
        return

    loop_thread_id = threading.get_ident()

    def run() -> None:
        sampled_stale_heartbeat: float | None = None
        while True:
            time.sleep(0.5)
            now = time.monotonic()
            heartbeat_age = now - _LAG_HEARTBEAT[0]
            heartbeat = _LAG_HEARTBEAT[0]
            if heartbeat_age <= threshold:
                sampled_stale_heartbeat = None
                continue
            if sampled_stale_heartbeat == heartbeat or now - _LAG_LAST_DUMP[0] <= cooldown:
                continue
            sampled_stale_heartbeat = heartbeat
            _LAG_LAST_DUMP[0] = now
            try:
                dump_path.parent.mkdir(parents=True, exist_ok=True)
                mode = "w" if dump_path.exists() and dump_path.stat().st_size > 2_000_000 else "a"
                samples: list[tuple[float, dict[int, object]]] = []
                cpu_started = time.process_time()
                thread_cpu_started = time.thread_time() if hasattr(time, "thread_time") else None
                sample_started = time.monotonic()
                for _ in range(3):
                    samples.append((time.monotonic(), sys._current_frames()))
                    time.sleep(0.05)
                cpu_delta = time.process_time() - cpu_started
                wall_delta = time.monotonic() - sample_started
                thread_cpu_delta = (
                    time.thread_time() - thread_cpu_started
                    if thread_cpu_started is not None else None
                )
                loop_frames = [frames.get(loop_thread_id) for _, frames in samples]
                stack_names = [
                    frame.f_code.co_name if frame is not None else "missing"
                    for frame in loop_frames
                ]
                sentinel_age = now - float(_LAG_LOOP_EVIDENCE["sentinel_at"])
                if int(_LAG_LOOP_EVIDENCE["ready_depth"]) > 10:
                    label = "heartbeat starvation candidate"
                elif len(set(stack_names)) == 1 and stack_names[0] not in {
                    "run_until_complete", "run_forever", "_run_once", "select"
                }:
                    label = "blocking stack candidate"
                elif wall_delta > 0 and cpu_delta / wall_delta < 0.1:
                    label = "OS deschedule candidate"
                else:
                    label = "heartbeat starvation candidate"
                evidence = (
                    f"event loop lag evidence heartbeat_age={heartbeat_age:.1f}s "
                    f"@ {datetime.now().isoformat()} label={label} "
                    f"sample_age_ms={sentinel_age * 1000.0:.1f} "
                    f"ready_depth={_LAG_LOOP_EVIDENCE['ready_depth']} "
                    f"sentinel_latency_ms={_LAG_LOOP_EVIDENCE['sentinel_latency_ms']} "
                        f"last_sentinel_callback={_LAG_LOOP_EVIDENCE['last_sentinel_callback']} "
                        f"monitor_task={_LAG_LOOP_EVIDENCE['monitor_task']} "
                        f"monitor_task_duration_ms={_LAG_LOOP_EVIDENCE['monitor_task_duration_ms']} "
                        f"last_sentinel_duration_ms={_LAG_LOOP_EVIDENCE['last_sentinel_duration_ms']} "
                    f"process_cpu_delta_ms={cpu_delta * 1000.0:.1f} "
                    f"watchdog_thread_cpu_delta_ms="
                    f"{thread_cpu_delta * 1000.0 if thread_cpu_delta is not None else -1.0:.1f} "
                    f"sample_overhead_ms={wall_delta * 1000.0:.1f}"
                )
                with open(dump_path, mode, encoding="utf-8") as fh:
                    fh.write(f"\n=== {evidence} ===\n")
                    import traceback
                    for index, (sample_at, frames) in enumerate(samples):
                        fh.write(f"--- sample {index + 1} at={sample_at:.6f} ---\n")
                        frame = frames.get(loop_thread_id)
                        if frame is not None:
                            traceback.print_stack(frame, file=fh, limit=40)
                    fh.write("--- all-thread tops ---\n")
                    for thread_id, frame in samples[-1][1].items():
                        fh.write(
                            f"thread={thread_id} name={frame.f_code.co_name} "
                            f"file={frame.f_code.co_filename}:{frame.f_lineno}\n"
                        )
                _report_lag_watchdog_issue(
                    label=label,
                    heartbeat_age=heartbeat_age,
                    dump_path=dump_path,
                    evidence=evidence,
                    stack_names=stack_names,
                )
                logger.warning(
                    "lag-watchdog: %s ~%.1fs, dumped to %s",
                    label,
                    heartbeat_age,
                    dump_path,
                )
            except Exception:
                logger.exception("lag-watchdog dump failed")

    threading.Thread(target=run, daemon=True, name="lag-watchdog").start()


@app.on_event("startup")
async def on_startup():
    """Boot uvicorn fast: every long-running step (migrations,
    recovery scans, jsonl replay) runs as a tracked background task
    via `startup_task_registry`. The frontend renders a non-blocking
    banner from `GET /api/startup_tasks` + the `startup_task_changed`
    WS event; sessions still mid-recovery surface a per-message
    `isRecovering` pill from `session_manager._recovering_msg_ids`.

    INVARIANT: this coroutine returns within milliseconds. Anything
    that touches disk, parses jsonl, or scans subprocesses MUST be
    scheduled, not awaited inline.
    """
    acquire_backend_instance_lock()
    from provider import reopen_provider_tasks
    reopen_provider_tasks()
    provider_setup.reopen_provider_setup()
    reopen_reconciles()
    reopen_ws_json_executor()
    coordinator.reopen_global_broadcasts()
    logger.info("backend version=%s", _GIT_SHA)

    # Native-transcript FTS index: spawn the background daemon that builds +
    # refreshes it (throttled, non-blocking). Skipped in test mode so test
    # backends don't scan the real ~/.claude/~/.codex corpus.
    if not os.environ.get("BETTER_AGENT_TEST_MODE"):
        try:
            import native_transcript_index
            native_transcript_index.ensure_started()
        except Exception:
            logger.debug("native transcript index worker failed to start", exc_info=True)

    # Install SIGINT flag so on_shutdown can distinguish Ctrl+C from
    # uvicorn reload (which sends SIGTERM, not SIGINT). The
    # `signal.signal` call only works on the main thread of the main
    # interpreter — when uvicorn is launched on a background thread
    # (integration tests do this) we skip the install rather than
    # crashing startup.
    global _uvicorn_sigint_handler, _intentional_shutdown
    global _kill_runners_on_shutdown, _sigint_count
    global _project_match_executor, _project_match_ready
    _intentional_shutdown = False
    _kill_runners_on_shutdown = False
    _sigint_count = 0
    _second_sigint_event.clear()
    try:
        current = signal.getsignal(signal.SIGINT)
        if callable(current) and current is not _sigint_flag_handler:
            _uvicorn_sigint_handler = current
            signal.signal(signal.SIGINT, _sigint_flag_handler)
    except ValueError:
        # "signal only works in main thread of the main interpreter"
        logger.debug("SIGINT handler install skipped (non-main thread)")

    loop = asyncio.get_running_loop()
    ws_broadcaster.bind(loop)

    # Perf rollup task — flushes a `PERF rollup` line every
    # ROLLUP_SECS seconds. Held at module scope inside perf.py so
    # the asyncio task isn't garbage-collected after this returns.
    perf.start_rollup_task()
    _fire_and_forget(asyncio.to_thread(shortcut_picker.prewarm_http_stack))

    # Background running-state tick: prunes dead `_run_state` entries
    # via os.kill(pid, 0) in a daemon thread (never blocks the event
    # loop) and publishes cached running/monitoring snapshots that
    # GET /api/sessions and GET /api/projects read via
    # is_running_cached / monitoring_state_cached.
    coordinator.turn_manager.start_background_tick()

    # Auto-restart-on-idle: when the user enables the pref, fire a
    # supervisor restart every time the system goes idle after work, so
    # code changes are picked up without a manual reload. Inert unless
    # `auto_restart_on_idle` is on AND we're under the run.sh supervisor.
    _auto_restart_on_idle_monitor.start()

    # Backend-owned schedule ticker — fires due schedules as normal
    # prompts through coordinator.submit_prompt.
    schedule_ticker.start()

    # Daily model-catalog refresher. Assumes uvicorn --workers 1
    # (see auth.py:8, run.sh:132) — a second worker would fire a
    # parallel refresh tick + double-write the cache file.
    #
    # 5-min poll: providers overdue by >=24h refresh on the next tick.
    # Worst-case latency between "model published upstream" and
    # "visible in dropdown" is THRESHOLD + POLL = 24h05m. Acceptable.
    # First iteration acts as cold-start warm-up; no explicit
    # pre-tick needed.
    #
    # Suspend-safe: `asyncio.sleep(POLL)` pauses while the host is
    # suspended; on resume, the next tick fires the wall-clock-overdue
    # providers via `last_refreshed_at + threshold < time.time()`.
    # Worst observed latency = up to POLL seconds late.
    import models as models_mod

    async def _prewarm_model_locks() -> None:
        try:
            await asyncio.to_thread(models_mod.prewarm_locks)
        except Exception:
            logger.exception("models prewarm_locks failed")

    asyncio.create_task(_prewarm_model_locks(), name="models-prewarm-locks")

    # Warm the get-requirements processor's provisioned base off the query
    # path — a spec version bump or restart would otherwise make the first
    # query pay the provision turn inside its dispatch budget.
    import requirement_prewarm

    async def _prewarm_requirements_processor() -> None:
        try:
            await requirement_prewarm.run_requirements_prewarm("startup")
        except Exception:
            logger.exception("requirements processor prewarm failed")

    async def _models_catalog_refresher() -> None:
        POLL = 300
        while True:
            try:
                async for pid, diff in models_mod.refresh_all_due():
                    if diff:
                        try:
                            await _broadcast_models_catalog_changed(pid, diff)
                        except Exception:
                            logger.exception(
                                "broadcast models_catalog_changed failed for %s",
                                pid,
                            )
            except Exception:
                logger.exception("models refresher error")
            await asyncio.sleep(POLL)

    asyncio.create_task(
        _models_catalog_refresher(),
        name="models-catalog-refresher",
    )

    async def _event_loop_lag_monitor() -> None:
        interval = 1.0
        warn_after = 0.5
        expected = time.monotonic() + interval
        loop = asyncio.get_running_loop()
        while True:
            task = asyncio.current_task()
            _LAG_LOOP_EVIDENCE["monitor_task"] = task.get_name() if task is not None else "none"
            _schedule_lag_sentinel(loop)
            await asyncio.sleep(interval)
            body_started = time.monotonic()
            now = time.monotonic()
            lag = now - expected
            if lag > warn_after:
                _warning_off_loop("event loop lag %.3fs", lag)
            # Heartbeat for the lag watchdog thread: proves the loop is
            # alive. A sync blocker starves this coroutine, the heartbeat
            # goes stale, and the watchdog dumps the blocker mid-flight.
            _LAG_HEARTBEAT[0] = time.monotonic()
            expected = now + interval
            _LAG_LOOP_EVIDENCE["monitor_task_duration_ms"] = (
                time.monotonic() - body_started
            ) * 1000.0

    asyncio.create_task(_event_loop_lag_monitor(), name="event-loop-lag-monitor")
    # Reset the heartbeat right before arming the watchdog: the module-level
    # init at import time is long stale by on_startup (heavy imports), which
    # would otherwise trip one spurious "blocked" dump before the monitor
    # stamps its first cycle.
    _LAG_HEARTBEAT[0] = time.monotonic()
    _start_lag_watchdog()

    # Async-reconcile wiring: session_manager owns the dirty flag +
    # single-flight task + 0.3s delayed-progress, but the reconcile
    # body and the WS event emission live here (circular-import
    # avoidance: reconcile pulls `orchs.get_strategy`, emit pulls
    # `coordinator`). Bind at startup so the manager can schedule
    # reconciles onto this loop and broadcast progress.
    session_manager.bind_loop(loop)
    # DraftStore needs the loop for its debounced flush scheduling.
    # The sm hook wiring (pin_check / on_persist / on_drop) happens in
    # DraftStore.__init__ — Coordinator construction is self-sufficient.
    coordinator.draft_store.bind_loop(loop)
    from event_journal import bind_event_journal_loop
    bind_event_journal_loop(loop)
    import extension_store
    session_manager.bind_reconcile_fn(_reconcile_root_by_id)
    session_manager.bind_processing_emitter(_emit_session_processing)
    session_manager.bind_stub_invalidated_emitter(_emit_stub_invalidated)
    session_manager.bind_reconciled_emitter(_emit_session_reconciled)
    # (`bind_active_run_gate` is wired at module-load time, right
    # after the coordinator is constructed — see the top of this
    # file — so the gate is in place before any route is mounted.)

    # Wire the in-process event bus's standard subscribers (persistence
    # to events.jsonl). Idempotent — safe across uvicorn reloads.
    # Sync µs-fast, stays inline.
    try:
        from event_bus_subscribers import register_default_subscribers
        register_default_subscribers()
        event_bus.unsubscribe("requirement_tags_ws")
        event_bus.subscribe(
            "requirement_tags.refreshed",
            _forward_requirement_tags_refreshed,
            priority=80,
            name="requirement_tags_ws",
        )
    except Exception:
        logger.exception("event_bus subscriber registration failed")

    # Bind + reset the startup-task registry. `reset()` broadcasts a
    # `{cleared: true}` ping so any tab connected through a uvicorn
    # --reload drops its stale local map before we register fresh
    # entries for this process.
    from startup_tasks import startup_task_registry, run_task, run_composite_task
    startup_task_registry.bind(coordinator, loop)
    startup_task_registry.reset()

    # Schedule every long-running step as a tracked background task.
    # `on_startup` returns the moment these are dispatched —
    # "Application startup complete" fires within milliseconds.
    from orchs.adv_sync import recover_running_overlays_on_startup
    from file_ref_resolver import run_migration_once
    from paths import ba_home
    import startup_recovery_gate
    startup_recovery_gate.begin_recovery()

    async def _on_startup_bg_orchestrator():
        """Sequence startup tasks that have ordering dependencies."""
        # 1. Housekeeping (load providers, prune old runs/approvals).
        # MUST run first so known_providers() is complete for recovery.
        await run_composite_task(
            "housekeeping",
            "startup_tasks.housekeeping",
            _housekeeping_task,
        )

        async def _reconcile_managed_extensions() -> None:
            await asyncio.to_thread(
                extension_store.list_extensions_with_reconciliation,
                include_hidden=True,
            )

        await run_task(
            "extension_reconciliation",
            "startup_tasks.extension_reconciliation",
            _reconcile_managed_extensions,
        )
        import extension_package_loader
        try:
            extension_package_loader.ensure_package_importable(
                extension_store.extension_id_for_role("requirements"),
                "requirement_analysis",
            )
            from requirement_analysis.session_tags import bind_event_loop as bind_requirement_tags_loop
        except (extension_package_loader.ExtensionPackageUnavailable, ModuleNotFoundError):
            pass
        else:
            bind_requirement_tags_loop(loop)
        asyncio.create_task(
            run_task(
                "requirements_processor_prewarm",
                "startup_tasks.requirements_processor_prewarm",
                _prewarm_requirements_processor,
            ),
            name="requirements-processor-prewarm",
        )

        # 2. Recovery tasks (depend on known_providers)
        # These can run in parallel with each other.
        asyncio.create_task(
            run_composite_task(
                "recover_in_flight",
                "startup_tasks.recover_in_flight",
                _recover_in_flight_task,
            ),
            name="startup-recover-in-flight",
        )
        asyncio.create_task(
            run_task(
                "adv_sync_overlay_recovery",
                "startup_tasks.adv_sync_overlay_recovery",
                recover_running_overlays_on_startup,
            ),
            name="startup-adv-sync-recovery",
        )
        from runs_dir import ensure_run_state_ledger_backfilled
        asyncio.create_task(
            run_task(
                "run_state_ledger_backfill",
                "startup_tasks.run_state_ledger_backfill",
                ensure_run_state_ledger_backfilled,
            ),
            name="startup-run-state-ledger-backfill",
        )

    # Launch the orchestrator.
    asyncio.create_task(_on_startup_bg_orchestrator(), name="startup-orchestrator")

    async def _delayed_startup_task(delay_s: float, task_coro_factory) -> None:
        await asyncio.sleep(delay_s)
        await task_coro_factory()

    # Backfill git remotes for existing projects + rebuild mappings.
    # Filesystem-only, independent of recovery.
    def _backfill_project_git_remotes():
        n = project_store.backfill_git_remotes()
        if n:
            logger.info("housekeeping: backfilled git_remote for %d projects", n)
        projects = project_store.list_projects()
        project_mapping_store.rebuild_and_save(projects)

    asyncio.create_task(
        run_task(
            "project_git_backfill",
            "startup_tasks.project_git_backfill",
            _backfill_project_git_remotes,
        ),
        name="startup-project-git-backfill",
    )

    # Eager-warm the session-summary index in a worker thread so the
    # first `GET /api/sessions` doesn't pay the cold-walk cost
    # (~2-5 s for 400+ session.json files). The walk MUST run off the
    # event loop — when blocked inline it starved every other
    # endpoint during startup (PERF showed /api/startup_tasks peaking
    # at 65 s, /api/sessions at 102 s, all blocked behind the lazy
    # first-call rebuild). `run_task` default `in_thread=True`
    # offloads the sync `_ensure_summary_index` via `to_thread`.
    # Independent of provider/recover tasks — filesystem-only.
    import session_store as _ss
    asyncio.create_task(
        run_task(
            "summary_index_warm",
            "startup_tasks.summary_index_warm",
            _ss._ensure_summary_index,
        ),
        name="startup-summary-index-warm",
    )

    asyncio.create_task(
        run_task(
            "virtual_session_summaries_warm",
            "startup_tasks.virtual_session_summaries_warm",
            virtual_session_store.list_all,
        ),
        name="startup-virtual-session-summaries-warm",
    )

    asyncio.create_task(
        run_task(
            "git_status_warm",
            "startup_tasks.git_status_warm",
            _warm_recent_git_statuses,
            in_thread=False,
        ),
        name="startup-git-status-warm",
    )

    asyncio.create_task(
        run_task(
            "project_update_counts_warm",
            "startup_tasks.project_update_counts_warm",
            project_update_store.warm_counts,
        ),
        name="startup-project-update-counts-warm",
    )

    def _warm_pending_node_projection() -> None:
        import node_link
        node_link.public_pending_nodes()

    asyncio.create_task(
        run_task(
            "pending_node_projection_warm",
            "startup_tasks.pending_node_projection_warm",
            _warm_pending_node_projection,
        ),
        name="startup-pending-node-projection-warm",
    )

    import session_search_index

    def _rebuild_session_search_index_if_empty() -> None:
        if not session_search_index.needs_rebuild():
            logger.info("session_search_index: persisted index present; skipping startup rebuild")
            return
        session_search_index.rebuild_from_disk()

    asyncio.create_task(
        _delayed_startup_task(
            20.0,
            lambda: run_task(
                "session_search_index_rebuild",
                "startup_tasks.session_search_index_rebuild",
                _rebuild_session_search_index_if_empty,
            ),
        ),
        name="startup-session-search-index-rebuild",
    )

    asyncio.create_task(
        run_task(
            "bcfile_migration",
            "startup_tasks.bcfile_migration",
            run_migration_once,
            ba_home(),
        ),
        name="startup-bcfile-migration",
    )

    if not any(
        t.get_name() == "periodic-session-auto-delete"
        for t in asyncio.all_tasks()
    ):
        async def _periodic_session_auto_delete() -> None:
            interval_s = 24 * 60 * 60
            while True:
                try:
                    await _auto_delete_expired_sessions()
                    await asyncio.sleep(interval_s)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("periodic session auto-delete failed")
                    await asyncio.sleep(interval_s)
        asyncio.create_task(
            _periodic_session_auto_delete(),
            name="periodic-session-auto-delete",
        )

    # Multi-machine: kick off the per-node last_acked_offset persistence
    # coalescer. Cheap (1s wakeup, idle most of the time). Only useful
    # in primary mode but harmless on the worker-node build since there
    # are no `mark_offsets_dirty` callers there.
    async def _start_node_offset_loop_if_ready() -> None:
        try:
            ready = await asyncio.to_thread(
                extension_store.is_extension_runtime_ready,
                extension_store.extension_id_for_role('machine-nodes'),
            )
            if ready:
                import node_store as _ns
                _ns.start_offset_flush_loop()
        except Exception:
            logger.exception("node_store: offset flush loop failed to start")
    asyncio.create_task(
        _start_node_offset_loop_if_ready(),
        name="node-offset-flush-startup",
    )

    # Phase-1 stage 5b: periodic internal_token rotation. Every 60 min
    # the coordinator mints a new token + retains the old one for a
    # 5min grace window. Runner mtime-cached `_load_internal_token`
    # picks up the new value within one stat() interval after rotation;
    # in-flight calls retry with the new token automatically.
    # Operators who want disabling can set
    # `BA_DISABLE_INTERNAL_TOKEN_ROTATION=1`.
    # Guard: on_startup can fire twice (uvicorn hot-reload, Starlette
    # lifespan edge-cases). Only ONE rotation task must run — a duplicate
    # clobbers `_prev_token` every ~6 min, nuking the grace window and
    # 403-ing in-flight runners.
    if not any(
        t.get_name() == "periodic-internal-token-rotation"
        for t in asyncio.all_tasks()
    ):
        async def _periodic_token_rotation() -> None:
            if os.environ.get(
                "BA_DISABLE_INTERNAL_TOKEN_ROTATION", "",
            ).strip().lower() in {"1", "true", "yes", "on"}:
                logger.info("token rotation disabled via env")
                return
            interval_s = 3600.0  # 60 minutes
            while True:
                try:
                    await asyncio.sleep(interval_s)
                    # Grace must exceed the interval so that a previous
                    # rotation's old token stays valid until the NEXT
                    # rotation preserves it as _prev_token.  2× interval
                    # gives a full rotation cycle of slack.
                    coordinator.rotate_internal_token(
                        grace_seconds=interval_s * 2,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("periodic token rotation failed")
        asyncio.create_task(
            _periodic_token_rotation(),
            name="periodic-internal-token-rotation",
        )


@app.on_event("shutdown")
async def on_shutdown():
    """Cancel every in-flight runner on intentional shutdown (Ctrl+C).
    During uvicorn hot-reload (SIGTERM), leave runners alive — they're
    detached (start_new_session=True) and will be re-attached by
    run_recovery on the next startup. The interactive "kill? [y/N]"
    prompt lives here (not the signal handler) so it runs off the
    signal frame and can't block the event loop or re-enter readline."""
    global _kill_runners_on_shutdown
    try:
        import extension_daemons

        await asyncio.to_thread(extension_daemons.shutdown_backend_daemons)
    except Exception:
        logger.exception("on_shutdown: extension_daemons shutdown failed")
    if _consume_shutdown_kill_runners_flag():
        _kill_runners_on_shutdown = True
    elif _intentional_shutdown:
        await _prompt_kill_runners()
    if _intentional_shutdown and _kill_runners_on_shutdown:
        from provider import known_providers
        try:
            killed_total = 0
            for prov in known_providers():
                # Y=kill covers in-flight turns. "Leave alive" keeps
                # everything: runners are detached, complete.json /
                # run_recovery integrates them on the next boot.
                killed_total += await asyncio.to_thread(prov.cancel_all)
            if killed_total:
                logger.info("on_shutdown: killed %d runner processes", killed_total)
        except Exception:
            logger.exception("on_shutdown: provider.cancel_all failed")
    elif _intentional_shutdown:
        logger.info("on_shutdown: user chose to leave runners alive")
    else:
        logger.info("on_shutdown: reload detected, leaving runners alive for recovery")
    try:
        import native_transcript_index
        await asyncio.to_thread(native_transcript_index.shutdown)
    except Exception:
        logger.exception("native transcript index shutdown failed")
    await schedule_ticker.shutdown()
    global _project_match_executor, _project_match_ready, _project_match_warm_task
    if _project_match_warm_task is not None:
        _project_match_warm_task.cancel()
        _project_match_warm_task = None
    if _project_match_executor is not None:
        _project_match_executor.shutdown(wait=False, cancel_futures=True)
        _project_match_executor = None
        _project_match_ready = False
    try:
        await provider_setup.shutdown_provider_setup()
    except Exception:
        logger.exception("provider setup shutdown failed")
    try:
        from provider import shutdown_provider_tasks
        await shutdown_provider_tasks()
    except Exception:
        logger.exception("provider task shutdown failed")
    try:
        await shutdown_reconciles()
    except Exception:
        logger.exception("session reconcile shutdown failed")
    await asyncio.to_thread(shutdown_recovery_lease_executor)
    _HOT_PATH_EXECUTOR.shutdown(wait=False, cancel_futures=True)
    _SESSION_DETAIL_EXECUTOR.shutdown(wait=False, cancel_futures=True)
    _SESSION_LIST_EXECUTOR.shutdown(wait=False, cancel_futures=True)
    # Drain the draft-persist coalescer before closing the event
    # ingester. Drafts are kept in memory for up to DRAFT_FLUSH_DELAY
    # before hitting disk — without this drain a clean shutdown would
    # lose typed-but-unflushed draft text.
    try:
        coordinator.draft_store.drain_pending_drafts()
    except Exception:
        logger.exception("drain_pending_drafts failed")
    # Drain the per-root write_full debounce queue. drafts.discard
    # above may have enqueued additional pending writes (via
    # `_persist_root`'s debounce), so this MUST run after
    # `drain_pending_drafts`. Without it, a clean shutdown loses up
    # to PERSIST_DEBOUNCE_S of mutations sitting in `_persist_pending`.
    try:
        session_manager.flush_pending_persists()
    except Exception:
        logger.exception("flush_pending_persists failed")
    # Drain queue-recovery projection writes after session persists. On a
    # clean shutdown this keeps the projection usable as the fast startup
    # source of truth; on a crash, the startup manifest fingerprint still
    # detects changed session files and falls back to a full rebuild.
    try:
        import session_queue_projection
        from session_manager import (
            begin_queue_projection_shutdown,
            drain_queue_projection_submissions,
            shutdown_queue_projection_executor,
        )
        begin_queue_projection_shutdown()
        certification_generation = session_queue_projection.certification_generation()
        try:
            await asyncio.to_thread(drain_queue_projection_submissions)
            if session_queue_projection.flush_pending_writes(timeout=5.0):
                session_queue_projection.mark_current_if_generation(
                    certification_generation,
                )
        finally:
            await asyncio.to_thread(shutdown_queue_projection_executor)
    except Exception:
        logger.exception("queue projection flush_pending_writes failed")
    try:
        from event_journal import event_journal_writer
        await asyncio.to_thread(event_journal_writer.close)
    except Exception:
        logger.exception("EventJournalWriter close failed")
    try:
        from event_bus_subscribers import shutdown_session_content_projection
        await asyncio.to_thread(shutdown_session_content_projection)
    except Exception:
        logger.exception("session content projection shutdown failed")
    try:
        event_ingester.close_all()
    except Exception:
        logger.exception("EventIngester close_all failed")
    release_backend_instance_lock()
    # Multi-machine: cancel the offset coalescer + final-flush every
    # dirty node. Without this, intentional-shutdown races could leave
    # in-memory offsets stranded; the next register() would seed an
    # outdated snapshot.
    if extension_store.is_extension_runtime_ready(
        extension_store.extension_id_for_role('machine-nodes')
    ):
        try:
            import node_store as _ns
            await _ns.stop_offset_flush_loop()
        except Exception:
            logger.exception("node_store: offset flush loop stop failed")
    await coordinator.drain_global_broadcasts()
    shutdown_ws_json_executor()


# ============================================================================
# Internal Endpoints (manager runner → backend delegate fan-out)
# ============================================================================

@app.post("/api/internal/ask-fork")
async def internal_ask_fork(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    """The fork engine behind `ask(run_mode='fork')`. Spawns a target run on
    a per-(caller, target) fork, streams its events back over the originating
    app session's WebSocket, and returns the aggregate result payload
    (jsonl_path + byte offsets) the caller samples to verify the outcome.
    """
    with perf.timed("ask_fork.route"):
        if not coordinator.is_internal_caller(x_internal_token):
            raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
        if not body.get("worker_session_id"):
            raise HTTPException(
                status_code=400,
                detail="ask-fork requires worker_session_id",
            )
        worker_session_id = str(body.get("worker_session_id") or "").strip()
        if not await _session_exists(worker_session_id):
            raise HTTPException(status_code=404, detail=t("error.session_not_found"))
        requested_provider_id = await _resolve_provider_id_ref(
            str(body.get("provider_id") or "").strip(),
        )
        provisioned_tool_profile = _api_optional_provisioned_tool_profile(
            body.get("provisioned_tool_profile"),
            body,
        )
        try:
            return await coordinator.run_delegation(
                app_session_id=body["app_session_id"],
                instructions=body["instructions"],
                worker_session_id=worker_session_id,
                worker_description=str(body.get("worker_description") or ""),
                provider_id=requested_provider_id,
                model=body["model"],
                reasoning_effort=str(body.get("reasoning_effort") or "").strip(),
                cwd=body["cwd"],
                justification=body.get("justification"),
                proposed_orchestration_mode=body.get("proposed_orchestration_mode"),
                client_delegation_id=body.get("client_delegation_id"),
                node_id=body.get("node_id"),
                run_mode=body.get("run_mode") or "fork",
                worker_registry_cwd=body.get("worker_registry_cwd"),
                ephemeral=body.get("ephemeral") is True,
                machine_completion=body.get("machine_completion") is True,
                provision_prompt=_api_optional_provision_prompt(body.get("provision_prompt")),
                provisioned_tool_profile=provisioned_tool_profile,
                include_events=body.get("include_events") is True,
            )
        except DelegateForkParentMissing as exc:
            # Race: the parent agent session vanished between the
            # worker_session existence check above and fork creation
            # (delete/eviction, or a stale/unknown agent session id).
            # Map to 409 instead of letting the strict-mode KeyError
            # surface as a bare 500. Catches ONLY this typed subclass so
            # unrelated KeyErrors still propagate as real errors.
            raise HTTPException(
                status_code=409,
                detail="parent agent session no longer available for fork",
            ) from exc


# Max chars accepted for a headless-generate prompt. Bounds the blast
# radius of an over-large client-supplied prompt; generous enough for a
# composer draft plus the extension's wrapping instruction.
_HEADLESS_GENERATE_MAX_PROMPT = 16_000
_HEADLESS_GENERATE_TIMEOUT = 60.0


@app.post("/api/internal/headless-generate")
async def internal_headless_generate(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    """One-shot, tool-less, render-tree-invisible text generation seeded
    with a session's conversation.

    Forks the session's provider sid (`--fork-session`) so the user's real
    conversation is never mutated, runs with EVERY built-in tool disabled
    (`no_tools=True`) so a generation can only produce text, and returns
    `{text}` synchronously. Leaves zero footprint in the session render
    tree / events.jsonl. Backs the
    composer-fill extension; internal-token callers only.
    """
    if not coordinator.is_internal_caller(x_internal_token):
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    session_id = str(body.get("session_id") or "").strip()
    prompt = str(body.get("prompt") or "").strip()
    if not session_id or not prompt:
        raise HTTPException(status_code=400, detail="session_id and prompt are required")
    if len(prompt) > _HEADLESS_GENERATE_MAX_PROMPT:
        raise HTTPException(status_code=413, detail="prompt too long")
    session = await _session_lite(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=t("error.session_not_found"))
    agent_sid = str(session.get("agent_session_id") or "").strip()
    if not agent_sid:
        # Fail closed: no provider sid to fork from yet (session never ran).
        raise HTTPException(status_code=409, detail="session has no provider session yet")
    provider = await asyncio.to_thread(coordinator.provider_for_session, session_id)
    # Fail closed: only providers that can fork (no real-session mutation)
    # AND guarantee a tool-less run may serve a fill.
    if not provider.supports_fork or not provider.supports_headless_no_tools:
        raise HTTPException(
            status_code=422, detail="session provider cannot run a tool-less fork",
        )
    result = await provider.run_headless(
        prompt=prompt,
        resume_sid=agent_sid,
        fork=True,
        no_tools=True,
        cwd=str(session.get("cwd") or "") or None,
        timeout=_HEADLESS_GENERATE_TIMEOUT,
    )
    if not result or result.get("is_error"):
        raise HTTPException(status_code=502, detail="generation failed")
    return {"text": str(result.get("result") or "")}


@app.post("/api/internal/delegate-task")
async def internal_delegate_task(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    """The `delegate_task` tool's backend: smart router. Per the global
    delegate_task_policy, resolves a target (caller-supplied → search first
    suggestion → create new), optionally gates on user approval, then dispatches
    the task detached (does NOT join the sender's turn). Generic — available to
    any session."""
    if not coordinator.is_internal_caller(x_internal_token):
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    sender_session_id = str(body.get("sender_session_id") or "").strip()
    task = str(body.get("task") or "").strip()
    if not sender_session_id or not task:
        raise HTTPException(
            status_code=400,
            detail="sender_session_id and task are required",
        )
    target = body.get("target_session_id")
    if target in ("", "null"):
        target = None
    raw_provider_id = str(body.get("provider_id") or "").strip()
    if target:
        requested_provider_id = await _resolve_provider_id_ref(raw_provider_id) if raw_provider_id else ""
    elif raw_provider_id.upper() == "ANY":
        requested_provider_id = "ANY"
    elif raw_provider_id:
        requested_provider_id = await _resolve_provider_id_ref(raw_provider_id)
    else:
        # Omitted provider means delegate auto-routing searches the same global
        # corpus the session-list AI search uses. If no existing target fits,
        # the coordinator still creates a fallback target from the sender's
        # provider/model.
        requested_provider_id = ""
    requested_model = str(body.get("model") or "").strip()
    model = requested_model
    run_provider_id = "" if requested_provider_id.upper() == "ANY" else requested_provider_id
    if requested_model or run_provider_id:
        sender = await _session_lite(sender_session_id)
        provider_id = run_provider_id or str((sender or {}).get("provider_id") or "").strip() or None
        if not model and run_provider_id:
            provider = await asyncio.to_thread(config_store.get_provider, provider_id) or {}
            model = str(provider.get("default_model") or "").strip()
            if not model:
                name = provider.get("name") or provider_id
                raise HTTPException(status_code=400, detail=f"{name} has no default model configured")
        if not model:
            model = str((sender or {}).get("model") or "").strip()
        await asyncio.to_thread(_validate_provider_model, provider_id, model)
    try:
        return await coordinator.run_delegate_task(
            sender_session_id=sender_session_id,
            task=task,
            target_session_id=target,
            provider_id=requested_provider_id,
            model=model,
            reasoning_effort=str(body.get("reasoning_effort") or "").strip(),
            sub_session=body.get("sub_session") is not False,
            cwd=str(body.get("cwd") or ""),
            run_mode=str(body.get("run_mode") or "direct").strip() or "direct",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _require_team_orchestration_internal(x_internal_token: str) -> None:
    if not coordinator.is_internal_caller(x_internal_token):
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    _require_builtin_runtime_extension(extension_store.extension_id_for_role('team-orchestration'))


@app.post("/api/internal/delegate-task-policy/get")
async def internal_get_delegate_task_policy_endpoint(
    body: dict | None = None,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    _require_team_orchestration_internal(x_internal_token)
    policy = await asyncio.to_thread(config_store.get_delegate_task_policy)
    return {"policy": policy}


@app.post("/api/internal/delegate-task-policy/set")
async def internal_set_delegate_task_policy_endpoint(
    body: dict = Body(default={}),
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    _require_team_orchestration_internal(x_internal_token)
    policy = await asyncio.to_thread(
        config_store.set_delegate_task_policy,
        str(body.get("policy") or ""),
    )
    return {"policy": policy}


@app.get("/api/settings/internal-llm")
async def get_internal_llm_endpoint():
    """Tasks: the known internal-LLM task keys. Assignments: the stored
    {task → {provider_id?, model?, reasoning_effort?}} map (missing fields
    mean "inherit active provider")."""
    assignments = await asyncio.to_thread(config_store.get_internal_llm_assignments)
    extension_tasks = await asyncio.to_thread(extension_store.extension_internal_llm_task_keys)
    tasks = await asyncio.to_thread(config_store.internal_llm_tasks)
    labels = extension_store.internal_llm_task_labels()
    return {
        "tasks": [task for task in tasks if task not in extension_tasks],
        "labels": {key: label for key, label in labels.items() if key not in extension_tasks},
        "assignments": {
            key: value
            for key, value in assignments.items()
            if key not in extension_tasks
        },
    }


@app.put("/api/settings/internal-llm")
async def set_internal_llm_endpoint(body: dict):
    raw_assignments = body.get("assignments") or {}
    if not isinstance(raw_assignments, dict):
        raise HTTPException(status_code=400, detail="assignments must be an object")
    extension_tasks = await asyncio.to_thread(extension_store.extension_internal_llm_task_keys)
    forbidden = sorted(str(key) for key in raw_assignments if str(key) in extension_tasks)
    if forbidden:
        raise HTTPException(status_code=403, detail="extension-owned internal LLM tasks must be edited in extension settings")
    current = await asyncio.to_thread(config_store.get_internal_llm_assignments)
    merged = {
        key: value
        for key, value in current.items()
        if key in extension_tasks
    }
    merged.update(raw_assignments)
    assignments = await asyncio.to_thread(
        config_store.set_internal_llm_assignments,
        merged,
    )
    await coordinator.broadcast_global("internal_llm_changed", {})
    return {
        "assignments": {
            key: value
            for key, value in assignments.items()
            if key not in extension_tasks
        }
    }


@app.post("/api/internal/team-definitions/list")
async def internal_list_extension_team_definitions(
    body: dict | None = None,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    _require_team_orchestration_internal(x_internal_token)
    try:
        return {"team_definitions": extension_store.team_definition_sources()}
    except extension_store.ExtensionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/internal/team-definitions/plan")
async def internal_plan_team_definition(
    body: dict = Body(default={}),
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    _require_team_orchestration_internal(x_internal_token)
    import team_definitions

    try:
        return {
            "plan": team_definitions.build_plan(
                source_id=str((body or {}).get("source_id") or ""),
                profile=str((body or {}).get("profile") or ""),
                team_instance_id=str((body or {}).get("team_instance_id") or ""),
                variables=(body or {}).get("variables") or {},
            )
        }
    except (extension_store.ExtensionError, team_definitions.TeamDefinitionError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/internal/teams/create")
async def internal_create_team(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    _require_builtin_runtime_extension(extension_store.extension_id_for_role('team-orchestration'))
    if not coordinator.is_internal_caller(x_internal_token):
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    import team_store

    root_session_id = str(body.get("root_session_id") or "").strip()
    if not root_session_id:
        raise HTTPException(status_code=400, detail="root_session_id is required")
    if not await _session_lite(root_session_id):
        raise HTTPException(status_code=400, detail="root_session_id does not exist")
    try:
        team = team_store.create(
            root_session_id=root_session_id,
            definition_ref=str(body.get("definition_ref") or "").strip(),
            profile=str(body.get("profile") or "").strip(),
            team_id=str(body.get("team_id") or "").strip() or None,
        )
    except team_store.TeamStoreError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"success": True, "team": team}


@app.post("/api/internal/teams/register-member")
async def internal_register_team_member(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    _require_builtin_runtime_extension(extension_store.extension_id_for_role('team-orchestration'))
    if not coordinator.is_internal_caller(x_internal_token):
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    import team_store

    provider_id = await _resolve_provider_id_ref(str(body.get("provider_id") or ""))
    try:
        member = team_store.upsert_member(
            str(body.get("team_instance_id") or ""),
            member_id=str(body.get("member_id") or ""),
            member_type=str(body.get("member_type") or ""),
            agent_session_id=str(body.get("agent_session_id") or ""),
            role=str(body.get("role") or ""),
            description=str(body.get("description") or ""),
            cwd=str(body.get("cwd") or ""),
            provider_id=provider_id,
            model=str(body.get("model") or ""),
            reasoning_effort=str(body.get("reasoning_effort") or ""),
            run_mode=str(body.get("run_mode") or ""),
            parent_member_id=str(body.get("parent_member_id") or ""),
            status=str(body.get("status") or "active"),
            nested_team_id=str(body.get("nested_team_id") or ""),
        )
    except team_store.TeamStoreError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"success": True, "member": member}


@app.post("/api/internal/team-definitions/activate")
async def internal_activate_team_definition(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    _require_builtin_runtime_extension(extension_store.extension_id_for_role('team-orchestration'))
    if not coordinator.is_internal_caller(x_internal_token):
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    import team_activation_store
    import team_definitions

    root_session_id = str(body.get("root_session_id") or "").strip()
    if not root_session_id:
        raise HTTPException(status_code=400, detail="root_session_id is required")
    if not await _session_lite(root_session_id):
        raise HTTPException(status_code=400, detail="root_session_id does not exist")
    raw_plan = body.get("plan")
    if raw_plan is not None and not isinstance(raw_plan, dict):
        raise HTTPException(status_code=400, detail="plan must be an object")
    team_instance_id = str(
        (raw_plan or {}).get("team_instance_id")
        or body.get("team_instance_id")
        or f"team-{uuid.uuid4().hex}"
    ).strip()
    if raw_plan is None:
        try:
            raw_plan = team_definitions.build_plan(
                source_id=str(body.get("source_id") or ""),
                profile=str(body.get("profile") or ""),
                team_instance_id=team_instance_id,
                variables=body.get("variables") if isinstance(body.get("variables"), dict) else {},
            )
        except (extension_store.ExtensionError, team_definitions.TeamDefinitionError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    elif not str(raw_plan.get("team_instance_id") or "").strip():
        raw_plan = {**raw_plan, "team_instance_id": team_instance_id}
    activation = team_activation_store.create(
        root_session_id=root_session_id,
        team_instance_id=team_instance_id,
        source_id=str(raw_plan.get("source_id") or body.get("source_id") or ""),
        profile=str(raw_plan.get("profile") or body.get("profile") or ""),
    )
    asyncio.create_task(
        _run_team_definition_activation(
            activation["id"],
            root_session_id=root_session_id,
            plan=raw_plan,
            default_cwd=str(body.get("cwd") or ""),
            bare_config=body.get("bare_config") is True,
        ),
        name=f"team-activation-{activation['id']}",
    )
    return {"success": True, "activation": activation}


@app.get("/api/internal/team-definitions/activate/{activation_id}")
async def internal_get_team_definition_activation(
    activation_id: str,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    _require_builtin_runtime_extension(extension_store.extension_id_for_role('team-orchestration'))
    if not coordinator.is_internal_caller(x_internal_token):
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    import team_activation_store

    try:
        activation = team_activation_store.get(activation_id)
    except team_activation_store.TeamActivationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if activation is None:
        raise HTTPException(status_code=404, detail="activation_id does not exist")
    return {"success": True, "activation": activation}


async def _run_team_definition_activation(
    activation_id: str,
    *,
    root_session_id: str,
    plan: dict,
    default_cwd: str,
    bare_config: bool,
) -> None:
    import team_activation_store
    import team_store

    try:
        team_id = str(plan.get("team_instance_id") or "").strip()
        profile = str(plan.get("profile") or "").strip()
        source_id = str(plan.get("source_id") or "").strip()
        team_activation_store.append_step(activation_id, "create runtime team")
        team = team_store.create(
            root_session_id=root_session_id,
            definition_ref=source_id,
            profile=profile,
            team_id=team_id,
        )
        manager = plan.get("manager") if isinstance(plan.get("manager"), dict) else {}
        team_activation_store.append_step(activation_id, "register manager")
        team_store.upsert_member(
            team_id,
            member_id="manager",
            member_type="manager",
            agent_session_id=root_session_id,
            role="manager",
            description=str(manager.get("id") or "manager"),
            cwd=str(manager.get("cwd") or default_cwd),
            provider_id=str(manager.get("provider_id") or ""),
            model=str(manager.get("model") or ""),
            reasoning_effort=str(manager.get("reasoning_effort") or ""),
            run_mode=str(manager.get("run_mode") or "direct"),
        )
        workers = plan.get("activate")
        if not isinstance(workers, list):
            raise ValueError("plan.activate must be a list")
        for worker in workers:
            if not isinstance(worker, dict):
                raise ValueError("plan.activate items must be objects")
            team_activation_store.append_step(
                activation_id,
                f"provision {worker.get('member_id') or worker.get('role_key') or 'worker'}",
                status="running",
            )
            result = await _provision_workers_from_body(
                {
                    "cwd": default_cwd,
                    "team_instance_id": team_id,
                    "bare_config": bare_config,
                    "workers": [worker],
                }
            )
            team_activation_store.append_step(
                activation_id,
                f"provisioned {worker.get('member_id') or worker.get('role_key') or 'worker'}",
                data=result,
            )
        team_activation_store.complete(
            activation_id,
            {"team": team_store.get(team_id) or team, "plan": plan},
        )
    except Exception as exc:
        team_activation_store.fail(activation_id, str(exc))




@app.post("/api/internal/create-worker")
async def internal_create_worker(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    _require_builtin_runtime_extension(extension_store.extension_id_for_role('team-orchestration'))
    if not coordinator.is_internal_caller(x_internal_token):
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    app_session_id = str(body.get("app_session_id") or "")
    requested_model = str(body.get("model") or "").strip()
    if requested_model:
        caller = await _session_lite(app_session_id)
        provider_id = str((caller or {}).get("provider_id") or "").strip() or None
        _validate_provider_model(provider_id, requested_model)
    return await coordinator.create_worker_for_session(
        app_session_id=app_session_id,
        worker_description=str(body.get("worker_description") or ""),
        justification=str(body.get("justification") or ""),
        proposed_orchestration_mode=str(body.get("orchestration_mode") or ""),
        model=requested_model,
        cwd=str(body.get("cwd") or ""),
        client_request_id=str(body.get("client_request_id") or "") or None,
        node_id=str(body.get("node_id") or "") or None,
    )


@app.post("/api/internal/create-session")
async def internal_create_session(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    """Low-level session creation exposed to agents: mint a standalone BC
    session (NOT a team worker — no roster registration, no approval, no init
    turn). Pairs with delegate/mssg/ask to spin up a fresh session to hand
    work off to. For a session that joins the team's worker roster, use
    /api/internal/create-worker instead."""
    if not coordinator.is_internal_caller(x_internal_token):
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    name = str(body.get("name") or "").strip()
    cwd = str(body.get("cwd") or "").strip()
    if not name or not cwd:
        raise HTTPException(status_code=400, detail="name and cwd are required")
    mode = str(body.get("orchestration_mode") or "native").strip() or "native"
    if mode == "manager":
        mode = "team"
    if mode not in ("team", "native"):
        raise HTTPException(status_code=400, detail="orchestration_mode must be 'team' or 'native'")
    bare_config = body.get("bare_config", False)
    if not isinstance(bare_config, bool):
        raise HTTPException(status_code=400, detail="bare_config must be a boolean")
    try:
        capability_contexts = normalize_capability_contexts(body.get("capability_contexts"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    sender_session_id = str(body.get("sender_session_id") or "").strip()
    sender_session = await _session_lite(sender_session_id) if sender_session_id else None
    if sender_session_id and not sender_session:
        raise HTTPException(status_code=400, detail="sender_session_id does not exist")
    requested_provider_id = await _resolve_provider_id_ref(
        str(body.get("provider_id") or "").strip(),
    )
    provider_id = requested_provider_id
    if not provider_id and sender_session:
        provider_id = str(sender_session.get("provider_id") or "").strip()
    provider_id = provider_id or None
    if provider_id and not await asyncio.to_thread(config_store.get_provider, provider_id):
        raise HTTPException(status_code=400, detail="provider_id does not exist")
    requested_model = str(body.get("model") or "").strip()
    model = ""
    if requested_model:
        model = requested_model
    elif requested_provider_id and provider_id:
        provider = await asyncio.to_thread(config_store.get_provider, provider_id) or {}
        model = await asyncio.to_thread(_required_model_from_body_or_provider, {}, provider)
    elif sender_session:
        model = str(sender_session.get("model") or "").strip()
    if not model and provider_id:
        provider = await asyncio.to_thread(config_store.get_provider, provider_id) or {}
        model = await asyncio.to_thread(_required_model_from_body_or_provider, {}, provider)
    if requested_model or requested_provider_id:
        await asyncio.to_thread(_validate_provider_model, provider_id, model)
    requested_effort = body.get("reasoning_effort")
    reasoning_effort: object = requested_effort
    if (reasoning_effort is None or not str(reasoning_effort).strip()) and sender_session:
        reasoning_effort = str(sender_session.get("reasoning_effort") or "").strip()
    reasoning_effort = await asyncio.to_thread(
        _provider_reasoning_effort,
        provider_id,
        _api_reasoning_effort(reasoning_effort),
    )
    node_id = str(body.get("node_id") or "").strip() or "primary"
    if not model:
        model = await asyncio.to_thread(config_store.default_session_model)
    sess = await asyncio.to_thread(
        lambda: session_manager.create(
            name=name,
            cwd=cwd,
            orchestration_mode=mode,
            model=model,
            provider_id=provider_id,
            reasoning_effort=reasoning_effort,
            node_id=node_id,
            source="cli",
            bare_config=bare_config,
            capability_contexts=capability_contexts,
        )
    )
    if sender_session_id:
        await coordinator.emit_session_created_panel(
            sender_session_id=sender_session_id,
            target_session=sess,
        )
    _ext_id = coordinator.principal_extension_id(x_internal_token) or ""
    if _ext_id and extension_store.is_extension_active(_ext_id):
        import extension_session_ownership
        extension_session_ownership.claim(sess["id"], _ext_id)
    return {
        "success": True,
        "session_id": sess["id"],
        "name": sess.get("name") or name,
        "cwd": sess.get("cwd") or cwd,
        "orchestration_mode": mode,
        "node_id": node_id,
        "provider_id": sess.get("provider_id"),
        "model": sess.get("model"),
        "reasoning_effort": sess.get("reasoning_effort"),
        "bare_config": sess.get("bare_config"),
        "capability_contexts": sess.get("capability_contexts") or [],
    }


@app.post("/api/internal/create-sub-session")
async def internal_create_sub_session(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    if not coordinator.is_internal_caller(x_internal_token):
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    parent_session_id = str(body.get("sender_session_id") or "").strip()
    description = str(body.get("description") or "").strip()
    if not parent_session_id:
        raise HTTPException(status_code=400, detail="sender_session_id is required")
    parent = await _session_lite(parent_session_id)
    if not parent:
        raise HTTPException(status_code=400, detail="sender_session_id does not exist")

    requested_provider_id = await _resolve_provider_id_ref(
        str(body.get("provider_id") or "").strip(),
    )
    provider_id = requested_provider_id or str(parent.get("provider_id") or "").strip()
    provider_id = provider_id or None
    if provider_id and not await asyncio.to_thread(config_store.get_provider, provider_id):
        raise HTTPException(status_code=400, detail="provider_id does not exist")
    requested_model = str(body.get("model") or "").strip()
    model = requested_model
    if not model and requested_provider_id and provider_id:
        provider = await asyncio.to_thread(config_store.get_provider, provider_id) or {}
        model = str(provider.get("default_model") or "").strip()
        if not model:
            name = provider.get("name") or provider_id
            raise HTTPException(status_code=400, detail=f"{name} has no default model configured")
    if not model:
        model = str(parent.get("model") or "").strip()
    if not model and provider_id:
        provider = await asyncio.to_thread(config_store.get_provider, provider_id) or {}
        model = str(provider.get("default_model") or "").strip()
    if requested_model or requested_provider_id:
        await asyncio.to_thread(_validate_provider_model, provider_id, model)
    requested_effort = body.get("reasoning_effort")
    reasoning_effort: object = requested_effort
    if reasoning_effort is None or not str(reasoning_effort).strip():
        reasoning_effort = str(parent.get("reasoning_effort") or "").strip()
    reasoning_effort = await asyncio.to_thread(
        _provider_reasoning_effort,
        provider_id,
        _api_reasoning_effort(reasoning_effort),
    )
    cwd = str(body.get("cwd") or "").strip() or str(parent.get("cwd") or "").strip()
    node_id = str(body.get("node_id") or "").strip() or str(parent.get("node_id") or "primary")
    disallowed_tools = _api_disallowed_tools(body.get("disallowed_tools"))
    disabled_builtin_extensions = _api_disabled_builtin_extensions(body.get("disabled_builtin_extensions"))
    name = description or "sub-session"

    try:
        sub = await asyncio.to_thread(
            lambda: session_manager.create_sub_session(
                parent_session_id=parent_session_id,
                name=name,
                model=model,
                provider_id=provider_id,
                reasoning_effort=reasoning_effort,
                cwd=cwd,
                node_id=node_id,
                disallowed_tools=disallowed_tools,
                disabled_builtin_extensions=disabled_builtin_extensions,
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await coordinator.emit_session_created_panel(
        sender_session_id=parent_session_id,
        target_session=sub,
    )
    _ext_id = coordinator.principal_extension_id(x_internal_token) or ""
    if _ext_id and extension_store.is_extension_active(_ext_id):
        import extension_session_ownership
        extension_session_ownership.claim(sub["id"], _ext_id)
    return {
        "success": True,
        "target_session_id": sub["id"],
        "name": sub.get("name") or name,
        "cwd": sub.get("cwd") or cwd,
        "orchestration_mode": sub.get("orchestration_mode") or "native",
        "node_id": sub.get("node_id") or node_id,
        "provider_id": sub.get("provider_id"),
        "model": sub.get("model"),
        "reasoning_effort": sub.get("reasoning_effort"),
        "disallowed_tools": sub.get("disallowed_tools") or [],
        "disabled_builtin_extensions": sub.get("disabled_builtin_extensions") or [],
    }


def _require_extension_session_ownership(
    x_internal_token: str, body: dict,
) -> tuple[str, str]:
    """SDK session-message mutation gate: verify the internal token AND that
    the calling extension owns the session named in the body. The caller's
    identity is derived from its per-extension token (the X-Extension-Id
    header is ignored), so an extension can only mutate sessions it created —
    never arbitrary sessions, and never by spoofing another extension's id.
    Returns (extension_id, session_id)."""
    if not coordinator.is_internal_caller(x_internal_token):
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    extension_id = coordinator.principal_extension_id(x_internal_token) or ""
    session_id = str((body or {}).get("session_id") or "").strip()
    import extension_session_ownership
    if not extension_session_ownership.is_owner(session_id, extension_id):
        raise HTTPException(status_code=403, detail="extension does not own this session")
    return extension_id, session_id


def _require_extension_permission(x_internal_token: str, permission: str) -> str:
    if not coordinator.is_internal_caller(x_internal_token):
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    extension_id = coordinator.principal_extension_id(x_internal_token) or ""
    record = extension_store.get_extension(extension_id) if extension_id else None
    if (
        record is None
        or not extension_store.is_extension_active(extension_id)
        or not extension_store.has_permission(record, permission)
    ):
        raise HTTPException(status_code=403, detail=f"extension lacks {permission} permission")
    return extension_id


@app.post("/api/internal/virtual-sessions/upsert")
async def internal_virtual_sessions_upsert(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    extension_id = _require_extension_permission(x_internal_token, "session_state")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")
    try:
        existing = await asyncio.to_thread(
            virtual_session_store.get,
            str(body.get("id") or ""),
        )
        session = await asyncio.to_thread(virtual_session_store.upsert, extension_id, body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    await coordinator.broadcast_global(
        "session_metadata_updated" if existing else "session_created",
        {"session_id": session["id"], "patch": session} if existing else {"session": session},
    )
    return {"success": True, "session": session}


@app.post("/api/internal/virtual-sessions/delete")
async def internal_virtual_sessions_delete(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    extension_id = _require_extension_permission(x_internal_token, "session_state")
    session_id = str((body or {}).get("session_id") or "").strip()
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id is required")
    try:
        deleted = await asyncio.to_thread(virtual_session_store.delete, extension_id, session_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if deleted:
        await coordinator.broadcast_global("session_deleted", {"session_id": session_id})
    return {"success": True, "deleted": deleted}


@app.post("/api/internal/virtual-sessions/append-message")
async def internal_virtual_sessions_append_message(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    extension_id = _require_extension_permission(x_internal_token, "session_state")
    session_id = str((body or {}).get("session_id") or "").strip()
    message = (body or {}).get("message")
    if not session_id or not isinstance(message, dict):
        raise HTTPException(status_code=400, detail="session_id and message are required")
    try:
        appended = await asyncio.to_thread(
            virtual_session_store.append_message,
            extension_id,
            session_id,
            message,
        )
        session = await asyncio.to_thread(virtual_session_store.get, session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ValueError, PermissionError) as exc:
        status = 403 if isinstance(exc, PermissionError) else 400
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    await coordinator.broadcast_global(
        "session_metadata_updated",
        {"session_id": session_id, "patch": session or {}},
    )
    return {"success": True, "message": appended, "session": session}


@app.post("/api/internal/synthetic-messages/inject")
async def internal_synthetic_messages_inject(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    _require_extension_permission(x_internal_token, "spawn_runs")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")
    session_id = str(body.get("session_id") or "").strip()
    prompt = str(body.get("prompt") or "")
    try:
        return await synthetic_messages.inject(
            coordinator,
            session_id,
            prompt=prompt,
            model=str(body.get("model") or ""),
            cwd=str(body.get("cwd") or ""),
            orchestration_mode=str(body.get("orchestration_mode") or ""),
            client_id=str(body.get("client_id") or ""),
            source=str(body.get("source") or "synthetic"),
            display_prompt=str(body.get("display_prompt") or ""),
            capability_contexts=(
                body.get("capability_contexts")
                if isinstance(body.get("capability_contexts"), list)
                else None
            ),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/internal/managed-runs/run")
async def internal_managed_run(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    extension_id = _require_extension_permission(x_internal_token, "spawn_runs")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")
    managed_session_id = str(body.get("managed_session_id") or "").strip()
    if not managed_session_id:
        raise HTTPException(status_code=400, detail="managed_session_id is required")
    import extension_session_ownership
    if not extension_session_ownership.is_owner(managed_session_id, extension_id):
        raise HTTPException(status_code=403, detail="extension does not own this managed session")
    record = extension_store.get_extension(extension_id)
    declared_env = set(
        ((record or {}).get("manifest") or {}).get("permissions", {}).get("managed_run_env") or []
    )
    raw_env = body.get("extra_env") or {}
    if not isinstance(raw_env, dict):
        raise HTTPException(status_code=400, detail="extra_env must be an object")
    extra_env: dict[str, str] = {}
    for key, value in raw_env.items():
        if not isinstance(key, str):
            raise HTTPException(status_code=400, detail="invalid extra_env key")
        key = key.strip()
        if not key or "\x00" in key:
            raise HTTPException(status_code=400, detail="invalid extra_env key")
        if key not in declared_env:
            raise HTTPException(
                status_code=403,
                detail=f"extra_env key is not declared in permissions.managed_run_env: {key}",
            )
        value = str(value)
        if "\x00" in value or len(value) > 4096:
            raise HTTPException(status_code=400, detail=f"invalid extra_env value for {key}")
        extra_env[key] = value
    import extension_managed_runs
    try:
        return await extension_managed_runs.run(
            coordinator,
            managed_session_id=managed_session_id,
            parent_session_id=str(body.get("parent_session_id") or "").strip(),
            prompt=str(body.get("prompt") or ""),
            model=str(body.get("model") or ""),
            cwd=str(body.get("cwd") or ""),
            init_prompt=str(body.get("init_prompt") or ""),
            agent_sid=str(body.get("agent_sid") or "").strip(),
            event_prefix=str(body.get("event_prefix") or "managed_run").strip(),
            extra_env=extra_env,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/internal/managed-runs/create-session")
async def internal_managed_run_create_session(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    extension_id = _require_extension_permission(x_internal_token, "spawn_runs")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")
    parent_session_id = str(body.get("parent_session_id") or "").strip()
    parent = await _session_lite(parent_session_id) if parent_session_id else None
    if parent_session_id and not parent:
        raise HTTPException(status_code=400, detail="parent_session_id does not exist")
    name = str(body.get("name") or "").strip()
    cwd = str(body.get("cwd") or "").strip() or str((parent or {}).get("cwd") or "").strip()
    if not name or not cwd:
        raise HTTPException(status_code=400, detail="name and cwd are required")
    requested_provider_id = await _resolve_provider_id_ref(
        str(body.get("provider_id") or "").strip(),
    )
    provider_id = requested_provider_id or str((parent or {}).get("provider_id") or "").strip()
    provider_id = provider_id or None
    if provider_id and not await asyncio.to_thread(config_store.get_provider, provider_id):
        raise HTTPException(status_code=400, detail="provider_id does not exist")
    model = str(body.get("model") or "").strip() or str((parent or {}).get("model") or "").strip()
    if not model:
        model = await asyncio.to_thread(config_store.default_session_model)
    node_id = str(body.get("node_id") or "").strip() or str((parent or {}).get("node_id") or "primary")
    sess = await asyncio.to_thread(
        lambda: session_manager.create(
            name=name,
            cwd=cwd,
            orchestration_mode="native",
            model=model,
            provider_id=provider_id,
            node_id=node_id,
            source="extension",
            browser_harness_enabled=False,
        )
    )
    import extension_session_ownership
    extension_session_ownership.claim(sess["id"], extension_id)
    if parent_session_id:
        await coordinator.emit_session_created_panel(
            sender_session_id=parent_session_id,
            target_session=sess,
        )
    return {
        "success": True,
        "session_id": sess["id"],
        "name": sess.get("name") or name,
        "cwd": sess.get("cwd") or cwd,
        "model": sess.get("model"),
        "provider_id": sess.get("provider_id"),
        "node_id": sess.get("node_id"),
    }


@app.post("/api/internal/headless-run")
async def internal_headless_run(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    """SDK: one-shot non-interactive provider run (``claude -p`` style) — a
    fresh or forked headless run that returns a raw LLM result, NOT a managed
    turn touching the render tree. Gated by ``spawn_runs`` (same trust level
    as ask-fork / provisioned sessions). Used by externalized lifecycle
    extensions that need a raw result without a session turn."""
    extension_id = _require_extension_permission(x_internal_token, "spawn_runs")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")
    prompt = str(body.get("prompt") or "")
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt is required")
    cwd = str(body.get("cwd") or "").strip() or None
    try:
        result = await provider.default_provider().run_headless(
            prompt=prompt,
            session_id=(str(body.get("session_id") or "").strip() or None),
            resume_sid=(str(body.get("resume_sid") or "").strip() or None),
            fork=bool(body.get("fork")),
            cwd=cwd,
            timeout=float(body.get("timeout")) if body.get("timeout") is not None else None,
        )
    except Exception as exc:
        logger.exception("headless-run failed")
        raise HTTPException(status_code=500, detail=f"headless run failed: {exc}") from exc
    if result is None:
        raise HTTPException(status_code=500, detail="headless run returned no result")
    logger.info("headless-run by extension %s (fork=%s)", extension_id, bool(body.get("fork")))
    return result


@app.post("/api/internal/session-messages/append")
async def internal_session_messages_append(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    """SDK: append a user or assistant message to a session the calling
    extension owns."""
    _extension_id, session_id = _require_extension_session_ownership(x_internal_token, body)
    import uuid
    from datetime import datetime
    role = str(body.get("role") or "").strip()
    content = str(body.get("content") or "")
    if role not in ("user", "assistant"):
        raise HTTPException(status_code=400, detail="role must be 'user' or 'assistant'")
    msg = {
        "id": str(body.get("message_id") or uuid.uuid4().hex),
        "role": role,
        "content": content,
        "timestamp": str(body.get("timestamp") or datetime.now().isoformat()),
    }
    if role == "user":
        result = await asyncio.to_thread(
            session_manager.append_user_msg,
            session_id,
            msg,
        )
    else:
        msg.setdefault("events", [])
        msg["isStreaming"] = bool(body.get("is_streaming", False))
        result = await asyncio.to_thread(
            session_manager.append_assistant_msg,
            session_id,
            msg,
        )
    if result is None:
        raise HTTPException(status_code=404, detail="session not found")
    return {"success": True, "message": msg}


@app.post("/api/internal/session-messages/update-content")
async def internal_session_messages_update_content(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    """SDK: overwrite the content of a message on an extension-owned session."""
    _extension_id, session_id = _require_extension_session_ownership(x_internal_token, body)
    msg_id = str(body.get("message_id") or "").strip()
    content = str(body.get("content") or "")
    if not msg_id:
        raise HTTPException(status_code=400, detail="message_id is required")
    result = await asyncio.to_thread(
        session_manager.update_running_content,
        session_id,
        msg_id,
        content,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="session or message not found")
    return {"success": True}


@app.post("/api/internal/session-messages/set-streaming")
async def internal_session_messages_set_streaming(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    """SDK: set the streaming flag on a message of an extension-owned session.

    NOTE: the session_manager ``streaming_set`` change-kind is currently dropped
    by the WS broadcaster (known gap), so this persists the flag but may not
    reach the UI live until that change-kind is mapped."""
    _extension_id, session_id = _require_extension_session_ownership(x_internal_token, body)
    msg_id = str(body.get("message_id") or "").strip()
    if not msg_id:
        raise HTTPException(status_code=400, detail="message_id is required")
    result = await asyncio.to_thread(
        session_manager.set_streaming,
        session_id,
        msg_id,
        bool(body.get("streaming")),
    )
    if result is None:
        raise HTTPException(status_code=404, detail="session or message not found")
    return {"success": True}


@app.post("/api/internal/session-field")
async def internal_session_field(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    """SDK: scoped mutation of a session-record field the caller does NOT own.

    The acting extension must have declared ``field`` under
    ``permissions.mutates_session_fields``; core enforces that allowlist and
    routes the write to the matching session_manager setter (no raw record
    write, no render-tree/apply_event path). Lets an externalized lifecycle
    extension (e.g. supervisor stamping a verdict) update session metadata it
    didn't create, without a blanket write grant."""
    if not coordinator.is_internal_caller(x_internal_token):
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    extension_id = coordinator.principal_extension_id(x_internal_token) or ""
    field = str(body.get("field") or "").strip()
    allowed = extension_store.session_field_allowlist(extension_id)
    if not allowed:
        raise HTTPException(status_code=403, detail="extension lacks mutates_session_fields permission")
    if field not in allowed:
        raise HTTPException(status_code=403, detail=f"extension may not mutate session field: {field}")
    session_id = str(body.get("session_id") or "").strip()
    if not await _session_exists(session_id):
        raise HTTPException(status_code=404, detail="session not found")
    try:
        updated = await asyncio.to_thread(
            session_manager.apply_session_field,
            session_id,
            field,
            body.get("value"),
        )
        if updated is None:
            raise HTTPException(status_code=404, detail="session not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    logger.info("session-field %s mutated on %s by extension %s", field, session_id, extension_id)
    return {"success": True}


@app.post("/api/internal/session-fields")
async def internal_session_fields(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    if not coordinator.is_internal_caller(x_internal_token):
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    extension_id = coordinator.principal_extension_id(x_internal_token) or ""
    if not extension_id or not extension_store.is_extension_active(extension_id):
        raise HTTPException(status_code=403, detail="extension is not active")
    session_id = str(body.get("session_id") or "").strip()
    if not await _session_exists(session_id):
        raise HTTPException(status_code=404, detail="session not found")
    requested = body.get("fields") or []
    if not isinstance(requested, list) or not all(isinstance(item, str) for item in requested):
        raise HTTPException(status_code=400, detail="fields must be a string list")
    allowed = set(extension_store.session_field_read_allowlist(extension_id))
    fields = [field for field in requested if field in allowed]
    session = await _session_lite(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    return {"success": True, "fields": {field: session.get(field) for field in fields}}


def _session_activity_snapshot(session_id: str, session: dict) -> dict:
    queued_prompts = session.get("queued_prompts") if isinstance(session, dict) else []
    queued_count = len(queued_prompts) if isinstance(queued_prompts, list) else 0
    if coordinator.has_queued_prompts(session_id):
        queued_count = max(queued_count, coordinator.get_queued_count(session_id), 1)
    is_running = coordinator.turn_manager.is_running_cached(session_id)
    monitoring_state = coordinator.turn_manager.monitoring_state_cached(session_id)
    return {
        "session_id": session_id,
        "is_running": bool(is_running),
        "monitoring_state": monitoring_state,
        "queued_prompts_count": queued_count,
        "idle": not is_running and queued_count == 0,
    }


@app.post("/api/internal/session-activity")
async def internal_session_activity(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    _require_extension_permission(x_internal_token, "session_state")
    session_id = str(body.get("session_id") or "").strip()
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id is required")
    session = await _session_lite(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    return {"success": True, **await asyncio.to_thread(_session_activity_snapshot, session_id, session)}


@app.post("/api/internal/mssg")
async def internal_mssg(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    if not coordinator.is_internal_caller(x_internal_token):
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    sender_session_id = str(body.get("sender_session_id") or "").strip()
    message = str(body.get("message") or "").strip()
    if not sender_session_id or not message:
        raise HTTPException(
            status_code=400,
            detail="sender_session_id, one target, and message are required",
        )
    try:
        requested_provider_id = await _resolve_provider_id_ref(
            str(body.get("provider_id") or "").strip(),
        )
        requested_model = str(body.get("model") or "").strip()
        await _validate_optional_run_selector(
            sender_session_id,
            requested_provider_id,
            requested_model,
        )
        target_session_id = await _resolve_communication_target(body)
        return await coordinator.submit_team_message(
            sender_session_id=sender_session_id,
            target_session_id=target_session_id,
            message=message,
            detach=True,
            provider_id=requested_provider_id,
            model=requested_model,
            reasoning_effort=str(body.get("reasoning_effort") or "").strip(),
            collapse_key=str(body.get("collapse_key") or "").strip(),
            collapse_policy=str(body.get("collapse_policy") or "").strip(),
            target_selector=_communication_target_selector(body),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


async def _ask_continue_and_expect_mssg_back_async(
    body: dict,
    sender_session_id: str,
    message: str,
    requested_provider_id: str,
    requested_model: str,
) -> dict[str, Any]:
    target_worker_pool = str(body.get("target_worker_pool") or "").strip()
    pool_affinity_key = _api_optional_pool_affinity_key(body.get("pool_affinity_key"))
    has_exact_target = (
        str(body.get("target_session_id") or "").strip()
        or str(body.get("target_worker_id") or "").strip()
    )
    if target_worker_pool and not has_exact_target:
        target = await asyncio.to_thread(
            _pick_pool_worker_for_sender,
            target_worker_pool,
            sender_session_id,
            pool_affinity_key,
            True,
        )
        if not target:
            queued = await _enqueue_worker_pool_message(
                tag=target_worker_pool,
                sender_session_id=sender_session_id,
                prompt=message,
                expect_mssg_response=True,
                pool_affinity_key=pool_affinity_key,
                provider_id=requested_provider_id,
                model=requested_model,
                reasoning_effort=str(body.get("reasoning_effort") or "").strip(),
            )
            return {"success": True, "queued": True, **queued}
        target_session_id = str(target.get("agent_session_id") or "")
    else:
        target_session_id = await _resolve_communication_target(body)
    return await coordinator.submit_team_message(
        sender_session_id=sender_session_id,
        target_session_id=target_session_id,
        message=message,
        detach=True,
        expect_mssg_response=True,
        provider_id=requested_provider_id,
        model=requested_model,
        reasoning_effort=str(body.get("reasoning_effort") or "").strip(),
        target_selector=_communication_target_selector(body),
    )


async def _ask_wait_and_grab_last_assistant_mssg_in_turn(
    body: dict,
    sender_session_id: str,
    message: str,
    requested_provider_id: str,
    requested_model: str,
) -> dict[str, Any]:
    target_worker_pool = str(body.get("target_worker_pool") or "").strip()
    pool_affinity_key = _api_optional_pool_affinity_key(body.get("pool_affinity_key"))
    has_exact_target = (
        str(body.get("target_session_id") or "").strip()
        or str(body.get("target_worker_id") or "").strip()
    )
    if target_worker_pool and not has_exact_target:
        ask_id = str(body.get("ask_id") or "")
        if ask_id:
            status = await _pool_ask_status(ask_id)
            if isinstance(status, dict):
                if isinstance(status.get("result"), dict):
                    return status["result"]
                if status.get("pool_queue_item_id"):
                    _ensure_worker_pool_processor(str(status.get("pool_tag") or target_worker_pool))
                    return await _wait_for_pool_ask_result(ask_id, status)
        target = await asyncio.to_thread(
            _pick_pool_worker_for_sender,
            target_worker_pool,
            sender_session_id,
            pool_affinity_key,
            True,
        )
        if not target:
            ask_id = ask_id or f"ask_{uuid.uuid4().hex[:10]}"
            queued = await _enqueue_worker_pool_message(
                tag=target_worker_pool,
                sender_session_id=sender_session_id,
                prompt=message,
                expect_mssg_response=True,
                pool_affinity_key=pool_affinity_key,
                provider_id=requested_provider_id,
                model=requested_model,
                reasoning_effort=str(body.get("reasoning_effort") or "").strip(),
                wait_for_ask_response=True,
                ask_id=ask_id,
            )
            import ask_status_store

            await ask_status_store.write_status_async(
                ask_id,
                pool_queue_item_id=str(((queued or {}).get("item") or {}).get("id") or ""),
                pool_tag=target_worker_pool,
                sender_session_id=sender_session_id,
            )
            return await _wait_for_pool_ask_result(ask_id, queued)
        target_session_id = str(target.get("agent_session_id") or "")
    else:
        target_session_id = await _resolve_communication_target(body)
    return await coordinator.ask_team_message(
        sender_session_id=sender_session_id,
        target_session_id=target_session_id,
        message=message,
        ask_id=str(body.get("ask_id") or ""),
        provider_id=requested_provider_id,
        model=requested_model,
        reasoning_effort=str(body.get("reasoning_effort") or "").strip(),
        target_selector=_communication_target_selector(body),
    )


async def _handle_internal_ask(body: dict) -> dict[str, Any]:
    sender_session_id = str(body.get("sender_session_id") or "").strip()
    message = str(body.get("message") or "").strip()
    if not sender_session_id or not message:
        raise HTTPException(
            status_code=400,
            detail="sender_session_id, one target, and message are required",
        )
    try:
        requested_provider_id = await _resolve_provider_id_ref(
            str(body.get("provider_id") or "").strip(),
        )
        requested_model = str(body.get("model") or "").strip()
        await _validate_optional_run_selector(
            sender_session_id,
            requested_provider_id,
            requested_model,
        )
        mode = normalize_ask_mode(body.get("mode"))
        if mode == ASK_MODE_CONTINUE_AND_EXPECT_MSSG_BACK_ASYNC:
            return await _ask_continue_and_expect_mssg_back_async(
                body,
                sender_session_id,
                message,
                requested_provider_id,
                requested_model,
            )
        return await _ask_wait_and_grab_last_assistant_mssg_in_turn(
            body,
            sender_session_id,
            message,
            requested_provider_id,
            requested_model,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


async def _resolve_communication_target(body: dict) -> str:
    sender_session_id = str((body or {}).get("sender_session_id") or "").strip()
    target_session_id = str((body or {}).get("target_session_id") or "").strip()
    target_worker_id = str((body or {}).get("target_worker_id") or "").strip()
    target_worker_pool = str((body or {}).get("target_worker_pool") or "").strip()
    pool_affinity_key = _api_optional_pool_affinity_key((body or {}).get("pool_affinity_key"))
    targets = [value for value in (target_session_id, target_worker_id, target_worker_pool) if value]
    if len(targets) != 1:
        raise HTTPException(status_code=400, detail="exactly one target is required")
    if target_session_id:
        return target_session_id
    if target_worker_id:
        worker = await asyncio.to_thread(_find_worker_by_agent_session_id, target_worker_id)
        if not worker:
            raise HTTPException(status_code=404, detail="target_worker_id does not exist")
        return str(worker.get("agent_session_id") or "")
    target = await asyncio.to_thread(
        _pick_pool_worker_for_sender,
        target_worker_pool,
        sender_session_id,
        pool_affinity_key,
        False,
    )
    if not target:
        raise HTTPException(status_code=409, detail="no idle worker in target_worker_pool")
    return str(target.get("agent_session_id") or "")


def _communication_target_selector(body: dict) -> dict:
    target_session_id = str((body or {}).get("target_session_id") or "").strip()
    target_worker_id = str((body or {}).get("target_worker_id") or "").strip()
    target_worker_pool = str((body or {}).get("target_worker_pool") or "").strip()
    pool_affinity_key = _api_optional_pool_affinity_key((body or {}).get("pool_affinity_key"))
    if target_session_id:
        return {"kind": "session", "value": target_session_id}
    if target_worker_id:
        return {"kind": "worker", "value": target_worker_id}
    if target_worker_pool:
        selector = {"kind": "pool", "value": target_worker_pool}
        if pool_affinity_key:
            selector["pool_affinity_key"] = pool_affinity_key
        return selector
    return {}


@app.post("/api/internal/ask")
async def internal_ask(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    if not coordinator.is_internal_caller(x_internal_token):
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    try:
        return await _handle_internal_ask(body)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="ask timed out")


@app.post("/api/internal/test/force-context-overflow")
async def internal_force_context_overflow(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    if not coordinator.is_internal_caller(x_internal_token):
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    if body.get("confirm") != "FORCE_CONTEXT_OVERFLOW_FOR_TESTING":
        raise HTTPException(status_code=400, detail="invalid confirmation")

    app_session_id = str(body.get("app_session_id") or "").strip()
    if not app_session_id:
        raise HTTPException(status_code=400, detail="app_session_id is required")
    session = await _session_lite(app_session_id)
    if not session:
        raise HTTPException(status_code=404, detail=t("error.session_not_found"))

    coordinator.turn_manager.force_context_overflow_once(app_session_id)

    prompt = str(body.get("prompt") or "").strip()
    if not prompt:
        return {"success": True, "armed": True, "submitted": False}

    await asyncio.to_thread(config_store.apply_env_vars)
    item_id = await coordinator.submit_prompt_async(app_session_id, {
        "prompt": prompt,
        "app_session_id": app_session_id,
        "model": session.get("model"),
        "cwd": session.get("cwd"),
        "ws_callback": None,
        "images": None,
        "files": None,
        "orchestration_mode": session.get("orchestration_mode"),
        "client_id": body.get("client_id"),
        "source": "internal_test",
        "user_initiated": True,
    })
    return {
        "success": True,
        "armed": True,
        "submitted": True,
        "queued_id": item_id,
    }


@app.post("/api/internal/credential/request")
async def internal_credential_request(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    _require_builtin_runtime_extension(extension_store.extension_id_for_role('credential-broker'))
    """Invoked by a runner's `credential_request` SDK MCP tool. The provider
    (via the model) proposes an operation that needs a user secret. We
    validate + pin-check it and persist a PENDING consent for the user to
    approve; we NEVER receive or return the secret here. Returns the public
    consent view (consent_id + computed sink + risk) or an error.
    """
    if not coordinator.is_internal_caller(x_internal_token):
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    from credential_broker import broker as _broker
    import config_store as _cfg

    app_session_id = (body.get("app_session_id") or "").strip()
    descriptor = body.get("descriptor")
    if not app_session_id or not isinstance(descriptor, dict):
        return {"ok": False, "error": "app_session_id and descriptor are required"}
    provider_id = str(descriptor.get("provider_id") or "")
    allowed_sinks = _cfg.get_allowed_sinks(provider_id)
    try:
        view = _broker.request_consent(
            app_session_id=app_session_id,
            descriptor_raw=descriptor,
            allowed_sinks=allowed_sinks,
        )
    except _broker.BrokerError as e:
        return {"ok": False, "error": str(e)}
    await coordinator.broadcast_credential_consent_changed(app_session_id)
    return {
        "ok": True,
        "consent_id": view["consent_id"],
        "sink": view["sink"],
        "status": view["status"],
        "message": (
            "A credential consent request was created and is awaiting user "
            "approval. Once approved, call credential_execute with this "
            "consent_id. You will never see the secret value."
        ),
    }


@app.post("/api/internal/credential/execute")
async def internal_credential_execute(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    _require_builtin_runtime_extension(extension_store.extension_id_for_role('credential-broker'))
    """Invoked by a runner's `credential_execute` SDK MCP tool. Runs the
    frozen operation for an approved consent. The caller supplies ONLY the
    consent_id (+ optional presence proof) — never a descriptor or secret.
    Returns a guarded result with no secret in it."""
    if not coordinator.is_internal_caller(x_internal_token):
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    from credential_broker import broker as _broker

    consent_id = (body.get("consent_id") or "").strip()
    if not consent_id:
        return {"ok": False, "error": "consent_id is required"}
    try:
        result = _broker.execute(consent_id, proof=body.get("proof"))
    except _broker.BrokerError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "result": result}


@app.post("/api/internal/open-file-panel")
async def internal_open_file_panel(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    """Invoked by the active session's `open_file_panel` SDK MCP tool.

    mode="panel": mutate the session's backend-owned `open_file_panels`
    (fires `session_metadata_updated` → every connected tab opens the
    tab). mode="inline": NO state mutation — the persisted tool-call
    event on the assistant message is the source of truth; the frontend
    renders an embedded viewer from it. Either way return success so
    the agent gets a clean tool_result.
    """
    if not coordinator.is_internal_caller(x_internal_token):
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))

    app_session_id = body.get("app_session_id") or ""
    sess = await _session_lite(app_session_id)
    if sess is None:
        return {"success": False, "error": t("error.session_not_found_retry")}

    mode = body.get("mode")
    if mode not in ("panel", "inline"):
        return {"success": False, "error": "mode must be 'panel' or 'inline'"}

    raw_path = str(body.get("path") or "").strip()
    if not raw_path:
        return {"success": False, "error": t("error.file_panel_path_required")}
    # Resolve relative paths against the session cwd so the persisted
    # panel path is absolute + consistent with how the frontend's
    # manual-open path resolves (App.tsx handleFileClick).
    if not raw_path.startswith("/"):
        cwd = (sess.get("cwd") or "").rstrip("/")
        raw_path = f"{cwd}/{raw_path}" if cwd else raw_path

    def _range(s, e) -> Optional[dict]:
        if not isinstance(s, (int, float)) or not isinstance(e, (int, float)):
            return None
        return {"startLine": int(s), "endLine": int(e)}

    panel = {
        "id": uuid.uuid4().hex[:12],
        "path": raw_path,
        "focus": _range(body.get("start_line"), body.get("end_line")),
        "selection": _range(body.get("selected_start"), body.get("selected_end")),
    }

    if mode == "panel":
        await asyncio.to_thread(
            session_manager.add_open_file_panel,
            app_session_id,
            panel,
        )

    return {"success": True, "mode": mode, "panel": panel}


@app.post("/api/internal/file-editor/start-discussion")
async def internal_start_file_discussion(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    if not coordinator.is_internal_caller(x_internal_token):
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    app_session_id = str(body.get("app_session_id") or "").strip()
    if not file_editor.is_file_editor_session(app_session_id):
        return {"success": False, "error": t("error.not_file_editor_session")}
    try:
        discussion = file_editor.start_discussion(
            app_session_id,
            file_path=str(body.get("file_path") or "").strip(),
            line=int(body.get("line")),
            title=str(body.get("title") or ""),
            opened_by="agent",
        )
    except (TypeError, ValueError) as e:
        return {"success": False, "error": str(e)}
    return {"success": True, "discussion": discussion}


def _validate_user_input_questions(raw_questions: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_questions, list) or not 1 <= len(raw_questions) <= 3:
        raise HTTPException(status_code=400, detail="questions must contain 1-3 items")
    questions: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for raw in raw_questions:
        if not isinstance(raw, dict):
            raise HTTPException(status_code=400, detail="question entries must be objects")
        qid = str(raw.get("id") or "").strip()
        header = str(raw.get("header") or "").strip()
        question = str(raw.get("question") or "").strip()
        if not qid or qid in seen_ids:
            raise HTTPException(status_code=400, detail="question ids must be unique and non-empty")
        if not header or not question:
            raise HTTPException(status_code=400, detail="question header and question are required")
        options_raw = raw.get("options") or []
        if not isinstance(options_raw, list) or len(options_raw) > 3:
            raise HTTPException(status_code=400, detail="question options must contain at most 3 items")
        options: list[dict[str, str]] = []
        for option_raw in options_raw:
            if not isinstance(option_raw, dict):
                raise HTTPException(status_code=400, detail="question options must be objects")
            label = str(option_raw.get("label") or "").strip()
            description = str(option_raw.get("description") or "").strip()
            if not label:
                raise HTTPException(status_code=400, detail="option label is required")
            options.append({"label": label[:120], "description": description[:500]})
        seen_ids.add(qid)
        questions.append({
            "id": qid[:80],
            "header": header[:120],
            "question": question[:1000],
            "options": options,
        })
    return questions


async def _broadcast_user_input(event_type: str, payload: dict[str, Any]) -> None:
    app_session_id = str(payload.get("app_session_id") or "").strip()
    if not app_session_id:
        return
    await coordinator.dispatch_raw(app_session_id, {"type": event_type, "data": payload})


async def _broadcast_user_input_state(app_session_id: str) -> None:
    sid = str(app_session_id or "").strip()
    if not sid:
        return
    pending_count = await asyncio.to_thread(
        user_input_store.pending_count_for_session,
        sid,
    )
    await coordinator.broadcast_global("session_user_input_changed", {
        "session_id": sid,
        "app_session_id": sid,
        "pending_user_input_count": pending_count,
    })


@app.get("/api/user-input/pending")
async def get_pending_user_inputs(app_session_id: str):
    sid = str(app_session_id or "").strip()
    if not sid:
        raise HTTPException(status_code=400, detail="app_session_id is required")
    if await _session_lite(sid) is None:
        raise HTTPException(status_code=404, detail=t("error.session_not_found_retry"))
    return {
        "requests": await asyncio.to_thread(
            user_input_store.pending_for_session,
            sid,
        )
    }


@app.post("/api/user-input/{request_id}/resolve")
async def resolve_user_input(request_id: str, body: dict):
    req = await asyncio.to_thread(user_input_store.get_request, request_id)
    if req is None:
        raise HTTPException(status_code=404, detail="request not found")
    if str(body.get("app_session_id") or "").strip() != req.get("app_session_id"):
        raise HTTPException(status_code=403, detail="session mismatch")
    if req.get("status") != "pending":
        return {"success": False, "status": req.get("status")}
    if not isinstance(body, dict) or not isinstance(body.get("answers"), dict):
        raise HTTPException(status_code=400, detail="answers object is required")
    expected = {q["id"] for q in req.get("questions") or []}
    answers: dict[str, str] = {}
    for qid in expected:
        value = str(body["answers"].get(qid) or "").strip()
        if not value:
            raise HTTPException(status_code=400, detail=f"answer is required for {qid}")
        answers[qid] = value[:2000]
    resolved = await asyncio.to_thread(
        user_input_store.resolve_request,
        request_id,
        answers,
    )
    if resolved is None:
        raise HTTPException(status_code=404, detail="request not found")
    await _broadcast_user_input("user_input_resolved", {
        "request_id": request_id,
        "app_session_id": resolved.get("app_session_id"),
        "status": resolved.get("status"),
    })
    await _broadcast_user_input_state(str(resolved.get("app_session_id") or ""))
    return {"success": True, "status": resolved.get("status")}


@app.post("/api/user-input/{request_id}/cancel")
async def cancel_user_input(request_id: str, body: dict):
    req = await asyncio.to_thread(user_input_store.get_request, request_id)
    if req is None:
        raise HTTPException(status_code=404, detail="request not found")
    if str(body.get("app_session_id") or "").strip() != req.get("app_session_id"):
        raise HTTPException(status_code=403, detail="session mismatch")
    resolved = await asyncio.to_thread(user_input_store.cancel_request, request_id)
    if resolved is None:
        raise HTTPException(status_code=404, detail="request not found")
    await _broadcast_user_input("user_input_resolved", {
        "request_id": request_id,
        "app_session_id": resolved.get("app_session_id"),
        "status": resolved.get("status"),
    })
    await _broadcast_user_input_state(str(resolved.get("app_session_id") or ""))
    return {"success": True, "status": resolved.get("status")}


@app.post("/api/internal/user-input/request")
async def internal_request_user_input(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    if not coordinator.is_internal_caller(x_internal_token):
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    app_session_id = str(body.get("app_session_id") or "").strip()
    if not app_session_id:
        return {"success": False, "error": "app_session_id is required"}
    if await _session_lite(app_session_id) is None:
        return {"success": False, "error": t("error.session_not_found_retry")}
    try:
        questions = _validate_user_input_questions(body.get("questions"))
    except HTTPException as exc:
        return {"success": False, "error": str(exc.detail)}
    raw_timeout = body.get("timeout_seconds")
    timeout_seconds = 86400.0
    if raw_timeout is not None:
        try:
            timeout_seconds = float(raw_timeout)
        except (TypeError, ValueError):
            return {"success": False, "error": "timeout_seconds must be a number"}
        if timeout_seconds <= 0 or timeout_seconds > 86400:
            return {"success": False, "error": "timeout_seconds must be between 1 and 86400"}
    public_req = await asyncio.to_thread(
        user_input_store.create_request,
        app_session_id=app_session_id,
        questions=questions,
        timeout_seconds=timeout_seconds,
    )
    await _broadcast_user_input("user_input_requested", public_req)
    await _broadcast_user_input_state(app_session_id)
    completed = await user_input_store.wait_for_completion(
        public_req["request_id"],
        timeout_seconds,
    )
    if completed is None:
        return {"success": False, "error": "request not found"}
    if completed.get("status") == "resolved":
        await _broadcast_user_input_state(str(completed.get("app_session_id") or ""))
        return {
            "success": True,
            "request_id": completed["request_id"],
            "answers": completed.get("answers") or {},
        }
    await _broadcast_user_input("user_input_resolved", {
        "request_id": completed.get("request_id"),
        "app_session_id": completed.get("app_session_id"),
        "status": completed.get("status"),
    })
    await _broadcast_user_input_state(str(completed.get("app_session_id") or ""))
    return {
        "success": False,
        "request_id": completed.get("request_id"),
        "status": completed.get("status"),
    }


@app.post("/api/internal/open-config-panel")
async def internal_open_config_panel(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    """Invoked by the active session's `open_config_panel` SDK MCP tool.

    Always INLINE: NO session state mutation. The persisted tool-call
    event on the assistant message is the source of truth — the frontend
    renders an embedded provider-config-sync capability editor from it.
    Popping it into the right side panel is a later user action via the
    inline widget's button (→ /api/sessions/.../config-panels). Returns
    success + the resolved panel so the agent gets a clean tool_result.
    """
    if not coordinator.is_internal_caller(x_internal_token):
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))

    app_session_id = body.get("app_session_id") or ""
    sess = await _session_lite(app_session_id)
    if sess is None:
        return {"success": False, "error": t("error.session_not_found_retry")}

    capability_id = str(body.get("capability_id") or "").strip()
    if not capability_id:
        return {"success": False, "error": "capability_id is required"}

    scope = str(body.get("scope") or "project").strip()
    if scope not in ("global", "project"):
        scope = "project"

    # Resolve project cwd against the session cwd when the agent didn't
    # pass one explicitly, so the persisted panel targets the right project.
    cwd = str(body.get("cwd") or "").strip()
    if scope == "project" and not cwd:
        cwd = (sess.get("cwd") or "").strip()

    panel = {
        "id": uuid.uuid4().hex[:12],
        "capability_id": capability_id,
        "scope": scope,
        "cwd": cwd,
    }
    return {"success": True, "panel": panel}


@app.post("/api/internal/schedules")
async def internal_schedules(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    """Invoked by the runner's `scheduler` SDK MCP tools. Backend owns
    the durable schedule store; the runner only publishes the request.
    All validation is server-side (schedule_store.create raises
    ValueError with a tool-surfaceable message)."""
    if not coordinator.is_internal_caller(x_internal_token):
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))

    app_session_id = body.get("app_session_id") or ""
    if not await _session_exists(app_session_id):
        return {"success": False, "error": t("error.session_not_found_retry")}

    action = body.get("action")
    if action == "create":
        delay = body.get("delay_seconds")
        fire_at = body.get("fire_at")
        if fire_at is None and delay is not None:
            import math as _math
            if (not isinstance(delay, (int, float)) or isinstance(delay, bool)
                    or not _math.isfinite(delay) or delay < 0
                    or delay > 366 * 24 * 3600):
                return {"success": False,
                        "error": "delay_seconds must be a finite number of "
                                 "seconds between 0 and one year"}
            fire_at = (datetime.now() + timedelta(seconds=float(delay))).isoformat()
        from stores import task_store
        source_task_id = None
        owner = await asyncio.to_thread(task_store.find_pending_run_for_session, app_session_id)
        if owner is not None:
            source_task_id = owner[0]
        try:
            rec = await asyncio.to_thread(
                schedule_store.create,
                app_session_id=app_session_id,
                prompt=body.get("prompt"),
                kind=body.get("kind"),
                fire_at=fire_at,
                interval_seconds=body.get("interval_seconds"),
                source_task_id=source_task_id,
            )
        except ValueError as e:
            return {"success": False, "error": str(e)}
        await broadcast_schedules(coordinator, app_session_id)
        return {"success": True, "schedule": rec}

    if action == "list":
        return {
            "success": True,
            "schedules": await asyncio.to_thread(
                schedule_store.list_for_session,
                app_session_id,
            ),
        }

    if action == "delete":
        schedule_id = str(body.get("schedule_id") or "")
        existing = await asyncio.to_thread(schedule_store.get, schedule_id)
        if existing is None or existing.get("app_session_id") != app_session_id:
            return {"success": False, "error": "unknown schedule_id"}
        await asyncio.to_thread(schedule_store.delete, schedule_id)
        await broadcast_schedules(coordinator, app_session_id)
        return {"success": True}

    return {"success": False, "error": "action must be create|list|delete"}


@app.get("/api/schedules")
async def get_all_schedules():
    """User-facing snapshot of every schedule across all sessions,
    enriched with the owning session's name and existence. Deliberately
    NOT exposed on the model-facing internal endpoint — a session's
    model must not read other sessions' schedule prompts. Push side:
    the global `schedules_changed` WS ping (clients refetch here)."""
    schedules = await asyncio.to_thread(schedule_store.list_all)
    summaries = await asyncio.to_thread(session_manager.list)
    names = {s.get("id"): s.get("name") for s in summaries}
    out = []
    for rec in schedules:
        sid = rec.get("app_session_id")
        entry = dict(rec)
        if sid in names:
            entry["session_name"] = names[sid]
            entry["session_exists"] = True
        else:
            # Non-root sid (fork) or deleted session — resolve the rare
            # case individually instead of loading every root.
            sess = await asyncio.to_thread(session_manager.get, sid)
            entry["session_name"] = (sess or {}).get("name")
            entry["session_exists"] = sess is not None
        out.append(entry)
    return {"schedules": out}


@app.delete("/api/schedules/{schedule_id}")
async def delete_schedule_by_id(schedule_id: str):
    """User-facing cancel by id. No session-exists gate: schedules
    whose session was deleted (orphans) must stay cancelable."""
    removed = await asyncio.to_thread(schedule_store.delete, schedule_id)
    if removed is None:
        raise HTTPException(status_code=404, detail="unknown schedule_id")
    await broadcast_schedules(coordinator, removed.get("app_session_id") or "")
    return {"success": True}


def _require_tasks_internal(x_internal_token: str) -> None:
    """Gate for the tasks substrate. Tasks are surfaced by the (private)
    routines extension; in a pure-public checkout the extension is absent and
    this fails closed."""
    if not coordinator.is_internal_caller(x_internal_token):
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    _require_builtin_runtime_extension(extension_store.extension_id_for_role('routines'))


@app.post("/api/internal/tasks")
async def internal_tasks(
    body: dict = Body(default={}),
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    """Backend-owned task definitions + on-demand launch.

    Tasks are reusable, run-when-clicked definitions that spin up an
    autonomous session (via `task_runner.launch_task`). Core owns the
    durable store + launch; the routines extension's routes/MCP only forward
    here. All validation is server-side (`task_store` raises ValueError
    with a surfaceable message). Actions: list | get | create | update |
    delete | run | stop.
    """
    _require_tasks_internal(x_internal_token)
    from stores import task_store
    from stores import task_trigger_store
    import task_runner

    action = (body or {}).get("action")

    if action == "list":
        cwd = str((body or {}).get("cwd") or "").strip()
        node_id = str((body or {}).get("node_id") or "primary").strip() or "primary"
        if not cwd:
            return {"success": False, "error": "cwd is required"}
        return {
            "success": True,
            "tasks": await asyncio.to_thread(task_store.list_for_project, cwd, node_id),
        }

    if action == "get":
        task_id = str((body or {}).get("task_id") or "").strip()
        rec = await asyncio.to_thread(task_store.get, task_id)
        if rec is None:
            return {"success": False, "error": "unknown task_id"}
        return {"success": True, "task": rec}

    if action == "create":
        node_id = _resolve_session_node_id(body or {})
        try:
            rec = await asyncio.to_thread(
                task_store.create,
                cwd=str((body or {}).get("cwd") or "").strip(),
                name=(body or {}).get("name"),
                prompt=(body or {}).get("prompt"),
                node_id=node_id,
                description=(body or {}).get("description") or "",
                orchestration_mode=(body or {}).get("orchestration_mode") or "native",
                worker_creation_policy=(body or {}).get("worker_creation_policy") or "approve",
                session_type=(body or {}).get("session_type") or "normal",
                model=(body or {}).get("model"),
                provider_id=(body or {}).get("provider_id"),
                reasoning_effort=(body or {}).get("reasoning_effort"),
                permission=(body or {}).get("permission"),
                capability_contexts=(body or {}).get("capability_contexts"),
                singleton=bool((body or {}).get("singleton", False)),
                goal=(body or {}).get("goal") or "",
                trigger=(body or {}).get("trigger"),
                scripts=(body or {}).get("scripts"),
                assessment=(body or {}).get("assessment"),
            )
        except ValueError as e:
            return {"success": False, "error": str(e)}
        await asyncio.to_thread(task_trigger_store.register_for_task, rec)
        await task_runner.broadcast_tasks_changed(coordinator, rec["cwd"], rec["node_id"])
        return {"success": True, "task": rec}

    if action == "update":
        task_id = str((body or {}).get("task_id") or "").strip()
        patch = (body or {}).get("patch")
        if not isinstance(patch, dict):
            return {"success": False, "error": "patch must be an object"}
        try:
            rec = await asyncio.to_thread(task_store.update, task_id, patch)
        except ValueError as e:
            return {"success": False, "error": str(e)}
        if rec is None:
            return {"success": False, "error": "unknown task_id"}
        await asyncio.to_thread(task_trigger_store.register_for_task, rec)
        await task_runner.broadcast_tasks_changed(coordinator, rec["cwd"], rec["node_id"])
        return {"success": True, "task": rec}

    if action == "delete":
        task_id = str((body or {}).get("task_id") or "").strip()
        removed = await asyncio.to_thread(task_store.delete, task_id)
        if removed is None:
            return {"success": False, "error": "unknown task_id"}
        from stores import task_output_store
        await asyncio.to_thread(task_output_store.delete_for_task, task_id)
        await asyncio.to_thread(task_trigger_store.unregister_task, task_id)
        await task_runner.broadcast_tasks_changed(
            coordinator, removed.get("cwd") or "", removed.get("node_id") or "primary",
        )
        return {"success": True}

    if action == "run":
        task_id = str((body or {}).get("task_id") or "").strip()
        try:
            result = await task_runner.launch_task(
                task_id,
                coordinator=coordinator,
                prompt_override=(body or {}).get("prompt"),
                client_id=(body or {}).get("client_id"),
            )
        except task_runner.TaskLaunchError as e:
            return {"success": False, "error": str(e)}
        return {"success": True, **result}

    if action == "stop":
        task_id = str((body or {}).get("task_id") or "").strip()
        try:
            result = await task_runner.stop_task(task_id, coordinator=coordinator)
        except task_runner.TaskLaunchError as e:
            return {"success": False, "error": str(e)}
        return {"success": True, **result}

    return {"success": False, "error": "action must be list|get|create|update|delete|run|stop"}


@app.post("/api/internal/task-outputs")
async def internal_task_outputs(
    body: dict = Body(default={}),
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    _require_tasks_internal(x_internal_token)
    from stores import task_output_store, task_store
    import task_runner

    action = str((body or {}).get("action") or "").strip()
    task_id = str((body or {}).get("task_id") or "").strip()
    if not task_id:
        return {"success": False, "error": "task_id is required"}
    task = await asyncio.to_thread(task_store.get, task_id)
    if task is None:
        return {"success": False, "error": "unknown task_id"}

    if action == "list":
        outputs = await asyncio.to_thread(
            task_output_store.list_for_task,
            task_id,
            limit=int((body or {}).get("limit") or 50),
        )
        return {"success": True, "outputs": outputs, "latest": outputs[0] if outputs else None}

    if action == "publish":
        try:
            output = await asyncio.to_thread(
                task_output_store.publish,
                task_id=task_id,
                task_cwd=str(task.get("cwd") or ""),
                title=(body or {}).get("title"),
                kind=(body or {}).get("kind") or "artifact",
                content_type=(body or {}).get("content_type") or "text/html",
                content=(body or {}).get("content") or "",
                file_path=(body or {}).get("file_path") or "",
                session_id=(body or {}).get("session_id") or (body or {}).get("app_session_id") or "",
            )
        except ValueError as e:
            return {"success": False, "error": str(e)}
        await task_runner.broadcast_tasks_changed(
            coordinator,
            task.get("cwd") or "",
            task.get("node_id") or "primary",
        )
        return {"success": True, "output": output}

    return {"success": False, "error": "action must be list|publish"}


@app.get("/api/internal/task-outputs/{task_id}/{output_id}/content")
async def internal_task_output_content(
    task_id: str,
    output_id: str,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    _require_tasks_internal(x_internal_token)
    from fastapi.responses import FileResponse
    from stores import task_output_store

    try:
        path, content_type = await asyncio.to_thread(
            task_output_store.content_path,
            task_id,
            output_id,
        )
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="unknown output")
    return FileResponse(
        path,
        media_type=content_type,
        headers={
            "Content-Security-Policy": (
                "sandbox allow-popups allow-popups-to-escape-sandbox; "
                "default-src 'none'; img-src data: blob: https:; "
                "style-src 'unsafe-inline'; font-src data:; base-uri 'none'"
            ),
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.post("/api/internal/ask-propose")
async def internal_ask_propose(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    _require_builtin_runtime_extension(extension_store.extension_id_for_role('team-orchestration'))
    """Invoked by the session-bridge `propose_sessions` MCP tool. Stamps the
    inline session picker on the CALLING session's in-flight assistant
    message (validated ids; broadcasts `message_ask_result_changed`). The
    Ask UI's own picker is stamped directly server-side by
    `session_search.search()`, not via this endpoint."""
    if not coordinator.is_internal_caller(x_internal_token):
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    caller_sid = str(body.get("caller_sid") or "")
    if not caller_sid:
        raise HTTPException(status_code=400, detail="caller_sid is required")
    in_flight = coordinator.turn_manager.get_in_flight_assistant_msg(caller_sid)
    if not in_flight or not in_flight.get("id"):
        raise HTTPException(
            status_code=409,
            detail="no in-flight assistant message to attach the picker to",
        )
    result = await asyncio.to_thread(
        session_search.propose_sessions,
        body.get("session_ids") or [],
        str(body.get("reasoning") or ""),
        target_sid=caller_sid,
        msg_id=in_flight["id"],
        proposed_project_path=str(body.get("proposed_project_path") or ""),
    )
    return {"success": True, **result}


@app.post("/api/internal/session-bridge/search")
async def internal_session_bridge_search(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    _require_builtin_runtime_extension(extension_store.extension_id_for_role('team-orchestration'))
    """Invoked by the session-bridge `search_sessions` MCP tool. Runs the
    same provisioned search-worker ranking engine as the Ask UI and returns
    ranked sessions. No picker; the agent can call `propose_sessions`
    separately. Optional filters (`provider_id` / `model` /
    `reasoning_effort` / `node_id`) narrow the candidate set before the
    worker runs and post-validate its output."""
    if not coordinator.is_internal_caller(x_internal_token):
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    query = str(body.get("query") or "").strip()
    if not query:
        return {"results": [], "error": "empty_query"}
    try:
        limit = int(body.get("limit") or 5)
    except (TypeError, ValueError):
        limit = 5

    def _opt_str(key: str) -> str:
        val = body.get(key)
        return val.strip() if isinstance(val, str) else ""

    provider_id = await _resolve_auto_search_provider_id(
        body,
        str(body.get("app_session_id") or ""),
    )
    flow = await session_search.run_search_sessions_session(
        query,
        timeout=session_search._DEFAULT_TIMEOUT_SECONDS,
        max_results=max(1, min(limit, 10)),
        provider_id=provider_id or None,
        model=_opt_str("model") or None,
        reasoning_effort=_opt_str("reasoning_effort") or None,
        node_id=_opt_str("node_id") or None,
    )
    return await asyncio.to_thread(session_search.canonical_search_response, flow)


@app.post("/api/internal/coordination/lock-ops")
async def internal_coordination_lock_ops(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    _require_builtin_runtime_extension(extension_store.BUILTIN_COORDINATION_EXTENSION_ID)
    if not coordinator.is_internal_caller(x_internal_token):
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    raw_owner = body.get("owner") if isinstance(body.get("owner"), dict) else {}
    principal_extension_id = coordinator.principal_extension_id(x_internal_token) or "core"
    requested_op = str(body.get("op") or "").strip().lower().replace("-", "_")
    release = bool(body.get("release") or False)
    renew = bool(body.get("renew") or False)
    validate = bool(body.get("validate") or False)
    reattach = bool(body.get("reattach") or False)
    owned = bool(body.get("owned") or False)
    holder_token = str(body.get("holder_token") or "")
    if not requested_op:
        if release and owned:
            requested_op = "release_owned"
        elif release:
            requested_op = "release"
        elif renew:
            requested_op = "renew"
        elif validate:
            requested_op = "validate"
        elif reattach:
            requested_op = "reattach"
        elif owned:
            requested_op = "list_owned"
        else:
            requested_op = "acquire"
    owner_auth_required = requested_op in {"reattach", "list_owned", "release_owned"} or (
        requested_op == "renew" and not holder_token
    )
    if owner_auth_required and principal_extension_id != "core":
        raise HTTPException(status_code=403, detail="trusted runner identity required for owner-based lock operation")
    owner = {
        **raw_owner,
        "principal_extension_id": principal_extension_id,
        "source": str(raw_owner.get("source") or "internal_coordination_lock_ops"),
    }
    return await coordination.lock_ops(
        key=str(body.get("key") or ""),
        keys=body.get("keys") if isinstance(body.get("keys"), list) else None,
        op=str(body.get("op") or ""),
        release=release,
        renew=renew,
        validate=validate,
        reattach=reattach,
        owned=owned,
        holder_token=holder_token,
        timeout_seconds=body.get("timeout_seconds"),
        lease_seconds=body.get("lease_seconds"),
        owner=owner,
    )


def _require_marketplace_internal(x_internal_token: str) -> None:
    if not coordinator.is_internal_caller(x_internal_token):
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    _require_builtin_runtime_extension(extension_store.MARKETPLACE_EXTENSION_ID)
    if coordinator.principal_extension_id(x_internal_token) != extension_store.MARKETPLACE_EXTENSION_ID:
        raise HTTPException(status_code=403, detail="marketplace extension is required")


@app.post("/api/internal/marketplace")
async def internal_marketplace(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    _require_marketplace_internal(x_internal_token)
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")
    action = str(body.get("action") or "").strip()
    changed = False
    try:
        if action == "search":
            result = await asyncio.to_thread(
                extension_store.search_marketplace_catalog,
                query=str(body.get("query") or ""),
                limit=body.get("limit") or 20,
            )
        elif action == "install":
            extension_id = str(body.get("extension_id") or "").strip()
            metadata_url = extension_store.marketplace_metadata_url(extension_id)
            result = {
                "extension": await asyncio.to_thread(
                    extension_store.install_from_marketplace_metadata,
                    metadata_url=metadata_url,
                    entitlement_token=str(body.get("entitlement_token") or ""),
                )
            }
            changed = True
        elif action == "list_installed":
            result = {"extensions": await asyncio.to_thread(extension_store.list_extensions)}
        elif action == "get_installed":
            extension_id = str(body.get("extension_id") or "").strip()
            record = await asyncio.to_thread(extension_store.get_extension, extension_id)
            if not record or extension_id in extension_store.PUBLIC_EXTENSION_LIST_HIDDEN_IDS:
                raise HTTPException(status_code=404, detail="Extension not installed")
            result = {"extension": record}
        elif action == "set_enabled":
            extension_id = str(body.get("extension_id") or "").strip()
            enabled = body.get("enabled")
            if not isinstance(enabled, bool):
                raise extension_store.ExtensionError("enabled must be a boolean")
            result = {
                "extension": await asyncio.to_thread(
                    extension_store.set_enabled,
                    extension_id,
                    enabled,
                )
            }
            changed = True
        elif action == "uninstall":
            extension_id = str(body.get("extension_id") or "").strip()
            await asyncio.to_thread(extension_store.uninstall, extension_id)
            result = {"ok": True}
            changed = True
        elif action == "update":
            result = await asyncio.to_thread(extension_store.update_installed_extensions)
            changed = bool(result.get("updated"))
        else:
            raise extension_store.ExtensionError("unknown marketplace action")
    except extension_store.ExtensionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if changed:
        await coordinator.broadcast_global("extensions_changed", {})
    return result


@app.post("/api/internal/session-bridge/delegate")
async def internal_session_bridge_delegate(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    _require_builtin_runtime_extension(extension_store.extension_id_for_role('team-orchestration'))
    """Invoked by the session-bridge `delegate_to_session` MCP tool. Blocks
    on the picker approval (unless auto+fork) AND the delegated turn, then
    returns the target's final message."""
    if not coordinator.is_internal_caller(x_internal_token):
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    caller_sid = str(body.get("app_session_id") or "")
    requested_provider_id = await _resolve_provider_id_ref(
        str(body.get("provider_id") or "").strip(),
    )
    requested_model = str(body.get("model") or "").strip()
    await _validate_optional_run_selector(
        caller_sid,
        requested_provider_id,
        requested_model,
    )
    result = await session_bridge.delegate(
        caller_sid=caller_sid,
        target_sid=str(body.get("session_id") or ""),
        prompt=str(body.get("prompt") or ""),
        run_mode=str(body.get("run_mode") or ""),
        approval=str(body.get("approval") or ""),
        display_prompt=str(body.get("display_prompt") or "") or None,
        source=str(body.get("source") or "") or None,
        client_id=str(body.get("client_id") or "") or None,
        provider_id=requested_provider_id,
        model=requested_model,
        reasoning_effort=str(body.get("reasoning_effort") or "").strip(),
    )
    return result


_AGENT_BOARD_MAX_PROMPT_LEN = 8000
# Strong refs to in-flight delivery tasks so they aren't GC'd mid-await.
_AGENT_BOARD_DELIVERY_TASKS: set[asyncio.Task] = set()


@app.post("/api/internal/agent-board/run-prompt")
async def internal_agent_board_run_prompt(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    """Deliver a prompt lane action from the Agent Board extension. Only the
    agent-board builtin (identified by its minted per-extension token) may
    call this; the privileged cross-session turn stays inside core. Runs the
    turn in the background so the drop request returns immediately."""
    if not coordinator.is_internal_caller(x_internal_token):
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    # Runtime-readiness gate MUST precede the identity check: in a pure-public
    # agent-board role is absent,
    # and principal_extension_id() is also None for a core token — so a bare
    # `None != None` identity check would let any core-token holder through.
    # runtime_not_ready_message(None) returns "not installed" -> 404 first.
    _require_builtin_runtime_extension(extension_store.extension_id_for_role('agent-board'))
    if (
        coordinator.principal_extension_id(x_internal_token)
        != extension_store.extension_id_for_role('agent-board')
    ):
        raise HTTPException(status_code=403, detail="caller is not the agent-board extension")
    session_id = str((body or {}).get("session_id") or "").strip()
    prompt = str((body or {}).get("prompt") or "").strip()
    if not session_id or not prompt:
        raise HTTPException(status_code=400, detail="session_id and prompt are required")
    if len(prompt) > _AGENT_BOARD_MAX_PROMPT_LEN:
        raise HTTPException(status_code=400, detail="prompt too long")
    # Constrain the target to a real, existing session so a bug in the
    # extension subprocess cannot drive arbitrary/virtual session ids.
    if not await _session_exists(session_id):
        raise HTTPException(status_code=404, detail="unknown session")
    # Continue-mode delivery refuses a busy target; surface that synchronously
    # so the drop UI can tell the user instead of silently dropping the prompt.
    if coordinator.turn_manager.has_active_runs(session_id):
        raise HTTPException(status_code=409, detail="session has an in-flight turn")

    async def _deliver() -> None:
        try:
            result = await session_bridge.run_for_extension(session_id, prompt, source="agent-board")
        except Exception:
            logger.warning(
                "agent-board run-prompt failed for %s", session_id[:8], exc_info=True
            )
            return
        # run_for_extension returns an error dict (e.g. target became busy in
        # the race after the pre-check) rather than raising — don't drop it.
        if isinstance(result, dict) and result.get("error"):
            logger.warning(
                "agent-board run-prompt for %s returned error: %s",
                session_id[:8], result.get("error"),
            )

    # Hold a reference until completion: a bare create_task may be GC'd
    # mid-await, silently cancelling the delivery.
    task = asyncio.create_task(_deliver())
    _AGENT_BOARD_DELIVERY_TASKS.add(task)
    task.add_done_callback(_AGENT_BOARD_DELIVERY_TASKS.discard)
    return {"scheduled": True}


@app.post("/api/internal/provider-config-sync/broadcast")
async def internal_provider_config_sync_broadcast(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    _require_builtin_runtime_extension(extension_store.BUILTIN_PROVIDER_CONFIG_SYNC_EXTENSION_ID)
    """Change webhook hit by the out-of-process provider-config-sync MCP
    server (run as a stdio subprocess) after it writes a provider config
    file. Re-broadcasts the fact to open frontend clients so they refetch
    the active scope. The subprocess cannot share Better Agent's
    in-process broadcast callback, so it POSTs here instead."""
    if not coordinator.is_internal_caller(x_internal_token):
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    await coordinator.broadcast_global(
        "provider_config_sync_changed",
        {
            "scope": body.get("scope"),
            "category": body.get("category"),
            "capability_id": body.get("capability_id"),
            "path": body.get("path"),
            "cwd": body.get("cwd"),
        },
    )
    return {"ok": True}


def _resolve_session_bridge_delegation(body: dict, delegation_id: str = "") -> dict:
    _require_builtin_runtime_extension(extension_store.extension_id_for_role('team-orchestration'))
    body = body or {}
    delegation_id = delegation_id or str(body.get("delegation_id") or "").strip()
    if not delegation_id:
        return {"success": False, "status": 400, "error": "delegation_id is required"}
    chosen = body.get("chosen_session_id")
    chosen = str(chosen) if chosen else None
    ok = session_bridge.resolve_delegation(delegation_id, chosen)
    if not ok:
        return {
            "success": False,
            "status": 409,
            "error": "delegation not pending, already resolved, or invalid target",
        }
    return {"success": True}


@app.post("/api/internal/session-bridge/delegate/resolve")
async def internal_session_bridge_delegate_resolve(
    body: dict = Body(default={}),
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    if not coordinator.is_internal_caller(x_internal_token):
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    return _resolve_session_bridge_delegation(body)


@app.post("/api/sessions/{sid}/messages/{msg_id}/ask-choice")
async def set_ask_choice(sid: str, msg_id: str, body: dict = Body(default={})):
    """Record the user's pick from a turn's session picker. Stamps
    `chosen_session_id` on the producing assistant message so the chosen
    row stays highlighted across reloads / tabs / previous turns. Pass
    `chosen_session_id: null` to clear."""
    body = body or {}
    chosen = body.get("chosen_session_id")
    chosen = str(chosen) if chosen else None
    if sid == session_search.ASK_SINGLETON_ID:
        updated = await session_search.set_ask_choice_async(msg_id, chosen)
    else:
        updated = await asyncio.to_thread(
            session_manager.set_msg_ask_choice,
            sid,
            msg_id,
            chosen,
        )
    if updated is None:
        raise HTTPException(status_code=404, detail="session or message not found")
    return {"success": True, "chosen_session_id": chosen}


@app.get("/api/sessions/{session_id}/images/{filename}")
async def get_session_image(session_id: str, filename: str):
    img_path = resolve_session_image_path(session_id, filename)
    if not img_path.exists():
        raise HTTPException(status_code=404, detail=t("error.image_not_found"))
    return FileResponse(img_path)


def resolve_session_image_path(session_id: str, filename: str) -> Path:
    image_root = (ba_home() / "sessions" / "images").resolve()
    session_dir = (image_root / session_id).resolve()
    img_path = (session_dir / filename).resolve()
    try:
        img_path.relative_to(session_dir)
        session_dir.relative_to(image_root)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=t("error.image_not_found")) from exc
    return img_path


# ============================================================================
# Mobile app download — serve APK/IPA from ba_home()/mobile/
# ============================================================================
from pathlib import Path as _PathLib  # noqa: E402
import mimetypes as _mimetypes  # noqa: E402

_MOBILE_DIR = ba_home() / "mobile"


def _desktop_downloads_dir() -> _PathLib:
    return ba_home() / "desktop" / "downloads"


def _desktop_update_repo_dir() -> _PathLib:
    return ba_home() / "desktop" / "updates" / "repository"


def _mobile_version() -> dict:
    """Read the staged build's version side-channel (written by the APK
    rebuild hook alongside the APK). Returns {} if absent so callers can
    treat version-checking as optional."""
    import json
    vf = _MOBILE_DIR / "version.json"
    if not vf.exists():
        return {}
    try:
        with vf.open(encoding="utf-8") as f:
            data = json.load(f)
        code = data.get("version_code")
        return {
            "version_code": int(code) if isinstance(code, (int, float)) else None,
            "version_name": data.get("version_name"),
        }
    except (ValueError, OSError):
        return {}


def _desktop_version() -> dict:
    try:
        from _version import __version__ as version
    except ImportError:
        return {}
    return {"version": version}


def _lan_ip() -> str:
    """Best-effort primary LAN IPv4 of this machine, so the mobile QR
    encodes a phone-reachable address instead of localhost. Opens a UDP
    socket toward a public IP (no packets sent) and reads the local end
    the OS would route through. Falls back to 127.0.0.1."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def _local_server_url(request: Request) -> str:
    port = request.url.port or (443 if request.url.scheme == "https" else 80)
    lan_ip = _lan_ip()
    return f"{request.url.scheme}://{lan_ip}:{port}"


def _preferred_server_url_info(request: Request, *, allow_loopback_https: bool = False) -> dict:
    import tailscale_https

    preference = tailscale_https.preferred_external_url_details(
        _local_server_url(request),
        allow_loopback_https=allow_loopback_https,
    )
    return {
        "server_url": preference.url,
        "server_url_source": preference.source,
        "https_available": preference.https_available,
        "https_unavailable_reason": preference.https_unavailable_reason,
    }


@app.get("/api/download/android")
async def download_android():
    """Serve the Android APK. Looks for any .apk file in ba_home()/mobile/."""
    def _latest_apk() -> _PathLib | None:
        _MOBILE_DIR.mkdir(parents=True, exist_ok=True)
        apks = sorted(_MOBILE_DIR.glob("*.apk"))
        return apks[-1] if apks else None

    apk = await asyncio.to_thread(_latest_apk)
    if apk is None:
        raise HTTPException(status_code=404, detail="No Android APK found. Place the APK in ~/.better-agent/mobile/")
    return FileResponse(
        apk,
        media_type="application/vnd.android.package-archive",
        filename=apk.name,
    )


@app.get("/api/download/ios")
async def download_ios():
    """Serve the iOS IPA. Looks for any .ipa file in ba_home()/mobile/."""
    def _latest_ipa() -> _PathLib | None:
        _MOBILE_DIR.mkdir(parents=True, exist_ok=True)
        ipas = sorted(_MOBILE_DIR.glob("*.ipa"))
        return ipas[-1] if ipas else None

    ipa = await asyncio.to_thread(_latest_ipa)
    if ipa is None:
        raise HTTPException(status_code=404, detail="No iOS IPA found. Place the IPA in ~/.better-agent/mobile/")
    return FileResponse(
        ipa,
        media_type="application/octet-stream",
        filename=ipa.name,
    )


@app.get("/api/mobile/status")
async def mobile_status(request: Request):
    """Return which mobile builds are available and the server's
    phone-reachable base URL (for QR code generation). Prefer verified
    Tailscale HTTPS when available; otherwise fall back to the LAN URL."""
    def _mobile_build_status() -> dict:
        _MOBILE_DIR.mkdir(parents=True, exist_ok=True)
        return {
            "android": any(_MOBILE_DIR.glob("*.apk")),
            "ios": any(_MOBILE_DIR.glob("*.ipa")),
            **_mobile_version(),
        }

    build_status = await asyncio.to_thread(_mobile_build_status)
    server_url_info = await asyncio.to_thread(_preferred_server_url_info, request)
    return {
        **server_url_info,
        **build_status,
    }


def _desktop_file_response(path: _PathLib, filename: str | None = None):
    media_type = _mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(path, media_type=media_type, filename=filename or path.name)


def _desktop_update_file(rel_path: str) -> _PathLib:
    root = _desktop_update_repo_dir().resolve()
    candidate = (root / rel_path).resolve()
    if candidate == root or root not in candidate.parents:
        raise HTTPException(status_code=404, detail="desktop update file not found")
    if not candidate.is_file():
        raise HTTPException(status_code=404, detail="desktop update file not found")
    return candidate


@app.get("/api/download/desktop/macos")
async def download_desktop_macos():
    def _dmg_path() -> _PathLib | None:
        downloads_dir = _desktop_downloads_dir()
        downloads_dir.mkdir(parents=True, exist_ok=True)
        dmg = downloads_dir / "BetterAgent.dmg"
        return dmg if dmg.exists() else None

    dmg = await asyncio.to_thread(_dmg_path)
    if dmg is None:
        raise HTTPException(status_code=404, detail="No macOS desktop build found")
    return _desktop_file_response(dmg)


@app.get("/api/download/desktop/windows")
async def download_desktop_windows():
    def _installer_path() -> _PathLib | None:
        downloads_dir = _desktop_downloads_dir()
        downloads_dir.mkdir(parents=True, exist_ok=True)
        installer = downloads_dir / "BetterAgentSetup.exe"
        return installer if installer.exists() else None

    installer = await asyncio.to_thread(_installer_path)
    if installer is None:
        raise HTTPException(status_code=404, detail="No Windows desktop build found")
    return _desktop_file_response(installer)


@app.get("/api/desktop/status")
async def desktop_status(request: Request):
    def _desktop_build_status() -> dict:
        downloads_dir = _desktop_downloads_dir()
        update_repo_dir = _desktop_update_repo_dir()
        downloads_dir.mkdir(parents=True, exist_ok=True)
        update_repo_dir.mkdir(parents=True, exist_ok=True)
        return {
            "macos": (downloads_dir / "BetterAgent.dmg").exists(),
            "windows": (downloads_dir / "BetterAgentSetup.exe").exists(),
            "update_repo": (update_repo_dir / "metadata" / "root.json").exists(),
            **_desktop_version(),
        }

    build_status = await asyncio.to_thread(_desktop_build_status)
    server_url_info = await asyncio.to_thread(
        _preferred_server_url_info,
        request,
        allow_loopback_https=True,
    )
    server_url = server_url_info["server_url"]
    return {
        "desktop_shell": get_env("BETTER_CLAUDE_DESKTOP_SHELL") == "1",
        **server_url_info,
        "update_url": f"{server_url}/api/desktop/updates",
        **build_status,
    }


@app.get("/api/desktop/updates/{rel_path:path}")
async def desktop_update_file(rel_path: str):
    path = await asyncio.to_thread(_desktop_update_file, rel_path)
    return _desktop_file_response(path, filename=None)


# ============================================================================
# Workers panel + approval endpoints
# ============================================================================

def _internal_list_workers_for_cwd_sync(cwd: str) -> dict:
    import team_orchestration_read

    return team_orchestration_read.list_workers_for_cwd(cwd)


@app.post("/api/internal/workers/list")
async def internal_list_workers_for_cwd(
    body: dict | None = None,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    _require_team_orchestration_internal(x_internal_token)
    cwd = str((body or {}).get("cwd") or "")
    return await asyncio.to_thread(_internal_list_workers_for_cwd_sync, cwd)


@app.post("/api/internal/workers/create")
async def internal_create_worker(
    body: dict = Body(default={}),
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    _require_team_orchestration_internal(x_internal_token)
    """Create a new Better Agent session, run a tiny init turn to mint its
    agent_sid, and register it as a worker for the given cwd.

    Body: {cwd, description, orchestration_mode, model}.

    Blocks until the init turn completes (a few seconds). Returns the
    new worker record.
    """
    return await _create_worker_from_body(body or {})


@app.post("/api/internal/workers/provision-ui")
async def internal_provision_workers_ui(
    body: dict = Body(default={}),
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    _require_team_orchestration_internal(x_internal_token)
    """Idempotently create/reuse worker sessions for a cwd.

    Body: {cwd, workers:[{role_key, description, orchestration_mode, model}]}.
    Idempotency is by role_key when present, otherwise description. Existing
    workers are matched by the stable session name `worker:<key>`.
    """
    return await _provision_workers_from_body(body or {})


@app.post("/api/internal/workers/provision")
async def internal_provision_workers(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    _require_builtin_runtime_extension(extension_store.extension_id_for_role('team-orchestration'))
    """Internal-token variant for first-party local orchestrators."""
    if not coordinator.is_internal_caller(x_internal_token):
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    return await _provision_workers_from_body(body or {})


# Serializes find-then-create per (name, cwd) so two concurrent provisions
# of the same singleton worker can't both pass the find-None check and
# create duplicates. Single uvicorn worker (assumed everywhere else too)
# means asyncio.Lock is sufficient — the event loop can't context-switch
# inside the synchronous setdefault that creates a new lock.
_PROVISION_LOCKS: dict[str, asyncio.Lock] = {}
_POOL_PROCESSORS: dict[str, asyncio.Task] = {}
_POOL_ASK_WAITERS: dict[str, list[asyncio.Future]] = {}


def _provision_lock(name: str, cwd: str) -> asyncio.Lock:
    return _PROVISION_LOCKS.setdefault(f"{name}\0{cwd}", asyncio.Lock())


def _pool_worker_specs_for_prompt(specs: list, default_cwd: str) -> list[dict]:
    out: list[dict] = []
    for raw in specs:
        if not isinstance(raw, dict):
            continue
        tags = _normalize_pool_context_tags(raw.get("tags"))
        if not tags:
            continue
        key = str(raw.get("role_key") or raw.get("description") or "").strip()
        if not key:
            continue
        out.append({
            "name": f"worker:{key}",
            "description": str(raw.get("description") or f"worker:{key}").strip(),
            "cwd": str(raw.get("cwd") or default_cwd).strip(),
            "orchestration_mode": str(raw.get("orchestration_mode") or "native").strip(),
            "tags": tags,
        })
    return out


def _normalize_pool_context_tags(value) -> list[str]:
    from stores import worker_store as _ws

    return _ws.normalize_tags(value)


def _pool_worker_context_for_prompt(*, body: dict, bc_session_id: str, description: str) -> str:
    tags = _normalize_pool_context_tags(body.get("tags"))
    if not tags:
        return ""
    peers_by_name: dict[str, dict] = {}
    for worker in body.get("pool_worker_specs") or []:
        if not set(tags).intersection(_normalize_pool_context_tags(worker.get("tags"))):
            continue
        peers_by_name[str(worker.get("name") or "")] = worker
    from stores import worker_store as _ws

    for worker in _ws.list_workers(""):
        worker_tags = _normalize_pool_context_tags(worker.get("tags"))
        if not set(tags).intersection(worker_tags):
            continue
        name = str(worker.get("name") or worker.get("agent_session_id") or "").strip()
        if not name:
            continue
        peers_by_name[name] = {
            "name": name,
            "description": name,
            "cwd": str(worker.get("cwd") or "").strip(),
            "orchestration_mode": str(worker.get("orchestration_mode") or "native").strip(),
            "tags": worker_tags,
            "agent_session_id": str(worker.get("agent_session_id") or "").strip(),
        }
    lines = [
        "<worker_pool>",
        f"<self session_id=\"{escape(bc_session_id, quote=True)}\" "
        f"description=\"{escape(description, quote=True)}\" "
        f"tags=\"{escape(', '.join(tags), quote=True)}\" />",
        "<peers>",
    ]
    for peer in sorted(peers_by_name.values(), key=lambda item: str(item.get("name") or "")):
        lines.append(
            "<peer "
            f"name=\"{escape(str(peer.get('name') or ''), quote=True)}\" "
            f"session_id=\"{escape(str(peer.get('agent_session_id') or ''), quote=True)}\" "
            f"cwd=\"{escape(str(peer.get('cwd') or ''), quote=True)}\" "
            f"mode=\"{escape(str(peer.get('orchestration_mode') or 'native'), quote=True)}\" "
            f"tags=\"{escape(', '.join(_normalize_pool_context_tags(peer.get('tags'))), quote=True)}\" "
            f"description=\"{escape(str(peer.get('description') or ''), quote=True)}\" "
            "/>"
        )
    lines.extend([
        "</peers>",
        "<messaging>Use mssg(target_session_id, message) to coordinate with pool peers that have a session_id.</messaging>",
        "</worker_pool>",
    ])
    return "\n".join(lines)


def _worker_provision_prompt_for_body(*, body: dict, bc_session_id: str, description: str) -> str:
    base = _api_optional_provision_prompt(body.get("provision_prompt"))
    if base is None:
        base = render_prompt("worker_prep.md", {"description": description})
    pool_context = _pool_worker_context_for_prompt(
        body=body,
        bc_session_id=bc_session_id,
        description=description,
    )
    if not pool_context:
        return base
    return f"{base}\n\n{pool_context}"


def _api_optional_pool_affinity_key(value: object) -> str:
    if value in (None, ""):
        return ""
    key = str(value).strip()
    if not key:
        return ""
    if len(key) > 200:
        raise HTTPException(status_code=400, detail="pool_affinity_key must be at most 200 characters")
    return key


def _api_disallowed_tools(value: object) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise HTTPException(status_code=400, detail="disallowed_tools must be a list")
    tools = []
    for item in value:
        tool = str(item).strip()
        if not tool:
            raise HTTPException(status_code=400, detail="disallowed_tools entries must be non-empty strings")
        tools.append(tool)
    return list(dict.fromkeys(tools))


def _api_disabled_builtin_extensions(value: object) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise HTTPException(status_code=400, detail="disabled_builtin_extensions must be a list")
    extensions = []
    for item in value:
        extension_id = str(item).strip()
        if not extension_id:
            raise HTTPException(status_code=400, detail="disabled_builtin_extensions entries must be non-empty strings")
        extensions.append(extension_id)
    return list(dict.fromkeys(extensions))


@app.post("/api/internal/worker-pools/enqueue")
async def internal_enqueue_worker_pool_prompt(
    body: dict = Body(default={}),
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    _require_team_orchestration_internal(x_internal_token)

    tag = str((body or {}).get("tag") or "").strip()
    sender_session_id = str((body or {}).get("sender_session_id") or "").strip()
    prompt = str((body or {}).get("prompt") or "").strip()
    if not tag or not sender_session_id or not prompt:
        raise HTTPException(status_code=400, detail="tag, sender_session_id, and prompt are required")
    if not await _session_lite(sender_session_id):
        raise HTTPException(status_code=404, detail="sender_session_id does not exist")
    queued = await _enqueue_worker_pool_message(
        tag=tag,
        sender_session_id=sender_session_id,
        prompt=prompt,
        expect_mssg_response=bool((body or {}).get("expect_mssg_response")),
        pool_affinity_key=_api_optional_pool_affinity_key((body or {}).get("pool_affinity_key")),
    )
    return {"success": True, **queued}


async def _enqueue_worker_pool_message(
    *,
    tag: str,
    sender_session_id: str,
    prompt: str,
    expect_mssg_response: bool,
    pool_affinity_key: str = "",
    provider_id: str = "",
    model: str = "",
    reasoning_effort: str = "",
    wait_for_ask_response: bool = False,
    ask_id: str = "",
) -> dict:
    from stores import worker_store as _ws

    item = {
        "id": str(uuid.uuid4()),
        "tag": tag,
        "sender_session_id": sender_session_id,
        "prompt": prompt,
        "expect_mssg_response": expect_mssg_response,
        "pool_affinity_key": pool_affinity_key,
        "provider_id": provider_id,
        "model": model,
        "reasoning_effort": reasoning_effort,
        "wait_for_ask_response": wait_for_ask_response,
        "ask_id": ask_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    queued = await asyncio.to_thread(_ws.enqueue_pool_task, tag, item)
    _ensure_worker_pool_processor(tag)
    await coordinator.broadcast_workers_changed(None)
    return queued


def _ensure_worker_pool_processor(tag: str) -> None:
    clean = str(tag or "").strip()
    if not clean:
        return
    task = _POOL_PROCESSORS.get(clean)
    if task is None or task.done():
        _POOL_PROCESSORS[clean] = asyncio.create_task(
            _process_worker_pool_queue(clean),
            name=f"worker-pool-{clean}",
        )


async def _process_worker_pool_queue(tag: str) -> None:
    from stores import worker_store as _ws

    while True:
        item = await asyncio.to_thread(_ws.peek_pool_task, tag)
        if not item:
            return
        target = await asyncio.to_thread(
            _pick_pool_worker_for_sender,
            tag,
            str(item.get("sender_session_id") or ""),
            str(item.get("pool_affinity_key") or ""),
            True,
        )
        if not target:
            await asyncio.sleep(1)
            continue
        try:
            if item.get("wait_for_ask_response"):
                result = await coordinator.ask_team_message(
                    sender_session_id=str(item.get("sender_session_id") or ""),
                    target_session_id=target["agent_session_id"],
                    message=str(item.get("prompt") or ""),
                    ask_id=str(item.get("ask_id") or ""),
                    provider_id=str(item.get("provider_id") or ""),
                    model=str(item.get("model") or ""),
                    reasoning_effort=str(item.get("reasoning_effort") or ""),
                    target_selector={
                        "kind": "pool",
                        "value": tag,
                        "pool_affinity_key": str(item.get("pool_affinity_key") or ""),
                    },
                )
                ask_id = str(item.get("ask_id") or "")
                if ask_id:
                    import ask_status_store

                    await ask_status_store.write_status_async(ask_id, result=result)
                _complete_pool_ask_waiters(ask_id, result)
            else:
                await coordinator.submit_team_message(
                    sender_session_id=str(item.get("sender_session_id") or ""),
                    target_session_id=target["agent_session_id"],
                    message=str(item.get("prompt") or ""),
                    detach=True,
                    expect_mssg_response=bool(item.get("expect_mssg_response")),
                    provider_id=str(item.get("provider_id") or ""),
                    model=str(item.get("model") or ""),
                    reasoning_effort=str(item.get("reasoning_effort") or ""),
                    target_selector={
                        "kind": "pool",
                        "value": tag,
                        "pool_affinity_key": str(item.get("pool_affinity_key") or ""),
                    },
                )
        except Exception as exc:
            logger.exception(
                "worker pool dispatch failed tag=%s item_id=%s target_session_id=%s",
                tag,
                item.get("id"),
                target.get("agent_session_id"),
            )
            failure = await asyncio.to_thread(
                _ws.record_pool_task_failure,
                tag,
                str(item.get("id") or ""),
                str(exc),
            )
            await coordinator.broadcast_workers_changed(None)
            if failure.get("action") == "failed" and item.get("wait_for_ask_response"):
                result = {"success": False, "error": str(exc) or exc.__class__.__name__}
                ask_id = str(item.get("ask_id") or "")
                if ask_id:
                    import ask_status_store

                    await ask_status_store.write_status_async(ask_id, result=result)
                _complete_pool_ask_waiters(ask_id, result)
            if failure.get("action") == "requeued" and int(failure.get("queued_count") or 0) <= 1:
                return
            continue
        await asyncio.to_thread(_ws.pop_pool_task, tag, str(item.get("id") or ""))
        await coordinator.broadcast_workers_changed(None)


async def _pool_ask_status(ask_id: str) -> dict | None:
    if not ask_id:
        return None
    import ask_status_store

    return await asyncio.to_thread(ask_status_store.read_status, ask_id)


async def _pool_ask_result_if_done(ask_id: str) -> dict | None:
    status = await _pool_ask_status(ask_id)
    if isinstance(status, dict) and isinstance(status.get("result"), dict):
        return status["result"]
    return None


async def _wait_for_pool_ask_result(ask_id: str, queued: dict) -> dict:
    existing = await _pool_ask_result_if_done(ask_id)
    if existing is not None:
        return existing

    loop = asyncio.get_running_loop()
    future = loop.create_future()
    waiters = _POOL_ASK_WAITERS.setdefault(ask_id, [])
    waiters.append(future)
    try:
        existing = await _pool_ask_result_if_done(ask_id)
        if existing is not None:
            return existing
        result = await asyncio.wait_for(future, timeout=24 * 60 * 60)
        if isinstance(result, dict):
            return result
        return {
            "success": False,
            "error": "ask failed",
            "queued": True,
            "pool_queue_item_id": ((queued or {}).get("item") or {}).get("id"),
        }
    finally:
        waiters = _POOL_ASK_WAITERS.get(ask_id) or []
        if future in waiters:
            waiters.remove(future)
        if not waiters:
            _POOL_ASK_WAITERS.pop(ask_id, None)


def _complete_pool_ask_waiters(ask_id: str, result: dict) -> None:
    if not ask_id:
        return
    waiters = list(_POOL_ASK_WAITERS.get(ask_id) or [])
    for future in waiters:
        if future.done():
            continue
        future.get_loop().call_soon_threadsafe(future.set_result, result)


def _pick_idle_pool_worker(tag: str) -> dict | None:
    from stores import worker_store as _ws

    candidates = []
    for worker in _ws.list_workers(""):
        if tag not in _ws.normalize_tags(worker.get("tags")):
            continue
        sid = str(worker.get("agent_session_id") or "")
        session = session_manager.get_lite(sid)
        if not session:
            continue
        if coordinator.turn_manager.is_running_cached(sid):
            continue
        if session.get("queued_prompts"):
            continue
        candidates.append({**worker, "name": session.get("name") or worker.get("name")})
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: item.get("last_active") or "")[0]


def _pool_worker_by_session_id(tag: str, worker_session_id: str) -> dict | None:
    from stores import worker_store as _ws

    wanted = str(worker_session_id or "").strip()
    if not wanted:
        return None
    for worker in _ws.list_workers(""):
        if str(worker.get("agent_session_id") or "") != wanted:
            continue
        if tag not in _ws.normalize_tags(worker.get("tags")):
            return None
        session = session_manager.get_lite(wanted)
        if not session:
            return None
        return {**worker, "name": session.get("name") or worker.get("name")}
    return None


def _pick_pool_worker_for_sender(
    tag: str,
    sender_session_id: str,
    pool_affinity_key: str,
    require_idle: bool,
) -> dict | None:
    clean_key = str(pool_affinity_key or "").strip()
    if clean_key:
        from stores import pool_affinity_store as _pas

        bound_id = _pas.get_binding(tag, sender_session_id, clean_key)
        if bound_id:
            bound = _pool_worker_by_session_id(tag, bound_id)
            if bound:
                if not require_idle:
                    return bound
                if not coordinator.turn_manager.is_running_cached(bound_id):
                    session = session_manager.get_lite(bound_id)
                    if session and not session.get("queued_prompts"):
                        return bound
                return None
            _pas.clear_binding(tag, sender_session_id, clean_key)
    target = _pick_idle_pool_worker(tag)
    if target and clean_key:
        from stores import pool_affinity_store as _pas

        _pas.bind(tag, sender_session_id, clean_key, str(target.get("agent_session_id") or ""))
    return target


def _find_worker_by_agent_session_id(agent_session_id: str) -> dict | None:
    from stores import worker_store as _ws

    wanted = str(agent_session_id or "").strip()
    if not wanted:
        return None
    for worker in _ws.list_workers(""):
        if str(worker.get("agent_session_id") or "") == wanted:
            return worker
    return None


def _provision_parent_session_id(body: dict, spec: dict) -> str:
    parent_id = str(
        spec.get("parent_session_id")
        or body.get("parent_session_id")
        or body.get("app_session_id")
        or ""
    ).strip()
    if parent_id:
        return parent_id
    team_id = str((spec.get("team_instance_id") or body.get("team_instance_id") or "")).strip()
    if not team_id:
        return ""
    import team_store

    team = team_store.get(team_id)
    return str((team or {}).get("root_session_id") or "").strip()


def _worker_working_mode_meta(parent_session_id: str, body: dict, spec: dict, key: str) -> dict:
    from stores import worker_store as _ws

    meta = {
        "parent_session_id": parent_session_id,
        "role_key": key,
    }
    team_id = str((spec.get("team_instance_id") or body.get("team_instance_id") or "")).strip()
    if team_id:
        meta["team_instance_id"] = team_id
    tags = _ws.normalize_tags(spec.get("tags"))
    if tags:
        meta["pool_tags"] = tags
    return meta


def _mark_worker_under_parent(worker_session_id: str, parent_session_id: str, body: dict, spec: dict, key: str) -> None:
    worker_sid = str(worker_session_id or "").strip()
    parent_sid = str(parent_session_id or "").strip()
    if not worker_sid or not parent_sid or worker_sid == parent_sid:
        return
    if not session_manager.get_lite(parent_sid):
        raise HTTPException(status_code=400, detail="parent_session_id does not exist")
    import working_mode

    working_mode.mark_working_mode(
        worker_sid,
        mode="worker_pool",
        meta=_worker_working_mode_meta(parent_sid, body, spec, key),
    )


async def _provision_workers_from_body(body: dict):
    import team_store
    from stores import worker_store as _ws

    cwd = str((body or {}).get("cwd") or "").strip()
    specs = (body or {}).get("workers") or []
    # Top-level default lets a first-party orchestrator (TestApe) provision a
    # whole worker set as bare in one call; a per-worker spec can override.
    body_bare = bool((body or {}).get("bare_config", False))
    if not cwd:
        raise HTTPException(status_code=400, detail=t("error.cwd_required"))
    if not isinstance(specs, list):
        raise HTTPException(status_code=400, detail="workers must be a list")
    results = []
    created_any = False
    pool_worker_specs = _pool_worker_specs_for_prompt(specs, cwd)
    try:
        for raw in specs:
            spec = raw if isinstance(raw, dict) else {}
            key = str(spec.get("role_key") or spec.get("description") or "").strip()
            if not key:
                raise HTTPException(status_code=400, detail="worker role_key or description required")
            worker_cwd = str(spec.get("cwd") or cwd).strip()
            if not worker_cwd:
                raise HTTPException(status_code=400, detail=t("error.cwd_required"))
            name = f"worker:{key}"
            disallowed_tools = _api_disallowed_tools(spec.get("disallowed_tools"))
            has_disabled_extensions = "disabled_builtin_extensions" in spec
            disabled_extensions = (
                _api_disabled_builtin_extensions(spec.get("disabled_builtin_extensions"))
                if has_disabled_extensions
                else None
            )
            parent_session_id = _provision_parent_session_id(body, spec)
            async with _provision_lock(name, worker_cwd):
                existing = await asyncio.to_thread(_find_worker_by_session_name, worker_cwd, name)
                if existing:
                    if disallowed_tools:
                        await asyncio.to_thread(
                            session_manager.set_disallowed_tools,
                            existing["agent_session_id"],
                            disallowed_tools,
                        )
                    if has_disabled_extensions:
                        await asyncio.to_thread(
                            session_manager.set_disabled_builtin_extensions,
                            existing["agent_session_id"],
                            disabled_extensions or [],
                        )
                    existing_cwd = existing.get("cwd") or existing.get("registry_cwd") or worker_cwd
                    requested_tags = spec.get("tags")
                    if requested_tags is not None:
                        existing_tags = _ws.normalize_tags(existing.get("tags"))
                        merged_tags = _ws.normalize_tags([*existing_tags, *requested_tags])
                        if merged_tags != existing_tags:
                            existing = await asyncio.to_thread(
                                _ws.upsert_worker,
                                agent_session_id=existing["agent_session_id"],
                                name=existing.get("name") or name,
                                cwd=existing_cwd,
                                orchestration_mode=(
                                    existing.get("orchestration_mode")
                                    or spec.get("orchestration_mode")
                                    or "native"
                                ),
                                agent_sid=existing.get("agent_sid"),
                                node_id=existing.get("node_id"),
                                role_key=existing.get("role_key") or key,
                                tags=merged_tags,
                            )
                    await asyncio.to_thread(
                        _mark_worker_under_parent,
                        existing["agent_session_id"],
                        parent_session_id,
                        body,
                        spec,
                        key,
                    )
                    result = {
                        **existing,
                        "created": False,
                        "role_key": key,
                        "registry_cwd": existing_cwd,
                        "parent_session_id": parent_session_id or None,
                    }
                    _register_provisioned_team_member(team_store, body, spec, result, key)
                    results.append(result)
                    continue
                create_body = {
                    "cwd": worker_cwd,
                    "name": name,
                    "description": spec.get("description") or name,
                    "orchestration_mode": spec.get("orchestration_mode") or "native",
                    "model": spec.get("model"),
                    "provider_id": spec.get("provider_id"),
                    "reasoning_effort": spec.get("reasoning_effort"),
                    "node_id": spec.get("node_id"),
                    "role_key": key,
                    "tags": spec.get("tags"),
                    "bare_config": bool(spec.get("bare_config", body_bare)),
                    "disallowed_tools": disallowed_tools,
                    "disabled_builtin_extensions": disabled_extensions,
                    "provision_prompt": spec.get("provision_prompt"),
                    "capability_contexts": spec.get("capability_contexts"),
                    "pool_worker_specs": pool_worker_specs,
                }
                if create_body["bare_config"]:
                    created = await asyncio.to_thread(
                        _create_pending_worker_from_body,
                        create_body,
                    )
                else:
                    created = await _create_worker_from_body(create_body, broadcast=False)
                created_any = True
                await asyncio.to_thread(
                    _mark_worker_under_parent,
                    created["agent_session_id"],
                    parent_session_id,
                    body,
                    spec,
                    key,
                )
                result = {
                    **created,
                    "created": True,
                    "role_key": key,
                    "registry_cwd": created.get("cwd") or worker_cwd,
                    "parent_session_id": parent_session_id or None,
                }
                _register_provisioned_team_member(team_store, body, spec, result, key)
                results.append(result)
    finally:
        if created_any:
            await coordinator.broadcast_workers_changed(None)
    return {"workers": results}


def _register_provisioned_team_member(team_store_module, body: dict, spec: dict, result: dict, key: str) -> None:
    team_id = str((spec.get("team_instance_id") or body.get("team_instance_id") or "")).strip()
    if not team_id:
        return
    member_id = str(spec.get("member_id") or key).strip()
    team_store_module.upsert_member(
        team_id,
        member_id=member_id,
        member_type="worker",
        agent_session_id=result["agent_session_id"],
        role=str(spec.get("role") or key).strip(),
        description=str(spec.get("description") or result.get("name") or key).strip(),
        cwd=str(result.get("registry_cwd") or result.get("cwd") or body.get("cwd") or "").strip(),
        provider_id=str(spec.get("provider_id") or "").strip(),
        model=str(spec.get("model") or "").strip(),
        reasoning_effort=str(spec.get("reasoning_effort") or "").strip(),
        run_mode=str(spec.get("run_mode") or "").strip(),
        parent_member_id=str(spec.get("parent_member_id") or "").strip(),
        status="active",
    )


def _find_worker_by_session_name(cwd: str, name: str) -> dict | None:
    from stores import worker_store as _ws

    raw = _ws._read()
    for worker in raw.get("workers", []):
        worker_name = str(worker.get("name") or "").strip()
        bc = session_manager.get_lite(worker.get("agent_session_id"))
        worker_cwd = str((worker.get("cwd") or bc.get("cwd")) if bc else "").strip()
        if worker_name and worker_name == name and worker_cwd == str(cwd or "").strip():
            return {
                "agent_session_id": worker.get("agent_session_id"),
                "name": worker_name,
                "display_name": bc.get("name") if bc else worker_name,
                "role_key": worker.get("role_key"),
                "cwd": worker.get("cwd") or (bc.get("cwd") if bc else cwd),
                "registry_cwd": worker.get("cwd") or (bc.get("cwd") if bc else cwd),
                "orchestration_mode": worker.get("orchestration_mode") or (bc.get("orchestration_mode") if bc else None),
                "agent_sid": worker.get("agent_sid"),
                "initialized": bool(bc.get("agent_session_id")) if bc else bool(worker.get("agent_sid")),
                "diverged": False,
                "delegation_count": worker.get("delegation_count", 0),
            }
        if bc and bc.get("name") == name and worker_cwd == str(cwd or "").strip():
            return {
                "agent_session_id": bc["id"],
                "name": worker.get("name") or bc.get("name"),
                "display_name": bc.get("name"),
                "role_key": worker.get("role_key"),
                "cwd": worker.get("cwd") or bc.get("cwd"),
                "registry_cwd": worker.get("cwd") or bc.get("cwd"),
                "orchestration_mode": worker.get("orchestration_mode") or bc.get("orchestration_mode"),
                "agent_sid": worker.get("agent_sid"),
                "initialized": bool(bc.get("agent_session_id")),
                "diverged": False,
                "delegation_count": worker.get("delegation_count", 0),
            }
    return None


def _create_pending_worker_from_body(body: dict):
    from stores import worker_store as _ws

    cwd = body.get("cwd")
    description = body.get("description") or t("worker.default_name")
    session_name = body.get("name") or description
    mode = body.get("orchestration_mode") or "native"
    provider_id = body.get("provider_id")
    try:
        capability_contexts = normalize_capability_contexts(body.get("capability_contexts"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    provider_record = _provider_for_required_model(provider_id)
    model = _required_model_from_body_or_provider(body, provider_record)
    reasoning_effort = _provider_reasoning_effort(
        provider_id,
        _api_reasoning_effort(body.get("reasoning_effort")),
    )
    if not cwd:
        raise HTTPException(status_code=400, detail=t("error.cwd_required"))
    if mode == "manager":
        mode = "team"
    if mode not in ("team", "native"):
        raise HTTPException(status_code=400, detail=t("error.orchestration_mode_must_be_manager_or_native"))
    node_id = _resolve_session_node_id(body)
    bc = session_manager.create(
        name=session_name,
        model=model,
        cwd=cwd,
        orchestration_mode=mode,
        provider_id=provider_id,
        reasoning_effort=reasoning_effort,
        node_id=node_id,
        bare_config=True,
        capability_contexts=capability_contexts,
        disallowed_tools=body.get("disallowed_tools"),
        disabled_builtin_extensions=body.get("disabled_builtin_extensions"),
    )
    rec = _ws.upsert_worker(
        cwd=cwd,
        agent_session_id=bc["id"],
        orchestration_mode=mode,
        agent_sid=None,
        node_id=node_id,
        name=body.get("name"),
        role_key=body.get("role_key"),
        tags=body.get("tags"),
    )
    return {
        "agent_session_id": bc["id"],
        "name": body.get("name") or bc["name"],
        "display_name": bc["name"],
        "role_key": body.get("role_key"),
        "cwd": cwd,
        "registry_cwd": cwd,
        "orchestration_mode": mode,
        "agent_sid": None,
        "initialized": False,
        "diverged": False,
        "delegation_count": rec.get("delegation_count", 0),
        "tags": rec.get("tags") or [],
    }


async def _create_worker_from_body(body: dict, broadcast: bool = True):
    from stores import worker_store as _ws

    cwd = body.get("cwd")
    description = body.get("description") or t("worker.default_name")
    session_name = body.get("name") or description
    mode = body.get("orchestration_mode") or "native"
    provider_id = body.get("provider_id")
    try:
        capability_contexts = normalize_capability_contexts(body.get("capability_contexts"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    provider_record = await asyncio.to_thread(_provider_for_required_model, provider_id)
    model = _required_model_from_body_or_provider(body, provider_record)
    reasoning_effort = await asyncio.to_thread(
        _provider_reasoning_effort,
        provider_id,
        _api_reasoning_effort(body.get("reasoning_effort")),
    )
    if not cwd:
        raise HTTPException(status_code=400, detail=t("error.cwd_required"))
    if mode == "manager":
        mode = "team"
    if mode not in ("team", "native"):
        raise HTTPException(status_code=400, detail=t("error.orchestration_mode_must_be_manager_or_native"))
    node_id = _resolve_session_node_id(body)

    bc = await asyncio.to_thread(
        lambda: session_manager.create(
            name=session_name, model=model, cwd=cwd, orchestration_mode=mode,
            provider_id=provider_id,
            reasoning_effort=reasoning_effort,
            node_id=node_id, bare_config=bool(body.get("bare_config", False)),
            capability_contexts=capability_contexts,
            disallowed_tools=body.get("disallowed_tools"),
            disabled_builtin_extensions=body.get("disabled_builtin_extensions"),
        )
    )
    cancel_event = asyncio.Event()
    coordinator.init_cancel_events[bc["id"]] = ("__rest_api__", cancel_event)
    try:
        init_sid = await coordinator._init_target_agent_session(
            bc_session=bc, model=model, cwd=cwd,
            description=description, cancel_event=cancel_event,
            provision_prompt=_worker_provision_prompt_for_body(
                body=body,
                bc_session_id=bc["id"],
                description=description,
            ),
        )
    except Exception as e:
        await asyncio.to_thread(session_manager.delete, bc["id"])
        raise HTTPException(status_code=500, detail=t("error.init_turn_failed", e=str(e)))
    finally:
        coordinator.init_cancel_events.pop(bc["id"], None)
    if not init_sid:
        await asyncio.to_thread(session_manager.delete, bc["id"])
        raise HTTPException(status_code=500, detail=t("error.init_turn_no_session_id"))

    rec = await asyncio.to_thread(
        _ws.upsert_worker,
        cwd=cwd,
        agent_session_id=bc["id"],
        orchestration_mode=mode,
        agent_sid=init_sid,
        node_id=node_id,
        name=body.get("name"),
        role_key=body.get("role_key"),
        tags=body.get("tags"),
    )
    if broadcast:
        await coordinator.broadcast_workers_changed(None)
    return {
        "agent_session_id": bc["id"],
        "name": body.get("name") or bc["name"],
        "display_name": bc["name"],
        "role_key": body.get("role_key"),
        "cwd": cwd,
        "registry_cwd": cwd,
        "orchestration_mode": mode,
        "agent_sid": init_sid,
        "initialized": True,
        "diverged": False,
        "delegation_count": rec.get("delegation_count", 0),
        "tags": rec.get("tags") or [],
    }


@app.post("/api/internal/workers/from-session")
async def internal_register_existing_session_as_worker(
    body: dict = Body(default={}),
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    _require_team_orchestration_internal(x_internal_token)
    """Register an existing Better Agent session as a worker.

    Body: {cwd, agent_session_id}. If the session already has an
    agent_sid it is registered immediately. Otherwise a one-time init
    turn is run to mint the agent_sid before registration.
    """
    from stores import worker_store as _ws
    cwd = (body or {}).get("cwd")
    bc_sid = (body or {}).get("agent_session_id")
    if not bc_sid:
        raise HTTPException(status_code=400, detail="agent_session_id is required")
    if not cwd:
        raise HTTPException(status_code=400, detail=t("error.cwd_plus_session_id_required"))
    bc = await _session_lite(bc_sid)
    if not bc:
        raise HTTPException(status_code=404, detail=t("error.bc_session_not_found"))
    worker_cwd = str(bc.get("cwd") or cwd)
    mode = bc.get("orchestration_mode") or "native"
    if mode == "manager":
        mode = "team"
    if mode not in ("team", "native"):
        mode = "native"
    agent_sid = bc.get("agent_session_id")

    # No prior turn — run the init turn to mint agent_sid.
    if not agent_sid:
        if bc_sid in coordinator.init_cancel_events:
            raise HTTPException(
                status_code=409,
                detail=t("error.session_already_initializing"),
            )
        cancel_event = asyncio.Event()
        coordinator.init_cancel_events[bc_sid] = ("__rest_api__", cancel_event)
        try:
            model = str(bc.get("model") or "").strip()
            if not model:
                raise HTTPException(status_code=400, detail="session has no model configured")
            init_sid = await coordinator._init_target_agent_session(
                bc_session=bc, model=model,
                cwd=worker_cwd, description=bc.get("name") or "", cancel_event=cancel_event,
                provision_prompt=_worker_provision_prompt_for_body(
                    body=body,
                    bc_session_id=bc_sid,
                    description=bc.get("name") or "",
                ),
            )
        except Exception as e:
            raise HTTPException(status_code=500, detail=t("error.init_turn_failed", e=str(e)))
        finally:
            coordinator.init_cancel_events.pop(bc_sid, None)
        if not init_sid:
            raise HTTPException(
                status_code=500,
                detail=t("error.init_turn_no_session_id"),
            )
        agent_sid = init_sid

    rec = await asyncio.to_thread(
        _ws.upsert_worker,
        cwd=worker_cwd,
        agent_session_id=bc_sid,
        orchestration_mode=mode,
        agent_sid=agent_sid,
        # The worker runs wherever its Better Agent session lives — the session
        # record is the single source of truth for its node binding.
        node_id=bc.get("node_id") or "primary",
        tags=(body or {}).get("tags"),
    )
    await coordinator.broadcast_workers_changed(None)
    return {
        "agent_session_id": bc_sid,
        "name": bc.get("name"),
        "cwd": worker_cwd,
        "registry_cwd": worker_cwd,
        "orchestration_mode": mode,
        "agent_sid": agent_sid,
        "initialized": True,
        "diverged": False,
        "delegation_count": rec.get("delegation_count", 0),
        "tags": rec.get("tags") or [],
    }


@app.post("/api/internal/workers/unregister")
async def internal_unregister_worker(
    body: dict = Body(default={}),
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    _require_team_orchestration_internal(x_internal_token)
    agent_session_id = str((body or {}).get("agent_session_id") or "").strip()
    cwd = str((body or {}).get("cwd") or "").strip()
    if not agent_session_id:
        raise HTTPException(status_code=400, detail="agent_session_id is required")
    if not cwd:
        raise HTTPException(status_code=400, detail=t("error.cwd_required"))
    """Unregister a worker. Does NOT delete the Better Agent session
    itself — only removes it from the worker registry and clears
    any per-pair forks pointing at it as worker. Also cancels an
    in-flight init turn for this Better Agent session if one is still running."""
    from stores import worker_store as _ws
    init_entry = coordinator.init_cancel_events.get(agent_session_id)
    if init_entry:
        init_entry[1].set()
    removed = await asyncio.to_thread(_ws.remove_worker, cwd, agent_session_id)
    if removed:
        await coordinator.broadcast_workers_changed(None)
    return {"removed": removed}


@app.post("/api/internal/workers/reset-forks")
async def internal_reset_worker_forks(
    body: dict = Body(default={}),
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    _require_team_orchestration_internal(x_internal_token)
    agent_session_id = str((body or {}).get("agent_session_id") or "").strip()
    if not agent_session_id:
        raise HTTPException(status_code=400, detail="agent_session_id is required")
    """Drop all per-pair forks pointing at `agent_session_id` as worker.

    Used by the Team Orchestration extension reset action when the worker BC
    session has diverged from the manager's view (user typed in it
    directly). Next delegation will re-fork from the current head.
    """
    from stores import worker_store as _ws
    cleared = await asyncio.to_thread(_ws.clear_forks_for_worker_everywhere, agent_session_id)
    for fbsid in cleared:
        try:
            await asyncio.to_thread(session_manager.delete, fbsid)
        except Exception:
            logger.exception("delete delegate-fork BC %s failed during invalidate", fbsid)
    if cleared:
        await coordinator.broadcast_workers_changed(None)
    return {"forks_cleared": len(cleared)}


def _require_credential_broker_internal(x_internal_token: str) -> None:
    if not coordinator.is_internal_caller(x_internal_token):
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    _require_builtin_runtime_extension(extension_store.extension_id_for_role('credential-broker'))


@app.post("/api/internal/credential-ui/pending")
async def internal_list_pending_credentials(
    body: dict = Body(default={}),
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    _require_credential_broker_internal(x_internal_token)
    from credential_broker import consent_store as _cs

    app_session_id = (body or {}).get("app_session_id")
    pending = await asyncio.to_thread(_cs.list_pending, app_session_id=app_session_id)
    out = [
        _cs.public_view(rec)
        for rec in pending
    ]
    return {"consents": out}


@app.post("/api/internal/credential-ui/approve")
async def internal_approve_credential_consent(
    body: dict = Body(default={}),
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    _require_credential_broker_internal(x_internal_token)
    from credential_broker import broker as _broker

    body = body or {}
    consent_id = str(body.get("consent_id") or "").strip()
    if not consent_id:
        raise HTTPException(status_code=400, detail="consent_id is required")
    secret_values = body.get("secrets")
    secret_value = body.get("secret")
    if secret_values is not None and not isinstance(secret_values, dict):
        raise HTTPException(status_code=400, detail="secrets must be an object")
    try:
        rec, reason = _broker.approve_consent(
            consent_id,
            secret_value=secret_value,
            secret_values=secret_values,
        )
    except _broker.BrokerError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if reason == "missing":
        raise HTTPException(status_code=404, detail="consent not found")
    if reason == "expired":
        raise HTTPException(status_code=410, detail="consent expired")
    app_sid = (rec or {}).get("app_session_id")
    await coordinator.broadcast_credential_consent_changed(app_sid)
    return {"status": reason}


@app.post("/api/internal/credential-ui/deny")
async def internal_deny_credential_consent(
    body: dict = Body(default={}),
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    _require_credential_broker_internal(x_internal_token)
    from credential_broker import broker as _broker

    consent_id = str((body or {}).get("consent_id") or "").strip()
    if not consent_id:
        raise HTTPException(status_code=400, detail="consent_id is required")
    rec, reason = _broker.deny_consent(consent_id)
    if reason == "missing":
        raise HTTPException(status_code=404, detail="consent not found")
    await coordinator.broadcast_credential_consent_changed(
        (rec or {}).get("app_session_id")
    )
    return {"status": reason}


@app.post("/api/internal/credential-ui/revoke")
async def internal_revoke_credential_consent(
    body: dict = Body(default={}),
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    _require_credential_broker_internal(x_internal_token)
    from credential_broker import broker as _broker

    consent_id = str((body or {}).get("consent_id") or "").strip()
    if not consent_id:
        raise HTTPException(status_code=400, detail="consent_id is required")
    rec, reason = _broker.revoke_consent(consent_id)
    if reason == "missing":
        raise HTTPException(status_code=404, detail="consent not found")
    await coordinator.broadcast_credential_consent_changed(
        (rec or {}).get("app_session_id")
    )
    return {"status": reason}


@app.post("/api/internal/credential-ui/password-manager/list")
async def internal_list_password_manager_secrets(
    body: dict | None = None,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    _require_credential_broker_internal(x_internal_token)
    import password_manager

    try:
        return password_manager.list_service_passwords()
    except password_manager.PasswordManagerError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception:
        logger.exception("password manager keychain list failed")
        raise HTTPException(status_code=500, detail="failed to list passwords")


@app.post("/api/internal/credential-ui/password-manager/store")
async def internal_store_password_manager_secret(
    body: dict = Body(default={}),
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    _require_credential_broker_internal(x_internal_token)
    import password_manager

    try:
        stored = password_manager.store_service_password(body or {})
    except password_manager.PasswordManagerError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        logger.exception("password manager keychain store failed")
        raise HTTPException(status_code=500, detail="failed to store password")
    return {"status": "stored", **stored}


@app.post("/api/internal/credential-ui/password-manager/delete")
async def internal_delete_password_manager_secret(
    body: dict = Body(default={}),
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    _require_credential_broker_internal(x_internal_token)
    import password_manager

    try:
        deleted = password_manager.delete_service_password(body or {})
    except password_manager.PasswordManagerError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        logger.exception("password manager keychain delete failed")
        raise HTTPException(status_code=500, detail="failed to delete password")
    return {"status": "deleted", **deleted}


@app.post("/api/internal/pending-approvals/list")
async def internal_list_pending_approvals(
    body: dict | None = None,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    _require_team_orchestration_internal(x_internal_token)
    cwd = (body or {}).get("cwd")
    """Pending fresh-worker creation approvals, optionally filtered by
    cwd. Used by the frontend on WS reconnect to rehydrate inline
    approval cards.

    Filtered to delegations the backend is ACTIVELY awaiting — disk
    records with no in-memory waiter Future are orphans (left over
    from a crash; the runner already gave up or never came back).
    Surfacing them as cards would invite the user to approve a
    delegation no one is waiting on, spawning a worker for nothing.
    """
    active_dids = set(coordinator.approval_waiters.keys())
    pending = await asyncio.to_thread(pending_approvals.list_pending, cwd=cwd)
    out = [rec for rec in pending if rec.get("delegation_id") in active_dids]
    return {"approvals": out}


@app.post("/api/internal/tool-approvals/request")
async def internal_tool_approval_request(
    body: dict = Body(default={}),
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    """Runner→backend: a CLI tool/command needs human approval mid-turn
    (Claude `can_use_tool` or Codex app-server approval ServerRequest).

    Creates a pending record, broadcasts `tool_approval_requested` to the
    session's WS subscribers, and BLOCKS until the frontend decides or the
    fail-closed timeout fires. Returns {approved}; the runner treats any
    non-approved/failed response as a denial."""
    if not coordinator.is_internal_caller(x_internal_token):
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    body = body or {}
    app_session_id = str(body.get("app_session_id") or "").strip()
    if not app_session_id:
        raise HTTPException(status_code=400, detail="app_session_id is required")
    rec = tool_approval.registry.create(
        app_session_id=app_session_id,
        run_id=str(body.get("run_id") or ""),
        provider_kind=str(body.get("provider_kind") or ""),
        tool_name=str(body.get("tool_name") or ""),
        summary=body.get("summary") if isinstance(body.get("summary"), dict) else {},
    )
    await coordinator.broadcast_session(
        app_session_id,
        "tool_approval_requested",
        tool_approval.registry.public_view(rec),
        source="tool_approval",
    )
    approved = await tool_approval.registry.await_decision(rec)
    await coordinator.broadcast_session(
        app_session_id,
        "tool_approval_resolved",
        {"approval_id": rec.approval_id, "approved": approved},
        source="tool_approval",
    )
    return {"approved": approved, "approval_id": rec.approval_id}


@app.post("/api/sessions/{session_id}/tool-approvals/{approval_id}/decide")
async def decide_tool_approval(session_id: str, approval_id: str, body: dict = Body(default={})):
    """Frontend→backend: user clicked Approve/Deny on a tool-approval card.
    Resolves the runner's blocked request. Unknown/already-resolved ids are
    a no-op (the runner already timed out/denied).

    Object-level authz: the approval MUST belong to this session — the
    approval_id alone (even though uuid4) is not accepted across sessions."""
    rec = tool_approval.registry.get(approval_id)
    if rec is None or rec.app_session_id != session_id:
        raise HTTPException(status_code=404, detail=t("error.session_not_found_retry"))
    approved = bool((body or {}).get("approved"))
    ok = tool_approval.registry.decide(approval_id, approved)
    return {"ok": ok}


@app.get("/api/sessions/{session_id}/tool-approvals/pending")
async def list_pending_tool_approvals(session_id: str):
    """Rehydrate pending tool-approval cards on mount/reconnect. Live-only
    source (in-memory registry); a missed WS event no longer silently becomes
    a 5-minute denial — the card reappears on the next poll/reload."""
    return {
        "approvals": [tool_approval.registry.public_view(r) for r in tool_approval.registry.list_for_session(session_id)]
    }


@app.post("/api/internal/pending-approvals/approve")
async def internal_approve_pending_approval(
    body: dict = Body(default={}),
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    _require_team_orchestration_internal(x_internal_token)
    """Approve a fresh-worker creation request. Body may include
    optional `description` and `orchestration_mode` overrides (the
    user's edits in the inline card)."""
    body = body or {}
    delegation_id = str(body.get("delegation_id") or "").strip()
    if not delegation_id:
        raise HTTPException(status_code=400, detail="delegation_id is required")
    description = body.get("description")
    mode = body.get("orchestration_mode")
    rec, reason = pending_approvals.approve(
        delegation_id, description=description, orchestration_mode=mode,
    )
    if reason == "missing":
        raise HTTPException(status_code=404, detail=t("error.approval_not_found"))
    if reason == "expired":
        raise HTTPException(status_code=410, detail=t("error.approval_expired"))
    if reason == "already_resolved":
        # Idempotent — return the existing record so the second tab sees
        # the same answer the first tab got.
        return {"status": rec.get("status"), "record": rec, "idempotent": True}
    coordinator._resolve_approval(delegation_id, rec)
    return {"status": "approved", "record": rec}


@app.post("/api/internal/pending-approvals/deny")
async def internal_deny_pending_approval(
    body: dict = Body(default={}),
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    _require_team_orchestration_internal(x_internal_token)
    delegation_id = str((body or {}).get("delegation_id") or "").strip()
    if not delegation_id:
        raise HTTPException(status_code=400, detail="delegation_id is required")
    rec, reason = pending_approvals.deny(delegation_id)
    if reason == "missing":
        raise HTTPException(status_code=404, detail=t("error.approval_not_found"))
    if reason == "expired":
        raise HTTPException(status_code=410, detail=t("error.approval_expired"))
    if reason == "already_resolved":
        return {"status": rec.get("status"), "record": rec, "idempotent": True}
    coordinator._resolve_approval(delegation_id, rec)
    return {"status": "denied", "record": rec}


# ============================================================================
# Worker-node registration approvals
# ----------------------------------------------------------------------------
# A brand-new worker-node (not in topology.yaml, not yet in the registry)
# dials primary and blocks on its WS handshake while these endpoints let
# the logged-in user approve or deny it. Mirrors the fresh-worker approval
# flow above. All three are gated by the standard `/api/*` auth middleware,
# so only an authenticated operator can act.
# ============================================================================

@app.post("/api/internal/machine-nodes/pending")
async def internal_list_pending_nodes(
    body: dict | None = None,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    _require_machine_nodes_internal(x_internal_token)
    """Worker-nodes currently awaiting registration approval. Used by the
    frontend on mount / WS reconnect to (re)render the approval popup.

    Secrets never leave the server — only the display fingerprint does."""
    import node_link
    with perf.timed("internal.machine_nodes.pending"):
        pending = node_link.public_pending_nodes_cached()
        if pending is None:
            pending = await asyncio.to_thread(node_link.public_pending_nodes)
        return {
            "pending_nodes": pending,
        }


@app.post("/api/internal/machine-nodes/approve")
async def internal_approve_pending_node(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    _require_machine_nodes_internal(x_internal_token)
    """Approve a node registration: persist it to the registry (so future
    reconnects auto-authenticate with its secret) and, if the node is
    holding its WS open right now, let it connect immediately."""
    node_id = (body or {}).get("node_id")
    if not isinstance(node_id, str) or not node_id:
        raise HTTPException(status_code=400, detail="node_id is required")
    import node_link
    rec, reason = await node_link.approve_registration(node_id)
    if reason == "missing":
        raise HTTPException(status_code=404, detail=t("error.node_request_not_found"))
    if reason == "expired":
        raise HTTPException(status_code=410, detail=t("error.node_request_expired"))
    if reason == "already_resolved":
        return {"status": rec.get("status"), "record": node_link._public_rec(rec), "idempotent": True}
    return {"status": "approved", "record": node_link._public_rec(rec)}


@app.post("/api/internal/machine-nodes/deny")
async def internal_deny_pending_node(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    _require_machine_nodes_internal(x_internal_token)
    node_id = (body or {}).get("node_id")
    if not isinstance(node_id, str) or not node_id:
        raise HTTPException(status_code=400, detail="node_id is required")
    import node_link
    rec, reason = await node_link.deny_registration(node_id)
    if reason == "missing":
        raise HTTPException(status_code=404, detail=t("error.node_request_not_found"))
    if reason == "expired":
        raise HTTPException(status_code=410, detail=t("error.node_request_expired"))
    if reason == "already_resolved":
        return {"status": rec.get("status"), "record": node_link._public_rec(rec), "idempotent": True}
    return {"status": "denied", "record": node_link._public_rec(rec)}


@app.post("/api/internal/machine-nodes/revoke")
async def internal_revoke_node(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    _require_machine_nodes_internal(x_internal_token)
    node_id = (body or {}).get("node_id")
    if not isinstance(node_id, str) or not node_id:
        raise HTTPException(status_code=400, detail="node_id is required")
    """Revoke a node: drops it from the dynamic registry or static
    topology.yaml. Cleans up node_store state and fires WS broadcast."""
    import node_registry_store
    import node_store
    import topology

    if node_registry_store.remove(node_id):
        await node_store.forget(node_id)
        return {"status": "revoked", "node_id": node_id}

    try:
        removed = topology.remove_node(node_id)
    except topology.TopologyError:
        raise HTTPException(
            status_code=500,
            detail="topology.yaml is malformed — cannot delete node",
        )
    if not removed:
        raise HTTPException(status_code=404, detail=t("error.node_request_not_found"))

    await node_store.forget(node_id)
    return {"status": "revoked", "node_id": node_id}


@app.post("/api/internal/machine-nodes/restart")
async def internal_restart_node(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    _require_machine_nodes_internal(x_internal_token)
    node_id = (body or {}).get("node_id")
    if not isinstance(node_id, str) or not node_id:
        raise HTTPException(status_code=400, detail="node_id is required")
    """Tell a connected worker-node to restart its process."""
    import node_link

    try:
        await node_link.send_restart(node_id)
    except node_link.NodeOffline:
        raise HTTPException(
            status_code=409,
            detail="Node is not connected",
        )
    return {"status": "restart_sent", "node_id": node_id}


@app.get("/api/version")
async def version():
    return {"version": _GIT_SHA}


@app.get("/api/analytics")
async def get_analytics(
    start: str = Query(None),
    end: str = Query(None),
    granularity: str = Query(None),
):
    """Usage analytics over a time range. ``start``/``end`` are ISO dates
    ('YYYY-MM-DD'); optional ``granularity`` is hour/day/week/month."""
    start_dt, end_dt = analytics.resolve_bounds(start, end)
    bucket = analytics.resolve_granularity(granularity, start_dt, end_dt)
    return await asyncio.to_thread(
        analytics.compute_analytics,
        start_dt,
        end_dt,
        bucket,
    )


# ============================================================================
# WebSocket — Streaming Chat (Manager/Worker)
# ============================================================================

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


@app.websocket("/ws/chat")
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
                        # session_manager cache REST reads. Previously
                        # this branch ran reconcile inline on the full
                        # un-paginated tree per subscribe (≥1 per pane,
                        # multi-pane split-view → N× redundant). The
                        # cache is now reconciled async via
                        # `schedule_reconcile_if_needed` (cold-load +
                        # orphan-event triggered) — no inline reconcile
                        # here. Cap replay at the same `msg_limit` REST
                        # uses so cold-hop `since_seq=0` doesn't ship
                        # the entire history; frontend upsert-by-id
                        # makes the overlap with REST harmless.
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
                                sub_rid = await asyncio.to_thread(session_manager._root_id_for, sub_sid)
                                sub_gen = session_manager._reconcile_gen.get(sub_rid, 0) if sub_rid else 0
                                sub_dirty = await asyncio.to_thread(
                                    session_manager.is_reconcile_dirty,
                                    sub_rid,
                                ) if sub_rid else False
                                replay_asst = [
                                    m for m in replay_msgs
                                    if m.get("role") == "assistant"
                                ]
                                last_asst_evts = replay_asst[-1].get("events") if replay_asst else None
                                last_asst_stub = replay_asst[-1].get("stub") if replay_asst else None
                                logger.debug(
                                    "WS replay %s: since_seq=%d next_seq=%d msgs=%d "
                                    "inflight=%s gen=%d dirty=%s last_asst_evts=%s "
                                    "last_asst_stub=%s build=%.1fms delta=%.1fms post=%.1fms send=%.1fms",
                                    sub_sid[:8],
                                    since_seq,
                                    delta["next_seq"],
                                    len(replay_msgs),
                                    in_flight is not None,
                                    sub_gen,
                                    sub_dirty,
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
                    # Async-reconcile catch-up: schedule if needed (cold
                    # cache or orphan-event dirty), and emit a catch-up
                    # `session_processing_started` to this subscriber if
                    # a reconcile is already in flight. The catch-up
                    # emit + the timer-driven `started/finished` emits
                    # in `_async_reconcile_with_progress` share the
                    # per-root RLock, so this subscriber either sees
                    # in-flight=True and the catch-up arrives BEFORE
                    # any matching `finished`, or sees in-flight=False
                    # (and `finished` has already been broadcast).
                    try:
                        sub_root_id, in_flight = await asyncio.to_thread(
                            _reconcile_catchup_state,
                            sub_sid,
                        )
                        if sub_root_id and in_flight:
                            await ws_callback({
                                "type": "session_processing_started",
                                "data": {"root_id": sub_root_id},
                            })
                    except Exception:
                        logger.exception("processing catch-up on subscribe failed")
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

                # Sample the active provider RIGHT NOW so the next CLI spawn
                # inherits the right ANTHROPIC_API_KEY / BASE_URL / CONFIG_DIR.
                # Switching provider in the UI between turns must take effect
                # on the very next turn without a backend restart.
                await asyncio.to_thread(config_store.apply_env_vars)

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
                _offline_err = _node_offline_error(_offline_session)
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
                    except Exception:
                        _release_claim_on_failure()
                        raise
                elif alter_rewind_latest:
                    params["_alter_rewind_latest"] = True
                    try:
                        await coordinator.turn_manager.cancel_turn(
                            app_session_id,
                            interrupted_by_msg_id=lifecycle_msg_id,
                        )
                    except Exception:
                        _release_claim_on_failure()
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
                    "created_at": datetime.now().isoformat(),
                }
                try:
                    admission = await asyncio.to_thread(
                        session_manager.admit_queued_prompt,
                        app_session_id,
                        queued_prompt,
                    )
                except Exception:
                    _release_claim_on_failure()
                    raise
                if not admission.get("session"):
                    _release_claim_on_failure()
                    await _send_message_error(t("error.session_not_found_retry"))
                    continue
                existing_user_message = admission.get("existing_user_message")
                if existing_user_message:
                    _release_claim_on_failure()
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
                try:
                    await coordinator.submit_prompt_async(app_session_id, params)
                except Exception:
                    # Release the client_id claim taken above so the
                    # failed item doesn't block a later genuine re-send.
                    _release_claim_on_failure()
                    await asyncio.to_thread(
                        session_manager.remove_queued_prompt,
                        app_session_id,
                        item_id,
                    )
                    raise
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


async def _accept_ws_if_needed(websocket: WebSocket) -> None:
    if websocket.application_state == WebSocketState.CONNECTED:
        logger.warning("WebSocket entered /ws/chat already accepted")
        return
    await websocket.accept()


@app.websocket("/{_unknown_ws_path:path}")
async def unknown_websocket(websocket: WebSocket, _unknown_ws_path: str):
    await websocket.close(code=1008)


from fastapi.staticfiles import StaticFiles  # noqa: E402
import sys as _sys                                                  # noqa: E402

# Make `index.html` non-cacheable so a reload (browser ↻ or Capacitor
# WebView reload after the in-app restart button) always re-fetches
# the SPA shell. The shell references content-hashed JS/CSS bundles
# (Vite default), so once HTML is fresh the WebView pulls the new
# bundles via normal cache-miss. Web tabs get the same guarantee on
# top of the SW skipWaiting+clientsClaim flow. WITHOUT this header,
# WKWebView's HTTP cache can serve a stale index.html that still
# points at the OLD hashed bundles, leaving the user on the previous
# build even after the refresh button completes.
_NO_CACHE_HEADERS = {
    "Cache-Control": "no-cache, no-store, must-revalidate",
    "Pragma": "no-cache",
    "Expires": "0",
}


class _NoCacheIndexStaticFiles(StaticFiles):
    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        # `path` is the path RELATIVE to the mount root; the bare-mount
        # root "" and the explicit "index.html" both resolve to the SPA
        # shell. Everything else (hashed bundles, icons, manifest) keeps
        # the default long-cache behaviour StaticFiles already grants.
        if path in ("", ".", "index.html"):
            for k, v in _NO_CACHE_HEADERS.items():
                response.headers[k] = v
        return response


from fastapi import Request as _Request                          # noqa: E402
from fastapi.responses import JSONResponse as _JSONResponse      # noqa: E402
from fastapi.responses import HTMLResponse as _HTMLResponse      # noqa: E402


def frontend_dist_dir() -> Path:
    if getattr(_sys, "frozen", False):
        # PyInstaller bundle: the built frontend is bundled as data under the
        # extraction root `sys._MEIPASS` (see desktop/BetterAgent.spec).
        return Path(_sys._MEIPASS) / "frontend_dist"
    return Path(__file__).resolve().parent.parent / "frontend" / "dist"


@app.get("/provider-config-sync", include_in_schema=False)
@app.get("/provider-config-sync/", include_in_schema=False)
async def provider_config_sync_spa_route():
    _require_builtin_extension(extension_store.BUILTIN_PROVIDER_CONFIG_SYNC_EXTENSION_ID)
    return FileResponse(
        frontend_dist_dir() / "index.html",
        headers=_NO_CACHE_HEADERS,
    )


# Placeholder served while run.sh builds the frontend on a cold clone (no built
# dist/ yet). Auto-refreshes so the browser picks up the real app once the build
# lands and the supervisor restarts the backend. English-only by design — it is
# a pre-React bootstrap page, not part of the i18n surface.
_COLD_BUILDING_HTML = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="5">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Building frontend…</title>
<style>
  html,body{height:100%;margin:0}
  body{display:flex;align-items:center;justify-content:center;
       font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;
       color:#1f2937;background:#f8fafc}
  .card{padding:1.75rem 2.25rem;border-radius:12px;background:#fff;
        box-shadow:0 1px 3px rgba(0,0,0,.08);text-align:center}
  .dot{display:inline-block;width:9px;height:9px;border-radius:50%;
       background:#3b82f6;margin-right:9px;vertical-align:middle;
       animation:_p 1s ease-in-out infinite}
  @keyframes _p{0%,100%{opacity:.3}50%{opacity:1}}
</style>
</head>
<body><div class="card"><span class="dot"></span>Building frontend…</div></body>
</html>
"""

_COLD_BUILD_CHECK_INTERVAL = 1.0


def _mount_cold_build_stub(target_app: FastAPI) -> None:
    """Serve a placeholder while the frontend builds on a cold clone.

    Registered last, so every real API/WS route above still matches first.
    The supervisor restarts the backend once the build lands, after which
    mount_frontend mounts the real dist instead.
    """

    @target_app.get("/{full_path:path}", include_in_schema=False)
    async def _cold_build_placeholder(full_path: str):
        return _HTMLResponse(_COLD_BUILDING_HTML, headers=_NO_CACHE_HEADERS)


def _arm_cold_build_restart(target_app: FastAPI, dist_index: Path) -> None:
    """Restart the backend once the frontend build lands a real dist.

    The run.sh supervisor is the only thing that can respawn us, so this is a
    no-op (placeholder served until a manual restart) without it. One-shot:
    after the restart, mount_frontend sees the dist and takes the normal
    StaticFiles branch, so the watcher never re-arms.
    """
    if get_env("BETTER_CLAUDE_RUN_SH_SUPERVISOR") != "1":
        logger.info(
            "cold frontend build: supervisor absent; placeholder served until manual restart"
        )
        return

    async def _watch_for_build():
        try:
            while not dist_index.exists():
                await asyncio.sleep(_COLD_BUILD_CHECK_INTERVAL)
            # Empty request id: the build already completed (that is why we
            # are restarting), so run.sh must not rebuild again on the way
            # back up — an empty PENDING_REFRESH_ID skips start_frontend_build.
            logger.info("cold frontend build landed; requesting supervisor restart")
            await _trigger_supervisor_restart("")
        except Exception:
            logger.exception("cold-build restart watcher failed")

    async def _start_watcher():
        asyncio.create_task(_watch_for_build())

    target_app.add_event_handler("startup", _start_watcher)


def mount_frontend(target_app: FastAPI, *, dist_dir: Path | None = None) -> None:
    """Mount the built React frontend onto an already-registered API app.

    Registered AFTER every `@app.get("/api/...")` / `@app.websocket(...)`
    route above so explicit routes still match first; only unmatched paths
    fall through to StaticFiles.
    """
    resolved_dist_dir = dist_dir or frontend_dist_dir()
    if not resolved_dist_dir.exists():
        if dist_dir is not None:
            # An explicit caller (e.g. tests) asked for a specific dist and did
            # not provide one — fail loudly rather than silently serving a stub.
            raise RuntimeError(
                f"frontend dist directory not found at {resolved_dist_dir}. "
                "Run `cd frontend && npm run build` (or use ./run.sh which does it)."
            )
        # Cold clone with no built frontend yet: serve a placeholder so the API
        # is usable immediately while run.sh builds in the background, then arm
        # a one-shot supervisor restart to swap in the real dist when it lands.
        _mount_cold_build_stub(target_app)
        _arm_cold_build_restart(target_app, resolved_dist_dir / "index.html")
        return

    target_app.mount(
        "/",
        _NoCacheIndexStaticFiles(directory=str(resolved_dist_dir), html=True),
        name="frontend",
    )

    # SPA fallback. StaticFiles returns 404 for any path that isn't an
    # actual file in dist/, so direct navigation to a client-side route
    # (e.g. refresh on /s/<id>, or any future route) breaks. Catch 404
    # on non-API paths and serve index.html instead — React then mounts
    # and `useRoute` parses the URL.
    #
    # /api/* and /ws/* 404s keep returning JSON so REST clients aren't
    # fooled by an HTML body on a missing endpoint.
    @target_app.exception_handler(404)
    async def _spa_fallback(request: _Request, _exc):
        p = request.url.path
        if p.startswith("/api/") or p.startswith("/ws/"):
            return _JSONResponse({"detail": "Not Found"}, status_code=404)
        # Hashed bundles must 404 for real: serving index.html as a module
        # script makes the browser throw an opaque MIME error instead of a
        # clean missing-chunk failure.
        if p.startswith("/assets/"):
            return _JSONResponse({"detail": "Not Found"}, status_code=404)
        return FileResponse(resolved_dist_dir / "index.html", headers=_NO_CACHE_HEADERS)


# Production / desktop imports serve both API and frontend. Tests that only
# need API routes set this before importing `main` so no frontend build or
# fake `dist/` stub is required.
if get_env("BETTER_CLAUDE_API_ONLY") != "1":
    mount_frontend(app)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host=user_prefs.get_network_bind_address(),
        port=8000,
        reload=True,
        proxy_headers=False,
        ws_per_message_deflate=False,
    )
