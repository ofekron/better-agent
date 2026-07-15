import json
import os
import shutil
import tempfile
import time
import sqlite3
import subprocess
import sys
import threading
from unittest.mock import patch
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="historical-projection-")
os.environ["BETTER_AGENT_HOME"] = HOME

import historical_children_projection as projection


ROOT, SID, MSG = "root", "session", "message"
JOURNAL = Path(HOME) / "sessions" / ROOT / "events.jsonl"


def append(seq, uuid, *, parent=None, msg=MSG, text=None):
    JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    data = {"uuid": uuid, "type": "assistant", "message": {"content": [{"type": "text", "text": text or uuid}]}}
    if parent:
        data["parentUuid"] = parent
    entry = {"seq": seq, "sid": SID, "msg_id": msg, "type": "agent_message", "data": data}
    raw = (json.dumps(entry) + "\n").encode()
    with JOURNAL.open("ab") as handle:
        start = handle.tell()
        handle.write(raw)
        handle.flush()
        end = handle.tell()
    projection.note_event(ROOT, entry, start, end)


def append_entry(entry):
    raw = (json.dumps(entry) + "\n").encode()
    with JOURNAL.open("ab") as handle:
        start = handle.tell()
        handle.write(raw)
        handle.flush()
        end = handle.tell()
    projection.note_event(ROOT, entry, start, end)


def manifest(msg=MSG):
    return projection.root_manifest(ROOT, SID, msg)


def children(parent, revision, *, msg=MSG, limit=50, cursor=None):
    return projection.children(ROOT, SID, msg, parent, revision, limit=limit, cursor=cursor)


def test_cursor_pagination_is_complete_bound_and_tamper_proof():
    page_msg = "paged"
    for seq in range(200000, 200237):
        append(seq, f"paged-{seq}", msg=page_msg)
    root = manifest(page_msg)
    cursor = None
    seen = []
    while True:
        page = children(root["id"], root["revision"], msg=page_msg, limit=37, cursor=cursor)
        assert page["parent"]["direct_child_count"] == 237
        seen.extend(row["id"] for row in page["children"])
        cursor = page["next_cursor"]
        if not page["has_more"]:
            break
        assert cursor
    assert len(seen) == len(set(seen)) == 237
    first = children(root["id"], root["revision"], msg=page_msg, limit=1)["next_cursor"]
    for bad in (first[:-1] + ("A" if first[-1] != "A" else "B"),):
        try:
            children(root["id"], root["revision"], msg=page_msg, cursor=bad)
            raise AssertionError("tampered cursor accepted")
        except projection.ProjectionConflict:
            pass
    try:
        projection.children(ROOT, "foreign", page_msg, root["id"], root["revision"], limit=50, cursor=first)
        raise AssertionError("cross-session cursor accepted")
    except (projection.ProjectionConflict, projection.ProjectionUnavailable):
        pass
    append(300000, "invalidates-cursor", msg=page_msg)
    try:
        children(root["id"], root["revision"], msg=page_msg, cursor=first)
        raise AssertionError("stale cursor accepted")
    except projection.ProjectionConflict:
        pass


def test_root_path_is_confined():
    for root in ("../escape", "/absolute", "a/b", "a\\b", ".."):
        try:
            projection._journal(root)
            raise AssertionError("unsafe root accepted")
        except projection.ProjectionConflict:
            pass


def test_one_level_and_bounded_payload_refs():
    append(1, "parent")
    append(2, "child", parent="parent")
    append(3, "grandchild", parent="child")
    root = manifest()
    first = children(root["id"], root["revision"])
    assert [row["display_summary"] for row in first["children"]] == ["parent"]
    assert "grandchild" not in json.dumps(first)
    parent = first["children"][0]
    second = children(parent["id"], parent["revision"])
    assert [row["display_summary"] for row in second["children"]] == ["child"]


def test_lifecycle_only_has_no_visible_children_or_revision_churn():
    msg = "lifecycle-only"
    for seq, event_type in enumerate((
        "turn_start", "turn_complete", "run_state", "messages_delta",
        "user_message_sent", "user_message_done",
    ), 500000):
        append_entry({"seq": seq, "sid": SID, "msg_id": msg, "type": event_type, "data": {"uuid": f"life-{seq}"}})
    root = manifest(msg)
    assert root["direct_child_count"] == 0
    assert children(root["id"], root["revision"], msg=msg)["children"] == []


def test_mixed_raw_eight_projects_exactly_three_visible_rows():
    msg = "mixed-eight"
    raw = [
        ("turn_start", {"uuid": "mix-1"}),
        ("run_state", {"uuid": "mix-2"}),
        ("user_message_sent", {"uuid": "mix-3"}),
        ("messages_delta", {"uuid": "mix-4"}),
        ("turn_complete", {"uuid": "mix-5"}),
        ("tool_call", {"uuid": "mix-6", "tool": "Read"}),
        ("error", {"uuid": "mix-7", "error": "failed"}),
        ("complete", {"uuid": "mix-8", "success": True, "token_usage": {"input_tokens": 4, "output_tokens": 2}}),
    ]
    for offset, (event_type, data) in enumerate(raw):
        append_entry({"seq": 510000 + offset, "sid": SID, "msg_id": msg, "type": event_type, "data": data})
    root = manifest(msg)
    page = children(root["id"], root["revision"], msg=msg)
    assert root["direct_child_count"] == 3
    assert [row["type"] for row in page["children"]] == ["tool_call", "error", "complete"]
    path = projection._path(ROOT)
    for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        candidate.unlink(missing_ok=True)
    projection.schedule_rebuild(ROOT, None)
    deadline = time.monotonic() + 10
    while ROOT in projection._rebuilding and time.monotonic() < deadline:
        time.sleep(0.02)
    rebuilt = manifest(msg)
    assert rebuilt["revision"] == root["revision"]
    assert rebuilt["direct_child_count"] == 3


def test_visible_pagination_skips_hidden_records_without_short_pages():
    msg = "hidden-pagination"
    for offset in range(120):
        visible = offset % 2 == 1
        append_entry({
            "seq": 520000 + offset, "sid": SID, "msg_id": msg,
            "type": "output" if visible else "turn_start",
            "data": {"uuid": f"page-hidden-{offset}", "output": f"visible-{offset}"},
        })
    root = manifest(msg)
    cursor = None
    seen = []
    while True:
        page = children(root["id"], root["revision"], msg=msg, limit=17, cursor=cursor)
        seen.extend(row["id"] for row in page["children"])
        if not page["has_more"]:
            break
        assert len(page["children"]) == 17
        cursor = page["next_cursor"]
    assert len(seen) == len(set(seen)) == 60


def test_hidden_parent_normalizes_visible_nested_adjacency():
    msg = "hidden-parent"
    append_entry({"seq": 530000, "sid": SID, "msg_id": msg, "type": "tool_call", "data": {"uuid": "visible-parent", "tool": "Read"}})
    append_entry({"seq": 530001, "sid": SID, "msg_id": msg, "type": "turn_start", "data": {"uuid": "hidden-middle", "parentUuid": "visible-parent"}})
    append_entry({"seq": 530002, "sid": SID, "msg_id": msg, "type": "error", "data": {"uuid": "visible-child", "parentUuid": "hidden-middle", "error": "nested"}})
    root = manifest(msg)
    direct = children(root["id"], root["revision"], msg=msg)["children"]
    assert len(direct) == 1 and direct[0]["type"] == "tool_call"
    assert direct[0]["direct_child_count"] == 1
    nested = children(direct[0]["id"], direct[0]["revision"], msg=msg)["children"]
    assert len(nested) == 1 and nested[0]["type"] == "error"


def test_empty_agent_message_parent_is_skipped_but_visible_descendant_is_reachable():
    msg = "empty-agent-parent"
    append_entry({"seq": 535000, "sid": SID, "msg_id": msg, "type": "agent_message", "data": {
        "uuid": "empty-agent", "message": {"content": []},
    }})
    append_entry({"seq": 535001, "sid": SID, "msg_id": msg, "type": "error", "data": {
        "uuid": "visible-under-empty", "parentUuid": "empty-agent", "error": "reachable",
    }})
    root = manifest(msg)
    direct = children(root["id"], root["revision"], msg=msg)["children"]
    assert len(direct) == 1
    assert direct[0]["type"] == "error"
    assert direct[0]["render_payload"]["data"]["error"] == "reachable"


def test_hidden_append_preserves_cursor_visible_append_invalidates_it():
    msg = "hidden-cursor"
    for offset in range(3):
        append_entry({"seq": 540000 + offset, "sid": SID, "msg_id": msg, "type": "output", "data": {"uuid": f"cursor-visible-{offset}", "output": str(offset)}})
    before = manifest(msg)
    cursor = children(before["id"], before["revision"], msg=msg, limit=1)["next_cursor"]
    append_entry({"seq": 540003, "sid": SID, "msg_id": msg, "type": "turn_complete", "data": {"uuid": "cursor-hidden"}})
    unchanged = manifest(msg)
    assert unchanged == before
    assert children(unchanged["id"], unchanged["revision"], msg=msg, limit=1, cursor=cursor)["children"]
    append_entry({"seq": 540004, "sid": SID, "msg_id": msg, "type": "output", "data": {"uuid": "cursor-new", "output": "new"}})
    try:
        children(unchanged["id"], unchanged["revision"], msg=msg, limit=1, cursor=cursor)
        raise AssertionError("visible projection change kept stale cursor valid")
    except projection.ProjectionConflict:
        pass


def test_out_of_order_and_cycle_fail_closed_to_root():
    append(4, "late-child", parent="late-parent")
    append(5, "late-parent")
    root = manifest()
    direct = children(root["id"], root["revision"])["children"]
    late_parent = next(row for row in direct if row["display_summary"] == "late-parent")
    assert children(late_parent["id"], late_parent["revision"])["children"][0]["display_summary"] == "late-child"
    append(6, "cycle-a", parent="cycle-b")
    append(7, "cycle-b", parent="cycle-a")
    root = manifest()
    assert any(row["display_summary"] == "cycle-a" for row in children(root["id"], root["revision"])["children"])


def test_workers_revision_persistence_and_isolation():
    projection.note_workers(ROOT, SID, MSG, [{
        "delegation_id": "d1", "worker_session_id": "w1", "worker_description": "worker",
        "is_new": False, "instructions_preview": "inspect", "events": [{"secret": "not duplicated"}],
    }])
    root = manifest()
    direct = children(root["id"], root["revision"])["children"]
    worker = next(row for row in direct if row["type"] == "worker")
    assert worker["render_payload"]["events"] == []
    try:
        children(root["id"], "stale")
        raise AssertionError("stale revision accepted")
    except projection.ProjectionConflict:
        pass
    assert projection.root_manifest(ROOT, SID, MSG)["revision"] == root["revision"]
    append(8, "foreign", msg="other")
    assert all(row["display_summary"] != "foreign" for row in children(root["id"], root["revision"])["children"])


def test_orphan_ownership_resolution_folds_pointer_without_scan():
    orphan = {"seq": 9, "sid": SID, "type": "agent_message", "data": {
        "uuid": "resolved-orphan", "type": "assistant",
        "message": {"content": [{"type": "text", "text": "resolved"}]},
    }}
    append_entry(orphan)
    append_entry({"seq": 10, "sid": SID, "msg_id": MSG, "type": "event_ownership_resolved", "data": {
        "event_seq": 9, "message_id": MSG,
    }})
    root = manifest()
    assert any(row["display_summary"] == "resolved" for row in children(root["id"], root["revision"])["children"])


def test_100k_unrelated_rows_do_not_widen_read_bytes():
    with JOURNAL.open("ab") as handle:
        for seq in range(11, 100011):
            handle.write((json.dumps({"seq": seq, "sid": "unrelated", "type": "metadata", "data": {"n": seq}}) + "\n").encode())
        handle.flush()
        end = handle.tell()
    projection.note_event(ROOT, {"seq": 100010, "sid": "unrelated", "type": "metadata", "data": {}}, end - 1, end)
    root = manifest()
    read_bytes = 0
    seeks = 0
    queries = 0
    real_open = Path.open

    class Metered:
        def __init__(self, handle):
            self.handle = handle
        def __enter__(self): return self
        def __exit__(self, *args): return self.handle.__exit__(*args)
        def seek(self, *args):
            nonlocal seeks
            seeks += 1
            return self.handle.seek(*args)
        def read(self, size=-1):
            nonlocal read_bytes
            data = self.handle.read(size)
            read_bytes += len(data)
            return data

    def metered_open(path, *args, **kwargs):
        handle = real_open(path, *args, **kwargs)
        return Metered(handle) if path == JOURNAL and "rb" in (args[0] if args else kwargs.get("mode", "r")) else handle

    def observe(_sql):
        nonlocal queries
        queries += 1
    projection._query_observer = observe
    try:
        with patch.object(Path, "open", metered_open):
            page = children(root["id"], root["revision"], limit=50)
    finally:
        projection._query_observer = None
    expected = len(json.dumps(page).encode())
    assert seeks <= len([row for row in page["children"] if row["type"] != "worker"])
    assert read_bytes < expected * 4
    assert queries <= 31 + len(page["children"])


def test_long_turn_append_queries_are_constant_and_rebuild_converges():
    root_id, sid, msg_id = "complexity-root", "complexity-session", "complexity-message"
    journal = Path(HOME) / "sessions" / root_id / "events.jsonl"
    journal.parent.mkdir(parents=True, exist_ok=True)
    query_counts = []
    for seq in range(1, 1002):
        entry = {"seq": seq, "sid": sid, "msg_id": msg_id, "type": "agent_message", "data": {
            "uuid": f"complexity-{seq}", "message": {"content": [{"type": "text", "text": str(seq)}]},
        }}
        raw = (json.dumps(entry) + "\n").encode()
        with journal.open("ab") as handle:
            start = handle.tell(); handle.write(raw); end = handle.tell()
        queries = 0
        def observe(_sql):
            nonlocal queries
            queries += 1
        projection._query_observer = observe
        try:
            projection.note_event(root_id, entry, start, end)
        finally:
            projection._query_observer = None
        if seq in (1, 1001):
            query_counts.append(queries)
    assert query_counts[1] <= query_counts[0] + 2, query_counts
    before = projection.root_manifest(root_id, sid, msg_id)
    path = projection._path(root_id)
    for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        candidate.unlink(missing_ok=True)
    projection.schedule_rebuild(root_id, None)
    deadline = time.monotonic() + 10
    while root_id in projection._rebuilding and time.monotonic() < deadline:
        time.sleep(0.02)
    after = projection.root_manifest(root_id, sid, msg_id)
    assert after["revision"] == before["revision"]
    assert after["direct_child_count"] == before["direct_child_count"] == 1001


def test_limit_and_missing_projection():
    root = manifest()
    page = children(root["id"], root["revision"], limit=1)
    assert len(page["children"]) == 1 and page["has_more"] is True
    try:
        children(root["id"], root["revision"], limit=101)
        raise AssertionError("oversized limit accepted")
    except projection.ProjectionConflict:
        pass
    try:
        projection.children("missing", SID, MSG, "x", "y", limit=1)
        raise AssertionError("missing projection accepted")
    except projection.ProjectionUnavailable:
        pass


def test_locked_manifest_releases_after_exception():
    root = manifest()
    try:
        with projection.locked_root_manifest(ROOT, SID, MSG) as locked:
            assert locked["revision"] == root["revision"]
            raise RuntimeError("release")
    except RuntimeError:
        pass
    completed = threading.Event()
    errors = []

    def read_again():
        try:
            projection.root_manifest(ROOT, SID, MSG)
        except BaseException as exc:
            errors.append(exc)
        finally:
            completed.set()

    reader = threading.Thread(target=read_again)
    reader.start()
    assert completed.wait(2)
    reader.join(2)
    assert not errors


def test_corrupt_sidecar_rebuilds_in_background_without_partial_reads():
    path = projection._path(ROOT)
    path.write_bytes(b"corrupt")
    snapshot = {"id": SID, "messages": [{
        "id": MSG, "role": "assistant", "workers": [{
            "delegation_id": "rebuilt-worker", "worker_session_id": "rw",
            "worker_description": "rebuilt", "is_new": False,
            "instructions_preview": "", "events": [],
        }],
    }], "forks": []}
    projection.schedule_rebuild(ROOT, snapshot)
    deadline = time.monotonic() + 5
    while ROOT in projection._rebuilding and time.monotonic() < deadline:
        time.sleep(0.02)
    if ROOT in projection._rebuilding:
        raise AssertionError("background rebuild did not publish")
    root = projection.root_manifest(ROOT, SID, MSG)
    direct = projection.children(ROOT, SID, MSG, root["id"], root["revision"], limit=50)["children"]
    assert any(row["display_summary"] == "rebuilt" for row in direct)


def test_schedule_all_loads_durable_unloaded_worker_tree():
    root_id, sid, msg_id = "durable-root", "durable-root", "durable-message"
    journal = Path(HOME) / "sessions" / root_id / "events.jsonl"
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_bytes(b"")
    snapshot = {"id": sid, "messages": [{"id": msg_id, "role": "assistant", "workers": [{
        "delegation_id": "durable-worker", "worker_session_id": "dw",
        "worker_description": "durable", "is_new": False,
        "instructions_preview": "", "events": [],
    }]}], "forks": []}
    owner = type("ProjectionOwner", (), {
        "list": lambda _self: [{"id": root_id}],
        "get_ref": lambda _self, requested: snapshot if requested == root_id else None,
    })()
    with patch.object(projection, "schedule_rebuild") as rebuild:
        projection.schedule_all(owner)
    rebuild.assert_called_once_with(root_id, snapshot, priority=False)


def test_missing_journal_worker_projection_is_current_and_readable():
    root_id, sid, msg_id = "missing-journal-root", "missing-journal-root", "missing-journal-message"
    snapshot = {"id": sid, "messages": [{"id": msg_id, "role": "assistant", "workers": [{
        "delegation_id": "missing-worker", "worker_session_id": "missing-worker-session",
        "worker_description": "missing journal worker", "is_new": False,
        "instructions_preview": "", "events": [],
    }]}], "forks": []}
    journal = Path(HOME) / "sessions" / root_id / "events.jsonl"
    assert not journal.exists()
    projection.schedule_rebuild(root_id, snapshot).result(timeout=5)
    assert projection._is_current(root_id)
    root = projection.root_manifest(root_id, sid, msg_id)
    rows = projection.children(root_id, sid, msg_id, root["id"], root["revision"], limit=50)["children"]
    assert [row["type"] for row in rows] == ["worker"]
    assert rows[0]["display_summary"] == "missing journal worker"
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_bytes(b"")
    assert not projection._is_current(root_id)


def test_event_payload_fails_closed_after_journal_disappears():
    root_id, sid, msg_id = "missing-event-root", "missing-event-root", "missing-event-message"
    journal = Path(HOME) / "sessions" / root_id / "events.jsonl"
    journal.parent.mkdir(parents=True, exist_ok=True)
    entry = {"seq": 1, "sid": sid, "msg_id": msg_id, "type": "error", "data": {
        "uuid": "missing-event", "error": "must not survive missing journal",
    }}
    raw = (json.dumps(entry) + "\n").encode()
    journal.write_bytes(raw)
    projection.note_event(root_id, entry, 0, len(raw))
    root = projection.root_manifest(root_id, sid, msg_id)
    journal.unlink()
    try:
        projection.children(root_id, sid, msg_id, root["id"], root["revision"], limit=50)
        raise AssertionError("event payload rendered without authoritative journal")
    except projection.ProjectionUnavailable:
        pass


def test_active_rebuild_is_not_queued_behind_background_migration():
    occupied, release = threading.Event(), threading.Event()
    blocker = projection._startup_executor.submit(lambda: (occupied.set(), release.wait(5)))
    assert occupied.wait(2)
    root_id = "priority-rebuild-root"
    try:
        future = projection.schedule_rebuild(
            root_id, {"id": root_id, "messages": [], "forks": []}, priority=True,
        )
        future.result(timeout=2)
        assert projection._is_current(root_id)
    finally:
        release.set()
        blocker.result(timeout=2)


def test_active_same_root_promotes_background_rebuild():
    root_id, sid, msg_id = "promoted-root", "promoted-root", "promoted-message"
    journal = Path(HOME) / "sessions" / root_id / "events.jsonl"
    journal.parent.mkdir(parents=True, exist_ok=True)
    entry = {"seq": 1, "sid": sid, "msg_id": msg_id, "type": "error", "data": {
        "uuid": "promoted-event", "error": "visible",
    }}
    journal.write_bytes((json.dumps(entry) + "\n").encode())
    entered, release, promoted_scheduled = threading.Event(), threading.Event(), threading.Event()
    first_note = True
    original_note = projection.note_event
    original_schedule = projection.schedule_rebuild
    promoted_futures = []

    def gated_note(*args):
        nonlocal first_note
        if first_note:
            first_note = False
            entered.set()
            assert release.wait(5)
        return original_note(*args)

    def observed_schedule(*args, **kwargs):
        future = original_schedule(*args, **kwargs)
        if kwargs.get("priority") is True and future is not None:
            promoted_futures.append(future)
            promoted_scheduled.set()
        return future

    with patch.object(projection, "note_event", side_effect=gated_note), patch.object(
        projection, "schedule_rebuild", side_effect=observed_schedule,
    ):
        background = original_schedule(root_id, None, priority=False)
        assert entered.wait(2)
        assert projection.schedule_rebuild(root_id, None, priority=True) is None
        release.set()
        background.result(timeout=5)
        assert promoted_scheduled.wait(2)
        promoted_futures[0].result(timeout=5)
    assert projection._is_current(root_id)


def test_journal_create_delete_replace_during_rebuild_never_publishes_ready():
    original_snapshot = projection._journal_snapshot
    for mutation in ("create", "delete", "replace"):
        root_id = f"journal-{mutation}-race-root"
        journal = Path(HOME) / "sessions" / root_id / "events.jsonl"
        journal.parent.mkdir(parents=True, exist_ok=True)
        if mutation != "create":
            journal.write_bytes(b"\n")
        calls = 0

        def racing_snapshot(candidate):
            nonlocal calls
            snapshot = original_snapshot(candidate)
            if candidate == root_id:
                calls += 1
                if calls == 3:
                    if mutation == "create":
                        journal.write_bytes(b"")
                    elif mutation == "delete":
                        journal.unlink()
                    else:
                        journal.write_bytes(b"{}\n")
            return snapshot

        with (
            patch.object(projection, "_journal_snapshot", side_effect=racing_snapshot),
            patch.object(projection.logger, "exception"),
        ):
            future = projection.schedule_rebuild(root_id, {"id": root_id, "messages": [], "forks": []})
            try:
                future.result(timeout=5)
                raise AssertionError(f"journal {mutation} race published ready projection")
            except projection.ProjectionUnavailable:
                pass
        assert not projection._is_current(root_id)


def test_live_append_during_rebuild_is_caught_up_before_ready():
    root_id, sid, msg_id = "rebuild-append-root", "rebuild-append-session", "rebuild-append-message"
    journal = Path(HOME) / "sessions" / root_id / "events.jsonl"
    journal.parent.mkdir(parents=True, exist_ok=True)

    def event(seq, suffix):
        return {
            "seq": seq, "sid": sid, "msg_id": msg_id, "type": "agent_message",
            "data": {
                "uuid": f"rebuild-{suffix}",
                "message": {"content": [{"type": "text", "text": suffix}]},
            },
        }

    first = (json.dumps(event(1, "first")) + "\n").encode()
    journal.write_bytes(first)
    scan_entered = threading.Event()
    scan_release = threading.Event()
    original_renderable = projection.is_renderable_event

    def gated_renderable(candidate):
        data = candidate.get("data") or {}
        if data.get("uuid") == "rebuild-first":
            scan_entered.set()
            assert scan_release.wait(5)
        return original_renderable(candidate)

    with patch.object(projection, "is_renderable_event", side_effect=gated_renderable):
        future = projection.schedule_rebuild(root_id, None)
        assert scan_entered.wait(5)
        second_event = event(2, "second")
        second = (json.dumps(second_event) + "\n").encode()
        with journal.open("ab") as handle:
            second_start = handle.tell()
            handle.write(second)
            handle.flush()
            second_end = handle.tell()
        live_errors = []
        live_done = threading.Event()

        def project_live_append():
            try:
                projection.note_event(root_id, second_event, second_start, second_end)
            except Exception as exc:
                live_errors.append(exc)
            finally:
                live_done.set()

        live = threading.Thread(target=project_live_append)
        live.start()
        try:
            assert live_done.wait(1)
            assert live_errors == []
        finally:
            scan_release.set()
        live.join(timeout=1)
        future.result(timeout=5)
        projection.ensure_current(root_id, None).result(timeout=5)

    root = projection.root_manifest(root_id, sid, msg_id)
    page = projection.children(
        root_id, sid, msg_id, root["id"], root["revision"], limit=50,
    )
    assert [row["display_summary"] for row in page["children"]] == ["first", "second"]


def test_append_after_publish_schedules_followup_before_rebuild_clear():
    root_id, sid, msg_id = "rebuild-boundary-root", "rebuild-boundary-session", "rebuild-boundary-message"
    journal = Path(HOME) / "sessions" / root_id / "events.jsonl"
    journal.parent.mkdir(parents=True, exist_ok=True)

    def event(seq, suffix):
        return {
            "seq": seq, "sid": sid, "msg_id": msg_id, "type": "agent_message",
            "data": {
                "uuid": f"boundary-{suffix}",
                "message": {"content": [{"type": "text", "text": suffix}]},
            },
        }

    first = (json.dumps(event(1, "first")) + "\n").encode()
    journal.write_bytes(first)
    first_published = threading.Event()
    second_published = threading.Event()
    release_first = threading.Event()
    release_second = threading.Event()
    submitted = []
    publish_count = 0
    original_submit = projection._ondemand_executor.submit

    def observed_submit(fn):
        future = original_submit(fn)
        submitted.append(future)
        return future

    def gate_publish(*_args, **_kwargs):
        nonlocal publish_count
        publish_count += 1
        if publish_count == 1:
            first_published.set()
            assert release_first.wait(5)
            return
        second_published.set()
        assert release_second.wait(5)

    with (
        patch.object(projection._ondemand_executor, "submit", side_effect=observed_submit),
        patch.object(projection.logger, "info", side_effect=gate_publish),
    ):
        waiter = projection.ensure_current(root_id, None)
        assert first_published.wait(5)
        second_event = event(2, "second")
        second = (json.dumps(second_event) + "\n").encode()
        with journal.open("ab") as handle:
            start = handle.tell()
            handle.write(second)
            handle.flush()
            end = handle.tell()
        projection.note_event(root_id, second_event, start, end)
        release_first.set()
        submitted[0].result(timeout=5)
        assert len(submitted) == 2
        assert second_published.wait(5)
        assert not waiter.done()
        release_second.set()
        submitted[1].result(timeout=5)
        assert waiter.result(timeout=1) is None

    root = projection.root_manifest(root_id, sid, msg_id)
    page = projection.children(
        root_id, sid, msg_id, root["id"], root["revision"], limit=50,
    )
    assert [row["display_summary"] for row in page["children"]] == ["first", "second"]


def test_mixed_startup_sweep_completes_present_and_missing_journals():
    present, missing = "startup-present", "startup-missing"
    present_journal = Path(HOME) / "sessions" / present / "events.jsonl"
    present_journal.parent.mkdir(parents=True, exist_ok=True)
    present_journal.write_bytes(b"")
    snapshots = {
        present: {"id": present, "messages": [], "forks": []},
        missing: {"id": missing, "messages": [], "forks": []},
    }
    owner = type("ProjectionOwner", (), {
        "list": lambda _self: [{"id": present}, {"id": missing}],
        "get_ref": lambda _self, root_id: snapshots[root_id],
    })()
    projection.schedule_all(owner)
    assert projection._is_current(present)
    assert projection._is_current(missing)


def test_connections_close_and_fd_count_stays_constant():
    fd_root, fd_sid, fd_msg = "fd-root", "fd-session", "fd-message"
    journal = Path(HOME) / "sessions" / fd_root / "events.jsonl"
    journal.parent.mkdir(parents=True, exist_ok=True)
    before = len(list(Path("/dev/fd").iterdir()))
    for seq in range(1, 501):
        entry = {"seq": seq, "sid": fd_sid, "msg_id": fd_msg, "type": "agent_message", "data": {"uuid": f"fd-{seq}"}}
        raw = (json.dumps(entry) + "\n").encode()
        with journal.open("ab") as handle:
            start = handle.tell(); handle.write(raw); end = handle.tell()
        projection.note_event(fd_root, entry, start, end)
    after = len(list(Path("/dev/fd").iterdir()))
    assert after <= before + 3, (before, after)


def test_valid_startup_skips_snapshot_and_rebuild():
    owner = type("ProjectionOwner", (), {
        "list": lambda _self: [{"id": ROOT}],
        "get_ref": lambda *_args: (_ for _ in ()).throw(AssertionError("loaded current root")),
    })()
    with patch.object(
        projection, "_is_current", return_value=True,
    ), patch.object(projection, "schedule_rebuild") as rebuild:
        projection.schedule_all(owner)
    rebuild.assert_not_called()


def test_same_size_journal_replacement_invalidates_sidecar():
    identity_root, identity_sid, identity_msg = "identity-root", "identity-session", "identity-message"
    journal = Path(HOME) / "sessions" / identity_root / "events.jsonl"
    journal.parent.mkdir(parents=True, exist_ok=True)
    entry = {"seq": 1, "sid": identity_sid, "msg_id": identity_msg, "type": "agent_message", "data": {"uuid": "identity"}}
    raw = (json.dumps(entry) + "\n").encode()
    journal.write_bytes(raw)
    projection.note_event(identity_root, entry, 0, len(raw))
    assert projection._is_current(identity_root)
    stat = journal.stat()
    journal.write_bytes(raw)
    os.utime(journal, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1))
    assert not projection._is_current(identity_root)


def test_append_lag_is_unavailable_then_incrementally_ready():
    lag_msg = "lag-message"
    append(400001, "lag-base", msg=lag_msg)
    root = manifest(lag_msg)
    entry = {"seq": 400002, "sid": SID, "msg_id": lag_msg, "type": "agent_message", "data": {
        "uuid": "lag-next", "message": {"content": [{"type": "text", "text": "next"}]},
    }}
    raw = (json.dumps(entry) + "\n").encode()
    with JOURNAL.open("ab") as handle:
        start = handle.tell(); handle.write(raw); end = handle.tell()
    try:
        projection.children(ROOT, SID, lag_msg, root["id"], root["revision"], limit=50)
        raise AssertionError("journal lag was served")
    except projection.ProjectionUnavailable:
        pass
    projection.note_event(ROOT, entry, start, end)
    refreshed = manifest(lag_msg)
    assert any(row["type"] == "agent_message" for row in children(refreshed["id"], refreshed["revision"], msg=lag_msg)["children"])


def test_rebuild_requests_coalesce_and_executors_are_bounded():
    pending_root = "coalesced-root"
    with patch.object(projection._ondemand_executor, "submit") as submit:
        projection.schedule_rebuild(pending_root, None)
        projection.schedule_rebuild(pending_root, None)
    assert submit.call_count == 1
    with projection._locks_guard:
        projection._rebuilding.discard(pending_root)
    assert projection._ondemand_executor._max_workers == 1
    assert projection._startup_executor._max_workers == 1


def test_ready_zero_at_indexed_eof_resumes_without_rescan_and_replays_pending_bootstrap():
    current = manifest(MSG)
    with projection._lock(ROOT), projection._connection(ROOT, create=True) as conn:
        conn.execute("INSERT OR REPLACE INTO meta VALUES('ready','0')")

    resume_entered = threading.Event()
    resume_release = threading.Event()
    pending_finished = threading.Event()
    original_resume = projection._resume_at_eof
    original_is_current = projection._is_current
    current_checks = 0

    def gated_resume(root_id, snapshot):
        resume_entered.set()
        assert resume_release.wait(5)
        return original_resume(root_id, snapshot)

    def observed_is_current(root_id):
        nonlocal current_checks
        current_checks += 1
        result = original_is_current(root_id)
        if current_checks == 2:
            pending_finished.set()
        return result

    with (
        patch.object(projection, "_resume_at_eof", side_effect=gated_resume),
        patch.object(projection, "_is_current", side_effect=observed_is_current),
        patch.object(projection, "note_event", side_effect=AssertionError("EOF resume rescanned journal")),
    ):
        future = projection.schedule_rebuild(ROOT, None)
        assert resume_entered.wait(5)
        assert projection.schedule_rebuild(ROOT, None) is None
        resume_release.set()
        future.result(timeout=5)
        assert pending_finished.wait(5)

    assert projection._is_current(ROOT)
    resumed = projection.root_manifest(ROOT, SID, MSG)
    assert resumed["revision"] == current["revision"]


def test_emfile_open_failure_does_not_corrupt_valid_sidecar():
    path = projection._path(ROOT)
    before = path.read_bytes()
    with patch.object(sqlite3, "connect", side_effect=OSError(24, "Too many open files")):
        try:
            projection.root_manifest(ROOT, SID, MSG)
            raise AssertionError("EMFILE was served")
        except projection.ProjectionUnavailable:
            pass
    assert path.read_bytes() == before


def _child(code: str) -> subprocess.Popen:
    env = {**os.environ, "BETTER_AGENT_HOME": HOME, "PYTHONPATH": str(Path(__file__).parents[1])}
    return subprocess.Popen(
        [sys.executable, "-c", code], env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )


def _sidecar_bytes(root_id: str) -> dict[str, bytes]:
    path = projection._path(root_id)
    return {
        suffix: candidate.read_bytes()
        for suffix in ("", "-wal", "-shm")
        if (candidate := Path(str(path) + suffix)).exists()
    }


def _authoritative_bytes(root_id: str) -> dict[str, bytes]:
    return {key: value for key, value in _sidecar_bytes(root_id).items() if key != "-shm"}


def test_read_only_open_sees_committed_wal_without_mutating_sidecars():
    root = "readonly-wal-root"
    projection.note_event(root, {}, 0, 0)
    projection.note_workers(root, root, "message", [])
    path = projection._path(root)
    writer = sqlite3.connect(path)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("UPDATE messages SET direct_child_count=7 WHERE sid=? AND msg_id=?", (root, "message"))
    writer.commit()
    before = _authoritative_bytes(root)
    sidecars = set(_sidecar_bytes(root))
    meta = writer.execute("SELECT key,value FROM meta ORDER BY key").fetchall()
    assert projection.root_manifest(root, root, "message")["direct_child_count"] == 7
    assert _authoritative_bytes(root) == before
    assert set(_sidecar_bytes(root)) == sidecars
    assert writer.execute("SELECT key,value FROM meta ORDER BY key").fetchall() == meta
    writer.close()


def test_missing_read_only_open_creates_nothing_and_executes_no_initialization():
    root = "missing-readonly-root"
    path = projection._path(root)
    before = set(path.parent.iterdir()) if path.parent.exists() else set()
    queries = []
    prior = projection._query_observer
    projection._query_observer = queries.append
    try:
        try:
            projection.root_manifest(root, root, "message")
            raise AssertionError("missing projection was served")
        except projection.ProjectionUnavailable:
            pass
    finally:
        projection._query_observer = prior
    after = set(path.parent.iterdir()) if path.parent.exists() else set()
    assert after == before
    assert queries == []


def test_external_writer_does_not_block_or_mutate_read_only_manifest():
    root = "external-writer-root"
    projection.note_event(root, {}, 0, 0)
    projection.note_workers(root, root, "message", [])
    writer = sqlite3.connect(projection._path(root))
    writer.execute("BEGIN IMMEDIATE")
    before = _authoritative_bytes(root)
    sidecars = set(_sidecar_bytes(root))
    meta = writer.execute("SELECT key,value FROM meta ORDER BY key").fetchall()
    assert projection.root_manifest(root, root, "message")["id"] == "message-message"
    assert _authoritative_bytes(root) == before
    assert set(_sidecar_bytes(root)) == sidecars
    assert writer.execute("SELECT key,value FROM meta ORDER BY key").fetchall() == meta
    writer.rollback()
    writer.close()


def test_cooperating_writer_waits_for_serializer_then_commits():
    root = "serializer-wait-root"
    projection.note_event(root, {}, 0, 0)
    projection.note_workers(root, root, "message", [])
    child = _child(
        "import time; import historical_children_projection as p\n"
        f"with p._lock({root!r}), p._connection({root!r}, create=True) as c:\n"
        " c.execute(\"UPDATE meta SET value=value WHERE key='ready'\")\n"
        " print('locked', flush=True)\n"
        " time.sleep(1.2)\n"
    )
    assert child.stdout.readline().strip() == "locked"
    started = time.monotonic()
    projection.note_workers(root, root, "message", [{"id": "waited"}])
    assert time.monotonic() - started >= 1.0
    stdout, stderr = child.communicate(timeout=5)
    assert child.returncode == 0, stdout + stderr
    assert projection.root_manifest(root, root, "message")["direct_child_count"] == 1


def test_simultaneous_first_create_and_crashed_lock_release():
    root = "simultaneous-create-root"
    code = (
        "import historical_children_projection as p\n"
        f"p.note_event({root!r}, {{}}, 0, 0)\n"
        f"p.note_workers({root!r}, {root!r}, 'message', [])\n"
    )
    children = [_child(code), _child(code)]
    for child in children:
        stdout, stderr = child.communicate(timeout=10)
        assert child.returncode == 0, stdout + stderr
    assert projection.root_manifest(root, root, "message")["direct_child_count"] == 0

    crash_root = "crashed-lock-root"
    child = _child(
        "import os; import historical_children_projection as p\n"
        f"with p._sidecar_lock({crash_root!r}):\n"
        " print('locked', flush=True)\n"
        " os._exit(0)\n"
    )
    assert child.stdout.readline().strip() == "locked"
    child.wait(timeout=5)
    projection.note_event(crash_root, {}, 0, 0)
    projection.note_workers(crash_root, crash_root, "message", [])
    assert projection.root_manifest(crash_root, crash_root, "message")


def test_busy_preserves_sidecars_and_never_schedules_rebuild():
    root = "busy-preservation-root"
    projection.note_event(root, {}, 0, 0)
    projection.note_workers(root, root, "message", [])
    writer = sqlite3.connect(projection._path(root))
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("UPDATE meta SET value=value WHERE key='ready'")
    before = _sidecar_bytes(root)
    with patch.object(projection, "schedule_rebuild", side_effect=AssertionError("busy scheduled rebuild")):
        try:
            projection.note_workers(root, root, "message", [{"id": "blocked"}])
            raise AssertionError("busy mutation succeeded")
        except projection.ProjectionBusy:
            pass
    assert _sidecar_bytes(root) == before
    writer.rollback()
    writer.close()


def test_mutation_and_rebuild_follow_global_lock_order():
    import hydration_index_store

    root = "lock-order-root"
    journal_held = False
    original_guard = hydration_index_store.journal_guard
    original_sidecar = projection._sidecar_lock

    @projection.contextmanager
    def observed_guard(*args, **kwargs):
        nonlocal journal_held
        with original_guard(*args, **kwargs):
            journal_held = True
            try:
                yield
            finally:
                journal_held = False

    @projection.contextmanager
    def observed_sidecar(root_id):
        assert projection._lock(root_id)._is_owned()
        if getattr(projection._rebuild_local, "root_id", None) == root_id:
            assert journal_held
        with original_sidecar(root_id):
            yield

    with patch.object(projection, "_sidecar_lock", observed_sidecar):
        projection.note_event(root, {}, 0, 0)
        with patch.object(hydration_index_store, "journal_guard", observed_guard):
            projection.schedule_rebuild(root, None).result(timeout=10)


def test_current_waiters_cover_zero_manifest_coalescing_and_cancellation():
    empty_root = "projection-ready-empty-root"
    snapshot = {"id": empty_root, "messages": [], "forks": []}
    ready = projection.ensure_current(empty_root, snapshot, priority=True)
    ready.result(timeout=5)
    revision, manifests = projection.root_manifests(empty_root, empty_root, [])
    assert isinstance(revision, int)
    assert manifests == {}

    waiting_root = "projection-ready-waiters"
    with (
        patch.object(projection, "_is_current", return_value=False),
        patch.object(projection, "schedule_rebuild", return_value=object()),
    ):
        cancelled = projection.ensure_current(waiting_root, None)
        active = projection.ensure_current(waiting_root, None)
    assert cancelled.cancel()
    projection._settle_current_waiters(waiting_root)
    projection._settle_current_waiters(waiting_root)
    assert active.result(timeout=1) is None
    assert waiting_root not in projection._current_waiters

    class CancelDuringSettlement:
        def cancelled(self):
            return False

        def done(self):
            return False

        def set_result(self, _value):
            raise projection.InvalidStateError()

    survivor: projection.Future = projection.Future()
    with projection._locks_guard:
        projection._current_waiters[waiting_root] = {CancelDuringSettlement(), survivor}
    projection._settle_current_waiters(waiting_root)
    assert survivor.result(timeout=1) is None


def test_projection_executor_reopens_for_a_new_app_lifespan():
    projection.shutdown()
    projection.reopen()
    root = "reopened-projection-root"
    snapshot = {"id": root, "messages": [], "forks": []}
    projection.ensure_current(root, snapshot).result(timeout=5)
    revision, manifests = projection.root_manifests(root, root, [])
    assert isinstance(revision, int)
    assert manifests == {}


if __name__ == "__main__":
    tests = [
        test_one_level_and_bounded_payload_refs,
        test_lifecycle_only_has_no_visible_children_or_revision_churn,
        test_mixed_raw_eight_projects_exactly_three_visible_rows,
        test_visible_pagination_skips_hidden_records_without_short_pages,
        test_hidden_parent_normalizes_visible_nested_adjacency,
        test_empty_agent_message_parent_is_skipped_but_visible_descendant_is_reachable,
        test_hidden_append_preserves_cursor_visible_append_invalidates_it,
        test_cursor_pagination_is_complete_bound_and_tamper_proof,
        test_root_path_is_confined,
        test_out_of_order_and_cycle_fail_closed_to_root,
        test_workers_revision_persistence_and_isolation,
        test_orphan_ownership_resolution_folds_pointer_without_scan,
        test_limit_and_missing_projection,
        test_locked_manifest_releases_after_exception,
        test_corrupt_sidecar_rebuilds_in_background_without_partial_reads,
        test_schedule_all_loads_durable_unloaded_worker_tree,
        test_missing_journal_worker_projection_is_current_and_readable,
        test_event_payload_fails_closed_after_journal_disappears,
        test_active_rebuild_is_not_queued_behind_background_migration,
        test_active_same_root_promotes_background_rebuild,
        test_journal_create_delete_replace_during_rebuild_never_publishes_ready,
        test_live_append_during_rebuild_is_caught_up_before_ready,
        test_append_after_publish_schedules_followup_before_rebuild_clear,
        test_mixed_startup_sweep_completes_present_and_missing_journals,
        test_connections_close_and_fd_count_stays_constant,
        test_valid_startup_skips_snapshot_and_rebuild,
        test_same_size_journal_replacement_invalidates_sidecar,
        test_append_lag_is_unavailable_then_incrementally_ready,
        test_rebuild_requests_coalesce_and_executors_are_bounded,
        test_ready_zero_at_indexed_eof_resumes_without_rescan_and_replays_pending_bootstrap,
        test_current_waiters_cover_zero_manifest_coalescing_and_cancellation,
        test_emfile_open_failure_does_not_corrupt_valid_sidecar,
        test_read_only_open_sees_committed_wal_without_mutating_sidecars,
        test_missing_read_only_open_creates_nothing_and_executes_no_initialization,
        test_external_writer_does_not_block_or_mutate_read_only_manifest,
        test_cooperating_writer_waits_for_serializer_then_commits,
        test_simultaneous_first_create_and_crashed_lock_release,
        test_busy_preserves_sidecars_and_never_schedules_rebuild,
        test_mutation_and_rebuild_follow_global_lock_order,
        test_100k_unrelated_rows_do_not_widen_read_bytes,
        test_long_turn_append_queries_are_constant_and_rebuild_converges,
        test_projection_executor_reopens_for_a_new_app_lifespan,
    ]
    try:
        for test in tests:
            test()
            print("PASS", test.__name__)
    finally:
        shutil.rmtree(HOME, ignore_errors=True)
