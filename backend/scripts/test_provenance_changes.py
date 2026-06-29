"""Tests for the file-change projection in provenance_store (the Changes
right-panel data source). Verifies file-edit tools normalize and non-edit
tools are dropped, across Claude/Codex tool-name variants."""

import os
import sys
import tempfile

os.environ.setdefault("BETTER_AGENT_HOME", tempfile.mkdtemp(prefix="ba-prov-changes-"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from stores import provenance_store  # noqa: E402


SID = "test-provenance-changes-sid"


def _event(uuid: str, content: list) -> dict:
    return {
        "uuid": uuid,
        "timestamp": "2026-06-28T12:00:00Z",
        "data": {
            "type": "assistant",
            "uuid": uuid,
            "timestamp": "2026-06-28T12:00:00Z",
            "message": {"id": uuid, "role": "assistant", "content": content},
        },
    }


def _text(t: str) -> dict:
    return {"type": "text", "text": t}


def _tool(name: str, inp: dict, tid: str) -> dict:
    return {"type": "tool_use", "id": tid, "name": name, "input": inp}


def setup_function(_):
    try:
        os.remove(provenance_store._path(SID))
    except FileNotFoundError:
        pass
    provenance_store._seen.pop(SID, None)


def test_edit_normalized_with_reasoning():
    ev = _event("m1", [
        _text("Fixing the auth bug."),
        _tool("Edit", {"file_path": "/a/b.ts", "old_string": "x", "new_string": "y"}, "tu1"),
    ])
    provenance_store.record_from_event(SID, ev)
    changes = provenance_store.read_file_changes(SID)
    assert len(changes) == 1
    c = changes[0]
    assert c["kind"] == "edit"
    assert c["file_path"] == "/a/b.ts"
    assert c["edits"] == [{"old_string": "x", "new_string": "y"}]
    assert c["why"] == "Fixing the auth bug."


def test_write_is_create_with_empty_old():
    ev = _event("m2", [
        _tool("Write", {"file_path": "/new.txt", "content": "hello"}, "tu2"),
    ])
    provenance_store.record_from_event(SID, ev)
    c = provenance_store.read_file_changes(SID)[0]
    assert c["kind"] == "create"
    assert c["edits"] == [{"old_string": "", "new_string": "hello"}]


def test_codex_tool_name_variants():
    ev = _event("m3", [
        _tool("edit_file", {"path": "/c.go", "old_string": "1", "new_string": "2"}, "tu3"),
        _tool("write_file", {"filename": "/d.go", "file_text": "go"}, "tu4"),
    ])
    provenance_store.record_from_event(SID, ev)
    out = {(c["file_path"], c["kind"]) for c in provenance_store.read_file_changes(SID)}
    assert ("/c.go", "edit") in out
    assert ("/d.go", "create") in out


def test_multiedit_multiple_pairs():
    ev = _event("m4", [
        _tool("MultiEdit", {"file_path": "/m.ts", "edits": [
            {"old_string": "a", "new_string": "A"},
            {"old_string": "b", "new_string": "B"},
        ]}, "tu5"),
    ])
    provenance_store.record_from_event(SID, ev)
    c = provenance_store.read_file_changes(SID)[0]
    assert c["kind"] == "edit"
    assert len(c["edits"]) == 2


def test_apply_patch_is_patch_kind():
    ev = _event("m5", [
        _tool("apply_patch", {"patch": "--- a\n+++ b\n"}, "tu6"),
    ])
    provenance_store.record_from_event(SID, ev)
    c = provenance_store.read_file_changes(SID)[0]
    assert c["kind"] == "patch"
    assert c["edits"] == [{"old_string": "", "new_string": "--- a\n+++ b\n"}]


def test_non_edit_tools_dropped():
    ev = _event("m6", [
        _text("Running things."),
        _tool("Bash", {"command": "ls"}, "tu7"),
        _tool("Read", {"file_path": "/r.ts"}, "tu8"),
    ])
    provenance_store.record_from_event(SID, ev)
    assert provenance_store.read_file_changes(SID) == []


def _assistant_msg(mid, content):
    return {"id": mid, "role": "assistant", "content": content}


def _user_msg(text):
    return {"id": f"u-{text[:4]}", "role": "user", "content": text}


def test_group_changes_by_turn_buckets_by_user_prompt():
    # Two turns: each has a user prompt + an assistant msg whose edits land in
    # that turn. msg_id on the change matches the assistant msg id.
    provenance_store.record_from_event(SID, _event("a1", [
        _tool("Edit", {"file_path": "/a.ts", "old_string": "x", "new_string": "y"}, "tu-a1"),
    ]))
    provenance_store.record_from_event(SID, _event("a2", [
        _tool("Edit", {"file_path": "/b.ts", "old_string": "1", "new_string": "2"}, "tu-a2"),
    ]))
    changes = provenance_store.read_file_changes(SID)
    messages = [
        _user_msg("fix the bug"),
        _assistant_msg("a1", []),
        _user_msg("refactor it"),
        _assistant_msg("a2", []),
    ]
    turns = provenance_store.group_changes_by_turn(messages, changes)
    assert [t["turn_index"] for t in turns] == [0, 1]
    assert turns[0]["user_prompt"] == "fix the bug"
    assert turns[1]["user_prompt"] == "refactor it"
    assert turns[0]["changes"][0]["file_path"] == "/a.ts"
    assert turns[1]["changes"][0]["file_path"] == "/b.ts"


def test_group_changes_by_turn_ungrouped_bucket():
    provenance_store.record_from_event(SID, _event("a1", [
        _tool("Edit", {"file_path": "/a.ts", "old_string": "x", "new_string": "y"}, "tu-a1"),
    ]))
    changes = provenance_store.read_file_changes(SID)
    # No matching assistant msg in the render tree → ungrouped (turn -1).
    turns = provenance_store.group_changes_by_turn([], changes)
    assert len(turns) == 1 and turns[0]["turn_index"] == -1
    assert turns[0]["changes"][0]["file_path"] == "/a.ts"


def test_group_changes_by_turn_user_content_as_blocks():
    provenance_store.record_from_event(SID, _event("a1", [
        _tool("Edit", {"file_path": "/a.ts", "old_string": "x", "new_string": "y"}, "tu-a1"),
    ]))
    changes = provenance_store.read_file_changes(SID)
    messages = [
        {"id": "u1", "role": "user", "content": [
            {"type": "text", "text": "hello"},
            {"type": "text", "text": "world"},
        ]},
        _assistant_msg("a1", []),
    ]
    turns = provenance_store.group_changes_by_turn(messages, changes)
    assert turns[0]["user_prompt"] == "hello world"
