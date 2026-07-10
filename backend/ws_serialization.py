from __future__ import annotations

import asyncio
import contextvars
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

_WS_JSON_EXECUTOR: ThreadPoolExecutor | None = ThreadPoolExecutor(
    max_workers=2, thread_name_prefix="ws-json",
)
_WS_JSON_EXECUTOR_LOCK = threading.Lock()


async def dumps_ws_json(value: Any) -> str:
    ctx = contextvars.copy_context()
    loop = asyncio.get_running_loop()

    def _dump() -> str:
        return json.dumps(
            value,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )

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
