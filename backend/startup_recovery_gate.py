from __future__ import annotations

import asyncio
import logging
from typing import Optional

_pending = False
_failed: Optional[str] = None
_ready: Optional[asyncio.Event] = None
_DEFAULT_WAIT_TIMEOUT_SECONDS = 2.0
_log = logging.getLogger(__name__)


def begin_recovery() -> None:
    global _pending, _failed, _ready
    _pending = True
    _failed = None
    _ready = asyncio.Event()


def mark_recovery_done() -> None:
    global _pending
    _pending = False
    if _ready is not None:
        _ready.set()


def mark_recovery_failed(error: str) -> None:
    global _pending, _failed
    _pending = False
    _failed = error or "unknown error"
    if _ready is not None:
        _ready.set()


async def wait_for_recovery_ready(timeout: float | None = _DEFAULT_WAIT_TIMEOUT_SECONDS) -> None:
    ready = _ready
    if _pending and ready is not None:
        try:
            if timeout is None:
                await ready.wait()
            else:
                await asyncio.wait_for(ready.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            _log.warning(
                "startup recovery gate still pending after %.1fs; continuing",
                timeout,
            )
    if _failed:
        raise RuntimeError(f"startup recovery failed: {_failed}")


def reset_for_tests() -> None:
    global _pending, _failed, _ready
    _pending = False
    _failed = None
    _ready = None
