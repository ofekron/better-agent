"""Regression test: a race-helper block must not leak child tasks when
its owning coroutine is cancelled.

`_race_readline` (and the structurally identical `asyncio.wait` race
blocks across the backend) create two child tasks and race them with
`asyncio.wait`. If the owning coroutine is cancelled while parked on
`asyncio.wait`, the loser-cancel cleanup must still run — otherwise the
child `create_task(event.wait())` task is orphaned, GC'd while pending,
and logged as "Task was destroyed but it is pending!".

This test parks `_race_readline`, cancels its task, and asserts neither
child task is left pending.

Run with:
    cd backend && .venv/bin/python scripts/test_race_block_no_leak.py
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-raceleak-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from jsonl_tailer import _race_readline  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


async def test_cancelled_race_readline_leaks_no_child_task() -> bool:
    """Park `_race_readline` on `asyncio.wait`, cancel its task, and
    assert neither child task (`stream.readline()` / `stop_event.wait()`)
    is left pending. A correctly-cancelled child reaches `done` within a
    couple of ticks; a leaked child stays pending forever because its
    source never produces."""
    stream = asyncio.StreamReader()   # never fed → readline() blocks
    stop_event = asyncio.Event()      # never set → wait() blocks

    before = asyncio.all_tasks()
    t = asyncio.create_task(_race_readline(stream, stop_event))

    # Let _race_readline create its two child tasks and park on
    # asyncio.wait before we cancel it.
    for _ in range(5):
        await asyncio.sleep(0)

    t.cancel()

    # Drain enough ticks for any cancelled child to settle into `done`.
    for _ in range(50):
        await asyncio.sleep(0)

    if not t.done():
        print("  _race_readline task never finished after cancel")
        return False

    # all_tasks() returns only not-yet-finished tasks — any task created
    # after the `before` snapshot that survives here is a leak.
    leaked = asyncio.all_tasks() - before - {asyncio.current_task()}
    if leaked:
        print(
            f"  leaked {len(leaked)} pending task(s): "
            f"{sorted(str(x.get_coro()) for x in leaked)}"
        )
        return False
    return True


TESTS = [
    (
        "cancelled _race_readline leaks no child task",
        test_cancelled_race_readline_leaks_no_child_task,
    ),
]


def main_run() -> int:
    failed = 0
    try:
        for name, fn in TESTS:
            try:
                ok = asyncio.run(fn())
            except Exception as e:
                ok = False
                import traceback
                traceback.print_exc()
                print(f"  exception: {e}")
            print(f"{PASS if ok else FAIL}  {name}")
            if not ok:
                failed += 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
    print()
    if failed:
        print(f"{failed} of {len(TESTS)} test(s) FAILED")
    else:
        print(f"all {len(TESTS)} tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main_run())
