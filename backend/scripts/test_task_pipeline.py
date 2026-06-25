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
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-task-pipeline-")

from stores import task_store  # noqa: E402
from stores import task_output_store  # noqa: E402
from stores import task_trigger_store  # noqa: E402
from event_bus import BusEvent, bus  # noqa: E402
from event_bus_subscribers import bind_task_turn_end_triggers  # noqa: E402
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
        session_type="provisioned_fork",
        model="gpt-5.5",
        provider_id="openai",
        reasoning_effort="high",
    )
    check(rec["goal"] == "all green", "goal stored")
    check(rec["trigger"]["kind"] == "schedule", "trigger kind stored")
    check(rec["trigger"]["config"]["interval_seconds"] == 60, "trigger config stored")
    check(rec["assessment"]["kind"] == "script", "assessment kind stored")
    check(rec["scripts"]["pre"][0]["command"] == ["echo", "hi"], "pre-script stored")
    check(rec["session_type"] == "provisioned_fork", "session type stored")
    check(rec["model"] == "gpt-5.5", "model stored")
    check(rec["provider_id"] == "openai", "provider stored")
    check(rec["reasoning_effort"] == "high", "reasoning effort stored")

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

    print("T3b turn-end trigger requires singleton")
    try:
        task_store.create(
            cwd="/tmp/proj", name="bad3", prompt="x",
            trigger={"kind": "turn_end", "config": {}},
        )
        check(False, "non-singleton turn-end trigger should raise")
    except ValueError:
        check(True, "non-singleton turn-end trigger raises")

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

    print("T5b task outputs publish/list/content path")
    out = task_output_store.publish(
        task_id=t2["id"],
        task_cwd="/tmp/proj2",
        title="Daily report",
        kind="html_report",
        content_type="text/html",
        content="<html><body>ok</body></html>",
        session_id="sess-1",
    )
    listed = task_output_store.list_for_task(t2["id"])
    path, media_type = task_output_store.content_path(t2["id"], out["id"])
    check(listed[0]["id"] == out["id"], "output listed newest first")
    check(media_type == "text/html", "content type preserved")
    check(path.read_text(encoding="utf-8").startswith("<html>"), "output content served from durable file")


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

    print("T9b turn-end trigger registers and matches outcome/reason")
    created = task_trigger_store.register_for_task({
        "id": "task-turn-end",
        "cwd": "/tmp/p",
        "trigger": {
            "kind": "turn_end",
            "config": {
                "outcomes": ["complete"],
                "reasons": ["success"],
                "provider_kind": "codex",
            },
        },
    })
    check(len(created) == 1 and created[0]["kind"] == "turn_end",
          "turn-end trigger creates one armed record")
    matched = task_trigger_store.matching_turn_end(
        "lifecycle.turn_complete", "success",
    )
    check([item["task_id"] for item in matched] == ["task-turn-end"],
          "matching completion finds the trigger")
    check(task_trigger_store.matching_turn_end(
        "lifecycle.turn_complete", "error",
    ) == [], "non-matching reason is ignored")
    check(task_trigger_store.due() == [], "turn-end triggers never enter scheduler due queue")


async def test_turn_end_receipt():
    print("T9c turn-end receipts are scoped, durable, idempotent, and revalidated")
    task = task_store.create(
        cwd="/tmp/turn-end",
        name="Codex render audit",
        prompt="Inspect the completed turn",
        singleton=True,
        trigger={
            "kind": "turn_end",
            "config": {
                "outcomes": ["complete"],
                "reasons": ["success"],
                "provider_kind": "codex",
            },
        },
    )
    task_trigger_store.register_for_task(task)

    original_get_fields = __import__("event_bus_subscribers").session_manager.get_fields
    original_launch = task_runner.launch_task
    calls = []

    async def _fake_launch(task_id, **kwargs):
        calls.append((task_id, kwargs))
        return {"task_id": task_id}

    __import__("event_bus_subscribers").session_manager.get_fields = lambda *_a: {
        "cwd": "/tmp/turn-end",
        "node_id": "primary",
        "storage_scope": None,
    }
    task_runner.launch_task = _fake_launch
    event = BusEvent(
        type="lifecycle.turn_complete",
        root_id="root-1",
        sid="codex-session",
        payload={
            "reason": "success",
            "trace_id": "trace-1",
            "provider_kind": "codex",
        },
        persist=False,
    )
    try:
        bind_task_turn_end_triggers()
        await bus.publish(event)
        receipts = [item for item in task_trigger_store.due()
                    if item.get("kind") == "turn_end_once"]
        check(len(receipts) == 1,
              "bus completion writes one durable receipt")
        await bus.publish(event)
        receipts = [item for item in task_trigger_store.due()
                    if item.get("kind") == "turn_end_once"]
        check(len(receipts) == 1, "same bus completion is idempotent")

        launched = await Scheduler(object()).fire_task_triggers()
        check(launched == 1 and len(calls) == 1,
              "scheduler launches the durable receipt exactly once")
        check(calls[0][1].get("event_receipt_id") == receipts[0]["id"],
              "authoritative receipt is passed to the routine")
        check("prompt_override" not in calls[0][1],
              "scheduler does not carry stale task prompt or event context")
        check(calls[0][1]["client_id"].startswith("routine-event:"),
              "receipt supplies a deterministic launch id")

        event.payload["trace_id"] = "trace-2"
        await bus.publish(event)
        check(len([item for item in task_trigger_store.due()
                   if item.get("kind") == "turn_end_once"]) == 1,
              "a later bus completion writes a distinct receipt")
        task_store.update(task["id"], {"trigger": {"kind": "manual", "config": {}}})
        launched = await Scheduler(object()).fire_task_triggers()
        check(launched == 0 and len(calls) == 1,
              "updated trigger invalidates a stale receipt before launch")

        __import__("event_bus_subscribers").session_manager.get_fields = lambda *_a: {
            "cwd": "/tmp/turn-end",
            "node_id": "primary",
            "storage_scope": {"kind": "routine", "routine_id": "other-task"},
        }
        event.payload["trace_id"] = "trace-3"
        await bus.publish(event)
        check(not [item for item in task_trigger_store.due()
                   if item.get("kind") == "turn_end_once"],
              "routine-owned bus events cannot create feedback loops")
    finally:
        bus.unsubscribe("task_turn_end_triggers")
        __import__("event_bus_subscribers").session_manager.get_fields = original_get_fields
        task_runner.launch_task = original_launch


def test_turn_end_admission_races() -> None:
    print("T9d turn-end admission rejects stale task/source generations")
    trigger = {
        "kind": "turn_end",
        "config": {"outcomes": ["complete"], "reasons": ["success"]},
    }
    task = task_store.create(
        cwd="/tmp/turn-end-races", name="Race", prompt="original",
        singleton=True, trigger=trigger,
    )
    task_trigger_store.register_for_task(task)

    def enqueue(key: str) -> str:
        assert task_trigger_store.enqueue_turn_end(
            event_type="lifecycle.turn_complete", event_key=key,
            root_id="root", session_id="source", reason="success",
            timestamp=datetime.now().isoformat(), provider_kind=None,
            cwd=task["cwd"], node_id="primary",
        ) == 1
        return next(
            item["id"] for item in task_trigger_store.due()
            if item.get("kind") == "turn_end_once"
        )

    receipt = enqueue("update")
    status, snap, _ = task_trigger_store.event_launch_snapshot(receipt)
    check(status == "current" and snap.get("prompt") == "original",
          "launch snapshot reads current prompt")
    task_store.update(task["id"], {"prompt": "updated"})
    status, _ = task_trigger_store.claim_event_run(
        receipt, "session-update",
        expected_task_updated_at=str(snap.get("updated_at") or ""),
    )
    check(status == "stale", "prompt update invalidates admission snapshot")

    latest = task_store.get(task["id"])
    task_store.set_stopped(task["id"], True)
    status, _, _ = task_trigger_store.event_launch_snapshot(receipt)
    check(status == "stopped", "stop wins before admission")
    resumed = task_store.set_stopped(task["id"], False)
    task_trigger_store.register_for_task(resumed)
    status, _, _ = task_trigger_store.event_launch_snapshot(receipt)
    check(status == "missing", "same-config re-registration invalidates old receipt generation")

    retry_receipt = enqueue("retry")
    task_trigger_store.retry_later(retry_receipt)
    status, retry_task, _ = task_trigger_store.event_launch_snapshot(retry_receipt)
    check(status == "current", "retry preserves current receipt generation")
    status, _ = task_trigger_store.claim_event_run(
        retry_receipt, "session-retry",
        expected_task_updated_at=str(retry_task.get("updated_at") or ""),
    )
    check(status == "admitted", "retry can admit exactly its current generation")

    cancel_receipt = enqueue("cancel")
    task_trigger_store.mark_fired(cancel_receipt)
    status, _, _ = task_trigger_store.event_launch_snapshot(cancel_receipt)
    check(status == "missing", "receipt cancellation wins before admission")


def test_script_runner():
    print("T10 run_script uses argv (never a shell) — metacharacters stay literal")
    res = task_script.run_script({"command": ["python3", "-c", "import sys; print(sys.argv[1])", "a;rm -rf /"]})
    check(res.ok, "argv script exited 0")
    check("a;rm -rf /" in res.stdout, "shell metacharacters printed literally (no shell)")

    print("T11 non-zero exit propagated")
    res = task_script.run_script({"command": ["python3", "-c", "raise SystemExit(3)"]})
    check(res.exit_code == 3 and not res.ok, "exit code 3 propagated, ok=False")


class _StubJudge:
    def __init__(self, result, exc=None):
        self._result = result
        self._exc = exc
        self.calls = []

    async def run_headless(self, *, prompt, cwd=None, timeout=None, no_tools=False):
        self.calls.append(prompt)
        if self._exc:
            raise self._exc
        return self._result


async def test_assessor():
    print("T12 assessment none → skipped")
    v, _, k = await task_assessor.assess(
        {"cwd": "/tmp", "scripts": {"pre": [], "post": []},
         "assessment": {"kind": "none", "config": {}}}, "s")
    check(v == "skipped" and k == "none", "none assessment → skipped")

    print("T13 script assessment exit 0 → pass, non-zero → fail")
    v, _, _ = await task_assessor.assess(
        {"cwd": "/tmp", "scripts": {"pre": [], "post": []},
         "assessment": {"kind": "script", "config": {"command": ["true"]}}}, "s")
    check(v == "pass", "exit 0 → pass")
    v, _, _ = await task_assessor.assess(
        {"cwd": "/tmp", "scripts": {"pre": [], "post": []},
         "assessment": {"kind": "script", "config": {"command": ["false"]}}}, "s")
    check(v == "fail", "exit 1 → fail")

    print("T14 script assessment JSON {pass:false} → fail even with exit 0")
    v, r, _ = await task_assessor.assess(
        {"cwd": "/tmp", "scripts": {"pre": [], "post": []},
         "assessment": {"kind": "script",
           "config": {"command": ["python3", "-c", "print('{\"pass\": false, \"reason\": \"nope\"}')"]}}}, "s")
    check(v == "fail" and "nope" in r, "JSON verdict overrides exit code, reason carried")

    print("T15 llm_judge: pass verdict parsed from model reply")
    judge = _StubJudge({"result": '{"pass": true, "reason": "all criteria met"}', "is_error": False})
    orig_judge = task_assessor._resolve_judge_provider
    orig_text = task_assessor._extract_run_text
    task_assessor._resolve_judge_provider = lambda cfg: judge
    task_assessor._extract_run_text = lambda sid: "the agent did the thing"
    try:
        v, r, k = await task_assessor.assess(
            {"cwd": "/tmp", "goal": "do thing", "scripts": {"pre": [], "post": []},
             "assessment": {"kind": "llm_judge", "config": {"criteria": "thing done"}}}, "s")
        check(v == "pass" and k == "llm_judge" and "criteria met" in r, "llm_judge → pass with reason")
        check(len(judge.calls) == 1 and "GOAL" in judge.calls[0], "judge prompt built from goal+criteria+output")

        print("T16 llm_judge: fail verdict")
        judge2 = _StubJudge({"result": '{"pass": false, "reason": "missing X"}', "is_error": False})
        task_assessor._resolve_judge_provider = lambda cfg: judge2
        v, r, _ = await task_assessor.assess(
            {"cwd": "/tmp", "goal": "g", "scripts": {"pre": [], "post": []},
             "assessment": {"kind": "llm_judge", "config": {"criteria": "c"}}}, "s")
        check(v == "fail" and "missing X" in r, "llm_judge → fail with reason")

        print("T17 llm_judge: unparseable reply → error (fail closed)")
        judge3 = _StubJudge({"result": "I think it mostly worked", "is_error": False})
        task_assessor._resolve_judge_provider = lambda cfg: judge3
        v, r, _ = await task_assessor.assess(
            {"cwd": "/tmp", "goal": "g", "scripts": {"pre": [], "post": []},
             "assessment": {"kind": "llm_judge", "config": {"criteria": "c"}}}, "s")
        check(v == "error" and "unparseable" in r, "llm_judge unparseable → error (never silently pass)")

        print("T18 llm_judge: no run output captured → error")
        task_assessor._extract_run_text = lambda sid: ""
        v, r, _ = await task_assessor.assess(
            {"cwd": "/tmp", "goal": "g", "scripts": {"pre": [], "post": []},
             "assessment": {"kind": "llm_judge", "config": {"criteria": "c"}}}, "s")
        check(v == "error" and "no assistant output" in r, "llm_judge with empty output → error")
    finally:
        task_assessor._resolve_judge_provider = orig_judge
        task_assessor._extract_run_text = orig_text


class _StubCoord:
    def __init__(self):
        self.broadcasts = []

    async def broadcast_global(self, _type, _data):
        self.broadcasts.append(_type)


def test_scheduler_trigger_dispatch(monkeypatch_launch):
    print("T19 detector exit 0 launches task; non-zero skips; both advance")
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

    print("T20 non-firing detector does not launch but advances (backoff)")
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


def test_routine_prompt_source_spec():
    print("T21 routine prompt treats description as source spec")
    prompt = task_runner._routine_prompt(
        {
            "name": "Morning PR review",
            "description": "Review open PRs",
            "goal": "Summarize risky failures",
        },
        "Every morning, inspect PRs and create follow-up sessions for risky items.",
    )
    check("saved routine: Morning PR review" in prompt, "routine name included")
    check("source spec" in prompt, "description is marked as source spec")
    check("Review open PRs" in prompt, "summary included")
    check("Summarize risky failures" in prompt, "success criteria included")
    check("create follow-up sessions" in prompt, "natural-language description included")
    check("routine output tool/SDK" in prompt, "output publishing instruction included")


def main() -> int:
    test_store_model()
    test_trigger_store()
    asyncio.run(test_turn_end_receipt())
    test_turn_end_admission_races()
    test_script_runner()
    asyncio.run(test_assessor())

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
    test_routine_prompt_source_spec()

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
