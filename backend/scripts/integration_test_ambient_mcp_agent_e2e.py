#!/usr/bin/env python3
"""E2e proof that ambient_native MCPs are actually served and callable.

Two layers against ONE self-booted real backend in an isolated test home,
with the real shipped `extensions/coordination` extension installed and
granted globally:

1. Ambient (session-less) serving — always runs, no LLM cost. Resolves the
   launcher stub through the production `native_mcp_launcher_server_configs`
   funnel with ambient inputs (empty app_session_id), spawns it as a real
   MCP subprocess, and completes a real `tools/call lock_ops(op=list_owned)`
   against the live backend. Unlike test_ambient_mcp_local_dispatch.py this
   never skips for lack of a backend — it boots its own.

2. Real agent turns — opt-in via RUN_LLM_TESTS=1 (cheap models only). For
   each installed provider CLI (claude / codex / agy) a real orchestrated
   turn is run through `prepare_and_start_run`; the model is instructed to
   call `lock_ops` and the session render tree is asserted to contain the
   successful mcp tool call. Providers whose CLI is absent are skipped.

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

EXTENSION_ID = "ofek-dev.coordination"
SERVER_ID = "better-agent-coordination"

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


def _install_and_grant_coordination() -> None:
    import extension_store

    package_dir = _REPO / "extensions" / "coordination"
    extension_store._install_from_package_dir(
        package_dir=package_dir,
        source={"kind": "path", "path": str(package_dir)},
        force_enabled=True,
        persist=True,
    )
    extension_store.grant_native_mcp_server(EXTENSION_ID, SERVER_ID, "global")


# ---------------------------------------------------------------------------
# Layer 1: ambient (session-less) serving via the production launcher funnel
# ---------------------------------------------------------------------------

def _ambient_launcher_item(backend_url: str) -> dict:
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
    configs = extension_store.native_mcp_launcher_server_configs(
        inputs, user_facing=False, bare=False
    )
    for name, item in configs.items():
        if "coordination" in name:
            return item
    raise AssertionError(
        f"ambient resolution did not admit the coordination server; got {sorted(configs)}"
    )


def _mcp_rpc_calls(command: list[str], env: dict[str, str], calls: list[dict]) -> list[dict]:
    """One MCP subprocess session: initialize, then run each tools/call in order.

    Returns the parsed tool-result payload (the JSON body of content[0].text)
    per call; a failed round trip yields {}.
    """
    proc = subprocess.Popen(
        command,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env, text=True, bufsize=1,
    )
    try:
        def send(msg: dict) -> None:
            proc.stdin.write(json.dumps(msg) + "\n")
            proc.stdin.flush()

        def request(msg: dict) -> dict | None:
            send(msg)
            ready, _, _ = select.select([proc.stdout], [], [], 30)
            line = proc.stdout.readline() if ready else None
            return json.loads(line) if line else None

        request({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "ambient-e2e", "version": "0"},
            },
        })
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        payloads: list[dict] = []
        for index, call in enumerate(calls):
            response = request({
                "jsonrpc": "2.0", "id": 2 + index, "method": "tools/call", "params": call,
            })
            result = (response or {}).get("result") or {}
            text = str((result.get("content") or [{}])[0].get("text", ""))
            try:
                payloads.append(json.loads(text) if not result.get("isError") else {})
            except ValueError:
                payloads.append({})
        return payloads
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


def test_ambient_sessionless_tools_call(backend_url: str) -> bool:
    """Acquire + release a lock through the ambient launcher stub — key-based
    lock ops are the coordination ops that legitimately work session-less
    (owner-based ops like list_owned require a trusted runner identity)."""
    item = _ambient_launcher_item(backend_url)
    command = [item["command"], *item.get("args", [])]
    env = {**os.environ, **(item.get("env") or {}), "BETTER_CLAUDE_BACKEND_URL": backend_url}
    key = f"ambient-e2e-sessionless-{uuid.uuid4().hex[:8]}"
    acquired = _mcp_rpc_calls(
        command, env,
        [{"name": "lock_ops", "arguments": {"key": key, "lease_seconds": 120}}],
    )[0]
    holder_token = str(acquired.get("holder_token") or "")
    released: dict = {}
    if holder_token:
        released = _mcp_rpc_calls(
            command, env,
            [{"name": "lock_ops", "arguments": {
                "key": key, "release": True, "holder_token": holder_token,
            }}],
        )[0]
    ok = bool(acquired.get("success")) and bool(released.get("success"))
    print(f"{OK if ok else FAIL} ambient session-less lock_ops acquire+release round trip "
          f"succeeds via launcher stub (acquired={acquired} released={released})")
    return ok


# ---------------------------------------------------------------------------
# Layer 2: real agent turn per provider (opt-in, cheap models)
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
    _install_and_grant_coordination()

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
        results = [test_ambient_sessionless_tools_call(backend_url)]

        from live_llm_test_guard import require_live_llm_tests

        if require_live_llm_tests("ambient MCP real-agent e2e turns"):
            from provider_agy import AgyProvider
            from provider_claude import ClaudeProvider
            from provider_codex import CodexProvider

            import config_store

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
