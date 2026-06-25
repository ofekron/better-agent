#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import _test_home  # noqa: E402

_TMP_HOME = Path(_test_home.isolate("ba-mcp-restart-survival-"))

import builtin_mcp_config  # noqa: E402
import communicate_mcp  # noqa: E402
import extension_jobs  # noqa: E402
import extension_store  # noqa: E402
import main as backend_main  # noqa: E402


FAILURES: list[str] = []


def check(condition: bool, message: str) -> None:
    print(f"  {'✓' if condition else '✗'} {message}")
    if not condition:
        FAILURES.append(message)


class _ReusableHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True


class _FakeBackend:
    def __init__(self) -> None:
        self.generation = 0
        self.port = 0
        self.requests: list[dict[str, Any]] = []
        self._server: _ReusableHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self, generation: int) -> None:
        self.generation = generation
        handler = self._handler()
        self._server = _ReusableHTTPServer(("127.0.0.1", self.port), handler)
        self.port = int(self._server.server_address[1])
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def restart(self, generation: int) -> None:
        self.stop()
        self.start(generation)

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._server = None
        self._thread = None

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or "0")
                raw = self.rfile.read(length)
                try:
                    payload = json.loads(raw.decode("utf-8") or "{}")
                except json.JSONDecodeError:
                    payload = {}
                token = self.headers.get("X-Internal-Token") or ""
                owner.requests.append({
                    "generation": owner.generation,
                    "path": self.path,
                    "token": token,
                    "payload": payload,
                })
                if self.path == "/api/internal/mssg" and payload.get("_mcp_job_id"):
                    body = json.dumps({
                        "success": True,
                        "id": payload["_mcp_job_id"],
                        "status": "running",
                        "ready": False,
                    }).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if self.path == "/api/internal/mcp-jobs/results":
                    body = json.dumps({
                        "success": True,
                        "id": payload.get("id"),
                        "status": "complete",
                        "ready": True,
                        "result": {
                            "success": True,
                            "message_id": "durable-message",
                            "generation": owner.generation,
                        },
                    }).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                body = json.dumps({
                    "success": True,
                    "generation": owner.generation,
                    "path": self.path,
                    "payload": payload,
                    "token_present": bool(token),
                }).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *_args: Any) -> None:
                return

        return Handler


class _McpSession:
    def __init__(self, config: dict[str, Any]) -> None:
        self._config = config
        self._proc: asyncio.subprocess.Process | None = None
        self._next_id = 1

    async def __aenter__(self) -> "_McpSession":
        command = str(self._config.get("command") or "")
        args = [str(arg) for arg in self._config.get("args") or []]
        env = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONIOENCODING": "utf-8",
            **{str(k): str(v) for k, v in (self._config.get("env") or {}).items()},
        }
        self._proc = await asyncio.create_subprocess_exec(
            command,
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            limit=1024 * 1024,
        )
        try:
            await self._request("initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "mcp-restart-survival-test", "version": "1"},
            })
            await self._send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
            return self
        except BaseException:
            await self._close()
            raise

    async def __aexit__(self, _exc_type, _exc, _tb) -> None:
        await self._close()

    async def _close(self) -> None:
        if self._proc is None:
            return
        if self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                self._proc.kill()
                await self._proc.wait()
        self._proc = None

    async def list_tools(self) -> list[dict[str, Any]]:
        result = await self._request("tools/list", {})
        return [tool for tool in result.get("tools") or [] if isinstance(tool, dict)]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self._request("tools/call", {"name": name, "arguments": arguments})

    async def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        await self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        return await self._read_response(request_id)

    async def _send(self, payload: dict[str, Any]) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        self._proc.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        await self._proc.stdin.drain()

    async def _read_response(self, request_id: int) -> dict[str, Any]:
        assert self._proc is not None and self._proc.stdout is not None
        while True:
            line = await asyncio.wait_for(self._proc.stdout.readline(), timeout=10.0)
            if not line:
                stderr = b""
                if self._proc.stderr is not None:
                    try:
                        stderr = await asyncio.wait_for(self._proc.stderr.read(), timeout=0.2)
                    except asyncio.TimeoutError:
                        stderr = b""
                raise RuntimeError(f"MCP server closed stdout: {stderr.decode('utf-8', 'replace')}")
            response = json.loads(line.decode("utf-8", "replace"))
            if response.get("id") != request_id:
                continue
            if response.get("error"):
                raise RuntimeError(json.dumps(response["error"], ensure_ascii=False))
            return response.get("result") or {}


def _tool_payload(result: dict[str, Any]) -> dict[str, Any]:
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    for item in result.get("content") or []:
        if not isinstance(item, dict) or item.get("type") != "text":
            continue
        text = str(item.get("text") or "")
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(decoded, dict):
            return decoded
    return result


def _write_extension_package(extension_id: str, server_name: str) -> Path:
    package = _TMP_HOME / f"{extension_id}-package"
    server_dir = package / "mcp"
    server_dir.mkdir(parents=True, exist_ok=True)
    (server_dir / "server.py").write_text(
        """
from __future__ import annotations

import json
import os
import sys
import urllib.request

from mcp.server.fastmcp import FastMCP


def _post(marker: str) -> dict:
    backend_url = os.environ["BETTER_CLAUDE_BACKEND_URL"].rstrip("/")
    req = urllib.request.Request(
        backend_url + "/api/internal/restart-survival",
        data=json.dumps({
            "marker": marker,
            "app_session_id": os.environ.get("BETTER_CLAUDE_APP_SESSION_ID", ""),
            "extension_id": os.environ.get("BETTER_CLAUDE_EXTENSION_ID", ""),
        }).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Internal-Token": os.environ.get("BETTER_CLAUDE_INTERNAL_TOKEN", ""),
        },
    )
    with urllib.request.urlopen(req, timeout=10.0) as resp:
        return json.loads(resp.read().decode("utf-8"))


server = FastMCP("restart-survival")


@server.tool()
def restart_survival(marker: str) -> dict:
    return _post(marker)


if __name__ == "__main__":
    server.run("stdio")
""".lstrip(),
        encoding="utf-8",
    )
    manifest = extension_store.validate_manifest({
        "kind": extension_store.MANIFEST_KIND,
        "id": extension_id,
        "name": extension_id,
        "version": "1.0.0",
        "description": "Restart survival test extension",
        "surfaces": ["runtime_mcp"],
        "entrypoints": {
            "mcp": [{
                "name": server_name,
                "python": "mcp/server.py",
                "args": [],
                "env": {},
                "user_facing": False,
                "bare_allowed": False,
                "requires_backend_auth": True,
            }]
        },
        "permissions": {"internal_loopback": True},
        "marketplace": {},
        "protocol": {
            "version": 1,
            "smoke_test": {
                "required_paths": ["better-agent-extension.json", "mcp/server.py"],
                "python_modules": ["mcp.server"],
            },
        },
    })
    (package / "better-agent-extension.json").write_text(json.dumps(manifest), encoding="utf-8")
    return package


def _install_extension(extension_id: str, server_name: str) -> None:
    package = _write_extension_package(extension_id, server_name)
    data = extension_store._load()  # type: ignore[attr-defined]
    manifest = json.loads((package / "better-agent-extension.json").read_text(encoding="utf-8"))
    data["extensions"][extension_id] = {
        "manifest": manifest,
        "enabled": True,
        "installed_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "source": {
            "type": "git",
            "repo_url": "https://example.test/restart-survival.git",
            "extension_path": f"extensions/{extension_id}",
            "ref": "",
            "commit_sha": f"{extension_id}-sha",
            "install_path": str(package),
        },
        "entitlement": {
            "status": "not_required",
            "product_id": "",
            "token_present": False,
            "last_checked_at": "",
            "expires_at": "",
        },
    }
    extension_store._save(data)  # type: ignore[attr-defined]


def _run_inputs(backend: _FakeBackend) -> dict[str, Any]:
    return {
        "open_file_panel_enabled": True,
        "app_session_id": "restart-survival-sid",
        "backend_url": backend.url,
        "internal_token": "run-token",
        "mode": "native",
        "cwd": str(ROOT.parent),
        "model": "m",
        "provider_id": "provider-restart-survival",
    }


async def _assert_process_survives_restart(
    label: str,
    config: dict[str, Any],
    tool_name: str,
    first_args: dict[str, Any],
    second_args: dict[str, Any],
    backend: _FakeBackend,
) -> None:
    async with _McpSession(config) as session:
        tools = await session.list_tools()
        check(tool_name in {str(tool.get("name") or "") for tool in tools}, f"{label} exposes {tool_name}")
        first = _tool_payload(await session.call_tool(tool_name, first_args))
        check(first.get("generation") == 1, f"{label} calls fake backend before restart")
        backend.restart(2)
        second = _tool_payload(await session.call_tool(tool_name, second_args))
        check(second.get("generation") == 2, f"{label} same MCP process calls fake backend after restart")
        check(second.get("token_present") is True, f"{label} sends auth after restart")


async def test_core_mcp_process_survives_backend_restart() -> None:
    backend = _FakeBackend()
    try:
        backend.start(1)
        inputs = _run_inputs(backend)
        config = builtin_mcp_config.with_builtin_mcp_servers(inputs, {})["mcp_servers"]["capabilities"]
        await _assert_process_survives_restart(
            "core capabilities MCP",
            config,
            "list_capabilities",
            {},
            {},
            backend,
        )
    finally:
        backend.stop()


async def test_runtime_extension_mcp_process_survives_backend_restart() -> None:
    backend = _FakeBackend()
    try:
        backend.start(1)
        extension_id = "ofek.restart-runtime"
        server_name = "restart-runtime"
        _install_extension(extension_id, server_name)
        config = extension_store.runtime_mcp_server_configs(
            _run_inputs(backend),
            user_facing=False,
            bare=False,
        )[server_name]
        await _assert_process_survives_restart(
            "runtime extension MCP",
            config,
            "restart_survival",
            {"marker": "before"},
            {"marker": "after"},
            backend,
        )
    finally:
        backend.stop()


async def test_session_bound_extension_mcp_is_not_ambient_native() -> None:
    backend = _FakeBackend()
    try:
        backend.start(1)
        extension_id = "ofek.restart-native"
        server_name = "restart-native"
        _install_extension(extension_id, server_name)
        inputs = _run_inputs(backend)
        native_configs = extension_store.native_mcp_launcher_server_configs(
            inputs,
            user_facing=False,
            bare=False,
        )
        check(server_name not in native_configs, "session-bound MCP remains unavailable ambiently")
    finally:
        backend.stop()


async def test_communicate_mcp_uses_durable_job_polling() -> None:
    backend = _FakeBackend()
    previous = {
        key: os.environ.get(key)
        for key in (
            "BETTER_CLAUDE_BACKEND_URL",
            "BETTER_AGENT_BACKEND_URL",
            "BETTER_CLAUDE_INTERNAL_TOKEN",
            "BETTER_AGENT_INTERNAL_TOKEN",
            "BETTER_CLAUDE_MSSG_SENDER_SESSION_ID",
            "BETTER_AGENT_MSSG_SENDER_SESSION_ID",
        )
    }
    try:
        backend.start(1)
        os.environ["BETTER_CLAUDE_BACKEND_URL"] = backend.url
        os.environ["BETTER_AGENT_BACKEND_URL"] = backend.url
        os.environ["BETTER_CLAUDE_INTERNAL_TOKEN"] = "run-token"
        os.environ["BETTER_AGENT_INTERNAL_TOKEN"] = "run-token"
        os.environ["BETTER_CLAUDE_MSSG_SENDER_SESSION_ID"] = "sender-sid"
        os.environ["BETTER_AGENT_MSSG_SENDER_SESSION_ID"] = "sender-sid"
        result = await asyncio.to_thread(
            communicate_mcp.mssg_response,
            "hello",
            target_session_id="target-sid",
        )
        check(result.get("message_id") == "durable-message", "communicate mssg unwraps durable job result")
        paths = [request["path"] for request in backend.requests]
        check(paths[:2] == ["/api/internal/mssg", "/api/internal/mcp-jobs/results"], "communicate mssg fires then polls")
        check("_mcp_job_id" in backend.requests[0]["payload"], "communicate mssg sends durable job id")
    finally:
        backend.stop()
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_session_bridge_grants_durable_polling() -> None:
    manifest = json.loads((ROOT.parent / "extensions/session-bridge/better-agent-extension.json").read_text(encoding="utf-8"))
    grants = ((manifest.get("permissions") or {}).get("capabilities") or [])
    check("core.mcp-jobs.results" in grants, "session-bridge grants durable MCP job polling")


def test_stale_nonresumable_core_mcp_job_fails_closed() -> None:
    job_id = "mcp_stale_mssg"
    extension_jobs.persist_running(
        "core-mcp",
        "mssg",
        job_id,
        phase="running",
        message="MCP job is running",
    )
    response = TestClient(backend_main.app).post(
        "/api/internal/mcp-jobs/results",
        headers={"X-Internal-Token": backend_main.coordinator.internal_token},
        json={"operation": "mssg", "id": job_id, "_mcp_job_wait": 0},
    )
    check(response.status_code == 200, "stale mssg job status endpoint responds")
    payload = response.json()
    check(payload.get("success") is False, "stale mssg job fails closed")
    check("cannot be resumed" in str(payload.get("error") or ""), "stale mssg job is not replayed")


async def main_async() -> None:
    await test_core_mcp_process_survives_backend_restart()
    await test_runtime_extension_mcp_process_survives_backend_restart()
    await test_session_bound_extension_mcp_is_not_ambient_native()
    await test_communicate_mcp_uses_durable_job_polling()
    test_session_bridge_grants_durable_polling()
    test_stale_nonresumable_core_mcp_job_fails_closed()


def main() -> int:
    try:
        asyncio.run(main_async())
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
    if FAILURES:
        print(f"\nFAILED: {len(FAILURES)} assertion(s)")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
