"""Locks the `local=True` dispatch fix in `run_mcp_or_cli`/`build_mcp_server`.

An `OperationSpec` with a non-empty `operation=` field is, by default, routed
through the per-run operation broker (`RuntimeTransport` /
`BETTER_CLAUDE_RUNTIME_BROKER`). That broker only exists inside a Better
Agent-orchestrated run -- an ambient (session-less) launch has none, so
every `tools/call` for such a tool failed with "Better Agent runtime broker
is unavailable" even though `initialize`/`tools/list` succeeded (the
handshake working is not evidence the tool works). This locks that
`capabilities`'s ambient launch actually completes a real `tools/call`
against the live backend, not just a resolver-level or handshake-level
check.

Requires a real backend reachable at BETTER_CLAUDE_BACKEND_URL (defaults to
http://localhost:18765, this project's standard dev port) and a real
session id to target, since `list_capabilities` needs one to resolve
against. Skips (not fails) if neither is available, matching how other
live-backend-dependent scripts in this suite behave.

Run with:
    cd backend && .venv/bin/python scripts/test_ambient_mcp_local_dispatch.py
"""
from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import urllib.error
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

OK = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"
SKIP = "\033[93mSKIP\033[0m"

_BACKEND_URL = os.environ.get("BETTER_CLAUDE_BACKEND_URL", "http://localhost:18765")


def _backend_reachable() -> bool:
    try:
        urllib.request.urlopen(_BACKEND_URL + "/api/health", timeout=2.0)
        return True
    except Exception:
        try:
            urllib.request.urlopen(_BACKEND_URL, timeout=2.0)
            return True
        except urllib.error.HTTPError:
            return True  # reachable, just not this exact path
        except Exception:
            return False


def _rpc_session(server_name: str, env: dict[str, str]):
    proc = subprocess.Popen(
        [sys.executable, os.path.join(_BACKEND, "core_ambient_mcp_launcher.py"), server_name],
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

    request({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"},
        },
    })
    send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    return proc, request


def _rpc_roundtrip(env: dict[str, str], call: dict) -> dict | None:
    proc, request = _rpc_session("capabilities", env)
    try:
        return request({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": call})
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


def _first_real_session_id() -> str:
    """`list_capabilities` needs a real session to target. Reads the on-disk
    session store directly (read-only) rather than `session_store.list_sessions()`,
    which serves an in-memory index that a fresh one-off process never warms."""
    import re

    from paths import bc_home

    sessions_dir = bc_home() / "sessions"
    if not sessions_dir.is_dir():
        return ""
    uuid_re = re.compile(r"^[0-9a-f-]{36}\.json$")
    for entry in sorted(sessions_dir.iterdir()):
        if uuid_re.match(entry.name):
            return entry.name[: -len(".json")]
    return ""


def test_ambient_capabilities_call_succeeds_with_local_dispatch() -> bool:
    if not _backend_reachable():
        print(f"{SKIP} no live backend at {_BACKEND_URL}; cannot verify a real tools/call")
        return True
    session_id = _first_real_session_id()
    if not session_id:
        print(f"{SKIP} no existing session to target with list_capabilities")
        return True
    env = {**os.environ}
    response = _rpc_roundtrip(
        env,
        {"name": "list_capabilities", "arguments": {"session_id": session_id}},
    )
    is_error = bool((response or {}).get("result", {}).get("isError"))
    ok = response is not None and not is_error
    print(f"{OK if ok else FAIL} ambient capabilities tools/call succeeds via local dispatch "
          f"(response={response})")
    return ok


def test_without_the_ambient_marker_the_call_hits_the_absent_broker() -> bool:
    """Regression guard: `core_ambient_mcp_launcher.py` always sets
    `BETTER_CLAUDE_AMBIENT_LAUNCH=1`. Spawning `capabilities_mcp.py` directly
    (bypassing the launcher, so the marker is unset) must fail with the
    broker-unavailable error -- proving the marker, not something else,
    is what makes the ambient call above succeed."""
    if not _backend_reachable():
        print(f"{SKIP} no live backend at {_BACKEND_URL}")
        return True
    env = {
        **os.environ,
        "BETTER_CLAUDE_BACKEND_URL": _BACKEND_URL,
        "BETTER_CLAUDE_INTERNAL_TOKEN": "not-a-real-token-this-should-never-be-reached",
    }
    env.pop("BETTER_CLAUDE_AMBIENT_LAUNCH", None)
    env.pop("BETTER_AGENT_AMBIENT_LAUNCH", None)
    proc = subprocess.Popen(
        [sys.executable, os.path.join(_BACKEND, "capabilities_mcp.py")],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env, text=True, bufsize=1,
    )
    try:
        def send(msg: dict) -> None:
            proc.stdin.write(json.dumps(msg) + "\n")
            proc.stdin.flush()

        send({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"},
            },
        })
        ready, _, _ = select.select([proc.stdout], [], [], 15)
        if ready:
            proc.stdout.readline()
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "list_capabilities", "arguments": {"session_id": "irrelevant"}},
        })
        ready, _, _ = select.select([proc.stdout], [], [], 15)
        line = proc.stdout.readline() if ready else None
        response = json.loads(line) if line else None
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
    text = str(((response or {}).get("result") or {}).get("content", [{}])[0].get("text", ""))
    ok = "runtime broker" in text.lower()
    print(f"{OK if ok else FAIL} without the ambient marker, dispatch falls through to the "
          f"(absent) broker as before (response={response})")
    return ok


def test_ambient_ui_tool_list_is_narrowed_to_open_file_panel() -> bool:
    """request_user_input/request_user_approval/start_file_discussion and
    open_file_panel(mode="inline") all attach to an in-flight turn's
    assistant message, which doesn't exist ambiently -- they must not even
    be advertised (see core_ambient_mcp_launcher.py's docstring)."""
    if not _backend_reachable():
        print(f"{SKIP} no live backend at {_BACKEND_URL}")
        return True
    proc, request = _rpc_session("ui", {**os.environ})
    try:
        response = request({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
    names = sorted(
        tool.get("name") for tool in ((response or {}).get("result") or {}).get("tools", [])
    )
    ok = names == ["open_file_panel"]
    print(f"{OK if ok else FAIL} ambient ui server only advertises open_file_panel (got {names})")
    return ok


def test_ambient_open_file_panel_mode_panel_succeeds_and_is_cleaned_up() -> bool:
    """mode='panel' is a plain per-session state mutation, not tied to any
    in-flight turn -- it's the one ui tool that can do real ambient work."""
    if not _backend_reachable():
        print(f"{SKIP} no live backend at {_BACKEND_URL}")
        return True
    session_id = _first_real_session_id()
    if not session_id:
        print(f"{SKIP} no existing session to target with open_file_panel")
        return True
    proc, request = _rpc_session("ui", {**os.environ})
    panel_id = ""
    try:
        response = request({
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {
                "name": "open_file_panel",
                "arguments": {
                    "mode": "panel",
                    "path": "backend/scripts/test_ambient_mcp_local_dispatch.py",
                    "session_id": session_id,
                },
            },
        })
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
    is_error = bool((response or {}).get("result", {}).get("isError"))
    try:
        text = response["result"]["content"][0]["text"]
        body = json.loads(text)
        panel_id = str((body.get("panel") or {}).get("id") or "")
    except Exception:
        body = None
    ok = response is not None and not is_error and bool(panel_id)
    if panel_id:
        from session_manager import manager as session_manager

        session_manager.remove_open_file_panel(session_id, panel_id, client_id=None)
    print(f"{OK if ok else FAIL} ambient open_file_panel(mode='panel') succeeds and is cleaned "
          f"up (response={response})")
    return ok


if __name__ == "__main__":
    results = [
        fn()
        for name, fn in sorted(globals().items())
        if name.startswith("test_") and callable(fn)
    ]
    print(f"\n{sum(1 for r in results if r)}/{len(results)} ambient MCP local-dispatch tests passed")
    raise SystemExit(0 if all(results) else 1)
