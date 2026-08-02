"""Unit tests for ``user_msg_lifecycle``.

Covers the paths NOT reached by test_turn_manager_lifecycle_emit.py /
test_synthetic_messages_unit.py (which drive the coordinator happy-path
through ``TurnManager._publish_user_lifecycle``):

  * ``terminal_result`` — every terminal-result branch.
  * ``new_lifecycle_msg_id`` — uniqueness/type.
  * ``queued_payload`` — field shape, client_id, interrupt cross-ref rules,
    content preview truncation.
  * ``terminal_event_for_lifecycle`` (+ async) — the events.jsonl scan:
    done/failed match, reversed last-match, wrong-type skip, malformed-JSON
    skip, substring fast-path, data-field id match, missing file, unreadable
    file, no root.
  * ``_publish_lifecycle`` fallback paths — no active coordinator with a
    root (direct bus.publish, True) / no root (False) / ``get_active_coordinator``
    raising (belt-and-suspenders fallback).
  * ``emit_queued`` / ``emit_requested`` no-root error log.
  * ``emit_done`` ``interrupted_by_msg_id`` payload branch.
  * ``_root_id_for`` delegation.

Async entry points are driven through ``asyncio.run`` (no pytest-asyncio).

Run with:
    cd backend && .venv/bin/python -m pytest scripts/test_user_msg_lifecycle_unit.py -v
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import _test_home

_test_home.isolate("bc-test-user-msg-lifecycle-unit-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest  # noqa: E402

import user_msg_lifecycle as ulm  # noqa: E402
from event_bus import bus  # noqa: E402


# --- terminal_result -------------------------------------------------------

def test_terminal_result_failed_with_error() -> None:
    assert ulm.terminal_result("user_message_failed", {"error": "boom"}) == {
        "success": False, "error": "boom",
    }


def test_terminal_result_failed_with_reason() -> None:
    assert ulm.terminal_result("user_message_failed", {"reason": "r"}) == {
        "success": False, "error": "r",
    }


def test_terminal_result_failed_default_message() -> None:
    assert ulm.terminal_result("user_message_failed", {}) == {
        "success": False, "error": "target turn failed",
    }


def test_terminal_result_failed_non_dict_payload() -> None:
    assert ulm.terminal_result("user_message_failed", None) == {
        "success": False, "error": "target turn failed",
    }


def test_terminal_result_unsupported_event_raises() -> None:
    with pytest.raises(ValueError):
        ulm.terminal_result("user_message_received", {})


def test_terminal_result_done_success() -> None:
    assert ulm.terminal_result("user_message_done", {"success": True}) == {"success": True}


def test_terminal_result_done_success_but_cancelled_is_failure() -> None:
    res = ulm.terminal_result("user_message_done", {"success": True, "cancelled": True})
    assert res == {"success": False, "error": "target turn cancelled"}


def test_terminal_result_done_failure_with_error() -> None:
    res = ulm.terminal_result("user_message_done", {"success": False, "error": "x"})
    assert res == {"success": False, "error": "x"}


def test_terminal_result_done_failure_with_reason() -> None:
    res = ulm.terminal_result("user_message_done", {"success": False, "reason": "r"})
    assert res == {"success": False, "error": "r"}


def test_terminal_result_done_failure_default() -> None:
    res = ulm.terminal_result("user_message_done", {"success": False})
    assert res == {"success": False, "error": "target turn failed"}


def test_terminal_result_done_cancelled_default_message() -> None:
    res = ulm.terminal_result("user_message_done", {"cancelled": True})
    assert res == {"success": False, "error": "target turn cancelled"}


# --- new_lifecycle_msg_id --------------------------------------------------

def test_new_lifecycle_msg_id_unique_str() -> None:
    a = ulm.new_lifecycle_msg_id()
    b = ulm.new_lifecycle_msg_id()
    assert isinstance(a, str)
    assert a != b


# --- queued_payload --------------------------------------------------------

def test_queued_payload_basic_fields_and_truncation() -> None:
    content = "x" * 500
    p = ulm.queued_payload(
        lifecycle_msg_id="lid", content=content, kind="send",
        queue_position=3, images_count=2, orchestration_mode="native",
    )
    assert p["lifecycle_msg_id"] == "lid"
    assert p["kind"] == "send"
    assert p["queue_position"] == 3
    assert p["content_length"] == 500
    assert p["content_preview"] == "x" * 200
    assert p["images_count"] == 2
    assert p["orchestration_mode"] == "native"
    assert "client_id" not in p
    assert "interrupts_msg_id" not in p


def test_queued_payload_client_id_added() -> None:
    p = ulm.queued_payload(
        lifecycle_msg_id="l", content="c", kind="send",
        queue_position=1, client_id="cid",
    )
    assert p["client_id"] == "cid"


def test_queued_payload_interrupt_with_target() -> None:
    p = ulm.queued_payload(
        lifecycle_msg_id="l", content="c", kind="interrupt",
        queue_position=1, interrupts_msg_id="imid",
    )
    assert p["interrupts_msg_id"] == "imid"


def test_queued_payload_interrupt_without_target_omits_key() -> None:
    p = ulm.queued_payload(
        lifecycle_msg_id="l", content="c", kind="interrupt", queue_position=1,
    )
    assert "interrupts_msg_id" not in p


def test_queued_payload_non_interrupt_ignores_interrupts_id() -> None:
    p = ulm.queued_payload(
        lifecycle_msg_id="l", content="c", kind="send",
        queue_position=1, interrupts_msg_id="imid",
    )
    assert "interrupts_msg_id" not in p


# --- terminal_event_for_lifecycle (events.jsonl scan) ----------------------

def _point_scan(monkeypatch, tmp_path, root_id="rootX"):
    """Point the scan at ``tmp_path/<root_id>/events.jsonl``.

    Caller writes the file (or not) at the returned path."""
    monkeypatch.setattr(ulm, "_root_id_for", lambda _sid: root_id)
    import session_store
    monkeypatch.setattr(
        session_store, "session_file_path",
        lambda _sid: str(tmp_path / "sess.json"),
    )
    return tmp_path / root_id / "events.jsonl"


def _write_lines(path, lines) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")


def test_scan_finds_done_event(monkeypatch, tmp_path) -> None:
    done = {"type": "user_message_done",
            "data": {"lifecycle_msg_id": "L1", "success": True}}
    queued = {"type": "user_message_queued", "data": {"lifecycle_msg_id": "L1"}}
    path = _point_scan(monkeypatch, tmp_path)
    _write_lines(path, [json.dumps(queued), json.dumps(done)])
    assert ulm.terminal_event_for_lifecycle("sid", "L1") == done


def test_scan_finds_failed_event(monkeypatch, tmp_path) -> None:
    fail = {"type": "user_message_failed",
            "data": {"lifecycle_msg_id": "L1", "reason": "boom"}}
    path = _point_scan(monkeypatch, tmp_path)
    _write_lines(path, [json.dumps(fail)])
    assert ulm.terminal_event_for_lifecycle("sid", "L1") == fail


def test_scan_returns_last_match_in_file_order(monkeypatch, tmp_path) -> None:
    # Reversed scan -> first match from the end wins, i.e. the last in file order.
    early = {"type": "user_message_done",
             "data": {"lifecycle_msg_id": "L1", "success": True}}
    late = {"type": "user_message_failed",
            "data": {"lifecycle_msg_id": "L1", "reason": "r"}}
    path = _point_scan(monkeypatch, tmp_path)
    _write_lines(path, [json.dumps(early), json.dumps(late)])
    assert ulm.terminal_event_for_lifecycle("sid", "L1") == late


def test_scan_skips_non_terminal_type_with_same_lifecycle_id(monkeypatch, tmp_path) -> None:
    queued = {"type": "user_message_queued", "data": {"lifecycle_msg_id": "L1"}}
    path = _point_scan(monkeypatch, tmp_path)
    _write_lines(path, [json.dumps(queued)])
    assert ulm.terminal_event_for_lifecycle("sid", "L1") is None


def test_scan_skips_malformed_json_line_containing_id(monkeypatch, tmp_path) -> None:
    done = {"type": "user_message_done", "data": {"lifecycle_msg_id": "L1"}}
    path = _point_scan(monkeypatch, tmp_path)
    # Malformed line AFTER done in file order -> reversed scanner hits it first,
    # fails JSON parse, and continues to the real match.
    _write_lines(path, [json.dumps(done), '{"lifecycle_msg_id": "L1" oops bad json'])
    assert ulm.terminal_event_for_lifecycle("sid", "L1") == done


def test_scan_fast_path_skips_lines_without_id_substring(monkeypatch, tmp_path) -> None:
    done = {"type": "user_message_done", "data": {"lifecycle_msg_id": "L1"}}
    other = {"type": "user_message_done", "data": {"lifecycle_msg_id": "OTHER"}}
    path = _point_scan(monkeypatch, tmp_path)
    # No-id line AFTER done in file order -> reversed scanner reaches it first
    # and skips it on the substring fast-path before finding the match.
    _write_lines(path, [json.dumps(done), json.dumps(other)])
    assert ulm.terminal_event_for_lifecycle("sid", "L1") == done


def test_scan_requires_lifecycle_id_in_data_field(monkeypatch, tmp_path) -> None:
    # Line contains "L1" only as prose; data's id is L2 -> not a match.
    decoy = {"type": "user_message_done",
             "data": {"lifecycle_msg_id": "L2", "note": "mentions L1"}}
    path = _point_scan(monkeypatch, tmp_path)
    _write_lines(path, [json.dumps(decoy)])
    assert ulm.terminal_event_for_lifecycle("sid", "L1") is None


def test_scan_no_root_returns_none(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(ulm, "_root_id_for", lambda _sid: None)
    assert ulm.terminal_event_for_lifecycle("sid", "L1") is None


def test_scan_missing_file_returns_none(monkeypatch, tmp_path) -> None:
    _point_scan(monkeypatch, tmp_path)
    # No events.jsonl written.
    assert ulm.terminal_event_for_lifecycle("sid", "L1") is None


def test_scan_unreadable_file_returns_none(monkeypatch, tmp_path) -> None:
    path = _point_scan(monkeypatch, tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.mkdir()  # read_text raises OSError (IsADirectoryError).
    assert ulm.terminal_event_for_lifecycle("sid", "L1") is None


def test_terminal_event_for_lifecycle_async_matches_sync(monkeypatch, tmp_path) -> None:
    done = {"type": "user_message_done", "data": {"lifecycle_msg_id": "L1"}}
    path = _point_scan(monkeypatch, tmp_path)
    _write_lines(path, [json.dumps(done)])
    sync = ulm.terminal_event_for_lifecycle("sid", "L1")
    async_res = asyncio.run(ulm.terminal_event_for_lifecycle_async("sid", "L1"))
    assert async_res == sync == done


# --- _publish_lifecycle fallback paths -------------------------------------

def _no_active_coordinator(monkeypatch, *, raises: bool = False) -> None:
    import orchestrator
    if raises:
        def _boom():
            raise RuntimeError("no coordinator")
        monkeypatch.setattr(orchestrator, "get_active_coordinator", _boom)
    else:
        monkeypatch.setattr(orchestrator, "get_active_coordinator", lambda: None)


def _capture(name: str, pattern: str = "user_message_*") -> list:
    captured: list = []

    async def _handler(ev) -> None:
        captured.append(ev)

    bus.subscribe(pattern, _handler, name=name)
    return captured


def test_publish_no_coordinator_with_root_publishes_on_bus(monkeypatch) -> None:
    _no_active_coordinator(monkeypatch)
    monkeypatch.setattr(ulm, "_root_id_for", lambda _sid: "rootX")
    cap = _capture("pub_with_root")
    try:
        ok = asyncio.run(ulm._publish_lifecycle(
            "user_message_done", "sid", "lid", {"x": 1},
        ))
    finally:
        bus.unsubscribe("pub_with_root")
    assert ok is True
    assert len(cap) == 1
    ev = cap[0]
    assert ev.type == "user_message_done"
    assert ev.root_id == "rootX"
    assert ev.sid == "sid"
    assert ev.msg_id == "lid"
    assert ev.payload == {"x": 1}


def test_publish_no_coordinator_no_root_returns_false(monkeypatch) -> None:
    _no_active_coordinator(monkeypatch)
    monkeypatch.setattr(ulm, "_root_id_for", lambda _sid: None)
    cap = _capture("pub_no_root")
    try:
        ok = asyncio.run(ulm._publish_lifecycle(
            "user_message_done", "sid", "lid", {},
        ))
    finally:
        bus.unsubscribe("pub_no_root")
    assert ok is False
    assert cap == []


def test_publish_get_active_coordinator_raising_falls_back(monkeypatch) -> None:
    _no_active_coordinator(monkeypatch, raises=True)
    monkeypatch.setattr(ulm, "_root_id_for", lambda _sid: "rootX")
    cap = _capture("pub_raises", "user_message_sent")
    try:
        ok = asyncio.run(ulm._publish_lifecycle(
            "user_message_sent", "sid", "lid", {}, run_id="r1",
        ))
    finally:
        bus.unsubscribe("pub_raises")
    assert ok is True
    assert len(cap) == 1
    assert cap[0].run_id == "r1"


# --- emit_queued / emit_requested no-root error path -----------------------

def test_emit_queued_no_root_logs_error(monkeypatch, caplog) -> None:
    _no_active_coordinator(monkeypatch)
    monkeypatch.setattr(ulm, "_root_id_for", lambda _sid: None)
    with caplog.at_level("ERROR", logger="user_msg_lifecycle"):
        asyncio.run(ulm.emit_queued(
            app_session_id="sid", lifecycle_msg_id="L",
            content="hi", kind="send", queue_position=1,
        ))
    assert any("no root" in r.message for r in caplog.records)


def test_emit_requested_no_root_logs_error(monkeypatch, caplog) -> None:
    _no_active_coordinator(monkeypatch)
    monkeypatch.setattr(ulm, "_root_id_for", lambda _sid: None)
    with caplog.at_level("ERROR", logger="user_msg_lifecycle"):
        asyncio.run(ulm.emit_requested(
            app_session_id="sid", lifecycle_msg_id="L",
            content="hi", kind="send", queue_position=1,
        ))
    assert any("no root" in r.message for r in caplog.records)


def test_emit_requested_publishes_via_fallback(monkeypatch) -> None:
    _no_active_coordinator(monkeypatch)
    monkeypatch.setattr(ulm, "_root_id_for", lambda _sid: "rootX")
    cap = _capture("req_ok", "user_message_requested")
    try:
        asyncio.run(ulm.emit_requested(
            app_session_id="sid", lifecycle_msg_id="L",
            content="hi", kind="send", queue_position=1,
        ))
    finally:
        bus.unsubscribe("req_ok")
    assert len(cap) == 1
    assert cap[0].payload["content_length"] == 2


# --- emit_done interrupted_by_msg_id payload branch ------------------------

def test_emit_done_includes_interrupted_by_msg_id(monkeypatch) -> None:
    _no_active_coordinator(monkeypatch)
    monkeypatch.setattr(ulm, "_root_id_for", lambda _sid: "rootX")
    cap = _capture("done_interrupted", "user_message_done")
    try:
        asyncio.run(ulm.emit_done(
            app_session_id="sid", lifecycle_msg_id="L",
            success=False, cancelled=True, interrupted_by_msg_id="IMID",
        ))
    finally:
        bus.unsubscribe("done_interrupted")
    assert len(cap) == 1
    payload = cap[0].payload
    assert payload["interrupted_by_msg_id"] == "IMID"
    assert payload["success"] is False
    assert payload["cancelled"] is True


# --- _root_id_for delegation (covers the real wrapper body) ----------------

def test_root_id_for_delegates_to_session_manager() -> None:
    from session_manager import manager as session_manager
    sess = session_manager.create(name="t", cwd="/tmp", orchestration_mode="native")
    sid = sess["id"]
    delegated = ulm._root_id_for(sid)
    # Non-None guards against a trivial pass where both sides return None.
    assert delegated is not None
    assert delegated == session_manager._root_id_for(sid)
