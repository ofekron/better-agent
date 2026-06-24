"""Unit tests for scheduler.Scheduler.fire_due — the backend ticker that
turns due schedules into normal prompts via coordinator.submit_prompt.

Locks: due → submit_prompt(source="schedule", user_initiated=False) with
session-record model/cwd; once deleted after fire; recurring advanced
(overdue catch-up fires ONCE); session-gone → schedule dropped;
submit failure doesn't crash the tick.
"""
import asyncio
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-sched-fire-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import scheduler as scheduler_mod  # noqa: E402
from scheduler import Scheduler  # noqa: E402
from stores import schedule_store  # noqa: E402

failures: list[str] = []


def check(cond, msg):
    print(("  PASS" if cond else "  FAIL") + f": {msg}")
    if not cond:
        failures.append(msg)


class _StubSessions:
    def __init__(self, sessions: dict):
        self._sessions = sessions

    def get(self, sid):
        return self._sessions.get(sid)


class _StubCoordinator:
    def __init__(self, raise_on_submit: bool = False):
        self.submitted: list[tuple[str, dict]] = []
        self.dispatched: list[dict] = []
        self.raise_on_submit = raise_on_submit

    def submit_prompt(self, sid, params):
        if self.raise_on_submit:
            raise RuntimeError("queue locked")
        self.submitted.append((sid, params))
        return "item-id"

    async def dispatch_raw(self, sid, event):
        self.dispatched.append(event)


def main() -> int:
    now = datetime.now()
    past = (now - timedelta(minutes=30)).isoformat()

    scheduler_mod.session_manager = _StubSessions({
        "s1": {"model": "m1", "cwd": "/tmp/cwd1"},
    })

    print("T1 due once-schedule fires through submit_prompt and is deleted")
    r_once = schedule_store.create(
        app_session_id="s1", prompt="do it", kind="once", fire_at=past,
    )
    coord = _StubCoordinator()
    fired = asyncio.run(Scheduler(coord).fire_due(now))
    check(fired == 1, "fired exactly one")
    sid, params = coord.submitted[0]
    check(sid == "s1" and params["prompt"] == "do it", "prompt routed to session")
    check(params["source"] == "schedule" and params["user_initiated"] is False,
          "source='schedule', user_initiated=False")
    check(params["model"] == "m1" and params["cwd"] == "/tmp/cwd1",
          "model/cwd from the session record (authoritative)")
    check(schedule_store.get(r_once["id"]) is None, "once deleted after fire")
    check(any(e["type"] == "schedules_updated" for e in coord.dispatched),
          "schedules_updated broadcast after fire")

    print("T2 overdue recurring fires ONCE and advances past now (catch-up)")
    r_rec = schedule_store.create(
        app_session_id="s1", prompt="tick", kind="recurring",
        interval_seconds=60,
        fire_at=(now - timedelta(hours=3)).isoformat(),
    )
    coord = _StubCoordinator()
    fired = asyncio.run(Scheduler(coord).fire_due(now))
    check(fired == 1, "one catch-up fire despite 3h of missed intervals")
    rec = schedule_store.get(r_rec["id"])
    check(rec is not None and datetime.fromisoformat(rec["fire_at"]) > now,
          "recurring advanced past now")
    coord2 = _StubCoordinator()
    check(asyncio.run(Scheduler(coord2).fire_due(now)) == 0,
          "immediately re-ticking fires nothing")
    schedule_store.delete(r_rec["id"])

    print("T3 session gone → schedule dropped, nothing submitted")
    r_ghost = schedule_store.create(
        app_session_id="ghost", prompt="x", kind="once", fire_at=past,
    )
    coord = _StubCoordinator()
    fired = asyncio.run(Scheduler(coord).fire_due(now))
    check(fired == 0 and not coord.submitted, "no submit for missing session")
    check(schedule_store.get(r_ghost["id"]) is None, "ghost schedule dropped")

    print("T4 submit failure is contained (marked fired, no crash)")
    schedule_store.create(
        app_session_id="s1", prompt="boom", kind="once", fire_at=past,
    )
    coord = _StubCoordinator(raise_on_submit=True)
    fired = asyncio.run(Scheduler(coord).fire_due(now))
    check(fired == 0, "failed submit not counted as fired")
    check(schedule_store.due(now) == [],
          "marked before submit → no refire loop on persistent failure")

    print()
    if failures:
        print(f"FAILED: {len(failures)}")
        return 1
    print("OK: scheduler fire path")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
