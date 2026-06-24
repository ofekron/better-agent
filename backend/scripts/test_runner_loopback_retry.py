import os
import sys
import tempfile
import urllib.error
import json
from pathlib import Path

import _test_home
_test_home.isolate("bc-test-loopback-")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import runner  # noqa: E402
import delegation_status_store  # noqa: E402


def test_loopback_post_prefers_spawn_token_when_disk_token_differs(monkeypatch):
    token_file = Path(os.environ["BETTER_CLAUDE_HOME"]) / "internal_token"
    token_file.write_text("disk-token", encoding="utf-8")
    runner._token_cache["token"] = None
    runner._token_cache["mtime"] = 0.0

    seen_tokens = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"success": True}).encode("utf-8")

    def fake_urlopen(req, *args, **kwargs):
        seen_tokens.append(req.headers.get("X-internal-token"))
        return FakeResponse()

    monkeypatch.setattr(runner.urllib.request, "urlopen", fake_urlopen)

    recovered = runner._post_loopback_sync(
        {},
        backend_url="http://127.0.0.1:9999",
        internal_token="spawn-token",
        url_path="/api/internal/ask-fork",
        timeout=24 * 60 * 60,
        non_json_t_key="runner.delegate_non_json",
        log_prefix="delegate POST",
        backoff_cap=60.0,
    )

    assert recovered == {"success": True}
    assert seen_tokens == ["spawn-token"]


def test_loopback_post_retries_disk_token_after_forbidden(monkeypatch):
    token_file = Path(os.environ["BETTER_CLAUDE_HOME"]) / "internal_token"
    token_file.write_text("disk-token", encoding="utf-8")
    runner._token_cache["token"] = None
    runner._token_cache["mtime"] = 0.0

    seen_tokens = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"success": True}).encode("utf-8")

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
        return FakeResponse()

    monkeypatch.setattr(runner.urllib.request, "urlopen", fake_urlopen)

    recovered = runner._post_loopback_sync(
        {},
        backend_url="http://127.0.0.1:9999",
        internal_token="spawn-token",
        url_path="/api/internal/ask-fork",
        timeout=24 * 60 * 60,
        non_json_t_key="runner.delegate_non_json",
        log_prefix="delegate POST",
        backoff_cap=60.0,
    )

    assert recovered == {"success": True}
    assert seen_tokens == ["spawn-token", "disk-token"]


def test_loopback_post_recovers_completed_delegate_result(monkeypatch):
    def fake_urlopen(*args, **kwargs):
        raise urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))

    result = {
        "success": True,
        "worker_session_id": "worker-session",
        "worker_description": "worker",
    }
    delegation_status_store.write_status("del_done", status="complete", result=result)

    monkeypatch.setattr(runner.time, "sleep", lambda seconds: (_ for _ in ()).throw(
        AssertionError("durable delegate result should avoid retry sleep")
    ))
    monkeypatch.setattr(runner.urllib.request, "urlopen", fake_urlopen)

    recovered = runner._post_loopback_sync(
        {},
        backend_url="http://127.0.0.1:9999",
        internal_token="token",
        url_path="/api/internal/ask-fork",
        timeout=24 * 60 * 60,
        non_json_t_key="runner.delegate_non_json",
        log_prefix="delegate POST",
        backoff_cap=60.0,
        recover=lambda: runner._recover_delegate_result("del_done"),
    )

    assert recovered == result
