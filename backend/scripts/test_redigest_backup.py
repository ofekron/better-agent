#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import _test_home
_test_home.isolate("bc-test-redigest-")

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "backend"))

import redigest_backup  # noqa: E402
from redigest_backup import RedigestBackup  # noqa: E402
from event_ingester import event_ingester  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402
import session_manager as _sm_module  # noqa: E402


ROOT_ID = "root-test-1234"


def _write_root_json(content: dict) -> None:
    p = redigest_backup._root_json_path(ROOT_ID)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(content), encoding="utf-8")


def _read_root_json() -> dict:
    return json.loads(redigest_backup._root_json_path(ROOT_ID).read_text())


def _events_path() -> Path:
    return redigest_backup._events_jsonl_path(ROOT_ID)


def _bak_exists() -> bool:
    return (
        redigest_backup._root_json_path(ROOT_ID).with_name(
            redigest_backup._root_json_path(ROOT_ID).name + redigest_backup._BAK_SUFFIX
        ).exists()
    )


def test_rollback_restores_both_files() -> None:
    _write_root_json({"version": "old"})
    _events_path().parent.mkdir(parents=True, exist_ok=True)
    _events_path().write_text("OLDLINE\n", encoding="utf-8")

    backup = RedigestBackup(ROOT_ID).capture()
    assert _bak_exists()

    # Simulate a failed re-digest: overwrite the render tree and append
    # new event rows.
    _write_root_json({"version": "new"})
    with _events_path().open("a", encoding="utf-8") as f:
        f.write("NEWLINE\n")

    backup.rollback()

    assert _read_root_json() == {"version": "old"}
    assert _events_path().read_text() == "OLDLINE\n"
    assert not _bak_exists()
    assert backup._settled


def test_commit_keeps_new_state_drops_backup() -> None:
    _write_root_json({"version": "old"})
    _events_path().parent.mkdir(parents=True, exist_ok=True)
    _events_path().write_text("OLDLINE\n", encoding="utf-8")

    backup = RedigestBackup(ROOT_ID).capture()
    _write_root_json({"version": "new"})
    with _events_path().open("a", encoding="utf-8") as f:
        f.write("NEWLINE\n")

    backup.commit()

    assert _read_root_json() == {"version": "new"}
    assert _events_path().read_text() == "OLDLINE\nNEWLINE\n"
    assert not _bak_exists()
    assert backup._settled


def test_rollback_deletes_events_file_when_absent_pre_digest() -> None:
    # Pre-digest state: render tree exists, no events.jsonl yet.
    _write_root_json({"version": "old"})
    if _events_path().exists():
        _events_path().unlink()

    backup = RedigestBackup(ROOT_ID).capture()
    # Re-digest creates events.jsonl and mutates the tree.
    _write_root_json({"version": "new"})
    _events_path().parent.mkdir(parents=True, exist_ok=True)
    _events_path().write_text("NEWLINE\n", encoding="utf-8")

    backup.rollback()

    assert _read_root_json() == {"version": "old"}
    assert not _events_path().exists(), "absent pre-digest state must be restored"


def test_rollback_resets_event_ingester_handle() -> None:
    _write_root_json({"version": "old"})
    _events_path().parent.mkdir(parents=True, exist_ok=True)
    _events_path().write_text('{"uid":"u1"}\n', encoding="utf-8")
    # Force the ingester to open a handle + seed dedup state for this root.
    event_ingester._ensure_open(ROOT_ID)
    assert ROOT_ID in event_ingester._handles

    RedigestBackup(ROOT_ID).capture().rollback()

    assert ROOT_ID not in event_ingester._handles, "ingester handle must be closed"
    assert ROOT_ID not in event_ingester._seen_uuids


def test_reload_root_from_disk_evicts_cache() -> None:
    # Load the root into the manager's in-memory cache, then evict.
    _write_root_json({"id": ROOT_ID, "kind": "team", "messages": []})
    loaded = session_manager._load_root(ROOT_ID)
    assert loaded is not None
    assert ROOT_ID in session_manager._roots

    session_manager.reload_root_from_disk(ROOT_ID)

    assert ROOT_ID not in session_manager._roots, "cache must be evicted"
    assert ROOT_ID not in _sm_module._persist_pending


def main() -> int:
    test_rollback_restores_both_files()
    test_commit_keeps_new_state_drops_backup()
    test_rollback_deletes_events_file_when_absent_pre_digest()
    test_rollback_resets_event_ingester_handle()
    test_reload_root_from_disk_evicts_cache()
    print("PASS: redigest backup / rollback")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
