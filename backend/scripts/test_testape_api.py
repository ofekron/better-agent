"""Unit coverage for testape_api.py — the FastAPI router that exposes the
TestApe login-state and chat-panel detectors over REST.

The detectors themselves are covered by test_testape_login_detector.py and
test_testape_chat_panel_detector.py. What is NOT covered — and what this
module locks — is the router's HTTP error contract: which detector outcome
maps to which status code, and that query params (plus the fs_url default)
are wired through to the detector seam. The detector functions are mocked
at the import seam (testape_api.detector / testape_api.chat_detector); the
result objects are REAL dataclasses so their .to_dict() serialization is
exercised truthfully.

No real TestApe browser, no network, no live model: in-process TestClient.

Run:
    cd backend && .venv/bin/python -m pytest scripts/test_testape_api.py \
        --cov=testape_api --cov-report=term-missing --cov-branch
"""
from __future__ import annotations

from unittest import mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import testape_api
from testape_chat_panel_detector import ChatPanelValidation
from testape_login_detector import LoginState

app = FastAPI()
app.include_router(testape_api.router)
client = TestClient(app)


# ----------------------------- /login-state --------------------------------

def test_login_state_happy_returns_dict_with_default_fs_url():
    expected = LoginState("authenticated", True, "ad-1", title="App")
    with mock.patch.object(
        testape_api.detector, "detect_login_state", return_value=expected
    ) as spy:
        resp = client.get("/api/testape/login-state")

    assert resp.status_code == 200
    assert resp.json()["state"] == "authenticated"
    assert resp.json()["logged_in"] is True
    assert resp.json()["adapter_id"] == "ad-1"
    assert resp.json()["title"] == "App"
    # fs_url omitted -> detector.FS_DEFAULT default is wired through.
    assert spy.call_args.kwargs["fs_url"] == testape_api.detector.FS_DEFAULT
    assert spy.call_args.kwargs["adapter_id"] is None
    assert spy.call_args.kwargs["url"] is None


def test_login_state_passes_adapter_url_and_fs_url_through():
    expected = LoginState("login", False, "ad-9")
    with mock.patch.object(
        testape_api.detector, "detect_login_state", return_value=expected
    ) as spy:
        resp = client.get(
            "/api/testape/login-state",
            params={"adapter_id": "ad-9", "url": "http://127.0.0.1:8080", "fs_url": "http://localhost:9999/v1"},
        )
    assert resp.status_code == 200
    assert spy.call_args.kwargs["adapter_id"] == "ad-9"
    assert spy.call_args.kwargs["url"] == "http://127.0.0.1:8080"
    # Explicit fs_url overrides the default.
    assert spy.call_args.kwargs["fs_url"] == "http://localhost:9999/v1"


def test_login_state_value_error_maps_to_400():
    with mock.patch.object(
        testape_api.detector, "detect_login_state", side_effect=ValueError("bad adapter")
    ):
        resp = client.get("/api/testape/login-state")
    assert resp.status_code == 400
    assert resp.json()["detail"] == "bad adapter"


def test_login_state_generic_error_maps_to_502():
    with mock.patch.object(
        testape_api.detector, "detect_login_state", side_effect=RuntimeError("eval_js failed")
    ):
        resp = client.get("/api/testape/login-state")
    assert resp.status_code == 502
    assert "testape detection failed" in resp.json()["detail"]
    assert "eval_js failed" in resp.json()["detail"]


# --------------------------- /chat-panel/validate --------------------------

def test_chat_panel_validate_ok_returns_200_with_default_fs_url():
    expected = ChatPanelValidation(True, "ad-1", "sess-1")
    with mock.patch.object(
        testape_api.chat_detector, "validate_chat_panel", return_value=expected
    ) as spy:
        resp = client.get("/api/testape/chat-panel/validate")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["adapter_id"] == "ad-1"
    assert body["session_id"] == "sess-1"
    # fs_url default wired through on the chat endpoint too.
    assert spy.call_args.kwargs["fs_url"] == testape_api.detector.FS_DEFAULT


def test_chat_panel_validate_passes_adapter_session_url_fs_url_through():
    expected = ChatPanelValidation(True, "ad-2", "sess-2")
    with mock.patch.object(
        testape_api.chat_detector, "validate_chat_panel", return_value=expected
    ) as spy:
        resp = client.get(
            "/api/testape/chat-panel/validate",
            params={
                "adapter_id": "ad-2",
                "session_id": "sess-2",
                "url": "http://localhost:3000",
                "fs_url": "http://localhost:7777/v1",
            },
        )
    assert resp.status_code == 200
    k = spy.call_args.kwargs
    assert k["adapter_id"] == "ad-2"
    assert k["session_id"] == "sess-2"
    assert k["url"] == "http://localhost:3000"
    assert k["fs_url"] == "http://localhost:7777/v1"


def test_chat_panel_validate_not_ok_maps_to_409_with_detail_body():
    expected = ChatPanelValidation(
        False, "ad-1", "sess-1", mismatches=["messages differ"], reason="drift"
    )
    with mock.patch.object(
        testape_api.chat_detector, "validate_chat_panel", return_value=expected
    ):
        resp = client.get("/api/testape/chat-panel/validate")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["ok"] is False
    assert detail["mismatches"] == ["messages differ"]
    assert detail["reason"] == "drift"


def test_chat_panel_value_error_maps_to_400():
    with mock.patch.object(
        testape_api.chat_detector, "validate_chat_panel", side_effect=ValueError("no adapters")
    ):
        resp = client.get("/api/testape/chat-panel/validate")
    assert resp.status_code == 400
    assert resp.json()["detail"] == "no adapters"


def test_chat_panel_generic_error_maps_to_502():
    with mock.patch.object(
        testape_api.chat_detector, "validate_chat_panel", side_effect=OSError("boom")
    ):
        resp = client.get("/api/testape/chat-panel/validate")
    assert resp.status_code == 502
    assert "testape chat panel validation failed" in resp.json()["detail"]
    assert "boom" in resp.json()["detail"]


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
