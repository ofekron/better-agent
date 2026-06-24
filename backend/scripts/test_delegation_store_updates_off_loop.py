from __future__ import annotations

from pathlib import Path


def test_delegation_store_updates_are_off_loop() -> None:
    source = (
        Path(__file__).parents[1] / "orchs" / "manager" / "_delegation.py"
    ).read_text(encoding="utf-8")
    assert "await asyncio.to_thread(\n                    worker_store.touch_worker," in source
    assert "await asyncio.to_thread(\n                    session_fork_store.touch_fork," in source
    assert "worker_store.touch_worker(\n                    cwd, worker_agent_session_id" not in source
    assert "session_fork_store.touch_fork(\n                    cwd, app_session_id" not in source


if __name__ == "__main__":
    test_delegation_store_updates_are_off_loop()
    print("PASS delegation store updates off loop")
