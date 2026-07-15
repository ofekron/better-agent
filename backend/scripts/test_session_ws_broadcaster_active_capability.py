from __future__ import annotations

import asyncio
import os
import sys

import _test_home

_test_home.isolate("bc-test-ws-active-capability-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import runtime_ownership  # noqa: E402
runtime_ownership.register_current_process_writer()

from session_manager import manager as session_manager  # noqa: E402
import session_ws_broadcaster  # noqa: E402
from session_ws_broadcaster import SessionWSBroadcaster  # noqa: E402


class StubCoordinator:
    def __init__(self, captured: list[dict]) -> None:
        self._captured = captured

    def schedule_global(self, event_type: str, data: dict, **_kwargs) -> None:
        self._captured.append({"type": event_type, "data": data})


def _drive(broadcaster: SessionWSBroadcaster, sid: str, change: dict) -> None:
    """Fire one change through the broadcaster with a real bound event
    loop so `_dispatch`'s `asyncio.get_running_loop()` succeeds instead
    of silently dropping the frame."""
    loop = asyncio.new_event_loop()
    broadcaster.bind(loop)
    asyncio.set_event_loop(loop)
    try:
        broadcaster.on_change(sid, change)
        loop.run_until_complete(asyncio.sleep(0))
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def test_active_capability_changes_emit_metadata_patch() -> None:
    captured: list[dict] = []

    sess = session_manager.create(name="active-capability", cwd="/tmp")
    sid = sess["id"]
    session_manager.add_active_capability(sid, "ofek.testape:testape")

    broadcaster = SessionWSBroadcaster(StubCoordinator(captured))
    _drive(broadcaster, sid, {
        "kind": "active_capability_added",
        "capability_id": "ofek.testape:testape",
    })

    assert captured == [{
        "type": "session_metadata_updated",
        "data": {
            "session_id": sid,
            "patch": {"active_capability_ids": ["ofek.testape:testape"]},
            "originated_by": None,
        },
    }]


def test_last_opened_emits_metadata_patch() -> None:
    captured: list[dict] = []
    broadcaster = SessionWSBroadcaster(StubCoordinator(captured))

    _drive(broadcaster, "sid-1", {
        "kind": "last_opened_set",
        "at": "2026-06-29T11:22:33Z",
    })

    assert captured == [{
        "type": "session_metadata_updated",
        "data": {
            "session_id": "sid-1",
            "patch": {"last_opened_at": "2026-06-29T11:22:33Z"},
            "originated_by": None,
        },
    }]


def test_journal_event_projected_emits_messages_delta() -> None:
    captured: list[dict] = []
    broadcaster = SessionWSBroadcaster(StubCoordinator(captured))
    msg = {"id": "msg-1", "content": "updated"}

    _drive(broadcaster, "sid-1", {
        "kind": "journal_event_projected",
        "msg_id": "msg-1",
        "msg": msg,
    })

    assert captured == [{
        "type": "messages_delta",
        "data": {
            "app_session_id": "sid-1",
            "messages": [msg],
        },
    }]


def test_journal_event_projected_compacts_render_events() -> None:
    captured: list[dict] = []
    broadcaster = SessionWSBroadcaster(StubCoordinator(captured))
    msg = {
        "id": "msg-1",
        "content": "updated",
        "events": [{"type": "agent_message", "data": {"uuid": "ev-1"}}],
        "workers": [
            {
                "delegation_id": "d1",
                "worker_session_id": "w1",
                "events": [{"type": "agent_message", "data": {"uuid": "worker-ev-1"}}],
                "success": True,
            },
        ],
    }

    _drive(broadcaster, "sid-1", {
        "kind": "journal_event_projected",
        "msg_id": "msg-1",
        "msg": msg,
    })

    payload = captured[0]["data"]["messages"][0]
    assert captured[0]["type"] == "messages_delta"
    assert payload["id"] == "msg-1"
    assert payload["content"] == "updated"
    assert isinstance(payload["omitted_payloads"]["events"]["revision"], str)
    assert "events" not in payload
    assert "events" not in payload["workers"][0]
    assert payload["workers"][0]["success"] is True
    assert msg["events"][0]["data"]["uuid"] == "ev-1"
    assert msg["workers"][0]["events"][0]["data"]["uuid"] == "worker-ev-1"


def test_message_ownership_resolved_keeps_render_events() -> None:
    captured: list[dict] = []
    broadcaster = SessionWSBroadcaster(StubCoordinator(captured))
    msg = {
        "id": "msg-1",
        "events": [{"type": "agent_message", "data": {"uuid": "ev-1"}}],
    }

    _drive(broadcaster, "sid-1", {
        "kind": "message_ownership_resolved",
        "msg": msg,
    })

    assert captured == [{
        "type": "messages_delta",
        "data": {
            "app_session_id": "sid-1",
            "messages": [msg],
        },
    }]


def test_internal_worker_changes_do_not_warn_or_dispatch() -> None:
    captured: list[dict] = []
    seen: list[object] = []

    original_warning = session_ws_broadcaster.logger.warning
    session_ws_broadcaster.logger.warning = lambda *args, **kwargs: seen.append(args)
    try:
        broadcaster = SessionWSBroadcaster(StubCoordinator(captured))
        broadcaster.on_change("sid-1", {"kind": "worker_panel_event"})
        broadcaster.on_change("sid-1", {"kind": "delegate_fork_created"})
    finally:
        session_ws_broadcaster.logger.warning = original_warning

    assert captured == []
    assert seen == []


if __name__ == "__main__":
    test_active_capability_changes_emit_metadata_patch()
    test_last_opened_emits_metadata_patch()
    test_journal_event_projected_emits_messages_delta()
    test_journal_event_projected_compacts_render_events()
    test_message_ownership_resolved_keeps_render_events()
    test_internal_worker_changes_do_not_warn_or_dispatch()
    print("ok")
