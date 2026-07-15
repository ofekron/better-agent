from __future__ import annotations

import inspect
import os
import re
import shutil
import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import _test_home

TMP_HOME = _test_home.isolate("bc-test-session-ws-state-")

import runtime_ownership
runtime_ownership.register_current_process_writer()

from event_bus import BusEvent
from event_bus_subscribers import _session_change_from_event
from session_manager import manager as session_manager
from session_ws_broadcaster import (
    SessionWSBroadcaster,
    _INTERNAL_KINDS,
    _METADATA_KINDS,
)


class Coordinator:
    async def broadcast_global(self, _event_type: str, _data: dict) -> None:
        return None


def _broadcaster_frames(change: dict, tabs: int = 1) -> list[dict]:
    frames: list[dict] = []
    for _ in range(tabs):
        broadcaster = SessionWSBroadcaster(Coordinator())
        broadcaster._dispatch = frames.append
        broadcaster.on_change("sid", change)
    return frames


def test_direct_event_kind_is_authoritative_and_mismatch_is_visible() -> None:
    event = BusEvent(
        type="session.parent_deleted",
        root_id="root",
        sid="sid",
        payload={"kind": "wrong", "value": 1},
        persist=False,
    )
    with mock.patch("event_bus_subscribers.logger.error") as error:
        change = _session_change_from_event(event)
    assert change == {"kind": "parent_deleted", "value": 1}
    error.assert_called_once()
    assert _broadcaster_frames(change) == []


def test_completed_at_emits_one_authoritative_delta_per_tab() -> None:
    session = session_manager.create(
        name="completed",
        cwd=TMP_HOME,
        orchestration_mode="native",
    )
    sid = session["id"]
    msg_id = "assistant-completed"
    session_manager.append_assistant_msg(
        sid,
        {"id": msg_id, "role": "assistant", "content": "done", "events": []},
    )
    changes: list[dict] = []
    session_manager.add_listener(
        lambda changed_sid, change: changes.append(change)
        if changed_sid == sid and change.get("kind") == "completed_at_set"
        else None,
    )
    session_manager.set_completed_at(sid, msg_id, "2026-07-10T12:00:00")
    assert len(changes) == 1
    frames = _broadcaster_frames(changes[0], tabs=2)
    assert len(frames) == 2
    assert all(frame["type"] == "messages_delta" for frame in frames)
    assert all(
        frame["data"]["messages"][0]["completed_at"]
        == "2026-07-10T12:00:00"
        for frame in frames
    )


def test_missing_completed_message_emits_no_delta() -> None:
    session = session_manager.create(
        name="missing",
        cwd=TMP_HOME,
        orchestration_mode="native",
    )
    changes: list[dict] = []
    session_manager.add_listener(
        lambda _sid, change: changes.append(change)
        if change.get("kind") == "completed_at_set"
        else None,
    )
    session_manager.set_completed_at(session["id"], "missing", "now")
    assert changes and changes[-1].get("msg") is None
    assert _broadcaster_frames(changes[-1]) == []


def test_live_and_recovery_completed_paths_share_delta_shape() -> None:
    msg = {"id": "m", "role": "assistant", "content": "done", "completed_at": "now"}
    live = _broadcaster_frames({"kind": "completed_at_set", "msg": msg})
    recovery = _broadcaster_frames({"kind": "completed_at_set", "msg": dict(msg)})
    assert live == recovery


def test_emitted_session_kinds_are_exhaustively_classified() -> None:
    manager_source = inspect.getsource(sys.modules[session_manager.__class__.__module__])
    broadcaster_source = inspect.getsource(sys.modules[SessionWSBroadcaster.__module__])
    emitted = set(re.findall(r'"kind":\s*"([^"]+)"', manager_source))
    explicit = set(re.findall(r'kind\s*==\s*"([^"]+)"', broadcaster_source))
    for group in re.findall(r'kind\s+in\s+\(([^)]*)\)', broadcaster_source):
        explicit.update(re.findall(r'"([^"]+)"', group))
    unclassified = emitted - explicit - _INTERNAL_KINDS - _METADATA_KINDS
    assert not unclassified, sorted(unclassified)
    assert {"parent_deleted", "worker_fanout_required", "agent_sid_set", "context_tokens_set"} <= _INTERNAL_KINDS


def main() -> int:
    tests = [value for name, value in globals().items() if name.startswith("test_")]
    try:
        for test in tests:
            test()
            print(f"PASS {test.__name__}")
        return 0
    finally:
        shutil.rmtree(TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
