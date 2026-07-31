from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
import sys

import pytest

pytestmark = pytest.mark.anyio


BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

import _test_home  # noqa: E402

_test_home.isolate("bc-test-gpt56-rollout-completion-")

import runner_codex  # noqa: E402
from runner_codex import (  # noqa: E402
    _AppServerProcess,
    _await_pending_tool_calls,
    _forward_rollout_terminal,
    _rollout_attempt_boundary,
    _rollout_progress_seen,
    _settle_app_server_process,
    _should_preserve_recoverable_partial,
    _wait_codex_agent_tree_terminal,
)
from provider_completion import recoverable_partial_payload  # noqa: E402


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
            success, error, _retry = runner_codex.apply_ghost_completion_guard(
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
            success, error, _retry = runner_codex.apply_ghost_completion_guard(
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
            success, error, _retry = runner_codex.apply_ghost_completion_guard(
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
            assert len(client._pending_tool_calls) == 1
        finally:
            blocker.set()
            client._reader_task.cancel()
            client._steer_task.cancel()
            await asyncio.gather(
                client._reader_task,
                client._steer_task,
                *client._pending_tool_calls,
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
                *client._pending_tool_calls,
                return_exceptions=True,
            )


async def test_completed_turn_not_flipped_by_pending_tool_join() -> None:
    """The join must never turn a completed turn into a failure.

    `runner_codex._run` awaits `_await_pending_tool_calls` inside a guard whose
    `except Exception` rewrites the outcome to `success=False` /
    `error=f"{type}: {e}"`. Any attribute/API drift between the join and
    `_AppServerProcess` therefore reports a COMPLETED turn as failed. This runs
    the join against a real `_AppServerProcess` under the same guard shape and
    asserts the completed outcome survives.
    """
    rows = [
        {"id": 9, "method": "item/tool/call", "params": {"tool": "quick"}},
        {"method": "turn/completed", "params": {"turn": {"status": "completed"}}},
    ]

    async def quick(_params: dict) -> dict:
        return {"ok": True}

    with tempfile.TemporaryDirectory() as tmp:
        client = _AppServerProcess(_AppProc(rows), Path(tmp), {"quick": quick})
        success, error = True, None
        try:
            row = json.loads(await asyncio.wait_for(client.stdout.__anext__(), timeout=1))
            assert row["type"] == "turn.completed"
            try:
                await asyncio.wait_for(
                    _await_pending_tool_calls(
                        client, cancel_path=Path(tmp) / "cancel",
                    ),
                    timeout=2,
                )
            except asyncio.CancelledError:
                success, error = False, "cancelled"
            except Exception as join_error:
                success = False
                error = f"{type(join_error).__name__}: {join_error}"
            assert success is True, f"completed turn flipped to failure: {error}"
            assert error is None
            assert not client._pending_tool_calls
        finally:
            client._reader_task.cancel()
            client._steer_task.cancel()
            await asyncio.gather(
                client._reader_task,
                client._steer_task,
                return_exceptions=True,
            )

    # Lock the live call site: the join runs only for a completed, successful,
    # uncancelled turn, and any exception it raises rewrites that outcome.
    source = (BACKEND / "runner_codex.py").read_text(encoding="utf-8")
    assert "await _await_pending_tool_calls(proc, cancel_path=_cancel_path)" in source
    assert "if turn_completed_seen and success and not cancelled:" in source


async def test_pending_dynamic_tool_wait_honors_cancel() -> None:
    blocker = asyncio.Event()
    task = asyncio.create_task(blocker.wait())
    with tempfile.TemporaryDirectory() as tmp:
        client = _AppServerProcess(_AppProc([]), Path(tmp), {})
        client._pending_tool_calls.add(task)
        cancel_path = Path(tmp) / "cancel"
        waiting = asyncio.create_task(_await_pending_tool_calls(
            client, cancel_path=cancel_path,
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
            client._reader_task.cancel()
            client._steer_task.cancel()
            await asyncio.gather(
                task,
                client._reader_task,
                client._steer_task,
                return_exceptions=True,
            )


async def test_pending_tool_wait_reads_live_process_exit() -> None:
    """The join must observe the process exiting, not a stale snapshot."""
    blocker = asyncio.Event()
    task = asyncio.create_task(blocker.wait())
    with tempfile.TemporaryDirectory() as tmp:
        app_proc = _AppProc([])
        client = _AppServerProcess(app_proc, Path(tmp), {})
        client._pending_tool_calls.add(task)
        waiting = asyncio.create_task(_await_pending_tool_calls(client))
        await asyncio.sleep(0.05)
        assert not waiting.done()
        app_proc.returncode = 3
        try:
            await asyncio.wait_for(waiting, timeout=1)
        except RuntimeError as exc:
            assert "exited before pending tool calls" in str(exc)
        else:
            raise AssertionError("pending tool wait ignored app-server exit")
        finally:
            blocker.set()
            task.cancel()
            client._reader_task.cancel()
            client._steer_task.cancel()
            await asyncio.gather(
                task,
                client._reader_task,
                client._steer_task,
                return_exceptions=True,
            )


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


async def test_rollout_completion_closes_parent_and_reaps_owned_group() -> None:
    calls: list[str] = []

    class Control:
        def signal_owned_group(self, _group_id: int) -> None:
            calls.append("signal")

        def force_kill_owned_group(self, _group_id: int) -> None:
            calls.append("kill")

    class Proc:
        pid = 1
        process_group_id = 1
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
            stop_requested=False,
            log=runner_codex.logging.getLogger("test"),
        )
    finally:
        runner_codex._process_control = original
    assert calls == ["close", "kill"]


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


def test_transport_truncation_after_tool_progress_is_recoverable_without_replay() -> None:
    native_session_id = "019fa434-ed1e-7c31-ad76-5a7156851517"
    with tempfile.TemporaryDirectory() as tmp:
        rollout = Path(tmp) / "rollout.jsonl"
        rollout.write_text(
            json.dumps({
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "name": "exec_command",
                    "call_id": "call-once",
                },
            }) + "\n",
            encoding="utf-8",
        )
        progress_seen = _rollout_progress_seen(str(rollout), byte_offset=0)

    preserve = _should_preserve_recoverable_partial(
        error="WebSocket connection reset by peer",
        cancelled=False,
        turn_completed_seen=False,
        native_session_id=native_session_id,
        progress_seen=progress_seen,
    )
    complete = recoverable_partial_payload(
        session_id=native_session_id,
        cause="WebSocket connection reset by peer",
    )

    assert preserve is True
    assert complete["success"] is False
    assert complete["outcome"] == "recoverable_partial"
    assert complete["recoverable"] is True
    assert complete["session_id"] == native_session_id
    source = (BACKEND / "runner_codex.py").read_text(encoding="utf-8")
    assert source.index("if _should_preserve_recoverable_partial(") < source.index(
        "if error and not cancelled and _is_network_error_message(error):"
    )


async def main() -> None:
    await test_live_app_server_completes_from_rollout()
    await test_live_app_server_marks_tool_only_rollout_completion()
    await test_live_app_server_marks_empty_rollout_completion()
    await test_live_app_server_accepts_marked_final_answer()
    await test_dynamic_tool_does_not_block_terminal_reader()
    await test_terminal_waits_for_pending_dynamic_tool()
    await test_completed_turn_not_flipped_by_pending_tool_join()
    await test_pending_dynamic_tool_wait_honors_cancel()
    await test_pending_tool_wait_reads_live_process_exit()
    await test_parent_terminal_waits_for_recursive_agent_tree()
    await test_rollout_completion_closes_parent_and_reaps_owned_group()
    test_resumed_session_requires_proven_boundary()
    test_transport_truncation_after_tool_progress_is_recoverable_without_replay()


if __name__ == "__main__":
    asyncio.run(main())
    print("PASS GPT-5.6 rollout completion")
