from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-worker-policy-")
os.environ["BETTER_CLAUDE_TEST_AUTH_BYPASS"] = "1"

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
import session_store  # noqa: E402
from orchs.manager import _delegation  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _new_manager_session(policy: str = "ask") -> str:
    session = session_store.create_session(
        name="manager",
        model="claude-sonnet-4-6",
        cwd="/tmp",
        orchestration_mode="manager",
        worker_creation_policy=policy,
    )
    return session["id"]


def _capture_metadata_event(session_id: str, action) -> dict | None:
    received: list[dict] = []

    async def cb(ev):
        received.append(ev)

    main.coordinator.register_ws(session_id, cb)
    try:
        action()
    finally:
        main.coordinator.unregister_ws(session_id, cb)
    for ev in received:
        if ev.get("type") == "session_metadata_updated":
            return ev
    return None


def test_default_policy_is_ask(client: TestClient) -> bool:
    sid = _new_manager_session()
    session = session_store.get_session(sid)
    if session.get("worker_creation_policy") != "ask":
        print(f"  default mismatch: {session.get('worker_creation_policy')}")
        return False
    summary = next(s for s in session_store.list_sessions() if s["id"] == sid)
    if summary.get("worker_creation_policy") != "ask":
        print(f"  summary mismatch: {summary}")
        return False
    return True


def test_rest_sets_policy_and_broadcasts(client: TestClient) -> bool:
    sid = _new_manager_session()

    def go():
        return client.put(
            f"/api/sessions/{sid}/worker_creation_policy",
            json={"worker_creation_policy": "approve"},
        )

    ev = _capture_metadata_event(sid, go)
    session = session_store.get_session(sid)
    if session.get("worker_creation_policy") != "approve":
        print(f"  persisted mismatch: {session}")
        return False
    if not ev:
        print("  no session_metadata_updated broadcast")
        return False
    patch = ((ev.get("data") or {}).get("patch") or {})
    if patch.get("worker_creation_policy") != "approve":
        print(f"  patch mismatch: {ev}")
        return False
    return True


def test_invalid_policy_is_rejected(client: TestClient) -> bool:
    sid = _new_manager_session()
    r = client.put(
        f"/api/sessions/{sid}/worker_creation_policy",
        json={"worker_creation_policy": "whatever"},
    )
    if r.status_code != 400:
        print(f"  expected 400, got {r.status_code}: {r.text}")
        return False
    if session_store.get_session(sid).get("worker_creation_policy") != "ask":
        print("  invalid policy mutated session")
        return False
    return True


def test_auto_deny_short_circuits_fresh_worker_creation(client: TestClient) -> bool:
    sid = _new_manager_session("deny")

    class TurnManager:
        cancel_events: dict[str, asyncio.Event] = {}

        def get_turn_save_callback(self, app_session_id: str):
            return None

    class Coordinator:
        active_delegations: dict[str, int] = {}
        turn_manager = TurnManager()

        async def persist_and_dispatch_raw(self, app_session_id: str, event: dict) -> None:
            raise AssertionError("deny must not dispatch approval events")

    async def run() -> dict:
        return await _delegation.run_delegation(
            Coordinator(),
            app_session_id=sid,
            cwd="/tmp",
            instructions="do work",
            model="claude-sonnet-4-6",
            worker_description="new worker",
            worker_session_id=None,
            justification="needed",
            proposed_orchestration_mode="native",
        )

    result = asyncio.run(run())
    if result.get("success") is not False:
        print(f"  expected error payload: {result}")
        return False
    if "auto-denied" not in str(result.get("error")):
        print(f"  wrong error: {result}")
        return False
    return True


TESTS = [
    ("default policy is ask", test_default_policy_is_ask),
    ("REST sets policy and broadcasts", test_rest_sets_policy_and_broadcasts),
    ("invalid policy is rejected", test_invalid_policy_is_rejected),
    ("deny short-circuits fresh worker creation", test_auto_deny_short_circuits_fresh_worker_creation),
]


def main_run() -> int:
    with TestClient(main.app, client=("127.0.0.1", 50000)) as client:
        failed = 0
        try:
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
        finally:
            shutil.rmtree(_TMP_HOME, ignore_errors=True)
        print()
        if failed:
            print(f"{failed} of {len(TESTS)} test(s) FAILED")
            return 1
        print(f"all {len(TESTS)} tests passed")
        return 0


if __name__ == "__main__":
    sys.exit(main_run())
