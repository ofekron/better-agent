from __future__ import annotations

import os
from pathlib import Path

import _test_home

_HOME = _test_home.isolate("bc-test-provider-route-authority-")
os.environ["BETTER_CLAUDE_TEST_AUTH_BYPASS"] = "1"

import _test_installation

_test_installation.activate(Path(_HOME))

import auth
import config_store
import main
from fastapi.testclient import TestClient


def _client() -> TestClient:
    client = TestClient(main.app, client=("127.0.0.1", 50000))
    client.headers.update({"Authorization": f"Bearer {auth.create_token('test')}"})
    return client


def _provider(name: str) -> dict:
    return config_store.add_provider({
        "name": name,
        "kind": "claude",
        "mode": "subscription",
        "default_model": "model",
        "custom_models": ["model"],
    })


def _authority(provider: dict) -> dict:
    return {
        "expected_generation": provider["generation"],
        "expected_revision": provider["revision"],
    }


def _default_authority(target: dict, state: dict) -> dict:
    current = next(
        provider
        for provider in state["providers"]
        if provider["id"] == state["default_provider_id"]
    )
    return {
        **_authority(target),
        "expected_default_provider_id": current["id"],
        "expected_default_generation": current["generation"],
        "expected_default_revision": current["revision"],
    }


def test_provider_routes_reject_stale_record_authority() -> None:
    client = _client()

    patched = _provider("patch")
    first = client.patch(
        f"/api/providers/{patched['id']}",
        json={**_authority(patched), "nickname": "first"},
    )
    assert first.status_code == 200, first.text
    stale = client.patch(
        f"/api/providers/{patched['id']}",
        json={**_authority(patched), "nickname": "stale"},
    )
    assert stale.status_code == 409, stale.text

    suspended = _provider("suspend")
    config_store.update_provider(suspended["id"], {"nickname": "advanced"})
    stale = client.post(
        f"/api/providers/{suspended['id']}/suspended",
        json={**_authority(suspended), "suspended": True},
    )
    assert stale.status_code == 409, stale.text

    deleted = _provider("delete")
    config_store.update_provider(deleted["id"], {"nickname": "advanced"})
    stale = client.request(
        "DELETE",
        f"/api/providers/{deleted['id']}",
        json=_authority(deleted),
    )
    assert stale.status_code == 409, stale.text


def test_set_default_rejects_stale_target_and_current_default() -> None:
    client = _client()
    target = _provider("target")
    initial = config_store.list_providers()
    stale_target_payload = _default_authority(target, initial)
    config_store.update_provider(target["id"], {"nickname": "advanced"})
    stale = client.post(
        f"/api/providers/{target['id']}/set-default",
        json=stale_target_payload,
    )
    assert stale.status_code == 409, stale.text

    target = config_store.get_provider(target["id"])
    state = config_store.list_providers()
    stale_default_payload = _default_authority(target, state)
    current_id = state["default_provider_id"]
    config_store.update_provider(current_id, {"nickname": "advanced"})
    stale = client.post(
        f"/api/providers/{target['id']}/set-default",
        json=stale_default_payload,
    )
    assert stale.status_code == 409, stale.text


def test_provider_mutations_require_authority() -> None:
    client = _client()
    provider = _provider("required")
    assert client.patch(
        f"/api/providers/{provider['id']}",
        json={"nickname": "missing"},
    ).status_code == 422
    assert client.post(
        f"/api/providers/{provider['id']}/suspended",
        json={"suspended": True},
    ).status_code == 422
    assert client.request(
        "DELETE",
        f"/api/providers/{provider['id']}",
        json={},
    ).status_code == 422
    assert client.post(
        f"/api/providers/{provider['id']}/set-default",
        json=_authority(provider),
    ).status_code == 422
