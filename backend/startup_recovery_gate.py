"""Readiness of startup recovery, awaitable from any event loop.

The gate answers one question: has recovery finished, globally or for a
particular session? It owns readiness and nothing else — ordering lives
in `recovery_schedule`, execution in `recovery_manager`.

Waiters are not all on the main loop. Provisioning's `run_sync` drives
delegation on a private loop in a worker thread and reaches
`wait_for_recovery_ready` from there, so readiness is held in
`AsyncFact`, which any loop can await, rather than in a loop-bound
`asyncio.Event`.
"""

from __future__ import annotations

import logging
from typing import Optional

import recovery_schedule
from async_fact import AsyncFact

_pending = False
_failed: Optional[str] = None
_ready = AsyncFact("startup_recovery")
_session_ready: dict[str, AsyncFact] = {}
_DEFAULT_WAIT_TIMEOUT_SECONDS: float | None = None
_log = logging.getLogger(__name__)


def begin_recovery() -> None:
    global _pending, _failed
    _pending = True
    _failed = None
    _ready.clear()
    _session_ready.clear()
    recovery_schedule.reset_for_tests()


def is_pending() -> bool:
    return _pending


def is_session_pending(app_session_id: str) -> bool:
    return app_session_id in _session_ready


def mark_recovery_done() -> None:
    global _pending
    _pending = False
    _ready.set()


def mark_recovery_failed(error: str) -> None:
    global _pending, _failed
    _pending = False
    _failed = error or "unknown error"
    _release_all()


def _release_all() -> None:
    for fact in tuple(_session_ready.values()):
        fact.set()
    _session_ready.clear()
    _ready.set()


def register_session_recovery(app_session_ids: set[str]) -> None:
    if not _pending:
        return
    for sid in app_session_ids:
        if sid:
            _session_ready.setdefault(sid, AsyncFact(f"recovery:{sid}"))


def mark_session_recovery_done(app_session_id: str) -> None:
    fact = _session_ready.pop(app_session_id, None)
    if fact is not None:
        fact.set()


def request_session_priority(app_session_id: str) -> None:
    """Promote a session the user just opened to the front of recovery.

    Ordering belongs to `recovery_schedule`; this gate owns readiness
    only. Kept as a thin forward because waiting for a session is the
    natural place to learn the user wants it — callers reach the gate,
    not the schedule.
    """
    recovery_schedule.boost(app_session_id)


async def wait_for_recovery_ready(
    timeout: float | None = _DEFAULT_WAIT_TIMEOUT_SECONDS,
) -> None:
    if _pending and not await _ready.wait(timeout):
        _log.warning(
            "startup recovery gate still pending after %.1fs; continuing",
            timeout,
        )
    if _failed:
        raise RuntimeError(f"startup recovery failed: {_failed}")


async def wait_for_session_recovery_ready(
    app_session_id: str,
    timeout: float | None = _DEFAULT_WAIT_TIMEOUT_SECONDS,
) -> None:
    request_session_priority(app_session_id)
    fact = _session_ready.get(app_session_id)
    if fact is None:
        if _pending:
            await wait_for_recovery_ready(timeout)
        if _failed:
            raise RuntimeError(f"startup recovery failed: {_failed}")
        return
    if not await fact.wait(timeout):
        _log.warning(
            "session recovery gate still pending after %.1fs for %s; continuing",
            timeout,
            app_session_id,
        )
    if _failed:
        raise RuntimeError(f"startup recovery failed: {_failed}")


def reset_for_tests() -> None:
    global _pending, _failed
    _pending = False
    _failed = None
    _ready.clear()
    _session_ready.clear()
    recovery_schedule.reset_for_tests()
