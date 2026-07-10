#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
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
    path = redigest_backup._root_json_path(ROOT_ID)
    return any(path.parent.glob(path.name + redigest_backup._BAK_SUFFIX + ".*"))


def _write_root(root_id: str, version: str) -> None:
    path = redigest_backup._root_json_path(root_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": version}), encoding="utf-8")


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


def test_same_root_transactions_serialize_and_rollback_is_isolated() -> None:
    _write_root_json({"version": "old"})
    first = RedigestBackup(ROOT_ID).capture()
    _write_root_json({"version": "committed"})
    acquired = threading.Event()
    errors: list[BaseException] = []

    def second_transaction() -> None:
        try:
            second = RedigestBackup(ROOT_ID).capture()
            acquired.set()
            _write_root_json({"version": "failed"})
            second.rollback()
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=second_transaction)
    thread.start()
    time.sleep(0.1)
    assert not acquired.is_set(), "same-root capture must wait for the active transaction"
    first.commit()
    assert acquired.wait(2.0)
    thread.join(2.0)
    assert not thread.is_alive()
    assert not errors
    assert _read_root_json() == {"version": "committed"}
    assert not _bak_exists()


def test_atomic_copy_temp_names_do_not_collide_in_one_pid() -> None:
    source = redigest_backup._root_json_path(ROOT_ID).with_name("copy-source")
    destination = source.with_name("copy-destination")
    source.write_bytes(b"snapshot")
    errors: list[BaseException] = []

    def copy(token: str) -> None:
        try:
            redigest_backup._atomic_copy(source, destination, token)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=copy, args=(f"token-{i}",)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(2.0)
    assert not errors
    assert destination.read_bytes() == b"snapshot"
    assert not list(source.parent.glob(f".{destination.name}.*.tmp"))


def test_capture_cleans_crash_residuals_under_transaction_lock() -> None:
    _write_root_json({"version": "old"})
    live = redigest_backup._root_json_path(ROOT_ID)
    stale_backup = live.with_name(live.name + redigest_backup._BAK_SUFFIX + ".stale")
    stale_tmp = live.with_name(f".{stale_backup.name}.tmp")
    stale_backup.write_bytes(b"stale")
    stale_tmp.write_bytes(b"stale")
    backup = RedigestBackup(ROOT_ID).capture()
    try:
        assert not stale_backup.exists()
        assert not stale_tmp.exists()
    finally:
        backup.commit()


def test_cross_process_same_root_transaction_waits() -> None:
    _write_root_json({"version": "cross-process"})
    first = RedigestBackup(ROOT_ID).capture()
    code = (
        "import sys; "
        f"sys.path.insert(0, {str(ROOT / 'backend')!r}); "
        "from redigest_backup import RedigestBackup; "
        f"b=RedigestBackup({ROOT_ID!r}).capture(); "
        "print('ACQUIRED', flush=True); b.commit()"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=os.environ.copy(),
    )
    try:
        try:
            proc.communicate(timeout=0.2)
            raise AssertionError("child acquired a cross-process root lock before settle")
        except subprocess.TimeoutExpired:
            pass
        first.commit()
        stdout, stderr = proc.communicate(timeout=5.0)
        assert proc.returncode == 0, stderr
        assert stdout.strip() == "ACQUIRED"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()
        if not first._settled:
            first.commit()


def test_distinct_root_transactions_overlap() -> None:
    root_a = "root-overlap-a"
    root_b = "root-overlap-b"
    _write_root(root_a, "a")
    _write_root(root_b, "b")
    first = RedigestBackup(root_a).capture()
    acquired = threading.Event()
    errors: list[BaseException] = []

    def second_transaction() -> None:
        try:
            second = RedigestBackup(root_b).capture()
            acquired.set()
            second.commit()
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=second_transaction)
    thread.start()
    try:
        assert acquired.wait(2.0), "distinct roots must retain recovery parallelism"
        thread.join(2.0)
        assert not errors
    finally:
        first.commit()


def main() -> int:
    test_rollback_restores_both_files()
    test_commit_keeps_new_state_drops_backup()
    test_rollback_deletes_events_file_when_absent_pre_digest()
    test_rollback_resets_event_ingester_handle()
    test_reload_root_from_disk_evicts_cache()
    test_same_root_transactions_serialize_and_rollback_is_isolated()
    test_atomic_copy_temp_names_do_not_collide_in_one_pid()
    test_capture_cleans_crash_residuals_under_transaction_lock()
    test_cross_process_same_root_transaction_waits()
    test_distinct_root_transactions_overlap()
    print("PASS: redigest backup / rollback")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
