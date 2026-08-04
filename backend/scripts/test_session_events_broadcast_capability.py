"""Regression for the lag-incident spool wedge (assistant bug-report 500s).

The scoped-capabilities migration removed `internal_loopback` from the
assistant extension, so its WS publish via POST /api/internal/sessions/{sid}/broadcast
started 403ing, every lag-watchdog bug-report dispatch 500'd, and the spool
filled to _MAX_PENDING ("lag incident spool is full"). The fix is a scoped
`session-events.broadcast` capability routed through the capability router.
These assertions lock:
  * an extension granted `session-events.broadcast` can broadcast, with the
    event source pinned to ITS extension id (no impersonation),
  * an extension without the grant is rejected fail-closed,
  * the auth gate still denies direct /api/internal/sessions/{sid}/broadcast
    access to capability-token extensions lacking `internal_loopback` (the
    wide middleware exemption stays reverted).
"""
from __future__ import annotations

import inspect
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "sdk"))

import paths  # noqa: E402

paths.engage_test_home(tempfile.mkdtemp(prefix="ba-session-events-broadcast-"))

from fastapi.testclient import TestClient  # noqa: E402

import capability_api  # noqa: E402
import installation_profile  # noqa: E402
from better_agent_sdk.client import Client as SDKClient  # noqa: E402

EXT_ID = "ofek.test-broadcaster"


def _arm(monkeypatch, grants: list[str]) -> str:
    monkeypatch.setattr(
        installation_profile,
        "allows",
        lambda capability: capability == installation_profile.INTEGRATIONS,
    )
    record = {
        "enabled": True,
        "manifest": {"id": EXT_ID, "permissions": {"capabilities": grants}},
        "entitlement": {"status": "not_required"},
    }
    original_get = capability_api.extension_store.get_extension
    original_active = capability_api.extension_store.is_extension_active
    monkeypatch.setattr(
        capability_api.extension_store,
        "get_extension",
        lambda extension_id: record if extension_id == EXT_ID else original_get(extension_id),
    )
    monkeypatch.setattr(
        capability_api.extension_store,
        "is_extension_active",
        lambda extension_id: extension_id == EXT_ID or original_active(extension_id),
    )
    return capability_api.extension_token_registry.mint(EXT_ID)


def test_granted_extension_broadcasts_encapsulated_event_with_pinned_source(monkeypatch) -> None:
    import internal_extension_api
    import main

    token = _arm(monkeypatch, ["session-events.broadcast"])
    captured: list[tuple[str, str, dict, str]] = []

    async def fake_broadcast(app_session_id, event_type, data, *, source):
        captured.append((app_session_id, event_type, data, source))

    monkeypatch.setattr(internal_extension_api, "_broadcast_session", fake_broadcast)
    client = TestClient(main.app)
    try:
        response = client.post(
            "/api/internal/capabilities/invoke",
            headers={"X-Internal-Token": token},
            json={
                "capability": "session-events",
                "action": "broadcast",
                "payload": {
                    "session_id": "s-1",
                    "event_name": "session_deleted",
                    "data": {"status": "open"},
                },
            },
        )
    finally:
        client.close()
    assert response.status_code == 200, response.text
    assert captured == [(
        "s-1",
        "extension_event",
        {
            "extension_id": EXT_ID,
            "event_name": "session_deleted",
            "data": {"status": "open"},
        },
        f"extension:{EXT_ID}",
    )]
    assert response.json()["event_type"] == "extension_event"
    assert response.json()["event_name"] == "session_deleted"


def test_ungranted_extension_is_rejected(monkeypatch) -> None:
    import main

    token = _arm(monkeypatch, ["some-other.capability"])
    client = TestClient(main.app)
    try:
        response = client.post(
            "/api/internal/capabilities/invoke",
            headers={"X-Internal-Token": token},
            json={
                "capability": "session-events",
                "action": "broadcast",
                "payload": {"session_id": "s-1", "event_name": "x", "data": {}},
            },
        )
    finally:
        client.close()
    assert response.status_code == 403
    assert response.json()["detail"] == "capability action is not granted"


def test_direct_broadcast_route_still_requires_internal_loopback(monkeypatch) -> None:
    import main

    token = _arm(monkeypatch, ["session-events.broadcast"])
    client = TestClient(main.app)
    try:
        response = client.post(
            "/api/internal/sessions/s-1/broadcast",
            headers={"X-Internal-Token": token},
            json={"event_name": "x", "data": {}},
        )
    finally:
        client.close()
    assert response.status_code == 403
    assert response.json()["detail"] == "internal route requires internal_loopback permission"


def test_direct_broadcast_is_not_a_narrow_extension_route() -> None:
    import main

    source = inspect.getsource(main.auth_gate)
    narrow = source[source.index("narrow_extension_routes"):source.index("if principal[0]")]
    assert '"/api/internal/sessions/{sid}/broadcast"' not in narrow
    assert '"/api/internal/broadcast-session"' not in narrow


def test_sdk_posts_inner_event_name(monkeypatch) -> None:
    captured: list[tuple[str, dict, float]] = []
    client = SDKClient(
        internal_token="token",
        extension_id=EXT_ID,
        app_session_id="s-1",
        backend_url="http://core",
    )

    def fake_post(path: str, payload: dict, *, timeout: float):
        captured.append((path, payload, timeout))
        return {"success": True}

    monkeypatch.setattr(client, "_post", fake_post)
    client.broadcast_session_event("assistant.board_updated", {"status": "open"})
    client.notify_toast("saved")

    assert captured == [
        (
            "/api/internal/sessions/s-1/broadcast",
            {
                "event_name": "assistant.board_updated",
                "data": {"status": "open"},
            },
            10.0,
        ),
        (
            "/api/internal/sessions/s-1/broadcast",
            {
                "event_name": "extension_toast",
                "data": {"message": "saved", "level": "info"},
            },
            10.0,
        ),
    ]


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
