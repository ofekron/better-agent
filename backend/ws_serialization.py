from __future__ import annotations

import asyncio
import contextvars
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import perf
from global_events import GLOBAL_EVENT_TYPES

_WS_JSON_EXECUTOR: ThreadPoolExecutor | None = ThreadPoolExecutor(
    max_workers=2, thread_name_prefix="ws-json",
)
_WS_JSON_EXECUTOR_LOCK = threading.Lock()


_WS_TRANSPORT_EVENT_TYPES = frozenset({
    "agent_message",
    "error",
    "messages_replay",
    "turn_complete",
    "turn_start",
    "turn_stopped",
})


def metric_event_type(value: Any) -> str:
    if not isinstance(value, dict):
        return "unknown"
    event_type = value.get("type")
    if not isinstance(event_type, str) or not event_type:
        return "unknown"
    if (
        event_type not in GLOBAL_EVENT_TYPES
        and event_type not in _WS_TRANSPORT_EVENT_TYPES
    ):
        return "other"
    return event_type.replace("-", "_")


async def dumps_ws_json(value: Any) -> str:
    ctx = contextvars.copy_context()
    loop = asyncio.get_running_loop()
    queued_at = time.perf_counter()
    event_type = metric_event_type(value)

    def _dump() -> str:
        started = time.perf_counter()
        perf.record(
            "ws.serialize.queue_wait",
            (started - queued_at) * 1000.0,
        )
        perf.record(
            f"ws.serialize.queue_wait.type.{event_type}",
            (started - queued_at) * 1000.0,
        )
        text = json.dumps(
            value,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        perf.record(
            "ws.serialize.encode",
            (time.perf_counter() - started) * 1000.0,
        )
        payload_bytes = len(text.encode("utf-8"))
        perf.record_count("ws.serialize.payload_bytes", payload_bytes)
        perf.record_count(
            f"ws.serialize.payload_bytes.type.{event_type}",
            payload_bytes,
        )
        return text

    with _WS_JSON_EXECUTOR_LOCK:
        executor = _WS_JSON_EXECUTOR
    if executor is None:
        raise RuntimeError("WS JSON serializer is shut down")
    return await loop.run_in_executor(
        executor,
        ctx.run,
        _dump,
    )


def shutdown_ws_json_executor() -> None:
    global _WS_JSON_EXECUTOR
    with _WS_JSON_EXECUTOR_LOCK:
        executor = _WS_JSON_EXECUTOR
        _WS_JSON_EXECUTOR = None
    if executor is not None:
        executor.shutdown(wait=False, cancel_futures=True)


def reopen_ws_json_executor() -> None:
    global _WS_JSON_EXECUTOR
    with _WS_JSON_EXECUTOR_LOCK:
        if _WS_JSON_EXECUTOR is None:
            _WS_JSON_EXECUTOR = ThreadPoolExecutor(
                max_workers=2, thread_name_prefix="ws-json",
            )
