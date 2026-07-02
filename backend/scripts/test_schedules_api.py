"""Contract tests for the user-facing Schedules page endpoints:
GET /api/schedules (cross-session snapshot enriched with session_name /
session_exists) and DELETE /api/schedules/{id} (no session gate — orphans
stay cancelable). Also locks that the model-facing internal endpoint does
NOT grow a cross-session list action, and that every schedule mutation
fires the global `schedules_changed` invalidation ping.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-schedapi-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from starlette.testclient import TestClient  # noqa: E402
import auth  # noqa: E402
import main  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402
from stores import schedule_store  # noqa: E402

failures: list[str] = []


def check(cond, msg):
    print(("  PASS" if cond else "  FAIL") + f": {msg}")
    if not cond:
        failures.append(msg)


CLIENT = TestClient(main.app, client=("localhost", 50000))
AUTH_HEADERS = {"Authorization": f"Bearer {auth.create_token('test')}"}
TOKEN = main.coordinator.internal_token

_global_pings: list[str] = []
_orig_broadcast_global = main.coordinator.broadcast_global


async def _recording_broadcast_global(event_type: str, data: dict) -> None:
    _global_pings.append(event_type)
    await _orig_broadcast_global(event_type, data)


def main_test() -> int:
    main.coordinator.broadcast_global = _recording_broadcast_global
    soon = (datetime.now() + timedelta(hours=1)).isoformat()

    sid_a = session_manager.create(cwd=_TMP_HOME, name="sched-page-a")["id"]
    sid_b = session_manager.create(cwd=_TMP_HOME, name="sched-page-b")["id"]

    print("T1 auth required on the page endpoints")
    r = CLIENT.get("/api/schedules")
    check(r.status_code == 401, f"unauthenticated GET → 401 (got {r.status_code})")
    r = CLIENT.delete("/api/schedules/nope")
    check(r.status_code == 401, f"unauthenticated DELETE → 401 (got {r.status_code})")

    print("T2 cross-session snapshot with session enrichment")
    rec_a = schedule_store.create(
        app_session_id=sid_a, prompt="from a", kind="once", fire_at=soon)
    rec_b = schedule_store.create(
        app_session_id=sid_b, prompt="from b", kind="recurring",
        interval_seconds=3600)
    orphan = schedule_store.create(
        app_session_id="ghost-session", prompt="orphan", kind="once",
        fire_at=soon)
    r = CLIENT.get("/api/schedules", headers=AUTH_HEADERS)
    check(r.status_code == 200, f"GET ok (got {r.status_code})")
    by_id = {s["id"]: s for s in r.json()["schedules"]}
    check(
        {rec_a["id"], rec_b["id"], orphan["id"]} <= set(by_id),
        "schedules from BOTH sessions + orphan present in one snapshot",
    )
    check(by_id[rec_a["id"]].get("session_name") == "sched-page-a"
          and by_id[rec_a["id"]].get("session_exists") is True,
          "session A enrichment (name + exists)")
    check(by_id[rec_b["id"]].get("session_name") == "sched-page-b",
          "session B enrichment")
    check(by_id[orphan["id"]].get("session_exists") is False,
          "orphan flagged session_exists=False")

    print("T3 orphan cancel works (no session gate)")
    _global_pings.clear()
    r = CLIENT.delete(f"/api/schedules/{orphan['id']}", headers=AUTH_HEADERS)
    check(r.status_code == 200 and r.json().get("success") is True,
          f"orphan DELETE ok (got {r.status_code})")
    check(schedule_store.get(orphan["id"]) is None, "orphan gone from store")
    check("schedules_changed" in _global_pings,
          "delete fired global schedules_changed ping")

    print("T4 unknown id → 404")
    r = CLIENT.delete("/api/schedules/definitely-unknown", headers=AUTH_HEADERS)
    check(r.status_code == 404, f"unknown id → 404 (got {r.status_code})")

    print("T5 internal (model-facing) endpoint has no cross-session list")
    r = CLIENT.post(
        "/api/internal/schedules",
        json={"action": "list_all", "app_session_id": sid_a},
        headers={"X-Internal-Token": TOKEN},
    )
    body = r.json()
    check(r.status_code == 200 and body.get("success") is False,
          f"action=list_all rejected ({body})")

    print("T6 internal create fires the global ping too")
    _global_pings.clear()
    r = CLIENT.post(
        "/api/internal/schedules",
        json={"action": "create", "app_session_id": sid_a,
              "prompt": "ping", "kind": "once", "fire_at": soon},
        headers={"X-Internal-Token": TOKEN},
    )
    check(r.json().get("success") is True, "internal create ok")
    check("schedules_changed" in _global_pings,
          "create fired global schedules_changed ping")

    if failures:
        print(f"FAILED: {len(failures)}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    import shutil
    try:
        sys.exit(main_test())
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
