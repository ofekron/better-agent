"""Internal producers reach the runtime over the app-endpoint descriptor.

Locks:
- loopback_http.request_internal speaks UDS (POSIX) and loopback TCP
  (Windows-kind descriptor) with the X-Internal-Token header
- HTTP >= 400 surfaces as LoopbackHTTPStatusError with the runtime's
  error detail
- no descriptor -> fail closed (RuntimeEndpointError), never a fallback
  to the BFF port
- no runner/tool-subprocess file consumes BETTER_CLAUDE_BACKEND_URL or
  urllib for backend transport anymore
"""

from __future__ import annotations

import json
import socketserver
import sys
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import _test_home

_TEST_HOME = _test_home.isolate(prefix="ba-ipc-transport-")

import loopback_http
import runtime_endpoints

_BACKEND_DIR = Path(__file__).resolve().parents[1]


class _Handler(BaseHTTPRequestHandler):
    seen: list[dict] = []
    respond_status = 200
    respond_body = b'{"ok": true}'

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        _Handler.seen.append({
            "path": self.path,
            "token": self.headers.get("X-Internal-Token"),
            "body": self.rfile.read(length),
        })
        self.send_response(_Handler.respond_status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(_Handler.respond_body)))
        self.end_headers()
        self.wfile.write(_Handler.respond_body)

    def log_message(self, *args):
        pass


class _UDSHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def get_request(self):
        request, _client_address = super().get_request()
        return request, ("uds", 0)


class _TCPHTTPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True


def _serve(server) -> threading.Thread:
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread


def _reset_handler():
    _Handler.seen = []
    _Handler.respond_status = 200
    _Handler.respond_body = b'{"ok": true}'


def test_request_internal_over_uds():
    _reset_handler()
    import runtime_ipc

    runtime_ipc.ensure_socket_dir()
    socket_path = runtime_endpoints.app_socket_path()
    socket_path.unlink(missing_ok=True)
    server = _UDSHTTPServer(str(socket_path), _Handler)
    _serve(server)
    runtime_endpoints.write_app_endpoint({"kind": "uds", "path": str(socket_path)})
    try:
        raw = loopback_http.request_internal(
            "POST", "/api/internal/echo", b'{"a": 1}',
            internal_token="tkn-uds", timeout=5.0,
        )
        assert json.loads(raw) == {"ok": True}
        assert _Handler.seen == [{
            "path": "/api/internal/echo",
            "token": "tkn-uds",
            "body": b'{"a": 1}',
        }]

        _Handler.respond_status = 403
        _Handler.respond_body = json.dumps({"detail": "internal token rejected"}).encode()
        try:
            loopback_http.request_internal(
                "POST", "/api/internal/echo", b"{}",
                internal_token="bad", timeout=5.0,
            )
        except loopback_http.LoopbackHTTPStatusError as exc:
            assert exc.code == 403
            assert loopback_http.loopback_http_error_message(exc) == "internal token rejected"
        else:
            raise AssertionError("HTTP 403 did not raise LoopbackHTTPStatusError")
    finally:
        server.shutdown()
        server.server_close()
        socket_path.unlink(missing_ok=True)
        runtime_endpoints.clear_app_endpoint()


def test_request_internal_over_tcp_descriptor():
    _reset_handler()
    server = _TCPHTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    _serve(server)
    runtime_endpoints.write_app_endpoint({"kind": "tcp", "host": "127.0.0.1", "port": port})
    try:
        raw = loopback_http.request_internal(
            "POST", "/api/internal/echo", b'{"win": 1}',
            internal_token="tkn-tcp", timeout=5.0,
        )
        assert json.loads(raw) == {"ok": True}
        assert _Handler.seen[0]["token"] == "tkn-tcp"
    finally:
        server.shutdown()
        server.server_close()
        runtime_endpoints.clear_app_endpoint()


def test_missing_descriptor_fails_closed():
    runtime_endpoints.clear_app_endpoint()
    try:
        loopback_http.request_internal(
            "POST", "/api/internal/echo", b"{}",
            internal_token="tkn", timeout=2.0,
        )
    except runtime_endpoints.RuntimeEndpointError:
        pass
    else:
        raise AssertionError("missing descriptor did not fail closed")


def test_internal_producers_do_not_use_bff_transport():
    """Runners and stdio tool subprocesses must not consume the BFF URL
    or urllib for backend transport — descriptor IPC only."""
    for name in (
        "runner.py",
        "runner_codex.py",
        "runner_better_agent.py",
        "open_file_panel_mcp.py",
        "capabilities_mcp.py",
        "open_config_panel_mcp.py",
        "communicate_mcp.py",
        "tool_approval_client.py",
        "loopback_http.py",
    ):
        text = (_BACKEND_DIR / name).read_text(encoding="utf-8")
        assert "urllib.request.urlopen" not in text, f"{name} still uses urllib transport"
        assert 'BETTER_CLAUDE_BACKEND_URL"' not in text, f"{name} still consumes the BFF URL"


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}: {exc}")
    sys.exit(1 if failures else 0)
