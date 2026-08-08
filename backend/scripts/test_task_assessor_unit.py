"""Dedicated unit-coverage owner for task_assessor.py.

test_task_pipeline.test_assessor already exercises the happy paths
(none/script/llm_judge success, fail, unparseable, empty-output). This
file closes the remaining gaps: the pure helpers' error/edge branches,
_extract_run_text, the post-script failure logging path, the unknown-kind
fallthrough, _assess_completed_turn's store wiring, and bind()/turn-complete
dispatch.

All hermetic: isolated BETTER_AGENT_HOME, no provider CLI, no real model
turns. The LLM-judge error branches are driven with a stubbed
run_session_headless (same pattern as test_task_pipeline) so they never
spend a provider call.
"""
import os
import sys
from types import ModuleType
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.anyio

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home
_test_home.isolate("bc-test-task-assessor-unit-")

import task_assessor  # noqa: E402
from event_bus import BusEvent, bus  # noqa: E402
from stores import task_store  # noqa: E402
import event_bus_subscribers  # noqa: E402
import headless_admission  # noqa: E402
import session_manager  # noqa: E402


class _FakeRes:
    """Minimal stand-in for task_script.ScriptResult: _parse_script_verdict
    only reads stdout, ok, stderr, exit_code."""

    def __init__(self, stdout="", stderr="", ok=True, exit_code=0):
        self.stdout = stdout
        self.stderr = stderr
        self.ok = ok
        self.exit_code = exit_code


def test_parse_script_verdict_error_branches():
    # res=None -> the assessment script never produced a result.
    verdict, reason = task_assessor._parse_script_verdict(None)
    assert verdict == "error"
    assert "did not run" in reason

    # Non-empty stdout that is not JSON: JSONDecodeError path, payload=None,
    # verdict falls back to exit-code semantics.
    assert task_assessor._parse_script_verdict(_FakeRes(stdout="not json", ok=True)) == ("pass", "exit 0")
    assert task_assessor._parse_script_verdict(
        _FakeRes(stdout="not json", stderr="boom", ok=False, exit_code=3)
    ) == ("fail", "boom")

    # Valid JSON object that omits "pass": the {pass, reason} override only
    # applies when "pass" is present, so this still falls back to exit code.
    assert task_assessor._parse_script_verdict(
        _FakeRes(stdout='{"reason": "ambiguous"}', ok=True)
    ) == ("pass", "exit 0")


def test_parse_judge_json_empty_and_object_without_pass():
    assert task_assessor._parse_judge_json("") is None
    assert task_assessor._parse_judge_json("prose with no braces at all") is None
    # JSON object lacking a "pass" key is not a usable verdict.
    assert task_assessor._parse_judge_json('{"reason": "x"}') is None
    ok, reason = task_assessor._parse_judge_json('{"pass": true, "reason": "y"}')
    assert ok is True and reason == "y"


def test_extract_run_text_branches(monkeypatch):
    # Dict session -> extracted assistant text flows through.
    monkeypatch.setattr(session_manager.manager, "get", lambda sid: {"some": "sess"})
    monkeypatch.setattr(event_bus_subscribers, "_last_assistant_text", lambda s: "captured")
    assert task_assessor._extract_run_text("s") == "captured"

    # Non-dict (absent) session -> "".
    monkeypatch.setattr(session_manager.manager, "get", lambda sid: None)
    assert task_assessor._extract_run_text("s") == ""

    # The lazy import failing (helper module lacks the symbol) -> "".
    fake = ModuleType("event_bus_subscribers")  # no _last_assistant_text attr
    with patch.dict(sys.modules, {"event_bus_subscribers": fake}):
        assert task_assessor._extract_run_text("s") == ""


async def test_llm_judge_error_branches(monkeypatch):
    task = {"goal": "g", "assessment": {"kind": "llm_judge", "config": {"criteria": "c"}}}
    monkeypatch.setattr(task_assessor, "_extract_run_text", lambda sid: "agent output")

    async def _raise(*_a, **_kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(headless_admission, "run_session_headless", _raise)
    verdict, reason = await task_assessor._llm_judge(task, "s")
    assert verdict == "error" and "call failed" in reason

    async def _err(*_a, **_kw):
        return {"is_error": True}

    monkeypatch.setattr(headless_admission, "run_session_headless", _err)
    verdict, reason = await task_assessor._llm_judge(task, "s")
    assert verdict == "error" and "returned an error" in reason


async def test_assess_unknown_kind_and_post_script_failure():
    # Unknown assessment kind falls through to skipped, echoing the kind.
    verdict, reason, kind = await task_assessor.assess(
        {"cwd": "/tmp", "scripts": {"post": []},
         "assessment": {"kind": "mystery", "config": {}}}, "s")
    assert (verdict, kind) == ("skipped", "mystery")

    # A failing post-script is logged best-effort; assessment still proceeds.
    verdict, _, kind = await task_assessor.assess(
        {"cwd": "/tmp", "scripts": {"post": [{"command": ["false"]}]},
         "assessment": {"kind": "none", "config": {}}}, "s")
    assert (verdict, kind) == ("skipped", "none")

    # A succeeding post-script logs nothing; assessment still proceeds.
    verdict, _, kind = await task_assessor.assess(
        {"cwd": "/tmp", "scripts": {"post": [{"command": ["true"]}]},
         "assessment": {"kind": "none", "config": {}}}, "s")
    assert (verdict, kind) == ("skipped", "none")


class _FakeCoord:
    def __init__(self):
        self.broadcasts = []

    async def broadcast_global(self, event_type, _data):
        self.broadcasts.append(event_type)


def _run_for(task: dict, session_id: str) -> dict:
    return next(r for r in task["recent_runs"] if r["session_id"] == session_id)


async def test_assess_completed_turn_paths(monkeypatch):
    coord = _FakeCoord()
    task = task_store.create(cwd="/tmp/proj", name="t", prompt="p",
                             assessment={"kind": "none", "config": {}})

    # No pending run for this session -> no-op, no broadcast.
    await task_assessor._assess_completed_turn(coord, "no-pending-session")
    assert coord.broadcasts == []

    # Each remaining branch needs its own still-pending run: once a run is
    # assessed its verdict leaves "pending", so find_pending_run_for_session
    # stops returning it.

    # Pending run -> assessed + verdict recorded + broadcast.
    task_store.record_run(task["id"], "sess-ok", queue_item_id="q1")
    await task_assessor._assess_completed_turn(coord, "sess-ok")
    assert _run_for(task_store.get(task["id"]), "sess-ok")["verdict"] == "skipped"
    assert coord.broadcasts == ["tasks_changed"]

    # Pending run exists but the task vanished -> no-op.
    task_store.record_run(task["id"], "sess-gone", queue_item_id="q2")
    monkeypatch.setattr(task_store, "get", lambda _tid: None)
    coord.broadcasts = []
    await task_assessor._assess_completed_turn(coord, "sess-gone")
    assert coord.broadcasts == []
    monkeypatch.undo()

    # assess() raising -> recorded as an error verdict, still broadcast.
    task_store.record_run(task["id"], "sess-boom", queue_item_id="q3")

    async def _boom(_task, _sid):
        raise RuntimeError("assess exploded")

    monkeypatch.setattr(task_assessor, "assess", _boom)
    coord.broadcasts = []
    await task_assessor._assess_completed_turn(coord, "sess-boom")
    assert _run_for(task_store.get(task["id"]), "sess-boom")["verdict"] == "error"
    assert coord.broadcasts == ["tasks_changed"]


async def test_bind_and_turn_complete_dispatch(monkeypatch):
    coord = _FakeCoord()
    try:
        task_assessor.bind(coord)
        subs = [s for s in bus._subs if s.name == "task_assessor"]
        assert len(subs) == 1 and subs[0].pattern == "lifecycle.turn_complete"

        # Re-binding replaces the subscription rather than duplicating.
        task_assessor.bind(coord)
        assert len([s for s in bus._subs if s.name == "task_assessor"]) == 1

        # Firing turn_complete with no pending run dispatches the handler
        # cleanly (early-return), so no broadcast reaches the coordinator.
        event = BusEvent(type="lifecycle.turn_complete", root_id="r",
                         sid="no-such-session", payload={}, persist=False)
        await bus.publish(event)
        assert coord.broadcasts == []

        # A failure inside the handler is swallowed by the turn-complete
        # wrapper (it logs, never propagates), so publish still completes.
        def _explode(_sid):
            raise RuntimeError("find blew up")

        monkeypatch.setattr(task_store, "find_pending_run_for_session", _explode)
        event = BusEvent(type="lifecycle.turn_complete", root_id="r",
                         sid="boom-session", payload={}, persist=False)
        await bus.publish(event)
        assert coord.broadcasts == []
    finally:
        bus.unsubscribe("task_assessor")
