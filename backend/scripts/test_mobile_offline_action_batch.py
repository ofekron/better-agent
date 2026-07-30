from __future__ import annotations

import os
import shutil
import sys
import uuid
from pathlib import Path

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-mobile-offline-batch-")
_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_installation  # noqa: E402

_test_installation.activate(Path(_TMP_HOME))

from starlette.testclient import TestClient  # noqa: E402

import auth  # noqa: E402
import main  # noqa: E402
import offline_actions_api  # noqa: E402


def teardown_module() -> None:
    shutil.rmtree(_TMP_HOME, ignore_errors=True)


def _client(*, authenticated: bool = True) -> TestClient:
    client = TestClient(main.app, client=("127.0.0.1", 50000))
    if authenticated:
        client.headers.update({
            "Authorization": f"Bearer {auth.create_token('mobile-background')}",
        })
    return client


def _send_action(
    *,
    session_id: str,
    client_id: str,
    prompt: str,
    **extra: object,
) -> dict:
    return {
        "type": "send_message",
        "sessionId": session_id,
        "clientId": client_id,
        "prompt": prompt,
        "model": "test-model",
        "cwd": str(_TMP_HOME),
        **extra,
    }


def test_batch_preserves_order_and_acknowledges_duplicates(monkeypatch) -> None:
    session_id = str(uuid.uuid4())
    first_client_id = str(uuid.uuid4())
    second_client_id = str(uuid.uuid4())
    calls: list[tuple[str, str]] = []
    accepted: set[tuple[str, str]] = set()

    async def fake_send(action: dict) -> dict:
        identity = (action["sessionId"], action["clientId"])
        calls.append(identity)
        duplicate = identity in accepted
        accepted.add(identity)
        return {"duplicate": duplicate, "enqueued": not duplicate}

    monkeypatch.setattr(main, "_admit_offline_batch_message", fake_send)
    response = _client().post(
        "/api/offline-actions/batch",
        json={"actions": [
            _send_action(
                session_id=session_id,
                client_id=first_client_id,
                prompt="first",
            ),
            _send_action(
                session_id=session_id,
                client_id=first_client_id,
                prompt="first retry",
            ),
            _send_action(
                session_id=session_id,
                client_id=second_client_id,
                prompt="second",
            ),
        ]},
    )

    assert response.status_code == 200, response.text
    assert calls == [
        (session_id, first_client_id),
        (session_id, first_client_id),
        (session_id, second_client_id),
    ]
    results = response.json()["results"]
    assert [result["index"] for result in results] == [0, 1, 2]
    assert [result["accepted"] for result in results] == [True, True, True]
    assert [result["duplicate"] for result in results] == [False, True, False]


def test_batch_preserves_create_before_dependent_send(monkeypatch) -> None:
    session_id = str(uuid.uuid4())
    create_client_id = str(uuid.uuid4())
    send_client_id = str(uuid.uuid4())
    calls: list[tuple[str, str]] = []

    async def fake_create(action: dict) -> dict:
        calls.append(("create", action["session"]["id"]))
        return {"duplicate": False, "created": True}

    async def fake_send(action: dict) -> dict:
        calls.append(("send", action["sessionId"]))
        return {"duplicate": False, "enqueued": True}

    monkeypatch.setattr(main, "_admit_offline_batch_create", fake_create)
    monkeypatch.setattr(main, "_admit_offline_batch_message", fake_send)
    response = _client().post(
        "/api/offline-actions/batch",
        json={"actions": [
            {
                "type": "create_session",
                "clientId": create_client_id,
                "session": {
                    "id": session_id,
                    "name": "",
                    "model": "test-model",
                    "cwd": str(_TMP_HOME),
                },
                "prompt": "",
            },
            _send_action(
                session_id=session_id,
                client_id=send_client_id,
                prompt="after create",
            ),
        ]},
    )

    assert response.status_code == 200, response.text
    assert calls == [("create", session_id), ("send", session_id)]
    assert [result["accepted"] for result in response.json()["results"]] == [True, True]


def test_batch_returns_partial_rejection_and_continues(monkeypatch) -> None:
    session_id = str(uuid.uuid4())
    valid_client_id = str(uuid.uuid4())
    calls: list[str] = []

    async def fake_send(action: dict) -> dict:
        calls.append(action["clientId"])
        return {"duplicate": False, "enqueued": True}

    monkeypatch.setattr(main, "_admit_offline_batch_message", fake_send)
    response = _client().post(
        "/api/offline-actions/batch",
        json={"actions": [
            _send_action(
                session_id=session_id,
                client_id="",
                prompt="rejected",
            ),
            _send_action(
                session_id=session_id,
                client_id=valid_client_id,
                prompt="accepted",
            ),
        ]},
    )

    assert response.status_code == 200, response.text
    assert calls == [valid_client_id]
    rejected, accepted = response.json()["results"]
    assert rejected["accepted"] is False
    assert rejected["status"] == 400
    assert accepted["accepted"] is True


def test_existing_non_uuid_client_ids_are_preserved(monkeypatch) -> None:
    session_id = str(uuid.uuid4())
    client_ids = ["pending-123", f"offline-create-{session_id}"]
    calls: list[str] = []

    async def fake_send(action: dict) -> dict:
        calls.append(action["clientId"])
        return {"duplicate": False, "enqueued": True}

    monkeypatch.setattr(main, "_admit_offline_batch_message", fake_send)
    response = _client().post(
        "/api/offline-actions/batch",
        json={"actions": [
            _send_action(session_id=session_id, client_id=client_id, prompt="queued")
            for client_id in client_ids
        ]},
    )

    assert response.status_code == 200, response.text
    assert calls == client_ids
    assert [result["client_id"] for result in response.json()["results"]] == client_ids


def test_malformed_create_attachment_rejects_before_creation(monkeypatch) -> None:
    created: list[dict] = []

    async def fake_create_session(body: dict) -> dict:
        created.append(body)
        return {"id": body["client_session_id"]}

    monkeypatch.setattr(main, "create_session", fake_create_session)
    response = _client().post(
        "/api/offline-actions/batch",
        json={"actions": [{
            "type": "create_session",
            "clientId": "offline-create-test",
            "session": {
                "id": str(uuid.uuid4()),
                "name": "",
                "model": "test-model",
                "cwd": str(_TMP_HOME),
            },
            "prompt": "queued",
            "images": [{"data": "not-base64!", "media_type": "image/png"}],
        }]},
    )

    assert response.status_code == 200, response.text
    assert response.json()["results"][0]["accepted"] is False
    assert created == []


def test_attachment_validation_rejects_invalid_base64_and_size_mismatch() -> None:
    import pytest

    with pytest.raises(Exception):
        offline_actions_api._validate_offline_batch_attachments(
            [{"data": "not-base64!", "media_type": "image/png"}],
            [],
        )
    with pytest.raises(Exception):
        offline_actions_api._validate_offline_batch_attachments(
            [],
            [{"name": "a.txt", "data": "YQ==", "media_type": "text/plain", "size": 2}],
        )


def test_batch_requires_authentication(monkeypatch) -> None:
    calls: list[dict] = []

    async def fake_send(action: dict) -> dict:
        calls.append(action)
        return {"duplicate": False, "enqueued": True}

    monkeypatch.setattr(main, "_admit_offline_batch_message", fake_send)
    response = _client(authenticated=False).post(
        "/api/offline-actions/batch",
        json={"actions": [
            _send_action(
                session_id=str(uuid.uuid4()),
                client_id=str(uuid.uuid4()),
                prompt="must not reach admission",
            ),
        ]},
    )

    assert response.status_code == 401, response.text
    assert calls == []


def test_batch_rejects_unknown_envelope_fields() -> None:
    response = _client().post(
        "/api/offline-actions/batch",
        json={"actions": [], "unexpected": True},
    )
    assert response.status_code == 400, response.text
