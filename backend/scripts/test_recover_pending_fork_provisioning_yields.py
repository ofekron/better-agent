"""Root-session migration stays off-loop and incrementally consumed."""
from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _test_home  # noqa: E402

_test_home.isolate(prefix="fork-provisioning-yield-")

import file_editor  # noqa: E402
import session_manager as session_manager_module  # noqa: E402


def test_advances_generator_off_loop_incrementally() -> None:
    session_count = 25
    event_loop_thread = threading.get_ident()
    yielded = 0
    next_threads: list[int] = []
    processed_after_yields: list[int] = []

    def fake_sessions():
        nonlocal yielded
        for index in range(session_count):
            next_threads.append(threading.get_ident())
            yielded += 1
            yield {"id": f"sid-{index}", "working_mode": "not-file-edit"}

    original_iter = session_manager_module.manager.iter_root_sessions
    original_state_of = file_editor.session_readiness.state_of
    session_manager_module.manager.iter_root_sessions = fake_sessions

    def record_processing(session):
        del session
        processed_after_yields.append(yielded)
        return "ready"

    file_editor.session_readiness.state_of = record_processing
    try:
        result = asyncio.run(file_editor.recover_pending_fork_provisioning())
    finally:
        session_manager_module.manager.iter_root_sessions = original_iter
        file_editor.session_readiness.state_of = original_state_of

    assert result == {"rearmed": [], "failed": []}
    assert next_threads and all(thread != event_loop_thread for thread in next_threads)
    assert processed_after_yields == list(range(1, session_count + 1))


if __name__ == "__main__":
    test_advances_generator_off_loop_incrementally()
    print("PASS recover_pending_fork_provisioning scans off-loop incrementally")
