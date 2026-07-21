from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ["BETTER_AGENT_HOME"] = tempfile.mkdtemp(prefix="ba-capability-boundary-")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "sdk"))

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict, ValidationError

import capability_api
import extension_backend_loader
from better_agent_sdk import Client


class _Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str


def _client(monkeypatch, grants: list[str]) -> TestClient:
    monkeypatch.setattr(capability_api.extension_token_registry, "resolve", lambda token: "ofek.test" if token == "valid" else None)
    monkeypatch.setattr(
        capability_api.extension_store,
        "get_extension",
        lambda extension_id: {
            "enabled": True,
            "manifest": {"permissions": {"capabilities": grants}},
        } if extension_id == "ofek.test" else None,
    )
    monkeypatch.setattr(capability_api.extension_store, "is_extension_active", lambda extension_id: extension_id == "ofek.test")
    app = FastAPI()
    app.include_router(capability_api.router)
    return TestClient(app)


def _invoke(client: TestClient, capability: str, action: str, payload: dict, **headers: str):
    return client.post(
        "/api/internal/capabilities/invoke",
        json={"capability": capability, "action": action, "payload": payload},
        headers={"X-Internal-Token": "valid", **headers},
    )


def test_token_identity_cannot_be_forged(monkeypatch) -> None:
    client = _client(monkeypatch, ["test.echo"])
    response = _invoke(client, "ask", "ensure", {}, **{"X-Extension-Id": "ofek-dev.ask"})
    assert response.status_code == 403


def test_ungranted_and_unknown_actions_fail_closed(monkeypatch) -> None:
    client = _client(monkeypatch, ["test.echo"])
    assert _invoke(client, "test", "other", {}).status_code == 403
    client = _client(monkeypatch, ["test.missing"])
    assert _invoke(client, "test", "missing", {}).status_code == 404


def test_action_payload_schema_rejects_extra_fields(monkeypatch) -> None:
    capability_api._ACTIONS[("test", "echo")] = capability_api._Action(_Payload, lambda payload: payload.model_dump())
    try:
        client = _client(monkeypatch, ["test.echo"])
        response = _invoke(client, "test", "echo", {"value": "ok", "path": "/api/internal/ask-ui/ensure"})
        assert response.status_code == 422
    finally:
        capability_api._ACTIONS.pop(("test", "echo"), None)


def test_private_feature_actions_are_registered_with_strict_schemas() -> None:
    expected = {
        ("agent-board", "prompt.run"),
        ("credential-broker", "request"),
        ("credential-broker", "execute"),
        ("credential-broker", "ui.pending"),
        ("credential-broker", "ui.approve"),
        ("credential-broker", "ui.deny"),
        ("credential-broker", "ui.revoke"),
        ("credential-broker", "password-manager.list"),
        ("credential-broker", "password-manager.store"),
        ("credential-broker", "password-manager.delete"),
        ("git", "status"),
        ("git", "diff"),
        ("git", "log"),
        ("git", "add"),
        ("git", "commit"),
        ("git", "branch"),
        ("git", "push"),
        ("machine-nodes", "list"),
        ("machine-nodes", "local-node-id"),
        ("machine-nodes", "pending"),
        ("machine-nodes", "approve"),
        ("machine-nodes", "deny"),
        ("machine-nodes", "revoke"),
        ("machine-nodes", "restart"),
        ("project-structure", "updates.count"),
        ("project-structure", "updates.total"),
        ("project-structure", "updates.counts-batch"),
        ("project-structure", "updates.unseen"),
        ("project-structure", "updates.capture"),
        ("project-structure", "updates.mark-seen"),
        ("project-structure", "edit.status"),
        ("project-structure", "edit.ensure"),
        ("auto-tagging", "current-task"),
        ("auto-tagging", "snapshot"),
        ("auto-tagging", "select-tags"),
        ("auto-tagging", "ensure-tag"),
        ("auto-tagging", "update-tag"),
        ("auto-tagging", "delete-tag"),
        ("auto-tagging", "sync-session-tags"),
        ("auto-tagging", "tags-sql"),
    }
    assert expected <= set(capability_api._ACTIONS)
    for key in expected:
        schema = capability_api._ACTIONS[key].schema
        assert schema.model_config.get("extra") == "forbid"


def test_auto_tagging_update_rejects_unsupported_patch_fields() -> None:
    schema = capability_api._ACTIONS[("auto-tagging", "update-tag")].schema
    invalid_patches = [
        {},
        {"project_id": "other"},
        {"name": ""},
        {"name": ["wrong-type"]},
        {"color": ["wrong-type"]},
    ]
    for patch in invalid_patches:
        try:
            schema.model_validate({"tag_id": "tag-1", "patch": patch})
        except ValidationError:
            continue
        raise AssertionError(f"auto-tagging update accepted invalid patch: {patch!r}")


def test_auto_tagging_capability_payloads_are_bounded() -> None:
    invalid = [
        ("current-task", {"session_id": "/not-an-id"}),
        ("snapshot", {"project_id": "x" * 4097}),
        (
            "select-tags",
            {
                "session_id": "sid-1",
                "task": "task",
                "evidence": [{"text": "evidence", "role": "system"}],
                "existing_tags": [],
                "max_tags": 5,
                "cwd": "/repo",
            },
        ),
        (
            "select-tags",
            {
                "session_id": "sid-1",
                "task": "task",
                "evidence": [{"text": "evidence", "role": "user"}],
                "existing_tags": [],
                "max_tags": 6,
                "cwd": "/repo",
            },
        ),
        ("ensure-tag", {"name": "x" * 49, "project_id": "", "color": None}),
        ("ensure-tag", {"name": "tag", "project_id": "", "color": "blue"}),
        ("delete-tag", {"tag_id": "/not-an-id"}),
        (
            "sync-session-tags",
            {"session_id": "sid-1", "tag_ids": [], "source": "manual", "merge": False},
        ),
        ("tags-sql", {"sql": "x" * 16385}),
    ]
    for action, payload in invalid:
        schema = capability_api._ACTIONS[("auto-tagging", action)].schema
        try:
            schema.model_validate(payload)
        except ValidationError:
            continue
        raise AssertionError(f"auto-tagging {action} accepted invalid payload")


def test_public_sdk_has_no_raw_route_transport() -> None:
    assert not hasattr(Client, "request_internal")
    client = Client(internal_token="token")
    try:
        client.invoke_capability("ask", "ensure", {}, path="/api/internal/ask-ui/ensure")
    except TypeError:
        pass
    else:
        raise AssertionError("invoke_capability accepted a raw path")


def test_switch_capability_runtime_gets_identity_token(monkeypatch) -> None:
    monkeypatch.setattr("orchestrator.get_active_coordinator", lambda: None)
    env = extension_backend_loader._extension_sdk_env(
        {
            "extension_id": "ofek-dev.switch-control",
            "permissions": {"capabilities": ["switch-control.test.ping"]},
            "effective_permissions": {},
            "sdk_pythonpath": "",
        },
        "http://core",
    )
    token = env["BETTER_AGENT_INTERNAL_TOKEN"]
    assert capability_api.extension_token_registry.resolve(token) == "ofek-dev.switch-control"


def test_capability_only_token_cannot_reach_other_internal_routes(monkeypatch) -> None:
    import main

    original_broadcast = capability_api._ACTIONS[("provider-config-sync", "change.broadcast")]
    capability_api._ACTIONS[("switch-control", "test.ping")] = capability_api._Action(
        capability_api._StrictPayload,
        lambda _payload: {"ok": True},
    )
    capability_api._ACTIONS[("provider-config-sync", "change.broadcast")] = capability_api._Action(
        capability_api._ProviderConfigBroadcastPayload,
        lambda _payload: {"ok": True},
    )
    records = {
        "ofek-dev.switch-control": {
            "enabled": True,
            "manifest": {
                "id": "ofek-dev.switch-control",
                "permissions": {"capabilities": ["switch-control.test.ping"]},
            },
            "entitlement": {"status": "not_required"},
        },
        "ofek-dev.provider-config-sync": {
            "enabled": True,
            "manifest": {
                "id": "ofek-dev.provider-config-sync",
                "permissions": {"capabilities": ["provider-config-sync.change.broadcast"]},
            },
            "entitlement": {"status": "not_required"},
        },
    }
    original_get = capability_api.extension_store.get_extension
    original_active = capability_api.extension_store.is_extension_active
    monkeypatch.setattr(capability_api.extension_store, "get_extension", lambda extension_id: records.get(extension_id) or original_get(extension_id))
    monkeypatch.setattr(capability_api.extension_store, "is_extension_active", lambda extension_id: extension_id in records or original_active(extension_id))
    token = capability_api.extension_token_registry.mint("ofek-dev.switch-control")
    pcs_token = capability_api.extension_token_registry.mint("ofek-dev.provider-config-sync")
    client = TestClient(main.app)
    try:
        allowed = client.post(
            "/api/internal/capabilities/invoke",
            headers={"X-Internal-Token": token},
            json={"capability": "switch-control", "action": "test.ping", "payload": {}},
        )
        denied = client.post(
            "/api/internal/extension-settings",
            headers={"X-Internal-Token": token},
            json={},
        )
        pcs_allowed = client.post(
            "/api/internal/capabilities/invoke",
            headers={"X-Internal-Token": pcs_token},
            json={
                "capability": "provider-config-sync",
                "action": "change.broadcast",
                "payload": {"scope": "global", "category": "mcp", "capability_id": "x", "path": "", "cwd": ""},
            },
        )
        pcs_denied = client.post(
            "/api/internal/extension-settings",
            headers={"X-Internal-Token": pcs_token},
            json={},
        )
        assert allowed.status_code == 200
        assert denied.status_code == 403
        assert pcs_allowed.status_code == 200
        assert pcs_denied.status_code == 403
        assert denied.json()["detail"] == "internal route requires internal_loopback permission"
    finally:
        client.close()
        capability_api._ACTIONS.pop(("switch-control", "test.ping"), None)
        capability_api._ACTIONS[("provider-config-sync", "change.broadcast")] = original_broadcast
