#!/usr/bin/env python3
"""POST /api/sessions must be idempotent on client_session_id.

Regression lock for the duplicate/orphan session bug: a create attempt that
lands server-side but is reported to the client as a timeout/5xx gets replayed.
Without idempotency the replay either raced into a 500
("session id already exists") or, when the client minted a fresh id, left the
first session behind as an empty "Session HH:MM".
"""
from __future__ import annotations

import os
import shutil
import sys
import uuid

from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-create-idempotent-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_installation  # noqa: E402
_test_installation.activate(Path(_TMP_HOME))

from starlette.testclient import TestClient  # noqa: E402

import auth_test_helpers  # noqa: E402
import config_store  # noqa: E402
import main  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402


def _seed_default_provider() -> None:
    default = config_store.get_default_provider() or {}
    payload: dict = {}
    if not default.get("name"):
        payload["name"] = "Default Test Provider"
    if not default.get("default_model"):
        payload["default_model"] = "test-model"
        payload["custom_models"] = ["test-model"]
    if payload:
        config_store.update_provider(default["id"], payload)


_seed_default_provider()


def teardown_module():
    shutil.rmtree(_TMP_HOME, ignore_errors=True)


def _client() -> TestClient:
    client = TestClient(main.app, client=("127.0.0.1", 50000))
    auth_test_helpers.authenticate_client(client)
    return client


def _body(sid: str) -> dict:
    return {"name": "", "cwd": str(Path(_TMP_HOME)), "client_session_id": sid}


def test_replayed_create_returns_same_session():
    sid = str(uuid.uuid4())
    client = _client()
    first = client.post("/api/sessions", json=_body(sid))
    assert first.status_code == 200, first.text
    second = client.post("/api/sessions", json=_body(sid))
    assert second.status_code == 200, second.text
    assert first.json()["id"] == sid
    assert second.json()["id"] == sid


def test_lost_race_returns_existing_instead_of_500(monkeypatch):
    """The pre-check and the create are not one atomic step.

    Two replays of the same queued create can both pass the pre-check; the
    loser then hits the store's "session id already exists" guard. Simulate
    that deterministically by blinding the pre-check for a session that
    already exists, and require the loser to resolve to the same session.
    """
    sid = str(uuid.uuid4())
    client = _client()
    winner = client.post("/api/sessions", json=_body(sid))
    assert winner.status_code == 200, winner.text

    real_get = session_manager.get
    blinded = {sid}

    def blind_once(requested, *a, **kw):
        # Only the pre-check is blinded; the collision recovery lookup that
        # follows must still resolve the winner's session.
        if requested in blinded:
            blinded.discard(requested)
            return None
        return real_get(requested, *a, **kw)

    monkeypatch.setattr(session_manager, "get", blind_once)
    loser = client.post("/api/sessions", json=_body(sid))
    monkeypatch.undo()

    assert loser.status_code == 200, loser.text
    assert loser.json()["id"] == sid
    assert session_manager.get(sid) is not None
