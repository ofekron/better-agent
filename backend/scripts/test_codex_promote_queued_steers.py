#!/usr/bin/env python3

from __future__ import annotations

import asyncio
import base64
import os
from pathlib import Path
import shutil
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "backend"))

import _test_home
TMP_HOME = _test_home.isolate("bc-test-codex-steer-")

import orchestrator  # noqa: E402
import project_structure_edit_session  # noqa: E402
import config_store  # noqa: E402
from provider import _resolve_class  # noqa: E402
from orchestrator import Coordinator, build_semantic_alter_prompt  # noqa: E402


def _new_coord() -> Coordinator:
    coord = Coordinator.__new__(Coordinator)
    coord._active_prompt_client_ids = {}
    coord._prompt_client_id_by_item = {}
    return coord


def _configure_project_structure_runtime() -> None:
    provider = config_store.list_providers()["providers"][0]
    assignments = config_store.get_internal_llm_assignments()
    assignments["project_structure_edit"] = {
        "provider_id": provider["id"],
        "model": provider["default_model"],
        "reasoning_effort": provider.get("default_reasoning_effort") or "",
    }
    config_store.set_internal_llm_assignments(assignments)


class _SessionManager:
    def __init__(self) -> None:
        self.removed: list[tuple[str, str | None]] = []
        self.updated: list[tuple[str, str, dict]] = []
        self.sessions = {
            "sid": {
                "messages": [
                    {"id": "u1", "role": "user"},
                    {"id": "a1", "role": "assistant"},
                    {"id": "u2", "role": "user"},
                ],
            },
        }

    def remove_queued_prompt(self, sid: str, queued_id: str | None) -> None:
        self.removed.append((sid, queued_id))

    def update_queued_prompt(self, sid: str, queued_id: str, updates: dict) -> None:
        self.updated.append((sid, queued_id, updates))

    def get(self, sid: str) -> dict | None:
        return self.sessions.get(sid)


class _TurnManager:
    def __init__(self, save_callback=None) -> None:
        self.active_run_ids = {"sid": ["run-1"]}
        self._turn_save_callbacks = {}
        if save_callback is not None:
            self._turn_save_callbacks["sid"] = save_callback


class _Provider:
    supports_steering = True
    rewind_requires_agent_identity = True

    def __init__(self, transient_failures: int = 0) -> None:
        self._runs = {"run-1": object()}
        self.steered: list[tuple[str, str, list | None]] = []
        self.transient_failures = transient_failures

    def steer_run(self, run_id: str, prompt: str, images: list | None = None) -> bool:
        self.steered.append((run_id, prompt, images))
        if self.transient_failures > 0:
            self.transient_failures -= 1
            return False
        return True


async def _test_steer_active_turn_saves_in_turn_event() -> None:
    coord = _new_coord()
    saved: list[dict] = []
    dispatched: list[tuple[str, dict]] = []
    provider = _Provider()

    async def save_callback(event: dict) -> None:
        saved.append(event)

    async def dispatch_raw(app_session_id: str, event: dict) -> None:
        dispatched.append((app_session_id, event))

    def provider_for_session(app_session_id: str):
        assert app_session_id == "sid"
        return provider

    def init_turn_messages(**_kwargs):
        raise AssertionError("steer must not create a root user message")

    coord.turn_manager = _TurnManager(save_callback)
    coord.provider_for_session = provider_for_session  # type: ignore[method-assign]
    coord.dispatch_raw = dispatch_raw  # type: ignore[method-assign]
    coord._init_turn_messages = init_turn_messages  # type: ignore[method-assign]

    assert await coord.steer_active_turn(
        app_session_id="sid",
        prompt="model prompt",
        display_prompt="visible prompt",
        images=[{
            "data": base64.b64encode(b"png-bytes").decode("ascii"),
            "media_type": "image/png",
        }],
        client_id="client-1",
        lifecycle_msg_id="life-1",
    ) is True

    assert provider.steered == [("run-1", "model prompt", [{
        "data": base64.b64encode(b"png-bytes").decode("ascii"),
        "media_type": "image/png",
    }])]
    assert saved[0]["type"] == "steer_prompt"
    assert saved[0]["data"]["prompt"] == "visible prompt"
    assert saved[0]["data"]["client_id"] == "client-1"
    assert saved[0]["data"]["lifecycle_msg_id"] == "life-1"
    assert saved[0]["data"]["images"] == [{
        "filename": f"{saved[0]['data']['uuid']}_0.png",
        "media_type": "image/png",
    }]
    image_path = (
        Path(TMP_HOME)
        / "sessions"
        / "images"
        / "sid"
        / saved[0]["data"]["images"][0]["filename"]
    )
    assert image_path.read_bytes() == b"png-bytes"
    assert dispatched == [("sid", {
        "type": "steer_prompt_persisted",
        "data": {
            "app_session_id": "sid",
            "client_id": "client-1",
            "lifecycle_msg_id": "life-1",
        },
    })]


async def _test_steer_active_turn_waits_for_codex_turn_id() -> None:
    coord = _new_coord()
    saved: list[dict] = []
    dispatched: list[tuple[str, dict]] = []
    provider = _Provider(transient_failures=1)

    async def save_callback(event: dict) -> None:
        saved.append(event)

    async def dispatch_raw(app_session_id: str, event: dict) -> None:
        dispatched.append((app_session_id, event))

    def provider_for_session(app_session_id: str):
        assert app_session_id == "sid"
        return provider

    coord.turn_manager = _TurnManager(save_callback)
    coord.provider_for_session = provider_for_session  # type: ignore[method-assign]
    coord.dispatch_raw = dispatch_raw  # type: ignore[method-assign]

    original_interval = orchestrator._STEER_READY_RETRY_INTERVAL_SECONDS
    orchestrator._STEER_READY_RETRY_INTERVAL_SECONDS = 0
    try:
        assert await coord.steer_active_turn(
            app_session_id="sid",
            prompt="model prompt",
            display_prompt="visible prompt",
            images=None,
            client_id="client-1",
            lifecycle_msg_id="life-1",
        ) is True
    finally:
        orchestrator._STEER_READY_RETRY_INTERVAL_SECONDS = original_interval

    assert provider.steered == [
        ("run-1", "model prompt", None),
        ("run-1", "model prompt", None),
    ]
    assert saved[0]["type"] == "steer_prompt"
    assert dispatched[0][1]["type"] == "steer_prompt_persisted"


async def _test_promote_queued_steers_first_item() -> None:
    coord = _new_coord()
    coord._prompt_queues = {"sid": asyncio.Queue()}
    coord._queued_ids = {"sid": ["q1", "q2"]}
    await coord._prompt_queues["sid"].put({
        "_queued_id": "q1",
        "prompt": "visible",
        "cli_prompt": "model",
        "images": [{"data": "abc", "media_type": "image/png"}],
        "client_id": "client-1",
        "lifecycle_msg_id": "life-1",
    })
    await coord._prompt_queues["sid"].put({
        "_queued_id": "q2",
        "prompt": "next",
    })

    steered: list[dict] = []

    async def steer_active_turn(**kwargs):
        steered.append(kwargs)
        return True

    coord.steer_active_turn = steer_active_turn  # type: ignore[method-assign]
    fake_session_manager = _SessionManager()
    original_session_manager = orchestrator.session_manager
    orchestrator.session_manager = fake_session_manager  # type: ignore[assignment]
    try:
        assert await coord.promote_queued("sid", action="steer") is True
    finally:
        orchestrator.session_manager = original_session_manager

    assert steered == [{
        "app_session_id": "sid",
        "prompt": "model",
        "display_prompt": "visible",
        "images": [{"data": "abc", "media_type": "image/png"}],
        "client_id": "client-1",
        "lifecycle_msg_id": "life-1",
    }]
    assert coord._queued_ids == {"sid": ["q2"]}
    assert fake_session_manager.removed == [("sid", "q1")]
    remaining = await coord._prompt_queues["sid"].get()
    assert remaining["_queued_id"] == "q2"
    assert coord._prompt_queues["sid"].empty()


async def _test_promote_queued_steers_persisted_item_when_memory_queue_empty() -> None:
    coord = _new_coord()
    coord._prompt_queues = {}
    coord._queued_ids = {}
    fake_session_manager = _SessionManager()
    fake_session_manager.sessions["sid"]["queued_prompts"] = [{
        "id": "q1",
        "content": "visible",
        "cli_prompt": "model",
        "images": [{"data": "abc", "media_type": "image/png"}],
        "client_id": "client-1",
        "lifecycle_msg_id": "life-1",
        "capability_contexts": [],
    }]
    steered: list[dict] = []

    async def steer_active_turn(**kwargs):
        steered.append(kwargs)
        return True

    coord.steer_active_turn = steer_active_turn  # type: ignore[method-assign]
    original_session_manager = orchestrator.session_manager
    orchestrator.session_manager = fake_session_manager  # type: ignore[assignment]
    try:
        assert await coord.promote_queued("sid", action="steer") is True
    finally:
        orchestrator.session_manager = original_session_manager

    assert steered == [{
        "app_session_id": "sid",
        "prompt": "model",
        "display_prompt": "visible",
        "images": [{"data": "abc", "media_type": "image/png"}],
        "client_id": "client-1",
        "lifecycle_msg_id": "life-1",
    }]
    assert coord._queued_ids == {}
    assert fake_session_manager.removed == [("sid", "q1")]


async def _test_promote_queued_interrupts_first_item() -> None:
    coord = _new_coord()
    coord._prompt_queues = {"sid": asyncio.Queue()}
    coord._queued_ids = {"sid": ["q1"]}
    await coord._prompt_queues["sid"].put({
        "_queued_id": "q1",
        "prompt": "visible",
        "cli_prompt": "model",
        "lifecycle_msg_id": "life-1",
    })

    steered: list[dict] = []
    cancelled: list[dict] = []

    async def steer_active_turn(**kwargs):
        steered.append(kwargs)
        return True

    async def cancel_turn(app_session_id: str, interrupted_by_msg_id: str | None = None):
        cancelled.append({
            "app_session_id": app_session_id,
            "interrupted_by_msg_id": interrupted_by_msg_id,
        })
        return True

    coord.steer_active_turn = steer_active_turn  # type: ignore[method-assign]
    coord.cancel_turn = cancel_turn  # type: ignore[method-assign]

    assert await coord.promote_queued("sid", action="interrupt") is True

    assert steered == []
    assert cancelled == [{
        "app_session_id": "sid",
        "interrupted_by_msg_id": "life-1",
    }]
    remaining = await coord._prompt_queues["sid"].get()
    assert remaining["_queued_id"] == "q1"
    assert remaining["_interrupt"] is True
    assert coord._prompt_queues["sid"].empty()


async def _test_promote_queued_interrupts_selected_item() -> None:
    coord = _new_coord()
    coord._prompt_queues = {"sid": asyncio.Queue()}
    coord._queued_ids = {"sid": ["q1", "q2"]}
    await coord._prompt_queues["sid"].put({
        "_queued_id": "q1",
        "prompt": "first",
        "lifecycle_msg_id": "life-1",
    })
    await coord._prompt_queues["sid"].put({
        "_queued_id": "q2",
        "prompt": "second",
        "lifecycle_msg_id": "life-2",
    })

    cancelled: list[dict] = []

    async def cancel_turn(app_session_id: str, interrupted_by_msg_id: str | None = None):
        cancelled.append({
            "app_session_id": app_session_id,
            "interrupted_by_msg_id": interrupted_by_msg_id,
        })
        return True

    coord.cancel_turn = cancel_turn  # type: ignore[method-assign]

    assert await coord.promote_queued("sid", action="interrupt", queued_id="q2") is True

    assert cancelled == [{
        "app_session_id": "sid",
        "interrupted_by_msg_id": "life-2",
    }]
    first = await coord._prompt_queues["sid"].get()
    second = await coord._prompt_queues["sid"].get()
    assert first["_queued_id"] == "q2"
    assert first["_interrupt"] is True
    assert second["_queued_id"] == "q1"
    assert coord._prompt_queues["sid"].empty()


async def _test_normal_queued_prompts_batch_into_one_turn() -> None:
    coord = _new_coord()
    sid = "sid"
    coord._prompt_queues = {sid: asyncio.Queue()}
    coord._queued_ids = {sid: ["q1", "q2"]}
    coord._cancelled_ids = {}
    coord._in_flight_prompts = {}
    coord._processor_tasks = {}
    coord._session_cancelled = {}
    await coord._prompt_queues[sid].put({
        "_queued_id": "q1",
        "prompt": "first",
        "app_session_id": sid,
        "model": "m",
        "cwd": "/repo",
        "client_id": "client-1",
        "lifecycle_msg_id": "life-1",
        "images": [{"data": "img1"}],
        "files": [{"name": "a.txt"}],
        "capability_contexts": [{"source_id": "a"}],
    })
    await coord._prompt_queues[sid].put({
        "_queued_id": "q2",
        "prompt": "second",
        "app_session_id": sid,
        "model": "m",
        "cwd": "/repo",
        "client_id": "client-2",
        "lifecycle_msg_id": "life-2",
        "images": [{"data": "img2"}],
        "files": [{"name": "b.txt"}],
        "capability_contexts": [{"source_id": "b"}],
    })

    handled: list[dict] = []
    dispatched: list[dict] = []
    ran = asyncio.Event()

    class _ProcessorTurnManager:
        _pending_cancel = {}

        async def wait_for_clear_runs(self, _sid: str) -> None:
            pass

    class _ProcessorUserPromptManager:
        def set_in_flight_lifecycle_msg_id(self, *_args) -> None:
            pass

        def clear_in_flight_lifecycle_msg_id(self, *_args) -> None:
            pass

        def _clear_sent(self, *_args) -> None:
            pass

    async def dispatch_raw(_sid: str, event: dict) -> None:
        dispatched.append(event)

    async def handle_prompt(**kwargs) -> None:
        handled.append(kwargs)
        ran.set()

    fake_session_manager = _SessionManager()
    original_session_manager = orchestrator.session_manager
    orchestrator.session_manager = fake_session_manager  # type: ignore[assignment]
    coord.turn_manager = _ProcessorTurnManager()
    coord.user_prompt_manager = _ProcessorUserPromptManager()
    coord.dispatch_raw = dispatch_raw  # type: ignore[method-assign]
    coord.handle_prompt = handle_prompt  # type: ignore[method-assign]
    task = asyncio.create_task(coord._run_session_processor(sid))
    try:
        await asyncio.wait_for(ran.wait(), timeout=1)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        orchestrator.session_manager = original_session_manager

    assert len(handled) == 1
    assert handled[0]["prompt"] == "first\n\nsecond"
    assert handled[0]["images"] == [{"data": "img1"}, {"data": "img2"}]
    assert handled[0]["files"] == [{"name": "a.txt"}, {"name": "b.txt"}]
    assert handled[0]["capability_contexts"] == [
        {"source_id": "a"},
        {"source_id": "b"},
    ]
    assert [event["data"]["queued_id"] for event in dispatched] == ["q1", "q2"]
    assert fake_session_manager.removed == [(sid, "q1"), (sid, "q2")]


async def _test_update_latest_queued_alters_last_item_only() -> None:
    coord = _new_coord()
    coord._prompt_queues = {"sid": asyncio.Queue()}
    await coord._prompt_queues["sid"].put({
        "_queued_id": "q1",
        "prompt": "first visible",
        "cli_prompt": "first model",
        "client_id": "client-1",
    })
    await coord._prompt_queues["sid"].put({
        "_queued_id": "q2",
        "prompt": "second visible",
        "cli_prompt": "second model",
        "client_id": "client-2",
        "capability_contexts": [{"source_id": "old"}],
    })

    updated_id = await coord.update_latest_queued(
        "sid",
        "altered visible",
        "altered model",
        "client-3",
        "life-3",
        [{"source_id": "new"}],
    )

    assert updated_id == "q2"
    first = await coord._prompt_queues["sid"].get()
    second = await coord._prompt_queues["sid"].get()
    assert first == {
        "_queued_id": "q1",
        "prompt": "first visible",
        "cli_prompt": "first model",
        "client_id": "client-1",
    }
    assert second == {
        "_queued_id": "q2",
        "prompt": "altered visible",
        "cli_prompt": "altered model",
        "client_id": "client-3",
        "lifecycle_msg_id": "life-3",
        "capability_contexts": [{"source_id": "new"}],
    }
    assert coord._prompt_queues["sid"].empty()


async def _test_alter_rewind_runs_before_replacement_prompt() -> None:
    coord = _new_coord()
    coord._prompt_queues = {"sid": asyncio.Queue()}
    coord._queued_ids = {"sid": ["q1"]}
    coord._cancelled_ids = {}
    coord._in_flight_prompts = {}
    coord._processor_tasks = {}
    coord._session_cancelled = {}
    await coord._prompt_queues["sid"].put({
        "_queued_id": "q1",
        "prompt": "replacement",
        "app_session_id": "sid",
        "model": "m",
        "cwd": "/tmp",
        "_alter_rewind_latest": True,
    })

    calls: list[tuple[str, object]] = []
    handled = asyncio.Event()

    class _ProcessorTurnManager:
        _pending_cancel = {}

        async def wait_for_clear_runs(self, sid: str) -> None:
            calls.append(("wait", sid))

    class _ProcessorUserPromptManager:
        def set_in_flight_lifecycle_msg_id(self, *_args) -> None:
            pass

        def clear_in_flight_lifecycle_msg_id(self, *_args) -> None:
            pass

    async def dispatch_raw(sid: str, event: dict) -> None:
        calls.append(("dispatch", event.get("type")))

    async def rewind_files(sid: str, message_id: str, **kwargs) -> dict:
        calls.append(("rewind", {
            "message_id": message_id,
            "semantic_alter": kwargs.get("semantic_alter"),
        }))
        return {"semantic_alter_previous_prompt": "original"}

    async def handle_prompt(**kwargs) -> None:
        calls.append(("handle", kwargs.get("prompt")))
        calls.append(("cli_prompt", kwargs.get("cli_prompt")))
        handled.set()

    coord.turn_manager = _ProcessorTurnManager()
    coord.user_prompt_manager = _ProcessorUserPromptManager()
    coord.dispatch_raw = dispatch_raw  # type: ignore[method-assign]
    coord.rewind_files = rewind_files  # type: ignore[method-assign]
    coord.handle_prompt = handle_prompt  # type: ignore[method-assign]

    fake_session_manager = _SessionManager()
    original_session_manager = orchestrator.session_manager
    orchestrator.session_manager = fake_session_manager  # type: ignore[assignment]
    task = asyncio.create_task(coord._run_session_processor("sid"))
    try:
        await asyncio.wait_for(handled.wait(), timeout=1)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        orchestrator.session_manager = original_session_manager

    rewind_call = ("rewind", {"message_id": "u2", "semantic_alter": True})
    assert rewind_call in calls
    assert calls.index(rewind_call) < calls.index(("handle", "replacement"))
    cli_prompt = next(value for key, value in calls if key == "cli_prompt")
    assert isinstance(cli_prompt, str)
    assert '"previous_prompt": "original"' in cli_prompt
    assert cli_prompt.endswith("\n\nreplacement")


async def _test_rewind_files_supports_simulated_provider_without_agent_uuid() -> None:
    coord = _new_coord()
    calls: list[tuple[str, object]] = []

    class _SimulatedRewindProvider:
        supports_rewind = True
        rewind_requires_agent_identity = False

        async def rewind(self, rewind_session_id: str, message_uuid: str) -> None:
            calls.append(("rewind", (rewind_session_id, message_uuid)))

    class _RewindSessionManager:
        def __init__(self) -> None:
            self.sessions = {
                "sid": {
                    "agent_session_id": "provider-thread",
                    "messages": [
                        {"id": "u1", "role": "user", "agent_message_uuid": "native-u1"},
                        {"id": "a1", "role": "assistant"},
                        {"id": "u2", "role": "user", "agent_message_uuid": None},
                        {"id": "a2", "role": "assistant"},
                    ],
                },
            }

        def get(self, sid: str) -> dict | None:
            return self.sessions.get(sid)

        def truncate_messages(self, sid: str, keep_count: int) -> None:
            calls.append(("truncate", keep_count))
            self.sessions[sid]["messages"] = self.sessions[sid]["messages"][:keep_count]

    async def broadcast_session(sid: str, event_type: str, data: dict, **_kwargs) -> None:
        calls.append(("broadcast", (sid, event_type, [m["id"] for m in data["messages"]])))

    def provider_for_session(sid: str):
        assert sid == "sid"
        return _SimulatedRewindProvider()

    fake_session_manager = _RewindSessionManager()
    original_session_manager = orchestrator.session_manager
    orchestrator.session_manager = fake_session_manager  # type: ignore[assignment]
    coord.provider_for_session = provider_for_session  # type: ignore[method-assign]
    coord.broadcast_session = broadcast_session  # type: ignore[method-assign]
    try:
        result = await coord.rewind_files("sid", "u2")
    finally:
        orchestrator.session_manager = original_session_manager

    assert result["messages"] == fake_session_manager.sessions["sid"]["messages"]
    assert calls == [
        ("rewind", ("sid", "u2")),
        ("truncate", 2),
        ("broadcast", ("sid", "rewind_complete", ["u1", "a1"])),
    ]


async def _test_rewind_files_keeps_agent_identity_provider_strict() -> None:
    coord = _new_coord()
    calls: list[tuple[str, object]] = []

    class _AgentIdentityProvider:
        supports_rewind = True
        rewind_requires_agent_identity = True

        async def rewind(self, rewind_session_id: str, message_uuid: str) -> None:
            calls.append(("rewind", (rewind_session_id, message_uuid)))

    class _RewindSessionManager:
        def __init__(self) -> None:
            self.sessions = {
                "sid": {
                    "agent_session_id": "provider-thread",
                    "messages": [
                        {"id": "u1", "role": "user", "agent_message_uuid": "native-u1"},
                        {"id": "a1", "role": "assistant"},
                    ],
                },
            }

        def get(self, sid: str) -> dict | None:
            return self.sessions.get(sid)

        def truncate_messages(self, sid: str, keep_count: int) -> None:
            calls.append(("truncate", keep_count))
            self.sessions[sid]["messages"] = self.sessions[sid]["messages"][:keep_count]

    async def broadcast_session(sid: str, event_type: str, data: dict, **_kwargs) -> None:
        calls.append(("broadcast", (sid, event_type, [m["id"] for m in data["messages"]])))

    def provider_for_session(sid: str):
        assert sid == "sid"
        return _AgentIdentityProvider()

    fake_session_manager = _RewindSessionManager()
    original_session_manager = orchestrator.session_manager
    orchestrator.session_manager = fake_session_manager  # type: ignore[assignment]
    coord.provider_for_session = provider_for_session  # type: ignore[method-assign]
    coord.broadcast_session = broadcast_session  # type: ignore[method-assign]
    try:
        await coord.rewind_files("sid", "u1")
    finally:
        orchestrator.session_manager = original_session_manager

    assert calls == [
        ("rewind", ("provider-thread", "native-u1")),
        ("truncate", 0),
        ("broadcast", ("sid", "rewind_complete", [])),
    ]


async def _test_rewind_files_fails_closed_when_provider_does_not_support_rewind() -> None:
    coord = _new_coord()
    calls: list[tuple[str, object]] = []

    class _UnsupportedRewindProvider:
        supports_rewind = False
        rewind_requires_agent_identity = False

        async def rewind(self, *_args) -> None:
            raise AssertionError("unsupported provider rewind must not be called")

    class _RewindSessionManager:
        def __init__(self) -> None:
            self.sessions = {
                "sid": {
                    "agent_session_id": "provider-thread",
                    "messages": [
                        {"id": "u1", "role": "user", "agent_message_uuid": None},
                        {"id": "a1", "role": "assistant"},
                    ],
                },
            }

        def get(self, sid: str) -> dict | None:
            return self.sessions.get(sid)

        def truncate_messages(self, sid: str, keep_count: int) -> None:
            calls.append(("truncate", keep_count))

    async def broadcast_session(*_args, **_kwargs) -> None:
        raise AssertionError("unsupported rewind must not broadcast")

    def provider_for_session(sid: str):
        assert sid == "sid"
        return _UnsupportedRewindProvider()

    fake_session_manager = _RewindSessionManager()
    original_session_manager = orchestrator.session_manager
    orchestrator.session_manager = fake_session_manager  # type: ignore[assignment]
    coord.provider_for_session = provider_for_session  # type: ignore[method-assign]
    coord.broadcast_session = broadcast_session  # type: ignore[method-assign]
    try:
        try:
            await coord.rewind_files("sid", "u1")
        except ValueError as exc:
            assert str(exc) == "Provider does not support rewind"
        else:
            raise AssertionError("unsupported provider rewind must fail closed")
    finally:
        orchestrator.session_manager = original_session_manager

    assert calls == []


async def _test_rewind_files_allows_semantic_alter_without_provider_rewind() -> None:
    coord = _new_coord()
    calls: list[tuple[str, object]] = []

    class _SemanticAlterProvider:
        supports_rewind = False
        rewind_requires_agent_identity = True
        supports_semantic_alter = True

        async def rewind(self, *_args) -> None:
            raise AssertionError("semantic alter must not call provider rewind")

    class _RewindSessionManager:
        def __init__(self) -> None:
            self.sessions = {
                "sid": {
                    "agent_session_id": "provider-thread",
                    "messages": [
                        {
                            "id": "u1",
                            "role": "user",
                            "content": "original prompt",
                            "agent_message_uuid": None,
                        },
                        {"id": "a1", "role": "assistant"},
                    ],
                },
            }

        def get(self, sid: str) -> dict | None:
            return self.sessions.get(sid)

        def truncate_messages(self, sid: str, keep_count: int) -> None:
            calls.append(("truncate", keep_count))
            self.sessions[sid]["messages"] = self.sessions[sid]["messages"][:keep_count]

    async def broadcast_session(sid: str, event_type: str, data: dict, **_kwargs) -> None:
        calls.append(("broadcast", (sid, event_type, [m["id"] for m in data["messages"]])))

    def provider_for_session(sid: str):
        assert sid == "sid"
        return _SemanticAlterProvider()

    fake_session_manager = _RewindSessionManager()
    original_session_manager = orchestrator.session_manager
    orchestrator.session_manager = fake_session_manager  # type: ignore[assignment]
    coord.provider_for_session = provider_for_session  # type: ignore[method-assign]
    coord.broadcast_session = broadcast_session  # type: ignore[method-assign]
    try:
        result = await coord.rewind_files("sid", "u1", semantic_alter=True)
    finally:
        orchestrator.session_manager = original_session_manager

    assert result["semantic_alter_previous_prompt"] == "original prompt"
    assert calls == [
        ("truncate", 0),
        ("broadcast", ("sid", "rewind_complete", [])),
    ]


def _test_build_semantic_alter_prompt_tags_replacement() -> None:
    prompt = build_semantic_alter_prompt("old", "new")
    assert prompt.startswith("<user-alter-request>")
    assert '"previous_prompt": "old"' in prompt
    assert '"replacement_prompt": "new"' in prompt
    assert prompt.endswith("\n\nnew")


def _test_real_provider_rewind_identity_defaults() -> None:
    codex_cls = _resolve_class("codex")
    gemini_cls = _resolve_class("gemini")
    agy_cls = _resolve_class("agy")
    claude_cls = _resolve_class("claude")

    assert codex_cls.supports_rewind is True
    assert codex_cls.rewind_requires_agent_identity is False
    assert gemini_cls.supports_rewind is True
    assert gemini_cls.rewind_requires_agent_identity is False
    assert agy_cls.supports_rewind is False
    assert agy_cls.rewind_requires_agent_identity is True
    assert agy_cls.supports_semantic_alter is True
    assert claude_cls.supports_rewind is True
    assert claude_cls.rewind_requires_agent_identity is True


async def _test_project_structure_queue_routes_to_maintainer() -> None:
    _configure_project_structure_runtime()
    coord = _new_coord()
    sid = project_structure_edit_session.EDIT_SINGLETON_ID
    coord._prompt_queues = {sid: asyncio.Queue()}
    coord._queued_ids = {sid: ["q1"]}
    coord._cancelled_ids = {}
    coord._in_flight_prompts = {}
    coord._processor_tasks = {}
    coord._session_cancelled = {}
    await coord._prompt_queues[sid].put({
        "_queued_id": "q1",
        "prompt": "visible apply",
        "cli_prompt": "model apply",
        "app_session_id": sid,
        "model": "m",
        "cwd": "/repo",
        "client_id": "client-ps",
        "lifecycle_msg_id": "life-ps",
    })

    calls: list[tuple[str, object]] = []
    routed = asyncio.Event()

    class _ProcessorTurnManager:
        _pending_cancel = {}

        async def wait_for_clear_runs(self, app_session_id: str) -> None:
            calls.append(("wait", app_session_id))

    class _ProcessorUserPromptManager:
        def set_in_flight_lifecycle_msg_id(self, *_args) -> None:
            pass

        def clear_in_flight_lifecycle_msg_id(self, *_args) -> None:
            pass

        def _clear_sent(self, *_args) -> None:
            pass

    async def dispatch_raw(app_session_id: str, event: dict) -> None:
        calls.append(("dispatch", event.get("type")))

    async def handle_prompt(**_kwargs) -> None:
        raise AssertionError("project-structure queue must not run native handle_prompt")

    async def submit_user_prompt(project_cwd: str, prompt: str, **kwargs) -> dict:
        calls.append(("submit_user_prompt", {
            "project_cwd": project_cwd,
            "prompt": prompt,
            "client_id": kwargs.get("client_id"),
            "lifecycle_msg_id": kwargs.get("lifecycle_msg_id"),
        }))
        await kwargs["on_user_message"]({"id": "user-ps", "role": "user"})
        routed.set()
        return {"status": "ok", "queued_id": "maintainer-q"}

    coord.turn_manager = _ProcessorTurnManager()
    coord.user_prompt_manager = _ProcessorUserPromptManager()
    coord.dispatch_raw = dispatch_raw  # type: ignore[method-assign]
    coord.handle_prompt = handle_prompt  # type: ignore[method-assign]

    fake_session_manager = _SessionManager()
    original_session_manager = orchestrator.session_manager
    original_runtime_ready = project_structure_edit_session.extension_store.runtime_not_ready_message
    original_get_cwd = project_structure_edit_session.get_singleton_project_cwd
    original_find = project_structure_edit_session.find_user_message_by_client_id
    original_submit = project_structure_edit_session.submit_user_prompt
    orchestrator.session_manager = fake_session_manager  # type: ignore[assignment]
    project_structure_edit_session.extension_store.runtime_not_ready_message = lambda _id: None
    project_structure_edit_session.get_singleton_project_cwd = lambda fallback: "/repo"
    project_structure_edit_session.find_user_message_by_client_id = lambda _client_id: None
    project_structure_edit_session.submit_user_prompt = submit_user_prompt
    task = asyncio.create_task(coord._run_session_processor(sid))
    try:
        await asyncio.wait_for(routed.wait(), timeout=1)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        orchestrator.session_manager = original_session_manager
        project_structure_edit_session.extension_store.runtime_not_ready_message = original_runtime_ready
        project_structure_edit_session.get_singleton_project_cwd = original_get_cwd
        project_structure_edit_session.find_user_message_by_client_id = original_find
        project_structure_edit_session.submit_user_prompt = original_submit

    submit_calls = [call for call in calls if call[0] == "submit_user_prompt"]
    assert submit_calls == [("submit_user_prompt", {
        "project_cwd": "/repo",
        "prompt": "model apply",
        "client_id": "client-ps",
        "lifecycle_msg_id": "life-ps",
    })]
    assert ("dispatch", "user_message_persisted") in calls
    assert fake_session_manager.removed == [(sid, "q1")]


async def _test_project_structure_queue_duplicate_acks_existing_message() -> None:
    _configure_project_structure_runtime()
    coord = _new_coord()
    sid = project_structure_edit_session.EDIT_SINGLETON_ID
    coord._prompt_queues = {sid: asyncio.Queue()}
    coord._queued_ids = {sid: ["q1"]}
    coord._cancelled_ids = {}
    coord._in_flight_prompts = {}
    coord._processor_tasks = {}
    coord._session_cancelled = {}
    await coord._prompt_queues[sid].put({
        "_queued_id": "q1",
        "prompt": "retry",
        "app_session_id": sid,
        "model": "m",
        "cwd": "/repo",
        "client_id": "client-dupe",
        "lifecycle_msg_id": "life-dupe",
    })

    events: list[dict] = []
    acked = asyncio.Event()

    class _ProcessorTurnManager:
        _pending_cancel = {}

        async def wait_for_clear_runs(self, _sid: str) -> None:
            pass

    class _ProcessorUserPromptManager:
        def set_in_flight_lifecycle_msg_id(self, *_args) -> None:
            pass

        def clear_in_flight_lifecycle_msg_id(self, *_args) -> None:
            pass

        def _clear_sent(self, *_args) -> None:
            pass

    async def dispatch_raw(_sid: str, event: dict) -> None:
        events.append(event)
        if event.get("type") == "user_message_persisted":
            acked.set()

    async def handle_prompt(**_kwargs) -> None:
        raise AssertionError("duplicate project-structure retry must not run native")

    async def submit_user_prompt(*_args, **_kwargs) -> dict:
        raise AssertionError("duplicate project-structure retry must not resubmit")

    coord.turn_manager = _ProcessorTurnManager()
    coord.user_prompt_manager = _ProcessorUserPromptManager()
    coord.dispatch_raw = dispatch_raw  # type: ignore[method-assign]
    coord.handle_prompt = handle_prompt  # type: ignore[method-assign]

    fake_session_manager = _SessionManager()
    original_session_manager = orchestrator.session_manager
    original_runtime_ready = project_structure_edit_session.extension_store.runtime_not_ready_message
    original_find = project_structure_edit_session.find_user_message_by_client_id
    original_submit = project_structure_edit_session.submit_user_prompt
    orchestrator.session_manager = fake_session_manager  # type: ignore[assignment]
    existing = {"id": "user-existing", "role": "user", "client_id": "client-dupe"}
    project_structure_edit_session.extension_store.runtime_not_ready_message = lambda _id: None
    project_structure_edit_session.find_user_message_by_client_id = (
        lambda _client_id: existing
    )
    project_structure_edit_session.submit_user_prompt = submit_user_prompt
    task = asyncio.create_task(coord._run_session_processor(sid))
    try:
        await asyncio.wait_for(acked.wait(), timeout=1)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        orchestrator.session_manager = original_session_manager
        project_structure_edit_session.extension_store.runtime_not_ready_message = original_runtime_ready
        project_structure_edit_session.find_user_message_by_client_id = original_find
        project_structure_edit_session.submit_user_prompt = original_submit

    persisted = [e for e in events if e.get("type") == "user_message_persisted"]
    assert persisted[-1]["data"]["user_message"] == existing
    assert fake_session_manager.removed == [(sid, "q1")]


async def _test_unregistered_virtual_session_prompt_is_not_special() -> None:
    coord = _new_coord()
    events: list[dict] = []

    async def dispatch_ws(event: dict) -> None:
        events.append(event)

    handled = await coord._handle_special_session_prompt(
        "virtual:ofek-dev.unknown:test",
        {"client_id": "client-unregistered"},
        lifecycle_msg_id="life-unregistered",
        dispatch_ws=dispatch_ws,
    )

    assert handled is False
    assert events == []


def main() -> None:
    try:
        asyncio.run(_test_steer_active_turn_saves_in_turn_event())
        asyncio.run(_test_steer_active_turn_waits_for_codex_turn_id())
        asyncio.run(_test_promote_queued_steers_first_item())
        asyncio.run(_test_promote_queued_steers_persisted_item_when_memory_queue_empty())
        asyncio.run(_test_promote_queued_interrupts_first_item())
        asyncio.run(_test_promote_queued_interrupts_selected_item())
        asyncio.run(_test_normal_queued_prompts_batch_into_one_turn())
        asyncio.run(_test_update_latest_queued_alters_last_item_only())
        asyncio.run(_test_alter_rewind_runs_before_replacement_prompt())
        asyncio.run(_test_rewind_files_supports_simulated_provider_without_agent_uuid())
        asyncio.run(_test_rewind_files_keeps_agent_identity_provider_strict())
        asyncio.run(_test_rewind_files_fails_closed_when_provider_does_not_support_rewind())
        asyncio.run(_test_rewind_files_allows_semantic_alter_without_provider_rewind())
        _test_build_semantic_alter_prompt_tags_replacement()
        _test_real_provider_rewind_identity_defaults()
        asyncio.run(_test_project_structure_queue_routes_to_maintainer())
        asyncio.run(_test_project_structure_queue_duplicate_acks_existing_message())
        asyncio.run(_test_unregistered_virtual_session_prompt_is_not_special())
        print("PASS: queued prompt steering/promotion/alteration")
    finally:
        shutil.rmtree(TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    main()
