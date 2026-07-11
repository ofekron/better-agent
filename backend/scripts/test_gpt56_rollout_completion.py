from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
import sys


BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

import runner_codex
from runner_codex import (
    _AppServerProcess,
    _await_pending_tool_calls,
    _forward_rollout_terminal,
    _rollout_attempt_boundary,
    _settle_app_server_process,
    _wait_codex_agent_tree_terminal,
)


def _event(payload: dict) -> str:
    return json.dumps({"type": "event_msg", "payload": payload}) + "\n"


class _RolloutProc:
    returncode = None

    def __init__(self) -> None:
        self._mapped: asyncio.Queue[bytes] = asyncio.Queue()


async def test_live_app_server_completes_from_rollout() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        rollout = Path(tmp) / "rollout.jsonl"
        rollout.write_text(
            _event({"type": "agent_message", "message": "working", "phase": "commentary"}),
            encoding="utf-8",
        )
        proc = _RolloutProc()
        task = asyncio.create_task(
            _forward_rollout_terminal(proc, str(rollout), byte_offset=0),
        )
        try:
            await asyncio.sleep(0.1)
            assert proc._mapped.empty()
            with rollout.open("a", encoding="utf-8") as file:
                file.write(_event({"type": "task_complete"}))
            row = json.loads(await asyncio.wait_for(proc._mapped.get(), timeout=2))
            assert row["type"] == "turn.completed"
            assert row["rollout_terminal"] is True
            # A completed commentary-only turn is a SUCCESS: no parent-final
            # guard exists — content falls back to last-assistant text.
            assert not hasattr(runner_codex, "_apply_parent_final_guard")
            success, error = runner_codex.apply_ghost_completion_guard(
                success=True,
                cancelled=False,
                error=None,
                prompt="finish the task",
                assistant_seen=True,
                total_usage={"total_tokens": 10},
                result_seen=True,
            )
            assert success is True
            assert error is None
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_live_app_server_marks_tool_only_rollout_completion() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        rollout = Path(tmp) / "rollout.jsonl"
        rollout.write_text(
            json.dumps({
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call_worker",
                    "output": "## Executive summary\n\nworker done",
                },
            }) + "\n",
            encoding="utf-8",
        )
        proc = _RolloutProc()
        task = asyncio.create_task(
            _forward_rollout_terminal(proc, str(rollout), byte_offset=0),
        )
        try:
            with rollout.open("a", encoding="utf-8") as file:
                file.write(_event({"type": "task_complete"}))
            row = json.loads(await asyncio.wait_for(proc._mapped.get(), timeout=2))
            assert row["type"] == "turn.completed"
            assert row["assistant_seen"] is False
            success, error = runner_codex.apply_ghost_completion_guard(
                success=True,
                cancelled=False,
                error=None,
                prompt="finish the task",
                assistant_seen=False,
                total_usage={},
                result_seen=True,
            )
            assert success is False
            assert error == "prompt_not_executed"
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_live_app_server_marks_empty_rollout_completion() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        rollout = Path(tmp) / "rollout.jsonl"
        rollout.write_text("", encoding="utf-8")
        proc = _RolloutProc()
        task = asyncio.create_task(
            _forward_rollout_terminal(proc, str(rollout), byte_offset=0),
        )
        try:
            with rollout.open("a", encoding="utf-8") as file:
                file.write(_event({"type": "task_complete"}))
            row = json.loads(await asyncio.wait_for(proc._mapped.get(), timeout=2))
            assert row["type"] == "turn.completed"
            assert row["assistant_seen"] is False
            success, error = runner_codex.apply_ghost_completion_guard(
                success=True,
                cancelled=False,
                error=None,
                prompt="finish the task",
                assistant_seen=False,
                total_usage={},
                result_seen=True,
            )
            assert success is False
            assert error == "prompt_not_executed"
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_live_app_server_accepts_marked_final_answer() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        rollout = Path(tmp) / "rollout.jsonl"
        rollout.write_text(
            _event({
                "type": "agent_message",
                "message": "## Executive summary\n\ncomplete",
                "phase": "final_answer",
            }),
            encoding="utf-8",
        )
        proc = _RolloutProc()
        task = asyncio.create_task(
            _forward_rollout_terminal(proc, str(rollout), byte_offset=0),
        )
        try:
            with rollout.open("a", encoding="utf-8") as file:
                file.write(_event({"type": "task_complete"}))
            row = json.loads(await asyncio.wait_for(proc._mapped.get(), timeout=2))
            assert row["type"] == "turn.completed"
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


class _Stdout:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = [(json.dumps(row) + "\n").encode() for row in rows]

    async def readline(self) -> bytes:
        if self.rows:
            return self.rows.pop(0)
        await asyncio.Event().wait()


class _Stdin:
    def write(self, _data: bytes) -> None:
        pass

    async def drain(self) -> None:
        pass


class _AppProc:
    pid = 1
    returncode = None
    stderr = None

    def __init__(self, rows: list[dict]) -> None:
        self.stdout = _Stdout(rows)
        self.stdin = _Stdin()


async def test_dynamic_tool_does_not_block_terminal_reader() -> None:
    rows = [
        {"id": 9, "method": "item/tool/call", "params": {"tool": "slow"}},
        {"method": "turn/completed", "params": {"turn": {"status": "completed"}}},
    ]
    blocker = asyncio.Event()

    async def slow(_params: dict) -> dict:
        await blocker.wait()
        return {"ok": True}

    with tempfile.TemporaryDirectory() as tmp:
        client = _AppServerProcess(_AppProc(rows), Path(tmp), {"slow": slow})
        try:
            row = json.loads(await asyncio.wait_for(client.stdout.__anext__(), timeout=1))
            assert row["type"] == "turn.completed"
            assert len(client._server_request_tasks) == 1
        finally:
            blocker.set()
            client._reader_task.cancel()
            client._steer_task.cancel()
            await asyncio.gather(
                client._reader_task,
                client._steer_task,
                *client._server_request_tasks,
                return_exceptions=True,
            )


async def test_terminal_waits_for_pending_dynamic_tool() -> None:
    rows = [
        {"id": 9, "method": "item/tool/call", "params": {"tool": "slow"}},
        {"method": "turn/completed", "params": {"turn": {"status": "completed"}}},
    ]
    blocker = asyncio.Event()

    async def slow(_params: dict) -> dict:
        await blocker.wait()
        return {"ok": True}

    with tempfile.TemporaryDirectory() as tmp:
        client = _AppServerProcess(_AppProc(rows), Path(tmp), {"slow": slow})
        try:
            row = json.loads(await asyncio.wait_for(client.stdout.__anext__(), timeout=1))
            assert row["type"] == "turn.completed"
            waiting = asyncio.create_task(_await_pending_tool_calls(client))
            await asyncio.sleep(0.05)
            assert not waiting.done()
            blocker.set()
            await asyncio.wait_for(waiting, timeout=1)
        finally:
            blocker.set()
            client._reader_task.cancel()
            client._steer_task.cancel()
            await asyncio.gather(
                client._reader_task,
                client._steer_task,
                *client._server_request_tasks,
                return_exceptions=True,
            )


async def test_pending_dynamic_tool_wait_honors_cancel() -> None:
    proc = object.__new__(_AppServerProcess)
    blocker = asyncio.Event()
    task = asyncio.create_task(blocker.wait())
    proc._pending_tool_calls = {task: 9}
    proc.returncode = None
    with tempfile.TemporaryDirectory() as tmp:
        cancel_path = Path(tmp) / "cancel"
        waiting = asyncio.create_task(_await_pending_tool_calls(
            proc, cancel_path=cancel_path,
        ))
        await asyncio.sleep(0.05)
        cancel_path.touch()
        try:
            await asyncio.wait_for(waiting, timeout=1)
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("pending tool wait ignored cancellation")
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_parent_terminal_waits_for_recursive_agent_tree() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "root.jsonl"
        child = Path(tmp) / "child.jsonl"
        grandchild = Path(tmp) / "grandchild.jsonl"
        child_id = "child-thread"
        grandchild_id = "grandchild-thread"
        root.write_text(
            json.dumps({
                "type": "event_msg",
                "payload": {
                    "type": "sub_agent_activity",
                    "event_id": "spawn-child",
                    "agent_thread_id": child_id,
                    "agent_path": "/root/child",
                    "kind": "started",
                },
            }) + "\n",
            encoding="utf-8",
        )
        child.write_text(
            _event({"type": "task_started"}) +
            json.dumps({
                "type": "event_msg",
                "payload": {
                    "type": "sub_agent_activity",
                    "event_id": "spawn-grandchild",
                    "agent_thread_id": grandchild_id,
                    "agent_path": "/root/child/grandchild",
                    "kind": "started",
                },
            }) + "\n",
            encoding="utf-8",
        )
        grandchild.write_text(_event({"type": "task_started"}), encoding="utf-8")
        paths = {child_id: child, grandchild_id: grandchild}

        waiting = asyncio.create_task(_wait_codex_agent_tree_terminal(
            root,
            start_byte=0,
            resolve_path=lambda thread_id: paths.get(thread_id),
        ))
        await asyncio.sleep(0.1)
        assert not waiting.done()
        with child.open("a", encoding="utf-8") as file:
            file.write(_event({"type": "task_complete"}))
        await asyncio.sleep(0.1)
        assert not waiting.done()
        with grandchild.open("a", encoding="utf-8") as file:
            file.write(_event({"type": "task_complete"}))
        await asyncio.wait_for(waiting, timeout=2)


async def test_rollout_completion_never_signals_or_kills() -> None:
    calls: list[str] = []

    class Control:
        def signal_stop(self, _pid: int) -> None:
            calls.append("signal")

        def force_kill(self, _pid: int) -> None:
            calls.append("kill")

    class Proc:
        pid = 1
        returncode = None

        async def close_input(self) -> None:
            calls.append("close")
            self.returncode = 0

        async def wait(self) -> int:
            return 0

    original = runner_codex._process_control
    runner_codex._process_control = lambda: Control()
    try:
        await _settle_app_server_process(
            Proc(),
            rollout_terminal_completion=True,
            log=runner_codex.logging.getLogger("test"),
        )
    finally:
        runner_codex._process_control = original
    assert calls == ["close"]


def test_resumed_session_requires_proven_boundary() -> None:
    offset, known = _rollout_attempt_boundary("resumed-sid", None)
    assert offset == 0
    assert known is False
    offset, known = _rollout_attempt_boundary(None, None)
    assert offset == 0
    assert known is True
    source = (BACKEND / "runner_codex.py").read_text(encoding="utf-8")
    assert "not turn_completed_seen and not cancelled and attempt_boundary_known" in source
    assert "not cancelled and attempt_boundary_known" in source


async def test_fail_pending_tool_calls_synthesizes_error_response() -> None:
    proc = object.__new__(_AppServerProcess)
    proc._pending_tool_calls = {}
    sent: list[dict] = []

    async def _capture(message: dict) -> None:
        sent.append(message)

    proc._try_send_response = _capture

    hang = asyncio.Event()

    async def _hanging_handler() -> None:
        await hang.wait()

    async def _done_handler() -> str:
        return "done"

    hung_task = asyncio.create_task(_hanging_handler())
    done_task = asyncio.create_task(_done_handler())
    await done_task
    proc._pending_tool_calls[hung_task] = 42
    proc._pending_tool_calls[done_task] = 43

    await proc._fail_pending_tool_calls("turn interrupted before tool completed")

    assert hung_task.cancelled()
    assert len(sent) == 1
    assert sent[0]["id"] == 42
    assert "turn interrupted" in sent[0]["error"]["message"]

    sent.clear()
    await proc._fail_pending_tool_calls("again")
    assert sent == []


async def main() -> None:
    await test_live_app_server_completes_from_rollout()
    await test_live_app_server_marks_tool_only_rollout_completion()
    await test_live_app_server_marks_empty_rollout_completion()
    await test_live_app_server_accepts_marked_final_answer()
    await test_dynamic_tool_does_not_block_terminal_reader()
    await test_terminal_waits_for_pending_dynamic_tool()
    await test_pending_dynamic_tool_wait_honors_cancel()
    await test_parent_terminal_waits_for_recursive_agent_tree()
    await test_rollout_completion_never_signals_or_kills()
    await test_fail_pending_tool_calls_synthesizes_error_response()
    test_resumed_session_requires_proven_boundary()


if __name__ == "__main__":
    asyncio.run(main())
    print("PASS GPT-5.6 rollout completion")
