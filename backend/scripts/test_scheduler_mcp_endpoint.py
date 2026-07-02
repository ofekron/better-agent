"""Security + contract tests for POST /api/internal/schedules — the
loopback endpoint behind the runner's `scheduler` MCP tools.

Locks: internal-token required (403 without/with wrong token); unknown
session rejected; create validates server-side (oversized prompt, bad
kind, bad interval all rejected via schedule_store); delete scoped to
the owning session; list scoped; delay_seconds → fire_at conversion.

Also locks the spawn-side strip: provider_claude.start_run must append
TIMER_TOOLS to every input.json disallowed_tools list (source-level),
and the runner refuses to spawn without them (covered at runtime by
test_runner_alive_outlives_complete.py / test_per_turn_runner_regression.py).
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-schedep-")
os.environ["BETTER_CLAUDE_TEST_AUTH_BYPASS"] = "1"

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from starlette.testclient import TestClient  # noqa: E402
import extension_backend_loader  # noqa: E402
import extension_store  # noqa: E402
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
TOKEN = main.coordinator.internal_token
AUTH_HEADERS = {"Authorization": f"Bearer {auth.create_token('test')}"}


def _post(body: dict, token: str | None = TOKEN):
    headers = {"X-Internal-Token": token} if token is not None else {}
    return CLIENT.post("/api/internal/schedules", json=body, headers=headers)


def _patch_scheduler_extension_dispatch():
    old_enabled = extension_store.is_extension_enabled_cached
    old_spec = extension_backend_loader.backend_entrypoint_spec_cached
    old_dispatch = extension_backend_loader.dispatch_extension_backend_request
    scheduler_id = extension_store.BUILTIN_SCHEDULER_EXTENSION_ID

    def enabled(extension_id: str) -> bool:
        if extension_id == scheduler_id:
            return True
        return old_enabled(extension_id)

    def spec(extension_id: str):
        if extension_id == scheduler_id:
            return {"extension_id": scheduler_id, "backend_module": "backend.routes"}
        return old_spec(extension_id)

    async def dispatch(*_args, **_kwargs):
        raise AssertionError("scheduler GET should not hit extension subprocess dispatch")

    extension_store.is_extension_enabled_cached = enabled
    extension_backend_loader.backend_entrypoint_spec_cached = spec
    extension_backend_loader.dispatch_extension_backend_request = dispatch

    def restore() -> None:
        extension_store.is_extension_enabled_cached = old_enabled
        extension_backend_loader.backend_entrypoint_spec_cached = old_spec
        extension_backend_loader.dispatch_extension_backend_request = old_dispatch

    return restore


def main_test() -> int:
    sid = session_manager.create(cwd=_TMP_HOME, name="sched-ep-test")["id"]
    soon = (datetime.now() + timedelta(hours=1)).isoformat()

    print("T1 auth: internal token required")
    r = _post({"action": "list", "app_session_id": sid}, token="wrong-token")
    check(r.status_code == 403, f"wrong token → 403 (got {r.status_code})")
    r = _post({"action": "list", "app_session_id": sid}, token=None)
    check(r.status_code in (403, 422),
          f"missing token header rejected (got {r.status_code})")

    print("T2 unknown session rejected")
    r = _post({"action": "create", "app_session_id": "no-such-session",
               "prompt": "x", "kind": "once", "delay_seconds": 60})
    check(r.status_code == 200 and r.json()["success"] is False,
          "unknown session → success=False")

    print("T3 create with delay_seconds")
    r = _post({"action": "create", "app_session_id": sid,
               "prompt": "ping me", "kind": "once", "delay_seconds": 3600})
    body = r.json()
    check(body.get("success") is True, f"create ok ({body})")
    sched_id = body["schedule"]["id"]
    fire_at = datetime.fromisoformat(body["schedule"]["fire_at"])
    delta = (fire_at - datetime.now()).total_seconds()
    check(3500 < delta < 3700, f"delay_seconds → fire_at (~1h, got {delta:.0f}s)")

    print("T4 server-side validation (incl. hostile input → no 500s)")
    for bad in [
        {"prompt": "x" * 20001, "kind": "once", "fire_at": soon},
        {"prompt": "x", "kind": "weird", "fire_at": soon},
        {"prompt": "x", "kind": "recurring", "interval_seconds": 1},
        {"prompt": "x", "kind": "once"},
        {"prompt": "x", "kind": "once", "delay_seconds": -5},
        {"prompt": "x", "kind": "once", "delay_seconds": True},
        {"prompt": "x", "kind": "once", "fire_at": "2026-06-12T10:00:00+05:00"},
        {"prompt": "x", "kind": "once", "fire_at": "absurd"},
        {"prompt": "x", "kind": "recurring", "interval_seconds": 60,
         "fire_at": 12345},
        {"prompt": "x", "kind": "recurring", "interval_seconds": 60,
         "fire_at": "2026-06-12T10:00:00Z"},
    ]:
        r = _post({"action": "create", "app_session_id": sid, **bad})
        check(r.status_code == 200 and r.json().get("success") is False,
              f"rejected without 500: {str(bad)[:60]}")

    # NaN/Infinity can't ride Python's json.dumps but CAN arrive as raw
    # JSON (json.loads accepts them) — send raw bytes like an attacker.
    for raw_delay in ("NaN", "Infinity"):
        r = CLIENT.post(
            "/api/internal/schedules",
            content=('{"action":"create","app_session_id":"%s",'
                     '"prompt":"x","kind":"once","delay_seconds":%s}'
                     % (sid, raw_delay)),
            headers={"X-Internal-Token": TOKEN,
                     "Content-Type": "application/json"},
        )
        check(r.status_code == 200 and r.json().get("success") is False,
              f"raw {raw_delay} delay rejected without 500")

    print("T5 list + delete scoping")
    r = _post({"action": "list", "app_session_id": sid})
    check([s["id"] for s in r.json()["schedules"]] == [sched_id], "list shows it")
    other = session_manager.create(cwd=_TMP_HOME, name="other")["id"]
    r = _post({"action": "delete", "app_session_id": other,
               "schedule_id": sched_id})
    check(r.json()["success"] is False,
          "delete from another session rejected (scoped)")
    r = _post({"action": "delete", "app_session_id": sid,
               "schedule_id": sched_id})
    check(r.json()["success"] is True and schedule_store.get(sched_id) is None,
          "owner delete works")

    print("T6 scheduler feature routes are extension-owned")
    route_specs = {
        (getattr(route, "path", ""), tuple(sorted(getattr(route, "methods", set()) or [])))
        for route in main.app.routes
    }
    check(
        not any(path == "/api/sessions/{app_session_id}/schedules" for path, _ in route_specs),
        "public GET schedules route removed",
    )
    # The Schedules page owns a core cross-session cancel-by-id (DELETE
    # only) — per-session CRUD stays extension-owned.
    check(
        {m for p, ms in route_specs if p == "/api/schedules/{schedule_id}" for m in ms}
        == {"DELETE"},
        "core schedule-id route is DELETE-only (page cancel)",
    )

    print("T7 scheduler extension GET is core-fast")
    restore_dispatch = _patch_scheduler_extension_dispatch()
    try:
        r = CLIENT.get(
            f"/api/extensions/{extension_store.BUILTIN_SCHEDULER_EXTENSION_ID}"
            f"/backend/sessions/{sid}/schedules",
            headers=AUTH_HEADERS,
        )
        check(r.status_code == 200, f"extension GET schedules → 200 ({r.status_code})")
        check(r.json().get("schedules") == [], "extension GET schedules returns current list")
        r = CLIENT.get(
            f"/api/extensions/{extension_store.BUILTIN_SCHEDULER_EXTENSION_ID}"
            "/backend/sessions/no-such-session/schedules",
            headers=AUTH_HEADERS,
        )
        check(r.status_code == 404, f"missing session GET → 404 ({r.status_code})")
    finally:
        restore_dispatch()

    print("T8 spawn-side strip is wired (source-level)")
    from runs_dir import TIMER_TOOLS
    check(set(TIMER_TOOLS) == {
        "CronCreate", "CronDelete", "CronList", "ScheduleWakeup",
    }, "runs_dir.TIMER_TOOLS is the single source of the four names")
    src = open(os.path.join(_BACKEND, "provider_claude.py")).read()
    import ast
    tree = ast.parse(src)
    in_payload = False
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "start_run":
            seg = ast.get_source_segment(src, node) or ""
            in_payload = "TIMER_TOOLS" in seg and "disallowed_tools" in seg
    check(in_payload, "start_run merges TIMER_TOOLS into input.json disallowed_tools")
    runner_src = open(os.path.join(_BACKEND, "runner.py")).read()
    check("from runs_dir import TIMER_TOOLS" in runner_src,
          "runner's fail-closed gate uses the same constant")

    print()
    if failures:
        print(f"FAILED: {len(failures)}")
        return 1
    print("OK: scheduler internal endpoint")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main_test())
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
