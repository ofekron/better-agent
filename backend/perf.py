"""Performance instrumentation primitive — single source.

INVARIANT: only this module touches the perf logger. All call-sites use
`timed(name)` / `timed_fn(name)` / `register_queue(name, getter)` /
`stamp_enq()` + `record_lag(name, t)`. The background `_rollup_loop`
task (started exactly once from main.py startup) flushes an
aggregated `PERF rollup` line every ROLLUP_SECS seconds.

INVARIANT: stats accumulation holds `_lock` for O(1) dict updates only.
Disk I/O and network ops dominate by 4+ orders of magnitude — the lock
is acceptable noise floor.

INVARIANT: `_rollup_task` is held at module scope so the asyncio task
isn't garbage-collected after startup returns.
"""

import asyncio
import logging
import threading
import time
from collections import defaultdict
from contextlib import contextmanager
from functools import wraps
from typing import Callable, Optional

logger = logging.getLogger("perf")

ROLLUP_SECS = 60.0

_lock = threading.Lock()
_stats: dict[str, dict] = defaultdict(lambda: {"n": 0, "ms_sum": 0.0, "ms_max": 0.0})
_counts: dict[str, dict] = defaultdict(lambda: {"n": 0, "total": 0, "max": 0})
_queue_gauges: dict[str, Callable[[], int]] = {}

_rollup_task: Optional[asyncio.Task] = None


def record(name: str, ms: float) -> None:
    with _lock:
        s = _stats[name]
        s["n"] += 1
        s["ms_sum"] += ms
        if ms > s["ms_max"]:
            s["ms_max"] = ms


def record_count(name: str, value: int = 1) -> None:
    with _lock:
        counter = _counts[name]
        counter["n"] += 1
        counter["total"] += value
        if value > counter["max"]:
            counter["max"] = value


@contextmanager
def timed(name: str):
    t = time.perf_counter()
    try:
        yield
    finally:
        record(name, (time.perf_counter() - t) * 1000.0)


def timed_fn(name: str):
    """Decorator. Works on sync and async functions."""
    def deco(fn):
        if asyncio.iscoroutinefunction(fn):
            @wraps(fn)
            async def aw(*a, **kw):
                t = time.perf_counter()
                try:
                    return await fn(*a, **kw)
                finally:
                    record(name, (time.perf_counter() - t) * 1000.0)
            return aw

        @wraps(fn)
        def w(*a, **kw):
            t = time.perf_counter()
            try:
                return fn(*a, **kw)
            finally:
                record(name, (time.perf_counter() - t) * 1000.0)
        return w
    return deco


def register_queue(name: str, getter: Callable[[], int]) -> None:
    """Register a depth-gauge for a queue. `getter` is called on each rollup."""
    _queue_gauges[name] = getter


def unregister_queue(name: str) -> None:
    _queue_gauges.pop(name, None)


def stamp_enq() -> float:
    """Stamp a timestamp on enqueue. Pair with `record_lag` on dequeue."""
    return time.perf_counter()


def record_lag(name: str, enq_t: float) -> None:
    record(f"queue.lag.{name}", (time.perf_counter() - enq_t) * 1000.0)


class LaggedQueue(asyncio.Queue):
    """asyncio.Queue that records enqueue→dequeue lag under `name`.

    INVARIANT: drop-in compatible with `asyncio.Queue`. Wraps items in
    `(stamp, item)` tuples internally; `get()` strips the stamp and
    records lag. Callers see the same put/get signatures.
    """

    def __init__(self, *args, _perf_name: str, **kwargs):
        super().__init__(*args, **kwargs)
        self._perf_name = _perf_name

    # Override the low-level _put/_get hooks, NOT the public put/get/
    # put_nowait/get_nowait: asyncio.Queue.get() delegates to self.get_nowait()
    # and asyncio.Queue.put() to self.put_nowait(), so overriding the public
    # methods double-processes (super().get() returns the already-unwrapped
    # bare item, then `stamp, item = <item>` mis-parses it). _put/_get are the
    # single internal choke point every code path funnels through.
    def _put(self, item):
        super()._put((time.perf_counter(), item))

    def _get(self):
        stamp, item = super()._get()
        record_lag(self._perf_name, stamp)
        return item


def flush() -> None:
    """Snapshot + reset stats, then emit one aggregated log line."""
    with _lock:
        snap = {k: dict(v) for k, v in _stats.items()}
        count_snap = {k: dict(v) for k, v in _counts.items()}
        _stats.clear()
        _counts.clear()

    depth_lines: list[str] = []
    for qname in sorted(_queue_gauges):
        try:
            depth = _queue_gauges[qname]()
        except Exception:
            continue
        depth_lines.append(f"  q.{qname} depth={depth}")

    if not snap and not count_snap and not depth_lines:
        return

    op_lines: list[str] = []
    for k in sorted(snap):
        s = snap[k]
        n = s["n"]
        total = s["ms_sum"]
        mx = s["ms_max"]
        avg = total / n if n else 0.0
        op_lines.append(
            f"  {k} n={n} avg={avg:.2f}ms max={mx:.2f}ms total={total:.1f}ms"
        )
    for k in sorted(count_snap):
        counter = count_snap[k]
        op_lines.append(
            f"  {k} samples={counter['n']} count_total={counter['total']} "
            f"count_max={counter['max']}"
        )

    body = "\n".join(op_lines + depth_lines)
    logger.info("PERF rollup:\n%s", body)


async def _rollup_loop() -> None:
    while True:
        try:
            await asyncio.sleep(ROLLUP_SECS)
            # flush() is pure-sync and reads every registered gauge, including
            # ones that do filesystem I/O (e.g. lag_incident_queue.parked_depth
            # scanning an unbounded spool dir) -- run it off the event loop so
            # an expensive/slow gauge can never stall every concurrent
            # session/websocket/request for the rollup's duration.
            await asyncio.to_thread(flush)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("perf rollup failed")


def start_rollup_task() -> None:
    """Called once from FastAPI startup. Idempotent."""
    global _rollup_task
    if _rollup_task is not None and not _rollup_task.done():
        return
    _rollup_task = asyncio.create_task(_rollup_loop())
