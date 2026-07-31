from __future__ import annotations

import json
import socket
import sys
import threading
import asyncio
import types
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import _test_home
_test_home.isolate("bc-cli-session-resolution-test-")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cli  # noqa: E402


class Handler(BaseHTTPRequestHandler):
    created_body: dict | None = None

    def do_GET(self) -> None:
        if self.path == "/api/config":
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'{"detail":"Not Found"}')
            return
        if self.path == "/api/sessions":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"sessions":[]}')
            return
        if self.path == "/api/sessions/backend-session":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({
                "id": "backend-session",
                "cwd": r"C:\Users\Lenovo\better-agent",
                "node_id": "lenovo",
                "provider_id": "provider-1",
                "bare_config": True,
            }).encode())
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self) -> None:
        if self.path != "/api/sessions":
            self.send_response(404)
            self.end_headers()
            return
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        Handler.created_body = json.loads(raw.decode("utf-8"))
        self.send_response(200)
        self.end_headers()
        payload = {
            "id": "created-session",
            "name": Handler.created_body.get("name"),
            "cwd": Handler.created_body.get("cwd"),
            "model": Handler.created_body.get("model"),
            "orchestration_mode": Handler.created_body.get("orchestration_mode"),
            "provider_id": Handler.created_body.get("provider_id"),
            "node_id": Handler.created_body.get("node_id"),
            "bare_config": Handler.created_body.get("bare_config"),
        }
        self.wfile.write(json.dumps(payload).encode())

    def log_message(self, *_args) -> None:
        return


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def run_server(port: int) -> HTTPServer:
    server = HTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent: dict | None = None

    async def send(self, raw: str) -> None:
        self.sent = json.loads(raw)

    async def recv(self) -> str:
        return json.dumps({"type": "turn_complete"})


class FakeRenderer:
    def handle(self, _event: dict) -> None:
        return


async def assert_client_backend_sends_loopback_backend_url() -> None:
    backend = cli.ClientBackend(8123)
    ws = FakeWebSocket()
    backend._ws = ws
    result = await backend.send_prompt(
        prompt="probe",
        session={"id": "session-id"},
        model="glm-5.2",
        cwd="/tmp",
        mode="manager",
        renderer=FakeRenderer(),
    )
    assert result == "turn_complete"
    assert ws.sent is not None
    assert ws.sent["backend_url"] == "http://127.0.0.1:8123"


async def assert_client_backend_allows_large_ws_frames() -> None:
    calls: list[dict] = []

    async def fake_connect(url: str, **kwargs):
        calls.append({"url": url, **kwargs})
        return FakeWebSocket()

    original = sys.modules.get("websockets")
    sys.modules["websockets"] = types.SimpleNamespace(connect=fake_connect)
    try:
        backend = cli.ClientBackend(8124)
        await backend.start()
    finally:
        if original is None:
            sys.modules.pop("websockets", None)
        else:
            sys.modules["websockets"] = original

    assert calls == [{
        "url": cli._ws_chat_url(8124),
        "max_size": None,
        "ping_timeout": None,
    }]


def main() -> int:
    port = free_port()
    server = run_server(port)
    try:
        assert cli._probe_backend(port, retries=1), "backend probe should accept /api/sessions when /api/config is absent"
        session = cli._fetch_backend_session(port, "backend-session")
        assert session and session["id"] == "backend-session", "backend-created session should be fetchable"
        resumed = cli.resolve_backend_session(
            port=port,
            session_id="backend-session",
            cwd=None,
            model="glm-5.2",
            mode=None,
            provider_id=None,
        )
        assert resumed["cwd"] == r"C:\Users\Lenovo\better-agent"
        assert resumed["node_id"] == "lenovo"
        try:
            cli.resolve_backend_session(
                port=port,
                session_id="backend-session",
                cwd="/Users/ofekron/better-claude",
                model="glm-5.2",
                mode=None,
                provider_id=None,
            )
        except SystemExit as exc:
            assert "--cwd does not match" in str(exc)
        else:
            raise AssertionError("mismatched resumed cwd was accepted")
        created = cli.resolve_backend_session(
            port=port,
            session_id=None,
            cwd=r"C:\Users\Lenovo\better-agent",
            model="glm-5.2",
            mode="manager",
            provider_id="provider-1",
            node_id="lenovo",
            worker_creation_policy="approve",
            bare_config=True,
        )
        assert created["id"] == "created-session"
        assert Handler.created_body == {
            "name": "cli-default",
            "model": "glm-5.2",
            "cwd": r"C:\Users\Lenovo\better-agent",
            "orchestration_mode": "manager",
            "source": "cli",
            "provider_id": "provider-1",
            "node_id": "lenovo",
            "worker_creation_policy": "approve",
            "bare_config": True,
        }
        assert cli._resolve_cli_cwd(
            r"C:\Users\Lenovo\better-agent",
            "lenovo",
        ) == r"C:\Users\Lenovo\better-agent"
        try:
            cli._resolve_cli_cwd("relative/path", "lenovo")
        except SystemExit as exc:
            assert "invalid --cwd for node 'lenovo'" in str(exc)
        else:
            raise AssertionError("relative remote cwd was accepted")
        try:
            cli._resolve_cli_cwd(None, "lenovo")
        except SystemExit as exc:
            assert "--cwd is required" in str(exc)
        else:
            raise AssertionError("remote session creation accepted an omitted cwd")
        asyncio.run(assert_client_backend_sends_loopback_backend_url())
        asyncio.run(assert_client_backend_allows_large_ws_frames())
    finally:
        server.shutdown()
    print("cli backend session resolution test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
