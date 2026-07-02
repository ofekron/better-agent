from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-mssg-")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import Coordinator
from orchs.manager import bootstrap
from session_manager import manager as session_manager
import config_store
import team_messaging


def teardown_module():
    shutil.rmtree(_TMP_HOME, ignore_errors=True)


def test_team_context_uses_session_id_and_self_identity():
    prompt = bootstrap.build_wrapped_prompt(
        "/repo",
        "do work",
        False,
        known_workers=[{
            "agent_session_id": "mssg-worker-session",
            "registry_cwd": "/repo",
            "orchestration_mode": "native",
            "node_id": "primary",
            "delegation_count": 1,
            "description": "implementation worker",
        }],
        self_session_id="mssg-manager-session",
        self_role="manager",
        self_description="main manager",
        manager_session_id="mssg-manager-session",
        manager_description="main manager",
    )

    assert "<session_id>mssg-manager-session</session_id>" in prompt
    assert '<member session_id="mssg-manager-session" role="manager"' in prompt
    assert '<member session_id="mssg-worker-session" role="worker"' in prompt
    assert "target_session_id" in prompt
    assert "<member id=" not in prompt


def test_queue_payload_has_minimal_metadata():
    metadata = {
        "sender_session_id": "sender",
    }

    payload = team_messaging.queue_payload(
        queue_item_id="queued-1",
        sender_session_id="sender",
        message="hello",
        metadata=metadata,
        lifecycle_msg_id="life-1",
    )

    assert payload["source"] == "mssg"
    assert payload["sender_session_id"] == "sender"
    assert payload["content"] == "hello"
    assert payload["cli_prompt"].startswith('<mssg sender_session_id="sender"')
    assert "</mssg>" in payload["cli_prompt"]
    assert "<team_message" not in payload["cli_prompt"]
    assert 'sender_session_id="sender"' in payload["cli_prompt"]
    assert "sender_role" not in payload["cli_prompt"]
    assert "sender_description" not in payload["cli_prompt"]
    assert "created_at" not in payload["cli_prompt"]
    assert "target_session_id" not in payload
    assert "kind" not in payload
    assert "idempotency_key" not in payload


def test_message_metadata_uses_field_reads_not_full_session_copy(monkeypatch):
    sender = session_manager.create(
        name="sender",
        cwd="/repo/sender",
        orchestration_mode="native",
    )
    target = session_manager.create(
        name="target",
        cwd="/repo/target",
        orchestration_mode="native",
    )

    def fail_get(_sid: str):
        raise AssertionError("metadata hot path must not deepcopy full sessions")

    monkeypatch.setattr(team_messaging.session_manager, "get", fail_get)
    metadata = team_messaging.build_message_metadata(
        sender_session_id=sender["id"],
        target_session_id=target["id"],
    )

    assert metadata == {
        "sender_session_id": sender["id"],
        "sender_cwd": "/repo/sender",
    }


def test_plain_native_target_gets_no_invented_team_context(monkeypatch):
    sender = session_manager.create(
        name="plain sender",
        cwd="/repo-plain",
        orchestration_mode="native",
    )
    target = session_manager.create(
        name="plain native target",
        cwd="/repo-plain",
        orchestration_mode="native",
    )
    # Unrelated workers in the same cwd must not become an invented team.
    monkeypatch.setattr(
        team_messaging.worker_store,
        "list_worker_projection",
        lambda _cwd, limit=20: [{
            "agent_session_id": "unrelated-worker",
            "description": "someone else's worker",
        }],
    )

    prompt = team_messaging.format_team_message_prompt(
        "hello",
        {"sender_session_id": sender["id"]},
        target_session_id=target["id"],
    )

    assert "<team>" not in prompt
    assert "unrelated-worker" not in prompt
    assert 'role="manager"' not in prompt


def test_worker_target_still_gets_real_team_context(monkeypatch):
    target = session_manager.create(
        name="worker target",
        cwd="/repo-worker",
        orchestration_mode="native",
    )
    monkeypatch.setattr(
        team_messaging.worker_store,
        "list_worker_projection",
        lambda _cwd, limit=20: [{
            "agent_session_id": target["id"],
            "description": "implementation worker",
        }],
    )

    prompt = team_messaging.format_team_message_prompt(
        "hello",
        {"sender_session_id": "sender"},
        target_session_id=target["id"],
    )

    assert "<team>" in prompt
    assert f'session_id="{target["id"]}" role="worker"' in prompt


def test_submit_team_message_persists_queue_and_submits(monkeypatch):
    sender = session_manager.create(
        name="implementation worker",
        cwd="/repo",
        orchestration_mode="native",
    )
    target = session_manager.create(
        name="main manager",
        cwd="/repo",
        orchestration_mode="manager",
    )
    coordinator = Coordinator()
    captured: dict = {}

    def fake_submit_prompt(sid: str, params: dict, **_kwargs) -> str:
        captured["sid"] = sid
        captured["params"] = params
        return params["_queued_id"]

    monkeypatch.setattr(coordinator, "submit_prompt", fake_submit_prompt)

    async def run() -> dict:
        return await coordinator.submit_team_message(
            sender_session_id=sender["id"],
            target_session_id=target["id"],
            message="status update",
        )

    result = asyncio.run(run())

    assert result["success"] is True
    assert captured["sid"] == target["id"]
    assert captured["params"]["app_session_id"] == target["id"]
    assert captured["params"]["source"] == "mssg"
    assert captured["params"]["team_message"]["metadata"]["sender_session_id"] == sender["id"]
    assert "sender_role" not in captured["params"]["team_message"]["metadata"]
    assert "sender_description" not in captured["params"]["team_message"]["metadata"]
    assert "created_at" not in captured["params"]["team_message"]["metadata"]
    assert f"<session_id>{target['id']}</session_id>" in captured["params"]["cli_prompt"]
    assert '<team>' in captured["params"]["cli_prompt"]
    assert f'<member session_id="{target["id"]}" role="manager"' in captured["params"]["cli_prompt"]
    queued = session_manager.get(target["id"])["queued_prompts"]
    assert len(queued) == 1
    assert queued[0]["sender_session_id"] == sender["id"]
    assert queued[0]["content"] == "status update"
    assert f"<session_id>{target['id']}</session_id>" in queued[0]["cli_prompt"]
    assert '<team>' in queued[0]["cli_prompt"]
    assert "target_session_id" not in queued[0]
    assert "kind" not in queued[0]
    assert "idempotency_key" not in queued[0]


def test_assistant_self_message_uses_update_source(monkeypatch):
    assistant = session_manager.create(
        name="Assistant",
        cwd="/repo",
        orchestration_mode="native",
        source="extension",
    )
    coordinator = Coordinator()
    captured: dict = {}

    def fake_submit_prompt(sid: str, params: dict, **_kwargs) -> str:
        captured["sid"] = sid
        captured["params"] = params
        return params["_queued_id"]

    monkeypatch.setattr(coordinator, "submit_prompt", fake_submit_prompt)

    result = asyncio.run(coordinator.submit_team_message(
        sender_session_id=assistant["id"],
        target_session_id=assistant["id"],
        message="wake up",
        detach=True,
    ))

    assert result["success"] is True
    assert captured["sid"] == assistant["id"]
    assert captured["params"]["source"] == team_messaging.UPDATE_SOURCE
    assert captured["params"]["team_message"]["metadata"]["sender_session_id"] == assistant["id"]
    queued = session_manager.get(assistant["id"])["queued_prompts"]
    assert queued[0]["source"] == team_messaging.UPDATE_SOURCE
    assert queued[0]["sender_session_id"] == assistant["id"]


def test_submit_team_message_take_latest_collapse_keeps_one_waiting_message(monkeypatch):
    assistant = session_manager.create(
        name="Assistant",
        cwd="/repo",
        orchestration_mode="native",
        source="extension",
    )
    coordinator = Coordinator()
    submitted: list[dict] = []

    async def fake_submit_prompt_async(sid: str, params: dict, **_kwargs) -> str:
        q = coordinator._prompt_queues.setdefault(sid, asyncio.Queue())
        q.put_nowait(dict(params))
        coordinator._queued_ids.setdefault(sid, []).append(params["_queued_id"])
        submitted.append(params)
        return params["_queued_id"]

    monkeypatch.setattr(coordinator, "submit_prompt_async", fake_submit_prompt_async)

    first = asyncio.run(coordinator.submit_team_message(
        sender_session_id=assistant["id"],
        target_session_id=assistant["id"],
        message="wake count 16",
        detach=True,
        collapse_key="assistant-waker",
        collapse_policy="take_latest",
    ))
    second = asyncio.run(coordinator.submit_team_message(
        sender_session_id=assistant["id"],
        target_session_id=assistant["id"],
        message="wake count 17",
        detach=True,
        collapse_key="assistant-waker",
        collapse_policy="take_latest",
    ))

    assert first["queued_id"] == second["queued_id"]
    assert second["collapsed"] is True
    assert len(submitted) == 1
    queued = session_manager.get(assistant["id"])["queued_prompts"]
    assert len(queued) == 1
    assert queued[0]["id"] == first["queued_id"]
    assert queued[0]["content"] == "wake count 17"
    assert queued[0]["collapse_key"] == "assistant-waker"
    q = coordinator._prompt_queues[assistant["id"]]
    pending = q.get_nowait()
    assert pending["_queued_id"] == first["queued_id"]
    assert pending["prompt"] == "wake count 17"
    assert pending["team_message"]["message"] == "wake count 17"


def test_submit_team_message_collapse_key_does_not_replace_active_turn(monkeypatch):
    assistant = session_manager.create(
        name="Assistant active",
        cwd="/repo",
        orchestration_mode="native",
        source="extension",
    )
    coordinator = Coordinator()
    submitted: list[dict] = []

    async def fake_submit_prompt_async(sid: str, params: dict, **_kwargs) -> str:
        q = coordinator._prompt_queues.setdefault(sid, asyncio.Queue())
        q.put_nowait(dict(params))
        coordinator._queued_ids.setdefault(sid, []).append(params["_queued_id"])
        submitted.append(params)
        return params["_queued_id"]

    monkeypatch.setattr(coordinator, "submit_prompt_async", fake_submit_prompt_async)

    first = asyncio.run(coordinator.submit_team_message(
        sender_session_id=assistant["id"],
        target_session_id=assistant["id"],
        message="wake count 16",
        detach=True,
        collapse_key="assistant-waker",
        collapse_policy="take_latest",
    ))
    q = coordinator._prompt_queues[assistant["id"]]
    active = q.get_nowait()
    session_manager.remove_queued_prompt(assistant["id"], active["_queued_id"])

    second = asyncio.run(coordinator.submit_team_message(
        sender_session_id=assistant["id"],
        target_session_id=assistant["id"],
        message="wake count 17",
        detach=True,
        collapse_key="assistant-waker",
        collapse_policy="take_latest",
    ))

    assert first["queued_id"] != second["queued_id"]
    assert "collapsed" not in second
    assert len(submitted) == 2
    queued = session_manager.get(assistant["id"])["queued_prompts"]
    assert len(queued) == 1
    assert queued[0]["id"] == second["queued_id"]
    assert queued[0]["content"] == "wake count 17"


def test_prompt_processor_strips_collapse_metadata_before_handle_prompt(monkeypatch):
    assistant = session_manager.create(
        name="Assistant processor collapse",
        cwd="/repo",
        orchestration_mode="native",
        source="extension",
    )
    coordinator = Coordinator()
    captured: dict = {}
    q: asyncio.Queue = asyncio.Queue()
    q.put_nowait({
        "_queued_id": "queued-collapse",
        "app_session_id": assistant["id"],
        "prompt": "wake count 18",
        "cli_prompt": "wrapped wake count 18",
        "provider_id": "",
        "model": "sonnet",
        "reasoning_effort": "",
        "allow_model_override": True,
        "cwd": "/repo",
        "orchestration_mode": "native",
        "source": team_messaging.UPDATE_SOURCE,
        "user_initiated": False,
        "team_message": {
            "message": "wake count 18",
            "metadata": {"sender_session_id": assistant["id"]},
        },
        "collapse_key": "assistant-waker",
        "collapse_policy": "take_latest",
    })
    q.put_nowait(None)
    coordinator._prompt_queues[assistant["id"]] = q
    coordinator._queued_ids[assistant["id"]] = ["queued-collapse"]

    async def fake_dispatch_raw(_sid: str, _event: dict) -> None:
        return None

    async def strict_handle_prompt(
        *,
        prompt: str,
        app_session_id: str,
        model: str,
        cwd: str,
        ws_callback,
        provider_id=None,
        reasoning_effort=None,
        images=None,
        files=None,
        orchestration_mode=None,
        client_id=None,
        send_target=None,
        cli_prompt=None,
        source=None,
        user_initiated=True,
        disallowed_tools=None,
        known_worker_registry_cwds=None,
        queue_item_id=None,
        team_message=None,
        capability_contexts=None,
        file_discussion_id=None,
        allow_model_override=False,
    ) -> None:
        captured.update({
            "prompt": prompt,
            "app_session_id": app_session_id,
            "model": model,
            "cwd": cwd,
            "cli_prompt": cli_prompt,
            "source": source,
            "queue_item_id": queue_item_id,
            "team_message": team_message,
            "allow_model_override": allow_model_override,
        })

    monkeypatch.setattr(coordinator, "dispatch_raw", fake_dispatch_raw)
    monkeypatch.setattr(coordinator, "handle_prompt", strict_handle_prompt)

    asyncio.run(coordinator._run_session_processor(assistant["id"]))

    assert captured["prompt"] == "wake count 18"
    assert captured["app_session_id"] == assistant["id"]
    assert captured["queue_item_id"] == "queued-collapse"
    assert captured["source"] == team_messaging.UPDATE_SOURCE
    assert captured["team_message"]["message"] == "wake count 18"
    assert captured["allow_model_override"] is True


def test_promotion_requeue_preserves_team_message_collapse_metadata():
    assistant = session_manager.create(
        name="Assistant promotion collapse",
        cwd="/repo",
        orchestration_mode="native",
        source="extension",
    )
    queue_item = team_messaging.queue_payload(
        queue_item_id="queued-promotion-collapse",
        sender_session_id=assistant["id"],
        message="wake count 19",
        metadata={"sender_session_id": assistant["id"]},
        lifecycle_msg_id="life-promotion-collapse",
        target_session_id=assistant["id"],
        source=team_messaging.UPDATE_SOURCE,
        collapse_key="assistant-waker",
        collapse_policy=team_messaging.COLLAPSE_POLICY_TAKE_LATEST,
    )
    session_manager.add_queued_prompt(assistant["id"], queue_item)
    coordinator = Coordinator()

    coordinator._queue_persisted_prompts_for_promotion(assistant["id"])

    pending = coordinator._prompt_queues[assistant["id"]].get_nowait()
    assert pending["_queued_id"] == "queued-promotion-collapse"
    assert pending["source"] == team_messaging.UPDATE_SOURCE
    assert pending["team_message"]["message"] == "wake count 19"
    assert pending["team_message"]["metadata"]["sender_session_id"] == assistant["id"]
    assert pending["collapse_key"] == "assistant-waker"
    assert pending["collapse_policy"] == team_messaging.COLLAPSE_POLICY_TAKE_LATEST


def test_startup_reenqueue_preserves_team_message_collapse_metadata(monkeypatch):
    import main

    assistant = session_manager.create(
        name="Assistant startup collapse",
        cwd="/repo",
        orchestration_mode="native",
        source="extension",
    )
    queue_item = team_messaging.queue_payload(
        queue_item_id="queued-startup-collapse",
        sender_session_id=assistant["id"],
        message="wake count 20",
        metadata={"sender_session_id": assistant["id"]},
        lifecycle_msg_id="life-startup-collapse",
        target_session_id=assistant["id"],
        source=team_messaging.UPDATE_SOURCE,
        collapse_key="assistant-waker",
        collapse_policy=team_messaging.COLLAPSE_POLICY_TAKE_LATEST,
    )
    session_manager.add_queued_prompt(assistant["id"], queue_item)
    captured: list[tuple[str, dict]] = []

    class FakeCoordinator:
        async def submit_prompt_async(self, sid: str, params: dict) -> str:
            captured.append((sid, params))
            return params["_queued_id"]

    monkeypatch.setattr(main, "coordinator", FakeCoordinator())

    asyncio.run(main._re_enqueue_queued_prompts())

    matching = [
        (sid, params)
        for sid, params in captured
        if params.get("_queued_id") == "queued-startup-collapse"
    ]
    assert len(matching) == 1
    sid, params = matching[0]
    assert sid == assistant["id"]
    assert params["_queued_id"] == "queued-startup-collapse"
    assert params["source"] == team_messaging.UPDATE_SOURCE
    assert params["team_message"]["message"] == "wake count 20"
    assert params["team_message"]["metadata"]["sender_session_id"] == assistant["id"]
    assert params["collapse_key"] == "assistant-waker"
    assert params["collapse_policy"] == team_messaging.COLLAPSE_POLICY_TAKE_LATEST


def test_session_activity_snapshot_reports_running_and_queued(monkeypatch):
    import main

    assistant = session_manager.create(
        name="Assistant activity",
        cwd="/repo",
        orchestration_mode="native",
        source="extension",
    )
    coordinator = Coordinator()
    sid = assistant["id"]
    coordinator._queued_ids[sid] = ["queued-1"]
    coordinator.turn_manager._cached_running.add(sid)
    coordinator.turn_manager._cached_monitoring[sid] = "active"
    monkeypatch.setattr(main, "coordinator", coordinator)

    snapshot = main._session_activity_snapshot(sid, assistant)

    assert snapshot == {
        "session_id": sid,
        "is_running": True,
        "monitoring_state": "active",
        "queued_prompts_count": 1,
        "idle": False,
    }


def test_update_source_is_still_a_team_message_for_queue_recovery():
    assert team_messaging.UPDATE_SOURCE in team_messaging.MESSAGE_SOURCES
    assert team_messaging.SOURCE in team_messaging.MESSAGE_SOURCES
    assert team_messaging.ASK_SOURCE in team_messaging.MESSAGE_SOURCES


def test_detached_team_message_does_not_register_turn_join(monkeypatch):
    sender = session_manager.create(name="sender", cwd="/repo", orchestration_mode="native")
    target = session_manager.create(name="target", cwd="/repo", orchestration_mode="native")
    coordinator = Coordinator()

    monkeypatch.setattr(coordinator.turn_manager, "has_active_turn", lambda _sid: True)
    monkeypatch.setattr(
        coordinator,
        "register_mssg_turn_waiter",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("detached mssg must not register a turn waiter")
        ),
    )
    monkeypatch.setattr(
        coordinator,
        "submit_prompt",
        lambda _sid, params, **_kw: params["_queued_id"],
    )

    result = asyncio.run(coordinator.submit_team_message(
        sender_session_id=sender["id"],
        target_session_id=target["id"],
        message="fire and forget",
        detach=True,
    ))

    assert result["success"] is True
    assert coordinator._mssg_turn_waiters == {}


def test_submit_team_message_uses_delegation_message_model_preference(monkeypatch):
    sender = session_manager.create(
        name="manager",
        cwd="/repo",
        orchestration_mode="native",
        model="sender-model",
    )
    target = session_manager.create(
        name="worker",
        cwd="/repo",
        orchestration_mode="native",
        model="target-model",
    )
    provider_id = config_store.get_default_provider()["id"]
    original = config_store.get_internal_llm_assignments()
    config_store.set_internal_llm_assignments({
        **original,
        "delegation_message": {"provider_id": provider_id, "model": "delegation-model"},
    })
    coordinator = Coordinator()
    captured: dict = {}

    def fake_submit_prompt(sid: str, params: dict, **_kwargs) -> str:
        captured["sid"] = sid
        captured["params"] = params
        return params["_queued_id"]

    monkeypatch.setattr(coordinator, "submit_prompt", fake_submit_prompt)
    try:
        result = asyncio.run(coordinator.submit_team_message(
            sender_session_id=sender["id"],
            target_session_id=target["id"],
            message="use preferred model",
        ))
    finally:
        config_store.set_internal_llm_assignments(original)

    assert result["success"] is True
    assert captured["params"]["provider_id"] == provider_id
    assert captured["params"]["model"] == "delegation-model"
    assert captured["params"]["allow_model_override"] is True


def test_submit_team_message_explicit_model_overrides_preference(monkeypatch):
    sender = session_manager.create(name="manager", cwd="/repo", orchestration_mode="native")
    target = session_manager.create(name="worker", cwd="/repo", orchestration_mode="native")
    provider_id = config_store.get_default_provider()["id"]
    original = config_store.get_internal_llm_assignments()
    config_store.set_internal_llm_assignments({
        **original,
        "delegation_message": {"provider_id": provider_id, "model": "preference-model"},
    })
    coordinator = Coordinator()
    captured: dict = {}

    def fake_submit_prompt(sid: str, params: dict, **_kwargs) -> str:
        captured["params"] = params
        return params["_queued_id"]

    monkeypatch.setattr(coordinator, "submit_prompt", fake_submit_prompt)
    try:
        asyncio.run(coordinator.submit_team_message(
            sender_session_id=sender["id"],
            target_session_id=target["id"],
            message="override",
            model="explicit-model",
        ))
    finally:
        config_store.set_internal_llm_assignments(original)

    assert captured["params"]["model"] == "explicit-model"


def test_submit_team_message_can_expect_async_mssg_response(monkeypatch):
    sender = session_manager.create(
        name="manager",
        cwd="/repo",
        orchestration_mode="manager",
    )
    target = session_manager.create(
        name="worker",
        cwd="/repo",
        orchestration_mode="native",
    )
    coordinator = Coordinator()
    captured: dict = {}

    def fake_submit_prompt(sid: str, params: dict, **_kwargs) -> str:
        captured["sid"] = sid
        captured["params"] = params
        return params["_queued_id"]

    monkeypatch.setattr(coordinator, "submit_prompt", fake_submit_prompt)

    result = asyncio.run(coordinator.submit_team_message(
        sender_session_id=sender["id"],
        target_session_id=target["id"],
        message="run test",
        detach=True,
        expect_mssg_response=True,
    ))

    metadata = captured["params"]["team_message"]["metadata"]
    assert result["expects_response"] is True
    assert metadata["expects_response"] is True
    assert metadata["response_mode"] == team_messaging.MSSG_RESPONSE_MODE
    assert 'expects_response="true"' in captured["params"]["cli_prompt"]
    assert f'mssg(target_session_id="{sender["id"]}"' in captured["params"]["cli_prompt"]


def test_submit_team_message_can_target_sub_session(monkeypatch):
    sender = session_manager.create(
        name="manager",
        cwd="/repo",
        orchestration_mode="native",
        model="sender-model",
    )
    sub = session_manager.create_sub_session(
        parent_session_id=sender["id"],
        name="hidden reviewer",
        cwd="/repo",
    )
    coordinator = Coordinator()
    captured: dict = {}

    def fake_submit_prompt(sid: str, params: dict, **_kwargs) -> str:
        captured["sid"] = sid
        captured["params"] = params
        return params["_queued_id"]

    monkeypatch.setattr(coordinator, "submit_prompt", fake_submit_prompt)

    async def run() -> dict:
        return await coordinator.submit_team_message(
            sender_session_id=sender["id"],
            target_session_id=sub["id"],
            message="check this",
        )

    result = asyncio.run(run())

    assert result["success"] is True
    assert captured["sid"] == sub["id"]
    assert captured["params"]["app_session_id"] == sub["id"]
    assert captured["params"]["orchestration_mode"] == "native"
    assert captured["params"]["source"] == team_messaging.SOURCE
    queued = session_manager.get(sub["id"])["queued_prompts"]
    assert len(queued) == 1
    assert queued[0]["content"] == "check this"


def test_ask_team_message_can_target_sub_session(monkeypatch):
    sender = session_manager.create(
        name="manager ask",
        cwd="/repo",
        orchestration_mode="native",
        model="sender-model",
    )
    sub = session_manager.create_sub_session(
        parent_session_id=sender["id"],
        name="hidden auditor",
        cwd="/repo",
    )
    coordinator = Coordinator()

    def fake_submit_prompt(sid: str, params: dict, **_kwargs) -> str:
        async def finish() -> None:
            await asyncio.sleep(0)
            session = session_manager.get(sid)
            coordinator._init_turn_messages(
                session=session,
                app_session_id=sid,
                prompt=params["prompt"],
                images=None,
                source=params["source"],
                lifecycle_msg_id=params["lifecycle_msg_id"],
                queue_item_id=params["_queued_id"],
                team_message=params["team_message"],
            )
            session_manager.append_assistant_msg(sid, {
                "id": "assistant-sub-answer",
                "role": "assistant",
                "content": "sub answer",
                "events": [],
                "timestamp": "2026-06-17T10:01:00",
                "isStreaming": False,
            })
            for cb in list(coordinator.ws_callbacks.get(sid, [])):
                await cb({
                    "type": "user_message_done",
                    "data": {
                        "lifecycle_msg_id": params["lifecycle_msg_id"],
                    },
                })
        asyncio.create_task(finish())
        return params["_queued_id"]

    monkeypatch.setattr(coordinator, "submit_prompt", fake_submit_prompt)

    async def run() -> dict:
        return await coordinator.ask_team_message(
            sender_session_id=sender["id"],
            target_session_id=sub["id"],
            message="answer this",
            timeout_s=1,
        )

    result = asyncio.run(run())

    assert result["success"] is True
    assert result["target_session_id"] == sub["id"]
    assert result["assistant_content"] == "sub answer"


def test_ask_team_message_failed_target_returns_error_without_empty_response(monkeypatch):
    sender = session_manager.create(
        name="manager ask failed",
        cwd="/repo",
        orchestration_mode="native",
    )
    target = session_manager.create(
        name="failed target",
        cwd="/repo",
        orchestration_mode="native",
    )
    coordinator = Coordinator()

    def fake_submit_prompt(sid: str, params: dict, **_kwargs) -> str:
        async def finish() -> None:
            await asyncio.sleep(0)
            session = session_manager.get(sid)
            coordinator._init_turn_messages(
                session=session,
                app_session_id=sid,
                prompt=params["prompt"],
                images=None,
                source=params["source"],
                lifecycle_msg_id=params["lifecycle_msg_id"],
                queue_item_id=params["_queued_id"],
                team_message=params["team_message"],
            )
            for cb in list(coordinator.ws_callbacks.get(sid, [])):
                await cb({
                    "type": "user_message_failed",
                    "data": {
                        "lifecycle_msg_id": params["lifecycle_msg_id"],
                        "error": "unsupported model",
                    },
                })
        asyncio.create_task(finish())
        return params["_queued_id"]

    monkeypatch.setattr(coordinator, "submit_prompt", fake_submit_prompt)

    async def run() -> dict:
        return await coordinator.ask_team_message(
            sender_session_id=sender["id"],
            target_session_id=target["id"],
            message="review this",
            timeout_s=1,
        )

    result = asyncio.run(run())

    assert result["success"] is False
    assert result["error"] == "unsupported model"
    assert result["target_session_id"] == target["id"]
    assert "assistant_content" not in result
    assert "response_message_id" not in result


def test_submit_team_message_allows_cross_cwd_target(monkeypatch):
    sender = session_manager.create(
        name="sender project",
        cwd="/repo-a",
        orchestration_mode="native",
    )
    target = session_manager.create(
        name="target project",
        cwd="/repo-b",
        orchestration_mode="manager",
    )
    coordinator = Coordinator()
    captured: dict = {}

    def fake_submit_prompt(sid: str, params: dict, **_kwargs) -> str:
        captured["sid"] = sid
        captured["params"] = params
        return params["_queued_id"]

    monkeypatch.setattr(coordinator, "submit_prompt", fake_submit_prompt)

    async def run() -> dict:
        return await coordinator.submit_team_message(
            sender_session_id=sender["id"],
            target_session_id=target["id"],
            message="cross-project update",
        )

    result = asyncio.run(run())

    assert result["success"] is True
    assert captured["sid"] == target["id"]
    assert captured["params"]["app_session_id"] == target["id"]
    assert captured["params"]["cwd"] == "/repo-b"
    assert f"<session_id>{target['id']}</session_id>" in captured["params"]["cli_prompt"]
    assert '<team>' in captured["params"]["cli_prompt"]
    assert "<cross_cwd_message>" in captured["params"]["cli_prompt"]
    assert "The sender session cwd is /repo-a." in captured["params"]["cli_prompt"]
    assert "Your session cwd" not in captured["params"]["cli_prompt"]
    queued = session_manager.get(target["id"])["queued_prompts"]
    assert queued[0]["sender_session_id"] == sender["id"]
    assert queued[0]["content"] == "cross-project update"
    assert "<cross_cwd_message>" in queued[0]["cli_prompt"]
    assert captured["params"]["team_message"]["metadata"]["sender_cwd"] == "/repo-a"
    assert "target_cwd" not in captured["params"]["team_message"]["metadata"]


def test_submit_team_message_removes_persisted_queue_when_submit_fails(monkeypatch):
    sender = session_manager.create(
        name="implementation worker failed submit",
        cwd="/repo",
        orchestration_mode="native",
    )
    target = session_manager.create(
        name="team failed submit",
        cwd="/repo",
        orchestration_mode="team",
    )
    coordinator = Coordinator()

    def fail_submit_prompt(_sid: str, _params: dict, **_kwargs) -> str:
        raise RuntimeError("submit failed")

    monkeypatch.setattr(coordinator, "submit_prompt", fail_submit_prompt)

    async def run() -> None:
        await coordinator.submit_team_message(
            sender_session_id=sender["id"],
            target_session_id=target["id"],
            message="status update",
        )

    try:
        asyncio.run(run())
    except RuntimeError as exc:
        assert str(exc) == "submit failed"
    else:
        raise AssertionError("submit_team_message should have raised")

    assert session_manager.get(target["id"])["queued_prompts"] == []


def test_init_turn_messages_persists_team_metadata():
    target = session_manager.create(
        name="manager two",
        cwd="/repo",
        orchestration_mode="manager",
    )
    coordinator = Coordinator()
    session = session_manager.get(target["id"])
    team_message = {
        "message": "done",
        "metadata": {
            "sender_session_id": "worker-session",
        },
    }

    user_msg = coordinator._init_turn_messages(
        session=session,
        app_session_id=target["id"],
        prompt="done",
        images=None,
        source="mssg",
        team_message=team_message,
    )

    assert user_msg["source"] == "mssg"
    assert user_msg["team_message"] == team_message


def test_ask_source_is_distinct_from_batchable_mssg():
    metadata = {
        "sender_session_id": "sender",
        "expects_response": True,
    }

    payload = team_messaging.queue_payload(
        queue_item_id="queued-ask",
        sender_session_id="sender",
        message="answer this",
        metadata=metadata,
        lifecycle_msg_id="life-ask",
        source=team_messaging.ASK_SOURCE,
    )

    assert payload["source"] == "team_ask"
    assert payload["source"] != team_messaging.SOURCE
    assert 'expects_response="true"' in payload["cli_prompt"]


def test_ask_team_message_submits_target_app_session_id(monkeypatch):
    sender = session_manager.create(
        name="ask sender",
        cwd="/repo",
        orchestration_mode="native",
    )
    target = session_manager.create(
        name="ask target",
        cwd="/repo",
        orchestration_mode="native",
    )
    coordinator = Coordinator()
    captured: dict = {}

    def fake_submit_prompt(sid: str, params: dict, **_kwargs) -> str:
        captured["sid"] = sid
        captured["params"] = params

        async def finish() -> None:
            for callback in list(coordinator.ws_callbacks.get(target["id"], [])):
                await callback({
                    "type": "user_message_done",
                    "data": {"lifecycle_msg_id": params["lifecycle_msg_id"]},
                })

        asyncio.get_running_loop().create_task(finish())
        return params["_queued_id"]

    monkeypatch.setattr(coordinator, "submit_prompt", fake_submit_prompt)
    monkeypatch.setattr(
        coordinator,
        "_team_message_turn_response",
        lambda **_kwargs: {},
    )

    async def run() -> dict:
        return await coordinator.ask_team_message(
            sender_session_id=sender["id"],
            target_session_id=target["id"],
            message="review this",
            timeout_s=1,
        )

    result = asyncio.run(run())

    assert result["success"] is True
    assert captured["sid"] == target["id"]
    assert captured["params"]["app_session_id"] == target["id"]
    assert captured["params"]["source"] == team_messaging.ASK_SOURCE


def test_ask_response_omits_token_usage():
    target = session_manager.create(
        name="manager three",
        cwd="/repo",
        orchestration_mode="manager",
    )
    coordinator = Coordinator()
    user_msg = coordinator._init_turn_messages(
        session=session_manager.get(target["id"]),
        app_session_id=target["id"],
        prompt="question",
        images=None,
        source="team_ask",
        lifecycle_msg_id="life-ask",
    )
    session_manager.append_assistant_msg(target["id"], {
        "id": "assistant-answer",
        "role": "assistant",
        "content": "answer content",
        "events": [],
        "timestamp": "2026-06-16T10:01:00",
        "isStreaming": False,
        "token_usage": {"total": 99},
    })

    response = coordinator._team_message_turn_response(
        target_session_id=target["id"],
        lifecycle_msg_id=user_msg["lifecycle_msg_id"],
    )

    assert response["assistant_content"] == "answer content"
    assert "token_usage" not in response


def test_ask_response_falls_back_to_event_text_when_content_empty():
    target = session_manager.create(
        name="manager event fallback",
        cwd="/repo",
        orchestration_mode="manager",
    )
    coordinator = Coordinator()
    user_msg = coordinator._init_turn_messages(
        session=session_manager.get(target["id"]),
        app_session_id=target["id"],
        prompt="question",
        images=None,
        source="team_ask",
        lifecycle_msg_id="life-event-fallback",
    )
    session_manager.append_assistant_msg(target["id"], {
        "id": "assistant-event-answer",
        "role": "assistant",
        "content": "",
        "events": [{
            "type": "agent_message",
            "data": {
                "type": "assistant",
                "uuid": "event-answer-uuid",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "event answer"}],
                },
            },
        }],
        "timestamp": "2026-06-16T10:01:00",
        "isStreaming": False,
    })

    response = coordinator._team_message_turn_response(
        target_session_id=target["id"],
        lifecycle_msg_id=user_msg["lifecycle_msg_id"],
    )

    assert response["assistant_content"] == "event answer"
    repaired = next(
        m for m in session_manager.get(target["id"])["messages"]
        if m.get("id") == "assistant-event-answer"
    )
    assert repaired["content"] == "event answer"


def test_ask_response_uses_matching_lifecycle_turn_only():
    target = session_manager.create(
        name="manager four",
        cwd="/repo",
        orchestration_mode="manager",
    )
    coordinator = Coordinator()
    session = session_manager.get(target["id"])

    coordinator._init_turn_messages(
        session=session,
        app_session_id=target["id"],
        prompt="earlier message",
        images=None,
        source="mssg",
        lifecycle_msg_id="life-earlier",
    )
    session_manager.append_assistant_msg(target["id"], {
        "id": "assistant-earlier",
        "role": "assistant",
        "content": "earlier answer",
        "events": [],
        "timestamp": "2026-06-16T10:01:00",
        "isStreaming": False,
    })
    coordinator._init_turn_messages(
        session=session_manager.get(target["id"]),
        app_session_id=target["id"],
        prompt="ask message",
        images=None,
        source="team_ask",
        lifecycle_msg_id="life-ask-only",
    )
    session_manager.append_assistant_msg(target["id"], {
        "id": "assistant-ask",
        "role": "assistant",
        "content": "ask answer",
        "events": [],
        "timestamp": "2026-06-16T10:02:00",
        "isStreaming": False,
    })

    response = coordinator._team_message_turn_response(
        target_session_id=target["id"],
        lifecycle_msg_id="life-ask-only",
    )

    assert response["response_message_id"] == "assistant-ask"
    assert response["assistant_content"] == "ask answer"


def test_team_ask_panel_uses_worker_event_path():
    sender = session_manager.create(
        name="sender manager",
        cwd="/repo",
        orchestration_mode="manager",
    )
    target = session_manager.create(
        name="target worker",
        cwd="/repo",
        orchestration_mode="native",
    )
    coordinator = Coordinator()
    events: list[dict] = []

    async def save(event: dict) -> None:
        events.append(event)

    coordinator.turn_manager._turn_save_callbacks[sender["id"]] = save
    coordinator.turn_manager.current_turn_workers[sender["id"]] = []

    async def run() -> None:
        panel = await coordinator._start_team_message_panel(
            sender_session_id=sender["id"],
            target_session_id=target["id"],
            target=target,
            message="please answer",
            queue_item_id="queue-1",
            run_mode=team_messaging.ASK_SOURCE,
        )
        await coordinator._forward_team_message_panel_event(
            sender_session_id=sender["id"],
            panel=panel,
            event={
                "type": "agent_message",
                "data": {"uuid": "event-1", "message": {"content": "working"}},
            },
        )
        await coordinator._emit_team_message_panel_complete(
            sender_session_id=sender["id"],
            panel=panel,
            success=True,
        )

    asyncio.run(run())

    panel = coordinator.turn_manager.current_turn_workers[sender["id"]][0]
    assert panel["run_mode"] == team_messaging.ASK_SOURCE
    assert panel["worker_session_id"] == target["id"]
    assert panel["panel_kind"] == "session"
    assert panel["events"][0]["data"]["uuid"] == "event-1"
    assert [event["type"] for event in events] == [
        "worker_start",
        "worker_event",
        "worker_complete",
    ]


def test_team_message_panel_scope_rejects_unrelated_lifecycle_events():
    assert Coordinator._team_message_panel_event_in_scope(
        {"type": "user_message_done", "data": {"lifecycle_msg_id": "other"}},
        "life",
        False,
    ) == (False, False)
    assert Coordinator._team_message_panel_event_in_scope(
        {"type": "user_message_persisted", "data": {"lifecycle_msg_id": "life"}},
        "life",
        False,
    ) == (True, True)
    assert Coordinator._team_message_panel_event_in_scope(
        {"type": "user_message_done", "data": {"lifecycle_msg_id": "other"}},
        "life",
        True,
    ) == (False, False)
    assert Coordinator._team_message_panel_event_in_scope(
        {"type": "agent_message", "data": {"uuid": "event-1"}},
        "life",
        True,
    ) == (True, True)


def test_async_mssg_panel_watcher_ignores_other_lifecycle_events():
    sender = session_manager.create(
        name="sender two",
        cwd="/repo",
        orchestration_mode="team",
    )
    target = session_manager.create(
        name="target two",
        cwd="/repo",
        orchestration_mode="native",
    )
    coordinator = Coordinator()
    events: list[dict] = []

    async def save(event: dict) -> None:
        events.append(event)

    coordinator.turn_manager._turn_save_callbacks[sender["id"]] = save
    coordinator.turn_manager.current_turn_workers[sender["id"]] = []

    async def run() -> None:
        panel = await coordinator._start_team_message_panel(
            sender_session_id=sender["id"],
            target_session_id=target["id"],
            target=target,
            message="please answer",
            queue_item_id="queue-2",
            run_mode=team_messaging.SOURCE,
        )
        coordinator._watch_team_message_panel(
            sender_session_id=sender["id"],
            target_session_id=target["id"],
            lifecycle_msg_id="life-target",
            panel=panel,
        )
        callback = coordinator.ws_callbacks[target["id"]][0]
        await callback({
            "type": "user_message_persisted",
            "data": {"lifecycle_msg_id": "other"},
        })
        await callback({
            "type": "agent_message",
            "data": {"uuid": "before-active"},
        })
        await callback({
            "type": "user_message_persisted",
            "data": {"lifecycle_msg_id": "life-target"},
        })
        await callback({
            "type": "agent_message",
            "data": {"uuid": "target-event"},
        })
        await callback({
            "type": "user_message_done",
            "data": {"lifecycle_msg_id": "other"},
        })
        await callback({
            "type": "agent_message",
            "data": {"uuid": "after-wrong-lifecycle"},
        })
        await callback({
            "type": "user_message_persisted",
            "data": {"lifecycle_msg_id": "life-target"},
        })
        await callback({
            "type": "agent_message",
            "data": {"uuid": "target-event-2"},
        })
        await callback({
            "type": "user_message_done",
            "data": {"lifecycle_msg_id": "life-target"},
        })
        await asyncio.sleep(0)

    asyncio.run(run())

    panel = coordinator.turn_manager.current_turn_workers[sender["id"]][0]
    assert [event["data"]["uuid"] for event in panel["events"]] == [
        "target-event",
        "target-event-2",
    ]
    worker_events = [event for event in events if event["type"] == "worker_event"]
    assert [event["data"]["event"]["data"]["uuid"] for event in worker_events] == [
        "target-event",
        "target-event-2",
    ]
    assert events[-1]["type"] == "worker_complete"
    assert events[-1]["data"]["success"] is True


def test_sub_session_panel_kind_and_snapshot_dedupes_by_delegation_id():
    sender = session_manager.create(
        name="sender panel kind",
        cwd="/repo",
        orchestration_mode="team",
    )
    sub = session_manager.create_sub_session(
        parent_session_id=sender["id"],
        name="hidden review",
        cwd="/repo",
    )
    coordinator = Coordinator()
    events: list[dict] = []

    async def save(event: dict) -> None:
        events.append(event)

    coordinator.turn_manager._turn_save_callbacks[sender["id"]] = save
    coordinator.turn_manager.current_turn_workers[sender["id"]] = []

    async def run() -> None:
        await coordinator._start_team_message_panel(
            sender_session_id=sender["id"],
            target_session_id=sub["id"],
            target=sub,
            message="review",
            queue_item_id="queue-sub",
            run_mode=team_messaging.SOURCE,
        )

    asyncio.run(run())

    panel = coordinator.turn_manager.current_turn_workers[sender["id"]][0]
    assert panel["panel_kind"] == "sub_session"
    assert panel["started_at"]
    assert events[0]["data"]["panel_kind"] == "sub_session"
    assert events[0]["data"]["started_at"] == panel["started_at"]

    assistant = {
        "id": "assistant-panel-kind",
        "role": "assistant",
        "content": "",
        "events": [],
        "workers": [],
        "timestamp": "2026-06-17T10:03:00",
        "isStreaming": False,
    }
    session_manager.append_assistant_msg(sender["id"], assistant)
    session_manager.snapshot_workers(sender["id"], assistant["id"], [
        {"delegation_id": "dup", "worker_description": "first", "events": []},
        {"delegation_id": "other", "worker_description": "other", "events": []},
        {"delegation_id": "dup", "worker_description": "second", "events": [
            {"type": "agent_message", "data": {"uuid": "event-dup"}},
        ]},
    ])
    fresh = session_manager.get(sender["id"])
    saved = next(m for m in fresh["messages"] if m["id"] == assistant["id"])
    assert [worker["delegation_id"] for worker in saved["workers"]] == ["dup", "other"]
    assert saved["workers"][0]["worker_description"] == "second"
    assert saved["workers"][0]["events"][0]["data"]["uuid"] == "event-dup"


def test_session_creation_panel_is_separate_from_message_turn_panel():
    sender = session_manager.create(
        name="sender creation panel",
        cwd="/repo",
        orchestration_mode="team",
    )
    sub = session_manager.create_sub_session(
        parent_session_id=sender["id"],
        name="review session",
        cwd="/repo",
    )
    coordinator = Coordinator()
    events: list[dict] = []

    async def save(event: dict) -> None:
        events.append(event)

    coordinator.turn_manager._turn_save_callbacks[sender["id"]] = save
    coordinator.turn_manager.current_turn_workers[sender["id"]] = []

    async def run() -> None:
        await coordinator.emit_session_created_panel(
            sender_session_id=sender["id"],
            target_session=sub,
        )
        await coordinator._start_team_message_panel(
            sender_session_id=sender["id"],
            target_session_id=sub["id"],
            target=sub,
            message="review this",
            queue_item_id="queue-review",
            run_mode=team_messaging.SOURCE,
        )

    asyncio.run(run())

    panels = coordinator.turn_manager.current_turn_workers[sender["id"]]
    assert [panel["delegation_id"] for panel in panels] == [
        f"created_{sub['id']}",
        "team_message_queue-review",
    ]
    assert panels[0]["panel_kind"] == "sub_session_created"
    assert panels[0]["run_mode"] == "created"
    assert panels[0]["is_new"] is True
    assert panels[0]["events"] == []
    assert panels[1]["panel_kind"] == "sub_session"
    assert panels[1]["run_mode"] == team_messaging.SOURCE
    assert [event["data"]["panel_kind"] for event in events] == [
        "sub_session_created",
        "sub_session",
    ]
