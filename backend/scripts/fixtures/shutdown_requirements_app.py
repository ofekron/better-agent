from __future__ import annotations

import hashlib
import os
from pathlib import Path

from requirements_query_runner import (
    REQUIREMENTS_SEARCH_EXECUTOR,
    run_requirements_query,
)


def _burn_cpu(*, _cancel_event) -> None:
    started = Path(os.environ["SHUTDOWN_TEST_STARTED"])
    stopped = Path(os.environ["SHUTDOWN_TEST_STOPPED"])
    started.touch()
    value = b"requirements-shutdown"
    while not _cancel_event.is_set():
        value = hashlib.sha256(value).digest()
    stopped.touch()


async def app(scope, receive, send) -> None:
    if scope["type"] == "lifespan":
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return

    if scope["path"] == "/work":
        await run_requirements_query(
            "requirements.shutdown_test",
            _burn_cpu,
            executor=REQUIREMENTS_SEARCH_EXECUTOR,
            cancellation_kwarg="_cancel_event",
        )
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send({"type": "http.response.body", "body": b'{"ok":true}'})
