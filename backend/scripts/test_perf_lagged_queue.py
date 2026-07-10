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


if __name__ == "__main__":
    test_lagged_queue_round_trips_dicts_of_any_key_count()
    test_lagged_queue_async_put_does_not_double_wrap()
    test_lagged_queue_get_nowait_strips_stamp()
    print("ok")
