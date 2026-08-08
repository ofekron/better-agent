"""Dedicated pytest owner for ``stores/provenance_store.py``.

Closes the unit-tier gap the script-style ``test_provenance_store.py``
(collects 0 items under pytest) leaves unowned: the validation, dedup,
hydration, read-projection, and timestamp/turn-grouping branches that the
incidental owners (``test_provenance_changes.py``,
``test_provenance_ledger_worker.py``) do not reach. Every test asserts
real behavior; no line-touchers.
"""

import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

_BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_BACKEND))

import _test_home  # noqa: E402
import pytest  # noqa: E402

# Inherited-home canary: prove isolate() overrides a leaked/real home rather
# than silently writing into it.
_INHERITED_HOME = tempfile.TemporaryDirectory(prefix="ba-prov-unit-inherited-")
_INHERITED_CANARY = Path(_INHERITED_HOME.name) / "preserve"
_INHERITED_CANARY.write_text("preserve", encoding="utf-8")
os.environ["BETTER_AGENT_HOME"] = _INHERITED_HOME.name
os.environ["BETTER_CLAUDE_HOME"] = _INHERITED_HOME.name
_TEST_HOME = _test_home.isolate("ba-prov-unit-")

from stores import provenance_store  # noqa: E402


def _prov_dir() -> str:
    return os.path.join(os.environ["BETTER_AGENT_HOME"], "provenance")


@pytest.fixture(autouse=True)
def _clean_provenance_state(monkeypatch):
    """Each test starts with no on-disk provenance and an empty dedup cache."""
    monkeypatch.setenv("BETTER_AGENT_HOME", _TEST_HOME)
    monkeypatch.setenv("BETTER_CLAUDE_HOME", _TEST_HOME)
    provenance_store._seen.clear()
    if os.path.isdir(_prov_dir()):
        shutil.rmtree(_prov_dir())
    yield
    provenance_store._seen.clear()
    if os.path.isdir(_prov_dir()):
        shutil.rmtree(_prov_dir())


def test_test_home_overrides_inherited_state_home():
    test_home = Path(_TEST_HOME).resolve()
    assert Path(os.environ["BETTER_AGENT_HOME"]).resolve() == test_home
    assert Path(os.environ["BETTER_CLAUDE_HOME"]).resolve() == test_home
    assert Path(provenance_store._path("any-sid")).resolve().is_relative_to(test_home)
    assert _INHERITED_CANARY.read_text(encoding="utf-8") == "preserve"


# ── _path validation ──────────────────────────────────────────────────

@pytest.mark.parametrize("bad", ["../x", "", ".", "..", "a/b", "sub/dir"])
def test_path_rejects_unsafe_app_session_id(bad):
    with pytest.raises(ValueError):
        provenance_store._path(bad)


def test_path_accepts_safe_id_and_lives_under_prov_dir():
    p = provenance_store._path("safe-id-123")
    assert p.endswith(os.path.join("provenance", "safe-id-123.jsonl"))


# ── extract ───────────────────────────────────────────────────────────

def _event(content, *, uuid="e1", msg_id="m", timestamp=None):
    return {
        "uuid": uuid,
        "timestamp": timestamp,
        "data": {
            "type": "assistant",
            "uuid": uuid,
            "timestamp": timestamp,
            "message": {"id": msg_id, "role": "assistant", "content": content},
        },
    }


def test_extract_non_list_content_returns_empty():
    assert provenance_store.extract(_event("a plain string")) == []
    assert provenance_store.extract({"uuid": "x", "data": {"message": {}}}) == []


def test_extract_skips_non_dict_blocks_and_empty_reasoning():
    ev = _event([
        "not-a-dict",                       # non-dict block -> skipped
        {"type": "text", "text": ""},       # empty text -> not appended to why
        {"type": "thinking", "thinking": ""},  # empty thinking -> not appended
        {"type": "tool_use", "name": "Bash", "input": {"x": 1}},  # no id -> uuid fallback
    ])
    rows = provenance_store.extract(ev)
    assert len(rows) == 1
    assert rows[0]["uuid"] == "e1"          # falls back to normalized uuid
    assert rows[0]["tool"] == "Bash"
    assert rows[0]["input"] == {"x": 1}
    assert rows[0]["why"] == ""             # nothing preceded it


def test_extract_continues_loop_after_tool_use():
    # A block after a tool_use forces the extract loop back-edge past the
    # tool_use append (only the tool_use yields a row).
    ev = _event([
        {"type": "tool_use", "id": "tu", "name": "Edit", "input": {}},
        {"type": "text", "text": "trailing block"},
    ])
    rows = provenance_store.extract(ev)
    assert len(rows) == 1


def test_extract_ignores_unrecognized_block_type():
    # A block whose type is neither thinking/text nor tool_use is skipped
    # via the elif-False loop continuation.
    ev = _event([
        {"type": "image", "source": {}},
        {"type": "tool_use", "id": "tu", "name": "Edit", "input": {}},
    ])
    rows = provenance_store.extract(ev)
    assert len(rows) == 1
    assert rows[0]["tool"] == "Edit"


def test_extract_msg_id_resolution():
    ev = _event([{"type": "tool_use", "id": "tu", "name": "Edit", "input": {}}],
                uuid="n1", msg_id="pmid", timestamp="2026-01-01T00:00:00Z")
    row = provenance_store.extract(ev)[0]
    assert row["msg_id"] == "pmid"
    assert row["provider_msg_id"] == "pmid"
    assert row["ts"] == "2026-01-01T00:00:00Z"
    row_override = provenance_store.extract(ev, backend_msg_id="bmid")[0]
    assert row_override["msg_id"] == "bmid"


def test_extract_text_then_tool_joins_reasoning():
    ev = _event([
        {"type": "thinking", "thinking": "Plan A."},
        {"type": "text", "text": "Step two."},
        {"type": "tool_use", "id": "tu", "name": "Edit", "input": {}},
    ])
    row = provenance_store.extract(ev)[0]
    assert row["why"] == "Plan A. Step two."


# ── record / _hydrate_seen / dedup ────────────────────────────────────

def test_record_empty_rows_returns_zero():
    assert provenance_store.record("sid-empty", []) == 0


def test_record_from_event_without_tools_returns_zero():
    ev = _event([{"type": "text", "text": "just talking"}])
    assert provenance_store.record_from_event("sid-notool", ev) == 0


def test_record_dedups_within_single_call():
    sid = "sid-dedup"
    n = provenance_store.record(sid, [
        {"uuid": "d1", "tool": "X"},
        {"uuid": "d1", "tool": "X"},   # duplicate within the batch
    ])
    assert n == 1
    assert len(provenance_store.read(sid)) == 1


def test_hydrate_seen_reads_existing_file_with_malformed_lines():
    """First touch of a sid with an existing file hydrates the dedup set
    from disk; malformed lines are skipped without aborting the read."""
    sid = "sid-hydrate"
    p = provenance_store._path(sid)
    with open(p, "w", encoding="utf-8") as f:
        f.write(json.dumps({"uuid": "seed-1"}) + "\n")   # valid
        f.write("not-a-json-line\n")                       # ValueError -> skip
        f.write(json.dumps("bare-string") + "\n")         # no .get -> skip
        f.write(json.dumps({"no-uuid": True}) + "\n")    # valid dict, uid None -> skip
        f.write("\n")                                      # blank -> skip

    seen = provenance_store._hydrate_seen(sid)
    assert "seed-1" in seen

    # Already-seeded uuid is deduped (uid-in-seen branch -> 0 written).
    assert provenance_store.record(sid, [{"uuid": "seed-1", "tool": "X"}]) == 0
    # A fresh uuid appends (1 written) and lands on disk.
    assert provenance_store.record(sid, [{"uuid": "seed-2", "tool": "X"}]) == 1
    # Force a fresh disk read (clear cache): both valid uuids hydrate.
    provenance_store._seen.pop(sid, None)
    assert provenance_store._hydrate_seen(sid) == {"seed-1", "seed-2"}


def test_hydrate_seen_caches_after_first_touch():
    sid = "sid-cache"
    provenance_store.record(sid, [{"uuid": "c1", "tool": "X"}])
    first = provenance_store._hydrate_seen(sid)
    second = provenance_store._hydrate_seen(sid)
    assert first is second                  # same cached object, no re-read


# ── read ──────────────────────────────────────────────────────────────

def test_read_missing_file_returns_empty():
    assert provenance_store.read("never-recorded-sid") == []


def test_read_returns_empty_on_malformed_line():
    # read() is strict: a single corrupt line fails the whole read closed
    # (unlike _hydrate_seen, which tolerates bad lines).
    sid = "sid-badread"
    p = provenance_store._path(sid)
    with open(p, "w", encoding="utf-8") as f:
        f.write(json.dumps({"uuid": "ok"}) + "\n")
        f.write("not-json\n")
    assert provenance_store.read(sid) == []


def test_read_limit_keeps_most_recent():
    sid = "sid-readlim"
    provenance_store.record(sid, [{"uuid": f"r{i}", "tool": "X"} for i in range(3)])
    assert [r["uuid"] for r in provenance_store.read(sid)] == ["r0", "r1", "r2"]
    last2 = provenance_store.read(sid, limit=2)
    assert [r["uuid"] for r in last2] == ["r1", "r2"]


# ── normalize_change (file-change projection edges) ───────────────────

def test_normalize_change_non_edit_returns_none():
    assert provenance_store.normalize_change({"tool": "Bash", "input": {}}) is None


def test_normalize_change_input_not_dict_treated_as_empty():
    # input=None -> coerced to {} -> edit still projects with empty pair.
    c = provenance_store.normalize_change({"tool": "Edit", "input": None})
    assert c is not None
    assert c["kind"] == "edit"
    assert c["edits"] == [{"old_string": "", "new_string": ""}]
    assert c["file_path"] is None


def test_normalize_change_write_is_create():
    c = provenance_store.normalize_change(
        {"tool": "Write", "input": {"file_path": "/n.txt", "content": "body"}}
    )
    assert c["kind"] == "create"
    assert c["file_path"] == "/n.txt"
    assert c["edits"] == [{"old_string": "", "new_string": "body"}]

    # Codex variant: path/file_text keys, no content.
    c2 = provenance_store.normalize_change(
        {"tool": "write_file", "input": {"path": "/d.go", "file_text": "ft"}}
    )
    assert c2["kind"] == "create"
    assert c2["file_path"] == "/d.go"
    assert c2["edits"] == [{"old_string": "", "new_string": "ft"}]


def test_normalize_change_empty_multi_edits_returns_none():
    # edit family with an empty edits list -> kind set, edits empty -> None
    assert provenance_store.normalize_change(
        {"tool": "MultiEdit", "input": {"file_path": "/x", "edits": []}}
    ) is None


def test_normalize_change_patch_tools():
    c = provenance_store.normalize_change(
        {"tool": "foo.apply_patch", "input": {"file_path": "/p", "patch": "@@ diff"}}
    )
    assert c["kind"] == "patch"
    assert c["edits"] == [{"old_string": "", "new_string": "@@ diff"}]
    assert c["file_path"] == "/p"

    c2 = provenance_store.normalize_change(
        {"tool": "apply_patch", "input": {"input": "raw patch text"}}
    )
    assert c2["kind"] == "patch"
    assert c2["edits"][0]["new_string"] == "raw patch text"
    assert c2["file_path"] is None


def test_normalize_change_notebook_edit_uses_notebook_path():
    c = provenance_store.normalize_change(
        {"tool": "NotebookEdit", "input": {"notebook_path": "/n.ipynb",
                                           "old_string": "a", "new_string": "b"}}
    )
    assert c["kind"] == "edit"
    assert c["file_path"] == "/n.ipynb"
    assert c["edits"] == [{"old_string": "a", "new_string": "b"}]


def test_read_file_changes_drops_non_edit_tools():
    sid = "sid-changes"
    provenance_store.record(sid, [
        {"uuid": "edit1", "tool": "Edit",
         "input": {"file_path": "/a", "old_string": "x", "new_string": "y"}, "why": "fix"},
        {"uuid": "bash1", "tool": "Bash", "input": {"command": "ls"}, "why": "look"},
    ])
    changes = provenance_store.read_file_changes(sid)
    assert len(changes) == 1
    assert changes[0]["uuid"] == "edit1"
    assert changes[0]["why"] == "fix"


# ── timestamp parsing & turn grouping ─────────────────────────────────

def test_parse_event_ts_branches():
    assert provenance_store._parse_event_ts(123) is None       # non-str
    assert provenance_store._parse_event_ts("") is None        # empty
    assert provenance_store._parse_event_ts("not-a-date") is None  # ValueError

    z = provenance_store._parse_event_ts("2026-01-01T00:00:00Z")  # Z suffix
    assert z == datetime(2026, 1, 1, tzinfo=timezone.utc)

    naive = provenance_store._parse_event_ts("2026-01-01T00:00:00")  # naive -> localized
    assert naive is not None
    assert naive.tzinfo is not None
    assert naive.astimezone(timezone.utc).year == 2026


def test_turn_for_ts_branches():
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    t5 = datetime(2026, 1, 1, 0, 5, tzinfo=timezone.utc)
    assert provenance_store._turn_for_ts([(0, t0)], t5) == 0   # matched
    assert provenance_store._turn_for_ts([(0, t5)], t0) is None  # every start > ts
    assert provenance_store._turn_for_ts([(0, t0)], None) is None  # ts None


def test_user_prompt_text_variants():
    assert provenance_store._user_prompt_text({"content": " hi "}) == "hi"
    assert provenance_store._user_prompt_text(
        {"content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}
    ) == "a b"
    assert provenance_store._user_prompt_text({"content": 123}) == ""
    assert provenance_store._user_prompt_text({"content": [{"type": "image"}]}) == ""


def test_group_changes_by_turn_ts_fallback_and_message_edges():
    """Covers: assistant-msg turn assignment + loop continuation, list-content
    user prompt, ts-fallback matching for unknown msg_ids across turns, and
    non-dict message skip."""
    messages = [
        {"role": "user", "id": "u1", "timestamp": "2026-01-01T00:00:00Z",
         "content": [{"type": "text", "text": "do the thing"}]},
        {"role": "assistant", "id": "a1", "content": []},   # turn 0; loop continues
        {"role": "user", "id": "u2", "timestamp": "2026-01-02T00:00:00Z",
         "content": "second turn"},
        "not-a-dict-message",                                # skipped (non-dict)
    ]
    changes = [
        {"msg_id": "unknown1", "ts": "2026-01-01T00:05:00Z",   # ts -> turn 0
         "tool": "Edit", "file_path": "/x"},
        {"msg_id": "unknown2", "ts": "2026-01-02T00:05:00Z",   # ts -> turn 1
         "tool": "Edit", "file_path": "/y"},
    ]
    grouped = provenance_store.group_changes_by_turn(messages, changes)
    by_turn = {g["turn_index"]: g for g in grouped}
    assert by_turn[0]["user_prompt"] == "do the thing"
    assert [c["file_path"] for c in by_turn[0]["changes"]] == ["/x"]
    assert by_turn[1]["user_prompt"] == "second turn"
    assert [c["file_path"] for c in by_turn[1]["changes"]] == ["/y"]


def test_group_changes_by_turn_ungrouped_bucket_for_unknown_change():
    """A change with no matching msg_id AND no parseable/within-range ts
    lands in the trailing turn_index = -1 'ungrouped' bucket."""
    messages = [{"role": "user", "id": "u1", "content": "hi"}]
    changes = [{"msg_id": "orphan", "ts": "not-a-ts", "tool": "Edit", "file_path": "/y"}]
    grouped = provenance_store.group_changes_by_turn(messages, changes)
    last = grouped[-1]
    assert last["turn_index"] == -1
    assert last["user_prompt"] == ""
    assert len(last["changes"]) == 1


def test_group_changes_by_turn_handles_empty_messages():
    grouped = provenance_store.group_changes_by_turn([], [])
    assert grouped == []


def test_group_changes_by_turn_direct_msg_match_and_non_assistant_roles():
    """Covers: a change whose msg_id matches an assistant directly (no ts
    fallback), a non-user/non-assistant message role, and an assistant
    message with no id."""
    messages = [
        {"role": "user", "id": "u1", "timestamp": "2026-01-01T00:00:00Z",
         "content": "go"},
        {"role": "system", "content": "system-prompt"},          # neither role -> skip
        {"role": "assistant", "id": "a1", "content": []},        # matched directly
        {"role": "assistant", "content": []},                    # no id -> skipped
    ]
    changes = [
        {"msg_id": "a1", "ts": "2026-01-01T00:05:00Z", "tool": "Edit", "file_path": "/m"},
    ]
    grouped = provenance_store.group_changes_by_turn(messages, changes)
    assert grouped[0]["turn_index"] == 0
    assert len(grouped[0]["changes"]) == 1
    assert grouped[0]["changes"][0]["file_path"] == "/m"
