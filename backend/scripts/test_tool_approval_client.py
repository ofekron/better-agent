from __future__ import annotations

import json
import os
import sys
import tempfile
import urllib.error
from pathlib import Path

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
    token_file = Path(os.environ["BETTER_AGENT_HOME"]) / "internal_token"
    token_file.write_text("disk-token", encoding="utf-8")
    client._token_cache["token"] = None
    client._token_cache["mtime"] = 0.0

    seen_tokens = []

    def fake_urlopen(req, *args, **kwargs):
        token = req.headers.get("X-internal-token")
        seen_tokens.append(token)
        if token == "spawn-token":
            raise urllib.error.HTTPError(
                req.full_url,
                403,
                "Forbidden",
                hdrs=None,
                fp=None,
            )
        return _FakeResponse({"approved": True})

    monkeypatch.setattr(client.urllib.request, "urlopen", fake_urlopen)

    assert client.request_tool_approval(**_approval_kwargs()) is True
    assert seen_tokens == ["spawn-token", "disk-token"]


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
