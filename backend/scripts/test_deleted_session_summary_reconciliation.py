from __future__ import annotations

import json
import os
import shutil
import sys
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
    session_store._summary_roots_fingerprint = ()


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
            "fork_ids": [],
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


def test_manual_root_delete_reconciles_hot_summary_index() -> bool:
    _reset_home()
    sid = "manual-delete-root"
    _write_root(sid)
    session_store._ensure_summary_index(blocking=True)
    before = sid in _listed_ids()
    _write_orphan_sidecars(sid)
    (_sessions_dir() / f"{sid}.json").unlink()

    listed = sid in _listed_ids()
    summary_exists = (_sessions_dir() / f"{sid}.summary.json").exists()
    opened_exists = (_sessions_dir() / f"{sid}.opened.json").exists()
    ok = before and not listed and not summary_exists and not opened_exists
    print(f"{PASS if ok else FAIL} manual root delete purges hot summary row")
    return ok


def test_manual_root_delete_reconciles_warming_summary_index() -> bool:
    _reset_home()
    sid = "manual-delete-warming-root"
    _write_root(sid)
    session_store._upsert_summary(_record(sid))
    original_warm = session_store._start_summary_index_warm
    session_store._start_summary_index_warm = lambda: None
    try:
        before = sid in _listed_ids()
        snapshot_complete = session_store.summary_index_snapshot_complete()
        (_sessions_dir() / f"{sid}.json").unlink()

        listed = sid in _listed_ids()
        summary_exists = (_sessions_dir() / f"{sid}.summary.json").exists()
    finally:
        session_store._start_summary_index_warm = original_warm
    ok = before and not snapshot_complete and not listed and not summary_exists
    print(f"{PASS if ok else FAIL} manual root delete purges warming summary row")
    return ok


def test_orphan_sidecars_are_removed_on_summary_build() -> bool:
    _reset_home()
    sid = "orphan-sidecar-root"
    _write_orphan_sidecars(sid)

    session_store._ensure_summary_index(blocking=True)
    listed = sid in _listed_ids()
    summary_exists = (_sessions_dir() / f"{sid}.summary.json").exists()
    opened_exists = (_sessions_dir() / f"{sid}.opened.json").exists()
    ok = not listed and not summary_exists and not opened_exists
    print(f"{PASS if ok else FAIL} orphan sidecars removed during summary build")
    return ok


def test_queued_summary_write_does_not_resurrect_deleted_root() -> bool:
    _reset_home()
    sid = "queued-summary-root"
    _write_root(sid)
    summary = {
        "id": sid,
        "updated_at": "2026-07-01T00:00:00+00:00",
        "fork_count": 0,
        "fork_ids": [],
        "last_seen_event_uid": None,
        "current_todos": [],
        "current_tasks": [],
    }
    session_store._schedule_summary_sidecar_write(sid, summary)
    (_sessions_dir() / f"{sid}.json").unlink()
    session_store._schedule_summary_sidecar_write(sid, summary)
    session_store._summary_sidecar_write_queue.join()

    listed = sid in _listed_ids()
    summary_exists = (_sessions_dir() / f"{sid}.summary.json").exists()
    ok = not listed and not summary_exists
    print(f"{PASS if ok else FAIL} queued summary write skips missing root")
    return ok


def test_summary_sidecar_batch_coalesces_latest_per_root() -> bool:
    _reset_home()
    for sid in ("summary-batch-a", "summary-batch-b"):
        _write_root(sid)
    writes: list[tuple[str, dict, int | None]] = []
    original_write = session_store._write_summary_file

    def record_write(root_id: str, summary: dict, **_kwargs) -> None:
        root_mtime_ns = _kwargs.get("root_mtime_ns")
        writes.append((root_id, summary, root_mtime_ns))

    session_store._write_summary_file = record_write  # type: ignore[assignment]
    try:
        session_store._summary_sidecar_write_queue.put_nowait(
            ("summary-batch-a", {"version": 1}, None, None)
        )
        session_store._summary_sidecar_write_queue.put_nowait(
            ("summary-batch-a", {"version": 2}, None, None)
        )
        session_store._summary_sidecar_write_queue.put_nowait(
            ("summary-batch-b", {"version": 1}, None, None)
        )
        session_store._summary_sidecar_write_queue.put_nowait(
            ("summary-batch-a", {"version": 3}, None, None)
        )
        stop = session_store._process_summary_sidecar_batch(
            session_store._summary_sidecar_write_queue.get_nowait()
        )
    finally:
        session_store._write_summary_file = original_write  # type: ignore[assignment]
        session_store._summary_sidecar_write_queue.join()
    by_root = {root_id: summary["version"] for root_id, summary, _ in writes}
    ok = not stop and by_root == {"summary-batch-a": 3, "summary-batch-b": 1}
    print(f"{PASS if ok else FAIL} summary sidecar batch coalesces latest per root")
    return ok


def test_summary_sidecar_batch_skips_stale_root_mtime() -> bool:
    _reset_home()
    sid = "summary-stale-root"
    _write_root(sid)
    root_path = _sessions_dir() / f"{sid}.json"
    old_signature = session_store._session_file_signature(root_path)
    old_mtime = root_path.stat().st_mtime_ns
    newer_mtime = old_mtime + 1_000_000
    os.utime(root_path, ns=(newer_mtime, newer_mtime))
    writes: list[str] = []
    original_write = session_store._write_summary_file

    def record_write(root_id: str, summary: dict, **_kwargs) -> None:
        writes.append(root_id)

    session_store._write_summary_file = record_write  # type: ignore[assignment]
    try:
        session_store._summary_sidecar_write_queue.put_nowait(
            (sid, {"version": 1}, old_mtime, old_signature)
        )
        stop = session_store._process_summary_sidecar_batch(
            session_store._summary_sidecar_write_queue.get_nowait()
        )
    finally:
        session_store._write_summary_file = original_write  # type: ignore[assignment]
        session_store._summary_sidecar_write_queue.join()
    ok = not stop and writes == []
    print(f"{PASS if ok else FAIL} stale summary sidecar batch item is skipped")
    return ok


def test_summary_sidecar_batch_handles_sentinel_after_work() -> bool:
    _reset_home()
    sid = "summary-sentinel-root"
    _write_root(sid)
    writes: list[str] = []
    original_write = session_store._write_summary_file

    def record_write(root_id: str, summary: dict, **_kwargs) -> None:
        writes.append(root_id)

    session_store._write_summary_file = record_write  # type: ignore[assignment]
    try:
        session_store._summary_sidecar_write_queue.put_nowait((sid, {"version": 1}, None, None))
        session_store._summary_sidecar_write_queue.put_nowait(None)
        stop = session_store._process_summary_sidecar_batch(
            session_store._summary_sidecar_write_queue.get_nowait()
        )
    finally:
        session_store._write_summary_file = original_write  # type: ignore[assignment]
        session_store._summary_sidecar_write_queue.join()
    ok = stop and writes == [sid] and session_store._summary_sidecar_write_queue.empty()
    print(f"{PASS if ok else FAIL} summary sidecar batch handles sentinel after work")
    return ok


def test_summary_sidecar_batch_failure_does_not_block_other_roots() -> bool:
    _reset_home()
    for sid in ("summary-fail-a", "summary-fail-b"):
        _write_root(sid)
    writes: list[str] = []
    original_write = session_store._write_summary_file

    def record_write(root_id: str, summary: dict, **_kwargs) -> None:
        if root_id == "summary-fail-a":
            raise RuntimeError("boom")
        writes.append(root_id)

    session_store._write_summary_file = record_write  # type: ignore[assignment]
    try:
        session_store._summary_sidecar_write_queue.put_nowait(
            ("summary-fail-a", {"version": 1}, None, None)
        )
        session_store._summary_sidecar_write_queue.put_nowait(
            ("summary-fail-b", {"version": 1}, None, None)
        )
        stop = session_store._process_summary_sidecar_batch(
            session_store._summary_sidecar_write_queue.get_nowait()
        )
    finally:
        session_store._write_summary_file = original_write  # type: ignore[assignment]
        session_store._summary_sidecar_write_queue.join()
    ok = not stop and writes == ["summary-fail-b"]
    print(f"{PASS if ok else FAIL} summary sidecar batch failure keeps other roots")
    return ok


if __name__ == "__main__":
    results = [
        test_manual_root_delete_reconciles_hot_summary_index(),
        test_manual_root_delete_reconciles_warming_summary_index(),
        test_orphan_sidecars_are_removed_on_summary_build(),
        test_queued_summary_write_does_not_resurrect_deleted_root(),
        test_summary_sidecar_batch_coalesces_latest_per_root(),
        test_summary_sidecar_batch_skips_stale_root_mtime(),
        test_summary_sidecar_batch_handles_sentinel_after_work(),
        test_summary_sidecar_batch_failure_does_not_block_other_roots(),
    ]
    if not all(results):
        raise SystemExit(1)
    print(f"{PASS} deleted session summary reconciliation")
