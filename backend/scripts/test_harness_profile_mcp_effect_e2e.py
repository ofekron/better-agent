"""Meaningful E2E proof that a harness profile's MCP inclusion/exclusion
actually WORKS — not just that a resolved config dict contains the right
server *names* (already locked by test_session_creating_mcps_harness_profile.py),
but that:

  1. A KEPT server's tool is genuinely present AND functionally callable for
     the profile-scoped session — invoked through its real internal loopback
     route (the same one an LLM's tool call would hit), with a real
     resulting state change verified independently.
  2. A DROPPED server is genuinely absent from the exact production function
     (`extension_store.runtime_mcp_server_configs`) that the real per-provider
     launch path (`provider_runtime_plan_source.py`) calls to decide which
     MCP subprocesses to spawn for a session — i.e. the real runner would
     never hand that tool to the model for this session. That absence IS
     the enforcement boundary: the underlying internal route itself gates
     only on "is this extension active at all", not on any one session's
     profile, so removal is enforced entirely by never wiring the subprocess
     — not by a per-call permission check.

Uses two REAL bundled extensions (not fake fixtures) so the "kept" side can
be driven end-to-end: session-control (kept) and session-bridge (dropped).

Run with:
    cd backend && .venv/bin/python scripts/test_harness_profile_mcp_effect_e2e.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest

import _test_home
_TMP_HOME = _test_home.isolate_installed("bc-test-harness-profile-mcp-effect-")
os.environ["BETTER_CLAUDE_TEST_AUTH_BYPASS"] = "1"

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from fastapi.testclient import TestClient  # noqa: E402

import extension_store  # noqa: E402
import harness_field_writer  # noqa: E402
import harness_fields  # noqa: E402
import harness_profile_resolver  # noqa: E402
import harness_profile_store  # noqa: E402
import installation_profile  # noqa: E402
import main  # noqa: E402
import session_store  # noqa: E402
from scripts.auth_test_helpers import authenticate_client  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402

# Same bypass test_session_control.py uses: only the bundled-extension
# activation gate, not a fake install, decides whether these real
# extensions are considered "active" in an isolated test home.
installation_profile.integrations_enabled = lambda: True


@pytest.fixture(autouse=True)
def _active_runtime_python() -> Any:
    """The test container exposes no managed backend dependency environment, so
    resolve extension runtime launch to this interpreter."""
    original = extension_store.dependency_plan.active_runtime_python
    extension_store.dependency_plan.active_runtime_python = (
        lambda _backend_dir: Path(sys.executable)
    )
    try:
        yield
    finally:
        extension_store.dependency_plan.active_runtime_python = original


PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"

KEPT_EXT = extension_store.BUILTIN_SESSION_CONTROL_EXTENSION_ID
KEPT_SERVER = "better-agent-session-control"
DROPPED_EXT = extension_store.BUILTIN_SESSION_BRIDGE_EXTENSION_ID
DROPPED_SERVER = "better-agent-session-bridge"


def _activate_public_extensions() -> None:
    data = extension_store._load()  # type: ignore[attr-defined]
    extension_store._ensure_public_extensions(data)  # type: ignore[attr-defined]
    extension_store._save(data)  # type: ignore[attr-defined]


def _scoped_profile() -> dict:
    """A profile that keeps session-control's server and drops
    session-bridge's — same mechanism a real user-authored harness profile
    uses (harness_field_writer over extension_instances.*.mcp_servers)."""
    profile = harness_profile_store.create_profile({"name": "MCP effect scoped"})
    harness_field_writer.apply_field_writes(profile["id"], profile["revision"], [{
        "path": ["extension_instances", DROPPED_EXT, harness_fields.GROUP_MCP, DROPPED_SERVER],
        "value": False,
    }])
    return harness_profile_store.get_profile(profile["id"])


def _tok() -> str:
    return main.coordinator.internal_token


def _resolved_server_names(session_id: str) -> set[str]:
    """The exact production call `provider_runtime_plan_source.py` makes to
    decide which MCP subprocesses to actually launch for this session."""
    session = session_manager.get(session_id)
    return set(extension_store.runtime_mcp_server_configs(
        {
            "mode": "native",
            "app_session_id": session_id,
            "working_mode": "native",
            "user_facing": True,
            "backend_url": "http://localhost:8000",
            "internal_token": _tok(),
            "resolved_harness_run_config": harness_profile_resolver.resolve_for_session(session),
        },
        user_facing=True,
        bare=False,
    ))


def _new_native_session(name: str, harness_profile_id: str = "") -> str:
    return session_store.create_session(
        name=name, model="m", cwd="/tmp", orchestration_mode="native",
        harness_profile_id=harness_profile_id,
    )["id"]


# ------------------------------------------------------------------ control
def test_default_session_gets_both_servers_by_default() -> bool:
    """Control: without a scoping profile, both real extensions' servers are
    offered — proves the later exclusion is caused by the profile, not by
    session-bridge being generally unavailable."""
    _activate_public_extensions()
    sid = _new_native_session("default-both")
    names = _resolved_server_names(sid)
    if KEPT_SERVER not in names or DROPPED_SERVER not in names:
        print(f"  control failed — expected both present by default: {names}")
        return False
    return True


# ------------------------------------------------------------------ exclusion
def test_scoped_profile_drops_session_bridge_from_the_real_launch_config() -> bool:
    _activate_public_extensions()
    profile = _scoped_profile()
    sid = _new_native_session("scoped-drop", harness_profile_id=profile["id"])
    names = _resolved_server_names(sid)
    if KEPT_SERVER not in names:
        print(f"  kept server wrongly missing: {names}")
        return False
    if DROPPED_SERVER in names:
        print(f"  dropped server still present in the real launch config: {names}")
        return False
    return True


# ------------------------------------------------------------------ inclusion + it actually works
def test_kept_server_tool_actually_works_for_the_scoped_session(client: TestClient) -> bool:
    """Presence in the resolved config isn't proof enough on its own — call
    the kept tool's real internal route for THIS exact profile-scoped
    session (the same route an LLM's tool call would hit) and verify a real
    state change, mirroring test_session_control.py's own functional check."""
    _activate_public_extensions()
    profile = _scoped_profile()
    sid = _new_native_session("scoped-functional", harness_profile_id=profile["id"])
    if KEPT_SERVER not in _resolved_server_names(sid):
        print("  precondition failed: kept server not offered to this session")
        return False

    r = client.post(
        "/api/internal/session-control/continue-fresh",
        headers={"X-Internal-Token": _tok()},
        json={"app_session_id": sid, "prompt": "keep going"},
    )
    if r.status_code != 200:
        print(f"  continue-fresh failed: {r.status_code} {r.text}")
        return False
    if r.json().get("when") != "next_turn":
        print(f"  unexpected response: {r.json()}")
        return False
    # Independently verify the real resulting state, not just the echoed response.
    req = (session_manager.get(sid) or {}).get("continuation_requested")
    if req != {"prompt": "keep going", "reason": "agent_requested", "when": "next_turn"}:
        print(f"  continuation flag not actually set on the session: {req}")
        return False
    return True


TESTS_NO_CLIENT = [
    ("default session gets both servers (control)", test_default_session_gets_both_servers_by_default),
    ("scoped profile drops session-bridge from the real launch config", test_scoped_profile_drops_session_bridge_from_the_real_launch_config),
]

TESTS_WITH_CLIENT = [
    ("kept server's tool actually works for the scoped session", test_kept_server_tool_actually_works_for_the_scoped_session),
]


def main_run() -> int:
    failed = 0
    for name, fn in TESTS_NO_CLIENT:
        try:
            ok = fn()
        except Exception as e:
            ok = False
            import traceback
            traceback.print_exc()
            print(f"  exception: {e}")
        print(f"{PASS if ok else FAIL}  {name}")
        if not ok:
            failed += 1
    with TestClient(main.app, client=("127.0.0.1", 50000)) as client:
        authenticate_client(client)
        for name, fn in TESTS_WITH_CLIENT:
            try:
                ok = fn(client)
            except Exception as e:
                ok = False
                import traceback
                traceback.print_exc()
                print(f"  exception: {e}")
            print(f"{PASS if ok else FAIL}  {name}")
            if not ok:
                failed += 1
    total = len(TESTS_NO_CLIENT) + len(TESTS_WITH_CLIENT)
    print()
    if failed:
        print(f"{failed} of {total} test(s) FAILED")
    else:
        print(f"all {total} tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main_run())
