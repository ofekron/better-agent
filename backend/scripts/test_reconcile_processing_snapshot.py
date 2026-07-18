import asyncio
import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path


STATE_HOME = tempfile.mkdtemp(prefix="ba-reconcile-processing-")
os.environ["BETTER_AGENT_HOME"] = STATE_HOME
BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import main  # noqa: E402
from session_manager import SessionManager  # noqa: E402


async def test_visible_phase_matches_emitted_progress() -> None:
    manager = SessionManager()
    release = threading.Event()
    started = asyncio.Event()
    observations: list[tuple[str, int, tuple[str, ...]]] = []

    manager._reconcile_fn = lambda _root_id, **_kwargs: release.wait() and []

    def emit(root_id: str, kind: str) -> None:
        _epoch, revision, roots = manager.reconcile_processing_state()
        observations.append((kind, revision, roots))
        if kind == "started":
            started.set()

    manager._emit_processing_fn = emit
    task = asyncio.create_task(manager._async_reconcile_with_progress("root-a"))
    await asyncio.wait_for(started.wait(), timeout=1)
    _epoch, revision, roots = manager.reconcile_processing_state()
    assert (revision, roots) == (1, ("root-a",))
    release.set()
    await task
    assert observations == [("started", 1, ("root-a",)), ("finished", 2, ())]
    assert manager.reconcile_processing_state()[1:] == (2, ())

    observations.clear()
    manager._reconcile_fn = lambda _root_id, **_kwargs: []
    await manager._async_reconcile_with_progress("root-fast")
    assert observations == []
    assert manager.reconcile_processing_state()[1:] == (2, ())


async def test_snapshot_repeats_until_backend_state_is_stable() -> None:
    epoch = "a" * 32
    states = iter([
        (epoch, 1, ("a",)),
        (epoch, 2, ("b",)),
        (epoch, 3, ("b", "c")),
        (epoch, 3, ("b", "c")),
    ])
    sent: list[list[str]] = []
    original = main.session_manager.reconcile_processing_state
    main.session_manager.reconcile_processing_state = lambda: next(states)
    try:
        async def send(frame: dict) -> None:
            sent.append(frame["data"]["root_ids"])

        await main._send_reconcile_processing_snapshot(send)
    finally:
        main.session_manager.reconcile_processing_state = original
    assert sent == [["a"], ["b"], ["b", "c"]]


async def test_subscriber_scheduling_runs_on_event_loop_thread() -> None:
    loop = asyncio.get_running_loop()
    observed: list[asyncio.AbstractEventLoop] = []
    original_root = main.session_manager._root_id_for
    original_schedule = main.session_manager.schedule_reconcile_if_needed
    main.session_manager._root_id_for = lambda _sid: "root-a"

    def schedule(_root_id: str) -> None:
        observed.append(asyncio.get_running_loop())

    main.session_manager.schedule_reconcile_if_needed = schedule
    try:
        await main._schedule_reconcile_for_subscriber("session-a")
    finally:
        main.session_manager._root_id_for = original_root
        main.session_manager.schedule_reconcile_if_needed = original_schedule
    assert observed == [loop]


async def run() -> None:
    await test_visible_phase_matches_emitted_progress()
    await test_snapshot_repeats_until_backend_state_is_stable()
    await test_subscriber_scheduling_runs_on_event_loop_thread()


if __name__ == "__main__":
    try:
        asyncio.run(run())
        print("reconcile processing snapshot: PASS")
    finally:
        shutil.rmtree(STATE_HOME, ignore_errors=True)
