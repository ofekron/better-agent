"""Locks NativeFilesManager: tail targets + demand both arrive only as
bus facts, and the manager reconciles OwnedClaudeJsonlTailers (open when
demanded, close when demand drops). Uses a fake tailer so no real file
IO / asyncio tail loops run.

Run: python backend/scripts/test_native_files_manager.py
"""

import os
import shutil
import sys
import tempfile
import threading
import time

import _test_home
_test_home.isolate("nfm-test-")
# Provider-agnostic resolver globs the claude projects dir for an existing
# <sid>.jsonl, so point it at a temp config dir and create the file below.
_CLAUDE_CFG = tempfile.mkdtemp(prefix="nfm-claude-")
os.environ["CLAUDE_CONFIG_DIR"] = _CLAUDE_CFG
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio  # noqa: E402

import jsonl_tailer  # noqa: E402
import native_session_miner as nsm_mod  # noqa: E402
import runs_dir as runs_dir_mod  # noqa: E402
from event_bus import BusEvent, bus  # noqa: E402
import native_files_manager as nfm_mod  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402


class FakeTailer:
    instances: list = []

    def __init__(self, **kw):
        self.kw = kw
        self.app_session_id = kw.get("app_session_id")
        self.agent_sid = kw.get("agent_sid")
        self.acquired = 0
        self.released = 0
        FakeTailer.instances.append(self)

    def acquire(self):
        self.acquired += 1

    def release(self):
        self.released += 1
        return None

    @property
    def alive(self):
        return self.acquired > self.released


def _patch():
    jsonl_tailer.OwnedClaudeJsonlTailer = FakeTailer


async def _demand(nfm, owning, token, present):
    await bus.publish(BusEvent(
        type="native_files.demand",
        root_id=session_manager._root_id_for(owning) or "",
        sid=owning,
        payload={"owning_session": owning, "token": token, "present": present},
        persist=False,
    ))


def _live_keys(nfm):
    return {k for k, t in nfm._tailers.items() if t.alive}


def _make_run_state_backfill_marker_stale(root):
    from runs_dir import run_state_ledger_backfill_marker_path

    run_state_ledger_backfill_marker_path(root).write_text(
        '{"version":-1,"backfilled_at":1}',
        encoding="utf-8",
    )


async def _wait_for(predicate, *, timeout=1.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


async def main():
    _patch()
    nfm = nfm_mod.NativeFilesManager()
    nfm.bind()

    # A session with a known primary sid + a worker-fork panel.
    sess = session_manager.create(name="t", cwd="/tmp/proj", orchestration_mode="manager")
    sid = sess["id"]
    # Create the on-disk claude jsonl the resolver globs for.
    proj = os.path.join(_CLAUDE_CFG, "projects", "-tmp-proj")
    os.makedirs(proj, exist_ok=True)
    open(os.path.join(proj, "PRIMARY-SID.jsonl"), "w").close()
    session_manager.set_agent_sid(sid, "manager", "PRIMARY-SID")
    # Add a fork panel onto the LIVE session record (what a delegation
    # turn would persist) so cold-seed picks it up. Include
    # `fork_agent_session_id` and write a prep-skip cursor on that fork BC
    # session — this is the delegation's prep-skip target.
    fork_bc = session_manager.create(name="fbc", cwd="/tmp/proj", orchestration_mode="manager")
    fork_bc_id = fork_bc["id"]
    PREP_SKIP_LINES = 17
    session_manager.advance_processed_lines(
        fork_bc_id, "FORK-SID", PREP_SKIP_LINES, bump_updated_at=False,
    )
    rid0 = session_manager._root_id_for(sid)
    session_manager._roots[rid0].setdefault("messages", []).append({
        "role": "assistant",
        "workers": [{
            "fork_agent_sid": "FORK-SID",
            "fork_agent_session_id": fork_bc_id,
            "jsonl_path": "/tmp/proj/FORK-SID.jsonl",
        }],
    })

    # 1. No demand → nothing tailed.
    await asyncio.sleep(0)
    assert not _live_keys(nfm), "no demand should mean no tailers"

    # 2. Demand from one subscriber → primary + fork tailers open (seeded
    #    from the store on first demand).
    await _demand(nfm, sid, token="tokA", present=True)
    rid = session_manager._root_id_for(sid)
    assert await _wait_for(lambda: (rid, "PRIMARY-SID") in _live_keys(nfm)), (
        "primary not tailed after background resolution"
    )
    live = _live_keys(nfm)
    assert (rid, "PRIMARY-SID") in live, f"primary not tailed: {live}"
    assert (rid, "FORK-SID") in live, f"fork not tailed: {live}"
    assert nfm.is_tailing_root(rid) is True

    # 2b. REGRESSION: cold-seed must read the prep-skip cursor off the
    #     FORK Better Agent session record (not the parent's). Without the fix, the
    #     fork tailer opens at offset 0 and re-emits the parent-inherited
    #     prep lines.
    fork_tailer = next(t for t in FakeTailer.instances
                       if t.kw.get("agent_sid") == "FORK-SID")
    assert fork_tailer.kw["start_offset"] == PREP_SKIP_LINES, (
        f"fork opened at offset {fork_tailer.kw['start_offset']}, "
        f"expected {PREP_SKIP_LINES} (prep-skip cursor on fork BC record)"
    )

    # 3. Second subscriber on same session → no duplicate tailers.
    n_before = len(live)
    await _demand(nfm, sid, token="tokB", present=True)
    assert len(_live_keys(nfm)) == n_before, "duplicate demand spawned tailers"

    # 4. One subscriber leaves → still demanded by the other → still open.
    await _demand(nfm, sid, token="tokA", present=False)
    assert nfm.is_tailing_root(rid) is True, "closed while still demanded"

    # 5. Last subscriber leaves → all tailers close.
    await _demand(nfm, sid, token="tokB", present=False)
    assert not _live_keys(nfm), "tailers not closed after last demand dropped"
    assert nfm.is_tailing_root(rid) is False

    # 6. Mid-session live discovery: re-demand, then a NEW fork arrives via
    #    `native_files.fork_target` AFTER demand is already present.
    #    Mirrors the delegation source order: prep-skip cursor is written
    #    on the fork Better Agent session FIRST, then the fork_target event fires.
    #    The opened tailer must start at the post-prep-skip offset.
    await _demand(nfm, sid, token="tokA", present=True)
    fork2_bc = session_manager.create(name="fbc2", cwd="/tmp/proj", orchestration_mode="manager")
    fork2_bc_id = fork2_bc["id"]
    PREP_SKIP_FORK2 = 9
    session_manager.advance_processed_lines(
        fork2_bc_id, "FORK2-SID", PREP_SKIP_FORK2, bump_updated_at=False,
    )
    await bus.publish(BusEvent(
        type="native_files.fork_target",
        root_id=rid,
        sid=sid,
        payload={
            "parent_app_session_id": sid,
            "fork_agent_sid": "FORK2-SID",
            "fork_agent_session_id": fork2_bc_id,
            "jsonl_path": "/tmp/proj/FORK2-SID.jsonl",
        },
        persist=False,
    ))
    assert (rid, "FORK2-SID") in _live_keys(nfm), "live fork target not tailed"
    fork2_tailer = next(t for t in FakeTailer.instances
                        if t.kw.get("agent_sid") == "FORK2-SID")
    assert fork2_tailer.kw["start_offset"] == PREP_SKIP_FORK2, (
        f"live fork opened at offset {fork2_tailer.kw['start_offset']}, "
        f"expected {PREP_SKIP_FORK2}"
    )

    # 7. ws_callback=None sweep (token=None present=False) drops ALL demand.
    await _demand(nfm, sid, token=None, present=False)
    assert not _live_keys(nfm), "token=None sweep did not drop all demand"

    # 8. REGRESSION (file lags agent_sid_set): the primary sid is announced
    #    before its native jsonl is flushed. The glob-based resolver misses,
    #    but the run state.json (written at spawn) carries the path. The
    #    primary MUST still be tailed — not silently dropped.
    from runs_dir import runs_root
    sess2 = session_manager.create(name="t2", cwd="/tmp/proj2", orchestration_mode="manager")
    sid2 = sess2["id"]
    lag_jsonl = "/tmp/proj2/LAGGING-SID.jsonl"  # deliberately NOT created
    run_dir = runs_root() / "run-lagging"
    run_dir.mkdir(parents=True, exist_ok=True)
    import json as _json
    (run_dir / "state.json").write_text(
        _json.dumps({"session_id": "LAGGING-SID", "jsonl_path": lag_jsonl}),
        encoding="utf-8",
    )
    assert not os.path.exists(lag_jsonl), "test setup: jsonl must not exist"
    session_manager.set_agent_sid(sid2, "manager", "LAGGING-SID")
    await _demand(nfm, sid2, token="tokC", present=True)
    rid2 = session_manager._root_id_for(sid2)
    assert await _wait_for(lambda: (rid2, "LAGGING-SID") in _live_keys(nfm)), (
        "primary dropped when jsonl file lags agent_sid_set (BLOCKER regression)"
    )
    assert (rid2, "LAGGING-SID") in _live_keys(nfm), (
        "primary dropped when jsonl file lags agent_sid_set (BLOCKER regression)"
    )
    # the tailer must have been handed the path the runner recorded.
    opened = next(t for t in FakeTailer.instances
                  if t.kw.get("agent_sid") == "LAGGING-SID")
    assert str(opened.kw["jsonl_path"]) == lag_jsonl, opened.kw["jsonl_path"]

    print("PASS test_native_files_manager")


async def test_local_run_state_skips_expensive_jsonl_scan() -> None:
    from runs_dir import runs_root
    from orchs import jsonl_helpers

    nfm_mod._RUN_STATE_LOOKUP_CACHE.clear()
    runs_dir_mod._RUN_STATE_RECENT_INDEX_CACHE.clear()
    sess = session_manager.create(name="state-first", cwd="/tmp/state-first", orchestration_mode="manager")
    sid = sess["id"]
    agent_sid = "STATE-FIRST-SID"
    lag_jsonl = "/tmp/state-first/STATE-FIRST-SID.jsonl"
    run_dir = runs_root() / "run-state-first"
    run_dir.mkdir(parents=True, exist_ok=True)
    import json as _json
    (run_dir / "state.json").write_text(
        _json.dumps({"session_id": agent_sid, "jsonl_path": lag_jsonl}),
        encoding="utf-8",
    )
    os.utime(run_dir / "state.json", (time.time() + 10, time.time() + 10))
    original_compute = jsonl_helpers.compute_jsonl_read_path

    def fail_compute(*_args, **_kwargs):
        raise AssertionError("run-state path should avoid expensive jsonl scan")

    jsonl_helpers.compute_jsonl_read_path = fail_compute
    try:
        path = nfm_mod._resolve_primary_jsonl(sess, agent_sid)
    finally:
        jsonl_helpers.compute_jsonl_read_path = original_compute
    assert str(path) == lag_jsonl, path
    print("PASS test_local_run_state_skips_expensive_jsonl_scan")


async def test_run_state_lookup_is_targeted_and_cached() -> None:
    from runs_dir import runs_root

    nfm_mod._RUN_STATE_LOOKUP_CACHE.clear()
    runs_dir_mod._RUN_STATE_RECENT_INDEX_CACHE.clear()
    root = runs_root()
    for name, sid in (("run-index-a", "INDEX-A-SID"), ("run-index-b", "INDEX-B-SID")):
        run_dir = root / name
        run_dir.mkdir(parents=True, exist_ok=True)
        import json as _json
        (run_dir / "state.json").write_text(
            _json.dumps({"session_id": sid, "jsonl_path": f"/tmp/{sid}.jsonl"}),
            encoding="utf-8",
        )
    assert str(nfm_mod._scan_run_state_for_jsonl("INDEX-A-SID")) == "/tmp/INDEX-A-SID.jsonl"
    original_state_files = nfm_mod._state_files_for_sid

    def fail_state_files(*_args, **_kwargs):
        raise AssertionError("cached run-state lookup should avoid rescanning runs_root")

    nfm_mod._state_files_for_sid = fail_state_files  # type: ignore
    try:
        assert str(nfm_mod._scan_run_state_for_jsonl("INDEX-A-SID")) == "/tmp/INDEX-A-SID.jsonl"
    finally:
        nfm_mod._state_files_for_sid = original_state_files  # type: ignore
    print("PASS test_run_state_lookup_is_targeted_and_cached")


async def test_run_state_lookup_uses_ledger_before_recent_scan() -> None:
    from runs_dir import runs_root, atomic_write_json

    nfm_mod._RUN_STATE_LOOKUP_CACHE.clear()
    runs_dir_mod._RUN_STATE_RECENT_INDEX_CACHE.clear()
    runs_dir_mod._RUN_STATE_LEDGER_CACHE.clear()
    root = runs_root()
    run_dir = root / "run-ledger-fast"
    run_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(
        run_dir / "state.json",
        {"session_id": "LEDGER-FAST-SID", "jsonl_path": "/tmp/ledger-fast.jsonl"},
    )
    original_scan = runs_dir_mod._recent_state_scan

    def fail_scan(*_args, **_kwargs):
        raise AssertionError("run-state ledger should avoid recent-dir scan")

    runs_dir_mod._recent_state_scan = fail_scan  # type: ignore
    try:
        path = nfm_mod._scan_run_state_for_jsonl("LEDGER-FAST-SID")
    finally:
        runs_dir_mod._recent_state_scan = original_scan  # type: ignore
    assert str(path) == "/tmp/ledger-fast.jsonl", path
    print("PASS test_run_state_lookup_uses_ledger_before_recent_scan")


async def test_run_state_ledger_rejects_paths_outside_runs_root() -> None:
    from runs_dir import runs_root, run_state_ledger_path

    nfm_mod._RUN_STATE_LOOKUP_CACHE.clear()
    runs_dir_mod._RUN_STATE_RECENT_INDEX_CACHE.clear()
    runs_dir_mod._RUN_STATE_LEDGER_CACHE.clear()
    root = runs_root()
    root.mkdir(parents=True, exist_ok=True)
    outside = root.parent / "outside-state.json"
    outside.write_text(
        '{"session_id":"LEDGER-ESCAPE","jsonl_path":"/tmp/escape.jsonl"}',
        encoding="utf-8",
    )
    run_state_ledger_path(root).write_text(
        '{"session_id":"LEDGER-ESCAPE","jsonl_path":"/tmp/escape.jsonl",'
        f'"state_path":"{outside}","written_at":1}}\n',
        encoding="utf-8",
    )
    original_scan = runs_dir_mod._recent_state_scan

    def no_scan(*_args, **_kwargs):
        return (), ()

    runs_dir_mod._recent_state_scan = no_scan  # type: ignore
    try:
        path = nfm_mod._scan_run_state_for_jsonl("LEDGER-ESCAPE")
    finally:
        runs_dir_mod._recent_state_scan = original_scan  # type: ignore
    assert path is None, path
    print("PASS test_run_state_ledger_rejects_paths_outside_runs_root")


async def test_run_state_recent_lookup_does_not_backfill_ledger() -> None:
    from runs_dir import runs_root

    nfm_mod._RUN_STATE_LOOKUP_CACHE.clear()
    runs_dir_mod._RUN_STATE_RECENT_INDEX_CACHE.clear()
    runs_dir_mod._RUN_STATE_LEDGER_CACHE.clear()
    root = runs_root()
    run_dir = root / "run-backfill"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "state.json").write_text(
        '{"session_id":"BACKFILL-SID","jsonl_path":"/tmp/backfill.jsonl"}',
        encoding="utf-8",
    )
    original_backfill = runs_dir_mod._backfill_run_state_ledger

    def fail_backfill(*_args, **_kwargs):
        raise AssertionError("recent lookup should not backfill the ledger")

    runs_dir_mod._backfill_run_state_ledger = fail_backfill  # type: ignore
    try:
        assert str(nfm_mod._scan_run_state_for_jsonl("BACKFILL-SID")) == "/tmp/backfill.jsonl"
        nfm_mod._RUN_STATE_LOOKUP_CACHE.clear()
        assert nfm_mod._scan_run_state_for_jsonl("MISSING-BACKFILL-SID") is None
    finally:
        runs_dir_mod._backfill_run_state_ledger = original_backfill  # type: ignore
    print("PASS test_run_state_recent_lookup_does_not_backfill_ledger")


async def test_run_state_backfill_rejects_symlink_escape() -> None:
    from runs_dir import runs_root, run_state_ledger_path

    nfm_mod._RUN_STATE_LOOKUP_CACHE.clear()
    runs_dir_mod._RUN_STATE_RECENT_INDEX_CACHE.clear()
    runs_dir_mod._RUN_STATE_LEDGER_CACHE.clear()
    root = runs_root()
    root.mkdir(parents=True, exist_ok=True)
    outside_dir = root.parent / "outside-run-state"
    outside_dir.mkdir(parents=True, exist_ok=True)
    (outside_dir / "state.json").write_text(
        '{"session_id":"SYMLINK-ESCAPE","jsonl_path":"/tmp/symlink-escape.jsonl"}',
        encoding="utf-8",
    )
    link_dir = root / "run-symlink-escape"
    try:
        link_dir.symlink_to(outside_dir, target_is_directory=True)
    except FileExistsError:
        pass
    assert runs_dir_mod._build_recent_state_index(
        ((1, 1, str(link_dir / "state.json")),),
        root.resolve(),
    ) == {}
    runs_dir_mod._backfill_run_state_ledger(root, {"SYMLINK-ESCAPE": [link_dir / "state.json"]})
    ledger = run_state_ledger_path(root)
    text = ledger.read_text(encoding="utf-8") if ledger.exists() else ""
    assert "SYMLINK-ESCAPE" not in text, text
    print("PASS test_run_state_backfill_rejects_symlink_escape")


async def test_run_state_ledger_dedupes_duplicate_rows() -> None:
    from runs_dir import runs_root, run_state_ledger_path, _RUN_STATE_LEDGER_SEEN

    nfm_mod._RUN_STATE_LOOKUP_CACHE.clear()
    runs_dir_mod._RUN_STATE_RECENT_INDEX_CACHE.clear()
    runs_dir_mod._RUN_STATE_LEDGER_CACHE.clear()
    _RUN_STATE_LEDGER_SEEN.clear()
    root = runs_root()
    run_dir = root / "run-ledger-dedup"
    run_dir.mkdir(parents=True, exist_ok=True)
    state_path = run_dir / "state.json"
    state_path.write_text(
        '{"session_id":"DEDUP-SID","jsonl_path":"/tmp/dedup.jsonl"}',
        encoding="utf-8",
    )
    runs_dir_mod._backfill_run_state_ledger(root, {"DEDUP-SID": [state_path]})
    _RUN_STATE_LEDGER_SEEN.clear()
    runs_dir_mod._backfill_run_state_ledger(root, {"DEDUP-SID": [state_path]})
    runs_dir_mod._RUN_STATE_LEDGER_CACHE.clear()
    paths = runs_dir_mod.ledger_state_files_for_sid(root, "DEDUP-SID")
    assert paths == [state_path], paths
    lines = [
        line for line in run_state_ledger_path(root).read_text(encoding="utf-8").splitlines()
        if "DEDUP-SID" in line
    ]
    assert len(lines) == 1, lines
    print("PASS test_run_state_ledger_dedupes_duplicate_rows")


async def test_run_state_full_backfill_finds_old_state_outside_recent_window() -> None:
    from runs_dir import runs_root, atomic_write_json

    nfm_mod._RUN_STATE_LOOKUP_CACHE.clear()
    runs_dir_mod._RUN_STATE_RECENT_INDEX_CACHE.clear()
    runs_dir_mod._RUN_STATE_LEDGER_CACHE.clear()
    root = runs_root()
    old_dir = root / "run-full-backfill-old"
    old_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(
        old_dir / "state.json",
        {"session_id": "FULL-BACKFILL-OLD", "jsonl_path": "/tmp/full-backfill-old.jsonl"},
    )
    old_time = time.time() - 3600
    os.utime(old_dir / "state.json", (old_time, old_time))
    for index in range(runs_dir_mod._RUN_STATE_RECENT_SCAN_LIMIT + 4):
        run_dir = root / f"run-full-backfill-new-{index}"
        run_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(
            run_dir / "state.json",
            {"session_id": f"FULL-BACKFILL-NEW-{index}", "jsonl_path": f"/tmp/new-{index}.jsonl"},
        )
    assert runs_dir_mod.ensure_run_state_ledger_backfilled(root) is True
    original_scan = runs_dir_mod._recent_state_scan

    def fail_scan(*_args, **_kwargs):
        raise AssertionError("explicit full ledger backfill should avoid recent-dir fallback")

    runs_dir_mod._recent_state_scan = fail_scan  # type: ignore
    try:
        path = nfm_mod._scan_run_state_for_jsonl("FULL-BACKFILL-OLD")
    finally:
        runs_dir_mod._recent_state_scan = original_scan  # type: ignore
    assert str(path) == "/tmp/full-backfill-old.jsonl", path
    print("PASS test_run_state_full_backfill_finds_old_state_outside_recent_window")


async def test_run_state_full_backfill_marker_dedupes_rows() -> None:
    from runs_dir import (
        ensure_run_state_ledger_backfilled,
        run_state_ledger_backfill_marker_path,
        run_state_ledger_path,
        atomic_write_json,
    )

    root = nfm_mod.Path(tempfile.mkdtemp(prefix="nfm-full-dedup-"))
    run_dir = root / "run-full-backfill-dedup"
    run_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(
        run_dir / "state.json",
        {"session_id": "FULL-DEDUP", "jsonl_path": "/tmp/full-dedup.jsonl"},
    )
    assert ensure_run_state_ledger_backfilled(root) is True
    assert ensure_run_state_ledger_backfilled(root) is False
    assert run_state_ledger_backfill_marker_path(root).exists()
    lines = [
        line for line in run_state_ledger_path(root).read_text(encoding="utf-8").splitlines()
        if "FULL-DEDUP" in line
    ]
    assert len(lines) == 1, lines
    print("PASS test_run_state_full_backfill_marker_dedupes_rows")


async def test_run_state_full_backfill_skips_symlink_escape() -> None:
    from runs_dir import (
        ensure_run_state_ledger_backfilled,
        run_state_ledger_path,
    )

    root = nfm_mod.Path(tempfile.mkdtemp(prefix="nfm-full-symlink-"))
    root.mkdir(parents=True, exist_ok=True)
    outside_dir = root.parent / "outside-full-backfill"
    outside_dir.mkdir(parents=True, exist_ok=True)
    (outside_dir / "state.json").write_text(
        '{"session_id":"FULL-SYMLINK-ESCAPE","jsonl_path":"/tmp/full-symlink.jsonl"}',
        encoding="utf-8",
    )
    link_dir = root / "run-full-symlink-escape"
    try:
        link_dir.symlink_to(outside_dir, target_is_directory=True)
    except FileExistsError:
        pass
    assert ensure_run_state_ledger_backfilled(root) is True
    ledger = run_state_ledger_path(root)
    text = ledger.read_text(encoding="utf-8") if ledger.exists() else ""
    assert "FULL-SYMLINK-ESCAPE" not in text, text
    print("PASS test_run_state_full_backfill_skips_symlink_escape")


async def test_run_state_full_backfill_coalesces_concurrent_marker_writes() -> None:
    from runs_dir import ensure_run_state_ledger_backfilled, atomic_write_json
    import runs_dir as runs_dir_mod

    root = nfm_mod.Path(tempfile.mkdtemp(prefix="nfm-full-concurrent-"))
    run_dir = root / "run-full-backfill-concurrent"
    run_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(
        run_dir / "state.json",
        {"session_id": "FULL-CONCURRENT", "jsonl_path": "/tmp/full-concurrent.jsonl"},
    )
    original_scandir = runs_dir_mod.os.scandir
    scan_count = 0
    scan_lock = threading.Lock()

    def counted_scandir(path):
        nonlocal scan_count
        if str(path) == str(root):
            with scan_lock:
                scan_count += 1
            time.sleep(0.05)
        return original_scandir(path)

    results: list[bool] = []
    runs_dir_mod.os.scandir = counted_scandir  # type: ignore
    try:
        threads = [
            threading.Thread(
                target=lambda: results.append(ensure_run_state_ledger_backfilled(root))
            )
            for _ in range(2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    finally:
        runs_dir_mod.os.scandir = original_scandir  # type: ignore
    assert scan_count == 1, scan_count
    assert results.count(True) == 1, results
    assert results.count(False) == 1, results
    print("PASS test_run_state_full_backfill_coalesces_concurrent_marker_writes")


async def test_run_state_backfill_is_scheduled_at_startup() -> None:
    source = (nfm_mod.Path(__file__).resolve().parents[1] / "main.py").read_text(
        encoding="utf-8"
    )
    startup_start = source.index("async def on_startup()")
    startup_source = source[startup_start:]
    assert "from runs_dir import ensure_run_state_ledger_backfilled" in startup_source
    assert '"run_state_ledger_backfill"' in startup_source
    assert "ensure_run_state_ledger_backfilled" in startup_source
    print("PASS test_run_state_backfill_is_scheduled_at_startup")


async def test_run_state_stale_index_does_not_hide_new_state() -> None:
    from runs_dir import atomic_write_json, runs_root

    nfm_mod._RUN_STATE_LOOKUP_CACHE.clear()
    runs_dir_mod._RUN_STATE_RECENT_INDEX_CACHE.clear()
    runs_dir_mod._RUN_STATE_LEDGER_CACHE.clear()
    root = runs_root()
    old_dir = root / "run-stale-index-old"
    old_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(
        old_dir / "state.json",
        {"session_id": "STALE-OLD", "jsonl_path": "/tmp/stale-old.jsonl"},
    )
    assert nfm_mod._scan_run_state_for_jsonl("STALE-OLD") is not None
    root_key = str(root)
    cached = runs_dir_mod._RUN_STATE_RECENT_INDEX_CACHE.get(root_key)
    if cached is not None:
        ts, fingerprint, index, root_signature, pending_run_dirs = cached
        runs_dir_mod._RUN_STATE_RECENT_INDEX_CACHE[root_key] = (
            ts - runs_dir_mod._RUN_STATE_RECENT_INDEX_TTL_S - 0.1,
            fingerprint,
            index,
            root_signature,
            pending_run_dirs,
        )
    new_dir = root / "run-stale-index-new"
    new_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(
        new_dir / "state.json",
        {"session_id": "STALE-NEW", "jsonl_path": "/tmp/stale-new.jsonl"},
    )
    path = nfm_mod._scan_run_state_for_jsonl("STALE-NEW")
    assert str(path) == "/tmp/stale-new.jsonl", path
    print("PASS test_run_state_stale_index_does_not_hide_new_state")


async def test_run_state_positive_cache_outlives_negative_cache() -> None:
    nfm_mod._RUN_STATE_LOOKUP_CACHE.clear()
    root_key = "/tmp/run-state-cache-root"
    sid = "CACHE-TTL-SID"
    positive_path = nfm_mod.Path("/tmp/cache-ttl.jsonl")
    now = nfm_mod.time.monotonic()
    nfm_mod._RUN_STATE_LOOKUP_CACHE[(root_key, sid)] = (
        now - nfm_mod._RUN_STATE_LOOKUP_CACHE_TTL_S - 0.5,
        positive_path,
    )
    assert nfm_mod._run_state_cache_get(root_key, sid) == positive_path
    nfm_mod._RUN_STATE_LOOKUP_CACHE[(root_key, sid)] = (
        now - nfm_mod._RUN_STATE_LOOKUP_CACHE_TTL_S - 0.5,
        None,
    )
    assert nfm_mod._run_state_cache_get(root_key, sid) is False
    print("PASS test_run_state_positive_cache_outlives_negative_cache")


async def test_run_state_ledger_cache_reuses_unchanged_index() -> None:
    from runs_dir import _append_run_state_ledger, runs_root

    nfm_mod._RUN_STATE_LOOKUP_CACHE.clear()
    runs_dir_mod._RUN_STATE_RECENT_INDEX_CACHE.clear()
    runs_dir_mod._RUN_STATE_LEDGER_CACHE.clear()
    root = runs_root()
    run_a = root / "run-ledger-cache-a"
    run_b = root / "run-ledger-cache-b"
    for run_dir, sid in ((run_a, "LEDGER-CACHE-A"), (run_b, "LEDGER-CACHE-B")):
        run_dir.mkdir(parents=True, exist_ok=True)
        state_path = run_dir / "state.json"
        state_path.write_text(
            f'{{"session_id":"{sid}","jsonl_path":"/tmp/{sid}.jsonl"}}',
            encoding="utf-8",
        )
        _append_run_state_ledger(
            state_path,
            {"session_id": sid, "jsonl_path": f"/tmp/{sid}.jsonl"},
        )
    assert runs_dir_mod.ledger_state_files_for_sid(root, "LEDGER-CACHE-A") == [
        run_a / "state.json"
    ]
    original_json_loads = nfm_mod.json.loads

    def fail_json_loads(*_args, **_kwargs):
        raise AssertionError("unchanged ledger cache should not reparse jsonl")

    nfm_mod.json.loads = fail_json_loads  # type: ignore
    try:
        path = runs_dir_mod.ledger_state_files_for_sid(root, "LEDGER-CACHE-B")
    finally:
        nfm_mod.json.loads = original_json_loads  # type: ignore
    assert path == [run_b / "state.json"], path
    print("PASS test_run_state_ledger_cache_reuses_unchanged_index")


async def test_run_state_ledger_cache_invalidates_on_append() -> None:
    from runs_dir import _append_run_state_ledger, runs_root

    nfm_mod._RUN_STATE_LOOKUP_CACHE.clear()
    runs_dir_mod._RUN_STATE_RECENT_INDEX_CACHE.clear()
    runs_dir_mod._RUN_STATE_LEDGER_CACHE.clear()
    root = runs_root()
    run_a = root / "run-ledger-append-a"
    run_a.mkdir(parents=True, exist_ok=True)
    state_a = run_a / "state.json"
    state_a.write_text(
        '{"session_id":"LEDGER-APPEND-A","jsonl_path":"/tmp/ledger-append-a.jsonl"}',
        encoding="utf-8",
    )
    _append_run_state_ledger(
        state_a,
        {"session_id": "LEDGER-APPEND-A", "jsonl_path": "/tmp/ledger-append-a.jsonl"},
    )
    assert runs_dir_mod.ledger_state_files_for_sid(root, "LEDGER-APPEND-A") == [state_a]
    run_b = root / "run-ledger-append-b"
    run_b.mkdir(parents=True, exist_ok=True)
    state_b = run_b / "state.json"
    state_b.write_text(
        '{"session_id":"LEDGER-APPEND-B","jsonl_path":"/tmp/ledger-append-b.jsonl"}',
        encoding="utf-8",
    )
    _append_run_state_ledger(
        state_b,
        {"session_id": "LEDGER-APPEND-B", "jsonl_path": "/tmp/ledger-append-b.jsonl"},
    )
    original_json_loads = runs_dir_mod.json.loads

    def fail_jsonl_parse(raw, *args, **kwargs):  # type: ignore[no-untyped-def]
        if '"session_id"' in raw:
            raise AssertionError("append should extend current in-memory cache")
        return original_json_loads(raw, *args, **kwargs)

    runs_dir_mod.json.loads = fail_jsonl_parse  # type: ignore
    try:
        assert runs_dir_mod.ledger_state_files_for_sid(root, "LEDGER-APPEND-B") == [state_b]
    finally:
        runs_dir_mod.json.loads = original_json_loads  # type: ignore
    print("PASS test_run_state_ledger_cache_invalidates_on_append")


async def test_run_state_ledger_cache_rejects_cached_symlink_escape() -> None:
    from runs_dir import _append_run_state_ledger, runs_root

    nfm_mod._RUN_STATE_LOOKUP_CACHE.clear()
    runs_dir_mod._RUN_STATE_RECENT_INDEX_CACHE.clear()
    runs_dir_mod._RUN_STATE_LEDGER_CACHE.clear()
    root = runs_root()
    run_dir = root / "run-ledger-cache-escape"
    run_dir.mkdir(parents=True, exist_ok=True)
    state_path = run_dir / "state.json"
    state_path.write_text(
        '{"session_id":"LEDGER-CACHE-ESCAPE","jsonl_path":"/tmp/ledger-cache-escape.jsonl"}',
        encoding="utf-8",
    )
    _append_run_state_ledger(
        state_path,
        {
            "session_id": "LEDGER-CACHE-ESCAPE",
            "jsonl_path": "/tmp/ledger-cache-escape.jsonl",
        },
    )
    assert runs_dir_mod.ledger_state_files_for_sid(root, "LEDGER-CACHE-ESCAPE") == [
        state_path
    ]
    outside_dir = root.parent / "outside-ledger-cache-escape"
    outside_dir.mkdir(parents=True, exist_ok=True)
    state_path.unlink()
    run_dir.rmdir()
    run_dir.symlink_to(outside_dir, target_is_directory=True)
    assert runs_dir_mod.ledger_state_files_for_sid(root, "LEDGER-CACHE-ESCAPE") == []
    nfm_mod._RUN_STATE_LOOKUP_CACHE.clear()
    assert nfm_mod._scan_run_state_for_jsonl("LEDGER-CACHE-ESCAPE") is None
    print("PASS test_run_state_ledger_cache_rejects_cached_symlink_escape")


async def test_run_state_ledger_sqlite_cache_skips_jsonl_parse() -> None:
    from runs_dir import _append_run_state_ledger, run_state_ledger_cache_path, runs_root

    nfm_mod._RUN_STATE_LOOKUP_CACHE.clear()
    runs_dir_mod._RUN_STATE_RECENT_INDEX_CACHE.clear()
    runs_dir_mod._RUN_STATE_LEDGER_CACHE.clear()
    root = runs_root()
    run_dir = root / "run-ledger-sqlite-cache"
    run_dir.mkdir(parents=True, exist_ok=True)
    state_path = run_dir / "state.json"
    state_path.write_text(
        '{"session_id":"LEDGER-SQLITE-CACHE","jsonl_path":"/tmp/ledger-sqlite-cache.jsonl"}',
        encoding="utf-8",
    )
    _append_run_state_ledger(
        state_path,
        {"session_id": "LEDGER-SQLITE-CACHE", "jsonl_path": "/tmp/ledger-sqlite-cache.jsonl"},
    )
    assert runs_dir_mod.ledger_state_files_for_sid(root, "LEDGER-SQLITE-CACHE") == [state_path]
    assert run_state_ledger_cache_path(root).exists()
    runs_dir_mod._RUN_STATE_LEDGER_CACHE.clear()
    original_json_loads = runs_dir_mod.json.loads

    def fail_jsonl_parse(raw, *args, **kwargs):  # type: ignore[no-untyped-def]
        if '"session_id"' in raw:
            raise AssertionError("sqlite cache hit should not parse ledger jsonl")
        return original_json_loads(raw, *args, **kwargs)

    runs_dir_mod.json.loads = fail_jsonl_parse  # type: ignore
    try:
        assert runs_dir_mod.ledger_state_files_for_sid(root, "LEDGER-SQLITE-CACHE") == [state_path]
        assert runs_dir_mod.ledger_state_files_for_sid(root, "LEDGER-SQLITE-MISSING") == []
    finally:
        runs_dir_mod.json.loads = original_json_loads  # type: ignore
    print("PASS test_run_state_ledger_sqlite_cache_skips_jsonl_parse")


async def test_run_state_ledger_sqlite_cache_invalidates_on_append() -> None:
    from runs_dir import _append_run_state_ledger, runs_root

    nfm_mod._RUN_STATE_LOOKUP_CACHE.clear()
    runs_dir_mod._RUN_STATE_RECENT_INDEX_CACHE.clear()
    runs_dir_mod._RUN_STATE_LEDGER_CACHE.clear()
    root = runs_root()
    run_a = root / "run-ledger-sqlite-invalidate-a"
    run_a.mkdir(parents=True, exist_ok=True)
    state_a = run_a / "state.json"
    state_a.write_text(
        '{"session_id":"LEDGER-SQLITE-INVALIDATE-A","jsonl_path":"/tmp/sqlite-invalidate-a.jsonl"}',
        encoding="utf-8",
    )
    _append_run_state_ledger(
        state_a,
        {"session_id": "LEDGER-SQLITE-INVALIDATE-A", "jsonl_path": "/tmp/sqlite-invalidate-a.jsonl"},
    )
    assert runs_dir_mod.ledger_state_files_for_sid(root, "LEDGER-SQLITE-INVALIDATE-A") == [state_a]
    runs_dir_mod._RUN_STATE_LEDGER_CACHE.clear()
    run_b = root / "run-ledger-sqlite-invalidate-b"
    run_b.mkdir(parents=True, exist_ok=True)
    state_b = run_b / "state.json"
    state_b.write_text(
        '{"session_id":"LEDGER-SQLITE-INVALIDATE-B","jsonl_path":"/tmp/sqlite-invalidate-b.jsonl"}',
        encoding="utf-8",
    )
    _append_run_state_ledger(
        state_b,
        {"session_id": "LEDGER-SQLITE-INVALIDATE-B", "jsonl_path": "/tmp/sqlite-invalidate-b.jsonl"},
    )
    runs_dir_mod._RUN_STATE_LEDGER_CACHE.clear()
    original_json_loads = runs_dir_mod.json.loads

    def fail_jsonl_parse(raw, *args, **kwargs):  # type: ignore[no-untyped-def]
        if '"session_id"' in raw:
            raise AssertionError("append should extend current sqlite cache")
        return original_json_loads(raw, *args, **kwargs)

    runs_dir_mod.json.loads = fail_jsonl_parse  # type: ignore
    try:
        assert runs_dir_mod.ledger_state_files_for_sid(root, "LEDGER-SQLITE-INVALIDATE-B") == [state_b]
    finally:
        runs_dir_mod.json.loads = original_json_loads  # type: ignore
    print("PASS test_run_state_ledger_sqlite_cache_invalidates_on_append")


async def test_run_state_ledger_concurrent_appends_extend_cache_without_lost_rows() -> None:
    from runs_dir import _append_run_state_ledger, runs_root

    nfm_mod._RUN_STATE_LOOKUP_CACHE.clear()
    runs_dir_mod._RUN_STATE_RECENT_INDEX_CACHE.clear()
    runs_dir_mod._RUN_STATE_LEDGER_CACHE.clear()
    root = runs_root()
    warm_dir = root / "run-ledger-concurrent-warm"
    warm_dir.mkdir(parents=True, exist_ok=True)
    warm_state = warm_dir / "state.json"
    warm_state.write_text(
        '{"session_id":"LEDGER-CONCURRENT-WARM","jsonl_path":"/tmp/concurrent-warm.jsonl"}',
        encoding="utf-8",
    )
    _append_run_state_ledger(
        warm_state,
        {"session_id": "LEDGER-CONCURRENT-WARM", "jsonl_path": "/tmp/concurrent-warm.jsonl"},
    )
    assert runs_dir_mod.ledger_state_files_for_sid(root, "LEDGER-CONCURRENT-WARM") == [warm_state]

    errors: list[BaseException] = []

    def append_one(index: int) -> None:
        try:
            sid = f"LEDGER-CONCURRENT-{index}"
            run_dir = root / f"run-ledger-concurrent-{index}"
            run_dir.mkdir(parents=True, exist_ok=True)
            state_path = run_dir / "state.json"
            state_path.write_text(
                f'{{"session_id":"{sid}","jsonl_path":"/tmp/{sid}.jsonl"}}',
                encoding="utf-8",
            )
            _append_run_state_ledger(
                state_path,
                {"session_id": sid, "jsonl_path": f"/tmp/{sid}.jsonl"},
            )
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=append_one, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2.0)
    assert not errors, errors
    assert all(not thread.is_alive() for thread in threads)

    runs_dir_mod._RUN_STATE_LEDGER_CACHE.clear()
    original_json_loads = runs_dir_mod.json.loads

    def fail_jsonl_parse(raw, *args, **kwargs):  # type: ignore[no-untyped-def]
        if '"session_id"' in raw:
            raise AssertionError("concurrent appends should extend sqlite cache")
        return original_json_loads(raw, *args, **kwargs)

    runs_dir_mod.json.loads = fail_jsonl_parse  # type: ignore
    try:
        for index in range(8):
            sid = f"LEDGER-CONCURRENT-{index}"
            expected = root / f"run-ledger-concurrent-{index}" / "state.json"
            assert runs_dir_mod.ledger_state_files_for_sid(root, sid) == [expected]
    finally:
        runs_dir_mod.json.loads = original_json_loads  # type: ignore
    print("PASS test_run_state_ledger_concurrent_appends_extend_cache_without_lost_rows")


async def test_run_index_uses_ledger_cache_without_state_scan() -> None:
    from runs_dir import _append_run_state_ledger, runs_root

    nsm_mod._RUN_INDEX = None
    nsm_mod._RUN_INDEX_MTIME = 0.0
    runs_dir_mod._RUN_STATE_LEDGER_CACHE.clear()
    root = runs_root()
    for index in range(3):
        sid = f"RUN-INDEX-SID-{index}"
        app_sid = f"RUN-INDEX-APP-{index}"
        run_dir = root / f"run-index-ledger-{index}"
        run_dir.mkdir(parents=True, exist_ok=True)
        state_path = run_dir / "state.json"
        data = {
            "session_id": sid,
            "app_session_id": app_sid,
            "jsonl_path": f"/tmp/{sid}.jsonl",
        }
        state_path.write_text(runs_dir_mod.json.dumps(data), encoding="utf-8")
        _append_run_state_ledger(state_path, data)
    assert runs_dir_mod.run_dirs_by_app_session(root)["RUN-INDEX-APP-2"] == root / "run-index-ledger-2"
    nsm_mod._RUN_INDEX = None
    nsm_mod._RUN_INDEX_MTIME = 0.0
    original_read_text = runs_dir_mod.Path.read_text

    def fail_state_read(path, *args, **kwargs):  # type: ignore[no-untyped-def]
        if getattr(path, "name", "") == "state.json":
            raise AssertionError("_run_index should not read state.json when ledger cache is current")
        return original_read_text(path, *args, **kwargs)

    runs_dir_mod.Path.read_text = fail_state_read  # type: ignore
    nsm_mod.Path.read_text = fail_state_read  # type: ignore
    try:
        index = nsm_mod._run_index()
    finally:
        runs_dir_mod.Path.read_text = original_read_text  # type: ignore
        nsm_mod.Path.read_text = original_read_text  # type: ignore
    assert index["RUN-INDEX-APP-0"] == root / "run-index-ledger-0"
    assert index["RUN-INDEX-APP-2"] == root / "run-index-ledger-2"
    print("PASS test_run_index_uses_ledger_cache_without_state_scan")


async def test_run_index_append_extends_ledger_cache_without_state_scan() -> None:
    from runs_dir import _append_run_state_ledger, runs_root

    nsm_mod._RUN_INDEX = None
    nsm_mod._RUN_INDEX_MTIME = 0.0
    runs_dir_mod._RUN_STATE_LEDGER_CACHE.clear()
    root = runs_root()
    run_a = root / "run-index-append-a"
    run_a.mkdir(parents=True, exist_ok=True)
    state_a = run_a / "state.json"
    data_a = {
        "session_id": "RUN-INDEX-APPEND-A-SID",
        "app_session_id": "RUN-INDEX-APPEND-A",
        "jsonl_path": "/tmp/run-index-append-a.jsonl",
    }
    state_a.write_text(runs_dir_mod.json.dumps(data_a), encoding="utf-8")
    _append_run_state_ledger(state_a, data_a)
    assert nsm_mod._run_index()["RUN-INDEX-APPEND-A"] == run_a
    nsm_mod._RUN_INDEX = None
    nsm_mod._RUN_INDEX_MTIME = 0.0
    run_b = root / "run-index-append-b"
    run_b.mkdir(parents=True, exist_ok=True)
    state_b = run_b / "state.json"
    data_b = {
        "session_id": "RUN-INDEX-APPEND-B-SID",
        "app_session_id": "RUN-INDEX-APPEND-B",
        "jsonl_path": "/tmp/run-index-append-b.jsonl",
    }
    state_b.write_text(runs_dir_mod.json.dumps(data_b), encoding="utf-8")
    _append_run_state_ledger(state_b, data_b)
    original_read_text = runs_dir_mod.Path.read_text

    def fail_state_read(path, *args, **kwargs):  # type: ignore[no-untyped-def]
        if getattr(path, "name", "") == "state.json":
            raise AssertionError("append should extend ledger-backed run index")
        return original_read_text(path, *args, **kwargs)

    runs_dir_mod.Path.read_text = fail_state_read  # type: ignore
    nsm_mod.Path.read_text = fail_state_read  # type: ignore
    try:
        index = nsm_mod._run_index()
    finally:
        runs_dir_mod.Path.read_text = original_read_text  # type: ignore
        nsm_mod.Path.read_text = original_read_text  # type: ignore
    assert index["RUN-INDEX-APPEND-A"] == run_a
    assert index["RUN-INDEX-APPEND-B"] == run_b
    print("PASS test_run_index_append_extends_ledger_cache_without_state_scan")


async def test_run_index_cache_rebuild_uses_latest_written_at_for_duplicate_app() -> None:
    from runs_dir import _append_run_state_ledger, run_state_ledger_cache_path, runs_root
    import uuid

    nsm_mod._RUN_INDEX = None
    nsm_mod._RUN_INDEX_MTIME = 0.0
    runs_dir_mod._RUN_STATE_LEDGER_CACHE.clear()
    root = runs_root()
    suffix = uuid.uuid4().hex
    app_sid = f"RUN-INDEX-DUPLICATE-APP-{suffix}"
    older = root / f"run-index-duplicate-older-{suffix}"
    newer = root / f"run-index-duplicate-newer-{suffix}"
    for run_dir, sid in (
        (older, "RUN-INDEX-DUPLICATE-OLDER"),
        (newer, "RUN-INDEX-DUPLICATE-NEWER"),
    ):
        run_dir.mkdir(parents=True, exist_ok=True)
        state_path = run_dir / "state.json"
        data = {
            "session_id": sid,
            "app_session_id": app_sid,
            "jsonl_path": f"/tmp/{sid}.jsonl",
        }
        state_path.write_text(runs_dir_mod.json.dumps(data), encoding="utf-8")
        _append_run_state_ledger(state_path, data)
        time.sleep(0.01)
    run_state_ledger_cache_path(root).unlink()
    runs_dir_mod._RUN_STATE_LEDGER_CACHE.clear()
    index = runs_dir_mod.run_dirs_by_app_session(root)
    assert index[app_sid] == newer
    nsm_mod._RUN_INDEX = None
    nsm_mod._RUN_INDEX_MTIME = 0.0
    assert nsm_mod._run_index()[app_sid] == newer
    print("PASS test_run_index_cache_rebuild_uses_latest_written_at_for_duplicate_app")


async def test_run_index_app_backfill_orders_duplicate_app_by_state_mtime() -> None:
    from runs_dir import run_state_ledger_cache_path, runs_root
    import uuid

    nsm_mod._RUN_INDEX = None
    nsm_mod._RUN_INDEX_MTIME = 0.0
    runs_dir_mod._RUN_STATE_LEDGER_CACHE.clear()
    root = runs_root()
    suffix = uuid.uuid4().hex
    app_sid = f"RUN-INDEX-BACKFILL-APP-{suffix}"
    older = root / f"zz-run-index-backfill-older-{suffix}"
    newer = root / f"aa-run-index-backfill-newer-{suffix}"
    for run_dir, sid in (
        (older, "RUN-INDEX-BACKFILL-OLDER"),
        (newer, "RUN-INDEX-BACKFILL-NEWER"),
    ):
        run_dir.mkdir(parents=True, exist_ok=True)
        state_path = run_dir / "state.json"
        data = {
            "session_id": sid,
            "app_session_id": app_sid,
            "jsonl_path": f"/tmp/{sid}.jsonl",
        }
        state_path.write_text(runs_dir_mod.json.dumps(data), encoding="utf-8")
    old_ts = 1_700_000_000
    new_ts = old_ts + 100
    os.utime(older / "state.json", (old_ts, old_ts))
    os.utime(newer / "state.json", (new_ts, new_ts))
    try:
        run_state_ledger_cache_path(root).unlink()
    except OSError:
        pass
    runs_dir_mod._RUN_STATE_LEDGER_CACHE.clear()
    marker = runs_dir_mod.run_state_app_index_backfill_marker_path(root)
    try:
        marker.unlink()
    except OSError:
        pass
    index = runs_dir_mod.run_dirs_by_app_session(root)
    assert index[app_sid] == newer
    print("PASS test_run_index_app_backfill_orders_duplicate_app_by_state_mtime")


async def test_run_state_ledger_sqlite_cache_corrupt_and_wrong_version_fallback() -> None:
    from runs_dir import _append_run_state_ledger, run_state_ledger_cache_path, runs_root

    nfm_mod._RUN_STATE_LOOKUP_CACHE.clear()
    runs_dir_mod._RUN_STATE_RECENT_INDEX_CACHE.clear()
    runs_dir_mod._RUN_STATE_LEDGER_CACHE.clear()
    root = runs_root()
    run_dir = root / "run-ledger-sqlite-corrupt"
    run_dir.mkdir(parents=True, exist_ok=True)
    state_path = run_dir / "state.json"
    state_path.write_text(
        '{"session_id":"LEDGER-SQLITE-CORRUPT","jsonl_path":"/tmp/ledger-sqlite-corrupt.jsonl"}',
        encoding="utf-8",
    )
    _append_run_state_ledger(
        state_path,
        {"session_id": "LEDGER-SQLITE-CORRUPT", "jsonl_path": "/tmp/ledger-sqlite-corrupt.jsonl"},
    )
    cache_path = run_state_ledger_cache_path(root)
    cache_path.write_text("not sqlite", encoding="utf-8")
    assert runs_dir_mod.ledger_state_files_for_sid(root, "LEDGER-SQLITE-CORRUPT") == [state_path]
    runs_dir_mod._RUN_STATE_LEDGER_CACHE.clear()
    with runs_dir_mod._sqlite_connect(cache_path) as conn:
        conn.execute("UPDATE meta SET value=-1 WHERE key='version'")
        conn.commit()
    assert runs_dir_mod.ledger_state_files_for_sid(root, "LEDGER-SQLITE-CORRUPT") == [state_path]
    print("PASS test_run_state_ledger_sqlite_cache_corrupt_and_wrong_version_fallback")


async def test_run_state_ledger_sqlite_cache_rejects_poisoned_paths() -> None:
    from runs_dir import _append_run_state_ledger, run_state_ledger_cache_path, runs_root

    nfm_mod._RUN_STATE_LOOKUP_CACHE.clear()
    runs_dir_mod._RUN_STATE_RECENT_INDEX_CACHE.clear()
    runs_dir_mod._RUN_STATE_LEDGER_CACHE.clear()
    root = runs_root()
    run_dir = root / "run-ledger-sqlite-poison"
    run_dir.mkdir(parents=True, exist_ok=True)
    state_path = run_dir / "state.json"
    state_path.write_text(
        '{"session_id":"LEDGER-SQLITE-POISON","jsonl_path":"/tmp/ledger-sqlite-poison.jsonl"}',
        encoding="utf-8",
    )
    _append_run_state_ledger(
        state_path,
        {"session_id": "LEDGER-SQLITE-POISON", "jsonl_path": "/tmp/ledger-sqlite-poison.jsonl"},
    )
    assert runs_dir_mod.ledger_state_files_for_sid(root, "LEDGER-SQLITE-POISON") == [state_path]
    poisoned_paths = [
        str(root / "run-ledger-sqlite-poison" / "nested" / "state.json"),
        str(root.parent / "outside-ledger-sqlite-poison" / "state.json"),
    ]
    for poisoned_path in poisoned_paths:
        runs_dir_mod._RUN_STATE_LEDGER_CACHE.clear()
        with runs_dir_mod._sqlite_connect(run_state_ledger_cache_path(root)) as conn:
            conn.execute("DELETE FROM entries")
            conn.execute(
                "INSERT INTO entries (sid, state_path, written_at) VALUES (?, ?, ?)",
                ("LEDGER-SQLITE-POISON", poisoned_path, 1.0),
            )
            conn.commit()
        assert runs_dir_mod.ledger_state_files_for_sid(root, "LEDGER-SQLITE-POISON") == [state_path]
    print("PASS test_run_state_ledger_sqlite_cache_rejects_poisoned_paths")


async def test_run_state_ledger_sqlite_cache_dedupes_duplicate_state_paths() -> None:
    from runs_dir import run_state_ledger_path, runs_root

    nfm_mod._RUN_STATE_LOOKUP_CACHE.clear()
    runs_dir_mod._RUN_STATE_RECENT_INDEX_CACHE.clear()
    runs_dir_mod._RUN_STATE_LEDGER_CACHE.clear()
    root = runs_root()
    state_path = root / "run-ledger-sqlite-dedupe" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        '{"session_id":"LEDGER-SQLITE-DEDUPE","jsonl_path":"/tmp/ledger-sqlite-dedupe-new.jsonl"}',
        encoding="utf-8",
    )
    ledger = run_state_ledger_path(root)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(
        "\n".join([
            runs_dir_mod.json.dumps({
                "session_id": "LEDGER-SQLITE-DEDUPE",
                "jsonl_path": "/tmp/ledger-sqlite-dedupe-old.jsonl",
                "state_path": str(state_path),
                "written_at": 1.0,
            }),
            runs_dir_mod.json.dumps({
                "session_id": "LEDGER-SQLITE-DEDUPE",
                "jsonl_path": "/tmp/ledger-sqlite-dedupe-new.jsonl",
                "state_path": str(state_path),
                "written_at": 2.0,
            }),
        ]) + "\n",
        encoding="utf-8",
    )
    assert runs_dir_mod.ledger_state_files_for_sid(root, "LEDGER-SQLITE-DEDUPE") == [state_path]
    runs_dir_mod._RUN_STATE_LEDGER_CACHE.clear()
    with runs_dir_mod._sqlite_connect(runs_dir_mod.run_state_ledger_cache_path(root)) as conn:
        rows = conn.execute(
            "SELECT written_at FROM entries WHERE sid=? AND state_path=?",
            ("LEDGER-SQLITE-DEDUPE", str(state_path)),
        ).fetchall()
    assert rows == [(2.0,)], rows
    assert runs_dir_mod.ledger_state_files_for_sid(root, "LEDGER-SQLITE-DEDUPE") == [state_path]
    print("PASS test_run_state_ledger_sqlite_cache_dedupes_duplicate_state_paths")


async def test_run_state_ledger_sqlite_cache_rebuild_singleflight_waits() -> None:
    from runs_dir import _append_run_state_ledger, run_state_ledger_cache_path, runs_root
    import threading
    import time

    nfm_mod._RUN_STATE_LOOKUP_CACHE.clear()
    runs_dir_mod._RUN_STATE_RECENT_INDEX_CACHE.clear()
    runs_dir_mod._RUN_STATE_LEDGER_CACHE.clear()
    runs_dir_mod._RUN_STATE_LEDGER_CACHE_REBUILD_INFLIGHT.clear()
    root = runs_root()
    for index in range(6):
        sid = f"LEDGER-SQLITE-SINGLEFLIGHT-{index}"
        run_dir = root / f"run-ledger-sqlite-singleflight-{index}"
        run_dir.mkdir(parents=True, exist_ok=True)
        state_path = run_dir / "state.json"
        state_path.write_text(
            f'{{"session_id":"{sid}","jsonl_path":"/tmp/{sid}.jsonl"}}',
            encoding="utf-8",
        )
        _append_run_state_ledger(
            state_path,
            {"session_id": sid, "jsonl_path": f"/tmp/{sid}.jsonl"},
        )
    try:
        run_state_ledger_cache_path(root).unlink()
    except OSError:
        pass
    original_open = runs_dir_mod.Path.open
    parse_count = 0
    parse_lock = threading.Lock()

    def slow_ledger_open(path, *args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal parse_count
        if path == runs_dir_mod.run_state_ledger_path(root):
            with parse_lock:
                parse_count += 1
            time.sleep(1.2)
        return original_open(path, *args, **kwargs)

    runs_dir_mod.Path.open = slow_ledger_open  # type: ignore
    try:
        results = await asyncio.gather(*[
            asyncio.to_thread(
                runs_dir_mod.ledger_state_files_for_sid,
                root,
                f"LEDGER-SQLITE-SINGLEFLIGHT-{index}",
            )
            for index in range(6)
        ])
    finally:
        runs_dir_mod.Path.open = original_open  # type: ignore
    assert parse_count == 1, f"expected one JSONL rebuild, got {parse_count}"
    assert all(len(result) == 1 for result in results), results
    print("PASS test_run_state_ledger_sqlite_cache_rebuild_singleflight_waits")


async def test_run_state_ledger_sqlite_cache_rebuild_signature_change_does_not_deadlock() -> None:
    from runs_dir import _append_run_state_ledger, runs_root
    import threading

    nfm_mod._RUN_STATE_LOOKUP_CACHE.clear()
    runs_dir_mod._RUN_STATE_RECENT_INDEX_CACHE.clear()
    runs_dir_mod._RUN_STATE_LEDGER_CACHE.clear()
    runs_dir_mod._RUN_STATE_LEDGER_CACHE_REBUILD_INFLIGHT.clear()
    root = runs_root()
    run_dir = root / "run-ledger-sqlite-signature-change"
    run_dir.mkdir(parents=True, exist_ok=True)
    state_path = run_dir / "state.json"
    state_path.write_text(
        '{"session_id":"LEDGER-SQLITE-SIGNATURE","jsonl_path":"/tmp/ledger-sqlite-signature.jsonl"}',
        encoding="utf-8",
    )
    _append_run_state_ledger(
        state_path,
        {"session_id": "LEDGER-SQLITE-SIGNATURE", "jsonl_path": "/tmp/ledger-sqlite-signature.jsonl"},
    )
    signature = runs_dir_mod._run_state_ledger_signature(runs_dir_mod.run_state_ledger_path(root))
    assert signature is not None
    calls = 0
    original_signature = runs_dir_mod._run_state_ledger_signature

    def changing_signature(path):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        if calls == 2:
            return (signature[0], signature[1], signature[2] + 1, signature[3], signature[4])
        return original_signature(path)

    result: list[list[nfm_mod.Path]] = []
    error: list[BaseException] = []

    def lookup() -> None:
        try:
            result.append(runs_dir_mod.ledger_state_files_for_sid(root, "LEDGER-SQLITE-SIGNATURE"))
        except BaseException as exc:
            error.append(exc)

    runs_dir_mod._run_state_ledger_signature = changing_signature  # type: ignore
    try:
        thread = threading.Thread(target=lookup)
        thread.start()
        thread.join(timeout=2.0)
    finally:
        runs_dir_mod._run_state_ledger_signature = original_signature  # type: ignore
    assert not thread.is_alive(), "signature-change retry deadlocked on its own rebuild event"
    assert not error, error
    assert result == [[state_path]], result
    print("PASS test_run_state_ledger_sqlite_cache_rebuild_signature_change_does_not_deadlock")


async def test_run_state_ledger_string_shape_is_strict() -> None:
    from runs_dir import runs_root

    root = runs_root()
    assert runs_dir_mod._run_state_path_string_has_ledger_shape(
        str(root / "run-direct" / "state.json"),
        root,
    )
    rejected = [
        str(root / "run-direct" / "nested" / "state.json"),
        str(root / "state.json"),
        str(root.parent / f"{root.name}2" / "run-direct" / "state.json"),
        str(root.parent / "outside" / "state.json"),
        "run-direct/state.json",
        str(root / "run-direct" / "other.json"),
    ]
    for state_path in rejected:
        assert not runs_dir_mod._run_state_path_string_has_ledger_shape(state_path, root), state_path
    print("PASS test_run_state_ledger_string_shape_is_strict")


async def test_run_state_ledger_parse_defers_resolve_to_target_sid() -> None:
    from runs_dir import run_state_ledger_path, runs_root

    nfm_mod._RUN_STATE_LOOKUP_CACHE.clear()
    runs_dir_mod._RUN_STATE_RECENT_INDEX_CACHE.clear()
    runs_dir_mod._RUN_STATE_LEDGER_CACHE.clear()
    root = runs_root()
    target_dir = root / "run-ledger-target"
    target_dir.mkdir(parents=True, exist_ok=True)
    target_state = target_dir / "state.json"
    target_state.write_text(
        '{"session_id":"LEDGER-TARGET","jsonl_path":"/tmp/ledger-target.jsonl"}',
        encoding="utf-8",
    )
    rows = []
    for index in range(50):
        rows.append(
            {
                "session_id": f"LEDGER-OTHER-{index}",
                "jsonl_path": f"/tmp/ledger-other-{index}.jsonl",
                "state_path": str(root / f"run-ledger-other-{index}" / "state.json"),
                "written_at": index,
            }
        )
    rows.extend(
        [
            {
                "session_id": "LEDGER-TARGET",
                "jsonl_path": "/tmp/ledger-target.jsonl",
                "state_path": str(target_state),
                "written_at": 100,
            },
            {
                "session_id": "LEDGER-BAD",
                "jsonl_path": "/tmp/ledger-bad-outside.jsonl",
                "state_path": str(root.parent / "outside-ledger-bad" / "state.json"),
                "written_at": 101,
            },
            {
                "session_id": "LEDGER-BAD",
                "jsonl_path": "/tmp/ledger-bad-nested.jsonl",
                "state_path": str(root / "run-ledger-bad" / "nested" / "state.json"),
                "written_at": 102,
            },
            {
                "session_id": "LEDGER-BAD",
                "jsonl_path": "/tmp/ledger-bad-relative.jsonl",
                "state_path": "run-ledger-relative/state.json",
                "written_at": 103,
            },
        ]
    )
    run_state_ledger_path(root).write_text(
        "\n".join(nfm_mod.json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    original_validator = runs_dir_mod._run_state_path_under_root
    original_shape = runs_dir_mod._run_state_path_has_ledger_shape
    validated_paths: list[nfm_mod.Path] = []

    def counting_validator(path, root_resolved):  # type: ignore[no-untyped-def]
        validated_paths.append(path)
        return original_validator(path, root_resolved)

    def fail_shape(*_args, **_kwargs):
        raise AssertionError("ledger parse should use string shape before Path construction")

    runs_dir_mod._run_state_path_under_root = counting_validator  # type: ignore
    if runs_dir_mod.os.sep == "/" and runs_dir_mod.os.altsep is None:
        runs_dir_mod._run_state_path_has_ledger_shape = fail_shape  # type: ignore
    try:
        assert runs_dir_mod.ledger_state_files_for_sid(root, "LEDGER-TARGET") == [
            target_state
        ]
        assert validated_paths == [target_state]
        validated_paths.clear()
        assert runs_dir_mod.ledger_state_files_for_sid(root, "LEDGER-TARGET") == [
            target_state
        ]
        assert validated_paths == [target_state]
        validated_paths.clear()
        assert runs_dir_mod.ledger_state_files_for_sid(root, "LEDGER-BAD") == []
        assert validated_paths == []
    finally:
        runs_dir_mod._run_state_path_under_root = original_validator  # type: ignore
        runs_dir_mod._run_state_path_has_ledger_shape = original_shape  # type: ignore
    print("PASS test_run_state_ledger_parse_defers_resolve_to_target_sid")


async def test_run_state_ledger_parse_defers_path_construction_to_target_sid() -> None:
    from runs_dir import run_state_ledger_path, runs_root

    nfm_mod._RUN_STATE_LOOKUP_CACHE.clear()
    runs_dir_mod._RUN_STATE_RECENT_INDEX_CACHE.clear()
    runs_dir_mod._RUN_STATE_LEDGER_CACHE.clear()
    root = runs_root()
    rows = []
    for index in range(50):
        rows.append(
            {
                "session_id": f"LEDGER-PATH-OTHER-{index}",
                "jsonl_path": f"/tmp/ledger-path-other-{index}.jsonl",
                "state_path": str(root / f"run-ledger-path-other-{index}" / "state.json"),
                "written_at": index,
            }
        )
    target_state = root / "run-ledger-path-target" / "state.json"
    rows.append(
        {
            "session_id": "LEDGER-PATH-TARGET",
            "jsonl_path": "/tmp/ledger-path-target.jsonl",
            "state_path": str(target_state),
            "written_at": 100,
        }
    )
    run_state_ledger_path(root).write_text(
        "\n".join(nfm_mod.json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    original_path = runs_dir_mod.Path
    path_calls: list[str] = []

    def counting_path(value):
        path_calls.append(str(value))
        return original_path(value)

    if runs_dir_mod.os.sep != "/" or runs_dir_mod.os.altsep is not None:
        return
    runs_dir_mod.Path = counting_path  # type: ignore
    try:
        assert runs_dir_mod.ledger_state_files_for_sid(root, "LEDGER-PATH-MISSING") == []
        assert path_calls == []
        assert runs_dir_mod.ledger_state_files_for_sid(root, "LEDGER-PATH-TARGET") == [target_state]
        assert path_calls == [str(target_state)]
    finally:
        runs_dir_mod.Path = original_path  # type: ignore
    print("PASS test_run_state_ledger_parse_defers_path_construction_to_target_sid")


async def test_run_state_recent_cache_revalidates_cached_paths() -> None:
    from runs_dir import runs_root

    nfm_mod._RUN_STATE_LOOKUP_CACHE.clear()
    runs_dir_mod._RUN_STATE_RECENT_INDEX_CACHE.clear()
    runs_dir_mod._RUN_STATE_LEDGER_CACHE.clear()
    root = runs_root()
    run_dir = root / "run-recent-cache-escape"
    run_dir.mkdir(parents=True, exist_ok=True)
    state_path = run_dir / "state.json"
    state_path.write_text(
        '{"session_id":"RECENT-CACHE-ESCAPE","jsonl_path":"/tmp/recent-cache-escape.jsonl"}',
        encoding="utf-8",
    )
    assert runs_dir_mod.recent_state_index_for_root(root)["RECENT-CACHE-ESCAPE"] == [
        state_path
    ]
    outside_dir = root.parent / "outside-recent-cache-escape"
    outside_dir.mkdir(parents=True, exist_ok=True)
    state_path.unlink()
    run_dir.rmdir()
    run_dir.symlink_to(outside_dir, target_is_directory=True)
    assert runs_dir_mod.recent_state_index_for_root(root).get("RECENT-CACHE-ESCAPE") is None
    print("PASS test_run_state_recent_cache_revalidates_cached_paths")


async def test_run_state_recent_candidates_skip_per_entry_resolve() -> None:
    from runs_dir import runs_root

    nfm_mod._RUN_STATE_LOOKUP_CACHE.clear()
    runs_dir_mod._RUN_STATE_RECENT_INDEX_CACHE.clear()
    root = runs_root()
    for index in range(runs_dir_mod._RUN_STATE_RECENT_SCAN_LIMIT + 8):
        run_dir = root / f"run-recent-no-resolve-{index}"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "state.json").write_text(
            f'{{"session_id":"RECENT-NO-RESOLVE-{index}","jsonl_path":"/tmp/no-resolve-{index}.jsonl"}}',
            encoding="utf-8",
        )
    original_validator = runs_dir_mod._run_state_path_under_root

    def fail_validator(*_args, **_kwargs):
        raise AssertionError("recent candidate scan should not resolve every entry")

    runs_dir_mod._run_state_path_under_root = fail_validator  # type: ignore
    try:
        candidates = runs_dir_mod._recent_state_candidates(root, root.resolve())
    finally:
        runs_dir_mod._run_state_path_under_root = original_validator  # type: ignore
    assert len(candidates) == runs_dir_mod._RUN_STATE_RECENT_SCAN_LIMIT
    print("PASS test_run_state_recent_candidates_skip_per_entry_resolve")


async def test_run_state_recent_scan_skips_per_entry_path_helper() -> None:
    nfm_mod._RUN_STATE_LOOKUP_CACHE.clear()
    runs_dir_mod._RUN_STATE_RECENT_INDEX_CACHE.clear()
    runs_dir_mod._RUN_STATE_LEDGER_CACHE.clear()
    root = nfm_mod.Path(tempfile.mkdtemp(prefix="nfm-no-helper-"))
    for index in range(4):
        run_dir = root / f"run-recent-no-helper-{index}"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "state.json").write_text(
            f'{{"session_id":"RECENT-NO-HELPER-{index}","jsonl_path":"/tmp/no-helper-{index}.jsonl"}}',
            encoding="utf-8",
        )
    original_candidate_stat = runs_dir_mod._run_state_candidate_stat

    def fail_candidate_stat(*_args, **_kwargs):
        raise AssertionError("recent cold scan should not call the Path-heavy candidate helper per entry")

    runs_dir_mod._run_state_candidate_stat = fail_candidate_stat  # type: ignore
    try:
        candidates = runs_dir_mod._recent_state_candidates(root, root.resolve())
    finally:
        runs_dir_mod._run_state_candidate_stat = original_candidate_stat  # type: ignore
        shutil.rmtree(root, ignore_errors=True)
    assert {candidate[2] for candidate in candidates} == {
        str(root / f"run-recent-no-helper-{index}" / "state.json")
        for index in range(4)
    }
    print("PASS test_run_state_recent_scan_skips_per_entry_path_helper")


async def test_run_state_recent_lookup_resolves_only_candidates() -> None:
    from runs_dir import runs_root

    nfm_mod._RUN_STATE_LOOKUP_CACHE.clear()
    runs_dir_mod._RUN_STATE_RECENT_INDEX_CACHE.clear()
    runs_dir_mod._RUN_STATE_LEDGER_CACHE.clear()
    root = runs_root()
    _make_run_state_backfill_marker_stale(root)
    target_sid = "RECENT-VALIDATE-TARGET"
    for index in range(runs_dir_mod._RUN_STATE_RECENT_SCAN_LIMIT + 12):
        sid = target_sid if index == runs_dir_mod._RUN_STATE_RECENT_SCAN_LIMIT + 11 else f"RECENT-VALIDATE-{index}"
        run_dir = root / f"run-recent-validate-{index}"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "state.json").write_text(
            f'{{"session_id":"{sid}","jsonl_path":"/tmp/recent-validate-{index}.jsonl"}}',
            encoding="utf-8",
        )
    original_validator = runs_dir_mod._run_state_path_under_root
    validated_paths: list[nfm_mod.Path] = []

    def counting_validator(path, root_resolved):  # type: ignore[no-untyped-def]
        validated_paths.append(path)
        return original_validator(path, root_resolved)

    runs_dir_mod._run_state_path_under_root = counting_validator  # type: ignore
    try:
        path = nfm_mod._scan_run_state_for_jsonl(target_sid)
    finally:
        runs_dir_mod._run_state_path_under_root = original_validator  # type: ignore
    assert str(path).startswith("/tmp/recent-validate-"), path
    assert len(validated_paths) <= runs_dir_mod._RUN_STATE_RECENT_SCAN_LIMIT + 2, len(validated_paths)
    print("PASS test_run_state_recent_lookup_resolves_only_candidates")


async def test_run_state_recent_candidates_skip_run_dir_symlink() -> None:
    from runs_dir import runs_root

    nfm_mod._RUN_STATE_LOOKUP_CACHE.clear()
    runs_dir_mod._RUN_STATE_RECENT_INDEX_CACHE.clear()
    runs_dir_mod._RUN_STATE_LEDGER_CACHE.clear()
    root = runs_root()
    _make_run_state_backfill_marker_stale(root)
    outside_dir = root.parent / "outside-recent-run-dir-symlink"
    outside_dir.mkdir(parents=True, exist_ok=True)
    (outside_dir / "state.json").write_text(
        '{"session_id":"RECENT-RUN-DIR-SYMLINK","jsonl_path":"/tmp/recent-run-dir-symlink.jsonl"}',
        encoding="utf-8",
    )
    run_dir = root / "run-recent-dir-symlink"
    run_dir.symlink_to(outside_dir, target_is_directory=True)
    candidates = runs_dir_mod._recent_state_candidates(root, root.resolve())
    assert str(run_dir / "state.json") not in {candidate[2] for candidate in candidates}
    assert nfm_mod._scan_run_state_for_jsonl("RECENT-RUN-DIR-SYMLINK") is None
    print("PASS test_run_state_recent_candidates_skip_run_dir_symlink")


async def test_run_state_recent_candidates_skip_state_symlink() -> None:
    from runs_dir import runs_root

    nfm_mod._RUN_STATE_LOOKUP_CACHE.clear()
    runs_dir_mod._RUN_STATE_RECENT_INDEX_CACHE.clear()
    runs_dir_mod._RUN_STATE_LEDGER_CACHE.clear()
    root = runs_root()
    _make_run_state_backfill_marker_stale(root)
    outside_dir = root.parent / "outside-recent-state-symlink"
    outside_dir.mkdir(parents=True, exist_ok=True)
    outside_state = outside_dir / "state.json"
    outside_state.write_text(
        '{"session_id":"RECENT-STATE-SYMLINK","jsonl_path":"/tmp/recent-state-symlink.jsonl"}',
        encoding="utf-8",
    )
    run_dir = root / "run-recent-state-symlink"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "state.json").symlink_to(outside_state)
    candidates = runs_dir_mod._recent_state_candidates(root, root.resolve())
    assert str(run_dir / "state.json") not in {candidate[2] for candidate in candidates}
    assert nfm_mod._scan_run_state_for_jsonl("RECENT-STATE-SYMLINK") is None
    print("PASS test_run_state_recent_candidates_skip_state_symlink")


async def test_run_state_recent_build_rejects_swapped_symlink() -> None:
    from runs_dir import runs_root

    nfm_mod._RUN_STATE_LOOKUP_CACHE.clear()
    runs_dir_mod._RUN_STATE_RECENT_INDEX_CACHE.clear()
    runs_dir_mod._RUN_STATE_LEDGER_CACHE.clear()
    root = runs_root()
    run_dir = root / "run-recent-swapped"
    run_dir.mkdir(parents=True, exist_ok=True)
    state_path = run_dir / "state.json"
    state_path.write_text(
        '{"session_id":"RECENT-SWAPPED","jsonl_path":"/tmp/recent-swapped.jsonl"}',
        encoding="utf-8",
    )
    outside_dir = root.parent / "outside-recent-swapped"
    outside_dir.mkdir(parents=True, exist_ok=True)
    (outside_dir / "state.json").write_text(
        '{"session_id":"RECENT-SWAPPED","jsonl_path":"/tmp/recent-swapped-escape.jsonl"}',
        encoding="utf-8",
    )
    candidates = runs_dir_mod._recent_state_candidates(root, root.resolve())
    state_path.unlink()
    run_dir.rmdir()
    run_dir.symlink_to(outside_dir, target_is_directory=True)
    assert runs_dir_mod._build_recent_state_index(candidates, root.resolve()).get("RECENT-SWAPPED") is None
    print("PASS test_run_state_recent_build_rejects_swapped_symlink")


async def test_run_state_recent_index_is_reused_across_sids() -> None:
    from runs_dir import runs_root

    nfm_mod._RUN_STATE_LOOKUP_CACHE.clear()
    runs_dir_mod._RUN_STATE_RECENT_INDEX_CACHE.clear()
    root = runs_root()
    _make_run_state_backfill_marker_stale(root)
    for name, sid in (("run-reuse-a", "REUSE-A-SID"), ("run-reuse-b", "REUSE-B-SID")):
        run_dir = root / name
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "state.json").write_text(
            f'{{"session_id":"{sid}","jsonl_path":"/tmp/{sid}.jsonl"}}',
            encoding="utf-8",
        )
    calls = 0
    original_build = runs_dir_mod._build_recent_state_index
    scan_calls = 0
    original_scan = runs_dir_mod._recent_state_scan

    def counted_build(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original_build(*args, **kwargs)

    def counted_scan(*args, **kwargs):
        nonlocal scan_calls
        scan_calls += 1
        return original_scan(*args, **kwargs)

    runs_dir_mod._build_recent_state_index = counted_build  # type: ignore
    runs_dir_mod._recent_state_scan = counted_scan  # type: ignore
    try:
        assert str(nfm_mod._scan_run_state_for_jsonl("REUSE-A-SID")) == "/tmp/REUSE-A-SID.jsonl"
        assert str(nfm_mod._scan_run_state_for_jsonl("REUSE-B-SID")) == "/tmp/REUSE-B-SID.jsonl"
    finally:
        runs_dir_mod._build_recent_state_index = original_build  # type: ignore
        runs_dir_mod._recent_state_scan = original_scan  # type: ignore
    assert calls == 1, f"expected one recent state index build, got {calls}"
    assert scan_calls == 1, f"expected one recent state scan, got {scan_calls}"
    print("PASS test_run_state_recent_index_is_reused_across_sids")


async def test_run_state_lookup_coalesces_concurrent_scans() -> None:
    from runs_dir import runs_root
    import threading
    import time

    nfm_mod._RUN_STATE_LOOKUP_CACHE.clear()
    nfm_mod._RUN_STATE_INFLIGHT.clear()
    runs_dir_mod._RUN_STATE_RECENT_INDEX_CACHE.clear()
    root = runs_root()
    _make_run_state_backfill_marker_stale(root)
    run_dir = root / "run-coalesced"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "state.json").write_text(
        '{"session_id":"COALESCED-SID","jsonl_path":"/tmp/coalesced.jsonl"}',
        encoding="utf-8",
    )
    original_state_files = nfm_mod._state_files_for_sid
    calls = 0
    calls_lock = threading.Lock()

    def slow_state_files(*args, **kwargs):
        nonlocal calls
        with calls_lock:
            calls += 1
        time.sleep(0.1)
        return original_state_files(*args, **kwargs)

    nfm_mod._state_files_for_sid = slow_state_files  # type: ignore
    try:
        results = await asyncio.gather(*[
            asyncio.to_thread(nfm_mod._scan_run_state_for_jsonl, "COALESCED-SID")
            for _ in range(8)
        ])
    finally:
        nfm_mod._state_files_for_sid = original_state_files  # type: ignore
    assert {str(path) for path in results} == {"/tmp/coalesced.jsonl"}
    assert calls == 1, f"expected one scan, got {calls}"
    print("PASS test_run_state_lookup_coalesces_concurrent_scans")


async def test_run_state_recent_index_coalesces_concurrent_sid_scans() -> None:
    from runs_dir import runs_root
    import threading
    import time

    nfm_mod._RUN_STATE_LOOKUP_CACHE.clear()
    nfm_mod._RUN_STATE_INFLIGHT.clear()
    runs_dir_mod._RUN_STATE_RECENT_INDEX_CACHE.clear()
    runs_dir_mod._RUN_STATE_RECENT_INDEX_INFLIGHT.clear()
    root = runs_root()
    _make_run_state_backfill_marker_stale(root)
    for i in range(8):
        sid = f"ROOT-COALESCED-{i}"
        run_dir = root / f"run-root-coalesced-{i}"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "state.json").write_text(
            f'{{"session_id":"{sid}","jsonl_path":"/tmp/{sid}.jsonl"}}',
            encoding="utf-8",
        )
    original_scan = runs_dir_mod._recent_state_scan
    calls = 0
    calls_lock = threading.Lock()

    def slow_scan(*args, **kwargs):
        nonlocal calls
        with calls_lock:
            calls += 1
        time.sleep(0.1)
        return original_scan(*args, **kwargs)

    runs_dir_mod._recent_state_scan = slow_scan  # type: ignore
    try:
        results = await asyncio.gather(*[
            asyncio.to_thread(nfm_mod._scan_run_state_for_jsonl, f"ROOT-COALESCED-{i}")
            for i in range(8)
        ])
    finally:
        runs_dir_mod._recent_state_scan = original_scan  # type: ignore
    assert {str(path) for path in results} == {
        f"/tmp/ROOT-COALESCED-{i}.jsonl"
        for i in range(8)
    }
    assert calls == 1, f"expected one root index scan, got {calls}"
    print("PASS test_run_state_recent_index_coalesces_concurrent_sid_scans")


async def test_run_state_lookup_checks_recent_dirs_first() -> None:
    from runs_dir import runs_root

    nfm_mod._RUN_STATE_LOOKUP_CACHE.clear()
    runs_dir_mod._RUN_STATE_RECENT_INDEX_CACHE.clear()
    root = runs_root()
    _make_run_state_backfill_marker_stale(root)
    agent_sid = "RECENT-FIRST-SID"
    for i in range(runs_dir_mod._RUN_STATE_RECENT_SCAN_LIMIT + 10):
        run_dir = root / f"old-run-{i}"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "state.json").write_text(
            '{"session_id":"old","jsonl_path":"/tmp/old.jsonl"}',
            encoding="utf-8",
        )
    run_dir = root / "recent-run"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "state.json").write_text(
        '{"session_id":"RECENT-FIRST-SID","jsonl_path":"/tmp/recent-first.jsonl"}',
        encoding="utf-8",
    )
    path = nfm_mod._scan_run_state_for_jsonl(agent_sid)
    assert str(path) == "/tmp/recent-first.jsonl", path
    print("PASS test_run_state_lookup_checks_recent_dirs_first")


async def test_run_state_recent_unledgered_state_is_found_without_backfill() -> None:
    from runs_dir import runs_root

    nfm_mod._RUN_STATE_LOOKUP_CACHE.clear()
    runs_dir_mod._RUN_STATE_RECENT_INDEX_CACHE.clear()
    runs_dir_mod._RUN_STATE_LEDGER_CACHE.clear()
    root = runs_root()
    _make_run_state_backfill_marker_stale(root)
    run_dir = root / "run-recent-unledgered"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "state.json").write_text(
        '{"session_id":"RECENT-UNLEDGERED","jsonl_path":"/tmp/recent-unledgered.jsonl"}',
        encoding="utf-8",
    )
    original_backfill = runs_dir_mod.ensure_run_state_ledger_backfilled

    def fail_backfill(*_args, **_kwargs):
        raise AssertionError("recent crash-window lookup should not run full backfill")

    runs_dir_mod.ensure_run_state_ledger_backfilled = fail_backfill  # type: ignore
    try:
        path = nfm_mod._scan_run_state_for_jsonl("RECENT-UNLEDGERED")
    finally:
        runs_dir_mod.ensure_run_state_ledger_backfilled = original_backfill  # type: ignore
    assert str(path) == "/tmp/recent-unledgered.jsonl", path
    print("PASS test_run_state_recent_unledgered_state_is_found_without_backfill")


async def test_run_state_current_backfill_marker_skips_recent_miss_scan() -> None:
    from runs_dir import (
        _RUN_STATE_LEDGER_BACKFILL_VERSION,
        run_state_ledger_backfill_marker_path,
        run_state_ledger_path,
        runs_root,
    )

    nfm_mod._RUN_STATE_LOOKUP_CACHE.clear()
    runs_dir_mod._RUN_STATE_LEDGER_CACHE.clear()
    runs_dir_mod._RUN_STATE_RECENT_INDEX_CACHE.clear()
    root = runs_root()
    for i in range(runs_dir_mod._RUN_STATE_RECENT_SCAN_LIMIT + 10):
        run_dir = root / f"authoritative-miss-run-{i}"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "state.json").write_text(
            '{"session_id":"OTHER-SID","jsonl_path":"/tmp/other.jsonl"}',
            encoding="utf-8",
        )
    run_state_ledger_path(root).write_text("", encoding="utf-8")
    run_state_ledger_backfill_marker_path(root).write_text(
        f'{{"version":{_RUN_STATE_LEDGER_BACKFILL_VERSION},"backfilled_at":1}}',
        encoding="utf-8",
    )
    original_recent = runs_dir_mod._recent_state_index_for_root

    def fail_recent(*_args, **_kwargs):
        raise AssertionError("current backfilled ledger miss should not scan recent run dirs")

    runs_dir_mod._recent_state_index_for_root = fail_recent  # type: ignore
    try:
        assert runs_dir_mod.state_files_for_sid(root, "MISSING-AUTHORITATIVE") == []
        assert nfm_mod._scan_run_state_for_jsonl("MISSING-AUTHORITATIVE") is None
    finally:
        runs_dir_mod._recent_state_index_for_root = original_recent  # type: ignore
    print("PASS test_run_state_current_backfill_marker_skips_recent_miss_scan")


async def test_run_state_stale_backfill_marker_keeps_recent_fallback() -> None:
    from runs_dir import run_state_ledger_backfill_marker_path, run_state_ledger_path, runs_root

    nfm_mod._RUN_STATE_LOOKUP_CACHE.clear()
    runs_dir_mod._RUN_STATE_LEDGER_CACHE.clear()
    runs_dir_mod._RUN_STATE_RECENT_INDEX_CACHE.clear()
    root = runs_root()
    root.mkdir(parents=True, exist_ok=True)
    run_state_ledger_path(root).write_text("", encoding="utf-8")
    run_state_ledger_backfill_marker_path(root).write_text(
        '{"version":-1,"backfilled_at":1}',
        encoding="utf-8",
    )
    run_dir = root / "stale-marker-recent"
    run_dir.mkdir(parents=True, exist_ok=True)
    state_path = run_dir / "state.json"
    state_path.write_text(
        '{"session_id":"STALE-MARKER-SID","jsonl_path":"/tmp/stale-marker.jsonl"}',
        encoding="utf-8",
    )
    assert runs_dir_mod.state_files_for_sid(root, "STALE-MARKER-SID") == [state_path]
    path = nfm_mod._scan_run_state_for_jsonl("STALE-MARKER-SID")
    assert str(path) == "/tmp/stale-marker.jsonl", path
    print("PASS test_run_state_stale_backfill_marker_keeps_recent_fallback")


async def test_run_state_current_backfill_marker_finds_new_ledger_write() -> None:
    from runs_dir import (
        _RUN_STATE_LEDGER_BACKFILL_VERSION,
        atomic_write_json,
        run_state_ledger_backfill_marker_path,
        runs_root,
    )

    nfm_mod._RUN_STATE_LOOKUP_CACHE.clear()
    runs_dir_mod._RUN_STATE_LEDGER_CACHE.clear()
    runs_dir_mod._RUN_STATE_RECENT_INDEX_CACHE.clear()
    root = runs_root()
    run_state_ledger_backfill_marker_path(root).write_text(
        f'{{"version":{_RUN_STATE_LEDGER_BACKFILL_VERSION},"backfilled_at":1}}',
        encoding="utf-8",
    )
    run_dir = root / "current-marker-new-write"
    run_dir.mkdir(parents=True, exist_ok=True)
    state_path = run_dir / "state.json"
    atomic_write_json(
        state_path,
        {"session_id": "CURRENT-MARKER-NEW", "jsonl_path": "/tmp/current-marker-new.jsonl"},
    )
    original_recent = runs_dir_mod._recent_state_index_for_root

    def fail_recent(*_args, **_kwargs):
        raise AssertionError("ledger-backed new state should not need recent scan")

    runs_dir_mod._recent_state_index_for_root = fail_recent  # type: ignore
    try:
        assert runs_dir_mod.state_files_for_sid(root, "CURRENT-MARKER-NEW") == [state_path]
        path = nfm_mod._scan_run_state_for_jsonl("CURRENT-MARKER-NEW")
    finally:
        runs_dir_mod._recent_state_index_for_root = original_recent  # type: ignore
    assert str(path) == "/tmp/current-marker-new.jsonl", path
    print("PASS test_run_state_current_backfill_marker_finds_new_ledger_write")


async def test_run_state_recent_index_reuses_unchanged_root_after_ttl() -> None:
    from runs_dir import runs_root

    nfm_mod._RUN_STATE_LOOKUP_CACHE.clear()
    runs_dir_mod._RUN_STATE_RECENT_INDEX_CACHE.clear()
    runs_dir_mod._RUN_STATE_LEDGER_CACHE.clear()
    root = runs_root()
    for index in range(4):
        run_dir = root / f"run-recent-root-reuse-{index}"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "state.json").write_text(
            f'{{"session_id":"RECENT-ROOT-REUSE-{index}","jsonl_path":"/tmp/root-reuse-{index}.jsonl"}}',
            encoding="utf-8",
        )
    assert runs_dir_mod.recent_state_files_for_sid(root, "MISSING-ROOT-REUSE") == []
    root_key = str(root)
    ts, fingerprint, index, root_signature, pending_run_dirs = runs_dir_mod._RUN_STATE_RECENT_INDEX_CACHE[root_key]
    runs_dir_mod._RUN_STATE_RECENT_INDEX_CACHE[root_key] = (
        ts - runs_dir_mod._RUN_STATE_RECENT_INDEX_TTL_S - 0.1,
        fingerprint,
        index,
        root_signature,
        pending_run_dirs,
    )
    original_scan = runs_dir_mod._recent_state_scan

    def fail_scan(*_args, **_kwargs):
        raise AssertionError("unchanged root should reuse recent index after TTL")

    runs_dir_mod._recent_state_scan = fail_scan  # type: ignore
    try:
        assert runs_dir_mod.recent_state_files_for_sid(root, "MISSING-ROOT-REUSE-2") == []
    finally:
        runs_dir_mod._recent_state_scan = original_scan  # type: ignore
    print("PASS test_run_state_recent_index_reuses_unchanged_root_after_ttl")


async def test_run_state_recent_pending_dir_detects_late_state() -> None:
    from runs_dir import runs_root

    nfm_mod._RUN_STATE_LOOKUP_CACHE.clear()
    runs_dir_mod._RUN_STATE_RECENT_INDEX_CACHE.clear()
    runs_dir_mod._RUN_STATE_LEDGER_CACHE.clear()
    root = runs_root()
    existing_dir = root / "run-recent-pending-existing"
    existing_dir.mkdir(parents=True, exist_ok=True)
    (existing_dir / "state.json").write_text(
        '{"session_id":"RECENT-PENDING-EXISTING","jsonl_path":"/tmp/recent-pending-existing.jsonl"}',
        encoding="utf-8",
    )
    pending_dir = root / "run-recent-pending"
    pending_dir.mkdir(parents=True, exist_ok=True)
    assert runs_dir_mod.recent_state_files_for_sid(root, "RECENT-PENDING") == []
    root_key = str(root)
    ts, fingerprint, index, root_signature, pending_run_dirs = runs_dir_mod._RUN_STATE_RECENT_INDEX_CACHE[root_key]
    runs_dir_mod._RUN_STATE_RECENT_INDEX_CACHE[root_key] = (
        ts - runs_dir_mod._RUN_STATE_RECENT_INDEX_TTL_S - 0.1,
        fingerprint,
        index,
        root_signature,
        pending_run_dirs,
    )
    (pending_dir / "state.json").write_text(
        '{"session_id":"RECENT-PENDING","jsonl_path":"/tmp/recent-pending.jsonl"}',
        encoding="utf-8",
    )
    paths = runs_dir_mod.recent_state_files_for_sid(root, "RECENT-PENDING")
    assert paths == [pending_dir / "state.json"], paths
    print("PASS test_run_state_recent_pending_dir_detects_late_state")


async def test_run_state_recent_empty_pending_reuses_unchanged_root() -> None:
    nfm_mod._RUN_STATE_LOOKUP_CACHE.clear()
    runs_dir_mod._RUN_STATE_RECENT_INDEX_CACHE.clear()
    runs_dir_mod._RUN_STATE_LEDGER_CACHE.clear()
    root = nfm_mod.Path(tempfile.mkdtemp(prefix="nfm-empty-pending-"))
    (root / "run-empty-pending").mkdir(parents=True, exist_ok=True)
    assert runs_dir_mod.recent_state_files_for_sid(root, "EMPTY-PENDING") == []
    root_key = str(root)
    ts, fingerprint, index, root_signature, pending_run_dirs = runs_dir_mod._RUN_STATE_RECENT_INDEX_CACHE[root_key]
    assert fingerprint == ()
    assert index == {}
    assert pending_run_dirs == ("run-empty-pending",)
    runs_dir_mod._RUN_STATE_RECENT_INDEX_CACHE[root_key] = (
        ts - runs_dir_mod._RUN_STATE_RECENT_INDEX_TTL_S - 0.1,
        fingerprint,
        index,
        root_signature,
        pending_run_dirs,
    )
    original_scan = runs_dir_mod._recent_state_scan

    def fail_scan(*_args, **_kwargs):
        raise AssertionError("unchanged all-pending root should reuse empty recent index")

    runs_dir_mod._recent_state_scan = fail_scan  # type: ignore
    try:
        assert runs_dir_mod.recent_state_files_for_sid(root, "EMPTY-PENDING-2") == []
    finally:
        runs_dir_mod._recent_state_scan = original_scan  # type: ignore
    print("PASS test_run_state_recent_empty_pending_reuses_unchanged_root")


async def test_run_state_recent_scan_failure_is_not_cached() -> None:
    from runs_dir import runs_root

    nfm_mod._RUN_STATE_LOOKUP_CACHE.clear()
    runs_dir_mod._RUN_STATE_RECENT_INDEX_CACHE.clear()
    runs_dir_mod._RUN_STATE_LEDGER_CACHE.clear()
    root = runs_root()
    root.mkdir(parents=True, exist_ok=True)
    original_scan = runs_dir_mod._recent_state_scan

    def failed_scan(*_args, **_kwargs):
        return None

    runs_dir_mod._recent_state_scan = failed_scan  # type: ignore
    try:
        assert runs_dir_mod.recent_state_files_for_sid(root, "SCAN-FAILURE") == []
    finally:
        runs_dir_mod._recent_state_scan = original_scan  # type: ignore
    assert str(root) not in runs_dir_mod._RUN_STATE_RECENT_INDEX_CACHE
    print("PASS test_run_state_recent_scan_failure_is_not_cached")


async def test_run_state_recent_invalid_state_becoming_valid_rebuilds() -> None:
    from runs_dir import runs_root

    nfm_mod._RUN_STATE_LOOKUP_CACHE.clear()
    runs_dir_mod._RUN_STATE_RECENT_INDEX_CACHE.clear()
    runs_dir_mod._RUN_STATE_LEDGER_CACHE.clear()
    root = runs_root()
    run_dir = root / "run-invalid-to-valid"
    run_dir.mkdir(parents=True, exist_ok=True)
    state_path = run_dir / "state.json"
    state_path.write_text('{"jsonl_path":"/tmp/invalid-to-valid.jsonl"}', encoding="utf-8")
    assert runs_dir_mod.recent_state_files_for_sid(root, "INVALID-TO-VALID") == []
    root_key = str(root)
    ts, fingerprint, index, root_signature, pending_run_dirs = runs_dir_mod._RUN_STATE_RECENT_INDEX_CACHE[root_key]
    state_path.write_text(
        '{"session_id":"INVALID-TO-VALID","jsonl_path":"/tmp/invalid-to-valid.jsonl"}',
        encoding="utf-8",
    )
    now = time.time()
    os.utime(state_path, (now + 2, now + 2))
    runs_dir_mod._RUN_STATE_RECENT_INDEX_CACHE[root_key] = (
        ts - runs_dir_mod._RUN_STATE_RECENT_INDEX_TTL_S - 0.1,
        fingerprint,
        index,
        root_signature,
        pending_run_dirs,
    )
    assert runs_dir_mod.recent_state_files_for_sid(root, "INVALID-TO-VALID") == [state_path]
    print("PASS test_run_state_recent_invalid_state_becoming_valid_rebuilds")


async def test_run_state_recent_state_sid_change_rebuilds() -> None:
    from runs_dir import runs_root

    nfm_mod._RUN_STATE_LOOKUP_CACHE.clear()
    runs_dir_mod._RUN_STATE_RECENT_INDEX_CACHE.clear()
    runs_dir_mod._RUN_STATE_LEDGER_CACHE.clear()
    root = runs_root()
    run_dir = root / "run-state-sid-change"
    run_dir.mkdir(parents=True, exist_ok=True)
    state_path = run_dir / "state.json"
    state_path.write_text(
        '{"session_id":"OLD-SID","jsonl_path":"/tmp/old-sid.jsonl"}',
        encoding="utf-8",
    )
    assert runs_dir_mod.recent_state_files_for_sid(root, "OLD-SID") == [state_path]
    root_key = str(root)
    ts, fingerprint, index, root_signature, pending_run_dirs = runs_dir_mod._RUN_STATE_RECENT_INDEX_CACHE[root_key]
    state_path.write_text(
        '{"session_id":"NEW-SID","jsonl_path":"/tmp/new-sid.jsonl"}',
        encoding="utf-8",
    )
    now = time.time()
    os.utime(state_path, (now + 2, now + 2))
    runs_dir_mod._RUN_STATE_RECENT_INDEX_CACHE[root_key] = (
        ts - runs_dir_mod._RUN_STATE_RECENT_INDEX_TTL_S - 0.1,
        fingerprint,
        index,
        root_signature,
        pending_run_dirs,
    )
    assert runs_dir_mod.recent_state_files_for_sid(root, "NEW-SID") == [state_path]
    print("PASS test_run_state_recent_state_sid_change_rebuilds")


async def test_run_state_lookup_miss_stays_bounded() -> None:
    from runs_dir import (
        _RUN_STATE_LEDGER_BACKFILL_VERSION,
        run_state_ledger_backfill_marker_path,
        run_state_ledger_path,
        runs_root,
    )
    from orchs import jsonl_helpers

    nfm_mod._RUN_STATE_LOOKUP_CACHE.clear()
    runs_dir_mod._RUN_STATE_LEDGER_CACHE.clear()
    runs_dir_mod._RUN_STATE_RECENT_INDEX_CACHE.clear()
    root = runs_root()
    root.mkdir(parents=True, exist_ok=True)
    run_state_ledger_path(root).write_text("", encoding="utf-8")
    run_state_ledger_backfill_marker_path(root).write_text(
        f'{{"version":{_RUN_STATE_LEDGER_BACKFILL_VERSION},"backfilled_at":1}}',
        encoding="utf-8",
    )
    for i in range(runs_dir_mod._RUN_STATE_RECENT_SCAN_LIMIT + 10):
        run_dir = root / f"bounded-miss-old-run-{i}"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "state.json").write_text(
            '{"session_id":"old","jsonl_path":"/tmp/old.jsonl"}',
            encoding="utf-8",
        )
    original_scan = runs_dir_mod._recent_state_scan
    original_backfill = runs_dir_mod.ensure_run_state_ledger_backfilled
    original_recent_backfill = runs_dir_mod._backfill_run_state_ledger
    original_compute = jsonl_helpers.compute_jsonl_read_path
    scan_calls = 0

    def counted_scan(*args, **kwargs):
        nonlocal scan_calls
        scan_calls += 1
        return original_scan(*args, **kwargs)

    def resolved_by_provider(*_args, **_kwargs):
        return "/tmp/provider-fallback.jsonl"

    def fail_backfill(*_args, **_kwargs):
        raise AssertionError("lookup miss should not run ledger backfill")

    runs_dir_mod._recent_state_scan = counted_scan  # type: ignore
    runs_dir_mod.ensure_run_state_ledger_backfilled = fail_backfill  # type: ignore
    runs_dir_mod._backfill_run_state_ledger = fail_backfill  # type: ignore
    jsonl_helpers.compute_jsonl_read_path = resolved_by_provider  # type: ignore
    try:
        path = nfm_mod._resolve_primary_jsonl(
            {"id": "bounded-miss", "cwd": "/tmp/bounded-miss"},
            "NOT-IN-RECENT-SID",
        )
    finally:
        runs_dir_mod._recent_state_scan = original_scan  # type: ignore
        runs_dir_mod.ensure_run_state_ledger_backfilled = original_backfill  # type: ignore
        runs_dir_mod._backfill_run_state_ledger = original_recent_backfill  # type: ignore
        jsonl_helpers.compute_jsonl_read_path = original_compute
    assert str(path) == "/tmp/provider-fallback.jsonl", path
    assert scan_calls == 0, f"expected current ledger miss to skip recent scan, got {scan_calls}"
    print("PASS test_run_state_lookup_miss_stays_bounded")


async def test_persisted_native_path_skips_run_state_lookup() -> None:
    _patch()
    nfm = nfm_mod.NativeFilesManager()
    sess = session_manager.create(name="persisted", cwd="/tmp/persisted", orchestration_mode="manager")
    sid = sess["id"]
    target = nfm_mod._Target(
        owning=sid,
        agent_sid="PERSISTED-SID",
        jsonl_path=nfm_mod.Path("/tmp/persisted.jsonl"),
        start_offset=12,
        can_tail=True,
    )
    nfm._append_native_path_target(sid, target)
    original_scan = nfm_mod._scan_run_state_for_jsonl
    original_compute = nfm_mod._resolve_primary_jsonl

    def fail_scan(*_args, **_kwargs):
        raise AssertionError("persisted native_paths should avoid run-state lookup")

    nfm_mod._scan_run_state_for_jsonl = fail_scan  # type: ignore
    nfm_mod._resolve_primary_jsonl = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("persisted native_paths should avoid jsonl resolution")
    )  # type: ignore
    try:
        resolved = await nfm._resolve_primary_target(sid, sess, "PERSISTED-SID")
    finally:
        nfm_mod._scan_run_state_for_jsonl = original_scan  # type: ignore
        nfm_mod._resolve_primary_jsonl = original_compute  # type: ignore
    assert resolved is not None
    assert str(resolved.jsonl_path) == "/tmp/persisted.jsonl"
    assert resolved.start_offset == 12
    print("PASS test_persisted_native_path_skips_run_state_lookup")


async def test_primary_jsonl_positive_cache_skips_path_stat() -> None:
    nfm = nfm_mod.NativeFilesManager()
    sess = {"id": "sid-cache", "cwd": "/tmp/cache"}
    key = ("sid-cache", "/tmp/cache", "PRIMARY-CACHE-SID")
    nfm._primary_jsonl_cache[key] = (
        nfm_mod.time.monotonic(),
        nfm_mod.Path("/tmp/primary-cache.jsonl"),
    )
    original_resolve = nfm_mod._resolve_primary_jsonl

    def fail_resolve(*_args, **_kwargs):
        raise AssertionError("positive primary cache should avoid resolve/stat path")

    nfm_mod._resolve_primary_jsonl = fail_resolve  # type: ignore
    try:
        path = nfm._resolve_primary_jsonl_cached(sess, "PRIMARY-CACHE-SID")
    finally:
        nfm_mod._resolve_primary_jsonl = original_resolve  # type: ignore
    assert str(path) == "/tmp/primary-cache.jsonl"
    print("PASS test_primary_jsonl_positive_cache_skips_path_stat")


async def test_codex_primary_not_tailed_by_claude_tailer() -> None:
    """A Codex rollout must NOT be handed to the claude-shaped
    OwnedClaudeJsonlTailer — it can't normalize raw Codex lines and would
    forward them verbatim as agent_message noise. Codex is covered by its
    run-scoped CodexRolloutTailer + recovery, so the claude backup is
    skipped. Regression for the 'unknown event: agent_message.*' garbage."""
    from pathlib import Path
    import native_files_manager as nfm

    rollout = Path.home() / ".codex" / "sessions" / "2026" / "06" / "16" / "rollout-X.jsonl"
    assert nfm._is_codex_rollout(rollout), rollout
    assert not nfm._is_codex_rollout(Path("/tmp/proj/SID.jsonl"))
    # Name-only detection when not under ~/.codex (e.g. custom CODEX_HOME).
    assert nfm._is_codex_rollout(Path("/custom/home/rollout-abc.jsonl"))

    # Codex rollout paths are tracked for native-path provenance, but
    # marked `can_tail=False` so they are not handed to the Claude-shaped
    # OwnedClaudeJsonlTailer.
    orig_scan = nfm._scan_run_state_for_jsonl
    orig_local = nfm._is_local_session
    nfm._scan_run_state_for_jsonl = lambda sid: rollout  # type: ignore
    nfm._is_local_session = lambda sess: True  # type: ignore
    try:
        resolved = nfm._resolve_primary_jsonl({"id": "s", "cwd": "/repo"}, "CODEX-SID")
        target = await nfm.NativeFilesManager()._resolve_primary_target(
            "s", {"id": "s", "cwd": "/repo"}, "CODEX-SID",
        )
    finally:
        nfm._scan_run_state_for_jsonl = orig_scan  # type: ignore
        nfm._is_local_session = orig_local  # type: ignore
    assert resolved == rollout, f"codex rollout was not recorded: {resolved}"
    assert target is not None, "codex rollout target was not recorded"
    assert target.can_tail is False, "codex rollout leaked to claude tailer"
    print("PASS test_codex_primary_not_tailed_by_claude_tailer")


async def test_demand_seed_does_not_block_event_loop() -> None:
    _patch()
    nfm = nfm_mod.NativeFilesManager()
    nfm.bind()
    sess = session_manager.create(name="slow", cwd="/tmp/slow", orchestration_mode="manager")
    sid = sess["id"]
    original_get = nfm_mod.session_manager.get

    def slow_get(*args, **kwargs):
        import time
        time.sleep(0.2)
        return original_get(*args, **kwargs)

    nfm_mod.session_manager.get = slow_get
    ticks = 0

    async def heartbeat():
        nonlocal ticks
        deadline = asyncio.get_running_loop().time() + 0.12
        while asyncio.get_running_loop().time() < deadline:
            ticks += 1
            await asyncio.sleep(0.01)

    try:
        await asyncio.gather(
            _demand(nfm, sid, token="slow-token", present=True),
            heartbeat(),
        )
    finally:
        nfm_mod.session_manager.get = original_get
    assert ticks >= 5, f"native_files demand blocked event loop, ticks={ticks}"
    print("PASS test_demand_seed_does_not_block_event_loop")


async def test_agent_sid_session_read_does_not_block_event_loop() -> None:
    _patch()
    nfm = nfm_mod.NativeFilesManager()
    nfm.bind()
    sess = session_manager.create(
        name="agent-sid-slow-read",
        cwd="/tmp/agent-sid-slow-read",
        orchestration_mode="manager",
    )
    sid = sess["id"]
    root_id = session_manager._root_id_for(sid) or sid
    agent_sid = "AGENT-SID-SLOW-READ"
    proj = os.path.join(_CLAUDE_CFG, "projects", "-tmp-agent-sid-slow-read")
    os.makedirs(proj, exist_ok=True)
    open(os.path.join(proj, f"{agent_sid}.jsonl"), "w").close()

    original_get_lite = nfm_mod.session_manager.get_lite

    def slow_get_lite(*args, **kwargs):
        import time
        time.sleep(0.2)
        return original_get_lite(*args, **kwargs)

    nfm_mod.session_manager.get_lite = slow_get_lite
    ticks = 0

    async def heartbeat():
        nonlocal ticks
        deadline = asyncio.get_running_loop().time() + 0.12
        while asyncio.get_running_loop().time() < deadline:
            ticks += 1
            await asyncio.sleep(0.01)

    try:
        await asyncio.gather(
            nfm._on_agent_sid(BusEvent(
                type="session.agent_sid_set",
                root_id=root_id,
                sid=sid,
                payload={"agent_sid": agent_sid},
                persist=False,
            )),
            heartbeat(),
        )
    finally:
        nfm_mod.session_manager.get_lite = original_get_lite
    assert ticks >= 5, f"native_files agent_sid read blocked event loop, ticks={ticks}"
    print("PASS test_agent_sid_session_read_does_not_block_event_loop")


async def test_demand_seed_schedules_slow_primary_resolution() -> None:
    _patch()
    nfm = nfm_mod.NativeFilesManager()
    nfm.bind()
    sess = session_manager.create(
        name="background-primary",
        cwd="/tmp/background-primary",
        orchestration_mode="manager",
    )
    sid = sess["id"]
    root_id = session_manager._root_id_for(sid) or sid
    session_manager.set_agent_sid(sid, "manager", "BACKGROUND-SID")
    original_resolve = nfm._resolve_primary_jsonl_cached

    def slow_resolve(*args, **kwargs):
        time.sleep(0.2)
        return nfm_mod.Path("/tmp/background-primary/BACKGROUND-SID.jsonl")

    nfm._resolve_primary_jsonl_cached = slow_resolve  # type: ignore
    try:
        start = time.monotonic()
        await _demand(nfm, sid, token="background-token", present=True)
        elapsed = time.monotonic() - start
        assert elapsed < 0.1, f"demand waited for slow primary resolution: {elapsed:.3f}s"
        assert await _wait_for(
            lambda: (root_id, "BACKGROUND-SID") in _live_keys(nfm),
            timeout=1.0,
        ), "background primary resolution did not open tailer"
    finally:
        nfm._resolve_primary_jsonl_cached = original_resolve  # type: ignore
    print("PASS test_demand_seed_schedules_slow_primary_resolution")


if __name__ == "__main__":
    asyncio.run(main())
    asyncio.run(test_local_run_state_skips_expensive_jsonl_scan())
    asyncio.run(test_run_state_lookup_is_targeted_and_cached())
    asyncio.run(test_run_state_lookup_uses_ledger_before_recent_scan())
    asyncio.run(test_run_state_ledger_rejects_paths_outside_runs_root())
    asyncio.run(test_run_state_recent_lookup_does_not_backfill_ledger())
    asyncio.run(test_run_state_backfill_rejects_symlink_escape())
    asyncio.run(test_run_state_ledger_dedupes_duplicate_rows())
    asyncio.run(test_run_state_full_backfill_finds_old_state_outside_recent_window())
    asyncio.run(test_run_state_full_backfill_marker_dedupes_rows())
    asyncio.run(test_run_state_full_backfill_skips_symlink_escape())
    asyncio.run(test_run_state_full_backfill_coalesces_concurrent_marker_writes())
    asyncio.run(test_run_state_backfill_is_scheduled_at_startup())
    asyncio.run(test_run_state_stale_index_does_not_hide_new_state())
    asyncio.run(test_run_state_positive_cache_outlives_negative_cache())
    asyncio.run(test_run_state_ledger_cache_reuses_unchanged_index())
    asyncio.run(test_run_state_ledger_cache_invalidates_on_append())
    asyncio.run(test_run_state_ledger_cache_rejects_cached_symlink_escape())
    asyncio.run(test_run_state_ledger_sqlite_cache_skips_jsonl_parse())
    asyncio.run(test_run_state_ledger_sqlite_cache_invalidates_on_append())
    asyncio.run(test_run_state_ledger_concurrent_appends_extend_cache_without_lost_rows())
    asyncio.run(test_run_index_uses_ledger_cache_without_state_scan())
    asyncio.run(test_run_index_append_extends_ledger_cache_without_state_scan())
    asyncio.run(test_run_index_cache_rebuild_uses_latest_written_at_for_duplicate_app())
    asyncio.run(test_run_index_app_backfill_orders_duplicate_app_by_state_mtime())
    asyncio.run(test_run_state_ledger_sqlite_cache_corrupt_and_wrong_version_fallback())
    asyncio.run(test_run_state_ledger_sqlite_cache_rejects_poisoned_paths())
    asyncio.run(test_run_state_ledger_sqlite_cache_dedupes_duplicate_state_paths())
    asyncio.run(test_run_state_ledger_sqlite_cache_rebuild_singleflight_waits())
    asyncio.run(test_run_state_ledger_sqlite_cache_rebuild_signature_change_does_not_deadlock())
    asyncio.run(test_run_state_ledger_string_shape_is_strict())
    asyncio.run(test_run_state_ledger_parse_defers_resolve_to_target_sid())
    asyncio.run(test_run_state_ledger_parse_defers_path_construction_to_target_sid())
    asyncio.run(test_run_state_recent_cache_revalidates_cached_paths())
    asyncio.run(test_run_state_recent_candidates_skip_per_entry_resolve())
    asyncio.run(test_run_state_recent_scan_skips_per_entry_path_helper())
    asyncio.run(test_run_state_recent_lookup_resolves_only_candidates())
    asyncio.run(test_run_state_recent_candidates_skip_run_dir_symlink())
    asyncio.run(test_run_state_recent_candidates_skip_state_symlink())
    asyncio.run(test_run_state_recent_build_rejects_swapped_symlink())
    asyncio.run(test_run_state_recent_index_is_reused_across_sids())
    asyncio.run(test_run_state_lookup_coalesces_concurrent_scans())
    asyncio.run(test_run_state_recent_index_coalesces_concurrent_sid_scans())
    asyncio.run(test_run_state_lookup_checks_recent_dirs_first())
    asyncio.run(test_run_state_recent_unledgered_state_is_found_without_backfill())
    asyncio.run(test_run_state_current_backfill_marker_skips_recent_miss_scan())
    asyncio.run(test_run_state_stale_backfill_marker_keeps_recent_fallback())
    asyncio.run(test_run_state_current_backfill_marker_finds_new_ledger_write())
    asyncio.run(test_run_state_recent_index_reuses_unchanged_root_after_ttl())
    asyncio.run(test_run_state_recent_pending_dir_detects_late_state())
    asyncio.run(test_run_state_recent_empty_pending_reuses_unchanged_root())
    asyncio.run(test_run_state_recent_scan_failure_is_not_cached())
    asyncio.run(test_run_state_recent_invalid_state_becoming_valid_rebuilds())
    asyncio.run(test_run_state_recent_state_sid_change_rebuilds())
    asyncio.run(test_run_state_lookup_miss_stays_bounded())
    asyncio.run(test_persisted_native_path_skips_run_state_lookup())
    asyncio.run(test_primary_jsonl_positive_cache_skips_path_stat())
    asyncio.run(test_codex_primary_not_tailed_by_claude_tailer())
    asyncio.run(test_demand_seed_does_not_block_event_loop())
    asyncio.run(test_agent_sid_session_read_does_not_block_event_loop())
    asyncio.run(test_demand_seed_schedules_slow_primary_resolution())
