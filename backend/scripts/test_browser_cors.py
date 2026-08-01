from __future__ import annotations

import asyncio
import os
import sys

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-browser-cors-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest  # noqa: E402
from starlette.datastructures import Headers, MutableHeaders  # noqa: E402

import browser_cors  # noqa: E402


def _make_middleware(app=None, **overrides):
    """Build a BrowserTrustCORSMiddleware with a recording ASGI app and
    sane CORS defaults; callers override per-test."""
    kwargs = dict(
        app=app if app is not None else lambda *a, **kw: None,
        allow_origins=["https://trusted.example"],
        allow_methods=["GET", "POST"],
        allow_headers=["x-custom"],
        allow_credentials=True,
        allow_private_network=True,
    )
    kwargs.update(overrides)
    return browser_cors.BrowserTrustCORSMiddleware(**kwargs)


# --------------------------------------------------------------------------- #
# _request_origin_allowed
# --------------------------------------------------------------------------- #

def test_origin_allowed_missing_origin():
    mw = _make_middleware()
    assert mw._request_origin_allowed(Headers({"host": "h.example"})) is False


def test_origin_allowed_missing_host():
    mw = _make_middleware()
    assert mw._request_origin_allowed(Headers({"origin": "https://x.example"})) is False


def test_origin_allowed_via_is_allowed_origin():
    mw = _make_middleware()
    headers = Headers({"origin": "https://trusted.example", "host": "h"})
    assert mw._request_origin_allowed(headers) is True


def test_origin_allowed_delegates_to_browser_trust_true(monkeypatch):
    calls = {}

    def fake(origin, host):
        calls.update(origin=origin, host=host)
        return True

    monkeypatch.setattr(browser_cors.browser_trust, "is_cors_origin_allowed", fake)
    mw = _make_middleware()
    headers = Headers({"origin": "https://other.example", "host": "host.example"})
    assert mw._request_origin_allowed(headers) is True
    assert calls == {"origin": "https://other.example", "host": "host.example"}


def test_origin_allowed_delegates_to_browser_trust_false(monkeypatch):
    monkeypatch.setattr(
        browser_cors.browser_trust, "is_cors_origin_allowed", lambda *a: False
    )
    mw = _make_middleware()
    headers = Headers({"origin": "https://other.example", "host": "host.example"})
    assert mw._request_origin_allowed(headers) is False


# --------------------------------------------------------------------------- #
# preflight_response
# --------------------------------------------------------------------------- #

def _preflight_headers(**extra):
    base = {
        "origin": "https://trusted.example",
        "host": "app.example",
        "access-control-request-method": "GET",
    }
    base.update(extra)
    return Headers(base)


def test_preflight_success_explicit_origin():
    mw = _make_middleware(allow_origins=["https://trusted.example"], allow_credentials=True)
    resp = mw.preflight_response(_preflight_headers())
    assert resp.status_code == 200
    assert resp.body == b"OK"
    assert resp.headers["Access-Control-Allow-Origin"] == "https://trusted.example"


def test_preflight_all_origins_no_explicit_origin_header():
    # allow_all_origins -> _request_origin_allowed True via is_allowed_origin,
    # but preflight_explicit_allow_origin is False, so the requested origin is
    # NOT echoed back; the header keeps its default "*".
    mw = _make_middleware(allow_origins=["*"], allow_credentials=False)
    resp = mw.preflight_response(_preflight_headers())
    assert resp.status_code == 200
    assert resp.headers["Access-Control-Allow-Origin"] == "*"


def test_preflight_origin_disallowed(monkeypatch):
    monkeypatch.setattr(
        browser_cors.browser_trust, "is_cors_origin_allowed", lambda *a: False
    )
    mw = _make_middleware(allow_origins=["https://other.example"])
    resp = mw.preflight_response(_preflight_headers(origin="https://evil.example"))
    assert resp.status_code == 400
    assert b"origin" in resp.body


def test_preflight_method_disallowed():
    mw = _make_middleware()
    resp = mw.preflight_response(_preflight_headers(**{"access-control-request-method": "DELETE"}))
    assert resp.status_code == 400
    assert b"method" in resp.body


def test_preflight_all_headers_echoed():
    mw = _make_middleware(allow_headers=["*"])
    resp = mw.preflight_response(
        _preflight_headers(**{"access-control-request-headers": "X-One, X-Two"})
    )
    assert resp.status_code == 200
    assert resp.headers["Access-Control-Allow-Headers"] == "X-One, X-Two"


def test_preflight_specific_header_allowed():
    mw = _make_middleware(allow_headers=["x-custom"])
    resp = mw.preflight_response(
        _preflight_headers(**{"access-control-request-headers": "X-Custom"})
    )
    assert resp.status_code == 200


def test_preflight_specific_header_disallowed():
    mw = _make_middleware(allow_headers=["x-custom"])
    resp = mw.preflight_response(
        _preflight_headers(**{"access-control-request-headers": "X-Missing"})
    )
    assert resp.status_code == 400
    assert b"headers" in resp.body


def test_preflight_private_network_allowed():
    mw = _make_middleware(allow_private_network=True)
    resp = mw.preflight_response(
        _preflight_headers(**{"access-control-request-private-network": "true"})
    )
    assert resp.status_code == 200
    assert resp.headers["Access-Control-Allow-Private-Network"] == "true"


def test_preflight_private_network_disallowed():
    mw = _make_middleware(allow_private_network=False)
    resp = mw.preflight_response(
        _preflight_headers(**{"access-control-request-private-network": "true"})
    )
    assert resp.status_code == 400
    assert b"private-network" in resp.body


# --------------------------------------------------------------------------- #
# simple_response
# --------------------------------------------------------------------------- #

def test_simple_response_wraps_send_and_calls_app():
    captured = {}

    async def app(scope, receive, send):
        captured["scope"] = scope
        captured["receive"] = receive
        captured["send"] = send

    async def receive():
        return {"type": "http.request"}

    mw = _make_middleware(app=app)
    request_headers = Headers({"origin": "https://trusted.example"})
    asyncio.run(mw.simple_response({"type": "http"}, receive, lambda m: None, request_headers))
    assert captured["scope"] == {"type": "http"}
    assert captured["receive"] is receive
    # send passed to app must be the functools.partial bound to self.send.
    assert captured["send"].func == mw.send
    assert captured["send"].keywords["request_headers"] is request_headers


# --------------------------------------------------------------------------- #
# send
# --------------------------------------------------------------------------- #

async def _drive_send(mw, message, request_headers):
    """Invoke mw.send with a real async ASGI send; return emitted messages."""
    sent = []

    async def send(msg):
        sent.append(msg)

    await mw.send(message, send, request_headers)
    return sent


def test_send_passthrough_non_start_message():
    mw = _make_middleware()
    message = {"type": "http.response.body", "body": b"x"}
    sent = asyncio.run(_drive_send(mw, message, Headers({})))
    assert sent == [message]


def _start_message():
    return {"type": "http.response.start", "status": 200, "headers": []}


def _allow_origin(msg):
    return MutableHeaders(scope=msg).get("access-control-allow-origin")


def test_send_allow_all_origins_with_cookie_sets_origin():
    # allow_all_origins + cookie: explicit origin overrides the "*" default.
    mw = _make_middleware(allow_origins=["*"], allow_credentials=False)
    headers = Headers({"Origin": "https://app.example", "host": "app.example", "cookie": "session=1"})
    sent = asyncio.run(_drive_send(mw, _start_message(), headers))
    assert _allow_origin(sent[0]) == "https://app.example"


def test_send_allow_all_origins_without_cookie_keeps_wildcard():
    # allow_all_origins, no cookie: neither branch runs, header stays "*".
    mw = _make_middleware(allow_origins=["*"], allow_credentials=False)
    headers = Headers({"Origin": "https://app.example", "host": "app.example"})
    sent = asyncio.run(_drive_send(mw, _start_message(), headers))
    assert _allow_origin(sent[0]) == "*"


def test_send_specific_origin_allowed_sets_origin():
    mw = _make_middleware(allow_origins=["https://app.example"], allow_credentials=True)
    headers = Headers({"Origin": "https://app.example", "host": "app.example"})
    sent = asyncio.run(_drive_send(mw, _start_message(), headers))
    assert _allow_origin(sent[0]) == "https://app.example"


def test_send_specific_origin_disallowed_no_origin(monkeypatch):
    monkeypatch.setattr(
        browser_cors.browser_trust, "is_cors_origin_allowed", lambda *a: False
    )
    mw = _make_middleware(allow_origins=["https://other.example"], allow_credentials=True)
    headers = Headers({"Origin": "https://app.example", "host": "app.example"})
    sent = asyncio.run(_drive_send(mw, _start_message(), headers))
    assert _allow_origin(sent[0]) is None
