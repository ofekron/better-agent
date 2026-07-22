"""Tests for stopping a routine — task_runner.stop_task and the stopped
lifecycle.

Locks: stop marks the task stopped and disarms its triggers; every session
in the spawned ledger gets queued prompts cancelled + all runs cancelled +
its schedules deleted; launch_task fails closed on a stopped task;
register_for_task arms nothing while stopped and re-arms on resume;
drop_session_references prunes the ledger; legacy records seed the ledger
from recent_runs.
"""
import asyncio
import os
import sys
from datetime import datetime, timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home  # noqa: E402
_TMP_HOME = _test_home.isolate("bc-test-task-stop-")

import session_manager as session_manager_mod  # noqa: E402
import task_runner  # noqa: E402
from stores import schedule_store, task_store, task_trigger_store  # noqa: E402

failures: list[str] = []


def check(cond, msg):
    print(("  PASS" if cond else "  FAIL") + f": {msg}")
    if not cond:
        failures.append(msg)


class _StubSessionManager:
    def __init__(self):
        self.removed_queued: list[tuple[str, object]] = []
        self.created: list[dict] = []

    def remove_queued_prompt(self, sid, queued_id):
        self.removed_queued.append((sid, queued_id))
        return {"id": sid}

    def queued_prompt_count(self, sid):
        return 1

    def create(self, **kwargs):
        record = {"id": f"sess-new-{len(self.created)}", **kwargs}
        self.created.append(record)
        return record


class _StubCoordinator:
    def __init__(self, fail_sids=()):
        self.cancelled_queued: list[str] = []
        self.cancelled_sessions: list[str] = []
        self.broadcasts: list[tuple[str, dict]] = []
        self.dispatched: list[dict] = []
        self.fail_sids = set(fail_sids)
        self.on_submit = None

    def cancel_queued(self, sid, queued_id=None):
        self.cancelled_queued.append(sid)
        return True

    async def cancel_session(self, sid):
        if sid in self.fail_sids:
            raise RuntimeError("provider wedged")
        self.cancelled_sessions.append(sid)
        return 1

    async def submit_prompt_async(self, sid, params):
        if self.on_submit is not None:
            self.on_submit(sid)
        return "item-race"

    async def broadcast_global(self, event_type, data):
        self.broadcasts.append((event_type, data))

    async def dispatch_raw(self, sid, event):
        self.dispatched.append(event)


def _make_task(**overrides) -> dict:
    fields = dict(
        cwd="/tmp/proj",
        name="Nightly checks",
        prompt="run the checks",
        trigger={
            "kind": "schedule",
            "config": {"mode": "recurring", "interval_seconds": 3600},
        },
    )
    fields.update(overrides)
    return task_store.create(**fields)


def main() -> int:
    stub_sessions = _StubSessionManager()
    session_manager_mod.manager = stub_sessions

    print("T1 stop tears down triggers, queued, runs, and schedules")
    task = _make_task()
    task_trigger_store.register_for_task(task)
    check(len(task_trigger_store.list_for_task(task["id"])) == 1, "trigger armed")
    task_store.record_run(task["id"], "sess-a", queue_item_id="q1")
    task_store.record_run(task["id"], "sess-b", queue_item_id="q2")
    schedule_store.create(
        app_session_id="sess-a", prompt="follow up", kind="once",
        fire_at=(datetime.now() + timedelta(hours=1)).isoformat(),
        source_task_id=task["id"],
    )
    schedule_store.create(
        app_session_id="sess-a", prompt="manual follow up", kind="once",
        fire_at=(datetime.now() + timedelta(hours=1)).isoformat(),
    )
    schedule_store.create(
        app_session_id="sess-a", prompt="other task's", kind="once",
        fire_at=(datetime.now() + timedelta(hours=1)).isoformat(),
        source_task_id="someothertask",
    )
    schedule_store.create(
        app_session_id="unrelated", prompt="keep me", kind="once",
        fire_at=(datetime.now() + timedelta(hours=1)).isoformat(),
    )
    coord = _StubCoordinator()
    result = asyncio.run(task_runner.stop_task(task["id"], coordinator=coord))

    stopped = task_store.get(task["id"])
    check(stopped["stopped"] is True, "task marked stopped")
    check(task_trigger_store.list_for_task(task["id"]) == [], "triggers disarmed")
    check(set(coord.cancelled_sessions) == {"sess-a", "sess-b"},
          "all ledger sessions run-cancelled")
    check(set(coord.cancelled_queued) == {"sess-a", "sess-b"},
          "queued prompts cancelled per session")
    check({s for s, _ in stub_sessions.removed_queued} == {"sess-a", "sess-b"},
          "queued prompts removed from session records")
    check([s["prompt"] for s in schedule_store.list_for_session("sess-a")] == [
        "manual follow up", "other task's",
    ], "only routine-created schedules deleted")
    check(len(schedule_store.list_for_session("unrelated")) == 1,
          "unrelated schedules untouched")
    check(result["cancelled_runs"] == 2 and result["deleted_schedules"] == 1,
          "stop result reports the teardown counts")
    check(any(b[0] == "tasks_changed" for b in coord.broadcasts),
          "tasks_changed broadcast after stop")
    check(any(e.get("type") == "schedules_updated" for e in coord.dispatched),
          "schedules_updated broadcast for the pruned session")

    print("T2 launch_task fails closed on a stopped task")
    try:
        asyncio.run(task_runner.launch_task(task["id"], coordinator=coord))
        check(False, "launch on stopped task raises")
    except task_runner.TaskLaunchError as e:
        check(e.status == 409, "launch on stopped task raises 409")

    print("T3 register_for_task arms nothing while stopped, re-arms on resume")
    check(task_trigger_store.register_for_task(stopped) == [],
          "no trigger records while stopped")
    resumed = task_store.update(task["id"], {"stopped": False})
    check(resumed["stopped"] is False, "update resumes the task")
    task_trigger_store.register_for_task(resumed)
    check(len(task_trigger_store.list_for_task(task["id"])) == 1,
          "trigger re-armed after resume")

    print("T4 ledger maintenance")
    check(resumed["spawned_session_ids"] == ["sess-a", "sess-b"],
          "record_run appends to the spawned ledger")
    task_store.record_run(task["id"], "sess-a", queue_item_id="q3")
    check(task_store.get(task["id"])["spawned_session_ids"] == ["sess-a", "sess-b"],
          "ledger dedupes repeated sessions")
    task_store.drop_session_references("sess-a")
    check(task_store.get(task["id"])["spawned_session_ids"] == ["sess-b"],
          "session delete prunes the ledger")

    print("T5 legacy records seed the ledger from recent_runs")
    legacy = {
        "id": "legacy1",
        "recent_runs": [{"session_id": "old-a"}, {"session_id": "old-b"}],
    }
    normalized = task_store._normalize_task(dict(legacy))
    check(normalized["spawned_session_ids"] == ["old-a", "old-b"],
          "ledger seeded from recent_runs")
    check(normalized["stopped"] is False, "legacy records default to not stopped")

    print("T7 update cannot stop — only the stop action can")
    try:
        task_store.update(task["id"], {"stopped": True})
        check(False, "update(stopped=true) raises")
    except ValueError:
        check(True, "update(stopped=true) rejected")
    check(task_store.get(task["id"])["stopped"] is False,
          "task stays running after rejected update-stop")

    print("T8 launch/stop race: launch that loses the race unwinds itself")
    race_task = _make_task(
        name="Racy", trigger=None, model="m1", provider_id="p1",
    )
    coord_race = _StubCoordinator()
    # Flips `stopped` between submit and the post-record_run re-read — the
    # launch-side unwind. The stop-side of the interleaving (record_run
    # lands first) is covered by the store-lock ordering: set_stopped then
    # returns a ledger that already contains the new session.
    coord_race.on_submit = lambda sid: task_store.set_stopped(race_task["id"], True)
    try:
        asyncio.run(task_runner.launch_task(race_task["id"], coordinator=coord_race))
        check(False, "race launch raises")
    except task_runner.TaskLaunchError as e:
        check(e.status == 409, "race launch raises 409")
    raced_sid = stub_sessions.created[-1]["id"]
    check(raced_sid in coord_race.cancelled_sessions,
          "raced session self-cancelled")
    check((raced_sid, None) in stub_sessions.removed_queued,
          "raced session queued prompt removed")
    check(raced_sid in task_store.get(race_task["id"])["spawned_session_ids"],
          "raced session still on the ledger for later teardown")

    print("T9 per-session error isolation in the stop cascade")
    iso_task = _make_task(name="Isolated", trigger=None)
    task_store.record_run(iso_task["id"], "bad-sid")
    task_store.record_run(iso_task["id"], "good-sid")
    coord_iso = _StubCoordinator(fail_sids={"bad-sid"})
    result = asyncio.run(task_runner.stop_task(iso_task["id"], coordinator=coord_iso))
    check("good-sid" in coord_iso.cancelled_sessions,
          "healthy session still torn down when another fails")
    check(len(result["errors"]) == 1 and "bad-sid" in result["errors"][0],
          "failure surfaced in stop result errors")

    print("T6 stop on unknown task raises 404")
    try:
        asyncio.run(task_runner.stop_task("nope", coordinator=coord))
        check(False, "unknown task raises")
    except task_runner.TaskLaunchError as e:
        check(e.status == 404, "unknown task raises 404")

    print()
    if failures:
        print(f"{len(failures)} FAILURES")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
