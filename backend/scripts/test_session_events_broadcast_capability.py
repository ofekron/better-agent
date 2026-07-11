"""Regression for the lag-incident spool wedge (assistant bug-report 500s).

The scoped-capabilities migration removed `internal_loopback` from the
assistant extension, so its WS publish via POST /api/internal/broadcast-session
started 403ing, every lag-watchdog bug-report dispatch 500'd, and the spool
filled to _MAX_PENDING ("lag incident spool is full"). The fix is a scoped
`session-events.broadcast` capability routed through the capability router.
These assertions lock:
  * an extension granted `session-events.broadcast` can broadcast, with the
    event source pinned to ITS extension id (no impersonation),
  * an extension without the grant is rejected fail-closed,
  * the auth gate still denies direct /api/internal/broadcast-session access
    to capability-token extensions lacking `internal_loopback` (the wide
    middleware exemption stays reverted).
"""
from __future__ import annotations

import os
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

EXT_ID = "ofek.test-broadcaster"


def _arm(monkeypatch, grants: list[str]) -> str:
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


def test_granted_extension_broadcasts_with_pinned_source(monkeypatch) -> None:
    import main

    token = _arm(monkeypatch, ["session-events.broadcast"])
    captured: list[tuple[str, str, dict, str]] = []

    async def fake_broadcast(app_session_id, event_type, data, *, source):
        captured.append((app_session_id, event_type, data, source))

    monkeypatch.setattr(main.coordinator, "broadcast_session", fake_broadcast)
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
                    "event_type": "assistant.board_updated",
                    "data": {"status": "open"},
                },
            },
        )
    finally:
        client.close()
    assert response.status_code == 200, response.text
    assert captured == [("s-1", "assistant.board_updated", {"status": "open"}, f"extension:{EXT_ID}")]


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
                "payload": {"session_id": "s-1", "event_type": "x", "data": {}},
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
            "/api/internal/broadcast-session",
            headers={"X-Internal-Token": token},
            json={"session_id": "s-1", "event_type": "x", "data": {}},
        )
    finally:
        client.close()
    assert response.status_code == 403
    assert response.json()["detail"] == "internal route requires internal_loopback permission"


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
