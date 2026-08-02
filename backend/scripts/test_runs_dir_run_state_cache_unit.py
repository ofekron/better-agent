"""Hermetic unit tests for the ``runs_dir`` run-state sqlite cache cluster.

Covers the cache load/rebuild, ledger lookup, and app-session index
machinery — the security-relevant failure branches the happy-path tests
leave out: signature mismatch (fail-closed), corrupt sqlite (fail-closed),
non-string / non-ledger-shaped cache rows (rejected), the rebuild
owner/not-owner concurrency handshake, the append-time cache extension
(memory + sqlite upsert + corruption tolerance), and ``run_dirs_by_app_session``
index assembly with symlink / missing-file / bad-row skipping.

All filesystem state lives under an isolated ``BETTER_AGENT_HOME`` tempdir
via ``_test_home.isolate``; nothing touches a real home. Module-level
caches are reset between tests so order never matters.

Run with:
    cd backend && .venv/bin/python -m pytest scripts/test_runs_dir_run_state_cache_unit.py -v
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-runs-dir-rs-cache-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest  # noqa: E402

import runs_dir  # noqa: E402
from runs_dir import (  # noqa: E402
    _claim_run_state_cache_rebuild,
    _extend_run_state_cache_after_append,
    _finish_run_state_cache_rebuild,
    _load_run_state_app_index,
    _load_run_state_cache_for_sid,
    _row_written_at,
    _run_state_cache_signature_current,
    _run_state_ledger_signature,
    _sqlite_connect,
    _write_run_state_cache,
    ledger_state_files_for_sid,
    run_dirs_by_app_session,
    run_state_ledger_backfill_marker_path,
    run_state_ledger_cache_path,
    run_state_ledger_path,
    run_state_app_index_backfill_marker_path,
    state_files_for_sid,
)
from json_store import write_json  # noqa: E402


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
    runs_dir._RUN_STATE_LEDGER_SEEN.clear()
    runs_dir._RUN_STATE_RECENT_INDEX_CACHE.clear()
    runs_dir._RUN_STATE_LEDGER_CACHE.clear()
    runs_dir._RUN_STATE_RECENT_INDEX_INFLIGHT.clear()
    runs_dir._RUN_STATE_LEDGER_CACHE_REBUILD_INFLIGHT.clear()


@pytest.fixture
def fresh_root():
    """A fresh per-test root directory (layout-controlled)."""
    path = Path(tempfile.mkdtemp(prefix="bc-rd-rs-root-"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


def _write(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        path.write_text(data, encoding="utf-8")
    else:
        path.write_bytes(data)
    return path


def _ledger(root: Path) -> Path:
    return run_state_ledger_path(root)


def _touch_ledger(root: Path) -> None:
    """Ensure the ledger jsonl exists so its stat signature is non-None."""
    ledger = _ledger(root)
    if not ledger.exists():
        _write(ledger, "")


def _sig(root: Path):
    return _run_state_ledger_signature(_ledger(root))


def _make_state(root: Path, run_id: str, sid: str, app: str | None = None) -> Path:
    """Create <root>/<run_id>/state.json and return its absolute path."""
    run_dir = root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    state = {"session_id": sid, "jsonl_path": str(run_dir / "events.jsonl")}
    if app is not None:
        state["app_session_id"] = app
    return _write(run_dir / "state.json", json.dumps(state))


def _write_cache(root: Path, index, app_map=None) -> object:
    """Materialize the ledger jsonl from the index, then build a cache whose
    meta matches that ledger's signature (signature is the ledger stat, so the
    ledger must exist and stay unchanged for read-back to validate)."""
    app_map = app_map or {}
    ledger = _ledger(root)
    if not ledger.exists():
        lines = []
        for sid, paths in index.items():
            for written_at, state_path in paths:
                row = {"session_id": sid, "state_path": state_path, "written_at": written_at}
                if state_path in app_map:
                    row["app_session_id"] = app_map[state_path]
                lines.append(json.dumps(row))
        _write(ledger, ("\n".join(lines) + "\n") if lines else "")
    sig = _sig(root)
    _write_run_state_cache(root, sig, index, app_map)
    return sig


# --------------------------------------------------------------------------- #
# _row_written_at / _upsert_run_state_index_row
# --------------------------------------------------------------------------- #


def test_row_written_at_parses_float():
    assert _row_written_at({"written_at": 3.5}) == 3.5
    assert _row_written_at({"written_at": "2"}) == 2.0


@pytest.mark.parametrize("bad", [None, "abc", {}, []])
def test_row_written_at_non_numeric_returns_zero(bad):
    assert _row_written_at({"written_at": bad}) == 0.0
    assert _row_written_at({}) == 0.0


def test_upsert_run_state_index_row_inserts_and_orders():
    index: dict = {}
    runs_dir._upsert_run_state_index_row(index, {"session_id": "s1", "state_path": "/r/a/state.json", "written_at": 2.0})
    runs_dir._upsert_run_state_index_row(index, {"session_id": "s1", "state_path": "/r/b/state.json", "written_at": 1.0})
    assert [p for _, p in index["s1"]] == ["/r/b/state.json", "/r/a/state.json"]


def test_upsert_run_state_index_row_replaces_same_path_keeps_order():
    index: dict = {"s1": [(1.0, "/r/a/state.json")]}
    runs_dir._upsert_run_state_index_row(index, {"session_id": "s1", "state_path": "/r/a/state.json", "written_at": 9.0})
    assert index["s1"] == [(9.0, "/r/a/state.json")]


def test_upsert_run_state_index_row_skips_missing_sid_or_path():
    index: dict = {}
    runs_dir._upsert_run_state_index_row(index, {"state_path": "/r/a/state.json", "written_at": 1.0})
    runs_dir._upsert_run_state_index_row(index, {"session_id": "s1", "written_at": 1.0})
    runs_dir._upsert_run_state_index_row(index, {"session_id": "", "state_path": "/r/a/state.json", "written_at": 1.0})
    assert index == {}


# --------------------------------------------------------------------------- #
# _load_run_state_cache_for_sid
# --------------------------------------------------------------------------- #


def test_load_cache_for_sid_returns_paths(fresh_root):
    sp = str(_make_state(fresh_root, "run-1", "s1"))
    _write(fresh_root / "run-1" / "events.jsonl", "")
    _write_cache(fresh_root, {"s1": [(1.0, sp)]})
    paths = _load_run_state_cache_for_sid(fresh_root, _sig(fresh_root), "s1", fresh_root.resolve())
    assert paths == [Path(sp)]


def test_load_cache_for_sid_filters_other_sid(fresh_root):
    sp = str(_make_state(fresh_root, "run-1", "s1"))
    _write_cache(fresh_root, {"s1": [(1.0, sp)], "other": [(1.0, str(_make_state(fresh_root, "run-2", "other")))]})
    assert _load_run_state_cache_for_sid(fresh_root, _sig(fresh_root), "s1", fresh_root.resolve()) == [Path(sp)]


def test_load_cache_for_sid_signature_mismatch_returns_none(fresh_root):
    sp = str(_make_state(fresh_root, "run-1", "s1"))
    _write_cache(fresh_root, {"s1": [(1.0, sp)]})
    wrong = tuple(v + 1 for v in _sig(fresh_root))
    assert _load_run_state_cache_for_sid(fresh_root, wrong, "s1", fresh_root.resolve()) is None


def test_load_cache_for_sid_corrupt_db_returns_none(fresh_root):
    _write(run_state_ledger_cache_path(fresh_root), b"not sqlite")
    assert _load_run_state_cache_for_sid(fresh_root, _sig(fresh_root), "s1", fresh_root.resolve()) is None


def test_load_cache_for_sid_rejects_non_string_state_path(fresh_root):
    sig = _write_cache(fresh_root, {})
    # Inject a row whose state_path column holds a non-string (defensive reject -> None).
    with _sqlite_connect(run_state_ledger_cache_path(fresh_root)) as conn:
        conn.execute("INSERT INTO entries (sid, state_path, written_at) VALUES (?, ?, ?)", ("s1", 12345, 1.0))
        conn.commit()
    assert _load_run_state_cache_for_sid(fresh_root, sig, "s1", fresh_root.resolve()) is None


def test_load_cache_for_sid_rejects_non_ledger_shaped_path(fresh_root):
    sig = _write_cache(fresh_root, {})
    bad_path = str(fresh_root / "state.json")  # directly under root: wrong shape
    with _sqlite_connect(run_state_ledger_cache_path(fresh_root)) as conn:
        conn.execute("INSERT INTO entries (sid, state_path, written_at) VALUES (?, ?, ?)", ("s1", bad_path, 1.0))
        conn.commit()
    assert _load_run_state_cache_for_sid(fresh_root, sig, "s1", fresh_root.resolve()) is None


# --------------------------------------------------------------------------- #
# _claim / _finish rebuild handshake
# --------------------------------------------------------------------------- #


def test_claim_rebuild_first_owner_then_joiner(fresh_root):
    key = str(fresh_root)
    event_a, owner_a = _claim_run_state_cache_rebuild(key)
    assert owner_a is True
    event_b, owner_b = _claim_run_state_cache_rebuild(key)
    assert owner_b is False
    assert event_a is event_b
    _finish_run_state_cache_rebuild(key)
    event_c, owner_c = _claim_run_state_cache_rebuild(key)
    assert owner_c is True
    _finish_run_state_cache_rebuild(key)


def test_finish_rebuild_unknown_key_is_noop():
    # No prior claim -> pop returns None -> no event.set(); must not raise.
    _finish_run_state_cache_rebuild(str(Path(tempfile.gettempdir()) / "never-claimed-rd"))


def test_finish_rebuild_signals_waiters(fresh_root):
    key = str(fresh_root)
    event, _owner = _claim_run_state_cache_rebuild(key)
    assert not event.is_set()
    _finish_run_state_cache_rebuild(key)
    assert event.is_set()


# --------------------------------------------------------------------------- #
# _write_run_state_cache
# --------------------------------------------------------------------------- #


def test_write_cache_roundtrips_signature_and_rows(fresh_root):
    sp = str(_make_state(fresh_root, "run-1", "s1", app="app-1"))
    _write(_ledger(fresh_root), json.dumps({"session_id": "s1", "state_path": sp, "written_at": 1.0, "app_session_id": "app-1"}) + "\n")
    sig = _sig(fresh_root)
    _write_run_state_cache(fresh_root, sig, {"s1": [(1.0, sp)]}, {sp: "app-1"})
    with _sqlite_connect(run_state_ledger_cache_path(fresh_root)) as conn:
        assert _run_state_cache_signature_current(conn, sig) is True
        row = conn.execute("SELECT app_session_id FROM entries WHERE sid=?", ("s1",)).fetchone()
        assert row == ("app-1",)


def test_write_cache_failure_is_swallowed(fresh_root):
    # cache_path resolves to an existing directory -> os.replace raises -> except swallows.
    _touch_ledger(fresh_root)
    run_state_ledger_cache_path(fresh_root).mkdir(parents=True, exist_ok=True)
    # Must not raise; cache simply isn't written.
    _write_run_state_cache(fresh_root, _sig(fresh_root), {})


def test_write_cache_replaces_stale_tmp_file(fresh_root):
    sp = str(_make_state(fresh_root, "run-1", "s1"))
    _write(_ledger(fresh_root), json.dumps({"session_id": "s1", "state_path": sp, "written_at": 1.0}) + "\n")
    tmp = run_state_ledger_cache_path(fresh_root).with_suffix(".sqlite3.tmp")
    _write(tmp, "stale")  # leftover tmp from a crashed prior write
    _write_run_state_cache(fresh_root, _sig(fresh_root), {"s1": [(1.0, sp)]})
    assert run_state_ledger_cache_path(fresh_root).exists()
    assert not tmp.exists()


# --------------------------------------------------------------------------- #
# _extend_run_state_cache_after_append
# --------------------------------------------------------------------------- #


def test_extend_updates_memory_cache_and_sqlite(fresh_root):
    sp = str(_make_state(fresh_root, "run-1", "s1", app="app-1"))
    prior = _write_cache(fresh_root, {"s1": [(1.0, sp)]})
    # Seed the in-memory cache so the prior-signature match path is entered.
    runs_dir._RUN_STATE_LEDGER_CACHE[str(fresh_root)] = (prior, {"s1": [(1.0, sp)]})
    current = prior  # ledger unchanged since write
    row = {"session_id": "s2", "state_path": str(_make_state(fresh_root, "run-2", "s2", app="app-2")), "written_at": 5.0, "app_session_id": "app-2"}
    _extend_run_state_cache_after_append(fresh_root, prior, current, row)
    # Memory cache advanced to current signature and holds the new sid.
    cached = runs_dir._RUN_STATE_LEDGER_CACHE[str(fresh_root)]
    assert cached[0] == current
    assert "s2" in cached[1]
    # SQLite row upserted.
    with _sqlite_connect(run_state_ledger_cache_path(fresh_root)) as conn:
        assert conn.execute("SELECT app_session_id FROM entries WHERE sid=?", ("s2",)).fetchone() == ("app-2",)


def test_extend_skips_when_path_not_ledger_shaped(fresh_root):
    prior = _write_cache(fresh_root, {})
    row = {"session_id": "s1", "state_path": str(fresh_root / "state.json"), "written_at": 1.0}
    _extend_run_state_cache_after_append(fresh_root, prior, _sig(fresh_root), row)
    assert str(fresh_root) not in runs_dir._RUN_STATE_LEDGER_CACHE


def test_extend_skips_when_rebuild_inflight(fresh_root):
    sp = str(_make_state(fresh_root, "run-1", "s1"))
    prior = _write_cache(fresh_root, {"s1": [(1.0, sp)]})
    key = str(fresh_root)
    runs_dir._RUN_STATE_LEDGER_CACHE_REBUILD_INFLIGHT[key] = threading.Event()
    row = {"session_id": "s2", "state_path": str(_make_state(fresh_root, "run-2", "s2")), "written_at": 1.0}
    _extend_run_state_cache_after_append(fresh_root, prior, _sig(fresh_root), row)
    # Inflight guard returned before touching the (unmatching) memory cache.
    assert key not in runs_dir._RUN_STATE_LEDGER_CACHE


def test_extend_tolerates_corrupt_sqlite(fresh_root):
    sp = str(_make_state(fresh_root, "run-1", "s1"))
    # Seed a memory cache so the prior-signature match path is entered, then corrupt sqlite.
    prior = _sig(fresh_root)
    runs_dir._RUN_STATE_LEDGER_CACHE[str(fresh_root)] = (prior, {"s1": [(1.0, sp)]})
    _write(run_state_ledger_cache_path(fresh_root), b"not sqlite")
    row = {"session_id": "s2", "state_path": str(_make_state(fresh_root, "run-2", "s2")), "written_at": 1.0}
    # Must not raise; memory cache still advanced despite sqlite failure.
    _extend_run_state_cache_after_append(fresh_root, prior, prior, row)
    assert "s2" in runs_dir._RUN_STATE_LEDGER_CACHE[str(fresh_root)][1]


def test_extend_skips_when_sqlite_signature_changed(fresh_root):
    sp = str(_make_state(fresh_root, "run-1", "s1"))
    _write_cache(fresh_root, {"s1": [(1.0, sp)]})
    stale = tuple(v - 1 for v in _sig(fresh_root))
    row = {"session_id": "s2", "state_path": str(_make_state(fresh_root, "run-2", "s2")), "written_at": 1.0}
    _extend_run_state_cache_after_append(fresh_root, stale, _sig(fresh_root), row)
    with _sqlite_connect(run_state_ledger_cache_path(fresh_root)) as conn:
        assert conn.execute("SELECT sid FROM entries WHERE sid=?", ("s2",)).fetchone() is None


# --------------------------------------------------------------------------- #
# ledger_state_files_for_sid (memory hit / cache hit / rebuild / failure)
# --------------------------------------------------------------------------- #


def test_ledger_state_files_no_ledger_returns_empty(fresh_root):
    assert ledger_state_files_for_sid(fresh_root, "s1") == []


def test_ledger_state_files_memory_cache_hit(fresh_root):
    sp = str(_make_state(fresh_root, "run-1", "s1"))
    _write(fresh_root / "run-1" / "events.jsonl", "")
    _touch_ledger(fresh_root)
    sig = _sig(fresh_root)
    runs_dir._RUN_STATE_LEDGER_CACHE[str(fresh_root)] = (sig, {"s1": [(1.0, sp)]})
    assert ledger_state_files_for_sid(fresh_root, "s1") == [Path(sp)]


def test_ledger_state_files_sqlite_cache_hit(fresh_root):
    sp = str(_make_state(fresh_root, "run-1", "s1"))
    _write(fresh_root / "run-1" / "events.jsonl", "")
    _write_cache(fresh_root, {"s1": [(1.0, sp)]})
    assert ledger_state_files_for_sid(fresh_root, "s1") == [Path(sp)]


def test_ledger_state_files_rebuilds_from_ledger_scan(fresh_root):
    sp = str(_make_state(fresh_root, "run-1", "s1"))
    _write(fresh_root / "run-1" / "events.jsonl", "")
    bad = str(_make_state(fresh_root, "run-2", "s1"))  # sibling same sid
    _write(fresh_root / "run-2" / "events.jsonl", "")
    ledger = _ledger(fresh_root)
    rows = [
        json.dumps({"session_id": "s1", "state_path": sp, "written_at": 1.0}),
        json.dumps({"session_id": "s1", "state_path": bad, "written_at": 5.0}),
        "{ corrupt line",
        json.dumps({"session_id": "", "state_path": sp}),
        json.dumps({"session_id": "s1"}),  # no state_path
        json.dumps({"session_id": "s1", "state_path": str(fresh_root / "state.json")}),  # bad shape
    ]
    _write(ledger, "\n".join(rows) + "\n")
    # Both runs exist on disk; both ledger-shaped paths are returned.
    assert set(ledger_state_files_for_sid(fresh_root, "s1")) == {Path(sp), Path(bad)}
    # Rebuild populated the memory cache for subsequent hits.
    assert str(fresh_root) in runs_dir._RUN_STATE_LEDGER_CACHE


def test_ledger_state_files_joiner_uses_owner_result(fresh_root):
    """The not-owner path: another caller owns the rebuild (inflight event
    pre-set), so this caller waits, finds no cache and no published memory
    cache, then falls through to the scan itself (owner=False)."""
    sp = str(_make_state(fresh_root, "run-1", "s1"))
    _write(fresh_root / "run-1" / "events.jsonl", "")
    _write(_ledger(fresh_root), json.dumps({"session_id": "s1", "state_path": sp, "written_at": 1.0}) + "\n")
    key = str(fresh_root)
    done = threading.Event(); done.set()
    runs_dir._RUN_STATE_LEDGER_CACHE_REBUILD_INFLIGHT[key] = done
    # No memory cache, no sqlite cache -> not-owner recheck finds nothing -> scans.
    assert ledger_state_files_for_sid(fresh_root, "s1") == [Path(sp)]


def test_ledger_state_files_ledger_unreadable_returns_empty(fresh_root):
    """The ledger path resolves (stat succeeds) but cannot be opened for read
    (it is a directory) -> OSError branch -> owner finishes, returns []."""
    sp = str(_make_state(fresh_root, "run-1", "s1"))
    _write(fresh_root / "run-1" / "events.jsonl", "")
    ledger = _ledger(fresh_root)
    ledger.mkdir(parents=True, exist_ok=True)  # stat ok, open() raises IsADirectoryError
    assert ledger_state_files_for_sid(fresh_root, "s1") == []


def test_ledger_state_files_signature_drift_recurses(fresh_root):
    sp = str(_make_state(fresh_root, "run-1", "s1"))
    _write(fresh_root / "run-1" / "events.jsonl", "")
    _write(_ledger(fresh_root), json.dumps({"session_id": "s1", "state_path": sp, "written_at": 1.0}) + "\n")
    real_sig = _sig(fresh_root)
    calls = {"n": 0}
    orig = runs_dir._run_state_ledger_signature

    def drifting(path):
        calls["n"] += 1
        # First call (pre-scan) returns a stale signature; every later call the real one.
        return tuple(v - 1 for v in real_sig) if calls["n"] == 1 else real_sig

    runs_dir._run_state_ledger_signature = drifting
    try:
        assert ledger_state_files_for_sid(fresh_root, "s1") == [Path(sp)]
    finally:
        runs_dir._run_state_ledger_signature = orig


# --------------------------------------------------------------------------- #
# state_files_for_sid fallback chain
# --------------------------------------------------------------------------- #


def test_state_files_returns_ledger_when_present(fresh_root):
    sp = str(_make_state(fresh_root, "run-1", "s1"))
    _write(fresh_root / "run-1" / "events.jsonl", "")
    _write_cache(fresh_root, {"s1": [(1.0, sp)]})
    assert state_files_for_sid(fresh_root, "s1") == [Path(sp)]


def test_state_files_returns_empty_when_backfilled_no_ledger(fresh_root):
    write_json(
        run_state_ledger_backfill_marker_path(fresh_root),
        {"version": runs_dir._RUN_STATE_LEDGER_BACKFILL_VERSION},
    )
    assert state_files_for_sid(fresh_root, "s1") == []


def test_state_files_falls_back_to_recent_index(fresh_root):
    # No ledger, no backfill marker -> recent_state_files_for_sid path.
    assert state_files_for_sid(fresh_root, "s1") == []


# --------------------------------------------------------------------------- #
# run_dirs_by_app_session / _load_run_state_app_index
# --------------------------------------------------------------------------- #


def test_run_dirs_by_app_session_no_ledger_returns_empty(fresh_root):
    assert run_dirs_by_app_session(fresh_root) == {}


def test_run_dirs_by_app_session_assembles_index(fresh_root):
    sp1 = str(_make_state(fresh_root, "run-1", "s1", app="app-1"))
    sp2 = str(_make_state(fresh_root, "run-2", "s2", app="app-2"))
    _write(fresh_root / "run-1" / "events.jsonl", "")
    _write(fresh_root / "run-2" / "events.jsonl", "")
    # Mark app-index backfill done so _backfill_run_state_app_index is a no-op
    # and the pre-seeded cache below is the authority.
    write_json(
        run_state_app_index_backfill_marker_path(fresh_root),
        {"version": runs_dir._RUN_STATE_APP_INDEX_BACKFILL_VERSION},
    )
    _write_cache(fresh_root, {"s1": [(1.0, sp1)], "s2": [(1.0, sp2)]}, {sp1: "app-1", sp2: "app-2"})
    result = run_dirs_by_app_session(fresh_root)
    assert result == {"app-1": Path(sp1).parent, "app-2": Path(sp2).parent}


def test_run_dirs_by_app_session_rebuilds_when_index_missing(fresh_root):
    sp = str(_make_state(fresh_root, "run-1", "s1", app="app-1"))
    _write(fresh_root / "run-1" / "events.jsonl", "")
    write_json(
        run_state_app_index_backfill_marker_path(fresh_root),
        {"version": runs_dir._RUN_STATE_APP_INDEX_BACKFILL_VERSION},
    )
    # Seed the ledger-backed cache (app index), but no app-index cache on disk yet.
    _write(_ledger(fresh_root), json.dumps({"session_id": "s1", "state_path": sp, "written_at": 1.0, "app_session_id": "app-1"}) + "\n")
    # First _load_run_state_app_index returns None (no cache) -> triggers rebuild via ledger_state_files_for_sid.
    result = run_dirs_by_app_session(fresh_root)
    assert result == {"app-1": Path(sp).parent}


def test_load_run_state_app_index_corrupt_db_returns_none(fresh_root):
    _write(run_state_ledger_cache_path(fresh_root), b"not sqlite")
    assert _load_run_state_app_index(fresh_root, (1, 2, 3, 4, 5), fresh_root.resolve()) is None


def test_load_run_state_app_index_signature_mismatch_returns_none(fresh_root):
    sp = str(_make_state(fresh_root, "run-1", "s1"))
    _write_cache(fresh_root, {"s1": [(1.0, sp)]})
    wrong = tuple(v + 7 for v in _sig(fresh_root))
    assert _load_run_state_app_index(fresh_root, wrong, fresh_root.resolve()) is None


def test_load_run_state_app_index_rejects_bad_shaped_path(fresh_root):
    sig = _write_cache(fresh_root, {})
    bad = str(fresh_root / "state.json")  # under root directly: wrong shape -> None
    with _sqlite_connect(run_state_ledger_cache_path(fresh_root)) as conn:
        conn.execute("INSERT INTO entries (sid, state_path, written_at, app_session_id) VALUES (?,?,?,?)", ("s", bad, 1.0, "app"))
        conn.commit()
    assert _load_run_state_app_index(fresh_root, sig, fresh_root.resolve()) is None


def test_load_run_state_app_index_skips_missing_and_empty_app(fresh_root):
    sig = _write_cache(fresh_root, {})
    good = str(_make_state(fresh_root, "run-1", "s1", app="app-1"))
    missing = str(fresh_root / "run-2" / "state.json")  # ledger-shaped but no file on disk -> skip
    with _sqlite_connect(run_state_ledger_cache_path(fresh_root)) as conn:
        conn.execute("INSERT INTO entries (sid, state_path, written_at, app_session_id) VALUES (?,?,?,?)", ("s", good, 1.0, "app-1"))
        conn.execute("INSERT INTO entries (sid, state_path, written_at, app_session_id) VALUES (?,?,?,?)", ("s", missing, 1.0, "app-2"))
        conn.commit()
    assert _load_run_state_app_index(fresh_root, sig, fresh_root.resolve()) == {"app-1": Path(good).parent}


def test_load_run_state_app_index_empty_returns_empty_dict(fresh_root):
    sig = _write_cache(fresh_root, {})
    assert _load_run_state_app_index(fresh_root, sig, fresh_root.resolve()) == {}


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
