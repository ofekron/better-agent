"""Full unit coverage for auth_routes.py — login/logout/whoami/QR/refresh HTTP
routes plus their request-classification helpers.

Two layers:
- Layer A: the pure request-classification helpers (`_is_loopback_request`,
  `_is_authenticated`, `_has_forwarding_headers`, `_origin_host_port`,
  `_is_same_origin_browser_request`, `_qr_mint_allowed`, `_origin_base_url`)
  driven directly with constructed Starlette scopes, so every security branch
  is exercised with the precise peer/header it decides on.
- Layer B: the HTTP endpoints via TestClient, with the auth/qr_auth/auth_secrets
  side effects patched out — real FastAPI + SessionMiddleware wiring, no live
  credential store.
"""

from __future__ import annotations

import os
import sys
import tempfile

os.environ["BETTER_AGENT_HOME"] = tempfile.mkdtemp(prefix="auth_routes_test_")
os.environ["BETTER_AGENT_TEST_MODE"] = "1"

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import auth  # noqa: E402
import auth_routes  # noqa: E402
import auth_secrets  # noqa: E402
import qr_auth  # noqa: E402
import setup_nonce  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from starlette.middleware.sessions import SessionMiddleware  # noqa: E402
from starlette.requests import Request  # noqa: E402

USERNAME = "alice"
TOKEN = "tok-xyz"


async def _verify_true(u, p) -> bool:
    return True


async def _verify_false(u, p) -> bool:
    return False


def _make_request(
    *,
    headers: dict[str, str] | None = None,
    client: tuple[str, int] | None = ("127.0.0.1", 0),
    session: dict | None = None,
    path: str = "/api/auth/qr_grant",
    scheme: str = "http",
    server: tuple[str, int] = ("127.0.0.1", 8000),
) -> Request:
    scope = {
        "type": "http",
        "scheme": scheme,
        "path": path,
        "headers": [
            (k.lower().encode(), v.encode()) for k, v in (headers or {}).items()
        ],
        "client": client,
        "server": server,
        "session": session if session is not None else {},
    }
    return Request(scope)


def _make_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-session-secret")
    app.include_router(auth_routes.router)
    return app


def _patch_defaults(monkeypatch, **overrides) -> dict:
    """Install the benign default stubs over the auth surface, then apply any
    per-test `overrides` (callables / values keyed by "module.attr"). Returns a
    shared recorder dict."""
    rec: dict = {}

    def noop(*a, **k):
        return None

    defaults = {
        "auth.is_bootstrapped": lambda: True,
        "auth.rate_limit_check": lambda ip: True,
        "auth.rate_limit_reset": noop,
        "auth.verify_credentials": _verify_true,
        "auth.reload_credentials": noop,
        # identify_request left real: it respects the session cookie, so
        # login/logout/me round-trips are exercised honestly.
        "auth.create_token": lambda u: TOKEN,
        "auth.verify_token": lambda t: {"username": USERNAME} if t else None,
        "auth.current_username": lambda: USERNAME,
        "auth.ACCESS_MAX_AGE": 900,
        "auth_secrets.write_credentials": lambda u, p: rec.setdefault("wc", (u, p)),
        "auth_secrets.write_login_credentials": lambda u, p: rec.setdefault(
            "wlc", (u, p)
        ),
        "qr_auth.mint_grant": lambda: "GRANT",
        "qr_auth.consume_grant": lambda g: True,
        "qr_auth.issue_session": lambda sub: ("access", "refresh"),
        "qr_auth.rotate": lambda t: ("access", "refresh"),
        "qr_auth.revoke_all_sessions": noop,
        "qr_auth.GRANT_TTL": 300,
        "setup_nonce.consume": lambda n: True,
    }
    defaults.update(overrides)
    for dotted, value in defaults.items():
        module_name, attr = dotted.split(".", 1)
        module = {"auth": auth, "auth_secrets": auth_secrets,
                  "qr_auth": qr_auth, "setup_nonce": setup_nonce}[module_name]
        monkeypatch.setattr(module, attr, value)
    return rec


# --- Layer A: request-classification helpers -------------------------------


def test_is_loopback_request_branches() -> None:
    assert auth_routes._is_loopback_request(_make_request(client=("127.0.0.1", 0)))
    assert auth_routes._is_loopback_request(
        _make_request(client=("::1", 0))
    )  # IPv6 loopback
    assert auth_routes._is_loopback_request(
        _make_request(client=("localhost", 0))
    )  # ValueError → host=="localhost"
    assert not auth_routes._is_loopback_request(
        _make_request(client=("8.8.8.8", 0))
    )
    assert not auth_routes._is_loopback_request(
        _make_request(client=("testclient", 0))
    )  # non-loopback string
    # No client at all → empty host, not loopback.
    assert not auth_routes._is_loopback_request(_make_request(client=None))


def test_is_authenticated_branches(monkeypatch) -> None:
    # Session cookie wins.
    assert auth_routes._is_authenticated(
        _make_request(session={"user": {"username": USERNAME}})
    )
    # Bearer token verified.
    _patch_defaults(monkeypatch)
    req = _make_request(headers={"authorization": "Bearer abc"})
    assert auth_routes._is_authenticated(req)
    # Malformed / no bearer prefix.
    assert not auth_routes._is_authenticated(
        _make_request(headers={"authorization": "xyz"})
    )
    # No auth signal at all.
    assert not auth_routes._is_authenticated(_make_request())
    # Bearer present but token invalid → verify_token returns None.
    monkeypatch.setattr(auth, "verify_token", lambda t: None)
    assert not auth_routes._is_authenticated(
        _make_request(headers={"authorization": "Bearer bad"})
    )


def test_has_forwarding_headers() -> None:
    for header in auth_routes._FORWARD_HEADERS:
        assert auth_routes._has_forwarding_headers(_make_request(headers={header: "v"}))
    assert not auth_routes._has_forwarding_headers(_make_request())


def test_origin_host_port(monkeypatch) -> None:
    assert auth_routes._origin_host_port("https://App.Com:8443/p") == "app.com:8443"
    assert auth_routes._origin_host_port("http://app.com") == "app.com"
    # No hostname → None.
    assert auth_routes._origin_host_port("not a url") is None
    # urlparse raises ValueError → None.
    def boom(raw):
        raise ValueError

    monkeypatch.setattr(auth_routes, "urlparse", boom)
    assert auth_routes._origin_host_port("http://x") is None


def test_is_same_origin_browser_request() -> None:
    # sec-fetch-site primary signal.
    assert auth_routes._is_same_origin_browser_request(
        _make_request(headers={"sec-fetch-site": "same-origin"})
    )
    assert auth_routes._is_same_origin_browser_request(
        _make_request(headers={"sec-fetch-site": "none"})
    )
    assert not auth_routes._is_same_origin_browser_request(
        _make_request(headers={"sec-fetch-site": "cross-site"})
    )
    # No browser-origin signal at all → non-browser caller → allowed.
    assert auth_routes._is_same_origin_browser_request(_make_request())
    # Origin fallback: matches host → allowed.
    assert auth_routes._is_same_origin_browser_request(
        _make_request(headers={"host": "127.0.0.1:8000",
                               "origin": "http://127.0.0.1:8000"})
    )
    # Origin mismatch → blocked.
    assert not auth_routes._is_same_origin_browser_request(
        _make_request(headers={"host": "127.0.0.1:8000",
                               "origin": "http://localhost:3000"})
    )
    # Referer fallback when origin absent.
    assert auth_routes._is_same_origin_browser_request(
        _make_request(headers={"host": "127.0.0.1:8000",
                               "referer": "http://127.0.0.1:8000/app"})
    )
    # Referer unparseable → src None → blocked.
    assert not auth_routes._is_same_origin_browser_request(
        _make_request(headers={"host": "127.0.0.1:8000", "referer": "garbage"})
    )
    # No host header anywhere → falls through to netloc match.
    req = _make_request(headers={"origin": "http://127.0.0.1:8000"})
    assert auth_routes._is_same_origin_browser_request(req)


def test_qr_mint_allowed() -> None:
    # Authenticated always.
    assert auth_routes._qr_mint_allowed(
        authed=True, loopback=False, has_forward=False, same_origin=False
    )
    # On-machine admin at the login screen.
    assert auth_routes._qr_mint_allowed(
        authed=False, loopback=True, has_forward=False, same_origin=True
    )
    # Remote peer.
    assert not auth_routes._qr_mint_allowed(
        authed=False, loopback=False, has_forward=False, same_origin=True
    )
    # Cross-site same-machine.
    assert not auth_routes._qr_mint_allowed(
        authed=False, loopback=True, has_forward=False, same_origin=False
    )
    # Proxy collapsed the peer to loopback.
    assert not auth_routes._qr_mint_allowed(
        authed=False, loopback=True, has_forward=True, same_origin=True
    )


def test_origin_base_url() -> None:
    # Reverse-proxy headers win.
    req = _make_request(
        headers={"x-forwarded-proto": "https", "x-forwarded-host": "ex.com"}
    )
    assert auth_routes._origin_base_url(req) == "https://ex.com"
    # Host header fallback.
    req = _make_request(headers={"host": "h.local"})
    assert auth_routes._origin_base_url(req) == "http://h.local"
    # Netloc fallback (no host header).
    req = _make_request(headers={}, client=("127.0.0.1", 0))
    assert auth_routes._origin_base_url(req).startswith("http://")


# --- Layer B: HTTP endpoints -------------------------------------------------


def test_needs_setup(monkeypatch) -> None:
    _patch_defaults(monkeypatch, **{"auth.is_bootstrapped": lambda: False})
    client = TestClient(_make_app())
    assert client.get("/api/auth/needs_setup").json() == {"needs_setup": True}
    monkeypatch.setattr(auth, "is_bootstrapped", lambda: True)
    assert client.get("/api/auth/needs_setup").json() == {"needs_setup": False}


def test_setup_loopback_bootstrap(monkeypatch) -> None:
    _patch_defaults(monkeypatch, **{"auth.is_bootstrapped": lambda: False})
    client = TestClient(_make_app(), client=("127.0.0.1", 0))
    res = client.post(
        "/api/auth/setup",
        json={"username": "alice", "password": "pw"},
    )
    assert res.status_code == 200, res.text
    assert res.json()["token"] == TOKEN


def test_setup_already_configured(monkeypatch) -> None:
    _patch_defaults(monkeypatch)  # bootstrapped True by default
    client = TestClient(_make_app(), client=("127.0.0.1", 0))
    res = client.post("/api/auth/setup", json={"username": "a", "password": "p"})
    assert res.status_code == 409, res.text


def test_setup_nonloopback_nonce_paths(monkeypatch) -> None:
    rec = _patch_defaults(monkeypatch, **{"auth.is_bootstrapped": lambda: False})

    # Valid nonce → allowed.
    client = TestClient(_make_app(), client=("8.8.8.8", 0))
    res = client.post(
        "/api/auth/setup",
        json={"username": "alice", "password": "pw"},
        headers={"X-Setup-Nonce": "n"},
    )
    assert res.status_code == 200, res.text

    # Consumed / invalid nonce → 403.
    monkeypatch.setattr(setup_nonce, "consume", lambda n: False)
    res = client.post(
        "/api/auth/setup",
        json={"username": "alice", "password": "pw"},
        headers={"X-Setup-Nonce": "stale"},
    )
    assert res.status_code == 403, res.text


def test_setup_validation_and_write_failure(monkeypatch) -> None:
    _patch_defaults(monkeypatch, **{"auth.is_bootstrapped": lambda: False})
    client = TestClient(_make_app(), client=("127.0.0.1", 0))
    # Missing username/password → 400.
    res = client.post("/api/auth/setup", json={"username": "  ", "password": ""})
    assert res.status_code == 400, res.text
    # write_credentials blows up → 500.
    def boom(u, p):
        raise OSError("disk full")

    monkeypatch.setattr(auth_secrets, "write_credentials", boom)
    res = client.post("/api/auth/setup", json={"username": "a", "password": "p"})
    assert res.status_code == 500, res.text
    assert "disk full" in res.json()["detail"]


def test_login_success_and_failures(monkeypatch) -> None:
    _patch_defaults(monkeypatch)
    client = TestClient(_make_app())

    # Rate limited → 429.
    monkeypatch.setattr(auth, "rate_limit_check", lambda ip: False)
    res = client.post("/api/auth/login", json={"username": "a", "password": "p"})
    assert res.status_code == 429, res.text

    # Bad credentials → 401.
    monkeypatch.setattr(auth, "rate_limit_check", lambda ip: True)
    monkeypatch.setattr(auth, "verify_credentials", _verify_false)
    res = client.post("/api/auth/login", json={"username": "a", "password": "p"})
    assert res.status_code == 401, res.text

    # Success → token + session.
    monkeypatch.setattr(auth, "verify_credentials", _verify_true)
    res = client.post("/api/auth/login", json={"username": "a", "password": "p"})
    assert res.status_code == 200, res.text
    assert res.json()["token"] == TOKEN
    assert client.get("/api/auth/me").json() == {"username": "a"}


def test_logout_clears_session(monkeypatch) -> None:
    _patch_defaults(monkeypatch)
    client = TestClient(_make_app())
    client.post("/api/auth/login", json={"username": "a", "password": "p"})
    assert client.get("/api/auth/me").status_code == 200
    res = client.post("/api/auth/logout")
    assert res.status_code == 204
    assert client.get("/api/auth/me").status_code == 401


def test_me_unauthenticated(monkeypatch) -> None:
    _patch_defaults(monkeypatch, **{"auth.identify_request": lambda r: None})
    client = TestClient(_make_app())
    assert client.get("/api/auth/me").status_code == 401


def test_change_credentials_paths(monkeypatch) -> None:
    _patch_defaults(monkeypatch)
    client = TestClient(_make_app(), client=("127.0.0.1", 0))
    body = {
        "current_username": "a",
        "current_password": "p",
        "new_username": "b",
        "new_password": "q",
    }

    # Unauthenticated → 401.
    monkeypatch.setattr(auth, "identify_request", lambda r: None)
    res = client.post("/api/auth/change_credentials", json=body)
    assert res.status_code == 401, res.text

    monkeypatch.setattr(auth, "identify_request", lambda r: {"username": "a"})

    # Rate limited → 429.
    monkeypatch.setattr(auth, "rate_limit_check", lambda ip: False)
    res = client.post("/api/auth/change_credentials", json=body)
    assert res.status_code == 429, res.text

    # Wrong current → 401.
    monkeypatch.setattr(auth, "rate_limit_check", lambda ip: True)
    monkeypatch.setattr(auth, "verify_credentials", _verify_false)
    res = client.post("/api/auth/change_credentials", json=body)
    assert res.status_code == 401, res.text

    # Empty new creds → 400.
    monkeypatch.setattr(auth, "verify_credentials", _verify_true)
    res = client.post(
        "/api/auth/change_credentials",
        json={**body, "new_username": "  ", "new_password": ""},
    )
    assert res.status_code == 400, res.text

    # ValueError from write → 400.
    def value_err(u, p):
        raise ValueError("too short")

    monkeypatch.setattr(auth_secrets, "write_login_credentials", value_err)
    res = client.post("/api/auth/change_credentials", json=body)
    assert res.status_code == 400, res.text
    assert "too short" in res.json()["detail"]

    # Generic exception → 500.
    def boom(u, p):
        raise OSError("nope")

    monkeypatch.setattr(auth_secrets, "write_login_credentials", boom)
    res = client.post("/api/auth/change_credentials", json=body)
    assert res.status_code == 500, res.text

    # Success → 200, sessions revoked, username stamped + stripped.
    rev = []
    monkeypatch.setattr(auth_secrets, "write_login_credentials",
                        lambda u, p: rev.append((u, p)))
    monkeypatch.setattr(qr_auth, "revoke_all_sessions", lambda: rev.append("rev"))
    res = client.post(
        "/api/auth/change_credentials",
        json={**body, "new_username": "  bob  "},
    )
    assert res.status_code == 200, res.text
    assert res.json()["username"] == "bob"
    assert ("bob", "q") in rev and "rev" in rev


def test_qr_grant_paths(monkeypatch) -> None:
    _patch_defaults(monkeypatch)

    # Not configured → 409.
    monkeypatch.setattr(auth, "is_bootstrapped", lambda: False)
    client = TestClient(_make_app(), client=("127.0.0.1", 0))
    res = client.get("/api/auth/qr_grant")
    assert res.status_code == 409, res.text

    monkeypatch.setattr(auth, "is_bootstrapped", lambda: True)

    # Non-loopback, unauthenticated, no browser signal → 403.
    remote = TestClient(_make_app(), client=("8.8.8.8", 0))
    res = remote.get("/api/auth/qr_grant")
    assert res.status_code == 403, res.text

    # Loopback but proxy-forwarded → collapsed peer → 403.
    res = client.get("/api/auth/qr_grant", headers={"x-forwarded-for": "1.2.3.4"})
    assert res.status_code == 403, res.text

    # Loopback, same-machine non-browser caller → allowed.
    res = client.get("/api/auth/qr_grant")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["login_url"].endswith("/?qr=GRANT")
    assert body["expires_in"] == 300

    # Authenticated session always allowed, even from a remote peer.
    remote.post("/api/auth/login", json={"username": "a", "password": "p"})
    res = remote.get("/api/auth/qr_grant")
    assert res.status_code == 200, res.text


def test_qr_grant_cross_origin_blocked(monkeypatch) -> None:
    _patch_defaults(monkeypatch)
    client = TestClient(_make_app(), client=("127.0.0.1", 0))
    # Loopback but cross-site fetch → blocked.
    res = client.get(
        "/api/auth/qr_grant",
        headers={"sec-fetch-site": "cross-site"},
    )
    assert res.status_code == 403, res.text


def test_qr_redeem_paths(monkeypatch) -> None:
    _patch_defaults(monkeypatch)
    client = TestClient(_make_app(), client=("127.0.0.1", 0))

    # Rate limited → 429.
    monkeypatch.setattr(auth, "rate_limit_check", lambda ip: False)
    res = client.post("/api/auth/qr_redeem", json={"grant": "G"})
    assert res.status_code == 429, res.text

    monkeypatch.setattr(auth, "rate_limit_check", lambda ip: True)

    # Not configured → 409.
    monkeypatch.setattr(auth, "is_bootstrapped", lambda: False)
    res = client.post("/api/auth/qr_redeem", json={"grant": "G"})
    assert res.status_code == 409, res.text

    monkeypatch.setattr(auth, "is_bootstrapped", lambda: True)

    # Invalid / used grant → 401.
    monkeypatch.setattr(qr_auth, "consume_grant", lambda g: False)
    res = client.post("/api/auth/qr_redeem", json={"grant": "G"})
    assert res.status_code == 401, res.text

    # Grant valid but no configured username → 409.
    monkeypatch.setattr(qr_auth, "consume_grant", lambda g: True)
    monkeypatch.setattr(auth, "current_username", lambda: None)
    res = client.post("/api/auth/qr_redeem", json={"grant": "G"})
    assert res.status_code == 409, res.text

    # Success.
    monkeypatch.setattr(auth, "current_username", lambda: USERNAME)
    res = client.post("/api/auth/qr_redeem", json={"grant": "G"})
    assert res.status_code == 200, res.text
    assert res.json() == {
        "access_token": "access",
        "refresh_token": "refresh",
        "expires_in": 900,
    }


def test_refresh_paths(monkeypatch) -> None:
    _patch_defaults(monkeypatch)
    client = TestClient(_make_app(), client=("127.0.0.1", 0))

    # Rate limited → 429.
    monkeypatch.setattr(auth, "rate_limit_check", lambda ip: False)
    res = client.post("/api/auth/refresh", json={"refresh_token": "r"})
    assert res.status_code == 429, res.text

    # Invalid / expired refresh → 401.
    monkeypatch.setattr(auth, "rate_limit_check", lambda ip: True)
    monkeypatch.setattr(qr_auth, "rotate", lambda t: None)
    res = client.post("/api/auth/refresh", json={"refresh_token": "r"})
    assert res.status_code == 401, res.text

    # Success.
    monkeypatch.setattr(qr_auth, "rotate", lambda t: ("a", "b"))
    res = client.post("/api/auth/refresh", json={"refresh_token": "r"})
    assert res.status_code == 200, res.text
    assert res.json() == {
        "access_token": "a",
        "refresh_token": "b",
        "expires_in": 900,
    }


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
