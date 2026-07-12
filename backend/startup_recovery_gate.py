from __future__ import annotations

import asyncio
import logging
from typing import Optional

_pending = False
_failed: Optional[str] = None
_ready: Optional[asyncio.Event] = None
_session_ready: dict[str, asyncio.Event] = {}
_priority_sessions: set[str] = set()
_DEFAULT_WAIT_TIMEOUT_SECONDS: float | None = None
_FOREIGN_LOOP_POLL_INTERVAL_SECONDS = 0.05
_log = logging.getLogger(__name__)


def begin_recovery() -> None:
    global _pending, _failed, _ready
    _pending = True
    _failed = None
    _ready = asyncio.Event()
    _session_ready.clear()
    _priority_sessions.clear()


def is_pending() -> bool:
    return _pending


def _signal_ready() -> None:
    """Wake waiters without assuming the caller is on the Event's loop.

    ``asyncio.Event`` binds lazily to the first loop that awaits it. During
    startup recovery that first waiter can be a provisioning worker's private
    loop, while completion is marked from uvicorn's main loop. Calling
    ``Event.set`` directly across loops is not thread-safe (and raises under
    asyncio debug); use the owning loop's thread-safe callback when needed.
    """
    ready = _ready
    if ready is None:
        return
    home = getattr(ready, "_loop", None)
    if home is not None:
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is not home and not home.is_closed():
            home.call_soon_threadsafe(ready.set)
            return
    try:
        ready.set()
    except RuntimeError:
        # Last-resort fail-open wakeup for a stale/closed loop. `_pending` was
        # already cleared by the caller, so polling waiters can still proceed.
        _log.exception("startup recovery gate failed to signal asyncio.Event")


def mark_recovery_done() -> None:
    global _pending
    _pending = False
    for ready in tuple(_session_ready.values()):
        ready.set()
    _session_ready.clear()
    _signal_ready()


def mark_recovery_failed(error: str) -> None:
    global _pending, _failed
    _pending = False
    _failed = error or "unknown error"
    for ready in tuple(_session_ready.values()):
        ready.set()
    _session_ready.clear()
    _signal_ready()


def register_session_recovery(app_session_ids: set[str]) -> None:
    if not _pending:
        return
    for sid in app_session_ids:
        if sid:
            _session_ready.setdefault(sid, asyncio.Event())


def mark_session_recovery_done(app_session_id: str) -> None:
    ready = _session_ready.pop(app_session_id, None)
    if ready is not None:
        ready.set()


def request_session_priority(app_session_id: str) -> None:
    if app_session_id:
        _priority_sessions.add(app_session_id)


def session_priority_rank(app_session_id: str | None) -> int:
    return 0 if app_session_id and app_session_id in _priority_sessions else 1


def _ready_bound_to_running_loop(ready: asyncio.Event) -> bool:
    """True if ``ready`` is unbound or already bound to the running loop.

    The gate ``Event`` is created on the main uvicorn loop. Provisioning's
    ``run_sync`` drives the delegation pipeline on a private loop in a worker
    thread, which then reaches ``wait_for_recovery_ready``; awaiting a
    main-loop-bound ``Event`` from that foreign loop raises ``RuntimeError:
    ... is bound to a different event loop``. Detect that case so we can fall
    back to polling ``_pending``.
    """
    home = getattr(ready, "_loop", None)
    if home is None:
        return True
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        return True
    return home is running


async def _poll_recovery(timeout: float | None) -> None:
    """Wait for recovery by polling ``_pending`` from a foreign event loop.

    Used when the gate ``Event`` is bound to the main loop but the caller is
    running on a different loop (e.g. provisioning.run_sync's private worker
    loop). The main loop clears ``_pending`` via mark_recovery_done() /
    mark_recovery_failed(); the simple bool read is atomic under the GIL and
    the sleep yields so the new value is observed within the poll interval.
    """
    loop = asyncio.get_running_loop()
    deadline = None if timeout is None else loop.time() + float(timeout)
    while _pending:
        if deadline is not None and loop.time() >= deadline:
            _log.warning(
                "startup recovery gate still pending after %.1fs; continuing",
                timeout,
            )
            return
        await asyncio.sleep(_FOREIGN_LOOP_POLL_INTERVAL_SECONDS)


async def wait_for_recovery_ready(timeout: float | None = _DEFAULT_WAIT_TIMEOUT_SECONDS) -> None:
    ready = _ready
    if _pending and ready is not None:
        if _ready_bound_to_running_loop(ready):
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
            except RuntimeError as exc:
                # Defensive: if CPython's loop-bound attribute ever changes
                # shape, still recover instead of crashing the delegation
                # pipeline that reached us from a foreign loop.
                if "bound to a different event loop" not in str(exc):
                    raise
                await _poll_recovery(timeout)
        else:
            await _poll_recovery(timeout)
    if _failed:
        raise RuntimeError(f"startup recovery failed: {_failed}")


async def wait_for_session_recovery_ready(
    app_session_id: str,
    timeout: float | None = _DEFAULT_WAIT_TIMEOUT_SECONDS,
) -> None:
    request_session_priority(app_session_id)
    ready = _session_ready.get(app_session_id)
    if ready is not None:
        try:
            if timeout is None:
                await ready.wait()
            else:
                await asyncio.wait_for(ready.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            _log.warning(
                "session recovery gate still pending after %.1fs for %s; continuing",
                timeout,
                app_session_id,
            )
    elif _pending:
        await wait_for_recovery_ready(timeout)
    if _failed:
        raise RuntimeError(f"startup recovery failed: {_failed}")


def reset_for_tests() -> None:
    global _pending, _failed, _ready
    _pending = False
    _failed = None
    _ready = None
    _session_ready.clear()
    _priority_sessions.clear()
