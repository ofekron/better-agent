"""Regression test: `recover_pending_fork_provisioning` must yield to the
event loop once per session it scans.

Live incident (2026-07-31): with ~4700 sessions on disk, this startup
recovery step ate `list(session_manager.iter_root_sessions())` whole
before the loop body ever ran — forcing full per-session migration
(including provider-config resolution) for every session in one
uninterruptible synchronous burst on the event loop. Confirmed via
`lag_watchdog` GIL-starvation warnings and `faulthandler` dumps pinned in
this exact call chain: the backend served zero requests for minutes,
twice in a row across restarts, because "recovery MUST run before prompt
admission reopens" held the whole HTTP/WS server hostage to one
synchronous scan.

Fix: iterate the generator directly and `await asyncio.sleep(0)` per
session — the same idiom `run_recovery.py` already uses for its
per-desc scan, for the identical reason (a long list must not starve
WS/REST handlers between iterations).

Run with:
    cd backend && .venv/bin/python scripts/test_recover_pending_fork_provisioning_yields.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _test_home  # noqa: E402

_test_home.isolate(prefix="fork-provisioning-yield-")

import file_editor  # noqa: E402
import session_manager as session_manager_module  # noqa: E402


def test_yields_once_per_session() -> None:
    session_count = 25
    fake_sessions = [
        {"id": f"sid-{index}", "working_mode": "not-file-edit"}
        for index in range(session_count)
    ]

    original_iter = session_manager_module.manager.iter_root_sessions
    session_manager_module.manager.iter_root_sessions = (
        lambda: iter(fake_sessions)
    )

    original_sleep = asyncio.sleep
    sleep_calls = []

    async def counting_sleep(delay, *args, **kwargs):
        sleep_calls.append(delay)
        return await original_sleep(0, *args, **kwargs)

    file_editor.asyncio.sleep = counting_sleep
    try:
        result = asyncio.run(file_editor.recover_pending_fork_provisioning())
    finally:
        session_manager_module.manager.iter_root_sessions = original_iter
        file_editor.asyncio.sleep = original_sleep

    assert result == {"rearmed": [], "failed": []}
    assert len(sleep_calls) == session_count, (
        f"expected one yield per session ({session_count}), "
        f"got {len(sleep_calls)}"
    )
    assert sleep_calls == [0] * session_count


if __name__ == "__main__":
    test_yields_once_per_session()
    print("PASS recover_pending_fork_provisioning yields once per session")
