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


def _rpc_roundtrip(env: dict[str, str], call: dict) -> dict | None:
    proc = subprocess.Popen(
        [sys.executable, os.path.join(_BACKEND, "core_ambient_mcp_launcher.py"), "capabilities"],
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
        if not ready:
            return None
        proc.stdout.readline()
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": call})
        ready, _, _ = select.select([proc.stdout], [], [], 15)
        line = proc.stdout.readline() if ready else None
        return json.loads(line) if line else None
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


if __name__ == "__main__":
    results = [
        fn()
        for name, fn in sorted(globals().items())
        if name.startswith("test_") and callable(fn)
    ]
    print(f"\n{sum(1 for r in results if r)}/{len(results)} ambient MCP local-dispatch tests passed")
    raise SystemExit(0 if all(results) else 1)
