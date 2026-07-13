#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import inspect
import json
import os
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch


os.environ["BETTER_AGENT_HOME"] = tempfile.mkdtemp(prefix="better-agent-compact-http-")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import Response
import main
import historical_children_projection


def test_compact_endpoint_is_no_store_and_defaults_to_five() -> None:
    original_page = main.session_manager.get_compact_turn_page
    original_pending = main.pending_user_input_projection.snapshot
    captured: dict[str, int] = {}
    try:
        def page(_sid: str, *, turn_limit: int, before_seq=None, cursor_revision=None, request_id=""):
            captured["limit"] = turn_limit
            return {"turns": [], "page_cursor": {}}
        main.session_manager.get_compact_turn_page = page
        main.pending_user_input_projection.snapshot = lambda _sid: {"requests": [], "revision": 0}
        response = Response()
        default_limit = inspect.signature(main.get_compact_turns).parameters["limit"].default
        assert default_limit.default == 5
        result = asyncio.run(main.get_compact_turns("session", response, limit=5, before_seq=None))
        assert captured["limit"] == 5
        assert result.headers["cache-control"] == "no-store"
    finally:
        main.session_manager.get_compact_turn_page = original_page
        main.pending_user_input_projection.snapshot = original_pending


def test_compact_endpoint_returns_typed_rebuilding_response_when_projection_unavailable() -> None:
    def unavailable(*_args, **_kwargs):
        raise historical_children_projection.ProjectionUnavailable("not ready")

    with (
        patch.object(main.session_manager, "get_compact_turn_page", side_effect=unavailable),
        patch.object(main.session_manager, "_root_id_for", return_value="root"),
        patch.object(main.session_manager, "get_ref", return_value={"id": "root"}),
        patch.object(historical_children_projection, "schedule_rebuild") as schedule,
    ):
        try:
            asyncio.run(main.get_compact_turns("session", Response(), limit=5, before_seq=None))
            raise AssertionError("projection unavailability returned success")
        except main.HTTPException as exc:
            assert exc.status_code == 503
            assert exc.detail["state"] == "historical_projection_rebuilding"
            assert isinstance(exc.detail["request_id"], str) and exc.detail["request_id"]
            assert exc.headers == {"Retry-After": "1"}
        schedule.assert_called_once_with("root", {"id": "root"}, priority=True)


def test_compact_endpoint_returns_typed_stale_page_conflict() -> None:
    with patch.object(
        main.session_manager, "get_compact_turn_page",
        side_effect=main.CompactTurnPageConflict("stale"),
    ):
        try:
            asyncio.run(main.get_compact_turns(
                "session", Response(), limit=5, before_seq=10, cursor_revision="old",
            ))
            raise AssertionError("stale compact page returned success")
        except main.HTTPException as exc:
            assert exc.status_code == 409
            assert exc.detail["state"] == "compact_page_stale"
            assert isinstance(exc.detail["request_id"], str) and exc.detail["request_id"]


def test_subscribe_run_state_journal_dependency_is_bound() -> None:
    assert main._current_event_journal_seq("missing-session") is None


def test_worker_only_historical_root_returns_one_worker_level() -> None:
    worker = {
        "delegation_id": "worker-only",
        "worker_session_id": "worker-session",
        "worker_description": "worker",
        "is_new": False,
        "instructions_preview": "inspect",
        "events": [{"type": "agent_message", "data": {"text": "nested"}}],
    }
    message = {
        "id": "assistant-worker-only",
        "seq": 2,
        "role": "assistant",
        "content": "done",
        "events": [],
        "workers": [worker],
    }
    root_id = "root-worker-only"
    journal = Path(os.environ["BETTER_AGENT_HOME"]) / "sessions" / root_id / "events.jsonl"
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_bytes(b"")
    historical_children_projection.note_event(root_id, {}, 0, 0)
    historical_children_projection.note_workers(root_id, "session", message["id"], [worker])
    root = historical_children_projection.root_manifest(root_id, "session", message["id"])
    original_get_message_full = main.session_manager.get_message_full
    original_root_id_for = main.session_manager._root_id_for
    original_get_ref = main.session_manager.get_ref
    main.session_manager.get_message_full = lambda *_args: (_ for _ in ()).throw(AssertionError("legacy full hydrate used"))
    main.session_manager._root_id_for = lambda _sid: root_id
    main.session_manager.get_ref = lambda _sid: {"messages": [message]}
    try:
        response = asyncio.run(main.get_historical_children(
            "session", message["id"], parent_id=root["id"], revision=root["revision"],
            limit=50,
        ))
    finally:
        main.session_manager.get_message_full = original_get_message_full
        main.session_manager._root_id_for = original_root_id_for
        main.session_manager.get_ref = original_get_ref
    assert response["parent"]["direct_child_count"] == 1
    assert len(response["children"]) == 1
    assert response["children"][0]["type"] == "worker"
    assert response["children"][0]["render_payload"]["events"] == []


def test_historical_http_cursor_is_paginated_and_fail_closed() -> None:
    root_id, sid, msg_id = "http-root", "http-session", "http-message"
    journal = Path(os.environ["BETTER_AGENT_HOME"]) / "sessions" / root_id / "events.jsonl"
    journal.parent.mkdir(parents=True, exist_ok=True)
    for seq in range(1, 106):
        entry = {"seq": seq, "sid": sid, "msg_id": msg_id, "type": "agent_message", "data": {
            "uuid": f"http-{seq}", "message": {"content": [{"type": "text", "text": str(seq)}]},
        }}
        raw = (json.dumps(entry) + "\n").encode()
        with journal.open("ab") as handle:
            start = handle.tell(); handle.write(raw); end = handle.tell()
        historical_children_projection.note_event(root_id, entry, start, end)
    root = historical_children_projection.root_manifest(root_id, sid, msg_id)
    original_root_id_for = main.session_manager._root_id_for
    original_get_ref = main.session_manager.get_ref
    main.session_manager._root_id_for = lambda candidate: root_id if candidate in (sid, "foreign") else None
    main.session_manager.get_ref = lambda candidate: {"messages": [{"id": msg_id}]} if candidate == sid else {"messages": []}
    try:
        first = asyncio.run(main.get_historical_children(sid, msg_id, root["id"], root["revision"], 40, None))
        assert first["parent"]["direct_child_count"] == 105
        assert len(first["children"]) == 40 and first["has_more"] and first["next_cursor"]
        second = asyncio.run(main.get_historical_children(sid, msg_id, root["id"], root["revision"], 40, first["next_cursor"]))
        assert not ({row["id"] for row in first["children"]} & {row["id"] for row in second["children"]})
        tampered = first["next_cursor"][:-1] + ("A" if first["next_cursor"][-1] != "A" else "B")
        try:
            asyncio.run(main.get_historical_children(sid, msg_id, root["id"], root["revision"], 40, tampered))
            raise AssertionError("tampered HTTP cursor accepted")
        except main.HTTPException as exc:
            assert exc.status_code == 409
        try:
            asyncio.run(main.get_historical_children("foreign", msg_id, root["id"], root["revision"], 40, first["next_cursor"]))
            raise AssertionError("cross-session HTTP cursor accepted")
        except main.HTTPException as exc:
            assert exc.status_code == 404
    finally:
        main.session_manager._root_id_for = original_root_id_for
        main.session_manager.get_ref = original_get_ref


def test_late_projection_updates_compact_root_and_expands_without_conflict() -> None:
    async def run() -> None:
        from event_bus_subscribers import bind_session_content_projection, bind_session_ws_broadcaster
        main.session_manager.bind_loop(asyncio.get_running_loop())
        bind_session_content_projection()
        bind_session_ws_broadcaster(main.ws_broadcaster)
        frames = []
        original_dispatch = main.ws_broadcaster._dispatch
        main.ws_broadcaster._dispatch = frames.append
        session = main.session_manager.create(name="late-projection", cwd="/tmp", orchestration_mode="native")
        sid = session["id"]
        msg_id = "late-assistant"
        try:
            main.session_manager.append_assistant_msg(sid, {
                "id": msg_id, "seq": 1, "role": "assistant", "content": "done",
                "events": [], "workers": [], "isStreaming": False,
            })
            journal = Path(os.environ["BETTER_AGENT_HOME"]) / "sessions" / sid / "events.jsonl"
            journal.parent.mkdir(parents=True, exist_ok=True)
            entry = {"seq": 1, "sid": sid, "msg_id": msg_id, "type": "agent_message", "data": {
                "uuid": "late-event", "message": {"content": [{"type": "text", "text": "late"}]},
            }}
            raw = (json.dumps(entry) + "\n").encode()
            journal.write_bytes(raw)
            await asyncio.to_thread(historical_children_projection.note_event, sid, entry, 0, len(raw))
            root = historical_children_projection.root_manifest(sid, sid, msg_id)
            compact = main.session_manager.get_compact_turn_page(sid, turn_limit=5)
            projected = next(turn for turn in compact["turns"] if turn["assistant"]["id"] == msg_id)
            hydration_root = projected["assistant"]["hydration_root"]
            assert hydration_root["revision"] == root["revision"]
            assert hydration_root["direct_child_count"] == 1
            assert any(frame.get("type") == "messages_delta" for frame in frames)
            expanded = await main.get_historical_children(
                sid, msg_id, hydration_root["id"], hydration_root["revision"], 50, None,
            )
            assert expanded["parent"]["direct_child_count"] == 1
        finally:
            main.ws_broadcaster._dispatch = original_dispatch
            main.session_manager.delete(sid)
    asyncio.run(run())


def test_reordered_and_rebuilt_projection_facts_cannot_regress_owner() -> None:
    session = main.session_manager.create(
        name="projection-order", cwd="/tmp", orchestration_mode="native",
    )
    sid = session["id"]
    msg_id = "ordered-assistant"
    journal = Path(os.environ["BETTER_AGENT_HOME"]) / "sessions" / sid / "events.jsonl"

    def write_event(seq: int, event_id: str, *, replace: bool = False) -> dict:
        entry = {
            "seq": seq, "sid": sid, "msg_id": msg_id, "type": "agent_message",
            "data": {"uuid": event_id, "message": {"content": [{"type": "text", "text": event_id}]}},
        }
        raw = (json.dumps(entry) + "\n").encode()
        mode = "wb" if replace else "ab"
        with journal.open(mode) as handle:
            start = handle.tell()
            handle.write(raw)
            end = handle.tell()
        historical_children_projection.note_event(sid, entry, start, end)
        return historical_children_projection.root_manifest(sid, sid, msg_id)

    try:
        main.session_manager.append_assistant_msg(sid, {
            "id": msg_id, "seq": 1, "role": "assistant", "content": "done",
            "events": [], "workers": [], "isStreaming": False,
        })
        journal.parent.mkdir(parents=True, exist_ok=True)
        first = write_event(1, "first")
        second = write_event(2, "second")
        main.session_manager.apply_historical_projection_changed(
            sid, sid, msg_id, second["revision"], second["direct_child_count"],
        )
        assert not main.session_manager.apply_historical_projection_changed(
            sid, sid, msg_id, first["revision"], first["direct_child_count"],
        )
        compact = main.session_manager.get_compact_turn_page(sid, turn_limit=5)
        assert compact["turns"][-1]["assistant"]["hydration_root"]["revision"] == second["revision"]

        sidecar = historical_children_projection._path(sid)
        for candidate in (sidecar, Path(str(sidecar) + "-wal"), Path(str(sidecar) + "-shm")):
            candidate.unlink(missing_ok=True)
        rebuilt = write_event(1, "rebuilt", replace=True)
        assert rebuilt["revision"] != second["revision"]
        assert not main.session_manager.apply_historical_projection_changed(
            sid, sid, msg_id, second["revision"], second["direct_child_count"],
        )
        main.session_manager.apply_historical_projection_changed(
            sid, sid, msg_id, rebuilt["revision"], rebuilt["direct_child_count"],
        )
    finally:
        main.session_manager.delete(sid)


def test_new_commit_cannot_overtake_validated_owner_publication() -> None:
    session = main.session_manager.create(
        name="projection-linearization", cwd="/tmp", orchestration_mode="native",
    )
    sid = session["id"]
    msg_id = "linearized-assistant"
    journal = Path(os.environ["BETTER_AGENT_HOME"]) / "sessions" / sid / "events.jsonl"
    journal.parent.mkdir(parents=True, exist_ok=True)

    def append(seq: int, event_id: str) -> dict:
        entry = {
            "seq": seq, "sid": sid, "msg_id": msg_id, "type": "agent_message",
            "data": {"uuid": event_id, "message": {"content": [{"type": "text", "text": event_id}]}},
        }
        raw = (json.dumps(entry) + "\n").encode()
        with journal.open("ab") as handle:
            start = handle.tell()
            handle.write(raw)
            end = handle.tell()
        historical_children_projection.note_event(sid, entry, start, end)
        return historical_children_projection.root_manifest(sid, sid, msg_id)

    original_observer = historical_children_projection._change_observer
    historical_children_projection.set_change_observer(None)
    try:
        main.session_manager.append_assistant_msg(sid, {
            "id": msg_id, "seq": 1, "role": "assistant", "content": "done",
            "events": [], "workers": [], "isStreaming": False,
        })
        first = append(1, "linear-first")
        second = append(2, "linear-second")
        main.session_manager.apply_historical_projection_changed(
            sid, sid, msg_id, first["revision"], first["direct_child_count"],
        )

        validated = threading.Event()
        commit_attempted = threading.Event()
        allow_owner_mutation = threading.Event()
        commit_done = threading.Event()
        owner_errors = []
        commit_errors = []
        committed = {}
        fired_revisions = []
        original_load = main.session_manager._load_root
        original_fire = main.session_manager._fire

        def blocked_load(*args, **kwargs):
            validated.set()
            if not allow_owner_mutation.wait(2):
                raise AssertionError("owner mutation barrier timed out")
            return original_load(*args, **kwargs)

        def capture_fire(_sid, change):
            stub = (change.get("delta") or {}).get("stub") or {}
            revision = stub.get("historical_revision")
            if revision:
                fired_revisions.append(revision)

        def apply_second():
            try:
                main.session_manager.apply_historical_projection_changed(
                    sid, sid, msg_id, second["revision"], second["direct_child_count"],
                )
            except BaseException as exc:
                owner_errors.append(exc)

        def commit_third():
            try:
                commit_attempted.set()
                committed["manifest"] = append(3, "linear-third")
            except BaseException as exc:
                commit_errors.append(exc)
            finally:
                commit_done.set()

        with patch.object(main.session_manager, "_load_root", side_effect=blocked_load), patch.object(
            main.session_manager, "_fire", side_effect=capture_fire,
        ):
            owner = threading.Thread(target=apply_second)
            owner.start()
            assert validated.wait(2)
            writer = threading.Thread(target=commit_third)
            writer.start()
            assert commit_attempted.wait(2)
            assert not commit_done.wait(0.05)
            allow_owner_mutation.set()
            owner.join(2)
            writer.join(2)
            assert not owner.is_alive() and not writer.is_alive()
            assert not owner_errors and not commit_errors
            third = committed["manifest"]
            main.session_manager.apply_historical_projection_changed(
                sid, sid, msg_id, third["revision"], third["direct_child_count"],
            )

        assert fired_revisions == [second["revision"], third["revision"]]
        compact = main.session_manager.get_compact_turn_page(sid, turn_limit=5)
        assert compact["turns"][-1]["assistant"]["hydration_root"]["revision"] == third["revision"]
    finally:
        historical_children_projection.set_change_observer(original_observer)
        main.session_manager.delete(sid)


def test_warm_compact_page_is_root_lock_free_batched_and_fresh() -> None:
    session = main.session_manager.create(
        name="compact-performance", cwd="/tmp", orchestration_mode="native",
    )
    sid = session["id"]
    journal = Path(os.environ["BETTER_AGENT_HOME"]) / "sessions" / sid / "events.jsonl"
    journal.parent.mkdir(parents=True, exist_ok=True)
    selected = []

    def append_projection(seq: int, msg_id: str, event_id: str) -> dict:
        entry = {
            "seq": seq, "sid": sid, "msg_id": msg_id, "type": "agent_message",
            "data": {"uuid": event_id, "message": {"content": [{"type": "text", "text": event_id}]}},
        }
        raw = (json.dumps(entry) + "\n").encode()
        with journal.open("ab") as handle:
            start = handle.tell()
            handle.write(raw)
            end = handle.tell()
        historical_children_projection.note_event(sid, entry, start, end)
        return historical_children_projection.root_manifest(sid, sid, msg_id)

    try:
        for turn in range(10):
            main.session_manager.append_user_msg(sid, {
                "id": f"perf-user-{turn}", "role": "user", "content": f"prompt {turn}",
            })
            msg_id = f"perf-assistant-{turn}"
            main.session_manager.append_assistant_msg(sid, {
                "id": msg_id, "role": "assistant", "content": f"answer {turn}",
                "events": [], "workers": [], "isStreaming": False,
            })
            append_projection(turn + 1, msg_id, f"perf-event-{turn}")
            if turn >= 5:
                selected.append((msg_id, f"answer {turn}"))

        connections = 0
        original_connect = historical_children_projection._connect

        def counted_connect(*args, **kwargs):
            nonlocal connections
            connections += 1
            return original_connect(*args, **kwargs)

        with patch.object(historical_children_projection, "_connect", side_effect=counted_connect):
            revision, manifests = historical_children_projection.root_manifests(
                sid, sid, selected,
            )
        assert revision > 0 and len(manifests) == 5
        assert connections == 1

        cold = main.session_manager.get_compact_turn_page(sid, turn_limit=5)
        root_lock = main.session_manager._lock_for_root(sid)
        held = threading.Event()
        release = threading.Event()

        def hold_root_lock():
            with root_lock:
                held.set()
                release.wait()

        holder = threading.Thread(target=hold_root_lock)
        holder.start()
        assert held.wait(2)
        test_thread = threading.get_ident()
        original_lock_for_root = main.session_manager._lock_for_root
        def forbid_test_thread_root_lock(root_id):
            if threading.get_ident() == test_thread:
                raise AssertionError("warm compact page acquired root lock")
            return original_lock_for_root(root_id)
        try:
            with patch.object(
                main.session_manager, "_lock_for_root",
                side_effect=forbid_test_thread_root_lock,
            ):
                warm = main.session_manager.get_compact_turn_page(sid, turn_limit=5)
        finally:
            release.set()
            holder.join(2)
        assert warm == cold

        main.session_manager.rename(sid, "compact-performance-renamed", force=True)
        renamed = main.session_manager.get_compact_turn_page(sid, turn_limit=5)
        assert renamed["session"]["name"] == "compact-performance-renamed"
        assert renamed != warm

        latest_id = selected[-1][0]
        updated = append_projection(11, latest_id, "perf-event-late")
        refreshed = main.session_manager.get_compact_turn_page(sid, turn_limit=5)
        latest_root = refreshed["turns"][-1]["assistant"]["hydration_root"]
        assert latest_root["revision"] == updated["revision"]
        assert latest_root["direct_child_count"] == 2
        assert refreshed != renamed
    finally:
        main.session_manager.delete(sid)


def test_compact_manifest_immediately_reflects_renderable_children_only() -> None:
    session = main.session_manager.create(
        name="compact-renderable", cwd="/tmp", orchestration_mode="native",
    )
    sid = session["id"]
    msg_id = "compact-renderable-assistant"
    journal = Path(os.environ["BETTER_AGENT_HOME"]) / "sessions" / sid / "events.jsonl"
    journal.parent.mkdir(parents=True, exist_ok=True)

    def append(seq: int, event_type: str, data: dict) -> dict:
        entry = {"seq": seq, "sid": sid, "msg_id": msg_id, "type": event_type, "data": data}
        raw = (json.dumps(entry) + "\n").encode()
        with journal.open("ab") as handle:
            start = handle.tell(); handle.write(raw); end = handle.tell()
        historical_children_projection.note_event(sid, entry, start, end)
        return main.session_manager.get_compact_turn_page(sid, turn_limit=5)

    try:
        main.session_manager.append_user_msg(sid, {
            "id": "compact-renderable-user", "role": "user", "content": "prompt",
        })
        main.session_manager.append_assistant_msg(sid, {
            "id": msg_id, "role": "assistant", "content": "done",
            "events": [], "workers": [], "isStreaming": False,
        })
        hidden_page = append(1, "turn_complete", {"uuid": "hidden-complete"})
        hidden_root = hidden_page["turns"][-1]["assistant"]["hydration_root"]
        assert hidden_root["direct_child_count"] == 0
        visible_page = append(2, "error", {"uuid": "visible-error", "error": "failed"})
        visible_root = visible_page["turns"][-1]["assistant"]["hydration_root"]
        assert visible_root["direct_child_count"] == 1
        assert visible_root["revision"] != hidden_root["revision"]
    finally:
        main.session_manager.delete(sid)


def test_running_turn_children_serve_live_without_hitting_index() -> None:
    session = main.session_manager.create(name="live-children", cwd="/tmp", orchestration_mode="native")
    sid = session["id"]
    msg_id = "live-children-assistant"
    journal = Path(os.environ["BETTER_AGENT_HOME"]) / "sessions" / sid / "events.jsonl"
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_bytes(b"")
    historical_children_projection.note_event(sid, {}, 0, 0)
    try:
        main.session_manager.append_user_msg(sid, {
            "id": "live-children-user", "role": "user", "content": "prompt",
        })
        main.session_manager.append_assistant_msg(sid, {
            "id": msg_id, "role": "assistant", "content": "partial",
            "events": [
                {"type": "tool_call", "data": {"name": "x"}},
                {"type": "agent_message", "data": {"uuid": "stream-1", "message": {"content": [{"type": "text", "text": "streaming"}]}}},
            ],
            "workers": [], "isStreaming": True,
        })
        compact = main.session_manager.get_compact_turn_page(sid, turn_limit=5)
        root = compact["turns"][-1]["assistant"]["hydration_root"]
        assert root["direct_child_count"] == 2

        with patch.object(
            historical_children_projection, "children",
            side_effect=AssertionError("live turn hit the async-indexed projection"),
        ):
            page = asyncio.run(main.get_historical_children(
                sid, msg_id, root["id"], root["revision"], 50, None,
            ))
        assert page["parent"]["direct_child_count"] == 2
        assert len(page["children"]) == 2
        assert page["children"][1]["display_summary"] == "streaming"
        assert not page["has_more"] and page["next_cursor"] is None

        try:
            asyncio.run(main.get_historical_children(sid, msg_id, root["id"], "stale-revision", 50, None))
            raise AssertionError("stale live revision accepted")
        except main.HTTPException as exc:
            assert exc.status_code == 409
    finally:
        main.session_manager.delete(sid)


def test_live_compact_snapshot_is_lock_free_fresh_and_pagination_fenced() -> None:
    session = main.session_manager.create(name="live-compact", cwd="/tmp", orchestration_mode="native")
    sid = session["id"]
    journal = Path(os.environ["BETTER_AGENT_HOME"]) / "sessions" / sid / "events.jsonl"
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_bytes(b"")
    historical_children_projection.note_event(sid, {}, 0, 0)
    try:
        for index in range(1, 7):
            main.session_manager.append_user_msg(sid, {
                "id": f"live-u{index}", "role": "user", "content": f"prompt {index}",
            })
            assistant = {
                "id": f"live-a{index}", "role": "assistant", "content": f"answer {index}",
                "events": [], "workers": [], "isStreaming": index == 6,
            }
            main.session_manager.append_assistant_msg(sid, assistant)
            if index < 6:
                historical_children_projection.note_workers(sid, sid, assistant["id"], [])
        with main.session_manager._cache_guard:
            main.session_manager._compact_turn_cache.clear()
        root_lock = main.session_manager._lock_for_root(sid)
        held, release = threading.Event(), threading.Event()
        def hold():
            with root_lock:
                held.set(); release.wait()
        holder = threading.Thread(target=hold)
        holder.start(); assert held.wait(2)
        try:
            with patch.object(main.session_manager, "_lock_for_root", side_effect=AssertionError("live compact acquired root lock")):
                latest = main.session_manager.get_compact_turn_page(sid, turn_limit=5)
        finally:
            release.set(); holder.join(2)
        assert [turn["prompt"]["id"] for turn in latest["turns"]] == [f"live-u{i}" for i in range(2, 7)]
        assert latest["turns"][-1]["assistant"]["running"] is True
        older = main.session_manager.get_compact_turn_page(
            sid, turn_limit=5, before_seq=latest["page_cursor"]["before_seq"],
            cursor_revision=latest["page_cursor"]["revision"],
        )
        assert [turn["prompt"]["id"] for turn in older["turns"]] == ["live-u1"]
        fire_entered, fire_release = threading.Event(), threading.Event()
        def slow_fire_listener(changed_sid, _change):
            if changed_sid == sid:
                fire_entered.set(); fire_release.wait()
        main.session_manager._listeners.append(slow_fire_listener)
        writer = threading.Thread(target=lambda: main.session_manager.append_user_msg(
            sid, {"id": "live-u7", "role": "user", "content": "new"},
        ))
        writer.start(); assert fire_entered.wait(2)
        try:
            with main.session_manager._cache_guard:
                main.session_manager._compact_turn_cache.clear()
            read_result, read_done = {}, threading.Event()
            def read_current():
                read_result["page"] = main.session_manager.get_compact_turn_page(sid, turn_limit=5)
                read_done.set()
            reader = threading.Thread(target=read_current)
            reader.start()
            assert read_done.wait(2), "compact read blocked behind slow _fire"
            reader.join(2)
            assert read_result["page"]["turns"][-1]["prompt"]["id"] == "live-u7"
        finally:
            fire_release.set(); writer.join(2)
            main.session_manager._listeners.remove(slow_fire_listener)
        try:
            main.session_manager.get_compact_turn_page(
                sid, turn_limit=5, before_seq=latest["page_cursor"]["before_seq"],
                cursor_revision=latest["page_cursor"]["revision"],
            )
            raise AssertionError("stale pagination cursor accepted")
        except main.CompactTurnPageConflict:
            pass
    finally:
        main.session_manager.delete(sid)


if __name__ == "__main__":
    test_compact_endpoint_is_no_store_and_defaults_to_five()
    print("PASS test_compact_endpoint_is_no_store_and_defaults_to_five")
    test_compact_endpoint_returns_typed_rebuilding_response_when_projection_unavailable()
    print("PASS test_compact_endpoint_returns_typed_rebuilding_response_when_projection_unavailable")
    test_compact_endpoint_returns_typed_stale_page_conflict()
    print("PASS test_compact_endpoint_returns_typed_stale_page_conflict")
    test_subscribe_run_state_journal_dependency_is_bound()
    print("PASS test_subscribe_run_state_journal_dependency_is_bound")
    test_worker_only_historical_root_returns_one_worker_level()
    print("PASS test_worker_only_historical_root_returns_one_worker_level")
    test_historical_http_cursor_is_paginated_and_fail_closed()
    print("PASS test_historical_http_cursor_is_paginated_and_fail_closed")
    test_late_projection_updates_compact_root_and_expands_without_conflict()
    print("PASS test_late_projection_updates_compact_root_and_expands_without_conflict")
    test_reordered_and_rebuilt_projection_facts_cannot_regress_owner()
    print("PASS test_reordered_and_rebuilt_projection_facts_cannot_regress_owner")
    test_new_commit_cannot_overtake_validated_owner_publication()
    print("PASS test_new_commit_cannot_overtake_validated_owner_publication")
    test_warm_compact_page_is_root_lock_free_batched_and_fresh()
    print("PASS test_warm_compact_page_is_root_lock_free_batched_and_fresh")
    test_compact_manifest_immediately_reflects_renderable_children_only()
    print("PASS test_compact_manifest_immediately_reflects_renderable_children_only")
    test_running_turn_children_serve_live_without_hitting_index()
    print("PASS test_running_turn_children_serve_live_without_hitting_index")
    test_live_compact_snapshot_is_lock_free_fresh_and_pagination_fenced()
    print("PASS test_live_compact_snapshot_is_lock_free_fresh_and_pagination_fenced")
