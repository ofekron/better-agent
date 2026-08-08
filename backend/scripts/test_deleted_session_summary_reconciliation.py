from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-deleted-session-summary-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import session_store  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _reset_home() -> None:
    session_store.shutdown_root_change_owner()
    session_store._summary_sidecar_write_queue.join()
    session_store._index_sidecar_write_queue.join()
    sessions_dir = Path(_TMP_HOME) / "sessions"
    if sessions_dir.exists():
        for _ in range(3):
            try:
                shutil.rmtree(sessions_dir)
                break
            except OSError:
                session_store._summary_sidecar_write_queue.join()
                session_store._index_sidecar_write_queue.join()
        else:
            shutil.rmtree(sessions_dir)
    session_store._fork_index.clear()
    session_store._root_forks.clear()
    session_store._root_index_signatures.clear()
    session_store._index_loaded = False
    session_store._index_fingerprint = None
    session_store._summary_index.clear()
    session_store._summary_index_loaded = False
    session_store._summary_index_version = 0
    session_store._summary_order_version = 0
    session_store._summary_metadata_version = 0
    session_store._summary_sorted_id_cache = []
    session_store._summary_sorted_id_caches.clear()
    session_store._summary_sorted_cache_version = -1


def _record(sid: str) -> dict:
    return {
        "_schema_version": session_store.SCHEMA_VERSION,
        "id": sid,
        "name": sid,
        "model": "gpt-5.5",
        "cwd": "/tmp/deleted-session-summary",
        "orchestration_mode": "native",
        "kind": "user",
        "parent_session_id": None,
        "forks": [],
        "messages": [],
        "next_seq": 0,
        "created_at": "2026-07-01T00:00:00+00:00",
        "updated_at": "2026-07-01T00:00:00+00:00",
        "source": "cli",
        "user_initiated": True,
    }


def _sessions_dir() -> Path:
    path = Path(_TMP_HOME) / "sessions"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _summary_item(
    sid: str,
    summary: dict,
    root_mtime_ns=None,
    root_signature=None,
):
    return (
        _sessions_dir().resolve(),
        sid,
        summary,
        root_mtime_ns,
        root_signature,
    )


def _write_root(sid: str) -> None:
    (_sessions_dir() / f"{sid}.json").write_text(
        json.dumps(_record(sid)),
        encoding="utf-8",
    )


def _write_orphan_sidecars(sid: str) -> None:
    sessions = _sessions_dir()
    (sessions / f"{sid}.summary.json").write_text(
        json.dumps({
            "id": sid,
            "updated_at": "2026-07-01T00:00:00+00:00",
            "fork_count": 0,
            "all_fork_ids": [],
            "last_seen_event_uid": None,
            "current_todos": [],
            "current_tasks": [],
        }),
        encoding="utf-8",
    )
    (sessions / f"{sid}.opened.json").write_text(
        json.dumps({sid: "2026-07-01T00:00:00+00:00"}),
        encoding="utf-8",
    )


def _listed_ids() -> set[str]:
    return {str(s.get("id")) for s in session_store.list_sessions()}


def _externally_delete_and_wait(sid: str) -> bool:
    session_store.start_root_change_owner()
    session_store._wait_root_change_owner_ready()
    binding = session_store._root_change_binding
    assert binding is not None
    generation = binding.owner.observation_generation
    (_sessions_dir() / f"{sid}.json").unlink()
    return binding.owner.wait_for_observation(generation, 2.0)


def test_root_change_owner_delete_projects_hot_summary_index() -> None:
    _reset_home()
    sid = "manual-delete-root"
    _write_root(sid)
    session_store._ensure_summary_index(blocking=True)
    before = sid in _listed_ids()
    _write_orphan_sidecars(sid)
    try:
        observed = _externally_delete_and_wait(sid)
        listed = sid in _listed_ids()
        summary_exists = (_sessions_dir() / f"{sid}.summary.json").exists()
        opened_exists = (_sessions_dir() / f"{sid}.opened.json").exists()
    finally:
        session_store.shutdown_root_change_owner()
    # `before` locks #372: a cold-scan build must load a freshly-written root,
    # not self-invalidate and leave the index empty.
    assert before, "root not listed after cold-scan build (#372 self-invalidation)"
    assert observed, "root-change owner did not observe the external delete"
    assert not listed, "deleted root still present in listed sessions"
    assert not summary_exists, "summary sidecar survived root delete"
    assert not opened_exists, "opened sidecar survived root delete"


def test_root_change_owner_delete_projects_warming_summary_index() -> None:
    _reset_home()
    sid = "manual-delete-warming-root"
    _write_root(sid)
    session_store._upsert_summary(
        _record(sid),
        storage_identity=_sessions_dir().resolve(),
    )
    original_warm = session_store._start_summary_index_warm
    session_store._start_summary_index_warm = lambda: None
    try:
        before = sid in _listed_ids()
        snapshot_complete = session_store.summary_index_snapshot_complete()
        observed = _externally_delete_and_wait(sid)
        listed = sid in _listed_ids()
        summary_exists = (_sessions_dir() / f"{sid}.summary.json").exists()
    finally:
        session_store.shutdown_root_change_owner()
        session_store._start_summary_index_warm = original_warm
    assert observed, "root-change owner did not observe the external delete"
    assert before, "upserted root not listed before delete"
    assert not snapshot_complete, "snapshot reported complete during warm build"
    assert not listed, "deleted warming root still present in listed sessions"
    assert not summary_exists, "summary sidecar survived warming root delete"


def test_warm_build_loads_after_sessions_dir_reset() -> None:
    # Locks the warm (blocking=False) path against #372: forcing _SESSIONS_DIR
    # to re-resolve bumps _summary_index_reset_epoch inside _do_build. The warm
    # builder must still complete and load the index, not self-invalidate.
    _reset_home()
    sid = "warm-reset-root"
    _write_root(sid)
    session_store._SESSIONS_DIR = None
    session_store._summary_index_loaded = False
    session_store._summary_index.clear()
    session_store._ensure_summary_index(blocking=False)
    deadline = time.monotonic() + 2.0
    while not session_store._summary_index_loaded and time.monotonic() < deadline:
        time.sleep(0.01)
    assert session_store._summary_index_loaded, (
        "warm build left summary index unloaded (#372 self-invalidation)"
    )
    assert sid in _listed_ids(), "warm build did not load freshly-written root"


def test_orphan_sidecars_are_removed_on_summary_build() -> None:
    _reset_home()
    sid = "orphan-sidecar-root"
    _write_orphan_sidecars(sid)

    session_store._ensure_summary_index(blocking=True)
    listed = sid in _listed_ids()
    summary_exists = (_sessions_dir() / f"{sid}.summary.json").exists()
    opened_exists = (_sessions_dir() / f"{sid}.opened.json").exists()
    assert not listed, "orphan-sidecar-only root appeared in listed sessions"
    assert not summary_exists, "orphan summary sidecar survived summary build"
    assert not opened_exists, "orphan opened sidecar survived summary build"


def test_queued_summary_write_does_not_resurrect_deleted_root() -> None:
    _reset_home()
    sid = "queued-summary-root"
    _write_root(sid)
    summary = {
        "id": sid,
        "updated_at": "2026-07-01T00:00:00+00:00",
        "fork_count": 0,
        "all_fork_ids": [],
        "last_seen_event_uid": None,
        "current_todos": [],
        "current_tasks": [],
    }
    session_store._schedule_summary_sidecar_write(
        _sessions_dir().resolve(), sid, summary,
    )
    (_sessions_dir() / f"{sid}.json").unlink()
    session_store._schedule_summary_sidecar_write(
        _sessions_dir().resolve(), sid, summary,
    )
    session_store._summary_sidecar_write_queue.join()

    listed = sid in _listed_ids()
    summary_exists = (_sessions_dir() / f"{sid}.summary.json").exists()
    assert not listed, "queued write resurrected a deleted root in listings"
    assert not summary_exists, "queued write wrote a sidecar for a deleted root"


def test_summary_sidecar_batch_coalesces_latest_per_root() -> None:
    _reset_home()
    for sid in ("summary-batch-a", "summary-batch-b"):
        _write_root(sid)
    writes: list[tuple[str, dict, int | None]] = []
    work_queue = session_store._StorageIdentityQueue()
    original_write = session_store._write_summary_file

    def record_write(root_id: str, summary: dict, **_kwargs) -> None:
        root_mtime_ns = _kwargs.get("root_mtime_ns")
        writes.append((root_id, summary, root_mtime_ns))

    session_store._write_summary_file = record_write  # type: ignore[assignment]
    try:
        work_queue.put_nowait(
            _summary_item("summary-batch-a", {"version": 1})
        )
        work_queue.put_nowait(
            _summary_item("summary-batch-a", {"version": 2})
        )
        work_queue.put_nowait(
            _summary_item("summary-batch-b", {"version": 1})
        )
        work_queue.put_nowait(
            _summary_item("summary-batch-a", {"version": 3})
        )
        stop = session_store._process_summary_sidecar_batch(
            work_queue.get_nowait(),
            work_queue,
        )
    finally:
        session_store._write_summary_file = original_write  # type: ignore[assignment]
        work_queue.join()
    by_root = {root_id: summary["version"] for root_id, summary, _ in writes}
    assert not stop, "batch stopped on a non-sentinel drain"
    assert by_root == {"summary-batch-a": 3, "summary-batch-b": 1}, (
        f"batch did not coalesce latest per root: {by_root}"
    )


def test_summary_sidecar_batch_skips_stale_root_mtime() -> None:
    _reset_home()
    sid = "summary-stale-root"
    _write_root(sid)
    root_path = _sessions_dir() / f"{sid}.json"
    old_signature = session_store._session_file_signature(root_path)
    old_mtime = root_path.stat().st_mtime_ns
    newer_mtime = old_mtime + 1_000_000
    os.utime(root_path, ns=(newer_mtime, newer_mtime))
    writes: list[str] = []
    work_queue = session_store._StorageIdentityQueue()
    original_write = session_store._write_summary_file

    def record_write(root_id: str, summary: dict, **_kwargs) -> None:
        writes.append(root_id)

    session_store._write_summary_file = record_write  # type: ignore[assignment]
    try:
        work_queue.put_nowait(
            _summary_item(sid, {"version": 1}, old_mtime, old_signature)
        )
        stop = session_store._process_summary_sidecar_batch(
            work_queue.get_nowait(),
            work_queue,
        )
    finally:
        session_store._write_summary_file = original_write  # type: ignore[assignment]
        work_queue.join()
    assert not stop, "batch stopped on a non-sentinel drain"
    assert writes == [], f"stale-mtime batch item was written: {writes}"


def test_summary_sidecar_batch_handles_sentinel_after_work() -> None:
    _reset_home()
    sid = "summary-sentinel-root"
    _write_root(sid)
    writes: list[str] = []
    work_queue = session_store._StorageIdentityQueue()
    original_write = session_store._write_summary_file

    def record_write(root_id: str, summary: dict, **_kwargs) -> None:
        writes.append(root_id)

    session_store._write_summary_file = record_write  # type: ignore[assignment]
    try:
        work_queue.put_nowait(
            _summary_item(sid, {"version": 1})
        )
        work_queue.put_nowait(None)
        stop = session_store._process_summary_sidecar_batch(
            work_queue.get_nowait(),
            work_queue,
        )
    finally:
        session_store._write_summary_file = original_write  # type: ignore[assignment]
        work_queue.join()
    assert stop, "sentinel did not stop the batch"
    assert writes == [sid], f"sentinel batch wrote unexpected roots: {writes}"
    assert work_queue.empty(), "work remained in queue after sentinel"


def test_summary_sidecar_batch_failure_does_not_block_other_roots() -> None:
    _reset_home()
    for sid in ("summary-fail-a", "summary-fail-b"):
        _write_root(sid)
    writes: list[str] = []
    work_queue = session_store._StorageIdentityQueue()
    original_write = session_store._write_summary_file

    def record_write(root_id: str, summary: dict, **_kwargs) -> None:
        if root_id == "summary-fail-a":
            raise RuntimeError("boom")
        writes.append(root_id)

    session_store._write_summary_file = record_write  # type: ignore[assignment]
    try:
        work_queue.put_nowait(
            _summary_item("summary-fail-a", {"version": 1})
        )
        work_queue.put_nowait(
            _summary_item("summary-fail-b", {"version": 1})
        )
        stop = session_store._process_summary_sidecar_batch(
            work_queue.get_nowait(),
            work_queue,
        )
    finally:
        session_store._write_summary_file = original_write  # type: ignore[assignment]
        work_queue.join()
    assert not stop, "batch stopped after a per-root failure"
    assert writes == ["summary-fail-b"], (
        f"per-root failure blocked other roots: {writes}"
    )


if __name__ == "__main__":
    tests = [
        ("projected root delete purges hot summary row", test_root_change_owner_delete_projects_hot_summary_index),
        ("projected root delete purges warming summary row", test_root_change_owner_delete_projects_warming_summary_index),
        ("warm build loads after sessions dir reset (#372)", test_warm_build_loads_after_sessions_dir_reset),
        ("orphan sidecars removed during summary build", test_orphan_sidecars_are_removed_on_summary_build),
        ("queued summary write skips missing root", test_queued_summary_write_does_not_resurrect_deleted_root),
        ("summary sidecar batch coalesces latest per root", test_summary_sidecar_batch_coalesces_latest_per_root),
        ("stale summary sidecar batch item is skipped", test_summary_sidecar_batch_skips_stale_root_mtime),
        ("summary sidecar batch handles sentinel after work", test_summary_sidecar_batch_handles_sentinel_after_work),
        ("summary sidecar batch failure keeps other roots", test_summary_sidecar_batch_failure_does_not_block_other_roots),
    ]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"{PASS}  {name}")
        except Exception:
            failed += 1
            import traceback
            traceback.print_exc()
            print(f"{FAIL}  {name}")
    print()
    if failed:
        print(f"{failed} of {len(tests)} test(s) FAILED")
        raise SystemExit(1)
    print(f"{PASS} deleted session summary reconciliation")
