"""Generic registry for long-running backend startup work.

INVARIANT: `on_startup` returns to uvicorn within milliseconds. Every
step that can take > ~50ms (migrations, recovery scans, replays) runs
as a named background task tracked here. Each registration broadcasts
`startup_task_changed` so any connected frontend renders a
non-blocking banner without polling. Authoritative state lives in this
in-memory registry; REST `GET /api/startup_tasks` returns the
snapshot, WS pushes deltas.

The registry deliberately lives in memory, not on disk: a backend
restart re-scans the world anyway, so persisting prior task state
would just confuse the frontend banner with stale entries. `reset()`
is called at the top of `on_startup` to wipe any duplicates left by a
uvicorn `--reload` cycle.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

# Frontend banner clears `done` tasks after a short fade; the backend
# keeps a small history so a tab opening mid-startup still sees the
# tail. 20 is enough for the 4 named startup tasks plus headroom for
# future additions; older entries roll off FIFO.
_DONE_HISTORY_CAP = 20


@dataclass
class StartupTask:
    id: str
    label: str
    # `running` is the initial state — tasks are only registered the
    # moment work starts, so `pending` would just produce a no-op WS
    # frame between register and the first mark_running. Skipped.
    state: str  # "running" | "done" | "failed"
    started_at: str
    finished_at: Optional[str] = None
    error: Optional[str] = None


class StartupTaskRegistry:
    """In-memory registry of backend startup tasks. Mutations are
    serialized by `self._lock` (a plain `threading.Lock`) so that the
    loop thread (`register`, `mark_done` from `run_task` after
    `to_thread` returns) and worker threads (any code that calls
    `mark_done` from inside `to_thread`) can't race on the underlying
    OrderedDict. Broadcasts are dispatched onto the bound event loop —
    worker-thread callers go through `run_coroutine_threadsafe`.
    """

    def __init__(self) -> None:
        # OrderedDict so `list()` returns insertion order (matches the
        # banner reading top-down). `self._lock` guards every read AND
        # write — CPython's OrderedDict is not safe under concurrent
        # iteration + pop (RuntimeError "dictionary changed size during
        # iteration"), and the eviction path inside `mark_*` pops while
        # `list()` may be iterating from the REST handler.
        self._tasks: "OrderedDict[str, StartupTask]" = OrderedDict()
        self._lock = threading.Lock()
        # Coordinator + loop are bound at backend startup; until then
        # broadcasts are dropped (the registry can still record state,
        # which is what `GET /api/startup_tasks` reads on first paint).
        self._coordinator = None  # type: Any
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    # -- Wiring ---------------------------------------------------------

    def bind(self, coordinator: Any, loop: asyncio.AbstractEventLoop) -> None:
        self._coordinator = coordinator
        self._loop = loop

    def bound_loop(self) -> Optional[asyncio.AbstractEventLoop]:
        """The runtime's event loop, for threads that must marshal
        coordinator calls onto it. None until `bind` runs."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return None
        return loop

    # -- Mutations ------------------------------------------------------

    def reset(self) -> None:
        """Clear all entries and broadcast a `cleared` ping so any
        already-connected frontend tab drops its local map. Called at
        the start of `on_startup` so a uvicorn `--reload` doesn't
        leave stale done-state entries piling up across reloads."""
        with self._lock:
            self._tasks.clear()
        self._broadcast({"cleared": True})

    def register(self, task_id: str, label: str) -> StartupTask:
        """Insert a fresh task in `running` state. If the same id is
        registered twice (unexpected — caller bug), the prior entry is
        overwritten so the frontend sees a clean restart."""
        task = StartupTask(
            id=task_id,
            label=label,
            state="running",
            started_at=datetime.now().isoformat(),
        )
        with self._lock:
            self._tasks[task_id] = task
            snapshot = asdict(task)
        self._broadcast({"task": snapshot})
        return task

    def mark_done(self, task_id: str) -> None:
        """Mark `done` and emit one WS frame. No-op if the id was
        evicted (e.g. survived a `reset()` from the loop side while a
        worker thread was still mid-task) — keeps post-reset callers
        safe."""
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return
            task.state = "done"
            task.finished_at = datetime.now().isoformat()
            snapshot = asdict(task)
            self._evict_old_done_locked()
        self._broadcast({"task": snapshot})

    def mark_failed(self, task_id: str, error: str) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return
            task.state = "failed"
            task.finished_at = datetime.now().isoformat()
            task.error = error
            snapshot = asdict(task)
            self._evict_old_done_locked()
        self._broadcast({"task": snapshot})

    # -- Reads ----------------------------------------------------------

    def list(self) -> list[dict]:
        # Hold the lock for the duration of the snapshot copy so a
        # concurrent `mark_*` can't pop mid-iteration.
        with self._lock:
            return [asdict(t) for t in self._tasks.values()]


    # -- Internals ------------------------------------------------------

    def _evict_old_done_locked(self) -> None:
        """Cap the count of `done`/`failed` entries so a long-running
        backend that reloads many times doesn't accumulate forever.
        Active (`running`) tasks are always retained. Caller MUST hold
        `self._lock`."""
        done_ids = [tid for tid, t in self._tasks.items() if t.state != "running"]
        excess = len(done_ids) - _DONE_HISTORY_CAP
        if excess <= 0:
            return
        for tid in done_ids[:excess]:
            self._tasks.pop(tid, None)

    def _broadcast(self, data: dict) -> None:
        """Schedule a `startup_task_changed` WS broadcast. Same
        cross-thread idea as `SessionWSBroadcaster._dispatch`: prefer
        the running loop (loop-side caller), fall back to the bound
        loop via `run_coroutine_threadsafe` (worker-thread caller).
        Each branch is exclusive — the coro is scheduled at most once,
        and closed otherwise. Silently drops if no loop is bound (e.g.
        early construction before `bind`)."""
        if self._coordinator is None:
            return
        coro = self._coordinator.broadcast_global("startup_task_changed", data)
        # Branch 1: there IS a running loop in this thread → schedule
        # inline and return. If `create_task` itself raises (loop is
        # closing mid-call), close the coro — falling through to the
        # bound-loop branch would re-schedule the same (already-
        # consumed) coroutine and raise "cannot reuse already awaited
        # coroutine".
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            try:
                loop.create_task(coro)
                return
            except Exception:
                logger.exception("startup_task broadcast: create_task failed")
                coro.close()
                return
        # Branch 2: no running loop in this thread → ship to the bound
        # loop. Same close-on-failure invariant.
        if self._loop is not None and not self._loop.is_closed():
            try:
                asyncio.run_coroutine_threadsafe(coro, self._loop)
                return
            except Exception:
                logger.exception("startup_task broadcast schedule failed")
        coro.close()


# Singleton — imported by main.py + run_recovery + any future startup
# step. Bound at on_startup.
startup_task_registry = StartupTaskRegistry()


async def run_task(
    task_id: str,
    label: str,
    fn: Callable[..., Any],
    *args: Any,
    in_thread: bool = True,
    **kwargs: Any,
) -> None:
    """Register, execute, and mark a task done/failed. `fn` may be
    sync or async — if sync and `in_thread=True` (default), it's
    offloaded to a worker via `asyncio.to_thread` so it doesn't starve
    the event loop. Async `fn` is awaited directly; pass
    `in_thread=False` to await a sync `fn` inline (rare; only when the
    work is truly µs-fast or already runs in a thread internally).

    Returning gracefully on exception is the contract: a failed
    startup task must not crash the gather/wait_for that scheduled
    it. The error is captured on the task and surfaced via WS."""
    startup_task_registry.register(task_id, label)
    try:
        if asyncio.iscoroutinefunction(fn):
            result: Any = await fn(*args, **kwargs)  # type: ignore[func-returns-value]
        elif in_thread:
            result = await asyncio.to_thread(fn, *args, **kwargs)
        else:
            result = fn(*args, **kwargs)
        # Some startup helpers return a payload the caller chains on
        # (e.g. `recover_all_in_flight` returns the recovered-descs
        # list). `run_task` ignores it — chaining lives in the
        # composite-task wrapper below.
        _ = result
        startup_task_registry.mark_done(task_id)
    except Exception as exc:
        logger.exception("startup task %s failed", task_id)
        startup_task_registry.mark_failed(task_id, str(exc))


async def run_composite_task(
    task_id: str,
    label: str,
    body: Callable[[], Awaitable[None]],
) -> None:
    """Variant for tasks whose body needs multiple awaits (e.g. a
    sync scan followed by an async integration). `body` is a
    zero-arg async callable that contains the full work; on exception
    the task is marked failed."""
    startup_task_registry.register(task_id, label)
    try:
        await body()
        startup_task_registry.mark_done(task_id)
    except Exception as exc:
        logger.exception("startup task %s failed", task_id)
        startup_task_registry.mark_failed(task_id, str(exc))
