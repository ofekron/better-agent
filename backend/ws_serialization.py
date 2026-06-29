from __future__ import annotations

import asyncio
import contextvars
import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any

_WS_JSON_EXECUTOR = ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix="ws-json",
)


async def dumps_ws_json(value: Any) -> str:
    ctx = contextvars.copy_context()
    loop = asyncio.get_running_loop()

    def _dump() -> str:
        return json.dumps(
            value,
            separators=(",", ":"),
            ensure_ascii=False,
        )

    return await loop.run_in_executor(
        _WS_JSON_EXECUTOR,
        ctx.run,
        _dump,
    )


def shutdown_ws_json_executor() -> None:
    _WS_JSON_EXECUTOR.shutdown(wait=False, cancel_futures=True)
