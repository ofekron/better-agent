from __future__ import annotations

import io
import json
from pathlib import Path
import threading
import urllib.error

import pytest

import runner_better_agent as runner
import runner_operation_host as authority_owner

SPAWN_TOKEN = "A" * 43
ROTATED_TOKEN = "B" * 43
BACKEND_URL = "http://127.0.0.1:9999"


class JsonResponse:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self._payload).encode("utf-8")


class Response(JsonResponse):
    def __init__(self):
        super().__init__({"success": True})


class RawResponse(JsonResponse):
    def read(self):
        return self._payload


def forbidden(req, detail: str = "invalid internal token"):
    return urllib.error.HTTPError(
        req.full_url,
        403,
        "Forbidden",
        hdrs=None,
        fp=io.BytesIO(json.dumps({"detail": detail}).encode("utf-8")),
    )


def call_loopback():
    return runner._post_loopback_sync(
        {},
        backend_url=BACKEND_URL,
        internal_token=SPAWN_TOKEN,
        url_path="/api/internal/ask",
        timeout_s=30.0,
    )


def write_token(root: Path, token: str) -> Path:
    token_file = root / "internal_token"
    token_file.unlink(missing_ok=True)
    token_file.write_text(token, encoding="utf-8")
    token_file.chmod(0o600)
    return token_file


@pytest.fixture
def token_authority(monkeypatch, tmp_path):
    monkeypatch.setenv("BETTER_AGENT_HOME", str(tmp_path))
    monkeypatch.setenv("BETTER_CLAUDE_HOME", str(tmp_path))
    authority_owner._install_internal_token_authority(SPAWN_TOKEN)
    try:
        yield tmp_path
    finally:
        authority_owner.stop_active_host()


def test_forbidden_retry_is_bounded_and_secret_free(
    monkeypatch, caplog, capsys, token_authority,
):
    write_token(token_authority, ROTATED_TOKEN)
    seen_tokens = []

    def reject(req, *args, **kwargs):
        seen_tokens.append(req.headers.get("X-internal-token"))
        detail = (
            "spawn rejected"
            if len(seen_tokens) == 1
            else f"rotated rejected {ROTATED_TOKEN}"
        )
        raise forbidden(req, detail)

    monkeypatch.setattr(runner.urllib.request, "urlopen", reject)
    with pytest.raises(
        RuntimeError,
        match=r"HTTP 403: rotated rejected \[redacted\]",
    ) as exc:
        call_loopback()

    captured = capsys.readouterr()
    emitted = caplog.text + captured.out + captured.err + str(exc.value)
    assert seen_tokens == [SPAWN_TOKEN, ROTATED_TOKEN]
    assert SPAWN_TOKEN not in emitted
    assert ROTATED_TOKEN not in emitted


def test_non_json_response_redacts_rotated_token(monkeypatch, token_authority):
    write_token(token_authority, ROTATED_TOKEN)

    def respond(req, *args, **kwargs):
        if req.headers.get("X-internal-token") == SPAWN_TOKEN:
            raise forbidden(req)
        return RawResponse(f"invalid response {ROTATED_TOKEN}".encode("utf-8"))

    monkeypatch.setattr(runner.urllib.request, "urlopen", respond)
    with pytest.raises(RuntimeError) as exc:
        call_loopback()
    assert ROTATED_TOKEN not in str(exc.value)
    assert "[redacted]" in str(exc.value)


@pytest.mark.parametrize(
    "disk_token",
    [None, "", "not-a-valid-token", "C" * 600, SPAWN_TOKEN],
)
def test_invalid_token_authority_fails_closed(
    monkeypatch, token_authority, disk_token,
):
    if disk_token is not None:
        write_token(token_authority, disk_token)
    seen_tokens = []

    def reject(req, *args, **kwargs):
        seen_tokens.append(req.headers.get("X-internal-token"))
        raise forbidden(req)

    monkeypatch.setattr(runner.urllib.request, "urlopen", reject)
    with pytest.raises(RuntimeError, match="HTTP 403: invalid internal token"):
        call_loopback()
    assert seen_tokens == [SPAWN_TOKEN]


def test_reload_requires_bootstrap_authority(monkeypatch, tmp_path):
    monkeypatch.setenv("BETTER_AGENT_HOME", str(tmp_path))
    monkeypatch.setenv("BETTER_CLAUDE_HOME", str(tmp_path))
    authority_owner.stop_active_host()
    write_token(tmp_path, ROTATED_TOKEN)
    seen_tokens = []

    def reject(req, *args, **kwargs):
        seen_tokens.append(req.headers.get("X-internal-token"))
        raise forbidden(req)

    monkeypatch.setattr(runner.urllib.request, "urlopen", reject)
    with pytest.raises(RuntimeError, match="HTTP 403: invalid internal token"):
        call_loopback()
    assert seen_tokens == [SPAWN_TOKEN]


@pytest.mark.parametrize("unsafe_kind", ["permissions", "owner", "symlink"])
def test_unsafe_token_authority_file_is_rejected(
    monkeypatch, token_authority, unsafe_kind,
):
    token_file = write_token(token_authority, ROTATED_TOKEN)
    target = None
    if unsafe_kind == "permissions":
        token_file.chmod(0o644)
    elif unsafe_kind == "owner":
        if not hasattr(authority_owner.os, "getuid"):
            pytest.skip("POSIX ownership is unavailable")
        monkeypatch.setattr(
            authority_owner.os,
            "getuid",
            lambda: token_file.stat().st_uid + 1,
        )
    else:
        target = token_file.with_name("token_target")
        token_file.replace(target)
        try:
            token_file.symlink_to(target)
        except OSError:
            pytest.skip("symlink creation is unavailable")

    def reject(req, *args, **kwargs):
        raise forbidden(req)

    monkeypatch.setattr(runner.urllib.request, "urlopen", reject)
    try:
        with pytest.raises(RuntimeError, match="HTTP 403: invalid internal token"):
            call_loopback()
    finally:
        token_file.unlink(missing_ok=True)
        if target is not None:
            target.unlink(missing_ok=True)


def test_concurrent_rotation_uses_one_authority_reload(
    monkeypatch, token_authority,
):
    write_token(token_authority, ROTATED_TOKEN)
    caller_count = 6
    forbidden_barrier = threading.Barrier(caller_count)
    read_count = 0
    read_lock = threading.Lock()
    original_read = authority_owner._read_backend_internal_token

    def counted_read():
        nonlocal read_count
        with read_lock:
            read_count += 1
        return original_read()

    def respond(req, *args, **kwargs):
        if req.headers.get("X-internal-token") == SPAWN_TOKEN:
            forbidden_barrier.wait(timeout=5)
            raise forbidden(req)
        return Response()

    monkeypatch.setattr(authority_owner, "_read_backend_internal_token", counted_read)
    monkeypatch.setattr(runner.urllib.request, "urlopen", respond)
    results = [None] * caller_count

    def invoke(index):
        results[index] = call_loopback()

    threads = [
        threading.Thread(target=invoke, args=(index,))
        for index in range(caller_count)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    assert results == [{"success": True}] * caller_count
    assert read_count == 1


@pytest.mark.parametrize("channel", ["runtime_operation", "capability", "approval"])
def test_every_openai_backend_channel_uses_token_authority(
    monkeypatch, token_authority, channel,
):
    write_token(token_authority, ROTATED_TOKEN)
    seen_tokens = []

    def respond(req, *args, **kwargs):
        token = req.headers.get("X-internal-token")
        seen_tokens.append(token)
        if token == SPAWN_TOKEN:
            raise forbidden(req)
        payload = {"approved": True} if channel == "approval" else {"success": True}
        return JsonResponse(payload)

    monkeypatch.setattr(runner.urllib.request, "urlopen", respond)
    if channel == "runtime_operation":
        from runtime_broker import BrokerRequest

        host = authority_owner._RunnerOperationHost(
            token_authority / "run",
            {
                "backend_url": BACKEND_URL,
                "internal_token": SPAWN_TOKEN,
                "app_session_id": "session",
                "provider_id": "provider",
                "cwd": str(token_authority),
            },
            authority_owner._ACTIVE_TOKEN_AUTHORITY,
        )
        result = host._handle(BrokerRequest(version=1, kind="catalog"))
        assert result == {"success": True}
    elif channel == "capability":
        assert json.loads(runner._capabilities_endpoint_post(
            backend_url=BACKEND_URL,
            internal_token=SPAWN_TOKEN,
            app_session_id="session",
            payload={"action": "list"},
        )) == {"success": True}
    else:
        import tool_approval_client

        assert tool_approval_client.request_tool_approval(
            backend_url=BACKEND_URL,
            internal_token=SPAWN_TOKEN,
            app_session_id="session",
            run_id="run",
            provider_kind="openai",
            tool_name="Bash",
            summary={},
        ) is True
    assert seen_tokens == [SPAWN_TOKEN, ROTATED_TOKEN]


def test_runtime_operation_retry_is_bounded_and_redacted(
    monkeypatch, token_authority,
):
    write_token(token_authority, ROTATED_TOKEN)
    seen_tokens = []

    def reject(req, *args, **kwargs):
        token = req.headers.get("X-internal-token")
        seen_tokens.append(token)
        raise forbidden(req, f"rejected {token}")

    monkeypatch.setattr(runner.urllib.request, "urlopen", reject)
    host = authority_owner._RunnerOperationHost(
        token_authority / "run",
        {
            "backend_url": BACKEND_URL,
            "internal_token": SPAWN_TOKEN,
            "app_session_id": "session",
            "provider_id": "provider",
            "cwd": str(token_authority),
        },
        authority_owner._ACTIVE_TOKEN_AUTHORITY,
    )
    from runtime_broker import BrokerRequest

    with pytest.raises(RuntimeError) as exc:
        host._handle(BrokerRequest(version=1, kind="catalog"))
    assert seen_tokens == [SPAWN_TOKEN, ROTATED_TOKEN]
    assert SPAWN_TOKEN not in str(exc.value)
    assert ROTATED_TOKEN not in str(exc.value)


def test_runtime_operation_refreshes_multiple_generations_and_redacts_history(
    monkeypatch, token_authority,
):
    tokens = (SPAWN_TOKEN, ROTATED_TOKEN, "C" * 43)
    write_token(token_authority, tokens[1])
    seen_tokens = []

    def reject(req, *args, **kwargs):
        token = req.headers.get("X-internal-token")
        seen_tokens.append(token)
        if token == tokens[1]:
            write_token(token_authority, tokens[2])
        raise forbidden(req, f"rejected {' '.join(tokens)}")

    monkeypatch.setattr(runner.urllib.request, "urlopen", reject)
    host = authority_owner._RunnerOperationHost(
        token_authority / "run",
        {
            "backend_url": BACKEND_URL,
            "internal_token": SPAWN_TOKEN,
            "app_session_id": "session",
            "provider_id": "provider",
            "cwd": str(token_authority),
        },
        authority_owner._ACTIVE_TOKEN_AUTHORITY,
    )
    from runtime_broker import BrokerRequest

    with pytest.raises(RuntimeError) as exc:
        host._handle(BrokerRequest(version=1, kind="catalog"))
    detail = str(exc.value)
    assert seen_tokens == list(tokens)
    assert detail.count("[redacted]") == len(tokens)
    assert not any(token in detail for token in tokens)
