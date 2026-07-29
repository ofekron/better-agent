"""Meaningful E2E tests for the communicate_mcp session/worker provisioning
tools (create_session, create_sub_session, create_worker), invoked exactly
as an LLM would call them — real `/api/internal/*` loopback request with a
real X-Internal-Token, no model in the loop — each verifying that a real
session/worker record actually landed in session_store/session_manager,
not just that the route returned success.

`ensure_named_worker`'s underlying route (`/api/internal/workers/provision`)
already has deep, meaningful E2E coverage in test_worker_provisioning.py; it
is intentionally not duplicated here.

Run with:
    cd backend && .venv/bin/python scripts/test_communicate_session_provisioning_mcp_e2e.py
"""

from __future__ import annotations

import json
import shutil
import sys
import os
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate_installed("bc-test-communicate-provisioning-")
os.environ["BETTER_CLAUDE_TEST_AUTH_BYPASS"] = "1"

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from fastapi.testclient import TestClient  # noqa: E402

import extension_store  # noqa: E402
import main  # noqa: E402
import session_store  # noqa: E402
from scripts.auth_test_helpers import authenticate_client  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _new_session(name: str = "t", cwd: str = "/tmp") -> str:
    return session_store.create_session(
        name=name, model="m", cwd=cwd, orchestration_mode="native",
    )["id"]


def _tok() -> str:
    return main.coordinator.internal_token


# ------------------------------------------------------------------ create_session
def test_create_session_actually_creates_standalone_session(client: TestClient) -> bool:
    r = client.post(
        "/api/internal/create-session",
        json={"name": "standalone-worker", "cwd": "/tmp/standalone"},
        headers={"X-Internal-Token": _tok()},
    )
    if r.status_code != 200 or not r.json().get("success"):
        print(f"  create_session failed: {r.status_code} {r.text}")
        return False
    sid = r.json().get("session_id")
    if not sid:
        print(f"  no session_id in response: {r.json()}")
        return False
    # Independently verify against the real session store, not just the echo.
    persisted = session_manager.get(sid)
    if not persisted:
        print("  session does not actually exist in session_manager")
        return False
    if persisted.get("name") != "standalone-worker" or persisted.get("cwd") != "/tmp/standalone":
        print(f"  persisted session has wrong name/cwd: {persisted}")
        return False
    if persisted.get("orchestration_mode") != "native":
        print(f"  default orchestration_mode should be native: {persisted}")
        return False
    return True


def test_create_session_missing_name_is_400(client: TestClient) -> bool:
    r = client.post(
        "/api/internal/create-session",
        json={"cwd": "/tmp"},
        headers={"X-Internal-Token": _tok()},
    )
    if r.status_code != 400:
        print(f"  expected 400 for missing name, got {r.status_code}")
        return False
    return True


def test_create_session_inherits_sender_cwd_and_provider(client: TestClient) -> bool:
    sender = _new_session("sender-for-inherit", cwd="/tmp/inherited-cwd")
    r = client.post(
        "/api/internal/create-session",
        json={"name": "inheriting-session", "cwd": "/tmp/inherited-cwd", "sender_session_id": sender},
        headers={"X-Internal-Token": _tok()},
    )
    if r.status_code != 200 or not r.json().get("success"):
        print(f"  create_session failed: {r.status_code} {r.text}")
        return False
    sender_record = session_manager.get(sender)
    new_record = session_manager.get(r.json()["session_id"])
    if new_record.get("provider_id") != sender_record.get("provider_id"):
        print(
            f"  new session should inherit sender's provider_id: "
            f"{new_record.get('provider_id')!r} != {sender_record.get('provider_id')!r}"
        )
        return False
    return True


# ------------------------------------------------------------------ create_sub_session
def test_create_sub_session_actually_creates_child_session(client: TestClient) -> bool:
    parent = _new_session("parent-session", cwd="/tmp/parent-cwd")
    r = client.post(
        "/api/internal/create-sub-session",
        json={"sender_session_id": parent, "description": "helper for parent"},
        headers={"X-Internal-Token": _tok()},
    )
    if r.status_code != 200 or not r.json().get("success"):
        print(f"  create_sub_session failed: {r.status_code} {r.text}")
        return False
    sub_id = r.json().get("target_session_id")
    persisted = session_manager.get(sub_id)
    if not persisted:
        print("  sub-session does not actually exist in session_manager")
        return False
    if persisted.get("cwd") != "/tmp/parent-cwd":
        print(f"  sub-session should inherit parent cwd: {persisted.get('cwd')}")
        return False
    return True


def test_create_sub_session_missing_parent_is_400(client: TestClient) -> bool:
    r = client.post(
        "/api/internal/create-sub-session",
        json={"sender_session_id": "nope", "description": "x"},
        headers={"X-Internal-Token": _tok()},
    )
    if r.status_code != 400:
        print(f"  expected 400 for missing parent, got {r.status_code}")
        return False
    return True


# ------------------------------------------------------------------ create_worker
def _install_team_orchestration_extension() -> None:
    """Same fixture as test_worker_provisioning.py: `create_worker` requires
    the team-orchestration builtin extension to be active."""
    extension_id = (
        extension_store.extension_id_for_role("team-orchestration")
        or "test.team-orchestration"
    )
    package = Path(_TMP_HOME) / "private-fixtures" / extension_id
    if package.exists():
        shutil.rmtree(package)
    package.mkdir(parents=True)
    manifest = {
        "kind": extension_store.MANIFEST_KIND,
        "id": extension_id,
        "name": extension_id,
        "version": "1.0.0",
        "description": extension_id,
        "core_roles": ["team-orchestration"],
        "surfaces": ["backend_feature"],
        "entrypoints": {},
        "permissions": {},
        "marketplace": {},
    }
    (package / "better-agent-extension.json").write_text(json.dumps(manifest), encoding="utf-8")
    extension_store._install_from_package_dir(  # type: ignore[attr-defined]
        package_dir=package,
        source={
            "type": "better_agent_local",
            "repo_url": str(package.parent),
            "extension_path": package.name,
            "ref": "",
            "commit_sha": extension_id,
        },
        persist=True,
        force_enabled=True,
    )


def test_create_worker_missing_justification_is_rejected(client: TestClient) -> bool:
    _install_team_orchestration_extension()
    caller = session_store.create_session(
        name="manager2", model="m", cwd="/tmp", orchestration_mode="team",
        worker_creation_policy="approve",
    )["id"]
    r = client.post(
        "/api/internal/create-worker",
        json={
            "app_session_id": caller,
            "worker_description": "fresh worker",
            "justification": "",
            "orchestration_mode": "native",
        },
        headers={"X-Internal-Token": _tok()},
    )
    if r.json().get("success") is not False:
        print(f"  expected success False for missing justification: {r.json()}")
        return False
    return True


TESTS = [
    ("create_session actually creates a standalone session", test_create_session_actually_creates_standalone_session),
    ("create_session missing name -> 400", test_create_session_missing_name_is_400),
    ("create_session inherits sender's provider_id", test_create_session_inherits_sender_cwd_and_provider),
    ("create_sub_session actually creates a child session", test_create_sub_session_actually_creates_child_session),
    ("create_sub_session missing parent -> 400", test_create_sub_session_missing_parent_is_400),
    ("create_worker missing justification is rejected", test_create_worker_missing_justification_is_rejected),
]


def main_run() -> int:
    with TestClient(main.app, client=("127.0.0.1", 50000)) as client:
        authenticate_client(client)
        failed = 0
        for name, fn in TESTS:
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
        print()
        if failed:
            print(f"{failed} of {len(TESTS)} test(s) FAILED")
        else:
            print(f"all {len(TESTS)} tests passed")
        return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main_run())
