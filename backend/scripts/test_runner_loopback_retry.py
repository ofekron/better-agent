import sys
import urllib.error
import json
import io
from pathlib import Path

import pytest

import _test_home
_test_home.isolate("bc-test-loopback-")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import runner  # noqa: E402
import delegation_status_store  # noqa: E402
from paths import bc_home  # noqa: E402
import runner_operation_host  # noqa: E402


def test_loopback_post_prefers_spawn_token_when_disk_token_differs(monkeypatch):
    token_file = bc_home() / "internal_token"
    token_file.write_text("disk-token", encoding="utf-8")

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
    spawn_token = "A" * 43
    disk_token = "B" * 43
    token_file = bc_home() / "internal_token"
    token_file.write_text(disk_token, encoding="utf-8")
    token_file.chmod(0o600)
    runner_operation_host._install_internal_token_authority(spawn_token)

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
        if token == spawn_token:
            raise urllib.error.HTTPError(
                req.full_url,
                403,
                "Forbidden",
                hdrs=None,
                fp=None,
            )
        return FakeResponse()

    monkeypatch.setattr(runner.urllib.request, "urlopen", fake_urlopen)

    try:
        recovered = runner._post_loopback_sync(
            {},
            backend_url="http://127.0.0.1:9999",
            internal_token=spawn_token,
            url_path="/api/internal/ask-fork",
            timeout=24 * 60 * 60,
            non_json_t_key="runner.delegate_non_json",
            log_prefix="delegate POST",
            backoff_cap=60.0,
        )
    finally:
        runner_operation_host.stop_active_host()

    assert recovered == {"success": True}
    assert seen_tokens == [spawn_token, disk_token]


def test_loopback_post_bounds_refresh_and_redacts_tokens(monkeypatch):
    spawn_token = "A" * 43
    disk_token = "B" * 43
    token_file = bc_home() / "internal_token"
    token_file.write_text(disk_token, encoding="utf-8")
    token_file.chmod(0o600)
    runner_operation_host._install_internal_token_authority(spawn_token)
    seen_tokens = []

    def fake_urlopen(req, *args, **kwargs):
        token = req.headers.get("X-internal-token")
        seen_tokens.append(token)
        raise urllib.error.HTTPError(
            req.full_url,
            403,
            "Forbidden",
            hdrs=None,
            fp=io.BytesIO(f'{{"detail":"rejected {token}"}}'.encode()),
        )

    monkeypatch.setattr(runner.urllib.request, "urlopen", fake_urlopen)
    try:
        with pytest.raises(RuntimeError) as exc:
            runner._post_loopback_sync(
                {},
                backend_url="http://127.0.0.1:9999",
                internal_token=spawn_token,
                url_path="/api/internal/ask-fork",
                timeout=24 * 60 * 60,
                non_json_t_key="runner.delegate_non_json",
                log_prefix="delegate POST",
                backoff_cap=60.0,
            )
    finally:
        runner_operation_host.stop_active_host()

    detail = str(exc.value)
    assert seen_tokens == [spawn_token, disk_token]
    assert spawn_token not in detail
    assert disk_token not in detail


def test_loopback_post_refreshes_multiple_generations_and_redacts_history(
    monkeypatch,
):
    tokens = ("A" * 43, "B" * 43, "C" * 43)
    token_file = bc_home() / "internal_token"
    token_file.write_text(tokens[1], encoding="utf-8")
    token_file.chmod(0o600)
    runner_operation_host._install_internal_token_authority(tokens[0])
    seen_tokens = []

    def fake_urlopen(req, *args, **kwargs):
        token = req.headers.get("X-internal-token")
        seen_tokens.append(token)
        if token == tokens[1]:
            token_file.write_text(tokens[2], encoding="utf-8")
            token_file.chmod(0o600)
        raise urllib.error.HTTPError(
            req.full_url,
            403,
            "Forbidden",
            hdrs=None,
            fp=io.BytesIO(
                f'{{"detail":"rejected {" ".join(tokens)}"}}'.encode(),
            ),
        )

    monkeypatch.setattr(runner.urllib.request, "urlopen", fake_urlopen)
    try:
        with pytest.raises(RuntimeError) as exc:
            runner._post_loopback_sync(
                {},
                backend_url="http://127.0.0.1:9999",
                internal_token=tokens[0],
                url_path="/api/internal/ask-fork",
                timeout=24 * 60 * 60,
                non_json_t_key="runner.delegate_non_json",
                log_prefix="delegate POST",
                backoff_cap=60.0,
            )
    finally:
        runner_operation_host.stop_active_host()

    detail = str(exc.value)
    assert seen_tokens == list(tokens)
    assert detail.count("[redacted]") == len(tokens)
    assert not any(token in detail for token in tokens)


def test_loopback_post_surfaces_http_error_detail(monkeypatch):
    def fake_urlopen(req, *args, **kwargs):
        raise urllib.error.HTTPError(
            req.full_url,
            409,
            "Conflict",
            hdrs=None,
            fp=io.BytesIO(b'{"detail":"no idle worker in target_worker_pool"}'),
        )

    monkeypatch.setattr(runner.urllib.request, "urlopen", fake_urlopen)

    try:
        runner._post_loopback_sync(
            {},
            backend_url="http://127.0.0.1:9999",
            internal_token="token",
            url_path="/api/internal/ask",
            timeout=24 * 60 * 60,
            non_json_t_key="runner.delegate_non_json",
            log_prefix="ask POST",
            backoff_cap=60.0,
        )
    except RuntimeError as exc:
        assert str(exc) == "HTTP 409: no idle worker in target_worker_pool"
    else:
        raise AssertionError("expected HTTP error detail to be surfaced")


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
