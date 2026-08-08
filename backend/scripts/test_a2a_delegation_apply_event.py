"""Render-tree convergence for A2A delegation: the worker panel it
projects (panel_kind="a2a") MUST go through the same
`OrchestrationStrategy.apply_event` funnel as every other worker panel
— worker_start / worker_event / worker_complete, uuid-deduped, journaled
to events.jsonl. Mirrors
scripts/test_apply_event_unified.py::test_worker_event_routes_to_existing_panel_owner.

Isolated via _test_home.isolate() before any backend import."""
from __future__ import annotations

import asyncio
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home
# _mk_session() below creates sessions with orchestration_mode="manager",
# which — since "Add selective installation runtime profiles" — requires an
# active, bootstrap-ready installation profile with integrations enabled.
# `isolate_installed()` seeds one; bare `isolate()` would make every such
# create() raise IncompatibleOrchestrationMode.
_test_home.isolate_installed("bc-test-a2a-delegation-")

from event_ingester import event_ingester  # noqa: E402
from event_journal import event_journal_writer  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402

import a2a.delegation as delegation  # noqa: E402

_AGENT_RECORD = {
    "id": "a2a_test_agent",
    "name": "Weather Agent",
    "base_url": "https://agent.example.com/a2a",
    "auth_header_name": "",
    "auth_secret": "",
}


def _mk_session() -> str:
    sess = session_manager.create(
        name="t", model="sonnet", cwd="/tmp", orchestration_mode="manager", source="cli",
    )
    return sess["id"]


async def _fake_stream_completed(**_kwargs):
    for result in [
        {"status": {"state": "working"}},
        {"artifact": {"artifactId": "a1", "parts": [{"type": "text", "text": "partial"}]},
         "append": False},
        {"status": {"state": "completed",
                    "message": {"parts": [{"type": "text", "text": "done"}]}}},
    ]:
        yield result


async def _fake_stream_failed(**_kwargs):
    for result in [
        {"status": {"state": "working"}},
        {"status": {"state": "failed",
                    "message": {"parts": [{"type": "text", "text": "remote blew up"}]}}},
    ]:
        yield result


def test_a2a_delegation_projects_worker_panel_and_completes(monkeypatch):
    monkeypatch.setattr(delegation, "stream_message", _fake_stream_completed)
    sid = _mk_session()

    delegation_id, msg_id = delegation.start_a2a_delegation(
        app_session_id=sid, agent_record=_AGENT_RECORD, instructions="what's the weather",
    )
    asyncio.run(delegation._run_a2a_delegation_async(
        app_session_id=sid, msg_id=msg_id, delegation_id=delegation_id,
        agent_record=_AGENT_RECORD, instructions="what's the weather",
    ))

    event_journal_writer.barrier_sync(sid)
    fresh = session_manager.get(sid)
    msg = next(m for m in fresh["messages"] if m["id"] == msg_id)
    panel = next(w for w in msg["workers"] if w["delegation_id"] == delegation_id)

    assert panel["panel_kind"] == "a2a"
    assert panel["run_mode"] == "a2a"
    assert len(panel["events"]) == 3
    assert panel["success"] is True
    assert panel.get("error") is None

    rows, _, _ = event_ingester.read_events(sid)
    types = [r.get("type") for r in rows]
    assert types.count("worker_start") == 1
    assert types.count("worker_event") == 3
    assert types.count("worker_complete") == 1
    for row in rows:
        if row.get("type") in ("worker_start", "worker_event", "worker_complete"):
            assert row.get("msg_id") == msg_id


def test_a2a_delegation_reports_remote_failure_truthfully(monkeypatch):
    monkeypatch.setattr(delegation, "stream_message", _fake_stream_failed)
    sid = _mk_session()

    delegation_id, msg_id = delegation.start_a2a_delegation(
        app_session_id=sid, agent_record=_AGENT_RECORD, instructions="what's the weather",
    )
    asyncio.run(delegation._run_a2a_delegation_async(
        app_session_id=sid, msg_id=msg_id, delegation_id=delegation_id,
        agent_record=_AGENT_RECORD, instructions="what's the weather",
    ))

    fresh = session_manager.get(sid)
    msg = next(m for m in fresh["messages"] if m["id"] == msg_id)
    panel = next(w for w in msg["workers"] if w["delegation_id"] == delegation_id)

    assert panel["success"] is False
    assert panel["error"] == "remote blew up"


def test_a2a_delegation_idempotent_reapply_does_not_duplicate_panel_events(monkeypatch):
    """Re-applying the same worker_event uuid (e.g. a crash-recovery
    replay of events.jsonl) must not duplicate panel entries — the same
    dedup guarantee every other apply_event caller relies on."""
    monkeypatch.setattr(delegation, "stream_message", _fake_stream_completed)
    sid = _mk_session()

    delegation_id, msg_id = delegation.start_a2a_delegation(
        app_session_id=sid, agent_record=_AGENT_RECORD, instructions="what's the weather",
    )
    asyncio.run(delegation._run_a2a_delegation_async(
        app_session_id=sid, msg_id=msg_id, delegation_id=delegation_id,
        agent_record=_AGENT_RECORD, instructions="what's the weather",
    ))
    first = session_manager.get(sid)
    first_msg = next(m for m in first["messages"] if m["id"] == msg_id)
    first_panel = next(w for w in first_msg["workers"] if w["delegation_id"] == delegation_id)
    first_count = len(first_panel["events"])

    # Re-run the SAME scripted stream against the SAME delegation_id/msg_id —
    # every inner-event uuid is deterministic (delegation_id + index /
    # artifact id), so this simulates a replay, not a second live run.
    asyncio.run(delegation._run_a2a_delegation_async(
        app_session_id=sid, msg_id=msg_id, delegation_id=delegation_id,
        agent_record=_AGENT_RECORD, instructions="what's the weather",
    ))
    second = session_manager.get(sid)
    second_msg = next(m for m in second["messages"] if m["id"] == msg_id)
    second_panel = next(w for w in second_msg["workers"] if w["delegation_id"] == delegation_id)
    assert len(second_panel["events"]) == first_count
