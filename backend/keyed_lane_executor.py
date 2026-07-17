"""Generic per-(key, lane) FIFO serial executor.

Each (key, lane) pair — e.g. (root_id, "write") — gets its own worker
thread, spun up lazily on the first submit for that pair and torn down
after `idle_timeout` seconds without work, then respawned lazily on the
next submit. Only visited keys ever get a thread; nothing is
pre-allocated per possible key.

Different keys, and different lanes of the same key, never share a
thread and never block on each other's *work*: the actual submitted
callable always runs with no lock held. Bookkeeping (enqueue, dequeue,
spawn/teardown decisions, the closed flag) shares one small mutex
across every (key, lane) pair, the same way `queue.Queue` protects its
deque — cheap O(1) operations, never held across a blocking call or a
submitted callable. Every `_Lane`'s `Condition` wraps that same mutex,
so a lane blocked in `cv.wait()` releases it for every other lane and
for `shutdown()`/`submit()` to proceed; only actual work execution is
lock-free and fully isolated per (key, lane).

Sharing one mutex for bookkeeping (rather than per-lane locks with an
ad hoc "closed" admission race) makes shutdown and idle-teardown exact
instead of best-effort: `shutdown()` and `submit()` can never
interleave around the closed check, and a lane whose queue just
emptied can safely remove itself from the directory in the same
critical section that decided to die, so the directory never grows
unbounded for keys that go idle.

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

    def __init__(self, directory_lock: threading.Lock) -> None:
        self.cv = threading.Condition(directory_lock)
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
        self._directory_lock = threading.Lock()
        self._lanes_by_key: dict[tuple[str, str], _Lane] = {}
        self._adapters: dict[tuple[str, str], _LaneExecutorAdapter] = {}
        self._closed = False

    def submit(self, key: str, fn, /, *args, lane: str = "default", **kwargs):
        if lane not in self._lane_names:
            raise ValueError(f"unknown lane {lane!r}")
        future: concurrent.futures.Future = concurrent.futures.Future()
        with self._directory_lock:
            if self._closed:
                raise RuntimeError("cannot schedule new futures after shutdown")
            entry = self._lanes_by_key.get((key, lane))
            if entry is None:
                entry = _Lane(self._directory_lock)
                self._lanes_by_key[(key, lane)] = entry
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
        with self._directory_lock:
            adapter = self._adapters.get((key, lane))
            if adapter is None:
                adapter = _LaneExecutorAdapter(self, key, lane)
                self._adapters[(key, lane)] = adapter
            return adapter

    def pending_count(self) -> int:
        """Queued-plus-running items across every (key, lane) pair."""
        with self._directory_lock:
            return sum(
                len(entry.queue) + (1 if entry.worker_alive else 0)
                for entry in self._lanes_by_key.values()
            )

    def active_lanes_count(self) -> int:
        """Number of (key, lane) pairs with a live worker thread right now."""
        with self._directory_lock:
            return sum(1 for entry in self._lanes_by_key.values() if entry.worker_alive)

    def _run_lane(self, key: str, lane: str, entry: _Lane) -> None:
        while True:
            with entry.cv:
                while not entry.queue and not entry.stopping:
                    if not entry.cv.wait(timeout=self._idle_timeout):
                        if not entry.queue:
                            entry.worker_alive = False
                            self._lanes_by_key.pop((key, lane), None)
                            self._adapters.pop((key, lane), None)
                            return
                        continue
                if not entry.queue:
                    entry.worker_alive = False
                    self._lanes_by_key.pop((key, lane), None)
                    self._adapters.pop((key, lane), None)
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
        with self._directory_lock:
            self._closed = True
            threads: list[threading.Thread] = []
            for entry in self._lanes_by_key.values():
                entry.stopping = True
                entry.cv.notify_all()
                if entry.worker_alive and entry.thread is not None:
                    threads.append(entry.thread)
        if wait:
            for thread in threads:
                thread.join()
