from __future__ import annotations

import asyncio
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import perf  # noqa: E402


def _lag_count(name: str) -> int:
    # perf._stats is module-private; read it under its lock to be safe.
    with perf._lock:
        return perf._stats.get(f"queue.lag.{name}", {}).get("n", 0)


def test_lagged_queue_round_trips_dicts_of_any_key_count():
    """A LaggedQueue must return the exact item put and record one lag per
    dequeue, regardless of the item's shape.

    Regression: LaggedQueue previously overrode the public get/put/put_nowait/
    get_nowait. Because asyncio.Queue.get() delegates to self.get_nowait() and
    asyncio.Queue.put() to self.put_nowait(), `super().get()` returned the
    already-unwrapped bare item; then `stamp, item = <item>` unpacked the dict's
    keys. A 2-key dict made `stamp` a str -> `time.perf_counter() - str` ->
    TypeError, crashing the WS outbox writer on every /ws/chat connection.
    """
    async def run():
        q = perf.LaggedQueue(maxsize=16, _perf_name="rl")
        cases = [
            {},
            {"a": 1},
            {"type": "x", "data": "y"},            # 2-key: the crash case
            {"type": "x", "data": "y", "more": "z"},
        ]
        for item in cases:
            q.put_nowait(item)
        for expected in cases:
            got = await q.get()
            assert got == expected, f"{got!r} != {expected!r}"
        # every dequeue recorded exactly one lag sample
        assert _lag_count("rl") == len(cases)

    asyncio.run(run())


def test_lagged_queue_async_put_does_not_double_wrap():
    """async put + get must round-trip intact (no double (stamp,(stamp,item)))."""
    async def run():
        q = perf.LaggedQueue(maxsize=4, _perf_name="async")
        await q.put({"k": "v"})
        got = await q.get()
        assert got == {"k": "v"}

    asyncio.run(run())


def test_lagged_queue_get_nowait_strips_stamp():
    q = perf.LaggedQueue(maxsize=4, _perf_name="nowait")
    q.put_nowait({"type": "ping", "v": 1})
    got = q.get_nowait()
    assert got == {"type": "ping", "v": 1}
    assert _lag_count("nowait") == 1


def test_rollup_loop_runs_flush_off_the_event_loop():
    """Regression: `_rollup_loop` calling `flush()` directly on the event
    loop lets ANY registered gauge (e.g. lag_incident_queue.parked_depth
    scanning an unbounded spool dir -- 1848 files and growing in prod, per
    faulthandler dump at 2026-07-15T22:23:53, `_count_spool` still O(N) CPU)
    stall every concurrent session/websocket/request for the gauge's
    duration. `_rollup_loop` must invoke `flush()` via `asyncio.to_thread` so
    a slow gauge only blocks a worker thread, never the loop.

    Drives the REAL `_rollup_loop` (with ROLLUP_SECS patched small) rather
    than calling `asyncio.to_thread(flush)` directly in the test -- a test
    that wraps flush() itself would pass trivially regardless of what
    `_rollup_loop`'s own source does.
    """
    import time

    def slow_gauge() -> int:
        time.sleep(0.3)
        return 42

    perf.register_queue("bc_test_slow_gauge", slow_gauge)
    original_rollup_secs = perf.ROLLUP_SECS
    perf.ROLLUP_SECS = 0.01
    try:
        async def run() -> tuple[int, int]:
            heartbeat_ticks = 0
            stop = False

            async def heartbeat() -> None:
                nonlocal heartbeat_ticks
                while not stop:
                    await asyncio.sleep(0.02)
                    heartbeat_ticks += 1

            hb_task = asyncio.create_task(heartbeat())
            rollup_task = asyncio.create_task(perf._rollup_loop())
            # One rollup fires almost immediately (ROLLUP_SECS=0.01) and
            # blocks on the slow gauge for ~0.3s; give it 0.5s total.
            await asyncio.sleep(0.5)
            rollup_task.cancel()
            try:
                await rollup_task
            except asyncio.CancelledError:
                pass
            stop = True
            await hb_task
            return heartbeat_ticks

        ticks = asyncio.run(run())
        # ~0.5s / 20ms = ~25 ticks if the loop stayed responsive throughout
        # the slow-gauge rollup. If flush() ran synchronously on the loop,
        # the heartbeat would starve for the full 300ms gauge read.
        assert ticks >= 15, (
            f"event loop starved during a rollup with a slow gauge: only "
            f"{ticks} heartbeat ticks in 0.5s (expected the loop to stay "
            f"responsive throughout)"
        )
    finally:
        perf.ROLLUP_SECS = original_rollup_secs
        perf.unregister_queue("bc_test_slow_gauge")


if __name__ == "__main__":
    test_lagged_queue_round_trips_dicts_of_any_key_count()
    test_lagged_queue_async_put_does_not_double_wrap()
    test_lagged_queue_get_nowait_strips_stamp()
    test_rollup_loop_runs_flush_off_the_event_loop()
    print("ok")
