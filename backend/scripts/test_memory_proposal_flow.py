from __future__ import annotations

import os
import shutil
import sys
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-memory-proposal-")

import _test_installation  # noqa: E402
from pathlib import Path  # noqa: E402
_test_installation.activate(Path(_TMP_HOME))

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
import memory_store  # noqa: E402
import session_store  # noqa: E402
import user_input_store  # noqa: E402
from scripts.auth_test_helpers import authenticate_client  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _new_session() -> str:
    return session_store.create_session(
        name="memory-proposal",
        model="m",
        cwd="/tmp",
        orchestration_mode="native",
    )["id"]


def _proposal(**overrides) -> dict:
    base = {
        "action": "add",
        "name": "test-memory-slug",
        "description": "A description.",
        "type": "user",
        "content": "The memory body.",
        "scope_type": "global",
        "scope_path": "",
    }
    base.update(overrides)
    return base


def test_create_and_approve_writes_memory(client: TestClient) -> bool:
    sid = _new_session()
    token = main.coordinator.internal_token
    result_holder: dict = {}

    def post_request() -> None:
        result_holder["response"] = client.post(
            "/api/internal/user-input/request",
            headers={"X-Internal-Token": token},
            json={
                "app_session_id": sid,
                "kind": "memory",
                "memory_proposal": _proposal(name="approve-me"),
                "timeout_seconds": 5,
            },
        )

    t = threading.Thread(target=post_request)
    t.start()
    request_id = ""
    deadline = time.time() + 3
    while time.time() < deadline:
        pending = user_input_store.pending_for_session(sid)
        if pending:
            request_id = pending[0]["request_id"]
            break
        time.sleep(0.02)
    if not request_id:
        return False

    pending_req = user_input_store.get_request(request_id)
    if pending_req.get("kind") != "memory" or pending_req.get("memory_proposal", {}).get("name") != "approve-me":
        return False

    edited = _proposal(name="approve-me", description="Edited by user.")
    resolve = client.post(
        f"/api/user-input/{request_id}/resolve",
        json={"app_session_id": sid, "approved": True, "edited": edited},
    )
    t.join(timeout=3)
    if t.is_alive() or resolve.status_code != 200:
        return False
    data = result_holder.get("response").json()
    if not data.get("success") or not data.get("approved"):
        return False
    if data.get("memory_proposal", {}).get("description") != "Edited by user.":
        return False

    write_res = client.post(
        "/api/internal/memory/write",
        headers={"X-Internal-Token": token},
        json={"memory_proposal": data["memory_proposal"]},
    )
    if write_res.status_code != 200 or not write_res.json().get("success"):
        return False
    stored = memory_store.read_memory(scope_type="global", scope_path="", slug="approve-me")
    return stored is not None and stored["description"] == "Edited by user."


def test_reject_does_not_write_memory(client: TestClient) -> bool:
    sid = _new_session()
    token = main.coordinator.internal_token
    result_holder: dict = {}

    def post_request() -> None:
        result_holder["response"] = client.post(
            "/api/internal/user-input/request",
            headers={"X-Internal-Token": token},
            json={
                "app_session_id": sid,
                "kind": "memory",
                "memory_proposal": _proposal(name="reject-me"),
                "timeout_seconds": 5,
            },
        )

    t = threading.Thread(target=post_request)
    t.start()
    request_id = ""
    deadline = time.time() + 3
    while time.time() < deadline:
        pending = user_input_store.pending_for_session(sid)
        if pending:
            request_id = pending[0]["request_id"]
            break
        time.sleep(0.02)
    if not request_id:
        return False

    resolve = client.post(
        f"/api/user-input/{request_id}/resolve",
        json={"app_session_id": sid, "approved": False},
    )
    t.join(timeout=3)
    if t.is_alive() or resolve.status_code != 200:
        return False
    data = result_holder.get("response").json()
    stored = memory_store.read_memory(scope_type="global", scope_path="", slug="reject-me")
    return data.get("approved") is False and stored is None


def test_invalid_proposal_is_rejected(client: TestClient) -> bool:
    sid = _new_session()
    token = main.coordinator.internal_token
    res = client.post(
        "/api/internal/user-input/request",
        headers={"X-Internal-Token": token},
        json={
            "app_session_id": sid,
            "kind": "memory",
            "memory_proposal": _proposal(type="not-a-real-type"),
            "timeout_seconds": 5,
        },
    )
    data = res.json()
    return res.status_code == 200 and data.get("success") is False and "type" in (data.get("error") or "")


def test_direct_user_edit_and_delete(client: TestClient) -> bool:
    memory_store.write_memory(
        scope_type="global", scope_path="", slug="browser-edit-target",
        description="Original.", mem_type="user", content="Original body.",
    )
    put_res = client.put(
        "/api/memory/global/browser-edit-target",
        json={"scope_path": "", "description": "Updated.", "type": "user", "content": "Updated body."},
    )
    if put_res.status_code != 200:
        return False
    all_res = client.get("/api/memory/all")
    global_names = {m["name"]: m for m in all_res.json().get("global", [])}
    if global_names.get("browser-edit-target", {}).get("description") != "Updated.":
        return False
    delete_res = client.delete("/api/memory/global/browser-edit-target")
    if delete_res.status_code != 200:
        return False
    return memory_store.read_memory(scope_type="global", scope_path="", slug="browser-edit-target") is None


def run() -> int:
    client = TestClient(main.app)
    authenticate_client(client)
    tests = [
        ("create + approve writes memory with user edits", lambda: test_create_and_approve_writes_memory(client)),
        ("reject does not write memory", lambda: test_reject_does_not_write_memory(client)),
        ("invalid proposal is rejected", lambda: test_invalid_proposal_is_rejected(client)),
        ("direct user edit and delete via /api/memory", lambda: test_direct_user_edit_and_delete(client)),
    ]
    failures: list[str] = []
    for name, fn in tests:
        try:
            ok = bool(fn())
        except Exception as exc:
            ok = False
            print(f"  {name} raised: {exc}")
        print(f"{PASS if ok else FAIL} {name}")
        if not ok:
            failures.append(name)
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        raise SystemExit(run())
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
