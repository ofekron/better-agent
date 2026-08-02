"""Hermetic unit tests for ``runs_dir`` helper clusters.

Covers three cohesive, security-relevant clusters that had no dedicated unit
coverage:

  1. Liveness / atomic-write helpers — ``turn_dir``, ``runner_alive_path``,
     ``read_best_complete`` (preference order + parse-failure fallback),
     ``salvage_complete_payload``, ``atomic_write_json`` (write + ledger
     append + recent-index invalidation), ``cli_liveness_corroborated``
     (pid + jsonl inode/size/mtime corroboration, fail-closed),
     ``pid_alive``.
  2. Path-validation guards — the ``_run_state_path_*`` / ``_root_signature``
     / ``_pending_run_dirs_have_state`` / ``_recent_candidates_unchanged``
     / ``_invalidate_recent_state_index`` / ``_ledger_state_paths_for_sid``
     family that defends the run-state index against traversal, symlinks,
     and non-regular files.
  3. Reconciled-marker index — ``_reconciled_marker_index_row`` (traversal +
     symlink defense + inode/size/mtime fingerprint),
     ``reconciled_marker_index_row_matches``, ``_reconciled_marker_index_key``,
     the run-state ledger key extractors, and the
     append / ensure-backfilled / load public surface.

All filesystem state lives under an isolated ``BETTER_AGENT_HOME`` tempdir
via ``_test_home.isolate``; nothing touches a real home. Module-level caches
are reset between tests so order never matters.

Run with:
    cd backend && .venv/bin/python -m pytest scripts/test_runs_dir_helpers_unit.py -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-runs-dir-helpers-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest  # noqa: E402

import runs_dir  # noqa: E402
from runs_dir import (  # noqa: E402
    CLI_LIVENESS_FRESH_S,
    _ledger_state_paths_for_sid,
    _pending_run_dirs_have_state,
    _recent_candidates_unchanged,
    _reconciled_marker_index_key,
    _reconciled_marker_index_row,
    _root_signature,
    _run_state_candidate_stat,
    _run_state_ledger_has_key,
    _run_state_ledger_keys,
    _run_state_path_has_ledger_shape,
    _run_state_path_string_has_ledger_shape,
    _run_state_path_under_root,
    append_reconciled_marker_index,
    atomic_write_json,
    cli_liveness_corroborated,
    ensure_reconciled_marker_index_backfilled,
    load_reconciled_marker_index,
    pid_alive,
    read_best_complete,
    reconciled_marker_index_row_matches,
    runs_root,
    salvage_complete_payload,
    turn_dir,
)


@pytest.fixture(autouse=True)
def _reset_module_caches():
    """runs_dir keeps process-global caches; clear them per test for order
    independence and to keep ledger-append dedup state local."""
    runs_dir._RUN_STATE_LEDGER_SEEN.clear()
    runs_dir._RUN_STATE_RECENT_INDEX_CACHE.clear()
    runs_dir._RUN_STATE_LEDGER_CACHE.clear()
    runs_dir._RUN_STATE_RECENT_INDEX_INFLIGHT.clear()
    runs_dir._RUN_STATE_LEDGER_CACHE_REBUILD_INFLIGHT.clear()
    yield


@pytest.fixture
def fresh_root():
    """A fresh per-test root directory (layout-controlled)."""
    path = Path(tempfile.mkdtemp(prefix="bc-rd-root-"))
    yield path
    import shutil

    shutil.rmtree(path, ignore_errors=True)


def _write(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        path.write_text(data, encoding="utf-8")
    else:
        path.write_bytes(data)
    return path


def _touch_now(path: Path) -> Path:
    # Refresh mtime to now WITHOUT clobbering existing content.
    os.utime(path, None)
    return path


def _set_mtime_old(path: Path, age_s: float = 10_000.0) -> None:
    when = time.time() - age_s
    os.utime(path, (when, when))


# --------------------------------------------------------------------------- #
# Cluster A — liveness / atomic-write helpers
# --------------------------------------------------------------------------- #


def test_turn_dir_and_runner_alive_path():
    run_dir = Path("/tmp/whatever/run-x")
    assert turn_dir(run_dir, "turn-7") == run_dir / "turns" / "turn-7"
    assert runs_dir.runner_alive_path(run_dir) == run_dir / "runner_alive"


def test_read_best_complete_prefers_run_level(fresh_root):
    run_dir = fresh_root / "run-1"
    _write(run_dir / "complete.json", json.dumps({"success": True, "src": "run"}))
    _write(run_dir / "turns" / "t1" / "complete.json", json.dumps({"src": "turn"}))
    assert read_best_complete(run_dir) == {"success": True, "src": "run"}


def test_read_best_complete_falls_back_to_most_recent_turn(fresh_root):
    run_dir = fresh_root / "run-1"
    older = _write(run_dir / "turns" / "old" / "complete.json", json.dumps({"i": 1}))
    newer = _write(run_dir / "turns" / "new" / "complete.json", json.dumps({"i": 2}))
    _set_mtime_old(older, age_s=100)
    _touch_now(newer)
    assert read_best_complete(run_dir) == {"i": 2}


def test_read_best_complete_run_level_corrupt_falls_back_to_turns(fresh_root):
    run_dir = fresh_root / "run-1"
    _write(run_dir / "complete.json", "{ not json")
    _write(run_dir / "turns" / "t1" / "complete.json", json.dumps({"ok": True}))
    assert read_best_complete(run_dir) == {"ok": True}


def test_read_best_complete_skips_corrupt_turn_files(fresh_root):
    run_dir = fresh_root / "run-1"
    bad = _write(run_dir / "turns" / "t1" / "complete.json", "{ broken")
    good = _write(run_dir / "turns" / "t2" / "complete.json", json.dumps({"ok": True}))
    _set_mtime_old(bad, age_s=5)
    _touch_now(good)
    assert read_best_complete(run_dir) == {"ok": True}


def test_read_best_complete_returns_none_when_nothing_exists(fresh_root):
    run_dir = fresh_root / "run-1"
    assert read_best_complete(run_dir) is None
    # An empty turns dir also yields None.
    (run_dir / "turns").mkdir(parents=True)
    assert read_best_complete(run_dir) is None


def test_salvage_complete_payload_projects_shape_with_defaults(fresh_root):
    run_id = "run-salvage"
    run_dir = runs_root() / run_id
    _write(run_dir / "complete.json", json.dumps({"session_id": "s1", "token_usage": {"x": 1}}))
    payload = salvage_complete_payload(run_id)
    assert payload == {
        "success": False,
        "cancelled": False,
        "error": None,
        "session_id": "s1",
        "token_usage": {"x": 1},
    }


def test_salvage_complete_payload_none_when_absent():
    assert salvage_complete_payload("definitely-missing-run-id") is None


def test_atomic_write_json_appends_ledger_and_invalidates_recent_index(fresh_root):
    root = fresh_root
    run_dir = root / "run-a"
    state_path = run_dir / "state.json"
    atomic_write_json(
        state_path,
        {"session_id": "s1", "jsonl_path": str(run_dir / "events.jsonl")},
    )
    assert json.loads(state_path.read_text(encoding="utf-8"))["session_id"] == "s1"
    ledger = runs_dir.run_state_ledger_path(root)
    rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(rows) == 1
    assert rows[0]["session_id"] == "s1"
    assert rows[0]["state_path"] == str(state_path)
    # Re-writing the same (path, session_id, jsonl_path) is deduped -> no new row.
    atomic_write_json(
        state_path,
        {"session_id": "s1", "jsonl_path": str(run_dir / "events.jsonl")},
    )
    rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(rows) == 1
    # The recent-state index cache for this root was invalidated.
    assert str(root) not in runs_dir._RUN_STATE_RECENT_INDEX_CACHE


def test_atomic_write_json_skips_ledger_for_non_state_json(fresh_root):
    root = fresh_root
    run_dir = root / "run-b"
    atomic_write_json(run_dir / "complete.json", {"success": True})
    assert json.loads((run_dir / "complete.json").read_text(encoding="utf-8")) == {"success": True}
    assert not runs_dir.run_state_ledger_path(root).exists()


def test_atomic_write_json_skips_ledger_when_session_or_jsonl_missing(fresh_root):
    root = fresh_root
    run_dir = root / "run-c"
    atomic_write_json(run_dir / "state.json", {"session_id": "s1"})  # no jsonl_path
    assert (run_dir / "state.json").exists()
    assert not runs_dir.run_state_ledger_path(root).exists()


def test_cli_liveness_dead_pid_returns_false():
    assert cli_liveness_corroborated(0, "/tmp/whatever.jsonl", 1, 0) is False
    assert cli_liveness_corroborated(None, "/tmp/whatever.jsonl", 1, 0) is False
    assert cli_liveness_corroborated(-5, "/tmp/whatever.jsonl", 1, 0) is False


def test_cli_liveness_live_pid_but_no_jsonl_returns_false(fresh_root):
    assert cli_liveness_corroborated(os.getpid(), None, 1, 0) is False
    assert cli_liveness_corroborated(os.getpid(), "", 1, 0) is False


def test_cli_liveness_missing_jsonl_returns_false(fresh_root):
    missing = str(fresh_root / "absent.jsonl")
    assert cli_liveness_corroborated(os.getpid(), missing, None, None) is False


def test_cli_liveness_inode_mismatch_returns_false(fresh_root):
    jsonl = _write(fresh_root / "live.jsonl", "data")
    st = jsonl.stat()
    assert cli_liveness_corroborated(os.getpid(), str(jsonl), st.st_ino + 1, None) is False


def test_cli_liveness_growth_past_processed_byte_is_true(fresh_root):
    jsonl = _write(fresh_root / "live.jsonl", "x" * 100)
    st = jsonl.stat()
    # size(100) > processed_byte(10) -> True even if mtime were stale.
    _set_mtime_old(jsonl, age_s=10_000)
    assert cli_liveness_corroborated(os.getpid(), str(jsonl), st.st_ino, 10) is True


def test_cli_liveness_fresh_mtime_with_line_cursor_is_true(fresh_root):
    jsonl = _write(fresh_root / "live.jsonl", "x" * 100)
    st = jsonl.stat()
    _touch_now(jsonl)
    # processed_byte None (line-cursor providers), mtime within window.
    assert cli_liveness_corroborated(os.getpid(), str(jsonl), st.st_ino, None) is True


def test_cli_liveness_stale_and_not_grown_returns_false(fresh_root):
    jsonl = _write(fresh_root / "live.jsonl", "x" * 100)
    st = jsonl.stat()
    _set_mtime_old(jsonl, age_s=CLI_LIVENESS_FRESH_S + 60)
    # size(100) not > processed_byte(100); mtime stale.
    assert cli_liveness_corroborated(os.getpid(), str(jsonl), st.st_ino, 100) is False


def test_pid_alive_self_true_invalid_false():
    assert pid_alive(os.getpid()) is True
    assert pid_alive(0) is False
    assert pid_alive(-1) is False
    assert pid_alive(None) is False


# --------------------------------------------------------------------------- #
# Cluster B — path-validation guards
# --------------------------------------------------------------------------- #


def test_run_state_path_under_root_basic(fresh_root):
    root = fresh_root.resolve()
    good = root / "run-1" / "state.json"
    assert _run_state_path_under_root(good, root) is True


def test_run_state_path_under_root_wrong_name(fresh_root):
    root = fresh_root.resolve()
    assert _run_state_path_under_root(root / "run-1" / "other.json", root) is False


def test_run_state_path_under_root_outside_root(fresh_root):
    root = fresh_root.resolve()
    outside = fresh_root.parent / "elsewhere" / "state.json"
    assert _run_state_path_under_root(outside, root) is False


def test_run_state_path_has_ledger_shape_valid(fresh_root):
    assert _run_state_path_has_ledger_shape(fresh_root / "run-1" / "state.json", fresh_root) is True


def test_run_state_path_has_ledger_shape_wrong_name(fresh_root):
    assert _run_state_path_has_ledger_shape(fresh_root / "run-1" / "state.txt", fresh_root) is False


def test_run_state_path_has_ledger_shape_relative_rejected():
    assert _run_state_path_has_ledger_shape(Path("run-1/state.json"), Path("/tmp/root")) is False


def test_run_state_path_has_ledger_shape_too_shallow(fresh_root):
    # state.json directly under root -> parent == root -> rejected.
    assert _run_state_path_has_ledger_shape(fresh_root / "state.json", fresh_root) is False


def test_run_state_path_has_ledger_shape_too_deep(fresh_root):
    assert _run_state_path_has_ledger_shape(fresh_root / "run-1" / "sub" / "state.json", fresh_root) is False


def test_run_state_path_string_has_ledger_shape_valid(fresh_root):
    s = str(fresh_root / "run-1" / "state.json")
    assert _run_state_path_string_has_ledger_shape(s, fresh_root) is True


def test_run_state_path_string_has_ledger_shape_wrong_suffix(fresh_root):
    s = str(fresh_root / "run-1" / "state.txt")
    assert _run_state_path_string_has_ledger_shape(s, fresh_root) is False


def test_run_state_path_string_has_ledger_shape_wrong_prefix(fresh_root):
    s = str(fresh_root.parent / "other" / "run-1" / "state.json")
    assert _run_state_path_string_has_ledger_shape(s, fresh_root) is False


def test_run_state_path_string_has_ledger_shape_nested_run_name(fresh_root):
    s = str(fresh_root / "run-1" / "sub" / "state.json")
    assert _run_state_path_string_has_ledger_shape(s, fresh_root) is False


def test_run_state_candidate_stat_regular_file(fresh_root):
    state = _write(fresh_root / "run-1" / "state.json", "{}")
    st = _run_state_candidate_stat(state, fresh_root)
    assert st is not None
    assert st.st_size == 2


def test_run_state_candidate_stat_bad_shape(fresh_root):
    assert _run_state_candidate_stat(fresh_root / "state.json", fresh_root) is None


def test_run_state_candidate_stat_missing(fresh_root):
    assert _run_state_candidate_stat(fresh_root / "run-1" / "state.json", fresh_root) is None


def test_run_state_candidate_stat_non_regular(fresh_root):
    # A directory named state.json is not a regular file.
    (fresh_root / "run-1" / "state.json").mkdir(parents=True)
    assert _run_state_candidate_stat(fresh_root / "run-1" / "state.json", fresh_root) is None


def test_root_signature_existing_and_missing(fresh_root):
    assert _root_signature(fresh_root) is not None
    assert _root_signature(fresh_root / "does-not-exist") is None


def test_pending_run_dirs_have_state_true(fresh_root):
    _write(fresh_root / "pending-a" / "state.json", "{}")
    assert _pending_run_dirs_have_state(fresh_root, ("pending-a", "pending-b")) is True


def test_pending_run_dirs_have_state_false(fresh_root):
    assert _pending_run_dirs_have_state(fresh_root, ("pending-a", "pending-b")) is False


def test_recent_candidates_unchanged_match(fresh_root):
    state = _write(fresh_root / "run-1" / "state.json", "abc")
    st = _run_state_candidate_stat(state, fresh_root)
    candidates = ((st.st_mtime_ns, st.st_size, str(state)),)
    assert _recent_candidates_unchanged(candidates, fresh_root) is True


def test_recent_candidates_unchanged_detects_change(fresh_root):
    state = _write(fresh_root / "run-1" / "state.json", "abc")
    st = _run_state_candidate_stat(state, fresh_root)
    _write(state, "different-length-content")
    candidates = ((st.st_mtime_ns, st.st_size, str(state)),)
    assert _recent_candidates_unchanged(candidates, fresh_root) is False


def test_recent_candidates_unchanged_missing(fresh_root):
    candidates = ((1, 2, str(fresh_root / "run-1" / "state.json")),)
    assert _recent_candidates_unchanged(candidates, fresh_root) is False


def test_invalidate_recent_state_index(fresh_root):
    runs_dir._RUN_STATE_RECENT_INDEX_CACHE[str(fresh_root)] = "stale"
    runs_dir._invalidate_recent_state_index(fresh_root)
    assert str(fresh_root) not in runs_dir._RUN_STATE_RECENT_INDEX_CACHE


def test_ledger_state_paths_for_sid_filters_by_root(fresh_root):
    root_resolved = fresh_root.resolve()
    inside = str(root_resolved / "run-1" / "state.json")
    outside = str(fresh_root.parent / "elsewhere" / "state.json")
    index = {"sid-a": [(1.0, inside), (2.0, outside)]}
    result = _ledger_state_paths_for_sid(index, "sid-a", root_resolved)
    assert result == [Path(inside)]
    assert _ledger_state_paths_for_sid(index, "sid-b", root_resolved) == []


# --------------------------------------------------------------------------- #
# Cluster C — reconciled-marker index
# --------------------------------------------------------------------------- #


def _make_marker(root: Path, run_id: str, *, provider_kind="claude", ingestion_version=2, body=None):
    marker = root / run_id / "reconciled.marker"
    payload = body if body is not None else {
        "provider_kind": provider_kind,
        "ingestion_version": ingestion_version,
    }
    _write(marker, json.dumps(payload))
    return marker


def test_reconciled_marker_index_row_valid(fresh_root):
    root = fresh_root.resolve()
    marker = _make_marker(root, "run-1")
    row = _reconciled_marker_index_row(marker, "claude", 2, root=root)
    assert row is not None
    assert row["run_id"] == "run-1"
    assert row["marker_path"] == str(marker)
    assert row["provider_kind"] == "claude"
    assert row["ingestion_version"] == 2
    assert row["marker_size"] == marker.stat().st_size
    assert row["marker_mtime_ns"] == marker.stat().st_mtime_ns


def test_reconciled_marker_index_row_wrong_name(fresh_root):
    marker = _make_marker(fresh_root.resolve(), "run-1")
    # Pass a path whose name is not reconciled.marker.
    wrong = marker.parent / "state.json"
    _write(wrong, "{}")
    assert _reconciled_marker_index_row(wrong, "claude", 2, root=fresh_root.resolve()) is None


def test_reconciled_marker_index_row_no_provider_kind(fresh_root):
    marker = _make_marker(fresh_root.resolve(), "run-1")
    assert _reconciled_marker_index_row(marker, None, 2, root=fresh_root.resolve()) is None


def test_reconciled_marker_index_row_run_dir_not_direct_child(fresh_root):
    root = fresh_root.resolve()
    # Marker nested two levels deep -> run_dir.parent.parent != root.
    marker = _write(root / "run-1" / "nested" / "reconciled.marker", "{}")
    assert _reconciled_marker_index_row(marker, "claude", 2, root=root) is None


def test_reconciled_marker_index_row_symlinked_marker_rejected(fresh_root):
    root = fresh_root.resolve()
    real = _write(root / "real.marker", "{}")
    run_dir = root / "run-1"
    run_dir.mkdir()
    link = run_dir / "reconciled.marker"
    link.symlink_to(real)
    assert _reconciled_marker_index_row(link, "claude", 2, root=root) is None


def test_reconciled_marker_index_row_missing_marker(fresh_root):
    root = fresh_root.resolve()
    (root / "run-1").mkdir()
    marker = root / "run-1" / "reconciled.marker"  # never written
    assert _reconciled_marker_index_row(marker, "claude", 2, root=root) is None


def test_reconciled_marker_index_key_builds_tuple():
    row = {
        "marker_path": "/p/m",
        "provider_kind": "codex",
        "ingestion_version": 3,
        "marker_size": 10,
        "marker_mtime_ns": 99,
        "marker_inode": 7,
    }
    assert _reconciled_marker_index_key(row) == ("/p/m", "codex", 3, 10, 99, 7)


def test_reconciled_marker_index_row_matches_true(fresh_root):
    marker = _make_marker(fresh_root, "run-1")
    st = marker.lstat()
    row = {
        "run_id": "run-1",
        "marker_path": str(marker),
        "marker_size": st.st_size,
        "marker_mtime_ns": st.st_mtime_ns,
        "marker_inode": st.st_ino,
    }
    assert reconciled_marker_index_row_matches(fresh_root / "run-1", row) is True


def test_reconciled_marker_index_row_matches_run_id_mismatch(fresh_root):
    marker = _make_marker(fresh_root, "run-1")
    st = marker.lstat()
    row = {"run_id": "other", "marker_path": str(marker)}
    assert reconciled_marker_index_row_matches(fresh_root / "run-1", row) is False


def test_reconciled_marker_index_row_matches_marker_path_mismatch(fresh_root):
    row = {"run_id": "run-1", "marker_path": "/somewhere/else/reconciled.marker"}
    assert reconciled_marker_index_row_matches(fresh_root / "run-1", row) is False


def test_reconciled_marker_index_row_matches_size_changed(fresh_root):
    marker = _make_marker(fresh_root, "run-1")
    st = marker.lstat()
    row = {
        "run_id": "run-1",
        "marker_path": str(marker),
        "marker_size": st.st_size + 999,
        "marker_mtime_ns": st.st_mtime_ns,
        "marker_inode": st.st_ino,
    }
    assert reconciled_marker_index_row_matches(fresh_root / "run-1", row) is False


def test_reconciled_marker_index_row_matches_bad_types_fail_closed(fresh_root):
    marker = _make_marker(fresh_root, "run-1")
    row = {
        "run_id": "run-1",
        "marker_path": str(marker),
        "marker_size": "not-an-int",  # int() raises ValueError -> False
    }
    assert reconciled_marker_index_row_matches(fresh_root / "run-1", row) is False


def test_reconciled_marker_index_row_matches_symlink_rejected(fresh_root):
    real = _write(fresh_root / "real.marker", "{}")
    run_dir = fresh_root / "run-1"
    run_dir.mkdir()
    link = run_dir / "reconciled.marker"
    link.symlink_to(real)
    row = {"run_id": "run-1", "marker_path": str(link)}
    assert reconciled_marker_index_row_matches(run_dir, row) is False


def test_run_state_ledger_keys_and_has_key(fresh_root):
    ledger = fresh_root / "run_state_index.jsonl"
    _write(
        ledger,
        json.dumps({"state_path": "/r/a/state.json", "session_id": "s1", "jsonl_path": "/r/a/e.jsonl"}) + "\n"
        + "not-json-at-all\n"
        + json.dumps({"state_path": "/r/b/state.json", "session_id": "s2", "jsonl_path": "/r/b/e.jsonl"}) + "\n",
    )
    keys = _run_state_ledger_keys(ledger)
    assert ("/r/a/state.json", "s1", "/r/a/e.jsonl") in keys
    assert ("/r/b/state.json", "s2", "/r/b/e.jsonl") in keys
    assert _run_state_ledger_has_key(ledger, ("/r/a/state.json", "s1", "/r/a/e.jsonl")) is True
    assert _run_state_ledger_has_key(ledger, ("/r/missing", "s1", "/r/a/e.jsonl")) is False


def test_run_state_ledger_keys_missing_file(fresh_root):
    assert _run_state_ledger_keys(fresh_root / "absent.jsonl") == set()
    assert _run_state_ledger_has_key(fresh_root / "absent.jsonl", ("a", "b", "c")) is False


def test_append_and_load_reconciled_marker_index(fresh_root):
    root = fresh_root.resolve()
    marker = _make_marker(root, "run-1")
    append_reconciled_marker_index(marker, "claude", 2, root=root)
    loaded = load_reconciled_marker_index(root)
    assert "run-1" in loaded
    assert loaded["run-1"]["provider_kind"] == "claude"


def test_append_reconciled_marker_index_bad_row_is_noop(fresh_root):
    root = fresh_root.resolve()
    # Wrong marker name -> row None -> append is a silent no-op.
    bad = _write(root / "run-1" / "state.json", "{}")
    append_reconciled_marker_index(bad, "claude", 2, root=root)
    assert load_reconciled_marker_index(root) == {}


def test_ensure_reconciled_marker_index_backfilled(fresh_root):
    root = fresh_root.resolve()
    _make_marker(root, "run-1", provider_kind="claude", ingestion_version=2)
    _make_marker(root, "run-2", provider_kind="codex", ingestion_version=3)

    first = ensure_reconciled_marker_index_backfilled(root)
    assert first is True
    loaded = load_reconciled_marker_index(root)
    assert {"run-1", "run-2"} <= set(loaded)
    # Backfill marker written.
    assert runs_dir._reconciled_marker_backfill_current(root) is True
    # Idempotent: second call short-circuits.
    second = ensure_reconciled_marker_index_backfilled(root)
    assert second is False


def test_ensure_reconciled_marker_index_backfilled_skips_invalid(fresh_root):
    root = fresh_root.resolve()
    # Valid marker.
    _make_marker(root, "good", provider_kind="claude", ingestion_version=2)
    # Missing provider_kind -> skipped.
    _make_marker(root, "nopk", body={"ingestion_version": 2})
    # Missing ingestion_version (wrong type) -> skipped.
    _make_marker(root, "noiv", body={"provider_kind": "claude", "ingestion_version": "x"})
    # Corrupt JSON -> skipped.
    _write(root / "broken" / "reconciled.marker", "{ not json")
    # Non-dir entry -> skipped.
    _write(root / "loose-file.json", "{}")

    changed = ensure_reconciled_marker_index_backfilled(root)
    assert changed is True
    loaded = load_reconciled_marker_index(root)
    assert set(loaded) == {"good"}


if __name__ == "__main__":
    rc = 0
    import inspect

    for _name, _obj in sorted(globals().items()):
        if _name.startswith("test_") and inspect.isfunction(_obj):
            try:
                _obj()
                print(f"PASS: {_name}")
            except Exception as _exc:  # pragma: no cover
                print(f"FAIL: {_name}: {_exc}")
                rc = 1
    raise SystemExit(rc)
