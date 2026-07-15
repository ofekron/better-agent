from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import loopback_http  # noqa: E402
import runtime_endpoints  # noqa: E402


def test_loopback_request_retries_connection_refused_then_succeeds(monkeypatch):
    attempts = []

    def fake_request_internal(method, url_path, body, *, internal_token, timeout):
        attempts.append(1)
        if len(attempts) < 3:
            raise ConnectionRefusedError(61, "Connection refused")
        return b'{"ok": true}'

    monkeypatch.setattr(loopback_http, "request_internal", fake_request_internal)

    sleeps = []
    raw = loopback_http.loopback_request(
        "POST", "/x", b"{}", internal_token="t", timeout=1.0, sleep=sleeps.append,
    )

    assert raw == b'{"ok": true}'
    assert len(attempts) == 3
    assert len(sleeps) == 2


def test_loopback_request_retries_missing_descriptor(monkeypatch):
    attempts = []

    def fake_request_internal(method, url_path, body, *, internal_token, timeout):
        attempts.append(1)
        if len(attempts) < 2:
            raise runtime_endpoints.RuntimeEndpointError("no descriptor")
        return b"{}"

    monkeypatch.setattr(loopback_http, "request_internal", fake_request_internal)

    raw = loopback_http.loopback_request(
        "POST", "/x", b"{}", internal_token="t", timeout=1.0, sleep=lambda s: None,
    )
    assert raw == b"{}"
    assert len(attempts) == 2


def test_loopback_request_does_not_retry_status_error(monkeypatch):
    attempts = []

    def fake_request_internal(method, url_path, body, *, internal_token, timeout):
        attempts.append(1)
        raise loopback_http.LoopbackHTTPStatusError(400, b"")

    monkeypatch.setattr(loopback_http, "request_internal", fake_request_internal)

    def _fail_on_sleep(seconds: float) -> None:
        raise AssertionError("a status error must not trigger a retry sleep")

    try:
        loopback_http.loopback_request(
            "POST", "/x", b"{}", internal_token="t", timeout=1.0, sleep=_fail_on_sleep,
        )
    except loopback_http.LoopbackHTTPStatusError as exc:
        assert exc.code == 400
    else:
        raise AssertionError("expected LoopbackHTTPStatusError to propagate")
    assert len(attempts) == 1


def test_loopback_request_raises_after_exhausting_retries(monkeypatch):
    attempts = []

    def fake_request_internal(method, url_path, body, *, internal_token, timeout):
        attempts.append(1)
        raise ConnectionRefusedError(61, "Connection refused")

    monkeypatch.setattr(loopback_http, "request_internal", fake_request_internal)

    try:
        loopback_http.loopback_request(
            "POST", "/x", b"{}", internal_token="t", timeout=1.0, sleep=lambda s: None,
        )
    except ConnectionRefusedError:
        pass
    else:
        raise AssertionError("expected the connection error after exhausting retries")
    assert len(attempts) == len(loopback_http._RETRY_BACKOFF)
