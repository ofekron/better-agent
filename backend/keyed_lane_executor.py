"""Generic per-(key, lane) FIFO serial executor.

Each (key, lane) pair — e.g. (root_id, "write") — gets its own worker
thread, spun up lazily on the first submit for that pair and torn down
after `idle_timeout` seconds without work, then respawned lazily on the
next submit. Only visited keys ever get a thread; nothing is
pre-allocated per possible key.

Different keys, and different lanes of the same key, never share a
thread or block on each other: there is no lock that spans more than
one (key, lane) pair. Each pair owns its own `threading.Condition`
guarding only its own queue — submitting/dispatching work for one
session can never be delayed by another session's (or another lane's)
backlog. The directory of lanes is a plain dict published through
`dict.setdefault`, which CPython guarantees is atomic, so discovering
or publishing a new (key, lane) entry needs no lock of its own; once
published, a `_Lane` object's identity never changes, only the state
behind its own condition/queue does.

Callers pick the lane per submit (e.g. "write" vs "read") so that
unrelated workloads for the same key never queue behind each other,
while work submitted to the *same* lane for the *same* key still runs
strictly FIFO, one item at a time.
"""
from __future__ import annotations

import concurrent.futures
import threading
from collections import deque
from typing import Callable


class _Lane:
    __slots__ = ("cv", "queue", "worker_alive", "thread", "stopping")

    def __init__(self) -> None:
        self.cv = threading.Condition()
        self.queue: deque[tuple[concurrent.futures.Future, Callable, tuple, dict]] = deque()
        self.worker_alive = False
        self.thread: threading.Thread | None = None
        self.stopping = False


class _LaneExecutorAdapter(concurrent.futures.Executor):
    def __init__(self, owner: "KeyedLaneExecutor", key: str, lane: str) -> None:
        self._owner = owner
        self._key = key
        self._lane = lane

    def submit(self, fn, /, *args, **kwargs):
        return self._owner.submit(self._key, fn, *args, lane=self._lane, **kwargs)


class KeyedLaneExecutor:
    """Thread-per-(key, lane), FIFO within a lane, idle threads self-reap."""

    def __init__(
        self,
        *,
        lanes: tuple[str, ...] = ("default",),
        idle_timeout: float = 30.0,
        thread_name_prefix: str = "kle",
    ) -> None:
        self._lane_names = frozenset(lanes)
        self._idle_timeout = idle_timeout
        self._thread_name_prefix = thread_name_prefix
        self._lanes_by_key: dict[tuple[str, str], _Lane] = {}
        self._adapters: dict[tuple[str, str], _LaneExecutorAdapter] = {}
        self._close_lock = threading.Lock()
        self._closed = False

    def submit(self, key: str, fn, /, *args, lane: str = "default", **kwargs):
        if lane not in self._lane_names:
            raise ValueError(f"unknown lane {lane!r}")
        with self._close_lock:
            if self._closed:
                raise RuntimeError("cannot schedule new futures after shutdown")
        entry = self._lanes_by_key.setdefault((key, lane), _Lane())
        future: concurrent.futures.Future = concurrent.futures.Future()
        with entry.cv:
            entry.queue.append((future, fn, args, kwargs))
            if not entry.worker_alive:
                entry.worker_alive = True
                thread = threading.Thread(
                    target=self._run_lane,
                    args=(key, lane, entry),
                    daemon=True,
                    name=f"{self._thread_name_prefix}-{lane}-{key}",
                )
                entry.thread = thread
                thread.start()
            entry.cv.notify()
        return future

    def executor(self, key: str, *, lane: str = "default") -> concurrent.futures.Executor:
        adapter = self._adapters.setdefault((key, lane), _LaneExecutorAdapter(self, key, lane))
        return adapter

    def _run_lane(self, key: str, lane: str, entry: _Lane) -> None:
        while True:
            with entry.cv:
                while not entry.queue and not entry.stopping:
                    if not entry.cv.wait(timeout=self._idle_timeout):
                        if not entry.queue:
                            entry.worker_alive = False
                            return
                        continue
                if not entry.queue:
                    entry.worker_alive = False
                    return
                work = entry.queue.popleft()
            future, fn, args, kwargs = work
            if future.set_running_or_notify_cancel():
                try:
                    result = fn(*args, **kwargs)
                except BaseException as exc:
                    future.set_exception(exc)
                else:
                    future.set_result(result)

    def shutdown(self, wait: bool = True) -> None:
        # Best-effort: a submit() that is between its closed-check and its
        # entry.cv admission right as shutdown runs can still enqueue and
        # spawn a straggler worker after this method's thread snapshot is
        # taken. That worker still drains its item (its own `stopping`
        # read races the same way, but the queue check after it always
        # wins), it just may finish after `shutdown(wait=True)` returns.
        # Acceptable for a rare, process-lifecycle call on daemon threads.
        with self._close_lock:
            self._closed = True
        threads: list[threading.Thread] = []
        for entry in list(self._lanes_by_key.values()):
            with entry.cv:
                entry.stopping = True
                entry.cv.notify_all()
                if entry.worker_alive and entry.thread is not None:
                    threads.append(entry.thread)
        if wait:
            for thread in threads:
                thread.join()
