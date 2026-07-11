"""Decoupled runtime + BFF (plan Phase 3).

Locks:
- the full runtime app serves on the internal endpoint (API-only)
- the endpoint descriptor is written only after readiness
- the BFF proxies REST to the runtime and serves the SPA shell
- the BFF bridges WebSockets (close-code propagation proven against
  the runtime's catch-all 1008 handler)
- killing the BFF leaves the runtime fully alive (the decoupling
  guarantee), and CLI stop-runtime shuts the runtime down gracefully
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import _test_home

_TEST_HOME = _test_home.isolate(prefix="ba-runtime-bff-")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import runtime_endpoints

_BACKEND_DIR = Path(__file__).resolve().parents[1]


def _env(home: str) -> dict:
    return {**os.environ, "BETTER_AGENT_HOME": home, "BETTER_CLAUDE_HOME": home,
            "PYTHONPATH": str(_BACKEND_DIR)}


def _free_port() -> int:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def _wait(predicate, deadline_seconds: float) -> bool:
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def _http_get_tcp(port: int, path: str, timeout: float = 5.0) -> tuple[int, bytes]:
    return runtime_endpoints.http_get(
        {"kind": "tcp", "host": "127.0.0.1", "port": port}, path, timeout=timeout
    )


def test_decoupled_runtime_and_bff_end_to_end():
    home = tempfile.mkdtemp(prefix="ba-bff-home-")
    env = _env(home)
    dist = Path(tempfile.mkdtemp(prefix="ba-bff-dist-"))
    (dist / "index.html").write_text("<html>ba-spa-shell</html>", encoding="utf-8")
    bff_port = _free_port()

    runtime = subprocess.Popen(
        [sys.executable, "-m", "runtime_cli", "start-runtime", "--foreground"],
        cwd=str(_BACKEND_DIR), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    bff = None
    try:
        descriptor_path = Path(home) / "runtime" / "app_endpoint.json"
        assert _wait(descriptor_path.exists, 30), "endpoint descriptor never written"
        descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))

        def _runtime_ready() -> bool:
            try:
                return runtime_endpoints.http_get(descriptor, "/healthz", timeout=2.0)[0] == 200
            except OSError:
                return False

        assert _wait(_runtime_ready, 60), "runtime app never became healthy"

        # Auth gate holds on the internal endpoint too: no token → 401.
        status, _b = runtime_endpoints.http_get(descriptor, "/api/sessions", timeout=10.0)
        assert status == 401

        # First-run bootstrap over the UDS: a direct unix-socket peer is
        # same-uid local, so setup must work without a nonce.
        status, body = runtime_endpoints.http_request(
            descriptor, "POST", "/api/auth/setup",
            body=json.dumps({"username": "bff-test", "password": "pw-123456"}).encode(),
            headers={"Content-Type": "application/json"}, timeout=10.0,
        )
        assert status == 200, body
        auth_headers = {"Authorization": f"Bearer {json.loads(body)['token']}"}

        status, body = runtime_endpoints.http_request(
            descriptor, "GET", "/api/sessions", headers=auth_headers, timeout=10.0
        )
        assert status == 200
        direct_sessions = json.loads(body)

        bff = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "bff_server:app",
             "--host", "127.0.0.1", "--port", str(bff_port)],
            cwd=str(_BACKEND_DIR),
            env={**env, "BETTER_AGENT_BFF_DIST": str(dist)},
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        assert _wait(lambda: _bff_health(bff_port), 30), "bff never became healthy"

        # REST through the BFF: the gate is preserved (401 without auth)
        # and the Authorization header passes through untouched.
        status, _b = _http_get_tcp(bff_port, "/api/sessions")
        assert status == 401
        status, body = runtime_endpoints.http_request(
            {"kind": "tcp", "host": "127.0.0.1", "port": bff_port},
            "GET", "/api/sessions", headers=auth_headers,
        )
        assert status == 200
        assert json.loads(body) == direct_sessions

        # App-owned routes terminate in the BFF. They keep the same auth
        # boundary, but the runtime no longer exposes or persists them.
        draft_body = json.dumps({
            "path": "/tmp/bff-owned.txt",
            "node_id": "primary",
            "content": "owned by bff",
        }).encode()
        status, _body = runtime_endpoints.http_request(
            {"kind": "tcp", "host": "127.0.0.1", "port": bff_port},
            "POST", "/api/file/draft", body=draft_body,
            headers={"Content-Type": "application/json"},
        )
        assert status == 401
        status, body = runtime_endpoints.http_request(
            {"kind": "tcp", "host": "127.0.0.1", "port": bff_port},
            "POST", "/api/file/draft", body=draft_body,
            headers={**auth_headers, "Content-Type": "application/json"},
        )
        assert status == 200, body
        assert json.loads(body)["content"] == "owned by bff"
        status, _body = runtime_endpoints.http_request(
            descriptor, "POST", "/api/file/draft", body=draft_body,
            headers={**auth_headers, "Content-Type": "application/json"},
        )
        assert status == 404

        status, _body = runtime_endpoints.http_request(
            {"kind": "tcp", "host": "127.0.0.1", "port": bff_port},
            "PATCH", "/api/sessions/nonexistent/draft",
            body=json.dumps({"draft_input": "x", "client_seq": 1}).encode(),
            headers={**auth_headers, "Content-Type": "application/json"},
        )
        assert status == 404

        # SPA shell + client-route fallback come from the BFF itself.
        status, body = _http_get_tcp(bff_port, "/")
        assert status == 200 and b"ba-spa-shell" in body
        status, body = _http_get_tcp(bff_port, "/s/some-client-route")
        assert status == 200 and b"ba-spa-shell" in body
        # API 404s stay JSON — never the SPA shell.
        status, body = runtime_endpoints.http_request(
            {"kind": "tcp", "host": "127.0.0.1", "port": bff_port},
            "GET", "/api/definitely-not-a-route", headers=auth_headers,
        )
        assert status == 404 and b"ba-spa-shell" not in body

        # WS bridge: /ws/chat without credentials accepts then closes
        # with a real 1008 frame — receiving that code through the BFF
        # proves both handshakes completed and the close propagated.
        close_code = _ws_close_code_via_bff(bff_port, "/ws/chat")
        assert close_code == 1008

        # AND the bearer must pass THROUGH the BFF to the runtime auth
        # gate: the same token authenticates the WS, so it does NOT get
        # the 1008 unauthenticated close.
        bearer = auth_headers["Authorization"].split(" ", 1)[1]
        authed_code = _ws_close_code_via_bff(bff_port, f"/ws/chat?token={bearer}")
        assert authed_code != 1008, "bearer did not authenticate through the BFF"

        # THE decoupling guarantee: killing the BFF leaves the runtime alive.
        bff.kill()
        bff.wait(timeout=10)
        status, _body = runtime_endpoints.http_request(
            descriptor, "GET", "/api/sessions", headers=auth_headers, timeout=10.0
        )
        assert status == 200

        # Graceful runtime stop through the CLI (authenticated IPC op).
        stop = subprocess.run(
            [sys.executable, "-m", "runtime_cli", "stop-runtime"],
            cwd=str(_BACKEND_DIR), env=env, capture_output=True, text=True, timeout=60,
        )
        assert stop.returncode == 0, stop.stdout + stop.stderr
        # uvicorn re-raises the captured SIGTERM after graceful shutdown
        # (proper unix exit semantics), so -SIGTERM IS the graceful code.
        import signal as _signal

        assert runtime.wait(timeout=30) in (0, -_signal.SIGTERM)
        assert not descriptor_path.exists()
    finally:
        for proc in (bff, runtime):
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)


def _bff_health(port: int) -> bool:
    try:
        status, body = _http_get_tcp(port, "/bff/healthz", timeout=2.0)
    except OSError:
        return False
    return status == 200 and json.loads(body).get("runtime") is True


def _ws_close_code_via_bff(port: int, path: str) -> int:
    import asyncio

    import websockets

    async def _roundtrip() -> int:
        try:
            async with websockets.connect(f"ws://127.0.0.1:{port}{path}") as ws:
                await ws.recv()
        except websockets.exceptions.ConnectionClosed as closed:
            return closed.rcvd.code if closed.rcvd else -1
        return -2

    return asyncio.run(_roundtrip())


def test_bff_readiness_requires_reachable_runtime():
    """CLI readiness must distinguish 'BFF serving' from 'BFF serving AND
    runtime reachable'. Closes the Codex finding that start-bff could
    report success against a dead runtime."""
    import runtime_cli

    saved = runtime_cli.runtime_endpoints.http_get
    try:
        runtime_cli.runtime_endpoints.http_get = lambda *a, **k: (
            200, json.dumps({"ok": True, "runtime": False}).encode()
        )
        assert runtime_cli._bff_alive(1234) is False  # runtime down → not ready
        assert runtime_cli._bff_alive(1234, require_runtime=False) is True  # serving
        runtime_cli.runtime_endpoints.http_get = lambda *a, **k: (
            200, json.dumps({"ok": True, "runtime": True}).encode()
        )
        assert runtime_cli._bff_alive(1234) is True
    finally:
        runtime_cli.runtime_endpoints.http_get = saved


def test_start_bff_reaps_child_when_runtime_unreachable():
    """A BFF that comes up but can't reach the runtime is a failed start;
    cmd_start_bff must terminate the spawned child — never leave an
    untracked BFF proxying to a dead runtime. Closes the Codex
    lifecycle-leak finding."""
    import runtime_cli

    class _FakeProc:
        def __init__(self) -> None:
            self.pid = 4242
            self._alive = True
            self.terminated = False
            self.returncode = None

        def poll(self):
            return None if self._alive else self.returncode

        def terminate(self):
            self.terminated = True
            self._alive = False
            self.returncode = -15

        def kill(self):
            self._alive = False
            self.returncode = -9

        def wait(self, timeout=None):
            return self.returncode

    fake = _FakeProc()
    saved_spawn = runtime_cli._spawn_detached
    saved_alive = runtime_cli._bff_alive
    saved_read = runtime_cli.runtime_endpoints.read_app_endpoint
    try:
        runtime_cli.runtime_endpoints.read_app_endpoint = lambda: {
            "kind": "uds", "path": "/nonexistent.sock"
        }
        runtime_cli._spawn_detached = lambda *a, **k: fake
        runtime_cli._bff_alive = lambda *a, **k: False  # never reaches runtime
        runtime_cli._START_DEADLINE_SECONDS = 0.2
        rc = runtime_cli.cmd_start_bff(port=59999, foreground=False)
        assert rc == 1
        assert fake.terminated is True  # child reaped, not leaked
        assert not runtime_cli._bff_pid_path().exists()  # no stale pid file
    finally:
        runtime_cli._spawn_detached = saved_spawn
        runtime_cli._bff_alive = saved_alive
        runtime_cli.runtime_endpoints.read_app_endpoint = saved_read
        runtime_cli._START_DEADLINE_SECONDS = 30.0


def test_bff_fails_closed_without_runtime_descriptor():
    # Fresh home with no descriptor: the BFF must refuse to start.
    home = tempfile.mkdtemp(prefix="ba-bff-nodesc-")
    port = _free_port()
    bff = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "bff_server:app",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(_BACKEND_DIR), env=_env(home),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        assert bff.wait(timeout=30) != 0  # startup failure, not a silent serve
    finally:
        if bff.poll() is None:
            bff.kill()
            bff.wait(timeout=10)


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
