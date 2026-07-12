from __future__ import annotations

from dataclasses import replace
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "sdk"))

import paths

paths.engage_test_home(tempfile.mkdtemp(prefix="ba-ambient-http-auth-"))
os.environ["BETTER_AGENT_TEST_MODE"] = "1"

from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict

import ambient_mcp_broker
import ambient_principal
import capability_api
import extension_store
import main


class _Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str


def test_broker_token_is_route_scoped_connection_bound_and_revoked(monkeypatch) -> None:
    record = {
        "enabled": True,
        "entitlement": {"status": "active"},
        "manifest": {
            "id": "test.extension",
            "permissions": {"capabilities": ["test.allowed"]},
            "entrypoints": {"mcp": [{
                "name": "tools",
                "native_exposure": {
                    "allowed": True,
                    "permissions": ["test.allowed", "internal_loopback"],
                },
            }]},
        },
    }
    monkeypatch.setattr(extension_store, "get_extension", lambda extension_id: record)
    monkeypatch.setattr(extension_store, "is_extension_active", lambda extension_id: True)
    monkeypatch.setattr(extension_store, "native_harness_exposed", lambda *args, **kwargs: True)
    monkeypatch.setattr(main, "_require_builtin_runtime_extension", lambda extension_id: None)
    capability_api._ACTIONS[("test", "allowed")] = capability_api._Action(
        _Payload, lambda payload: {"value": payload.value}
    )
    broker = ambient_mcp_broker.AmbientMcpBroker()
    principal_id = ""
    try:
        principal_id, token = broker._issue({
            "extension_id": "test.extension",
            "server_name": "tools",
            "provider_id": "codex",
            "pid": os.getpid(),
        }, str(os.getuid()) if hasattr(os, "getuid") else "test-user")
        client = TestClient(main.app, client=("127.0.0.1", 50000))
        headers = {"X-Internal-Token": token}
        allowed = client.post(
            "/api/internal/capabilities/invoke",
            headers=headers,
            json={"capability": "test", "action": "allowed", "payload": {"value": "ok"}},
        )
        assert allowed.status_code == 200, allowed.text
        assert allowed.json() == {"value": "ok"}

        denied_scope = client.post(
            "/api/internal/capabilities/invoke",
            headers=headers,
            json={"capability": "test", "action": "denied", "payload": {}},
        )
        assert denied_scope.status_code == 403, denied_scope.text
        denied_route = client.post("/api/internal/broadcast-session", headers=headers, json={})
        assert denied_route.status_code == 403, denied_route.text

        lock = client.post(
            "/api/internal/coordination/lock-ops",
            headers=headers,
            json={"key": "file_edit:/tmp/ambient-auth-test"},
        )
        assert lock.status_code == 200, lock.text
        assert lock.json()["success"] is True

        registry_record = ambient_principal.registry._records[principal_id]
        registry_record.principal = replace(registry_record.principal, expires_at=-1.0)
        assert ambient_principal.registry.resolve(token) is not None
        still_alive = client.post(
            "/api/internal/capabilities/invoke",
            headers=headers,
            json={"capability": "test", "action": "allowed", "payload": {"value": "alive"}},
        )
        assert still_alive.status_code == 200, still_alive.text

        broker._revoke(principal_id)
        principal_id = ""
        revoked = client.post(
            "/api/internal/capabilities/invoke",
            headers=headers,
            json={"capability": "test", "action": "allowed", "payload": {"value": "no"}},
        )
        assert revoked.status_code == 403, revoked.text
        client.close()
    finally:
        capability_api._ACTIONS.pop(("test", "allowed"), None)
        if principal_id:
            broker._revoke(principal_id)


def test_core_broker_registry_is_closed(monkeypatch) -> None:
    broker = ambient_mcp_broker.AmbientMcpBroker()
    peer_user = str(os.getuid()) if hasattr(os, "getuid") else "test-user"
    principal_id, token = broker._issue(
        {"source_kind": "core", "server_name": "ui", "provider_id": "codex"},
        peer_user,
    )
    try:
        principal = ambient_principal.registry.resolve(token)
        assert principal is not None
        assert principal.source_kind == "core"
        assert principal.core_server == "ui"
        assert principal.permissions == frozenset({"ui.open_file_panel", "ui.request_user_input"})
        try:
            broker._issue(
                {"source_kind": "core", "server_name": "arbitrary", "provider_id": "codex"},
                peer_user,
            )
        except PermissionError:
            pass
        else:
            raise AssertionError("unregistered core ambient MCP was accepted")
    finally:
        broker._revoke(principal_id)


def test_core_broker_tokens_reach_only_their_explicit_session_routes(monkeypatch) -> None:
    broker = ambient_mcp_broker.AmbientMcpBroker()
    peer_user = str(os.getuid()) if hasattr(os, "getuid") else "test-user"
    issued: list[str] = []

    def issue(server_name: str) -> str:
        principal_id, token = broker._issue(
            {"source_kind": "core", "server_name": server_name, "provider_id": "codex"},
            peer_user,
        )
        issued.append(principal_id)
        return token

    async def session_lite(sid: str):
        return {"id": sid, "cwd": "/tmp"} if sid == "session-1" else None

    monkeypatch.setattr(main, "_session_lite", session_lite)
    monkeypatch.setattr(main.session_manager, "get", lambda sid: {"id": sid} if sid == "session-1" else None)
    monkeypatch.setattr(main.extension_store, "capability_catalog", lambda: {})
    monkeypatch.setattr(main.file_editor, "is_file_editor_session", lambda sid: sid == "session-1")
    monkeypatch.setattr(
        main.file_editor,
        "start_discussion",
        lambda *args, **kwargs: {"id": "discussion-1"},
    )
    ui_token = issue("ui")
    config_token = issue("open-config-panel")
    capabilities_token = issue("capabilities")
    client = TestClient(main.app, client=("127.0.0.1", 50000))
    try:
        ui_headers = {"X-Internal-Token": ui_token}
        opened = client.post(
            "/api/internal/open-file-panel",
            headers=ui_headers,
            json={"app_session_id": "session-1", "mode": "inline", "path": "a.txt"},
        )
        assert opened.status_code == 200 and opened.json()["success"] is True, opened.text
        discussion = client.post(
            "/api/internal/file-editor/start-discussion",
            headers=ui_headers,
            json={"app_session_id": "session-1", "file_path": "/tmp/a", "line": 1},
        )
        assert discussion.status_code == 200 and discussion.json()["success"] is True

        async def completed(*args, **kwargs):
            return {"request_id": "r1", "app_session_id": "session-1", "status": "resolved", "answers": {}}

        monkeypatch.setattr(main.user_input_store, "create_request", lambda **kwargs: {
            "request_id": "r1", "app_session_id": kwargs["app_session_id"], "questions": kwargs["questions"],
        })
        monkeypatch.setattr(main.user_input_store, "wait_for_completion", completed)
        monkeypatch.setattr(main, "_broadcast_user_input", lambda *args, **kwargs: completed())
        monkeypatch.setattr(main, "_broadcast_user_input_state", lambda *args, **kwargs: completed())
        user_input = client.post(
            "/api/internal/user-input/request",
            headers=ui_headers,
            json={
                "app_session_id": "session-1",
                "questions": [{"id": "q", "header": "H", "question": "Q", "options": []}],
            },
        )
        assert user_input.status_code == 200 and user_input.json()["success"] is True

        config = client.post(
            "/api/internal/open-config-panel",
            headers={"X-Internal-Token": config_token},
            json={"app_session_id": "session-1", "capability_id": "cap"},
        )
        assert config.status_code == 200 and config.json()["success"] is True
        listed = client.post(
            "/api/internal/sessions/session-1/capabilities",
            headers={"X-Internal-Token": capabilities_token},
            json={"app_session_id": "session-1", "action": "list"},
        )
        assert listed.status_code == 200 and listed.json()["ok"] is True

        wrong_scope = client.post(
            "/api/internal/open-config-panel",
            headers=ui_headers,
            json={"app_session_id": "session-1", "capability_id": "cap"},
        )
        assert wrong_scope.status_code == 403
        wrong_session = client.post(
            "/api/internal/sessions/session-1/capabilities",
            headers={"X-Internal-Token": capabilities_token},
            json={"app_session_id": "session-2", "action": "list"},
        )
        assert wrong_session.status_code == 403
        missing_session = client.post(
            "/api/internal/open-file-panel",
            headers=ui_headers,
            json={"app_session_id": "missing", "mode": "inline", "path": "a.txt"},
        )
        assert missing_session.status_code == 200 and missing_session.json()["success"] is False
        denied_route = client.post("/api/internal/broadcast-session", headers=ui_headers, json={})
        assert denied_route.status_code == 403
    finally:
        client.close()
        for principal_id in issued:
            broker._revoke(principal_id)
