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
from pydantic import BaseModel, ConfigDict

import capability_api
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


def test_public_sdk_has_no_raw_route_transport() -> None:
    assert not hasattr(Client, "request_internal")
    client = Client(internal_token="token")
    try:
        client.invoke_capability("ask", "ensure", {}, path="/api/internal/ask-ui/ensure")
    except TypeError:
        pass
    else:
        raise AssertionError("invoke_capability accepted a raw path")
