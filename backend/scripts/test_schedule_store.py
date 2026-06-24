"""Unit tests for stores/schedule_store.py — the durable backend-owned
replacement for the CLI's in-process timers.

Locks: CRUD, once-vs-recurring mark_fired semantics, validation bounds
(min interval, max horizon, prompt length, per-session cap), and the
loud-empty behavior on schema-version mismatch.
"""
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-sched-store-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from stores import schedule_store  # noqa: E402

failures: list[str] = []


def check(cond, msg):
    print(("  PASS" if cond else "  FAIL") + f": {msg}")
    if not cond:
        failures.append(msg)


def main() -> int:
    now = datetime.now()
    soon = (now + timedelta(hours=1)).isoformat()

    print("T1 CRUD")
    r1 = schedule_store.create(
        app_session_id="s1", prompt="once-prompt", kind="once", fire_at=soon,
    )
    check(schedule_store.get(r1["id"]) is not None, "create+get once")
    r2 = schedule_store.create(
        app_session_id="s1", prompt="tick", kind="recurring",
        interval_seconds=60,
    )
    check(len(schedule_store.list_for_session("s1")) == 2, "list_for_session")
    check(schedule_store.list_for_session("other") == [], "list scoped by session")
    removed = schedule_store.delete(r1["id"])
    check(removed is not None and schedule_store.get(r1["id"]) is None, "delete")
    check(schedule_store.delete("nope") is None, "delete unknown → None")

    print("T2 due + mark_fired semantics")
    r3 = schedule_store.create(
        app_session_id="s1", prompt="due-once", kind="once", fire_at=soon,
    )
    later = now + timedelta(hours=2)
    due = schedule_store.due(later)
    check({d["id"] for d in due} == {r2["id"], r3["id"]}, "due returns both")
    check(schedule_store.due(now - timedelta(hours=1)) == [], "nothing due early")
    schedule_store.mark_fired(r3["id"], later)
    check(schedule_store.get(r3["id"]) is None, "once → deleted after fire")
    schedule_store.mark_fired(r2["id"], later)
    rec = schedule_store.get(r2["id"])
    check(rec is not None
          and datetime.fromisoformat(rec["fire_at"]) > later
          and rec["last_fired_at"] is not None,
          "recurring → fire_at advanced past now, last_fired_at stamped")

    print("T3 validation bounds")
    bads = [
        dict(prompt="x", kind="recurring", interval_seconds=5),
        dict(prompt="x", kind="recurring", interval_seconds=None),
        dict(prompt="x", kind="once"),
        dict(prompt="x", kind="once", fire_at="not-a-date"),
        dict(prompt="x", kind="once",
             fire_at=(now + timedelta(days=400)).isoformat()),
        dict(prompt="x" * 20001, kind="once", fire_at=soon),
        dict(prompt="", kind="once", fire_at=soon),
        dict(prompt="x", kind="weird", fire_at=soon),
    ]
    for bad in bads:
        try:
            schedule_store.create(app_session_id="s1", **bad)
            check(False, f"accepted invalid input: {str(bad)[:60]}")
        except ValueError:
            check(True, f"rejected: {str(bad)[:60]}")

    print("T4 per-session cap")
    for i in range(schedule_store.MAX_PER_SESSION):
        schedule_store.create(
            app_session_id="cap", prompt=f"p{i}", kind="once", fire_at=soon,
        )
    try:
        schedule_store.create(
            app_session_id="cap", prompt="overflow", kind="once", fire_at=soon,
        )
        check(False, "cap not enforced")
    except ValueError:
        check(True, f"per-session cap enforced at {schedule_store.MAX_PER_SESSION}")

    print("T5 schema-version mismatch → loud empty")
    schedule_store._path().write_text('{"version": 999, "schedules": [{}]}')
    check(schedule_store.list_for_session("s1") == [],
          "bad version reads as empty (wipe to start fresh)")

    print()
    if failures:
        print(f"FAILED: {len(failures)}")
        return 1
    print("OK: schedule_store")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
