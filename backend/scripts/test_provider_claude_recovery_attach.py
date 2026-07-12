"""Regression test for Claude recovered-run reattachment.

Run with:
    cd backend && PYTHONPATH=. python3 scripts/test_provider_claude_recovery_attach.py
"""
from __future__ import annotations

import asyncio
import os
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

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def test_attach_recovered_run_schedules_normal_bootstrap() -> bool:
    provider = ClaudeProvider({"id": "claude-recover-test"})
    queue: asyncio.Queue = asyncio.Queue()
    scheduled: list[tuple[asyncio.AbstractEventLoop, object, str]] = []

    def fake_schedule(loop, coro, *, name: str):
        task = loop.create_task(coro)
        scheduled.append((loop, task, name))
        return task

    original_schedule = provider_claude.schedule_loop_task
    original_bootstrap = provider._bootstrap_run
    original_write = provider._write_backend_state
    provider_claude.schedule_loop_task = fake_schedule
    provider._bootstrap_run = lambda _rs: asyncio.sleep(0)
    provider._write_backend_state = lambda _rs: None
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
            loop.run_until_complete(scheduled[0][1])
        finally:
            loop.close()
    finally:
        provider_claude.schedule_loop_task = original_schedule
        provider._bootstrap_run = original_bootstrap
        provider._write_backend_state = original_write

    if not attached:
        print(f"{FAIL} attach_recovered_run returned False")
        return False
    rs = provider._runs.get("run-live-1234567890")
    if not isinstance(rs, RunState):
        print(f"{FAIL} recovered run was not registered as RunState: {rs!r}")
        return False
    if not isinstance(rs.popen, RecoveredPopen) or rs.popen.pid != 12345:
        print(f"{FAIL} recovered popen not reconstructed correctly: {rs.popen!r}")
        return False
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
        if getattr(rs, field) != value:
            print(f"{FAIL} RunState.{field}={getattr(rs, field)!r}, expected {value!r}")
            return False
    if rs.queue is not queue:
        print(f"{FAIL} RunState did not use recovery queue")
        return False
    if len(scheduled) != 1 or scheduled[0][2] != "claude-recover-bootstrap-run-live":
        print(f"{FAIL} expected one scheduled bootstrap task, saw {scheduled!r}")
        return False
    print(f"{PASS} Claude recovered live run schedules normal bootstrap")
    return True


def test_attach_recovered_run_rejects_duplicates_and_bad_pid() -> bool:
    provider = ClaudeProvider({"id": "claude-recover-test"})
    queue: asyncio.Queue = asyncio.Queue()
    original_schedule = provider_claude.schedule_loop_task
    scheduled: list[asyncio.Task] = []
    def fake_schedule(_loop, coro, **_kwargs):
        task = _loop.create_task(coro)
        scheduled.append(task)
        return task
    provider_claude.schedule_loop_task = fake_schedule
    try:
        loop = asyncio.new_event_loop()
        try:
            desc = {"run_id": "run-dup", "pid": 12345, "app_session_id": "app"}
            if not provider.attach_recovered_run(desc=desc, queue=queue, loop=loop):
                print(f"{FAIL} initial attach unexpectedly failed")
                return False
            if provider.attach_recovered_run(desc=desc, queue=queue, loop=loop):
                print(f"{FAIL} duplicate attach unexpectedly succeeded")
                return False
            if provider.attach_recovered_run(
                desc={"run_id": "run-bad", "pid": "not-an-int"},
                queue=queue,
                loop=loop,
            ):
                print(f"{FAIL} bad pid attach unexpectedly succeeded")
                return False
            for task in scheduled:
                task.cancel()
            loop.run_until_complete(asyncio.gather(*scheduled, return_exceptions=True))
        finally:
            loop.close()
    finally:
        provider_claude.schedule_loop_task = original_schedule
    print(f"{PASS} Claude recovered attach rejects duplicate/bad inputs")
    return True


def test_attach_schedule_failure_clears_pending() -> bool:
    provider = ClaudeProvider({"id": "claude-recover-test"})
    original_schedule = provider_claude.schedule_loop_task

    def reject_schedule(_loop, coro, **_kwargs):
        coro.close()
        return None

    provider_claude.schedule_loop_task = reject_schedule
    try:
        loop = asyncio.new_event_loop()
        try:
            receipt = provider.attach_recovered_run(
                desc={"run_id": "run-rejected", "pid": 12345, "app_session_id": "app"},
                queue=asyncio.Queue(), loop=loop,
            )
            established = loop.run_until_complete(receipt.wait())
        finally:
            loop.close()
    finally:
        provider_claude.schedule_loop_task = original_schedule
    if receipt or established or provider._recovery_pending_states:
        print(f"{FAIL} rejected schedule retained recovery ownership")
        return False
    print(f"{PASS} Claude rejected recovery schedule clears pending ownership")
    return True


if __name__ == "__main__":
    ok = test_attach_recovered_run_schedules_normal_bootstrap()
    ok = test_attach_recovered_run_rejects_duplicates_and_bad_pid() and ok
    ok = test_attach_schedule_failure_clears_pending() and ok
    raise SystemExit(0 if ok else 1)
