from __future__ import annotations

import os
import sys
import tempfile

os.environ["BETTER_AGENT_HOME"] = tempfile.mkdtemp(prefix="auth_change_test_")

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import auth  # noqa: E402
import auth_routes  # noqa: E402
import auth_secrets  # noqa: E402

_app = FastAPI()
_app.add_middleware(SessionMiddleware, secret_key="test-session-secret")
_app.include_router(auth_routes.router)
_client = TestClient(_app)
_written: dict[str, str] = {}


def _reset() -> None:
    auth._BOOTSTRAPPED = True
    auth._USERNAME = "alice"
    auth._PASSWORD_HASH = auth_secrets.make_password_hash("old-password")
    auth.SESSION_SECRET = "0" * 64
    auth._rl_attempts.clear()
    _written.clear()


def _fake_write(username: str, password: str) -> None:
    _written["username"] = username
    _written["password"] = password


def _fake_reload() -> None:
    auth._USERNAME = _written["username"]
    auth._PASSWORD_HASH = auth_secrets.make_password_hash(_written["password"])
    auth._BOOTSTRAPPED = True


auth_secrets.write_login_credentials = _fake_write
auth.reload_credentials = _fake_reload


def _login() -> None:
    response = _client.post(
        "/api/auth/login",
        json={"username": "alice", "password": "old-password"},
    )
    assert response.status_code == 200, response.text


def test_requires_authenticated_session() -> None:
    _reset()
    client = TestClient(_app)
    response = client.post(
        "/api/auth/change_credentials",
        json={
            "current_username": "alice",
            "current_password": "old-password",
            "new_username": "bob",
            "new_password": "new-password",
        },
    )
    assert response.status_code == 401, response.text


def test_rejects_wrong_current_credentials() -> None:
    _reset()
    _login()
    response = _client.post(
        "/api/auth/change_credentials",
        json={
            "current_username": "alice",
            "current_password": "wrong-password",
            "new_username": "bob",
            "new_password": "new-password",
        },
    )
    assert response.status_code == 401, response.text
    assert _written == {}


def test_changes_credentials_and_session_username() -> None:
    _reset()
    _login()
    response = _client.post(
        "/api/auth/change_credentials",
        json={
            "current_username": "alice",
            "current_password": "old-password",
            "new_username": "  bob  ",
            "new_password": "new-password",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["username"] == "bob"
    assert _written == {"username": "bob", "password": "new-password"}
    assert _client.get("/api/auth/me").json() == {"username": "bob"}

    old_login = _client.post(
        "/api/auth/login",
        json={"username": "alice", "password": "old-password"},
    )
    assert old_login.status_code == 401, old_login.text

    new_login = _client.post(
        "/api/auth/login",
        json={"username": "bob", "password": "new-password"},
    )
    assert new_login.status_code == 200, new_login.text


if __name__ == "__main__":
    test_requires_authenticated_session()
    test_rejects_wrong_current_credentials()
    test_changes_credentials_and_session_username()
    print("PASS test_auth_change_credentials")
