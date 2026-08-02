"""Hermetic unit tests for the ``runs_dir`` recent-state index cluster.

Covers the fallback recent-state scan/index machinery (used when no run-state
ledger exists yet) and ``ensure_run_state_ledger_backfilled`` — the
security-relevant paths the ledger/cache tests don't touch: scandir candidate
collection (skip non-dirs / symlinked dirs / non-regular / un-stat-able),
state.json parse + session_id grouping with corrupt/empty skip, the
root-signature/TTL/inflight cache handshake, the under-root path filter
(traversal defense), and backfill idempotency + symlink/bad-row skipping.

All filesystem state lives under an isolated ``BETTER_AGENT_HOME`` tempdir
via ``_test_home.isolate``; nothing touches a real home. Module-level
caches are reset between tests so order never matters.

Run with:
    cd backend && .venv/bin/python -m pytest scripts/test_runs_dir_recent_state_unit.py -v
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-runs-dir-recent-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest  # noqa: E402

import runs_dir  # noqa: E402
from runs_dir import (  # noqa: E402
    _backfill_run_state_ledger,
    _build_recent_state_index,
    _filter_recent_state_index,
    _filter_recent_state_paths,
    _recent_state_candidates,
    _recent_state_scan,
    _root_signature,
    ensure_run_state_ledger_backfilled,
    recent_state_files_for_sid,
    recent_state_index_for_root,
    run_state_ledger_backfill_marker_path,
    run_state_ledger_path,
)


@pytest.fixture(autouse=True)
def _reset_module_caches():
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
    path = Path(tempfile.mkdtemp(prefix="bc-rd-recent-root-"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


def _write(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        path.write_text(data, encoding="utf-8")
    else:
        path.write_bytes(data)
    return path


def _make_state(root: Path, run_id: str, sid: str, jsonl: str | None = None) -> Path:
    run_dir = root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    state = {"session_id": sid, "jsonl_path": jsonl or str(run_dir / "events.jsonl")}
    return _write(run_dir / "state.json", json.dumps(state))


def _set_mtime_old(path: Path, age_s: float = 10_000.0) -> None:
    when = time.time() - age_s
    os.utime(path, (when, when))


# --------------------------------------------------------------------------- #
# _recent_state_scan / _recent_state_candidates
# --------------------------------------------------------------------------- #


def test_scan_collects_state_candidates(fresh_root):
    sp = _make_state(fresh_root, "run-1", "s1")
    candidates, pending = _recent_state_scan(fresh_root, fresh_root.resolve())
    assert len(candidates) == 1
    mtime_ns, size, path = candidates[0]
    assert path == str(sp)
    assert size == sp.stat().st_size
    assert pending == ()


def test_scan_pending_for_missing_and_nonregular_state(fresh_root):
    # run with no state.json -> pending; run with a directory named state.json -> pending.
    (fresh_root / "no-state").mkdir()
    (fresh_root / "bad" / "state.json").mkdir(parents=True)
    _make_state(fresh_root, "good", "s1")
    _candidates, pending = _recent_state_scan(fresh_root, fresh_root.resolve())
    assert set(pending) == {"no-state", "bad"}


def test_scan_ignores_non_top_level_files_and_uses_nlargest(fresh_root):
    # A stray file directly under root is not a dir -> skipped.
    _write(fresh_root / "stray.txt", "x")
    # More candidates than the scan limit -> only the newest kept.
    paths = []
    for i in range(runs_dir._RUN_STATE_RECENT_SCAN_LIMIT + 5):
        paths.append(_make_state(fresh_root, f"run-{i}", "s1"))
    _set_mtime_old(paths[0], age_s=10_000)  # oldest -> dropped
    candidates, _pending = _recent_state_scan(fresh_root, fresh_root.resolve())
    assert len(candidates) == runs_dir._RUN_STATE_RECENT_SCAN_LIMIT
    oldest = str(paths[0])
    assert all(path != oldest for _, _, path in candidates)


def test_candidates_wrapper_returns_tuple_or_empty(fresh_root):
    _make_state(fresh_root, "run-1", "s1")
    assert len(_recent_state_candidates(fresh_root)) == 1
    # Non-existent root -> scan returns None -> empty tuple.
    assert _recent_state_candidates(fresh_root / "missing") == ()


# --------------------------------------------------------------------------- #
# _filter_recent_state_paths / _filter_recent_state_index
# --------------------------------------------------------------------------- #


def test_filter_recent_state_paths_drops_outside_root(tmp_path, fresh_root):
    inside = fresh_root / "run-1" / "state.json"
    inside.parent.mkdir(parents=True)
    _write(inside, "{}")
    outside = Path("/tmp/whatever/state.json")  # absolute, not under root
    kept = _filter_recent_state_paths([inside, outside], fresh_root.resolve())
    assert kept == [inside]


def test_filter_recent_state_index_single_sid(fresh_root):
    sp = _make_state(fresh_root, "run-1", "s1")
    index = {"s1": [sp], "s2": [Path("/tmp/x/state.json")]}
    out = _filter_recent_state_index(index, fresh_root.resolve(), agent_sid="s1")
    assert out == {"s1": [sp]}


def test_filter_recent_state_index_single_sid_empty_returns_empty(fresh_root):
    out = _filter_recent_state_index({"s1": []}, fresh_root.resolve(), agent_sid="s1")
    assert out == {}


def test_filter_recent_state_index_all_drops_empty_and_outside(fresh_root):
    sp = _make_state(fresh_root, "run-1", "s1")
    index = {"s1": [sp], "s2": [Path("/tmp/x/state.json")], "s3": []}
    out = _filter_recent_state_index(index, fresh_root.resolve())
    assert out == {"s1": [sp]}


# --------------------------------------------------------------------------- #
# _build_recent_state_index
# --------------------------------------------------------------------------- #


def test_build_index_groups_by_session_id(fresh_root):
    sp1 = _make_state(fresh_root, "run-1", "s1")
    sp2 = _make_state(fresh_root, "run-2", "s1")
    _make_state(fresh_root, "run-3", "s2")
    cands = _recent_state_scan(fresh_root, fresh_root.resolve())[0]
    index = _build_recent_state_index(cands, fresh_root.resolve())
    assert set(index["s1"]) == {sp1, sp2}
    assert len(index["s2"]) == 1


def test_build_index_skips_corrupt_empty_sid_and_outside_root(fresh_root):
    _write(fresh_root / "run-1" / "state.json", "{ not json")  # corrupt
    _write(fresh_root / "run-2" / "state.json", json.dumps({"no": "sid"}))  # no session_id
    _make_state(fresh_root, "run-3", "s1")
    cands = _recent_state_scan(fresh_root, fresh_root.resolve())[0]
    index = _build_recent_state_index(cands, fresh_root.resolve())
    assert set(index.keys()) == {"s1"}


def test_build_index_without_root_resolved_keeps_all(fresh_root):
    sp = _make_state(fresh_root, "run-1", "s1")
    cands = _recent_state_scan(fresh_root, fresh_root.resolve())[0]
    index = _build_recent_state_index(cands, None)
    assert index == {"s1": [sp]}


def test_build_index_drops_outside_root_candidate(fresh_root, tmp_path):
    sp = _make_state(fresh_root, "run-1", "s1")
    # Candidate pointing outside root is dropped even though its text may parse.
    outside = tmp_path / "run-out" / "state.json"
    _write(outside, json.dumps({"session_id": "outsider"}))
    cands = (
        (sp.stat().st_mtime_ns, sp.stat().st_size, str(sp)),
        (outside.stat().st_mtime_ns, outside.stat().st_size, str(outside)),
    )
    assert _build_recent_state_index(cands, fresh_root.resolve()) == {"s1": [sp]}


# --------------------------------------------------------------------------- #
# _backfill_run_state_ledger
# --------------------------------------------------------------------------- #


def test_backfill_appends_valid_state_to_ledger(fresh_root):
    sp = _make_state(fresh_root, "run-1", "s1", jsonl="/x/events.jsonl")
    _backfill_run_state_ledger(fresh_root, {"s1": [sp]})
    ledger = run_state_ledger_path(fresh_root).read_text(encoding="utf-8").strip().splitlines()
    assert len(ledger) == 1
    row = json.loads(ledger[0])
    assert row["session_id"] == "s1"
    assert row["state_path"] == str(sp)


def test_backfill_skips_outside_root_corrupt_and_non_dict(fresh_root):
    valid = _make_state(fresh_root, "run-good", "s1")
    corrupt = _write(fresh_root / "run-bad" / "state.json", "{ not json")  # under root, corrupt
    outside = Path("/tmp/whatever/state.json")
    _backfill_run_state_ledger(fresh_root, {"s1": [valid, corrupt, outside]})
    # Only the valid under-root entry appended; corrupt re-read fails, outside rejected.
    rows = run_state_ledger_path(fresh_root).read_text(encoding="utf-8").strip().splitlines()
    assert len(rows) == 1
    assert json.loads(rows[0])["state_path"] == str(valid)


# --------------------------------------------------------------------------- #
# public recent_state_files_for_sid / recent_state_index_for_root
# --------------------------------------------------------------------------- #


def test_recent_state_files_for_sid_returns_paths(fresh_root):
    sp = _make_state(fresh_root, "run-1", "s1")
    assert recent_state_files_for_sid(fresh_root, "s1") == [sp]


def test_recent_state_files_for_sid_filters_other_sid(fresh_root):
    _make_state(fresh_root, "run-1", "s1")
    assert recent_state_files_for_sid(fresh_root, "other") == []


def test_recent_state_index_for_root_returns_full_map(fresh_root):
    sp1 = _make_state(fresh_root, "run-1", "s1")
    sp2 = _make_state(fresh_root, "run-2", "s2")
    idx = recent_state_index_for_root(fresh_root)
    assert idx == {"s1": [sp1], "s2": [sp2]}


def test_recent_state_index_empty_root_caches_and_returns_empty(fresh_root):
    assert recent_state_index_for_root(fresh_root) == {}
    # Empty-candidates result is cached (fingerprint is empty tuple).
    cached = runs_dir._RUN_STATE_RECENT_INDEX_CACHE[str(fresh_root)]
    assert cached[1] == ()  # fingerprint empty
    assert cached[2] == {}  # index empty


# --------------------------------------------------------------------------- #
# _recent_state_index_for_root cache handshake
# --------------------------------------------------------------------------- #


def test_recent_index_ttl_hit_returns_cached_without_scan(fresh_root):
    sp = _make_state(fresh_root, "run-1", "s1")
    rsig = _root_signature(fresh_root)
    # Seed a fresh, TTL-valid cache entry; prove the scan/rebuild path is NOT taken
    # by patching _build_recent_state_index to fail if invoked.
    runs_dir._RUN_STATE_RECENT_INDEX_CACHE[str(fresh_root)] = (
        time.monotonic(),
        ((1, 1, str(sp)),),
        {"s1": [sp]},
        rsig,
        (),
    )
    orig = runs_dir._build_recent_state_index

    def forbidden(*_a, **_k):
        raise AssertionError("TTL hit must not rebuild the index")

    runs_dir._build_recent_state_index = forbidden
    try:
        assert recent_state_files_for_sid(fresh_root, "s1") == [sp]
    finally:
        runs_dir._build_recent_state_index = orig


def test_recent_index_max_age_reuse_when_candidates_unchanged(fresh_root):
    sp = _make_state(fresh_root, "run-1", "s1")
    root_resolved = fresh_root.resolve()
    rsig = _root_signature(fresh_root)
    fingerprint, _pending = _recent_state_scan(fresh_root, root_resolved)
    # Seed cache just past TTL but within MAX_AGE, fingerprint matching current files.
    runs_dir._RUN_STATE_RECENT_INDEX_CACHE[str(fresh_root)] = (
        time.monotonic() - runs_dir._RUN_STATE_RECENT_INDEX_TTL_S - 0.2,
        fingerprint,
        {"s1": [sp]},
        rsig,
        (),
    )
    orig = runs_dir._build_recent_state_index

    def forbidden(*_a, **_k):
        raise AssertionError("max-age reuse must not rebuild the index")

    runs_dir._build_recent_state_index = forbidden
    try:
        assert recent_state_files_for_sid(fresh_root, "s1") == [sp]
    finally:
        runs_dir._build_recent_state_index = orig


def test_recent_index_max_age_rebuilds_when_candidates_changed(fresh_root):
    sp = _make_state(fresh_root, "run-1", "s1")
    root_resolved = fresh_root.resolve()
    rsig = _root_signature(fresh_root)
    # Seed a stale fingerprint that no longer matches the file on disk.
    runs_dir._RUN_STATE_RECENT_INDEX_CACHE[str(fresh_root)] = (
        time.monotonic() - runs_dir._RUN_STATE_RECENT_INDEX_TTL_S - 0.2,
        ((1, 1, str(sp)),),  # wrong mtime/size
        {"s1": [sp]},
        rsig,
        (),
    )
    # Reuse check fails -> falls through to a real scan + rebuild.
    assert recent_state_files_for_sid(fresh_root, "s1") == [sp]


def test_recent_index_inflight_joiner_returns_after_wait(fresh_root):
    sp = _make_state(fresh_root, "run-1", "s1")
    key = str(fresh_root)
    rsig = _root_signature(fresh_root)
    fingerprint, _pending = _recent_state_scan(fresh_root, fresh_root.resolve())
    # Another owner is "in flight" and has already published a TTL-valid result.
    done = threading.Event()
    done.set()
    runs_dir._RUN_STATE_RECENT_INDEX_INFLIGHT[key] = done
    runs_dir._RUN_STATE_RECENT_INDEX_CACHE[key] = (
        time.monotonic(),
        fingerprint,
        {"s1": [sp]},
        rsig,
        (),
    )
    assert recent_state_files_for_sid(fresh_root, "s1") == [sp]


def test_recent_index_inflight_joiner_no_cache_returns_empty(fresh_root):
    _make_state(fresh_root, "run-1", "s1")
    key = str(fresh_root)
    done = threading.Event()
    done.set()
    runs_dir._RUN_STATE_RECENT_INDEX_INFLIGHT[key] = done
    # No cache published -> joiner falls through to empty.
    assert recent_state_files_for_sid(fresh_root, "s1") == []


def test_recent_index_unstatable_root_returns_empty(fresh_root):
    # A root whose _root_signature is None (stat fails) short-circuits to {}.
    missing = fresh_root / "does-not-exist"
    assert recent_state_index_for_root(missing) == {}


# --------------------------------------------------------------------------- #
# ensure_run_state_ledger_backfilled
# --------------------------------------------------------------------------- #


def test_backfill_first_run_appends_rows_and_writes_marker(fresh_root):
    sp1 = _make_state(fresh_root, "run-1", "s1", jsonl="/a/e.jsonl")
    sp2 = _make_state(fresh_root, "run-2", "s2", jsonl="/b/e.jsonl")
    assert ensure_run_state_ledger_backfilled(fresh_root) is True
    rows = [json.loads(r) for r in run_state_ledger_path(fresh_root).read_text(encoding="utf-8").splitlines()]
    by_sid = {r["session_id"]: r for r in rows}
    assert set(by_sid) == {"s1", "s2"}
    assert by_sid["s1"]["state_path"] == str(sp1)
    assert run_state_ledger_backfill_marker_path(fresh_root).exists()


def test_backfill_second_run_is_idempotent(fresh_root):
    _make_state(fresh_root, "run-1", "s1", jsonl="/a/e.jsonl")
    assert ensure_run_state_ledger_backfilled(fresh_root) is True
    before = run_state_ledger_path(fresh_root).read_text(encoding="utf-8")
    assert ensure_run_state_ledger_backfilled(fresh_root) is False
    after = run_state_ledger_path(fresh_root).read_text(encoding="utf-8")
    assert before == after


def test_backfill_skips_missing_fields_and_non_dict(fresh_root):
    _make_state(fresh_root, "run-1", "s1", jsonl="/a/e.jsonl")  # valid
    _write(fresh_root / "no-sid" / "state.json", json.dumps({"jsonl_path": "/x"}))  # no session_id
    _write(fresh_root / "no-jsonl" / "state.json", json.dumps({"session_id": "s"}))  # no jsonl_path
    _write(fresh_root / "corrupt" / "state.json", "{ bad")  # unparseable
    assert ensure_run_state_ledger_backfilled(fresh_root) is True
    rows = [json.loads(r) for r in run_state_ledger_path(fresh_root).read_text(encoding="utf-8").splitlines()]
    assert [r["session_id"] for r in rows] == ["s1"]


def test_backfill_skips_stray_files_and_symlinked_run_dirs(fresh_root):
    _make_state(fresh_root, "run-1", "s1", jsonl="/a/e.jsonl")
    _write(fresh_root / "stray.json", "{}")  # top-level file, not a dir -> skipped
    # Symlinked run dir: is_dir(follow_symlinks=False) is False -> skipped.
    real = Path(tempfile.mkdtemp(prefix="bc-rd-real-"))
    try:
        _write(real / "state.json", json.dumps({"session_id": "sym", "jsonl_path": "/s/e.jsonl"}))
        os.symlink(real, fresh_root / "linked")
        assert ensure_run_state_ledger_backfilled(fresh_root) is True
        rows = [json.loads(r) for r in run_state_ledger_path(fresh_root).read_text(encoding="utf-8").splitlines()]
        assert [r["session_id"] for r in rows] == ["s1"]  # symlinked "sym" excluded
    finally:
        shutil.rmtree(real, ignore_errors=True)


def test_backfill_dedups_against_existing_ledger_keys(fresh_root):
    sp = _make_state(fresh_root, "run-1", "s1", jsonl="/a/e.jsonl")
    # Pre-seed the ledger with this exact key so backfill must not re-append.
    runs_dir._append_run_state_ledger(sp, {"session_id": "s1", "jsonl_path": "/a/e.jsonl"})
    before_count = len(run_state_ledger_path(fresh_root).read_text(encoding="utf-8").splitlines())
    assert ensure_run_state_ledger_backfilled(fresh_root) is True
    after_count = len(run_state_ledger_path(fresh_root).read_text(encoding="utf-8").splitlines())
    assert before_count == after_count


def test_backfill_empty_root_writes_marker_returns_true(fresh_root):
    assert ensure_run_state_ledger_backfilled(fresh_root) is True
    assert run_state_ledger_backfill_marker_path(fresh_root).exists()
    assert not run_state_ledger_path(fresh_root).exists()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
