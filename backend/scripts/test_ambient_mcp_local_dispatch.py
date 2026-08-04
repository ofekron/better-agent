"""Locks the `local=True` dispatch fix in `run_mcp_or_cli`/`build_mcp_server`.

An `OperationSpec` with a non-empty `operation=` field is, by default, routed
through the per-run operation broker (`RuntimeTransport` /
`BETTER_CLAUDE_RUNTIME_BROKER`). That broker only exists inside a Better
Agent-orchestrated run -- an ambient (session-less) launch has none, so
every `tools/call` for such a tool failed with "Better Agent runtime broker
is unavailable" even though `initialize`/`tools/list` succeeded (the
handshake working is not evidence the tool works). This locks that
`capabilities`'s ambient launch actually completes a real `tools/call`
against an isolated loopback backend, not just a resolver-level or
handshake-level check.

Run with:
    cd backend && .venv/bin/python scripts/test_ambient_mcp_local_dispatch.py
"""
from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home  # noqa: E402

_TEST_HOME = _test_home.TestHome.acquire("ba-ambient-mcp-local-dispatch-")
_SESSION_ID = "00000000-0000-4000-8000-000000000001"


def setup_function(_function) -> None:
    _test_home.engage(_TEST_HOME.path)


def teardown_module() -> None:
    _TEST_HOME.release()


class _SyntheticBackend(ThreadingHTTPServer):
    requests: list[dict]
    daemon_threads = True


class _SyntheticBackendHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        size = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(size) or b"{}")
        server = self.server
        assert isinstance(server, _SyntheticBackend)
        server.requests.append({
            "path": self.path,
            "body": body,
            "token": self.headers.get("X-Internal-Token", ""),
        })
        if self.path == f"/api/internal/sessions/{_SESSION_ID}/capabilities":
            payload = {"success": True, "capabilities": [], "active_capability_ids": []}
            status = 200
        elif self.path == "/api/internal/open-file-panel":
            payload = {"success": True, "panel": {"id": "synthetic-panel"}}
            status = 200
        else:
            payload = {"error": "unexpected test endpoint"}
            status = 404
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, _format: str, *_args) -> None:
        return


@contextmanager
def _synthetic_backend():
    server = _SyntheticBackend(("127.0.0.1", 0), _SyntheticBackendHandler)
    server.requests = []
    thread = threading.Thread(target=server.serve_forever, name="ambient-mcp-test-backend")
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}", server.requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive(), "synthetic backend thread did not stop"


def _rpc_env(backend_url: str) -> dict[str, str]:
    return {
        **os.environ,
        "BETTER_AGENT_BACKEND_URL": backend_url,
        "BETTER_CLAUDE_BACKEND_URL": backend_url,
        "BETTER_AGENT_INTERNAL_TOKEN": "synthetic-token",
        "BETTER_CLAUDE_INTERNAL_TOKEN": "synthetic-token",
    }


def _stop_process(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
    for pipe in (proc.stdin, proc.stdout, proc.stderr):
        if pipe is not None:
            pipe.close()


def _rpc_session(
    server_name: str,
    env: dict[str, str],
    *,
    ambient_launcher: bool = True,
):
    command = (
        [sys.executable, os.path.join(_BACKEND, "core_ambient_mcp_launcher.py"), server_name]
        if ambient_launcher
        else [sys.executable, os.path.join(_BACKEND, f"{server_name}_mcp.py")]
    )
    proc = subprocess.Popen(
        command,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env, text=True, bufsize=1,
    )

    def send(msg: dict) -> None:
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()

    def request(msg: dict) -> dict | None:
        send(msg)
        ready, _, _ = select.select([proc.stdout], [], [], 15)
        line = proc.stdout.readline() if ready else None
        return json.loads(line) if line else None

    try:
        initialized = request({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"},
            },
        })
        assert initialized is not None, f"{server_name} MCP initialize timed out"
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    except BaseException:
        _stop_process(proc)
        raise
    return proc, request


def _rpc_roundtrip(env: dict[str, str], call: dict) -> dict | None:
    proc, request = _rpc_session("capabilities", env)
    try:
        return request({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": call})
    finally:
        _stop_process(proc)


def test_ambient_capabilities_call_succeeds_with_local_dispatch() -> None:
    with _synthetic_backend() as (backend_url, requests):
        response = _rpc_roundtrip(
            _rpc_env(backend_url),
            {"name": "list_capabilities", "arguments": {"session_id": _SESSION_ID}},
        )

    assert response is not None
    assert not response.get("result", {}).get("isError"), response
    assert len(requests) == 1, requests
    request = requests[0]
    assert request == {
        "path": f"/api/internal/sessions/{_SESSION_ID}/capabilities",
        "body": {"action": "list"},
        "token": request["token"],
    }
    assert request["token"]


def test_without_the_ambient_marker_the_call_hits_the_absent_broker() -> None:
    """Regression guard: `core_ambient_mcp_launcher.py` always sets
    `BETTER_CLAUDE_AMBIENT_LAUNCH=1`. Spawning `capabilities_mcp.py` directly
    (bypassing the launcher, so the marker is unset) must fail with the
    broker-unavailable error -- proving the marker, not something else,
    is what makes the ambient call above succeed."""
    with _synthetic_backend() as (backend_url, requests):
        env = _rpc_env(backend_url)
        env.pop("BETTER_CLAUDE_AMBIENT_LAUNCH", None)
        env.pop("BETTER_AGENT_AMBIENT_LAUNCH", None)
        proc, request = _rpc_session("capabilities", env, ambient_launcher=False)
        try:
            response = request({
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {
                    "name": "list_capabilities",
                    "arguments": {"session_id": _SESSION_ID},
                },
            })
        finally:
            _stop_process(proc)
    text = str(((response or {}).get("result") or {}).get("content", [{}])[0].get("text", ""))
    assert "runtime broker" in text.lower(), response
    assert requests == []


def test_ambient_ui_tool_list_is_narrowed_to_open_file_panel() -> None:
    """request_user_input/request_user_approval/start_file_discussion and
    open_file_panel(mode="inline") all attach to an in-flight turn's
    assistant message, which doesn't exist ambiently -- they must not even
    be advertised (see core_ambient_mcp_launcher.py's docstring)."""
    with _synthetic_backend() as (backend_url, requests):
        proc, request = _rpc_session("ui", _rpc_env(backend_url))
        try:
            response = request({
                "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {},
            })
        finally:
            _stop_process(proc)
    names = sorted(
        tool.get("name") for tool in ((response or {}).get("result") or {}).get("tools", [])
    )
    assert names == ["open_file_panel"]
    assert requests == []


def test_ambient_open_file_panel_mode_panel_succeeds() -> None:
    """mode='panel' is a plain per-session state mutation, not tied to any
    in-flight turn -- it's the one ui tool that can do real ambient work."""
    with _synthetic_backend() as (backend_url, requests):
        proc, request = _rpc_session("ui", _rpc_env(backend_url))
        try:
            response = request({
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {
                    "name": "open_file_panel",
                    "arguments": {
                        "mode": "panel",
                        "path": "backend/scripts/test_ambient_mcp_local_dispatch.py",
                        "session_id": _SESSION_ID,
                    },
                },
            })
        finally:
            _stop_process(proc)
    is_error = bool((response or {}).get("result", {}).get("isError"))
    assert response is not None and not is_error, response
    body = json.loads(response["result"]["content"][0]["text"])
    assert body["panel"]["id"] == "synthetic-panel"
    assert len(requests) == 1, requests
    request = requests[0]
    assert request == {
        "path": "/api/internal/open-file-panel",
        "body": {
            "app_session_id": _SESSION_ID,
            "mode": "panel",
            "path": "backend/scripts/test_ambient_mcp_local_dispatch.py",
            "start_line": None,
            "end_line": None,
            "selected_start": None,
            "selected_end": None,
        },
        "token": request["token"],
    }
    assert request["token"]


if __name__ == "__main__":
    tests = [
        fn for name, fn in sorted(globals().items())
        if name.startswith("test_") and callable(fn)
    ]
    for test in tests:
        test()
    print(f"\n{len(tests)}/{len(tests)} ambient MCP local-dispatch tests passed")
