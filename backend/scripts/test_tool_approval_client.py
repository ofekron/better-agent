from __future__ import annotations

import http.client
import json
import os
import sys
import tempfile
import urllib.error
from pathlib import Path

import pytest

# Isolate state before importing backend modules.
_TMP_HOME = tempfile.mkdtemp(prefix="tool_approval_client_home_")
os.environ["BETTER_AGENT_HOME"] = _TMP_HOME
os.environ.setdefault("BETTER_CLAUDE_HOME", _TMP_HOME)

_BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BACKEND))

import tool_approval_client as client  # noqa: E402


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


class _RawResponse:
    """Response carrying arbitrary (possibly invalid) bytes, for JSON-decode
    and malformed-payload paths."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self) -> bytes:
        return self._body


def _approval_kwargs() -> dict:
    return {
        "backend_url": "http://127.0.0.1:9999",
        "internal_token": "spawn-token",
        "app_session_id": "session-1",
        "run_id": "run-1",
        "provider_kind": "openai",
        "tool_name": "Bash",
        "summary": {"tool": "Bash", "input": {"command": "echo hi"}},
    }


def test_request_tool_approval_retries_transient_backend_restart(monkeypatch):
    attempts = []

    def fake_urlopen(req, *args, **kwargs):
        attempts.append(req.headers.get("X-internal-token"))
        if len(attempts) == 1:
            raise urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))
        return _FakeResponse({"approved": True})

    sleeps = []
    monkeypatch.setattr(client.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(client.time, "sleep", lambda seconds: sleeps.append(seconds))

    assert client.request_tool_approval(**_approval_kwargs()) is True
    assert attempts == ["spawn-token", "spawn-token"]
    assert sleeps and sleeps[0] >= 0.5


def test_request_tool_approval_retries_disk_token_after_forbidden(monkeypatch):
    import runner_operation_host

    spawn_token = "A" * 43
    disk_token = "B" * 43
    token_file = Path(os.environ["BETTER_AGENT_HOME"]) / "internal_token"
    token_file.write_text(disk_token, encoding="utf-8")
    token_file.chmod(0o600)
    runner_operation_host._install_internal_token_authority(spawn_token)

    seen_tokens = []

    def fake_urlopen(req, *args, **kwargs):
        token = req.headers.get("X-internal-token")
        seen_tokens.append(token)
        if token == spawn_token:
            raise urllib.error.HTTPError(
                req.full_url,
                403,
                "Forbidden",
                hdrs=None,
                fp=None,
            )
        return _FakeResponse({"approved": True})

    monkeypatch.setattr(client.urllib.request, "urlopen", fake_urlopen)

    kwargs = _approval_kwargs()
    kwargs["internal_token"] = spawn_token
    try:
        assert client.request_tool_approval(**kwargs) is True
    finally:
        runner_operation_host.stop_active_host()
    assert seen_tokens == [spawn_token, disk_token]


def test_request_tool_approval_retries_http_5xx(monkeypatch):
    attempts = []

    def fake_urlopen(req, *args, **kwargs):
        attempts.append(req.headers.get("X-internal-token"))
        if len(attempts) == 1:
            raise urllib.error.HTTPError(
                req.full_url,
                503,
                "Service Unavailable",
                hdrs=None,
                fp=None,
            )
        return _FakeResponse({"approved": True})

    sleeps = []
    monkeypatch.setattr(client.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(client.time, "sleep", lambda seconds: sleeps.append(seconds))

    assert client.request_tool_approval(**_approval_kwargs()) is True
    assert attempts == ["spawn-token", "spawn-token"]
    assert sleeps and sleeps[0] >= 0.5


def test_request_tool_approval_fails_closed_after_transient_deadline(monkeypatch):
    times = iter([0.0, 1_000.0])
    monkeypatch.setattr(client.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(
        client.urllib.request,
        "urlopen",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))
        ),
    )
    monkeypatch.setattr(
        client.time,
        "sleep",
        lambda seconds: (_ for _ in ()).throw(
            AssertionError("deadline-expired approval should not sleep")
        ),
    )

    assert client.request_tool_approval(**_approval_kwargs()) is False


def test_describe_tool_call_preserves_strings_and_stringifies_non_strings() -> None:
    out = client.describe_tool_call(
        "Bash",
        {"command": "echo hi", "count": 3, "obj": {"x": 1}},
    )
    assert out["tool"] == "Bash"
    # String args pass through verbatim; non-strings are JSON-encoded.
    assert out["input"]["command"] == "echo hi"
    assert out["input"]["count"] == "3"
    assert out["input"]["obj"] == json.dumps({"x": 1})


def test_describe_tool_call_truncates_long_values_to_cap() -> None:
    out = client.describe_tool_call("Write", {"content": "a" * 600})
    assert len(out["input"]["content"]) == client._SUMMARY_VALUE_CAP


def test_describe_tool_call_non_dict_input_degrades_to_empty_args() -> None:
    for bad in (None, [], "raw-cmd", 42):
        out = client.describe_tool_call(bad, bad)
        assert out == {"tool": str(bad), "input": {}}


def test_describe_tool_call_propagates_when_value_str_fails() -> None:
    # json.dumps(default=str) delegates to str(); if str itself raises, the
    # per-value fallback re-invokes the same failing str() and propagates
    # rather than silently dropping the field.
    class _Unstringable:
        def __str__(self) -> str:
            raise ValueError("nope")

    with pytest.raises(ValueError):
        client.describe_tool_call("X", {"k": _Unstringable()})


def test_request_tool_approval_denies_when_required_field_missing() -> None:
    base = _approval_kwargs()
    # Each conjunct of the required-field guard is exercised independently.
    for field in ("backend_url", "internal_token", "app_session_id"):
        kwargs = dict(base)
        kwargs[field] = ""
        assert client.request_tool_approval(**kwargs) is False


def test_request_tool_approval_denies_on_non_transient_http_error(monkeypatch) -> None:
    def fake_urlopen(req, *args, **kwargs):
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", hdrs=None, fp=None)

    monkeypatch.setattr(client.urllib.request, "urlopen", fake_urlopen)
    assert client.request_tool_approval(**_approval_kwargs()) is False


def test_request_tool_approval_denies_on_invalid_json_response(monkeypatch) -> None:
    monkeypatch.setattr(
        client.urllib.request,
        "urlopen",
        lambda *args, **kwargs: _RawResponse(b"not-json"),
    )
    assert client.request_tool_approval(**_approval_kwargs()) is False


def test_request_tool_approval_denies_on_non_transient_generic_error(monkeypatch) -> None:
    def fake_urlopen(req, *args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(client.urllib.request, "urlopen", fake_urlopen)
    assert client.request_tool_approval(**_approval_kwargs()) is False


def test_request_tool_approval_retries_urlerror_with_non_os_reason(monkeypatch) -> None:
    # A URLError whose reason is not a connection/timeout/OS error still
    # counts as transient (transport-level), so it retries to success.
    attempts = []

    def fake_urlopen(req, *args, **kwargs):
        attempts.append(req.headers.get("X-internal-token"))
        if len(attempts) == 1:
            raise urllib.error.URLError("dns resolution failed")
        return _FakeResponse({"approved": True})

    sleeps = []
    monkeypatch.setattr(client.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(client.time, "sleep", lambda seconds: sleeps.append(seconds))

    assert client.request_tool_approval(**_approval_kwargs()) is True
    assert attempts == ["spawn-token", "spawn-token"]
    assert sleeps and sleeps[0] >= 0.5


def test_request_tool_approval_retries_http_exception(monkeypatch) -> None:
    # A bare http.client exception (not HTTPError/URLError) is transient.
    attempts = []

    def fake_urlopen(req, *args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise http.client.BadStatusLine("partial")
        return _FakeResponse({"approved": True})

    monkeypatch.setattr(client.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(client.time, "sleep", lambda seconds: None)

    assert client.request_tool_approval(**_approval_kwargs()) is True
    assert len(attempts) == 2
