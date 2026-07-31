"""Regression test: `launch_task` must renew the singleton routine-launch
coordination lock while `_launch_task_once` is still in flight, not just
acquire it once and release it at the end.

Iteration-63 finding: the lock is acquired with a fixed 15-minute lease
(`lease_seconds=15*60`), but a real launch's warm_base/bundle-capture step
observed in production took ~50 minutes. With no renewal, the lease
silently expires mid-launch — the release at the end then fails (lock
already gone), and for the whole gap between expiry and actual
completion a second concurrent launch of the SAME singleton routine
could acquire the now-empty lock and start a duplicate run, defeating
the exact race the lock exists to prevent.

Fix: renew the lock periodically (every 5 minutes) for the duration of
`_launch_task_once`, cancelling the renewal loop in the `finally` block
before releasing.

This test fakes a long-running launch (blocked on an event) and a
fast `asyncio.sleep` so the 5-minute renewal cadence collapses to a few
event-loop ticks, then asserts at least one renewal happened while the
launch was still in flight.

Run with:
    cd backend && .venv/bin/python scripts/test_task_runner_launch_lock_renewal.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _test_home
_test_home.isolate(prefix="task-runner-lock-renew-")

import coordination
import routine_lock
import task_runner
from stores import task_store


def test_launch_task_renews_lock_before_long_launch_completes() -> None:
    task = task_store.create(
        cwd="/repo",
        name="slow-singleton",
        prompt="run slowly",
        model="claude-sonnet-5",
        provider_id="anthropic",
        singleton=True,
    )

    renew_calls: list[dict] = []
    real_lock_ops = coordination.lock_ops

    async def spy_lock_ops(**kwargs):
        if kwargs.get("renew"):
            renew_calls.append(kwargs)
        return await real_lock_ops(**kwargs)

    original_sleep = asyncio.sleep
    sleep_calls: list[float] = []

    async def fast_sleep(delay, *args, **kwargs):
        sleep_calls.append(delay)
        return await original_sleep(0)

    launch_started = asyncio.Event()
    release_launch = asyncio.Event()

    async def slow_launch_once(task_id, **_kwargs):
        launch_started.set()
        await release_launch.wait()
        return {"session_id": "fake-session", "queue_item_id": "fake-item"}

    original_lock_ops = coordination.lock_ops
    original_launch_once = task_runner._launch_task_once
    original_acquire = routine_lock.acquire
    original_release = routine_lock.release

    coordination.lock_ops = spy_lock_ops
    task_runner._launch_task_once = slow_launch_once
    asyncio.sleep = fast_sleep
    routine_lock.acquire = lambda *_a, **_kw: object()
    routine_lock.release = lambda *_a, **_kw: None

    async def scenario() -> dict:
        launch_task_coro = asyncio.create_task(
            task_runner.launch_task(task["id"], coordinator=None, source="trigger")
        )
        await asyncio.wait_for(launch_started.wait(), timeout=5)
        # Give the renewal loop's (now-fast) sleep+renew cycle a few
        # ticks to run at least once while the launch is still in flight.
        for _ in range(20):
            await original_sleep(0)
        assert renew_calls, "lock was never renewed while the launch was still running"
        assert not launch_task_coro.done(), "launch finished before we could observe renewal"
        release_launch.set()
        return await launch_task_coro

    try:
        result = asyncio.run(scenario())
    finally:
        coordination.lock_ops = original_lock_ops
        task_runner._launch_task_once = original_launch_once
        asyncio.sleep = original_sleep
        routine_lock.acquire = original_acquire
        routine_lock.release = original_release

    assert result == {"session_id": "fake-session", "queue_item_id": "fake-item"}
    assert sleep_calls, "expected the renewal loop to have called asyncio.sleep"


if __name__ == "__main__":
    test_launch_task_renews_lock_before_long_launch_completes()
    print("PASS launch_task renews the singleton lock during a long launch")
