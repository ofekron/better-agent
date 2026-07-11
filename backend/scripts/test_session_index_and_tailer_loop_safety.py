#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time

HOME = tempfile.mkdtemp(prefix="ba-loop-safety-")
os.environ["BETTER_AGENT_HOME"] = HOME
os.environ["BETTER_AGENT_TEST_MODE"] = "1"

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import session_store
from jsonl_tailer import OwnedClaudeJsonlTailer
from session_manager import SessionManager


async def test_unknown_sid_index_build_does_not_block_loop() -> None:
    sessions = Path(HOME) / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    for index in range(2_000):
        (sessions / f"root-{index:04d}.json").write_text(
            json.dumps({"id": f"root-{index:04d}", "forks": []}),
            encoding="utf-8",
        )
    session_store._reset_home_scoped_caches()
    ticks = 0
    finished = False

    async def heartbeat() -> None:
        nonlocal ticks
        while not finished:
            ticks += 1
            await asyncio.sleep(0)

    ticker = asyncio.create_task(heartbeat())
    resolved = await asyncio.to_thread(session_store._resolve_root_id, "unknown-worker-sid")
    finished = True
    await ticker
    assert resolved is None
    assert ticks > 10
    session_store._reset_home_scoped_caches()
    resolved_after_restart = await asyncio.to_thread(
        session_store._resolve_root_id, "unknown-worker-sid-after-restart"
    )
    assert resolved_after_restart is None


class _FakeTailer:
    def __init__(self) -> None:
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


async def test_owner_unsubscribe_waits_off_loop() -> None:
    manager = SessionManager()
    manager._ensure_home_current()
    token = type("Token", (), {
        "root_id": "root",
        "sid": "sid",
        "generation": 1,
    })()
    manager._owner_generations["sid"] = 1
    unsubscribe = manager.subscribe_owner_revoked(token, lambda: None)
    root_lock = manager._lock_for_root("root")
    acquired = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with root_lock:
            acquired.set()
            release.wait(5)

    thread = threading.Thread(target=holder)
    thread.start()
    assert acquired.wait(1)
    owned = OwnedClaudeJsonlTailer(
        root_id="root",
        app_session_id="sid",
        agent_sid="agent",
        jsonl_path=Path(HOME) / "agent.jsonl",
        start_offset=0,
    )
    owned._refcount = 1
    owned._tailer = _FakeTailer()
    owned._task = asyncio.create_task(asyncio.sleep(0))
    owned._unsubscribe_owner_revoked = unsubscribe
    cleanup = asyncio.create_task(owned.release_async(trigger="test_barrier"))
    await asyncio.sleep(0.02)
    if cleanup.done():
        cleanup.result()
    assert not cleanup.done()
    assert owned._tailer is None
    release.set()
    stop_task = await asyncio.wait_for(cleanup, 1)
    assert stop_task is not None
    await stop_task
    thread.join(1)
    assert not thread.is_alive()


async def main() -> None:
    await test_unknown_sid_index_build_does_not_block_loop()
    await test_owner_unsubscribe_waits_off_loop()
    delegation = (
        Path(__file__).resolve().parents[1] / "orchs/manager/_delegation.py"
    ).read_text(encoding="utf-8")
    assert "team_context = await asyncio.to_thread(" in delegation
    main_source = (Path(__file__).resolve().parents[1] / "main.py").read_text(
        encoding="utf-8"
    )
    assert "_LAG_ATTRIBUTION_SAMPLES" in main_source
    assert "--- rolling loop attribution ---" in main_source
    print("PASS session index and tailer cleanup keep event loop responsive")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        shutil.rmtree(HOME)
