#!/usr/bin/env python3
"""Dedicated unit coverage for the one-time file-ref migration subsystem in
backend/file_ref_resolver.py (lines ~437-715): the copy-on-write isolate
helpers, `rewrite_event_data_isolated`, and the on-disk migration
(`_migrate_message_node` / `_migrate_session_node` / `_atomic_write_tmp` /
`_migrate_session_file` / `_migrate_events_jsonl` / `migrate_all` /
`run_migration_once`).

These rewrite historical session JSON + per-root events.jsonl on disk so
existing sessions gain bcfile: links without waiting for new turns. They are
pure file-IO over a session home, so they are covered hermetically against an
isolated BETTER_AGENT_HOME tempdir — no real state is ever touched.

`migrate_all` enumerates session files via `session_store._session_json_files`,
which resolves `_sessions_dir()` to `ba_home()/"sessions"`; engaging the test
home routes that enumeration at our fixture files.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

_TEST_HOME = Path(tempfile.mkdtemp(prefix="ba-frr-migration-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import paths  # noqa: E402

paths.engage_test_home(str(_TEST_HOME))

import file_ref_resolver as frr  # noqa: E402


@pytest.fixture
def home(tmp_path_factory) -> Path:
    """A pytest-managed tempdir engaged as BETTER_AGENT_HOME for one test.

    `ba_home()` is read fresh from the env on every call, so engaging here
    makes `session_store._session_json_files()` (used by `migrate_all`)
    enumerate OUR fixture files. A module-level engage is unreliable:
    pytest imports every collected module before running any test, and a
    later-imported module (or the suite's own home fixture) re-engages the
    global home, so the only stable contract is to engage per test.

    Backed by tmp_path_factory so pytest cleans the dir at session end
    (a bare mkdtemp would leak)."""
    h = tmp_path_factory.mktemp("frr-home")
    paths.engage_test_home(str(h))
    return h


@pytest.fixture(autouse=True)
def _reset_resolver_caches():
    """Ensure the resolver's process-global caches don't bleed fixture
    state across tests."""
    saved_cache = frr._cache
    saved_cwd_cache = frr._cwd_path_cache
    frr._cache = frr._ExistsCache()
    frr._cwd_path_cache = frr._CwdPathCache()
    try:
        yield
    finally:
        frr._cache = saved_cache
        frr._cwd_path_cache = saved_cwd_cache



# ─── _isolate_content_blocks ─────────────────────────────────────────────


def test_isolate_content_blocks_copies_dicts_so_mutations_are_owned():
    original = [{"type": "text", "text": "hi"}]
    isolated = frr._isolate_content_blocks(original)
    isolated[0]["text"] = "mutated"
    assert original[0]["text"] == "hi"


def test_isolate_content_blocks_recurses_tool_result_content_list():
    blocks = [{
        "type": "tool_result",
        "content": [{"type": "text", "text": "inner"}],
    }]
    isolated = frr._isolate_content_blocks(blocks)
    # The nested block is a fresh dict owned by the isolation.
    assert isolated[0]["content"][0] is not blocks[0]["content"][0]
    isolated[0]["content"][0]["text"] = "x"
    assert blocks[0]["content"][0]["text"] == "inner"


def test_isolate_content_blocks_passes_through_non_dict_blocks():
    out = frr._isolate_content_blocks(["raw", 7, None])
    assert out == ["raw", 7, None]


def test_isolate_content_blocks_tool_result_non_list_content_left_as_is():
    blocks = [{"type": "tool_result", "content": "flat string"}]
    out = frr._isolate_content_blocks(blocks)
    assert out[0]["content"] == "flat string"


# ─── _isolate_for_rewrite ────────────────────────────────────────────────


def test_isolate_for_rewrite_manager_event_recurses_inner_data():
    data = {"event": {"type": "agent_message",
                      "data": {"message": {"content": []}}}}
    out = frr._isolate_for_rewrite("manager_event", data)
    assert out is not data
    assert out["event"] is not data["event"]
    assert out["event"]["data"] is not data["event"]["data"]


def test_isolate_for_rewrite_manager_event_inner_not_dict_is_shallow():
    data = {"event": "not-a-dict"}
    out = frr._isolate_for_rewrite("manager_event", data)
    assert out == data and out is not data


def test_isolate_for_rewrite_manager_event_no_inner_key():
    src = {"other": 1}
    out = frr._isolate_for_rewrite("manager_event", src)
    assert out == {"other": 1}
    assert out is not src  # shallow top copy still made


def test_isolate_for_rewrite_manager_event_inner_data_not_dict():
    # inner is a dict but its `data` is not — recursion skipped, inner copied.
    src = {"event": {"type": "x", "data": "flat"}}
    out = frr._isolate_for_rewrite("manager_event", src)
    assert out["event"] is not src["event"]
    assert out["event"]["data"] == "flat"


def test_isolate_for_rewrite_agent_message_isolates_content_blocks():
    data = {"message": {"content": [{"type": "text", "text": "t"}]}}
    out = frr._isolate_for_rewrite("agent_message", data)
    assert out["message"] is not data["message"]
    assert out["message"]["content"][0] is not data["message"]["content"][0]


def test_isolate_for_rewrite_agent_message_message_not_dict():
    src = {"message": None}
    out = frr._isolate_for_rewrite("agent_message", src)
    assert out is not src  # distinct top container
    assert out["message"] is None


def test_isolate_for_rewrite_agent_message_content_not_list():
    # message is a dict but content is a string — block isolation skipped.
    src = {"message": {"content": "flat"}}
    out = frr._isolate_for_rewrite("agent_message", src)
    assert out["message"] is not src["message"]
    assert out["message"]["content"] == "flat"


def test_isolate_for_rewrite_other_event_is_shallow_top_copy():
    data = {"text": "x", "nested": {"k": 1}}
    out = frr._isolate_for_rewrite("legacy_output", data)
    assert out == data and out is not data
    # Shared by reference for non-mutated fields.
    assert out["nested"] is data["nested"]


# ─── rewrite_event_data_isolated ─────────────────────────────────────────


def test_rewrite_event_data_isolated_non_dict_passthrough():
    assert frr.rewrite_event_data_isolated("agent_message", "nope", None) == "nope"


def test_rewrite_event_data_isolated_does_not_mutate_caller(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "foo.py").write_text("x = 1\n")
    data = {"message": {"content": [
        {"type": "text", "text": "see src/foo.py"}]}}
    out = frr.rewrite_event_data_isolated(
        "agent_message", data, str(tmp_path))
    # Caller's payload is untouched.
    assert data["message"]["content"][0]["text"] == "see src/foo.py"
    # Returned copy carries the link.
    assert "bcfile:" in out["message"]["content"][0]["text"]


# ─── _migrate_message_node ───────────────────────────────────────────────


def test_migrate_message_node_rewrites_string_content(tmp_path):
    (tmp_path / "a.py").write_text("")
    msg = {"content": "look at a.py"}
    assert frr._migrate_message_node(msg, str(tmp_path)) is True
    assert "bcfile:" in msg["content"]


def test_migrate_message_node_unchanged_returns_false():
    msg = {"content": "no refs here"}
    assert frr._migrate_message_node(msg, None) is False
    assert msg["content"] == "no refs here"


def test_migrate_message_node_non_string_content_skipped():
    msg = {"content": ["not", "a", "string"]}
    assert frr._migrate_message_node(msg, None) is False


def test_migrate_message_node_rewrites_embedded_events(tmp_path):
    (tmp_path / "b.py").write_text("")
    msg = {"content": "x", "events": [
        {"type": "text", "data": {"text": "see b.py"}},
        "not-a-dict",
    ]}
    assert frr._migrate_message_node(msg, str(tmp_path)) is True
    assert "bcfile:" in msg["events"][0]["data"]["text"]


def test_migrate_message_node_event_non_dict_data_skipped():
    msg = {"content": "x", "events": [{"type": "text", "data": "nope"}]}
    assert frr._migrate_message_node(msg, None) is False


def test_migrate_message_node_event_dict_data_unchanged_loops():
    # A dict-data event whose text has no file ref → rewrite is a no-op; the
    # loop continues without flagging a change.
    msg = {"content": "x", "events": [
        {"type": "text", "data": {"text": "nothing to rewrite"}},
    ]}
    assert frr._migrate_message_node(msg, None) is False


# ─── _migrate_session_node ───────────────────────────────────────────────


def test_migrate_session_node_walks_messages_with_node_cwd(tmp_path):
    (tmp_path / "c.py").write_text("")
    node = {"cwd": str(tmp_path), "messages": [{"content": "see c.py"}]}
    assert frr._migrate_session_node(node) is True
    assert "bcfile:" in node["messages"][0]["content"]


def test_migrate_session_node_recurses_into_forks(tmp_path):
    (tmp_path / "d.py").write_text("")
    node = {
        "messages": [{"content": "nope"}],
        "forks": [{"cwd": str(tmp_path),
                   "messages": [{"content": "see d.py"}]}],
    }
    assert frr._migrate_session_node(node) is True
    assert "bcfile:" in node["forks"][0]["messages"][0]["content"]


def test_migrate_session_node_non_list_messages_skipped():
    node = {"messages": "nope"}
    assert frr._migrate_session_node(node) is False


def test_migrate_session_node_non_dict_message_skipped():
    node = {"messages": ["nope"]}
    assert frr._migrate_session_node(node) is False


def test_migrate_session_node_non_dict_fork_skipped():
    node = {"forks": ["nope"]}
    assert frr._migrate_session_node(node) is False


# ─── _atomic_write_tmp ───────────────────────────────────────────────────


def test_atomic_write_tmp_replaces_file_and_leaves_no_debris(tmp_path):
    target = tmp_path / "out.json"
    target.write_text("old")
    frr._atomic_write_tmp(target, "new")
    assert target.read_text() == "new"
    assert not (tmp_path / "out.json.bcfile.tmp").exists()


def test_atomic_write_tmp_cleans_tmp_when_replace_fails(tmp_path, monkeypatch):
    target = tmp_path / "out.json"
    target.write_text("old")

    def boom(src, dst):
        raise OSError("simulated")

    monkeypatch.setattr(frr.os, "replace", boom)
    with pytest.raises(OSError):
        frr._atomic_write_tmp(target, "new")
    # Tmp debris removed in finally; original untouched.
    assert not (tmp_path / "out.json.bcfile.tmp").exists()
    assert target.read_text() == "old"


def test_atomic_write_tmp_swallows_unlink_failure_in_finally(
        tmp_path, monkeypatch):
    """Double-failure: os.replace raises AND the finally's tmp.unlink also
    raises — the unlink OSError is swallowed (best-effort cleanup) while the
    original replace error still propagates."""
    target = tmp_path / "out.json"
    target.write_text("old")

    monkeypatch.setattr(frr.os, "replace",
                        lambda s, d: (_ for _ in ()).throw(OSError("replace")))
    monkeypatch.setattr(frr.Path, "unlink",
                        lambda self, *a, **k: (_ for _ in ()).throw(OSError("unlink")))
    with pytest.raises(OSError, match="replace"):
        frr._atomic_write_tmp(target, "new")


# ─── _migrate_session_file ───────────────────────────────────────────────


def test_migrate_session_file_invalid_json_returns_false(tmp_path):
    p = tmp_path / "s.json"
    p.write_text("{not json")
    assert frr._migrate_session_file(p) is False


def test_migrate_session_file_non_dict_json_returns_false(tmp_path):
    p = tmp_path / "s.json"
    p.write_text("[1, 2, 3]")
    assert frr._migrate_session_file(p) is False


def test_migrate_session_file_no_change_returns_false(tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"cwd": str(tmp_path),
                             "messages": [{"content": "no refs"}]}))
    assert frr._migrate_session_file(p) is False


def test_migrate_session_file_rewrites_and_persists(tmp_path):
    (tmp_path / "e.py").write_text("")
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"cwd": str(tmp_path),
                             "messages": [{"content": "see e.py"}]}))
    assert frr._migrate_session_file(p) is True
    node = json.loads(p.read_text())
    assert "bcfile:" in node["messages"][0]["content"]


# ─── _migrate_events_jsonl ───────────────────────────────────────────────


def test_migrate_events_jsonl_missing_file_returns_false(tmp_path):
    assert frr._migrate_events_jsonl(tmp_path / "nope.jsonl", None) is False


def test_migrate_events_jsonl_blank_and_unparseable_lines_preserved(tmp_path):
    p = tmp_path / "events.jsonl"
    p.write_text("\n{bad json\n")
    assert frr._migrate_events_jsonl(p, None) is False
    assert p.read_text() == "\n{bad json\n"


def test_migrate_events_jsonl_no_change_returns_false(tmp_path):
    p = tmp_path / "events.jsonl"
    p.write_text(json.dumps({"type": "text", "data": {"text": "nothing"}}) + "\n")
    assert frr._migrate_events_jsonl(p, None) is False


def test_migrate_events_jsonl_rewrites_changed_lines(tmp_path):
    (tmp_path / "f.py").write_text("")
    p = tmp_path / "events.jsonl"
    entry = {"type": "text", "data": {"text": "see f.py"}}
    p.write_text("\n" + json.dumps(entry) + "\n")
    assert frr._migrate_events_jsonl(p, str(tmp_path)) is True
    lines = [l for l in p.read_text().splitlines() if l.strip()]
    parsed = json.loads(lines[-1])
    assert "bcfile:" in parsed["data"]["text"]


def test_migrate_events_jsonl_non_dict_data_line_preserved(tmp_path):
    p = tmp_path / "events.jsonl"
    p.write_text(json.dumps({"type": "text", "data": "nope"}) + "\n")
    assert frr._migrate_events_jsonl(p, None) is False


# ─── migrate_all ─────────────────────────────────────────────────────────
#
# migrate_all enumerates session files via session_store._session_json_files,
# which resolves to ba_home()/"sessions". Each test takes the `home` fixture
# (a freshly engaged BETTER_AGENT_HOME) so ba_home() points at our fixtures,
# and passes that home as ba_home_dir (run_migration_once reads the sentinel).


def _write_session(home: Path, root_id: str, node: dict) -> Path:
    sessions = home / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    p = sessions / f"{root_id}.json"
    p.write_text(json.dumps(node))
    return p


def test_migrate_all_no_sessions_returns_zero(home):
    assert frr.migrate_all(home) == {
        "sessions_changed": 0, "events_files_changed": 0}


def test_migrate_all_rewrites_session_and_events(home, tmp_path):
    (tmp_path / "g.py").write_text("")
    _write_session(home, "root1", {
        "id": "root1", "cwd": str(tmp_path),
        "messages": [{"content": "see g.py"}],
    })
    # Per-root events.jsonl beside the session root file.
    sub = home / "sessions" / "root1"
    sub.mkdir(parents=True, exist_ok=True)
    (sub / "events.jsonl").write_text(
        json.dumps({"type": "text", "data": {"text": "see g.py"}}) + "\n")
    # A SECOND root with its own events.jsonl so the events loop iterates
    # more than once (exercises the loop back-edge) and a root whose
    # events.jsonl is unchanged.
    _write_session(home, "root1b", {
        "id": "root1b", "cwd": str(tmp_path),
        "messages": [{"content": "see g.py"}],
    })
    sub_b = home / "sessions" / "root1b"
    sub_b.mkdir(parents=True, exist_ok=True)
    (sub_b / "events.jsonl").write_text(
        json.dumps({"type": "text", "data": {"text": "no refs"}}) + "\n")

    stats = frr.migrate_all(home)
    assert stats == {"sessions_changed": 2, "events_files_changed": 1}
    node = json.loads((home / "sessions" / "root1.json").read_text())
    assert "bcfile:" in node["messages"][0]["content"]
    ev_line = (sub / "events.jsonl").read_text().strip()
    assert "bcfile:" in json.loads(ev_line)["data"]["text"]


def test_migrate_all_walks_embedded_forks_and_collects_fork_cwd(home, tmp_path):
    (tmp_path / "h.py").write_text("")
    # An embedded fork: the cwd-index loop must collect the fork's id+cwd
    # (`_collect_forks` recursion), and the session phase must recurse into
    # the fork's messages and rewrite them against the fork's own cwd.
    _write_session(home, "root2", {
        "id": "root2", "cwd": "/nonexistent",
        "forks": [{
            "id": "forkA", "cwd": str(tmp_path),
            "messages": [{"content": "see h.py"}],
            "forks": ["not-a-dict",  # non-dict fork entry skipped cleanly
                      # Fork dict with NO id → cwd setdefault skipped, but the
                      # recursion into it still runs.
                      {"cwd": str(tmp_path), "messages": []}],
        }],
    })

    stats = frr.migrate_all(home)
    assert stats["sessions_changed"] == 1
    node = json.loads((home / "sessions" / "root2.json").read_text())
    assert "bcfile:" in node["forks"][0]["messages"][0]["content"]


def test_migrate_all_corrupt_session_file_in_cwd_index_swallowed(home):
    # Corrupt JSON raises in the cwd-index json.loads loop → `continue`.
    sessions = home / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / "bad.json").write_text("{not json")
    assert frr.migrate_all(home) == {
        "sessions_changed": 0, "events_files_changed": 0}


def test_migrate_all_migration_exception_in_session_swallowed(
        home, monkeypatch, tmp_path):
    def boom(_path: Path) -> bool:
        raise RuntimeError("boom")

    _write_session(home, "root3", {"id": "root3", "cwd": str(tmp_path)})
    monkeypatch.setattr(frr, "_migrate_session_file", boom)
    # Session migration raises → swallowed; events phase still runs clean.
    stats = frr.migrate_all(home)
    assert stats["sessions_changed"] == 0


def test_migrate_all_events_exception_swallowed(home, monkeypatch, tmp_path):
    _write_session(home, "root4", {"id": "root4", "cwd": str(tmp_path)})
    sub = home / "sessions" / "root4"
    sub.mkdir(parents=True)
    (sub / "events.jsonl").write_text(
        json.dumps({"type": "text", "data": {"text": "x"}}) + "\n")

    def boom(_path: Path, _cwd: Any) -> bool:
        raise RuntimeError("boom")

    monkeypatch.setattr(frr, "_migrate_events_jsonl", boom)
    stats = frr.migrate_all(home)
    assert stats["events_files_changed"] == 0


def test_migrate_all_non_dict_node_in_cwd_index_skipped(home):
    sessions = home / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / "list.json").write_text("[1, 2, 3]")
    assert frr.migrate_all(home) == {
        "sessions_changed": 0, "events_files_changed": 0}


# ─── run_migration_once ──────────────────────────────────────────────────


def test_run_migration_once_skips_when_sentinel_present(home):
    sessions = home / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / frr._MIGRATION_SENTINEL).write_text("done")
    assert frr.run_migration_once(home) is None


def test_run_migration_once_creates_sessions_dir_and_sentinel(home):
    # No sessions dir yet; run must create it.
    stats = frr.run_migration_once(home)
    assert stats == {"sessions_changed": 0, "events_files_changed": 0}
    assert (home / "sessions" / frr._MIGRATION_SENTINEL).exists()
    # Second run is a no-op.
    assert frr.run_migration_once(home) is None


def test_run_migration_once_sentinel_write_oserror_swallowed(home, monkeypatch):
    def boom(_self, _path, *_a, **_kw):
        raise OSError("simulated")

    # sessions_dir.mkdir succeeds first; the sentinel write_text is what fails.
    (home / "sessions").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(frr.Path, "write_text", boom)
    stats = frr.run_migration_once(home)
    # Stats still returned even though the sentinel could not be written.
    assert stats == {"sessions_changed": 0, "events_files_changed": 0}


# ─── idempotency (the migration's stated contract) ───────────────────────


def test_migration_is_idempotent_across_runs(home, tmp_path):
    (tmp_path / "z.py").write_text("")
    _write_session(home, "rootZ", {
        "id": "rootZ", "cwd": str(tmp_path),
        "messages": [{"content": "see z.py"}],
    })
    first = frr.migrate_all(home)
    second = frr.migrate_all(home)
    assert first["sessions_changed"] == 1
    # Re-running over already-rewritten content changes nothing.
    assert second["sessions_changed"] == 0
