import asyncio
import inspect
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-known-workers-")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import Coordinator
from orchs.manager import bootstrap
from orchs.manager import _delegation
import cli as cli_module
import delegation_status_store


def teardown_module():
    shutil.rmtree(_TMP_HOME, ignore_errors=True)


def test_known_workers_override_replaces_cwd_projection(monkeypatch):
    def fail_if_used(cwd, limit):
        raise AssertionError("cwd worker projection should not be read")

    monkeypatch.setattr(
        bootstrap.worker_store,
        "list_worker_projection",
        fail_if_used,
    )

    prompt = bootstrap.build_wrapped_prompt(
        "/tmp/project",
        "verify login",
        False,
        known_workers=[{
            "agent_session_id": "worker-a",
            "registry_cwd": "/tmp/worker-project",
            "orchestration_mode": "native",
            "node_id": "primary",
            "delegation_count": 3,
            "description": "testape:device-worker",
        }],
    )

    assert "worker-a /tmp/worker-project native primary 3" in prompt
    assert '"testape:device-worker"' in prompt
    assert "<user_prompt>\nverify login\n</user_prompt>" in prompt


def test_handle_prompt_accepts_known_worker_registry_cwds():
    params = inspect.signature(Coordinator.handle_prompt).parameters
    assert "known_worker_registry_cwds" in params


def test_delegate_tools_accept_worker_registry_cwd():
    root = Path(__file__).resolve().parents[1]
    for rel in ("runner.py", "runner_codex.py"):
        source = (root / rel).read_text(encoding="utf-8")
        assert '"worker_registry_cwd"' in source
        assert '"worker_registry_cwd": worker_registry_cwd' in source


def test_bare_worker_missing_parent_preserves_requested_fork():
    assert _delegation.missing_parent_should_run_direct(
        "fork",
        {"bare_config": True},
    ) is False
    assert _delegation.missing_parent_should_run_direct(
        "direct",
        {"bare_config": True},
    ) is True


def test_known_workers_file_builds_registry_cwd_map(tmp_path):
    path = tmp_path / "known-workers.json"
    path.write_text(json.dumps([
        {
            "agent_session_id": "worker-a",
            "registry_cwd": "/tmp/worker-project",
        },
    ]))

    workers = cli_module._load_known_workers_file(str(path))

    assert cli_module._known_worker_registry_cwds(workers) == {
        "worker-a": str(Path("/tmp/worker-project").resolve()),
    }


def test_delegate_uses_known_worker_registry_cwd(monkeypatch):
    caller_sid = "caller-session"
    worker_sid = "worker-session"
    caller_cwd = "/tmp/caller-project"
    worker_cwd = str(Path("/tmp/worker-project").resolve())

    class TurnManager:
        cancel_events: dict[str, asyncio.Event] = {}
        current_turn_workers: dict[str, list[dict]] = {}
        current_assistant_msgs: dict[str, dict] = {}

        def get_turn_save_callback(self, app_session_id: str):
            return None

        def run_state_add(self, *args, **kwargs):
            pass

        def run_state_remove(self, *args, **kwargs):
            pass

        def in_flight_event_count(self, app_session_id: str):
            return 0

        def in_flight_event_count_after_current_event(self, app_session_id: str):
            return 0

        async def emit_run_state(self, app_session_id: str):
            pass

    class FakeCoordinator:
        pair_locks: dict[tuple[str, str], asyncio.Lock] = {}
        active_delegations: dict[str, int] = {}
        turn_manager = TurnManager()

        def known_worker_registry_cwd(self, app_session_id: str, agent_session_id: str):
            assert app_session_id == caller_sid
            return {worker_sid: worker_cwd}.get(agent_session_id)

        async def persist_and_dispatch_raw(self, app_session_id: str, event: dict):
            pass

    def fake_get_worker(cwd: str, agent_session_id: str):
        if cwd == worker_cwd and agent_session_id == worker_sid:
            return {
                "agent_session_id": worker_sid,
                "orchestration_mode": "native",
                "agent_sid": "agent-parent",
                "node_id": "primary",
            }
        return None

    def fake_session_get(agent_session_id: str):
        if agent_session_id != worker_sid:
            return None
        return {
            "id": worker_sid,
            "name": "Known worker",
            "orchestration_mode": "native",
            "agent_session_id": "agent-parent",
            "model": "claude-sonnet-4-6",
        }

    async def fake_locked(*args, cwd: str, worker_agent_session_id: str, **kwargs):
        return {
            "success": True,
            "cwd": cwd,
            "worker_session_id": worker_agent_session_id,
            "session_is_registered_worker": kwargs["session_is_registered_worker"],
        }

    monkeypatch.setattr(_delegation.worker_store, "get_worker", fake_get_worker)
    monkeypatch.setattr(_delegation.session_manager, "get", fake_session_get)
    monkeypatch.setattr(_delegation, "run_delegation_locked", fake_locked)

    result = asyncio.run(_delegation.run_delegation(
        FakeCoordinator(),
        app_session_id=caller_sid,
        instructions="do worker task",
        worker_session_id=worker_sid,
        worker_description="Known worker",
        model="claude-sonnet-4-6",
        cwd=caller_cwd,
    ))

    assert result["success"] is True
    assert result["worker_session_id"] == worker_sid
    assert result["cwd"] == worker_cwd
    assert result["session_is_registered_worker"] is True


def test_delegate_uses_session_cwd_without_worker_record(monkeypatch):
    caller_sid = "caller-session"
    worker_sid = "worker-session"
    caller_cwd = "/tmp/caller-project"
    worker_cwd = str(Path("/tmp/worker-project").resolve())

    class TurnManager:
        cancel_events: dict[str, asyncio.Event] = {}
        current_turn_workers: dict[str, list[dict]] = {}
        current_assistant_msgs: dict[str, dict] = {}

        def get_turn_save_callback(self, app_session_id: str):
            return None

        def run_state_add(self, *args, **kwargs):
            pass

        def run_state_remove(self, *args, **kwargs):
            pass

        def in_flight_event_count(self, app_session_id: str):
            return 0

        def in_flight_event_count_after_current_event(self, app_session_id: str):
            return 0

        async def emit_run_state(self, app_session_id: str):
            pass

    class FakeCoordinator:
        pair_locks: dict[tuple[str, str], asyncio.Lock] = {}
        active_delegations: dict[str, int] = {}
        turn_manager = TurnManager()

        def known_worker_registry_cwd(self, app_session_id: str, agent_session_id: str):
            return None

        async def persist_and_dispatch_raw(self, app_session_id: str, event: dict):
            pass

        async def broadcast_workers_changed(self, cwd: str):
            pass

    def fake_get_worker(cwd: str, agent_session_id: str):
        return None

    def fake_session_get(agent_session_id: str):
        if agent_session_id != worker_sid:
            return None
        return {
            "id": worker_sid,
            "name": "Ad hoc session",
            "cwd": worker_cwd,
            "orchestration_mode": "native",
            "agent_session_id": "agent-parent",
            "model": "claude-sonnet-4-6",
        }

    def fail_upsert_worker(*args, **kwargs):
        raise AssertionError("unregistered ask(fork) session must not enter worker roster")

    async def fake_locked(*args, cwd: str, worker_agent_session_id: str, **kwargs):
        return {
            "success": True,
            "cwd": cwd,
            "worker_session_id": worker_agent_session_id,
            "session_is_registered_worker": kwargs["session_is_registered_worker"],
        }

    monkeypatch.setattr(_delegation.worker_store, "get_worker", fake_get_worker)
    monkeypatch.setattr(_delegation.worker_store, "upsert_worker", fail_upsert_worker)
    monkeypatch.setattr(_delegation.session_manager, "get", fake_session_get)
    monkeypatch.setattr(_delegation, "run_delegation_locked", fake_locked)

    result = asyncio.run(_delegation.run_delegation(
        FakeCoordinator(),
        app_session_id=caller_sid,
        instructions="do worker task",
        worker_session_id=worker_sid,
        worker_description="Ad hoc session",
        model="claude-sonnet-4-6",
        cwd=caller_cwd,
    ))

    assert result["success"] is True
    assert result["worker_session_id"] == worker_sid
    assert result["cwd"] == worker_cwd
    assert result["session_is_registered_worker"] is False


def test_direct_delegations_to_same_worker_serialize_across_callers(monkeypatch):
    worker_sid = "worker-session"
    cwd = str(Path("/tmp/project").resolve())
    running = 0
    max_running = 0

    class TurnManager:
        cancel_events: dict[str, asyncio.Event] = {}
        current_turn_workers: dict[str, list[dict]] = {}
        current_assistant_msgs: dict[str, dict] = {}

        def get_turn_save_callback(self, app_session_id: str):
            return None

        def run_state_add(self, *args, **kwargs):
            pass

        def run_state_remove(self, *args, **kwargs):
            pass

        def in_flight_event_count(self, app_session_id: str):
            return 0

        def in_flight_event_count_after_current_event(self, app_session_id: str):
            return 0

        async def emit_run_state(self, app_session_id: str):
            pass

    class FakeCoordinator:
        def __init__(self):
            self.pair_locks: dict[tuple[str, str], asyncio.Lock] = {}
            self.active_delegations: dict[str, int] = {}
            self.turn_manager = TurnManager()

        def known_worker_registry_cwd(self, app_session_id: str, agent_session_id: str):
            return cwd

        async def persist_and_dispatch_raw(self, app_session_id: str, event: dict):
            pass

    def fake_get_worker(worker_cwd: str, agent_session_id: str):
        assert worker_cwd == cwd
        assert agent_session_id == worker_sid
        return {
            "agent_session_id": worker_sid,
            "orchestration_mode": "native",
            "agent_sid": "agent-parent",
            "node_id": "primary",
        }

    def fake_session_get(agent_session_id: str):
        if agent_session_id != worker_sid:
            return None
        return {
            "id": worker_sid,
            "name": "Shared worker",
            "orchestration_mode": "native",
            "agent_session_id": "agent-parent",
            "model": "claude-sonnet-4-6",
        }

    async def fake_locked(*args, worker_agent_session_id: str, **kwargs):
        nonlocal running, max_running
        assert worker_agent_session_id == worker_sid
        running += 1
        max_running = max(max_running, running)
        await asyncio.sleep(0.01)
        running -= 1
        return {
            "success": True,
            "worker_session_id": worker_agent_session_id,
        }

    monkeypatch.setattr(_delegation.worker_store, "get_worker", fake_get_worker)
    monkeypatch.setattr(_delegation.session_manager, "get", fake_session_get)
    monkeypatch.setattr(_delegation, "run_delegation_locked", fake_locked)

    async def run() -> list[dict]:
        coordinator = FakeCoordinator()
        return await asyncio.gather(
            _delegation.run_delegation(
                coordinator,
                app_session_id="manager-a",
                instructions="do worker task",
                worker_session_id=worker_sid,
                worker_description="Shared worker",
                model="claude-sonnet-4-6",
                cwd=cwd,
                run_mode="direct",
            ),
            _delegation.run_delegation(
                coordinator,
                app_session_id="manager-b",
                instructions="do worker task",
                worker_session_id=worker_sid,
                worker_description="Shared worker",
                model="claude-sonnet-4-6",
                cwd=cwd,
                run_mode="direct",
            ),
        )

    results = asyncio.run(run())

    assert all(result["success"] is True for result in results)
    assert max_running == 1


def test_worker_pid_stamp_emits_run_state(monkeypatch):
    class Popen:
        pid = 12345

    class Provider:
        KIND = "claude"
        id = "provider"
        record = {"name": "Claude"}

        def __init__(self):
            self._runs = {}
            self.start_kwargs = None
            self.observed_cwd = None

        def start_run(self, *, run_id, queue, cwd, **kwargs):
            self.observed_cwd = subprocess.check_output(
                [sys.executable, "-c", "import os; print(os.getcwd())"],
                cwd=cwd,
                text=True,
            ).strip()
            self.start_kwargs = kwargs
            self._runs[run_id] = SimpleNamespace(
                popen=Popen(),
                run_dir=Path(_TMP_HOME) / "runs" / run_id,
            )
            queue.put_nowait(_delegation.StreamEvent(
                type="complete",
                data={"success": True, "session_id": "worker-agent"},
            ))

        def cancel_turn(self, run_id):
            raise AssertionError("cancel_turn should not be called")

    class TurnManager:
        def __init__(self):
            self.active_run_ids = {}
            self.pid_stamps = []
            self.emits = []

        def run_state_set_pid(self, app_session_id, worker_run_id, pid):
            self.pid_stamps.append((app_session_id, worker_run_id, pid))

        async def emit_run_state(self, app_session_id):
            self.emits.append(app_session_id)

        async def _publish_terminal_lifecycle(self, *args, **kwargs):
            pass

    class Coordinator:
        def __init__(self):
            self.turn_manager = TurnManager()
            self.internal_token = "token"
            self.provider = Provider()

        def provider_for_session(self, worker_agent_session_id):
            return self.provider

        def provider_for_run(self, *_args):
            return self.provider

        async def broadcast_workers_changed(self, cwd):
            pass

    monkeypatch.setattr(_delegation, "compute_jsonl_read_path", lambda *args, **kwargs: None)
    monkeypatch.setattr(_delegation.session_manager, "get", lambda sid: {
        "id": sid,
        "agent_session_id": "worker-agent",
        "orchestration_mode": "native",
    })

    coordinator = Coordinator()
    events = []

    async def ws_callback(event):
        events.append(event)

    result = asyncio.run(_delegation.run_delegation_locked(
        coordinator,
        app_session_id="manager-session",
        ws_callback=ws_callback,
        cancel_event=asyncio.Event(),
        delegation_id="del_123",
        worker_run_id="worker-del_123",
        instructions="do work",
        instructions_preview="do work",
        worker_agent_session_id="worker-session",
        worker_session=_delegation.session_manager.get("worker-session") or {},
        worker_description="worker",
        worker_orchestration_mode="native",
        worker_parent_claude_sid="worker-agent",
        session_is_registered_worker=True,
        target_message_id="assistant-msg",
        run_mode="direct",
        model="claude-sonnet-4-6",
        cwd=_delegation.CanonicalDelegationCwd(str(Path("/tmp").resolve())),
        panel={},
    ))

    assert result["success"] is True
    assert coordinator.provider.observed_cwd == str(Path("/tmp").resolve())
    assert coordinator.provider.start_kwargs["target_message_id"] == "assistant-msg"
    assert coordinator.turn_manager.pid_stamps == [
        ("manager-session", "worker-del_123", 12345),
    ]
    assert coordinator.turn_manager.emits == ["manager-session"]
    assert any(event["type"] == "worker_complete" for event in events)
    status = delegation_status_store.read_status("del_123")
    assert status["status"] == "complete"
    assert status["result"]["success"] is True
    assert status["result"]["worker_session_id"] == "worker-session"


def test_run_delegation_locked_salvages_durable_complete_without_queue_terminal(monkeypatch):
    class Popen:
        pid = 12345

    class Provider:
        KIND = "claude"
        id = "provider"
        record = {"name": "Claude"}

        def __init__(self):
            self._runs = {}

        def start_run(self, *, run_id, queue, **kwargs):
            run_dir = Path(_TMP_HOME) / "runs" / run_id
            run_dir.mkdir(parents=True, exist_ok=True)
            jsonl_path = run_dir / "claude.jsonl"
            jsonl_path.write_text(
                json.dumps({"type": "assistant", "message": "durable result"}) + "\n",
                encoding="utf-8",
            )
            (run_dir / "backend_state.json").write_text(json.dumps({
                "jsonl_path": str(jsonl_path),
                "processed_byte": jsonl_path.stat().st_size,
            }), encoding="utf-8")
            (run_dir / "complete.json").write_text(json.dumps({
                "success": True,
                "session_id": "durable-agent",
                "token_usage": {"input_tokens": 1},
                "sdk_output": "durable result",
            }), encoding="utf-8")
            self._runs[run_id] = SimpleNamespace(
                popen=Popen(),
                run_dir=run_dir,
            )
            queue.put_nowait(_delegation.StreamEvent(
                type="session_discovered",
                data={"session_id": "durable-agent"},
            ))

        def cancel_turn(self, run_id):
            raise AssertionError("cancel_turn should not be called")

    class TurnManager:
        def __init__(self):
            self.active_run_ids = {}
            self.emits = []

        def run_state_set_pid(self, *_args):
            pass

        async def emit_run_state(self, app_session_id):
            self.emits.append(app_session_id)

        async def _publish_terminal_lifecycle(self, *_args, **_kwargs):
            pass

    class Coordinator:
        def __init__(self):
            self.turn_manager = TurnManager()
            self.internal_token = "token"
            self.provider = Provider()

        def provider_for_run(self, *_args):
            return self.provider

        async def broadcast_workers_changed(self, _cwd):
            pass

    monkeypatch.setattr(_delegation, "_compute_jsonl_read_path_off_loop", _fake_jsonl_path)
    monkeypatch.setattr(_delegation, "jsonl_byte_size", lambda _path: 12)
    monkeypatch.setattr(_delegation.llm_call_log, "append_call", lambda **_kwargs: None)

    manager = _delegation.session_manager.create(
        name="manager",
        cwd=_delegation.CanonicalDelegationCwd(str(Path(_TMP_HOME).resolve())),
        orchestration_mode="native",
        model="model",
        source="test",
    )
    worker = _delegation.session_manager.create(
        name="worker",
        cwd=_delegation.CanonicalDelegationCwd(str(Path(_TMP_HOME).resolve())),
        orchestration_mode="native",
        model="model",
        source="test",
    )
    _delegation.session_manager.set_agent_sid(
        worker["id"],
        "native",
        "worker-parent-agent",
        bump_updated_at=False,
    )

    events = []

    async def ws_callback(event):
        events.append(event)

    result = asyncio.run(_delegation.run_delegation_locked(
        Coordinator(),
        app_session_id=manager["id"],
        ws_callback=ws_callback,
        cancel_event=asyncio.Event(),
        delegation_id="del_durable",
        worker_run_id="worker-del_durable",
        instructions="do work",
        instructions_preview="do work",
        worker_agent_session_id=worker["id"],
        worker_session=_delegation.session_manager.get(worker["id"]) or {},
        worker_description="worker",
        worker_orchestration_mode="native",
        worker_parent_claude_sid="worker-parent-agent",
        session_is_registered_worker=False,
        target_message_id="assistant-msg",
        run_mode="fork",
        model="model",
        cwd=_TMP_HOME,
        panel={},
    ))

    assert result["success"] is True
    assert result["fork_agent_sid"] == "durable-agent"
    assert result["sdk_output"] == "durable result"
    assert any(event["type"] == "worker_complete" for event in events)
    status = delegation_status_store.read_status("del_durable")
    assert status["status"] == "complete"
    assert status["result"]["success"] is True
    assert status["result"]["sdk_output"] == "durable result"


def test_durable_complete_waits_for_claude_final_text_line():
    run_dir = Path(_TMP_HOME) / "runs" / "late-final-text"
    run_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = run_dir / "claude.jsonl"
    old_matching_line = json.dumps({
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "final answer"}]},
    }) + "\n"
    first_line = json.dumps({
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "draft"}]},
    }) + "\n"
    jsonl_path.write_text(old_matching_line, encoding="utf-8")
    start_offset = jsonl_path.stat().st_size
    with jsonl_path.open("a", encoding="utf-8") as fh:
        fh.write(first_line)
    (run_dir / "backend_state.json").write_text(json.dumps({
        "jsonl_path": str(jsonl_path),
        "processed_byte": jsonl_path.stat().st_size,
    }), encoding="utf-8")
    payload = {
        "success": True,
        "session_id": "durable-agent",
        "final_assistant_text": "final answer",
    }

    assert asyncio.run(
        _delegation._durable_provider_output_drained(
            run_dir,
            payload,
            start_offset,
        )
    ) is False

    with jsonl_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "type": "assistant",
            "message": {
                "content": [{"type": "text", "text": "final answer"}],
            },
        }) + "\n")
    (run_dir / "backend_state.json").write_text(json.dumps({
        "jsonl_path": str(jsonl_path),
        "processed_byte": jsonl_path.stat().st_size,
    }), encoding="utf-8")

    assert asyncio.run(
        _delegation._durable_provider_output_drained(
            run_dir,
            payload,
            start_offset,
        )
    ) is True


def test_ephemeral_delegate_fork_is_removed_after_completion(monkeypatch):
    class Popen:
        pid = 12345

    class Provider:
        KIND = "claude"
        record = {"name": "Claude"}

        def __init__(self):
            self._runs = {}

        def start_run(self, *, run_id, queue, **kwargs):
            self._runs[run_id] = SimpleNamespace(
                popen=Popen(),
                run_dir=Path(_TMP_HOME) / "runs" / run_id,
            )
            queue.put_nowait(_delegation.StreamEvent(
                type="session_discovered",
                data={"session_id": "ephemeral-agent"},
            ))
            queue.put_nowait(_delegation.StreamEvent(
                type="complete",
                data={"success": True, "session_id": "ephemeral-agent"},
            ))

        def cancel_turn(self, run_id):
            raise AssertionError("cancel_turn should not be called")

    class TurnManager:
        def __init__(self):
            self.active_run_ids = {}
            self.emits = []

        def run_state_set_pid(self, *_args):
            pass

        async def emit_run_state(self, app_session_id):
            self.emits.append(app_session_id)

        async def _publish_terminal_lifecycle(self, *_args, **_kwargs):
            pass

    class Coordinator:
        def __init__(self):
            self.turn_manager = TurnManager()
            self.internal_token = "token"
            self.provider = Provider()

        def provider_for_run(self, *_args):
            return self.provider

        async def broadcast_workers_changed(self, _cwd):
            pass

    monkeypatch.setattr(_delegation, "_compute_jsonl_read_path_off_loop", _fake_jsonl_path)
    monkeypatch.setattr(_delegation, "count_jsonl_lines", lambda _path: 1)
    monkeypatch.setattr(_delegation, "jsonl_byte_size", lambda _path: 0)

    manager = _delegation.session_manager.create(
        name="manager",
        cwd=_TMP_HOME,
        orchestration_mode="native",
        model="model",
        source="test",
    )
    worker = _delegation.session_manager.create(
        name="worker",
        cwd=_TMP_HOME,
        orchestration_mode="native",
        model="model",
        source="test",
    )
    _delegation.session_manager.set_agent_sid(
        worker["id"],
        "native",
        "worker-parent-agent",
        bump_updated_at=False,
    )

    events = []

    async def ws_callback(event):
        events.append(event)

    panel = {}
    result = asyncio.run(_delegation.run_delegation_locked(
        Coordinator(),
        app_session_id=manager["id"],
        ws_callback=ws_callback,
        cancel_event=asyncio.Event(),
        delegation_id="del_ephemeral",
        worker_run_id="worker-del_ephemeral",
        instructions="do work",
        instructions_preview="do work",
        worker_agent_session_id=worker["id"],
        worker_session=_delegation.session_manager.get(worker["id"]) or {},
        worker_description="worker",
        worker_orchestration_mode="native",
        worker_parent_claude_sid="worker-parent-agent",
        session_is_registered_worker=False,
        target_message_id="assistant-msg",
        run_mode="fork",
        model="model",
        cwd=_TMP_HOME,
        panel=panel,
        ephemeral=True,
    ))

    fork_session_id = panel["fork_agent_session_id"]
    assert result["success"] is True
    assert result["fork_agent_sid"] == "ephemeral-agent"
    assert result["jsonl_path"] == str(Path(_TMP_HOME) / "ephemeral-agent.jsonl")
    assert _delegation.session_manager.get(fork_session_id) is None
    manager_after = _delegation.session_manager.get(manager["id"]) or {}
    assert (
        manager_after.get("processed_line_by_sid") or {}
    ).get("ephemeral-agent") == 1


async def _fake_jsonl_path(*_args, **_kwargs):
    return Path(_TMP_HOME) / "ephemeral-agent.jsonl"
