"""Regression test for Claude recovered-run reattachment.

Run with:
    cd backend && PYTHONPATH=. python3 scripts/test_provider_claude_recovery_attach.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_BACKEND = _HERE.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import _test_home  # noqa: E402
_test_home.isolate("bc-test-claude-recover-attach-")

import provider_claude  # noqa: E402
from provider import RecoveredPopen  # noqa: E402
from provider_claude import ClaudeProvider, RunState  # noqa: E402


def test_attach_recovered_run_schedules_normal_bootstrap() -> None:
    provider = ClaudeProvider({"id": "claude-recover-test"})
    queue: asyncio.Queue = asyncio.Queue()
    scheduled: list[tuple[asyncio.AbstractEventLoop, object, str]] = []

    def fake_schedule(loop, coro, *, name: str) -> None:
        scheduled.append((loop, coro, name))
        coro.close()

    original_schedule = provider_claude.schedule_loop_task
    provider_claude.schedule_loop_task = fake_schedule
    try:
        loop = asyncio.new_event_loop()
        try:
            desc = {
                "run_id": "run-live-1234567890",
                "pid": 12345,
                "mode": "manager",
                "app_session_id": "app-session",
                "persist_to": "persist-session",
                "session_id": "claude-native-sid",
                "jsonl_path": str(Path("/tmp/fake-claude.jsonl")),
                "processed_byte": 42,
                "started_at": "2026-07-07T00:00:00",
                "cancelled": True,
                "target_message_id": "assistant-msg",
                "turn_run_id": "turn-run",
            }
            attached = provider.attach_recovered_run(desc=desc, queue=queue, loop=loop)
        finally:
            loop.close()
    finally:
        provider_claude.schedule_loop_task = original_schedule

    assert attached, "attach_recovered_run returned False"
    rs = provider._runs.get("run-live-1234567890")
    assert isinstance(rs, RunState), f"recovered run was not registered as RunState: {rs!r}"
    assert isinstance(rs.popen, RecoveredPopen) and rs.popen.pid == 12345, (
        f"recovered popen not reconstructed correctly: {rs.popen!r}"
    )
    expected = {
        "mode": "manager",
        "app_session_id": "app-session",
        "persist_to": "persist-session",
        "session_id": "claude-native-sid",
        "processed_byte": 42,
        "started_at": "2026-07-07T00:00:00",
        "cancelled": True,
        "target_message_id": "assistant-msg",
        "turn_run_id": "turn-run",
    }
    for field, value in expected.items():
        assert getattr(rs, field) == value, (
            f"RunState.{field}={getattr(rs, field)!r}, expected {value!r}"
        )
    assert rs.queue is queue, "RunState did not use recovery queue"
    assert len(scheduled) == 1 and scheduled[0][2] == "claude-recover-bootstrap-run-live", (
        f"expected one scheduled bootstrap task, saw {scheduled!r}"
    )


def test_attach_recovered_run_rejects_duplicates_and_bad_pid() -> None:
    provider = ClaudeProvider({"id": "claude-recover-test"})
    queue: asyncio.Queue = asyncio.Queue()
    original_schedule = provider_claude.schedule_loop_task
    provider_claude.schedule_loop_task = lambda _loop, coro, **_kwargs: coro.close()
    try:
        loop = asyncio.new_event_loop()
        try:
            desc = {"run_id": "run-dup", "pid": 12345, "app_session_id": "app"}
            assert provider.attach_recovered_run(desc=desc, queue=queue, loop=loop), (
                "initial attach unexpectedly failed"
            )
            assert not provider.attach_recovered_run(desc=desc, queue=queue, loop=loop), (
                "duplicate attach unexpectedly succeeded"
            )
            assert not provider.attach_recovered_run(
                desc={"run_id": "run-bad", "pid": "not-an-int"},
                queue=queue,
                loop=loop,
            ), "bad pid attach unexpectedly succeeded"
        finally:
            loop.close()
    finally:
        provider_claude.schedule_loop_task = original_schedule


if __name__ == "__main__":
    test_attach_recovered_run_schedules_normal_bootstrap()
    test_attach_recovered_run_rejects_duplicates_and_bad_pid()
