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
    sessions_dir = Path(_TMP_HOME) / "sessions"
    if sessions_dir.exists():
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


if __name__ == "__main__":
    results = [
        test_manual_root_delete_reconciles_hot_summary_index(),
        test_orphan_sidecars_are_removed_on_summary_build(),
        test_queued_summary_write_does_not_resurrect_deleted_root(),
    ]
    if not all(results):
        raise SystemExit(1)
    print(f"{PASS} deleted session summary reconciliation")
