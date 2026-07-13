from __future__ import annotations

import io
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import loopback_http  # noqa: E402


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def read(self) -> bytes:
        return self._body


def test_loopback_urlopen_retries_connection_refused_then_succeeds(monkeypatch):
    attempts = []

    def fake_urlopen(req, timeout=None):
        attempts.append(1)
        if len(attempts) < 3:
            raise urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))
        return _FakeResponse(b'{"ok": true}')

    monkeypatch.setattr(loopback_http.urllib.request, "urlopen", fake_urlopen)

    sleeps = []
    req = urllib.request.Request("http://127.0.0.1:1/x", method="GET")
    raw = loopback_http.loopback_urlopen(req, timeout=1.0, sleep=sleeps.append)

    assert raw == b'{"ok": true}'
    assert len(attempts) == 3
    assert len(sleeps) == 2


def test_loopback_urlopen_does_not_retry_http_error(monkeypatch):
    attempts = []

    def fake_urlopen(req, timeout=None):
        attempts.append(1)
        raise urllib.error.HTTPError(
            req.full_url, 400, "Bad Request", hdrs=None, fp=io.BytesIO(b""),
        )

    monkeypatch.setattr(loopback_http.urllib.request, "urlopen", fake_urlopen)

    def _fail_on_sleep(seconds: float) -> None:
        raise AssertionError("HTTPError must not trigger a retry sleep")

    req = urllib.request.Request("http://127.0.0.1:1/x", method="GET")
    try:
        loopback_http.loopback_urlopen(req, timeout=1.0, sleep=_fail_on_sleep)
    except urllib.error.HTTPError as exc:
        assert exc.code == 400
    else:
        raise AssertionError("expected HTTPError to propagate")
    assert len(attempts) == 1


def test_loopback_urlopen_raises_after_exhausting_retries(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))

    monkeypatch.setattr(loopback_http.urllib.request, "urlopen", fake_urlopen)

    req = urllib.request.Request("http://127.0.0.1:1/x", method="GET")
    try:
        loopback_http.loopback_urlopen(req, timeout=1.0, sleep=lambda seconds: None)
    except urllib.error.URLError:
        pass
    else:
        raise AssertionError("expected URLError after exhausting retries")
