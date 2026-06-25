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
CURRENT_ACTOR = "auth-test-principal-a"
NEXT_ACTOR = "auth-test-principal-b"
CURRENT_PROOF = "auth-test-proof-a"
NEXT_PROOF = "auth-test-proof-b"
INVALID_PROOF = "auth-test-proof-invalid"


def _reset() -> None:
    auth._BOOTSTRAPPED = True
    auth._USERNAME = CURRENT_ACTOR
    auth._PASSWORD_HASH = auth_secrets.make_password_hash(CURRENT_PROOF)
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
        json={"username": CURRENT_ACTOR, "password": CURRENT_PROOF},
    )
    assert response.status_code == 200, response.text


def test_requires_authenticated_session() -> None:
    _reset()
    client = TestClient(_app)
    response = client.post(
        "/api/auth/change_credentials",
        json={
            "current_username": CURRENT_ACTOR,
            "current_password": CURRENT_PROOF,
            "new_username": NEXT_ACTOR,
            "new_password": NEXT_PROOF,
        },
    )
    assert response.status_code == 401, response.text


def test_rejects_wrong_current_credentials() -> None:
    _reset()
    _login()
    response = _client.post(
        "/api/auth/change_credentials",
        json={
            "current_username": CURRENT_ACTOR,
            "current_password": INVALID_PROOF,
            "new_username": NEXT_ACTOR,
            "new_password": NEXT_PROOF,
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
            "current_username": CURRENT_ACTOR,
            "current_password": CURRENT_PROOF,
            "new_username": f"  {NEXT_ACTOR}  ",
            "new_password": NEXT_PROOF,
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["username"] == NEXT_ACTOR
    assert _written == {"username": NEXT_ACTOR, "password": NEXT_PROOF}
    assert _client.get("/api/auth/me").json() == {"username": NEXT_ACTOR}

    old_login = _client.post(
        "/api/auth/login",
        json={"username": CURRENT_ACTOR, "password": CURRENT_PROOF},
    )
    assert old_login.status_code == 401, old_login.text

    new_login = _client.post(
        "/api/auth/login",
        json={"username": NEXT_ACTOR, "password": NEXT_PROOF},
    )
    assert new_login.status_code == 200, new_login.text


if __name__ == "__main__":
    test_requires_authenticated_session()
    test_rejects_wrong_current_credentials()
    test_changes_credentials_and_session_username()
    print("PASS test_auth_change_credentials")
