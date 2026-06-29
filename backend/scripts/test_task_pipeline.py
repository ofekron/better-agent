"""Integration tests for the task pipeline: data model, trigger engine,
script runner, assessor, and scheduler task-trigger dispatch.

Standalone runner (mirrors test_scheduler_fire): no provider CLI, no real
sessions — launch_task is stubbed. Locks:
  - task_store: new additive fields round-trip + default-on-read for legacy
    records; record_run stamps a pending verdict; set_run_verdict +
    find_pending_run_for_session drive the post-turn assessor.
  - task_trigger_store: schedule/script kinds register + advance/delete on
    fire; unregister_task wipes a task's triggers.
  - task_script.run_script: argv (never shell) — metacharacters stay literal.
  - task_assessor.assess: none→skipped, script exit/JSON verdict, llm_judge→
    recorded as error (no one-shot LLM primitive yet).
  - Scheduler.fire_task_triggers: detector exit 0 launches, non-zero skips;
    both advance the poll window.
"""
import asyncio
import os
import sys

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-task-pipeline-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from stores import task_store  # noqa: E402
from stores import task_trigger_store  # noqa: E402
import task_script  # noqa: E402
import task_assessor  # noqa: E402
import task_runner  # noqa: E402
from scheduler import Scheduler  # noqa: E402

failures: list[str] = []


def check(cond, msg):
    print(("  PASS" if cond else "  FAIL") + f": {msg}")
    if not cond:
        failures.append(msg)


def test_store_model():
    print("T1 task_store round-trips goal/trigger/scripts/assessment")
    rec = task_store.create(
        cwd="/tmp/proj", name="t1", prompt="do thing",
        goal="all green",
        trigger={"kind": "schedule", "config": {"mode": "recurring", "interval_seconds": 60}},
        scripts={"pre": [{"command": ["echo", "hi"]}], "post": []},
        assessment={"kind": "script", "config": {"command": ["true"]}},
    )
    check(rec["goal"] == "all green", "goal stored")
    check(rec["trigger"]["kind"] == "schedule", "trigger kind stored")
    check(rec["trigger"]["config"]["interval_seconds"] == 60, "trigger config stored")
    check(rec["assessment"]["kind"] == "script", "assessment kind stored")
    check(rec["scripts"]["pre"][0]["command"] == ["echo", "hi"], "pre-script stored")

    print("T2 invalid trigger rejected")
    try:
        task_store.create(
            cwd="/tmp/proj", name="bad", prompt="x",
            trigger={"kind": "bogus", "config": {}},
        )
        check(False, "invalid trigger kind should raise")
    except ValueError:
        check(True, "invalid trigger kind raises")

    print("T3 script trigger requires detector")
    try:
        task_store.create(
            cwd="/tmp/proj", name="bad2", prompt="x",
            trigger={"kind": "script", "config": {}},
        )
        check(False, "script trigger without detector should raise")
    except ValueError:
        check(True, "script trigger without detector raises")

    print("T4 legacy record gains default additive fields on read")
    raw = task_store._read()
    raw["tasks"].append({"id": "legacy1", "cwd": "/tmp/proj", "name": "leg", "prompt": "p"})
    task_store._write(raw)
    task_store._data_cache = None
    got = task_store.get("legacy1")
    check(got["goal"] == "", "legacy goal defaulted")
    check(got["trigger"]["kind"] == "manual", "legacy trigger defaulted to manual")
    check(got["assessment"]["kind"] == "none", "legacy assessment defaulted to none")

    print("T5 record_run stamps pending verdict; assessor finds + grades it")
    task_store.create(cwd="/tmp/proj2", name="t2", prompt="p",
                      assessment={"kind": "script", "config": {"command": ["true"]}})
    t2 = [t for t in task_store.list_for_project("/tmp/proj2") if t["name"] == "t2"][0]
    task_store.record_run(t2["id"], "sess-1", queue_item_id="q1")
    found = task_store.find_pending_run_for_session("sess-1")
    check(found is not None and found[0] == t2["id"], "pending run found for session")
    task_store.set_run_verdict(t2["id"], "sess-1", verdict="pass", reason="exit 0", verdict_kind="script")
    found2 = task_store.find_pending_run_for_session("sess-1")
    check(found2 is None, "graded run no longer pending")
    t2b = task_store.get(t2["id"])
    check(t2b["recent_runs"][0]["verdict"] == "pass", "verdict persisted on run")


def test_trigger_store():
    print("T6 schedule-recurring trigger registers + advances on fire")
    task = {
        "id": "task-sched", "cwd": "/tmp/p", "node_id": "primary",
        "trigger": {"kind": "schedule", "config": {"mode": "recurring", "interval_seconds": 60}},
    }
    created = task_trigger_store.register_for_task(task)
    check(len(created) == 1 and created[0]["kind"] == "schedule_recurring", "recurring trigger created")
    recs = task_trigger_store.list_for_task("task-sched")
    check(len(recs) == 1, "one trigger registered")
    from datetime import datetime, timedelta
    past = (datetime.now() - timedelta(seconds=120))
    due = task_trigger_store.due(past)
    # Force due by backdating fire_at
    raw = task_trigger_store._read()
    raw["triggers"][0]["fire_at"] = past.isoformat()
    task_trigger_store._write(raw)
    task_trigger_store._data_cache = None
    check(len(task_trigger_store.due(datetime.now())) == 1, "backdated trigger is due")
    task_trigger_store.mark_fired(recs[0]["id"], datetime.now())
    check(len(task_trigger_store.list_for_task("task-sched")) == 1, "recurring trigger survives fire (advanced)")

    print("T7 schedule-once trigger is deleted after fire")
    task2 = {
        "id": "task-once", "cwd": "/tmp/p", "node_id": "primary",
        "trigger": {"kind": "schedule", "config": {"mode": "once", "fire_at": datetime.now().isoformat()}},
    }
    task_trigger_store.register_for_task(task2)
    once = task_trigger_store.list_for_task("task-once")[0]
    task_trigger_store.mark_fired(once["id"], datetime.now())
    check(len(task_trigger_store.list_for_task("task-once")) == 0, "once trigger deleted after fire")

    print("T8 unregister_task wipes triggers")
    check(len(task_trigger_store.list_for_task("task-sched")) == 1, "sched trigger present before unregister")
    task_trigger_store.unregister_task("task-sched")
    check(len(task_trigger_store.list_for_task("task-sched")) == 0, "unregister wipes triggers")

    print("T9 manual trigger registers no records")
    created = task_trigger_store.register_for_task(
        {"id": "task-man", "cwd": "/tmp/p", "trigger": {"kind": "manual", "config": {}}})
    check(created == [], "manual trigger creates no records")


def test_script_runner():
    print("T10 run_script uses argv (never a shell) — metacharacters stay literal")
    res = task_script.run_script({"command": ["python3", "-c", "import sys; print(sys.argv[1])", "a;rm -rf /"]})
    check(res.ok, "argv script exited 0")
    check("a;rm -rf /" in res.stdout, "shell metacharacters printed literally (no shell)")

    print("T11 non-zero exit propagated")
    res = task_script.run_script({"command": ["python3", "-c", "raise SystemExit(3)"]})
    check(res.exit_code == 3 and not res.ok, "exit code 3 propagated, ok=False")


def test_assessor():
    print("T12 assessment none → skipped")
    v, _, k = task_assessor.assess(
        {"cwd": "/tmp", "scripts": {"pre": [], "post": []},
         "assessment": {"kind": "none", "config": {}}}, "s")
    check(v == "skipped" and k == "none", "none assessment → skipped")

    print("T13 script assessment exit 0 → pass, non-zero → fail")
    v, _, _ = task_assessor.assess(
        {"cwd": "/tmp", "scripts": {"pre": [], "post": []},
         "assessment": {"kind": "script", "config": {"command": ["true"]}}}, "s")
    check(v == "pass", "exit 0 → pass")
    v, _, _ = task_assessor.assess(
        {"cwd": "/tmp", "scripts": {"pre": [], "post": []},
         "assessment": {"kind": "script", "config": {"command": ["false"]}}}, "s")
    check(v == "fail", "exit 1 → fail")

    print("T14 script assessment JSON {pass:false} → fail even with exit 0")
    v, r, _ = task_assessor.assess(
        {"cwd": "/tmp", "scripts": {"pre": [], "post": []},
         "assessment": {"kind": "script",
           "config": {"command": ["python3", "-c", "print('{\"pass\": false, \"reason\": \"nope\"}')"]}}}, "s")
    check(v == "fail" and "nope" in r, "JSON verdict overrides exit code, reason carried")

    print("T15 llm_judge assessment → error (no one-shot LLM primitive yet)")
    v, r, k = task_assessor.assess(
        {"cwd": "/tmp", "scripts": {"pre": [], "post": []},
         "assessment": {"kind": "llm_judge", "config": {"criteria": "be good"}}}, "s")
    check(v == "error" and k == "llm_judge", "llm_judge → error verdict")
    check("not yet wired" in r, "llm_judge reason explains the missing primitive")


class _StubCoord:
    def __init__(self):
        self.broadcasts = []

    async def broadcast_global(self, _type, _data):
        self.broadcasts.append(_type)


def test_scheduler_trigger_dispatch(monkeypatch_launch):
    print("T16 detector exit 0 launches task; non-zero skips; both advance")
    task = {
        "id": "task-det", "cwd": "/tmp/p", "node_id": "primary",
        "trigger": {"kind": "script",
                    "config": {"detector": {"command": ["true"]}, "poll_interval_seconds": 60}},
    }
    task_trigger_store.register_for_task(task)
    rec = task_trigger_store.list_for_task("task-det")[0]
    # Backdate so it's due now.
    from datetime import datetime, timedelta
    raw = task_trigger_store._read()
    for t in raw["triggers"]:
        if t["task_id"] == "task-det":
            t["fire_at"] = (datetime.now() - timedelta(seconds=120)).isoformat()
    task_trigger_store._write(raw)
    task_trigger_store._data_cache = None

    launched = asyncio.run(Scheduler(_StubCoord()).fire_task_triggers(datetime.now()))
    check(launched == 1, "exit-0 detector launched exactly one task")
    check(monkeypatch_launch["calls"] == ["task-det"], "launch_task called for the task")
    # After firing, the poll window advanced → no longer due immediately.
    check(len(task_trigger_store.due(datetime.now())) == 0, "poll window advanced after fire")

    print("T17 non-firing detector does not launch but advances (backoff)")
    monkeypatch_launch["calls"] = []
    task2 = {
        "id": "task-det2", "cwd": "/tmp/p", "node_id": "primary",
        "trigger": {"kind": "script",
                    "config": {"detector": {"command": ["false"]}, "poll_interval_seconds": 60}},
    }
    task_trigger_store.register_for_task(task2)
    raw = task_trigger_store._read()
    from datetime import datetime as _dt, timedelta as _td
    for t in raw["triggers"]:
        if t["task_id"] == "task-det2":
            t["fire_at"] = (_dt.now() - _td(seconds=120)).isoformat()
    task_trigger_store._write(raw)
    task_trigger_store._data_cache = None
    launched = asyncio.run(Scheduler(_StubCoord()).fire_task_triggers(_dt.now()))
    check(launched == 0, "non-firing detector launched nothing")
    check(monkeypatch_launch["calls"] == [], "launch_task not called")
    check(len(task_trigger_store.list_for_task("task-det2")) == 1, "trigger survives (advanced)")


def main() -> int:
    test_store_model()
    test_trigger_store()
    test_script_runner()
    test_assessor()

    launch_state = {"calls": []}

    async def _fake_launch(task_id, *, coordinator, source="manual", **_kw):
        launch_state["calls"].append(task_id)
        return {"task_id": task_id, "session_id": "stub", "queue_item_id": "q", "reused": False}

    original = task_runner.launch_task
    task_runner.launch_task = _fake_launch
    # scheduler.fire_task_triggers imports task_runner by name and reads
    # launch_task at call time, so the module attr patch is honored.
    try:
        test_scheduler_trigger_dispatch(launch_state)
    finally:
        task_runner.launch_task = original

    print()
    if failures:
        print(f"FAILURES ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
