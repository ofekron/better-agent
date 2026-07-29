"""Locks the three cross-thread defects that block driving ingestion
from a thread other than the main uvicorn loop.

Each test fails before its fix and passes after:
  1. session_manager.schedule_on_bound_loop reaches the bound loop from
     a foreign thread instead of dying on `get_running_loop`.
  2. The queued-prompt-count cache survives concurrent mutation while
     another thread iterates it.
  3. event_ingester.close() keeps the per-root lock so a concurrent
     caller cannot enter the critical section on a fresh lock.
"""

import asyncio
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import paths

_TEST_HOME = tempfile.mkdtemp(prefix="offloop-safety-")
paths.engage_test_home(_TEST_HOME)

import session_manager as session_manager_mod  # noqa: E402
import event_ingester as event_ingester_mod  # noqa: E402

manager = session_manager_mod.manager


def _run_loop_in_thread():
    loop = asyncio.new_event_loop()
    ready = threading.Event()

    def _main():
        asyncio.set_event_loop(loop)
        loop.call_soon(ready.set)
        loop.run_forever()

    thread = threading.Thread(target=_main, daemon=True)
    thread.start()
    ready.wait()
    return loop, thread


def _stop_loop(loop, thread):
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=5)
    loop.close()


def test_schedule_on_bound_loop_from_foreign_thread() -> None:
    loop, thread = _run_loop_in_thread()
    previous = manager._loop
    try:
        manager.bind_loop(loop)
        ran_on = []
        done = threading.Event()

        async def work():
            ran_on.append(asyncio.get_running_loop())
            done.set()

        # Call from a plain worker thread with no loop of its own —
        # this is what apply_event looks like during recovery replay.
        result = {}

        def caller():
            result["ok"] = manager.schedule_on_bound_loop(work())

        worker = threading.Thread(target=caller)
        worker.start()
        worker.join(timeout=5)

        assert result["ok"] is True
        assert done.wait(timeout=5)
        assert ran_on == [loop]
    finally:
        manager.bind_loop(previous) if previous else None
        manager._loop = previous
        _stop_loop(loop, thread)


def test_schedule_on_bound_loop_reports_failure_without_a_loop() -> None:
    previous = manager._loop
    try:
        manager._loop = None

        async def work():
            return None

        assert manager.schedule_on_bound_loop(work()) is False
    finally:
        manager._loop = previous


def test_queued_prompt_counts_survive_concurrent_mutation() -> None:
    manager._queued_prompt_counts_by_sid.clear()
    for i in range(200):
        manager._set_queued_prompt_count(f"seed{i}", 1)

    stop = threading.Event()
    errors: list[BaseException] = []

    def mutate():
        i = 0
        while not stop.is_set():
            sid = f"churn{i % 500}"
            manager._set_queued_prompt_count(sid, 1)
            manager._set_queued_prompt_count(sid, 0)
            i += 1

    def read():
        try:
            while not stop.is_set():
                manager.has_any_queued_prompts()
                manager.queued_prompt_count("seed0")
        except BaseException as exc:  # noqa: BLE001 - recording the race
            errors.append(exc)

    threads = [threading.Thread(target=mutate) for _ in range(2)]
    threads += [threading.Thread(target=read) for _ in range(2)]
    for t in threads:
        t.start()
    threading.Event().wait(1.0)
    stop.set()
    for t in threads:
        t.join(timeout=5)

    manager._queued_prompt_counts_by_sid.clear()
    assert not errors, f"concurrent access raised: {errors[0]!r}"


def test_close_keeps_the_per_root_lock() -> None:
    ingester = event_ingester_mod.EventIngester()
    root_id = "root-under-close"
    lock = ingester._locks.setdefault(root_id, threading.Lock())

    ingester.close(root_id)

    # Popping the lock would let a concurrent caller create a second one
    # and enter the critical section during teardown.
    assert ingester._locks.get(root_id) is lock


if __name__ == "__main__":
    import shutil

    try:
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                fn()
                print(f"ok {name}")
        print("PASS")
    finally:
        shutil.rmtree(_TEST_HOME, ignore_errors=True)
