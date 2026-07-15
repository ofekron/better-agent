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
import signal
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import _test_home

_TEST_HOME = _test_home.isolate(prefix="ba-runtime-bff-")

import runtime_endpoints

_BACKEND_DIR = Path(__file__).resolve().parents[1]


def test_bff_mobile_project_routes_have_trusted_cors_and_require_auth():
    import asyncio

    import httpx
    from fastapi.testclient import TestClient

    import bff_server
    import project_store
    import user_prefs
    from bff_runtime_service import runtime_service
    from bff_runtime_upstream import RuntimeUpstream

    requested_paths: list[str] = []
    previous_runtime = bff_server.runtime_upstream
    previous_bind = user_prefs.get_network_bind_address()

    async def upstream(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        if request.url.path == "/api/auth/me":
            if request.headers.get("authorization") != "Bearer mobile-token":
                return httpx.Response(401, json={"detail": "unauthenticated"})
            assert request.headers.get("origin") in {"http://localhost", "https://evil.example"}
            assert request.headers.get("x-forwarded-host") == "100.101.102.103:8000"
            return httpx.Response(200, json={"username": "mobile"})
        assert request.headers["x-better-agent-bff-token"] == "service-test"
        if request.url.path == "/api/bff-runtime/projects/facts":
            return httpx.Response(200, json={"candidates": [], "aggregates": []})
        if request.url.path == "/api/bff-runtime/projects/status":
            return httpx.Response(200, json={"aggregates": []})
        raise AssertionError(request.url.path)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream),
        base_url="http://better-agent-runtime",
    )
    upstream_proxy = RuntimeUpstream(
        descriptor_reader=lambda: {"kind": "tcp", "host": "127.0.0.1", "port": 1},
        token_reader=lambda: "service-test",
        client_factory=lambda _descriptor: client,
    )
    bff_server.runtime_upstream = upstream_proxy
    runtime_service.bind(upstream_proxy)
    user_prefs.set_network_bind_address("0.0.0.0")
    try:
        project_store.add_project(path="/tmp/mobile-project", node_id="primary")
        app_client = TestClient(bff_server.app)
        cors_headers = {
            "Origin": "http://localhost",
            "Host": "100.101.102.103:8000",
        }
        preflight = app_client.options(
            "/api/projects",
            headers={
                **cors_headers,
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization",
            },
        )
        assert preflight.status_code == 200, preflight.text
        assert preflight.headers.get("access-control-allow-origin") == "http://localhost"

        rejected_preflight = app_client.options(
            "/api/projects",
            headers={
                "Origin": "https://evil.example",
                "Host": "100.101.102.103:8000",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization",
            },
        )
        assert rejected_preflight.status_code == 400, rejected_preflight.text
        assert rejected_preflight.headers.get("access-control-allow-origin") is None

        rejected_get = app_client.get(
            "/api/projects",
            headers={
                "Origin": "https://evil.example",
                "Host": "100.101.102.103:8000",
                "Authorization": "Bearer mobile-token",
            },
        )
        assert rejected_get.status_code == 200, rejected_get.text
        assert rejected_get.headers.get("access-control-allow-origin") is None

        invalid_bearer = app_client.get(
            "/api/projects",
            headers={**cors_headers, "Authorization": "Bearer wrong-token"},
        )
        assert invalid_bearer.status_code == 401, invalid_bearer.text
        assert invalid_bearer.headers.get("access-control-allow-origin") == "http://localhost"

        unauthenticated = app_client.get("/api/projects/status", headers=cors_headers)
        assert unauthenticated.status_code == 401, unauthenticated.text
        assert unauthenticated.headers.get("access-control-allow-origin") == "http://localhost"

        projects = app_client.get(
            "/api/projects",
            headers={**cors_headers, "Authorization": "Bearer mobile-token"},
        )
        assert projects.status_code == 200, projects.text
        assert projects.headers.get("access-control-allow-origin") == "http://localhost"
        assert projects.json()["projects"]

        status = app_client.get(
            "/api/projects/status",
            headers={**cors_headers, "Authorization": "Bearer mobile-token"},
        )
        assert status.status_code == 200, status.text
        assert status.headers.get("access-control-allow-origin") == "http://localhost"
        assert "/api/auth/me" in requested_paths
        assert "/api/bff-runtime/projects/status" in requested_paths
    finally:
        runtime_service.unbind()
        bff_server.runtime_upstream = previous_runtime
        user_prefs.set_network_bind_address(previous_bind)
        asyncio.run(client.aclose())


def test_bff_project_routes_are_owned_for_auth():
    import bff_app_routes

    assert bff_app_routes.owns_path("GET", "/api/projects")
    assert bff_app_routes.owns_path("POST", "/api/projects")
    assert bff_app_routes.owns_path("DELETE", "/api/projects")
    assert bff_app_routes.owns_path("GET", "/api/projects/status")
    assert bff_app_routes.owns_path("POST", "/api/projects/touch")
    assert not bff_app_routes.owns_path("OPTIONS", "/api/projects")
    assert not bff_app_routes.owns_path("GET", "/api/projects/unknown")


def test_bff_mobile_refresh_proxies_with_trusted_cors_and_origin():
    import asyncio

    import httpx
    from fastapi.testclient import TestClient

    import bff_server
    import user_prefs
    from bff_runtime_upstream import RuntimeUpstream

    async def runtime(scope, receive, send):
        assert scope["path"] == "/api/auth/refresh"
        headers = {key.decode("latin-1"): value.decode("latin-1") for key, value in scope["headers"]}
        body = json.dumps({
            "access_token": "access-2",
            "refresh_token": "refresh-2",
            "origin": headers.get("origin"),
        }).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"application/json")],
        })
        await send({"type": "http.response.body", "body": body})

    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=runtime),
        base_url="http://better-agent-runtime",
    )
    previous_runtime = bff_server.runtime_upstream
    previous_bind = user_prefs.get_network_bind_address()
    bff_server.runtime_upstream = RuntimeUpstream(
        descriptor_reader=lambda: {"kind": "tcp", "host": "127.0.0.1", "port": 1},
        token_reader=lambda: "service-test",
        client_factory=lambda _descriptor: client,
    )
    user_prefs.set_network_bind_address("0.0.0.0")
    try:
        app_client = TestClient(bff_server.app)
        cors_headers = {
            "Origin": "http://localhost",
            "Host": "100.101.102.103:8000",
        }
        preflight = app_client.options(
            "/api/auth/refresh",
            headers={
                **cors_headers,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )
        assert preflight.status_code == 200, preflight.text
        assert preflight.headers.get("access-control-allow-origin") == "http://localhost"

        response = app_client.post(
            "/api/auth/refresh",
            json={"refresh_token": "refresh-1"},
            headers=cors_headers,
        )
        assert response.status_code == 200, response.text
        assert response.headers.get("access-control-allow-origin") == "http://localhost"
        assert response.json() == {
            "access_token": "access-2",
            "refresh_token": "refresh-2",
            "origin": "http://localhost",
        }
    finally:
        bff_server.runtime_upstream = previous_runtime
        user_prefs.set_network_bind_address(previous_bind)
        asyncio.run(client.aclose())


def test_bff_mobile_websocket_forwards_origin_host_and_token_query():
    from types import SimpleNamespace

    import bff_server

    class FakeHeaders:
        def __init__(self) -> None:
            self._values = {
                "origin": "http://localhost",
                "host": "100.101.102.103:8000",
            }

        def get(self, key: str):
            return self._values.get(key)

    websocket = SimpleNamespace(
        headers=FakeHeaders(),
        client=SimpleNamespace(host="192.168.1.50"),
        url=SimpleNamespace(path="/ws/chat", query="token=mobile-token", scheme="ws"),
    )
    headers = dict(bff_server._ws_forward_headers(websocket))
    assert headers["origin"] == "http://localhost"
    assert headers["x-forwarded-host"] == "100.101.102.103:8000"
    assert headers["x-forwarded-for"] == "192.168.1.50"
    target = websocket.url.path
    if websocket.url.query:
        target += f"?{websocket.url.query}"
    assert target == "/ws/chat?token=mobile-token"


def test_projected_proxy_recomputes_length_and_preserves_duplicate_headers():
    import httpx
    from fastapi.testclient import TestClient

    import bff_server

    async def upstream(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/sessions/header-test"
        body = b'{"id":"header-test"}'
        return httpx.Response(
            200,
            content=body,
            headers=[
                ("content-type", "application/json"),
                ("content-length", str(len(body))),
                ("set-cookie", "first=1; Path=/"),
                ("set-cookie", "second=2; Path=/"),
            ],
        )

    from bff_runtime_upstream import RuntimeUpstream

    previous = bff_server.runtime_upstream
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream),
        base_url="http://better-agent-runtime",
    )
    bff_server.runtime_upstream = RuntimeUpstream(
        descriptor_reader=lambda: {
            "kind": "tcp",
            "host": "127.0.0.1",
            "port": 1,
        },
        token_reader=lambda: "service-test",
        client_factory=lambda _descriptor: client,
    )
    try:
        response = TestClient(bff_server.app).get("/api/sessions/header-test")
        assert response.status_code == 200
        assert int(response.headers["content-length"]) == len(response.content)
        assert response.headers.get_list("set-cookie") == [
            "first=1; Path=/",
            "second=2; Path=/",
        ]
    finally:
        bff_server.runtime_upstream = previous
        import asyncio

        asyncio.run(client.aclose())


def test_bff_websocket_close_code_sanitizer_maps_reserved_codes():
    from websockets.frames import CloseCode

    import bff_server

    assert bff_server._browser_ws_close_code(1008) == 1008
    assert bff_server._browser_ws_close_code(CloseCode.INTERNAL_ERROR) == 1011
    assert bff_server._browser_ws_close_code(3000) == 3000
    assert bff_server._browser_ws_close_code(4999) == 4999

    for code in (None, object(), 999, 1005, 1006, 1015, 5000):
        assert bff_server._browser_ws_close_code(code) == 1000


def test_bff_websocket_proxy_maps_abnormal_upstream_close_to_normal():
    import asyncio
    from types import SimpleNamespace

    from websockets.frames import CloseCode

    import bff_server

    class FakeLease:
        descriptor = {"kind": "tcp", "host": "127.0.0.1", "port": 1}

        async def release(self) -> None:
            pass

    class FakeRuntimeUpstream:
        async def acquire(self) -> FakeLease:
            return FakeLease()

    class FakeUpstream:
        close_code = CloseCode.ABNORMAL_CLOSURE

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

        async def send(self, _data):
            pass

        async def close(self) -> None:
            pass

    class FakeWebSocket:
        url = SimpleNamespace(path="/ws/chat", query="", scheme="ws")
        headers = SimpleNamespace(raw=[], get=lambda _key: None)
        client = SimpleNamespace(host="127.0.0.1")

        def __init__(self) -> None:
            self.accepted = False
            self.close_code: int | None = None

        async def accept(self) -> None:
            self.accepted = True

        async def receive(self):
            await asyncio.Future()

        async def send_text(self, _frame: str) -> None:
            pass

        async def send_bytes(self, _frame: bytes) -> None:
            pass

        async def close(self, code: int = 1000, reason: str | None = None) -> None:
            self.close_code = code

    async def exercise() -> FakeWebSocket:
        previous_runtime = bff_server.runtime_upstream
        previous_connect = bff_server.websockets.connect
        websocket = FakeWebSocket()

        async def fake_connect(*_args, **_kwargs):
            return FakeUpstream()

        bff_server.runtime_upstream = FakeRuntimeUpstream()
        bff_server.websockets.connect = fake_connect
        try:
            await bff_server.proxy_ws(websocket, "chat")
        finally:
            bff_server.websockets.connect = previous_connect
            bff_server.runtime_upstream = previous_runtime
        return websocket

    websocket = asyncio.run(exercise())
    assert websocket.accepted is True
    assert websocket.close_code == 1000


def test_runtime_upstream_rotates_endpoint_and_drains_active_generation():
    import asyncio
    import httpx

    from bff_runtime_upstream import RuntimeUpstream

    state = {
        "descriptor": {"kind": "tcp", "host": "127.0.0.1", "port": 41001},
        "token": "token-1",
    }
    clients: list[httpx.AsyncClient] = []

    def client_factory(descriptor: dict) -> httpx.AsyncClient:
        port = descriptor["port"]

        async def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"port": port})

        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url=f"http://127.0.0.1:{port}",
        )
        clients.append(client)
        return client

    async def exercise() -> None:
        upstream = RuntimeUpstream(
            descriptor_reader=lambda: dict(state["descriptor"]),
            token_reader=lambda: state["token"],
            client_factory=client_factory,
        )
        old = await upstream.acquire()
        assert (await old.client.get("/healthz")).json() == {"port": 41001}
        state["descriptor"] = {
            "kind": "tcp",
            "host": "127.0.0.1",
            "port": 41002,
        }
        state["token"] = "token-2"
        current = await upstream.acquire()
        assert current.service_token == "token-2"
        assert (await current.client.get("/healthz")).json() == {"port": 41002}
        assert (await old.client.get("/healthz")).json() == {"port": 41001}
        await old.release()
        assert clients[0].is_closed
        assert not clients[1].is_closed
        await current.release()
        await upstream.shutdown()
        assert clients[1].is_closed

    asyncio.run(exercise())


def test_runtime_endpoint_descriptor_cannot_redirect_the_bff():
    path = runtime_endpoints.descriptor_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"kind": "uds", "path": "/tmp/untrusted-runtime.sock"}),
        encoding="utf-8",
    )
    try:
        try:
            runtime_endpoints.read_app_endpoint()
        except runtime_endpoints.RuntimeEndpointError:
            pass
        else:
            raise AssertionError("arbitrary runtime UDS descriptor was accepted")
    finally:
        path.unlink(missing_ok=True)


def test_windows_runtime_port_is_stable_and_unprivileged():
    import runtime_cli

    port = runtime_cli._WINDOWS_RUNTIME_PORT_BASE + (
        int(runtime_cli.runtime_ipc.home_digest(), 16)
        % runtime_cli._WINDOWS_RUNTIME_PORT_COUNT
    )
    assert 49152 <= port <= 65534
    assert port == runtime_cli._WINDOWS_RUNTIME_PORT_BASE + (
        int(runtime_cli.runtime_ipc.home_digest(), 16)
        % runtime_cli._WINDOWS_RUNTIME_PORT_COUNT
    )


def test_run_sh_launches_runtime_on_uds_only():
    """run.sh must launch main:app on the per-home unix socket (IPC, no
    network listener) and publish a uds app-endpoint descriptor — the
    launcher must never give the runtime a TCP --host/--port binding."""
    run_sh = (_BACKEND_DIR.parent / "run.sh").read_text(encoding="utf-8")
    launch_lines = [
        line
        for line in run_sh.splitlines()
        if "uvicorn main:app" in line and not line.lstrip().startswith("#")
    ]
    assert launch_lines, "run.sh no longer launches uvicorn main:app"
    for line in launch_lines:
        assert "--uds" in line, f"runtime launch is not UDS: {line.strip()}"
        assert "--host" not in line and "--port" not in line, (
            f"runtime launch binds TCP: {line.strip()}"
        )
    assert "'kind': 'uds'" in run_sh, "run.sh does not publish a uds descriptor"
    assert "'kind': 'tcp'" not in run_sh, "run.sh still publishes a tcp descriptor"
    assert "RUNTIME_PORT" not in run_sh, "run.sh still wires a runtime TCP port"


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
        bff_service_headers = {
            "X-Better-Agent-BFF-Token": (
                Path(home) / "runtime" / "bff-service.token"
            ).read_text(encoding="utf-8").strip()
        }

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

        project_path = str((Path(home) / "project-owned-by-bff").resolve())
        status, _body = runtime_endpoints.http_request(
            descriptor, "GET", "/api/projects", headers=auth_headers,
        )
        assert status == 404
        status, body = runtime_endpoints.http_request(
            {"kind": "tcp", "host": "127.0.0.1", "port": bff_port},
            "POST", "/api/projects",
            body=json.dumps({"path": project_path}).encode(),
            headers={**auth_headers, "Content-Type": "application/json"},
        )
        assert status == 200, body
        status, body = runtime_endpoints.http_request(
            {"kind": "tcp", "host": "127.0.0.1", "port": bff_port},
            "GET", "/api/projects", headers=auth_headers,
        )
        assert status == 200
        project_rows = json.loads(body)["projects"]
        assert project_path in {item["path"] for item in project_rows}, project_rows
        catalog = json.loads(
            (Path(home) / "runtime" / "project-catalog.json").read_text(
                encoding="utf-8"
            )
        )
        assert project_path in {item["path"] for item in catalog["projects"]}
        status, _body = runtime_endpoints.http_request(
            descriptor, "POST", "/api/sessions", body=b"{}",
            headers={**auth_headers, "Content-Type": "application/json"},
        )
        assert status == 405
        status, _body = runtime_endpoints.http_request(
            descriptor, "POST", "/api/bff-runtime/sessions", body=b"{}",
            headers={**auth_headers, "Content-Type": "application/json"},
        )
        assert status == 403

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

        selection_body = json.dumps({
            "selected_project": {"path": "/tmp/bff-project", "node_id": "primary"},
        }).encode()
        status, _body = runtime_endpoints.http_request(
            {"kind": "tcp", "host": "127.0.0.1", "port": bff_port},
            "PATCH", "/api/ui-selection", body=selection_body,
            headers={"Content-Type": "application/json"},
        )
        assert status == 401
        status, body = runtime_endpoints.http_request(
            {"kind": "tcp", "host": "127.0.0.1", "port": bff_port},
            "PATCH", "/api/ui-selection", body=selection_body,
            headers={**auth_headers, "Content-Type": "application/json"},
        )
        assert status == 200, body
        assert json.loads(body)["selected_project"] == {
            "path": "/tmp/bff-project",
            "node_id": "primary",
        }
        status, _body = runtime_endpoints.http_request(
            descriptor, "GET", "/api/ui-selection", headers=auth_headers,
        )
        assert status == 404

        prefs_patch = json.dumps({
            "font_size": 16,
            "send_mode": "interrupt",
        }).encode()
        status, body = runtime_endpoints.http_request(
            {"kind": "tcp", "host": "127.0.0.1", "port": bff_port},
            "PATCH", "/api/user-prefs", body=prefs_patch,
            headers={**auth_headers, "Content-Type": "application/json"},
        )
        assert status == 200, body
        assert json.loads(body)["font_size"] == 16
        assert json.loads(body)["send_mode"] == "interrupt"
        status, _body = runtime_endpoints.http_request(
            descriptor, "GET", "/api/user-prefs", headers=auth_headers,
        )
        assert status == 404
        status, _body = runtime_endpoints.http_request(
            descriptor, "GET", "/api/bff-runtime/preferences", headers=auth_headers,
        )
        assert status == 403
        status, body = runtime_endpoints.http_request(
            descriptor, "GET", "/api/bff-runtime/preferences", headers=bff_service_headers,
        )
        assert status == 200 and json.loads(body)["send_mode"] == "interrupt", (status, body)
        status, _body = runtime_endpoints.http_request(
            {"kind": "tcp", "host": "127.0.0.1", "port": bff_port},
            "GET", "/api/bff-runtime/preferences", headers=auth_headers,
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
        shutil.rmtree(home, ignore_errors=True)
        shutil.rmtree(dist, ignore_errors=True)


def _tcp_alive(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def test_stack_restarts_runtime_without_restarting_bff():
    home = tempfile.mkdtemp(prefix="ba-stack-restart-")
    port = _free_port()
    stack = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "runtime_cli",
            "start-stack",
            "--port",
            str(port),
        ],
        cwd=str(_BACKEND_DIR),
        env=_env(home),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    lock_path = Path(home) / "backend.lock"

    def healthy() -> bool:
        try:
            status, body = _http_get_tcp(port, "/bff/healthz", timeout=2.0)
            return status == 200 and bool(json.loads(body).get("runtime"))
        except (OSError, json.JSONDecodeError):
            return False

    def runtime_pid() -> int | None:
        try:
            for line in lock_path.read_text(encoding="utf-8").splitlines():
                if line.startswith("pid="):
                    return int(line.split("=", 1)[1])
        except (OSError, ValueError):
            return None
        return None

    try:
        assert _wait(healthy, 60), "stack never became healthy"
        first_runtime_pid = runtime_pid()
        assert first_runtime_pid is not None
        os.kill(first_runtime_pid, signal.SIGTERM)
        assert _wait(
            lambda: runtime_pid() not in (None, first_runtime_pid),
            60,
        ), "runtime was not replaced"
        assert stack.poll() is None
        assert _wait(healthy, 60), "same BFF did not reconnect to replacement runtime"
    finally:
        if stack.poll() is None:
            stack.terminate()
            stack.wait(timeout=30)
        assert _wait(
            lambda: not _tcp_alive(port),
            10,
        ), "stack shutdown left the BFF listening"
        shutil.rmtree(home, ignore_errors=True)


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
