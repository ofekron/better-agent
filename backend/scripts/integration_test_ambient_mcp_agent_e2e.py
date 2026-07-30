#!/usr/bin/env python3
"""E2e proof that ambient_native MCPs are actually served and callable.

Three layers against ONE self-booted real backend in an isolated test home.
Every shipped extension declaring an ambient_native MCP entrypoint is
discovered from `extensions/*/better-agent-extension.json`, installed, and
granted globally — a newly shipped ambient-eligible extension is covered
automatically, with no test change.

1. Ambient (session-less) serving for ALL shipped eligibles — always runs,
   no LLM cost. Each eligible must resolve through the production
   `native_mcp_launcher_server_configs` funnel with ambient inputs (empty
   app_session_id) and answer a real `initialize` + `tools/list` as a
   spawned MCP subprocess. The coordination server additionally completes a
   real `tools/call` lock_ops acquire+release round trip. Unlike
   test_ambient_mcp_local_dispatch.py this never skips for lack of a
   backend — it boots its own.

2. Core ambient servers (`capabilities`, `ui`) via
   `core_ambient_mcp_launcher.py` — always runs. capabilities completes a
   real `tools/call list_capabilities` against a real session; ui's ambient
   tool list must stay narrowed to open_file_panel.

3. Real agent turns — opt-in via RUN_LLM_TESTS=1 (cheap models only). For
   each installed provider CLI (claude / codex / agy) a real orchestrated
   turn is run through `prepare_and_start_run`; the model is instructed to
   call `lock_ops` and the acquired key is asserted in the coordination
   lock store. Providers whose CLI is absent are skipped.

Run with:
    cd backend && .venv/bin/python scripts/integration_test_ambient_mcp_agent_e2e.py
    RUN_LLM_TESTS=1 .venv/bin/python scripts/integration_test_ambient_mcp_agent_e2e.py
"""
from __future__ import annotations

import asyncio
import json
import os
import select
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from urllib.request import urlopen

_HERE = Path(__file__).resolve().parent
_BACKEND = _HERE.parent
_REPO = _BACKEND.parent
sys.path.insert(0, str(_BACKEND))
os.environ["PYTHONPATH"] = os.pathsep.join(
    [str(_REPO / "sdk"), str(_BACKEND)]
    + ([os.environ["PYTHONPATH"]] if os.environ.get("PYTHONPATH") else [])
)
sys.path.insert(0, str(_REPO / "sdk"))

OK = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"
SKIP = "\033[93mSKIP\033[0m"

COORDINATION_SERVER_ID = "better-agent-coordination"

CHEAP_MODELS = {
    "claude": os.environ.get("BA_AMBIENT_E2E_CLAUDE_MODEL", "claude-haiku-4-5-20251001"),
    "codex": os.environ.get("BA_AMBIENT_E2E_CODEX_MODEL", "gpt-5.4-mini"),
    "agy": os.environ.get("BA_AMBIENT_E2E_AGY_MODEL", "Gemini 3.5 Flash (Low)"),
}


def _agent_prompt(lock_key: str) -> str:
    return (
        "This is an integration test for ambient MCP serving. You MUST call the "
        "available MCP tool named lock_ops exactly once, with arguments "
        f'{{"key": "{lock_key}", "lease_seconds": 600}}. If you need to search '
        "for the tool, search for lock_ops or coordination. After the tool "
        "call, reply only with: done"
    )


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_server(url: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=1.0) as response:
                if response.status < 500:
                    return
        except Exception as exc:
            last_error = exc
        time.sleep(0.1)
    raise RuntimeError(f"backend did not become ready: {last_error}")


# ---------------------------------------------------------------------------
# Eligible discovery + install
# ---------------------------------------------------------------------------

def _shipped_ambient_entrypoints() -> list[dict]:
    """Every mcp entrypoint declaring ambient_native across shipped extensions."""
    found: list[dict] = []
    for manifest_path in sorted((_REPO / "extensions").glob("*/better-agent-extension.json")):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for item in (manifest.get("entrypoints") or {}).get("mcp") or []:
            if not item.get("ambient_native"):
                continue
            found.append({
                "package_dir": manifest_path.parent,
                "extension_id": manifest["id"],
                "server_name": item["name"],
                "server_id": item.get("replaces_builtin") or item["name"],
            })
    return found


def _install_and_grant(eligibles: list[dict]) -> None:
    import extension_store

    for entry in {e["extension_id"]: e for e in eligibles}.values():
        extension_store._install_from_package_dir(
            package_dir=entry["package_dir"],
            source={"kind": "path", "path": str(entry["package_dir"])},
            force_enabled=True,
            persist=True,
        )
    for entry in eligibles:
        extension_store.grant_native_mcp_server(
            entry["extension_id"], entry["server_id"], "global"
        )


# ---------------------------------------------------------------------------
# MCP stdio session helper
# ---------------------------------------------------------------------------

class _McpSession:
    def __init__(self, command: list[str], env: dict[str, str]) -> None:
        self._proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, text=True, bufsize=1,
        )
        self._next_id = 1
        self._request({
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "ambient-e2e", "version": "0"},
            },
        })
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _send(self, msg: dict) -> None:
        self._proc.stdin.write(json.dumps(msg) + "\n")
        self._proc.stdin.flush()

    def _request(self, msg: dict) -> dict | None:
        self._send({"jsonrpc": "2.0", "id": self._next_id, **msg})
        self._next_id += 1
        ready, _, _ = select.select([self._proc.stdout], [], [], 30)
        line = self._proc.stdout.readline() if ready else None
        return json.loads(line) if line else None

    def list_tools(self) -> list[str]:
        response = self._request({"method": "tools/list", "params": {}})
        tools = ((response or {}).get("result") or {}).get("tools") or []
        return sorted(str(tool.get("name") or "") for tool in tools)

    def call(self, name: str, arguments: dict) -> tuple[bool, str]:
        """Returns (succeeded, content text of the first result item)."""
        response = self._request({
            "method": "tools/call", "params": {"name": name, "arguments": arguments},
        })
        result = (response or {}).get("result") or {}
        text = str((result.get("content") or [{}])[0].get("text", ""))
        return bool(response) and not result.get("isError"), text

    def close(self) -> None:
        self._proc.terminate()
        try:
            self._proc.wait(timeout=5)
        except Exception:
            self._proc.kill()


def _launcher_command_env(item: dict, backend_url: str) -> tuple[list[str], dict[str, str]]:
    command = [item["command"], *item.get("args", [])]
    env = {**os.environ, **(item.get("env") or {}), "BETTER_CLAUDE_BACKEND_URL": backend_url}
    return command, env


# ---------------------------------------------------------------------------
# Layer 1: ambient (session-less) serving of ALL shipped eligibles
# ---------------------------------------------------------------------------

def _ambient_launcher_configs(backend_url: str) -> dict:
    import extension_store

    inputs = {
        "app_session_id": "",
        "backend_url": backend_url,
        "internal_token": "",
        "cwd": "/tmp",
        "user_facing": False,
        "bare_config": False,
        "extension_mcp_launcher_context": True,
    }
    return extension_store.native_mcp_launcher_server_configs(
        inputs, user_facing=False, bare=False
    )


def test_all_shipped_eligibles_serve_ambiently(backend_url: str, eligibles: list[dict]) -> bool:
    configs = _ambient_launcher_configs(backend_url)
    all_ok = True
    for entry in eligibles:
        server_id = entry["server_id"]
        item = configs.get(server_id)
        if item is None:
            print(f"{FAIL} {server_id}: declared ambient_native but ambient resolution "
                  f"did not admit it (got {sorted(configs)})")
            all_ok = False
            continue
        command, env = _launcher_command_env(item, backend_url)
        session = _McpSession(command, env)
        try:
            tools = session.list_tools()
        finally:
            session.close()
        ok = bool(tools)
        print(f"{OK if ok else FAIL} {server_id}: ambient launcher stub serves "
              f"tools/list session-less (tools={tools})")
        all_ok = all_ok and ok
    return all_ok


def test_coordination_ambient_lock_roundtrip(backend_url: str) -> bool:
    """Key-based lock ops are the coordination ops that legitimately work
    session-less (owner-based ops like list_owned require a trusted runner
    identity) — prove a real tools/call acquire+release round trip."""
    item = _ambient_launcher_configs(backend_url).get(COORDINATION_SERVER_ID)
    if item is None:
        print(f"{FAIL} coordination server missing from ambient resolution")
        return False
    command, env = _launcher_command_env(item, backend_url)
    key = f"ambient-e2e-sessionless-{uuid.uuid4().hex[:8]}"
    session = _McpSession(command, env)
    try:
        acquired_ok, acquired_text = session.call(
            "lock_ops", {"key": key, "lease_seconds": 120}
        )
        acquired = json.loads(acquired_text) if acquired_ok else {}
        holder_token = str(acquired.get("holder_token") or "")
        released: dict = {}
        if holder_token:
            released_ok, released_text = session.call(
                "lock_ops", {"key": key, "release": True, "holder_token": holder_token}
            )
            released = json.loads(released_text) if released_ok else {}
    finally:
        session.close()
    ok = bool(acquired.get("success")) and bool(released.get("success"))
    print(f"{OK if ok else FAIL} coordination: ambient lock_ops acquire+release round "
          f"trip succeeds (acquired={acquired} released={released})")
    return ok


# ---------------------------------------------------------------------------
# Layer 2: core ambient servers via core_ambient_mcp_launcher.py
# ---------------------------------------------------------------------------

def _core_ambient_session(server_name: str, backend_url: str) -> _McpSession:
    command = [sys.executable, str(_BACKEND / "core_ambient_mcp_launcher.py"), server_name]
    env = {**os.environ, "BETTER_CLAUDE_BACKEND_URL": backend_url}
    return _McpSession(command, env)


def test_core_capabilities_ambient_call(backend_url: str, session_id: str) -> bool:
    session = _core_ambient_session("capabilities", backend_url)
    try:
        ok, text = session.call("list_capabilities", {"session_id": session_id})
    finally:
        session.close()
    print(f"{OK if ok else FAIL} core capabilities: ambient list_capabilities tools/call "
          f"succeeds (response={text[:200]})")
    return ok


def test_core_ui_ambient_tool_list_narrowed(backend_url: str) -> bool:
    session = _core_ambient_session("ui", backend_url)
    try:
        tools = session.list_tools()
    finally:
        session.close()
    ok = tools == ["open_file_panel"]
    print(f"{OK if ok else FAIL} core ui: ambient tool list stays narrowed to "
          f"open_file_panel (got {tools})")
    return ok


# ---------------------------------------------------------------------------
# Layer 3: real agent turn per provider (opt-in, cheap models)
# ---------------------------------------------------------------------------

def _lock_key_held(lock_key: str) -> bool:
    """Provider-agnostic proof the agent's lock_ops call reached the backend:
    the acquired key sits in the coordination lock store with its lease."""
    from paths import bc_home

    locks_path = bc_home() / "coordination" / "locks.json"
    if not locks_path.is_file():
        return False
    try:
        store = json.loads(locks_path.read_text(encoding="utf-8"))
    except ValueError:
        return False
    locks = store.get("locks")
    return isinstance(locks, dict) and lock_key in locks


async def _run_agent_turn(provider_name: str, provider_cls, provider_record: dict, main,
                          session_store, port: int) -> bool:
    session = session_store.create_session(
        name=f"ambient e2e {provider_name}",
        model="",
        cwd="/tmp",
        orchestration_mode="native",
        source="cli",
        provider_id=provider_record["id"],
        browser_harness_enabled=False,
    )
    sid = session["id"]
    queue: asyncio.Queue = asyncio.Queue()
    provider = provider_cls(provider_record)
    run_id = f"ambient-e2e-{provider_name}-{uuid.uuid4().hex[:12]}"
    lock_key = f"ambient-e2e-{provider_name}-{uuid.uuid4().hex[:8]}"
    from provider import prepare_and_start_run

    prepare_and_start_run(
        provider,
        run_id=run_id,
        prompt=_agent_prompt(lock_key),
        cwd="/tmp",
        loop=asyncio.get_running_loop(),
        queue=queue,
        model=CHEAP_MODELS[provider_name],
        reasoning_effort=None,
        session_id=None,
        mode="native",
        app_session_id=sid,
        backend_url=f"http://127.0.0.1:{port}",
        internal_token=main.coordinator.internal_token,
        browser_harness_enabled=False,
        user_facing=True,
        provider_run_config={},
        capability_contexts=[],
        setting_sources=[],
    )
    run_dir = provider._runs[run_id].run_dir

    complete = None
    seen: list[str] = []
    deadline = time.monotonic() + 240.0
    while time.monotonic() < deadline:
        timeout = min(5.0, max(0.1, deadline - time.monotonic()))
        try:
            event = await asyncio.wait_for(queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            continue
        seen.append(event.type)
        if event.type == "complete":
            complete = event.data
            break
    if complete is None:
        print(f"{FAIL} {provider_name}: timed out waiting for complete; events={seen} "
              f"run_dir={run_dir}")
        return False
    if complete.get("error"):
        print(f"{FAIL} {provider_name}: runner error: {complete.get('error')} run_dir={run_dir}")
        return False
    if not _lock_key_held(lock_key):
        print(f"{FAIL} {provider_name}: lock {lock_key!r} absent from the coordination "
              f"store — the agent never completed a successful lock_ops call; "
              f"run_dir={run_dir}")
        return False
    print(f"{OK} {provider_name}: real {CHEAP_MODELS[provider_name]} turn acquired "
          f"{lock_key} via the ambient coordination MCP")
    return True


def _available_provider_clis() -> dict[str, str]:
    import cli_paths

    found: dict[str, str] = {}
    for name in ("claude", "codex", "agy"):
        binary = cli_paths.resolve_cli_binary(name, respect_installation_profile=False)
        if binary:
            found[name] = binary
    return found


async def _main(home: Path) -> bool:
    import uvicorn

    import _test_installation
    import config_store
    import main
    import session_store

    clis = _available_provider_clis()
    if not clis:
        raise RuntimeError("no provider CLI (claude/codex/agy) installed; cannot activate "
                           "an installation profile")
    profile_provider = next(iter(clis))
    _test_installation.activate(
        home, provider=profile_provider, launcher_path=clis[profile_provider]
    )
    eligibles = _shipped_ambient_entrypoints()
    if not eligibles:
        raise RuntimeError("no shipped extension declares an ambient_native MCP entrypoint")
    _install_and_grant(eligibles)

    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(main.app, host="127.0.0.1", port=port, log_level="warning",
                       lifespan="on")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        backend_url = f"http://127.0.0.1:{port}"
        _wait_for_server(f"{backend_url}/api/auth/needs_setup")

        provider_record = config_store.get_provider(
            config_store.add_provider({"name": "ambient e2e core", "kind": profile_provider})["id"]
        )
        core_session = session_store.create_session(
            name="ambient e2e core",
            model="",
            cwd="/tmp",
            orchestration_mode="native",
            source="cli",
            provider_id=provider_record["id"],
            browser_harness_enabled=False,
        )

        results = [
            test_all_shipped_eligibles_serve_ambiently(backend_url, eligibles),
            test_coordination_ambient_lock_roundtrip(backend_url),
            test_core_capabilities_ambient_call(backend_url, core_session["id"]),
            test_core_ui_ambient_tool_list_narrowed(backend_url),
        ]

        from live_llm_test_guard import require_live_llm_tests

        if require_live_llm_tests("ambient MCP real-agent e2e turns"):
            from provider_agy import AgyProvider
            from provider_claude import ClaudeProvider
            from provider_codex import CodexProvider

            provider_classes = {
                "claude": ClaudeProvider,
                "codex": CodexProvider,
                "agy": AgyProvider,
            }
            for name, cls in provider_classes.items():
                if name not in clis:
                    print(f"{SKIP} {name}: CLI not installed")
                    continue
                created = config_store.add_provider(
                    {"name": f"ambient e2e {name}", "kind": name}
                )
                record = config_store.get_provider(created["id"])
                results.append(await _run_agent_turn(
                    name, cls, record, main, session_store, port
                ))
        passed = sum(1 for r in results if r)
        print(f"\n{passed}/{len(results)} ambient MCP e2e checks passed")
        return all(results)
    finally:
        server.should_exit = True
        thread.join(timeout=10.0)


if __name__ == "__main__":
    home = Path(tempfile.mkdtemp(prefix="bc-ambient-mcp-e2e-"))
    import paths

    paths.engage_test_home(str(home))
    os.environ["BETTER_CLAUDE_TEST_AUTH_BYPASS"] = "1"
    try:
        success = asyncio.run(_main(home))
    except Exception:
        print(f"FAILED home preserved at {home}")
        raise
    if not success:
        print(f"FAILED home preserved at {home}")
        raise SystemExit(1)
    shutil.rmtree(home, ignore_errors=True)
