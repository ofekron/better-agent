"""Gate checks for the QR-grant minting endpoint: an unauthenticated caller
may mint a one-time login QR ONLY from a loopback peer, for a same-origin /
non-browser request, and only when no reverse-proxy forwarding header is
present. This closes the privilege downgrade where any same-machine web
content (a localhost:3000 dev server / local XSS) could mint+redeem a
full-access credential off the shared loopback peer, and the same-host-proxy
loopback-spoof variant. Runnable directly or via pytest.
"""

import os
import tempfile

os.environ["BETTER_AGENT_HOME"] = tempfile.mkdtemp(prefix="qr_routes_test_")

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

import auth
import auth_routes

# Bootstrap auth in-process without touching the keychain: these are plain
# module globals.
auth._BOOTSTRAPPED = True
auth._USERNAME = "alice"
auth.SESSION_SECRET = "0" * 64

# The peer-is-loopback question is trivial ipaddress logic exercised
# elsewhere; pin it True here so each test isolates the same-origin /
# forwarding dimensions of the gate.
auth_routes._is_loopback_request = lambda request: True

_app = FastAPI()
_app.add_middleware(SessionMiddleware, secret_key="test-session-secret")
_app.include_router(auth_routes.router)
_client = TestClient(_app)

_GRANT = "/api/auth/qr_grant"


def _grant(headers):
    return _client.get(_GRANT, headers={"host": "localhost:8000", **headers})


def test_same_origin_loopback_can_mint():
    # The served login screen: same-origin fetch from the backend itself.
    r = _grant({"sec-fetch-site": "same-origin"})
    assert r.status_code == 200, r.text
    assert "login_url" in r.json()


def test_non_browser_loopback_can_mint():
    # curl / desktop app on the box: no browser-origin signal at all.
    r = _grant({})
    assert r.status_code == 200, r.text


def test_cross_site_loopback_is_rejected():
    # THE core attack: a localhost:3000 dev server / local XSS doing a
    # cross-site fetch to the :8000 backend off the shared loopback peer.
    r = _grant({"sec-fetch-site": "cross-site"})
    assert r.status_code == 403, r.text


def test_mismatched_origin_without_secfetch_is_rejected():
    # Older browsers omit Sec-Fetch-Site → fall back to Origin-vs-Host.
    r = _grant({"origin": "http://localhost:3000"})
    assert r.status_code == 403, r.text


def test_matching_origin_without_secfetch_can_mint():
    r = _grant({"origin": "http://localhost:8000"})
    assert r.status_code == 200, r.text


def test_forwarding_header_rejects_even_same_origin():
    # Same-host reverse proxy collapses the peer to loopback; the presence of
    # any X-Forwarded-* means the loopback signal can't be trusted for an
    # unauthenticated mint.
    r = _grant({"sec-fetch-site": "same-origin", "x-forwarded-for": "203.0.113.7"})
    assert r.status_code == 403, r.text


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all qr_auth route-gate checks passed")
