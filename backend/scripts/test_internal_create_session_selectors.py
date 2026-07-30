from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-internal-create-session-")
os.environ["BETTER_CLAUDE_TEST_AUTH_BYPASS"] = "1"

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Extensions are only active under an activated installation, and a core role
# has no owner until its extension is active. Activate before importing main.
import _test_installation  # noqa: E402
_test_installation.activate(Path(_TMP_HOME))
import _test_extension  # noqa: E402

from starlette.testclient import TestClient  # noqa: E402

import config_store  # noqa: E402
import main  # noqa: E402
import model_catalog_read_projection  # noqa: E402
import models as models_mod  # noqa: E402
import runtime_profile  # noqa: E402
import orchs.manager._approval as approval  # noqa: E402
from model_catalog_refresh_state import CatalogProjection, changed_fact  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402


def _post(client: TestClient, body: dict):
    return client.post(
        "/api/internal/create-session",
        json=body,
        headers={"X-Internal-Token": main.coordinator.internal_token},
    )


def _configure_internal_llm_defaults(*tasks: str) -> None:
    provider = config_store.list_providers()["providers"][0]
    assignments = config_store.get_internal_llm_assignments()
    for task in tasks:
        assignments[task] = {
            "provider_id": provider["id"],
            "model": provider["default_model"],
            "reasoning_effort": provider.get("default_reasoning_effort") or "",
        }
    config_store.set_internal_llm_assignments(assignments)


def _seed_default_provider(model: str) -> None:
    """The installation fixture writes a bare provider with no selectors.

    Sessions store a concrete reasoning_effort, so the provider needs a real
    default for inheritance assertions to compare against.
    """
    default = config_store.get_default_provider() or {}
    payload: dict = {}
    if not default.get("name"):
        payload["name"] = "Default Test Provider"
    if not default.get("default_model"):
        payload["default_model"] = model
        payload["custom_models"] = [model]
    efforts = runtime_profile.reasoning_efforts(default)
    if not efforts:
        efforts = ("low", "medium", "high", "xhigh")
        payload["reasoning_effort_options"] = list(efforts)
    if not default.get("default_reasoning_effort"):
        payload["default_reasoning_effort"] = efforts[0]
    if payload:
        config_store.update_provider(default["id"], payload)


def main_test() -> int:
    _seed_default_provider("default-provider-model")
    _configure_internal_llm_defaults("default_session")
    _test_extension.install_core_role(Path(_TMP_HOME), "team-orchestration")
    client = TestClient(main.app, client=("127.0.0.1", 50000))
    provider = config_store.get_default_provider()
    provider_id = provider["id"]
    provider_model = provider["default_model"]
    provider_reasoning_effort = provider.get("default_reasoning_effort") or ""
    other_provider = config_store.add_provider({
        "name": "Other Test Provider",
        "kind": provider.get("kind") or "claude",
        "mode": provider.get("mode") or "subscription",
        "default_model": "other-provider-model",
        "custom_models": ["other-provider-model"],
    })
    other_provider_id = other_provider["id"]
    no_default_provider = config_store.add_provider({
        "name": "No Default Test Provider",
        "kind": provider.get("kind") or "claude",
        "mode": provider.get("mode") or "subscription",
        "default_model": "",
        "custom_models": ["no-default-provider-model"],
    })
    no_default_provider_id = no_default_provider["id"]
    sender = session_manager.create(
        name="sender",
        cwd="/repo",
        orchestration_mode="native",
        model="sender-model",
        provider_id=provider_id,
        reasoning_effort=provider_reasoning_effort,
    )

    inherited = _post(client, {
        "sender_session_id": sender["id"],
        "name": "inherited",
        "cwd": "/repo",
        "orchestration_mode": "native",
    })
    assert inherited.status_code == 200, inherited.text
    inherited_session = session_manager.get(inherited.json()["session_id"]) or {}
    assert inherited_session["provider_id"] == provider_id
    assert inherited_session["model"] == "sender-model"
    assert inherited_session["reasoning_effort"] == provider_reasoning_effort

    remote_inherited = _post(client, {
        "sender_session_id": sender["id"],
        "name": "remote inherited effort",
        "cwd": r"C:\Users\Lenovo\agent-session",
        "orchestration_mode": "native",
        "node_id": "lenovo",
        "provider_id": provider_id,
        "model": provider_model,
        "runner": "native",
    })
    assert remote_inherited.status_code == 200, remote_inherited.text
    remote_inherited_session = session_manager.get(remote_inherited.json()["session_id"]) or {}
    assert remote_inherited_session["node_id"] == "lenovo"
    assert remote_inherited_session["cwd"] == r"C:\Users\Lenovo\agent-session"
    assert remote_inherited_session["reasoning_effort"] == provider_reasoning_effort

    explicit = _post(client, {
        "sender_session_id": sender["id"],
        "name": "explicit",
        "cwd": "/repo",
        "orchestration_mode": "native",
        "provider_id": provider_id,
        "model": provider_model,
        "reasoning_effort": provider_reasoning_effort,
    })
    assert explicit.status_code == 200, explicit.text
    explicit_session = session_manager.get(explicit.json()["session_id"]) or {}
    assert explicit_session["provider_id"] == provider_id
    assert explicit_session["model"] == provider_model
    assert explicit_session["reasoning_effort"] == provider_reasoning_effort

    explicit_by_name = _post(client, {
        "sender_session_id": sender["id"],
        "name": "explicit by name",
        "cwd": "/repo",
        "orchestration_mode": "native",
        "provider_id": provider["name"],
        "model": provider_model,
        "reasoning_effort": provider_reasoning_effort,
    })
    assert explicit_by_name.status_code == 200, explicit_by_name.text
    explicit_by_name_session = session_manager.get(explicit_by_name.json()["session_id"]) or {}
    assert explicit_by_name_session["provider_id"] == provider_id
    assert explicit_by_name_session["model"] == provider_model

    invalid_session_model = _post(client, {
        "sender_session_id": sender["id"],
        "name": "bad model",
        "cwd": "/repo",
        "orchestration_mode": "native",
        "provider_id": provider_id,
        "model": "model-that-is-not-in-provider-catalog",
    })
    assert invalid_session_model.status_code == 400
    assert "does not support model" in invalid_session_model.text

    session_provider_default = _post(client, {
        "sender_session_id": sender["id"],
        "name": "provider default",
        "cwd": "/repo",
        "orchestration_mode": "native",
        "provider_id": other_provider_id,
    })
    assert session_provider_default.status_code == 200, session_provider_default.text
    session_provider_default_record = session_manager.get(session_provider_default.json()["session_id"]) or {}
    assert session_provider_default_record["provider_id"] == other_provider_id
    assert session_provider_default_record["model"] == "other-provider-model"

    codex_provider = config_store.add_provider({
        "name": "Codex Stale Default",
        "kind": "codex",
        "mode": "subscription",
        "default_model": "gpt-5.6",
        "custom_models": [],
    })
    model_catalog_read_projection.apply_fact(changed_fact(CatalogProjection(
        provider_id=codex_provider["id"],
        provider_generation=codex_provider["generation"],
        status="current",
        models=("gpt-5.5", "gpt-5.4"),
        models_current=True,
        retired=(),
        last_refreshed_at=1.0,
        reason="",
        authority_fingerprint="test-authority",
    )))
    stale_codex_default = _post(client, {
        "sender_session_id": sender["id"],
        "name": "stale codex default",
        "cwd": "/repo",
        "orchestration_mode": "native",
        "provider_id": codex_provider["id"],
    })
    assert stale_codex_default.status_code == 200, stale_codex_default.text
    stale_codex_default_record = session_manager.get(stale_codex_default.json()["session_id"]) or {}
    assert stale_codex_default_record["provider_id"] == codex_provider["id"]
    assert stale_codex_default_record["model"] == "gpt-5.5"

    session_provider_no_default = _post(client, {
        "sender_session_id": sender["id"],
        "name": "provider no default",
        "cwd": "/repo",
        "orchestration_mode": "native",
        "provider_id": no_default_provider_id,
    })
    assert session_provider_no_default.status_code == 400
    assert "has no default model configured" in session_provider_no_default.text

    original_available_models = models_mod.available_models
    models_mod.available_models = lambda _provider_id=None: []
    try:
        empty_catalog_model = _post(client, {
            "sender_session_id": sender["id"],
            "name": "empty catalog",
            "cwd": "/repo",
            "orchestration_mode": "native",
            "provider_id": provider_id,
            "model": provider_model,
        })
    finally:
        models_mod.available_models = original_available_models
    assert empty_catalog_model.status_code == 400
    assert "has no known models" in empty_catalog_model.text

    missing_sender = _post(client, {
        "sender_session_id": "missing",
        "name": "bad",
        "cwd": "/repo",
    })
    assert missing_sender.status_code == 400

    invalid_worker_model = client.post(
        "/api/internal/create-worker",
        json={
            "app_session_id": sender["id"],
            "worker_description": "bad worker",
            "justification": "validate model before worker creation",
            "orchestration_mode": "native",
            "model": "model-that-is-not-in-provider-catalog",
            "cwd": "/repo",
        },
        headers={"X-Internal-Token": main.coordinator.internal_token},
    )
    assert invalid_worker_model.status_code == 400
    assert "does not support model" in invalid_worker_model.text

    invalid_delegate_model = client.post(
        "/api/internal/delegate-task",
        json={
            "sender_session_id": sender["id"],
            "task": "bad delegate",
            "model": "model-that-is-not-in-provider-catalog",
            "cwd": "/repo",
        },
        headers={"X-Internal-Token": main.coordinator.internal_token},
    )
    assert invalid_delegate_model.status_code == 400
    assert "does not support model" in invalid_delegate_model.text

    original_run_delegate_task = main.coordinator.run_delegate_task
    delegate_call: dict = {}

    async def fake_run_delegate_task(**kwargs) -> dict:
        delegate_call.update(kwargs)
        return {"success": True}

    main.coordinator.run_delegate_task = fake_run_delegate_task
    try:
        delegate_provider_default = client.post(
            "/api/internal/delegate-task",
            json={
                "sender_session_id": sender["id"],
                "task": "delegate provider default",
                "provider_id": other_provider_id,
                "cwd": "/repo",
            },
            headers={"X-Internal-Token": main.coordinator.internal_token},
        )
    finally:
        main.coordinator.run_delegate_task = original_run_delegate_task
    assert delegate_provider_default.status_code == 200, delegate_provider_default.text
    assert delegate_call["provider_id"] == other_provider_id
    assert delegate_call["model"] == "other-provider-model"

    main.coordinator.run_delegate_task = fake_run_delegate_task
    delegate_call.clear()
    try:
        delegate_provider_by_name = client.post(
            "/api/internal/delegate-task",
            json={
                "sender_session_id": sender["id"],
                "task": "delegate provider by name",
                "provider_id": "other test provider",
                "cwd": "/repo",
            },
            headers={"X-Internal-Token": main.coordinator.internal_token},
        )
    finally:
        main.coordinator.run_delegate_task = original_run_delegate_task
    assert delegate_provider_by_name.status_code == 200, delegate_provider_by_name.text
    assert delegate_call["provider_id"] == other_provider_id
    assert delegate_call["model"] == "other-provider-model"

    delegate_provider_no_default = client.post(
        "/api/internal/delegate-task",
        json={
            "sender_session_id": sender["id"],
            "task": "delegate provider no default",
            "provider_id": no_default_provider_id,
            "cwd": "/repo",
        },
        headers={"X-Internal-Token": main.coordinator.internal_token},
    )
    assert delegate_provider_no_default.status_code == 400
    assert "has no default model configured" in delegate_provider_no_default.text

    session_manager.set_worker_creation_policy(sender["id"], "approve")
    original_spawn_approved_worker = approval.spawn_approved_worker
    spawn_call: dict = {}

    async def fake_spawn_approved_worker(*_args, **kwargs) -> dict:
        spawn_call.update(kwargs)
        worker = session_manager.create(
            name=kwargs["description"],
            cwd=kwargs["cwd"],
            orchestration_mode=kwargs["mode"],
            model=kwargs["model"],
            provider_id=kwargs["provider_id"],
            reasoning_effort=provider_reasoning_effort,
            node_id=kwargs["node_id"],
        )
        return {
            "agent_session_id": worker["id"],
            "description": kwargs["description"],
            "orchestration_mode": kwargs["mode"],
            "model": kwargs["model"],
            "node_id": kwargs["node_id"],
        }

    approval.spawn_approved_worker = fake_spawn_approved_worker
    try:
        inherited_worker_model = client.post(
            "/api/internal/create-worker",
            json={
                "app_session_id": sender["id"],
                "worker_description": "worker inherits model",
                "justification": "validate inherited caller model",
                "orchestration_mode": "native",
                "cwd": "/repo",
            },
            headers={"X-Internal-Token": main.coordinator.internal_token},
        )
    finally:
        approval.spawn_approved_worker = original_spawn_approved_worker
    assert inherited_worker_model.status_code == 200, inherited_worker_model.text
    assert inherited_worker_model.json()["success"] is True
    assert spawn_call["model"] == "sender-model"

    called_submit_team_message = False
    original_submit_team_message = main.coordinator.submit_team_message

    async def fake_submit_team_message(**_kwargs) -> dict:
        nonlocal called_submit_team_message
        called_submit_team_message = True
        return {"success": False}

    main.coordinator.submit_team_message = fake_submit_team_message
    try:
        sub = client.post(
            "/api/internal/create-sub-session",
            json={
                "sender_session_id": sender["id"],
                "description": "hidden reviewer",
                "cwd": "/repo",
            },
            headers={"X-Internal-Token": main.coordinator.internal_token},
        )
    finally:
        main.coordinator.submit_team_message = original_submit_team_message
    assert sub.status_code == 200, sub.text
    assert "sub_session_id" not in sub.json()
    assert "queued_id" not in sub.json()
    target_session_id = sub.json()["target_session_id"]
    assert called_submit_team_message is False
    sub_session = session_manager.get(target_session_id) or {}
    assert sub_session["kind"] == "sub_session"
    assert sub_session["parent_session_id"] == sender["id"]
    assert sub_session["orchestration_mode"] == "native"
    assert sub_session["provider_id"] == provider_id
    assert sub_session["model"] == "sender-model"
    assert sub_session["reasoning_effort"] == provider_reasoning_effort
    assert sub_session["messages"] == []
    assert target_session_id not in {s["id"] for s in session_manager.list()}

    no_model_parent = session_manager.create(
        name="no model parent",
        cwd="/repo",
        orchestration_mode="native",
        model=None,
        provider_id=provider_id,
        reasoning_effort=provider_reasoning_effort,
    )
    with session_manager.batch(no_model_parent["id"]):
        session_manager._cached(no_model_parent["id"])["model"] = ""  # type: ignore[attr-defined]
    sub_provider_default = client.post(
        "/api/internal/create-sub-session",
        json={
            "sender_session_id": no_model_parent["id"],
            "description": "provider default sub",
            "cwd": "/repo",
            "provider_id": other_provider_id,
        },
        headers={"X-Internal-Token": main.coordinator.internal_token},
    )
    assert sub_provider_default.status_code == 200, sub_provider_default.text
    sub_provider_default_session = session_manager.get(sub_provider_default.json()["target_session_id"]) or {}
    assert sub_provider_default_session["provider_id"] == other_provider_id
    assert sub_provider_default_session["model"] == "other-provider-model"

    sub_provider_default_from_sender = client.post(
        "/api/internal/create-sub-session",
        json={
            "sender_session_id": sender["id"],
            "description": "sub provider default from sender",
            "cwd": "/repo",
            "provider_id": other_provider_id,
        },
        headers={"X-Internal-Token": main.coordinator.internal_token},
    )
    assert sub_provider_default_from_sender.status_code == 200, sub_provider_default_from_sender.text
    sub_provider_default_from_sender_session = session_manager.get(
        sub_provider_default_from_sender.json()["target_session_id"]
    ) or {}
    assert sub_provider_default_from_sender_session["provider_id"] == other_provider_id
    assert sub_provider_default_from_sender_session["model"] == "other-provider-model"

    sub_provider_by_name = client.post(
        "/api/internal/create-sub-session",
        json={
            "sender_session_id": sender["id"],
            "description": "sub provider by name",
            "cwd": "/repo",
            "provider_id": "Other Test Provider",
        },
        headers={"X-Internal-Token": main.coordinator.internal_token},
    )
    assert sub_provider_by_name.status_code == 200, sub_provider_by_name.text
    sub_provider_by_name_session = session_manager.get(sub_provider_by_name.json()["target_session_id"]) or {}
    assert sub_provider_by_name_session["provider_id"] == other_provider_id
    assert sub_provider_by_name_session["model"] == "other-provider-model"

    sub_provider_no_default = client.post(
        "/api/internal/create-sub-session",
        json={
            "sender_session_id": sender["id"],
            "description": "sub provider no default",
            "cwd": "/repo",
            "provider_id": no_default_provider_id,
        },
        headers={"X-Internal-Token": main.coordinator.internal_token},
    )
    assert sub_provider_no_default.status_code == 400
    assert "has no default model configured" in sub_provider_no_default.text

    invalid_model = client.post(
        "/api/internal/create-sub-session",
        json={
            "sender_session_id": sender["id"],
            "description": "bad model",
            "cwd": "/repo",
            "provider_id": provider_id,
            "model": "model-that-is-not-in-provider-catalog",
        },
        headers={"X-Internal-Token": main.coordinator.internal_token},
    )
    assert invalid_model.status_code == 400
    assert "does not support model" in invalid_model.text

    config_store.add_provider({
        "name": "Other Test Provider",
        "kind": provider.get("kind") or "claude",
        "mode": provider.get("mode") or "subscription",
        "default_model": "duplicate-provider-model",
        "custom_models": ["duplicate-provider-model"],
    })
    ambiguous_provider = _post(client, {
        "sender_session_id": sender["id"],
        "name": "ambiguous provider",
        "cwd": "/repo",
        "orchestration_mode": "native",
        "provider_id": "Other Test Provider",
    })
    assert ambiguous_provider.status_code == 400
    assert "ambiguous" in ambiguous_provider.text

    session_manager.set_agent_sid(target_session_id, "native", "provider-sub-sid")
    fork = session_manager.create_delegate_fork(
        parent_agent_session_id=target_session_id,
        caller_agent_session_id=sender["id"],
        parent_agent_sid_at_fork="provider-sub-sid",
        parent_line_count_at_fork=0,
        orchestration_mode="native",
    )
    assert fork["kind"] == "delegate_fork"
    assert fork["parent_session_id"] == target_session_id
    assert fork["caller_agent_session_id"] == sender["id"]
    assert fork["forked_from_agent_sid"] == "provider-sub-sid"
    assert session_manager.get(fork["id"])["parent_session_id"] == target_session_id

    print("ALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main_test())
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
