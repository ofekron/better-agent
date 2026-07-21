#!/usr/bin/env python3

import asyncio
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ["BETTER_AGENT_HOME"] = tempfile.mkdtemp(prefix="ba-turn-start-test-")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from codex_native import CodexRolloutTailer  # noqa: E402
from turn_manager import TurnManager  # noqa: E402
import user_prefs  # noqa: E402


def test_threshold_validation() -> None:
    assert user_prefs.set_task_start_silence_seconds(45) == 45
    assert user_prefs.get_task_start_silence_seconds() == 45
    for invalid in (True, 14, 3601, "90"):
        try:
            user_prefs.set_task_start_silence_seconds(invalid)  # type: ignore[arg-type]
        except ValueError:
            continue
        raise AssertionError(f"accepted invalid threshold: {invalid!r}")


def test_turn_scoped_stall_and_recovery() -> None:
    manager = TurnManager(object())
    old = (datetime.now(timezone.utc) - timedelta(seconds=16)).isoformat()
    manager._run_state = {
        "sid": [
            {
                "run_id": "target",
                "startup_phase": "awaiting_provider_start",
                "startup_phase_started_at": old,
                "startup_silence_threshold_seconds": 15,
                "provider_kind": "codex",
                "startup_expected_activity": "task_started",
                "last_event_at": old,
            },
            {"run_id": "sibling", "last_event_at": old},
        ]
    }

    assert manager._update_startup_stalls("sid") is True
    target, sibling = manager._run_state["sid"]
    assert target["startup_phase"] == "stalled"
    assert target.get("stalled_at")

    assert manager.run_state_record_activity("sid", "target", "task_started") is True
    assert target["startup_phase"] == "awaiting_provider_ready"
    assert target["startup_expected_activity"] == "turn_context"
    assert manager.run_state_record_activity("sid", "target", "turn_context") is True
    assert target["startup_phase"] == "running"
    assert "stalled_at" not in target
    assert sibling["last_event_at"] == old

    manager.run_state_record_activity("sid", "target", "agent_message")
    assert sibling["last_event_at"] == old


async def test_codex_task_started_hook() -> None:
    with tempfile.TemporaryDirectory(prefix="ba-codex-activity-") as tmp:
        rollout = Path(tmp) / "rollout.jsonl"
        rollout.write_text(
            '{"type":"task_started","timestamp":"2026-01-01T00:00:00Z"}\n'
            '{"type":"turn_context","timestamp":"2026-01-01T00:00:01Z"}\n',
            encoding="utf-8",
        )
        lifecycle: list[str] = []
        rendered: list[dict] = []
        tailer = CodexRolloutTailer(
            path=rollout,
            start_byte=0,
            namespace="test",
            dispatch=rendered.append,
            on_lifecycle_update=lifecycle.append,
        )
        assert await tailer.drain_available() is True
        assert lifecycle == ["task_started", "turn_context"]
        assert rendered == []


async def main() -> None:
    test_threshold_validation()
    test_turn_scoped_stall_and_recovery()
    await test_codex_task_started_hook()
    print("turn start activity regression tests passed")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        import shutil

        shutil.rmtree(os.environ["BETTER_AGENT_HOME"], ignore_errors=True)
