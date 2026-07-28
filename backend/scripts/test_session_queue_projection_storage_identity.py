from __future__ import annotations

import json
import shutil
import tempfile
import threading
import time
from pathlib import Path

import _test_home

_PRIMARY_HOME = Path(_test_home.isolate("ba-test-queue-projection-primary-"))

import paths  # noqa: E402
import session_queue_projection as projection  # noqa: E402
import session_store  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402
from session_queue_projection_runtime import StorageContext, registry  # noqa: E402


def _write_root(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({
            "id": path.stem,
            "value": value,
            "messages": [],
            "queued_prompts": [{"id": f"prompt-{value}"}],
            "forks": [],
        }),
        encoding="utf-8",
    )


def _wait_for_failure(runtime) -> None:
    deadline = time.monotonic() + 2.0
    while runtime.failure is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert runtime.failure is not None


def main() -> int:
    secondary_home = Path(tempfile.mkdtemp(prefix="ba-test-queue-projection-secondary-"))
    primary_identity = (_PRIMARY_HOME / "sessions").resolve()
    secondary_identity = (secondary_home / "sessions").resolve()
    primary_identity.mkdir(parents=True, exist_ok=True)
    secondary_identity.mkdir(parents=True, exist_ok=True)
    primary_context = projection.storage_context(primary_identity)
    equivalent_context = projection.storage_context(primary_identity)
    primary_runtime = registry.acquire(primary_context)
    assert registry.acquire(equivalent_context) is primary_runtime

    conflicting_context = StorageContext.create(
        primary_identity,
        session_files=lambda: (),
        enumerator_contract="conflicting-enumerator.v1",
    )
    try:
        registry.acquire(conflicting_context)
        raise AssertionError("conflicting same-identity context was accepted")
    except RuntimeError:
        pass

    primary_runtime.upsert({"id": "same", "value": "primary"})
    entered = threading.Event()
    release = threading.Event()
    shutdown_errors: list[BaseException] = []

    def hold_operation() -> None:
        with primary_runtime.operation():
            entered.set()
            assert release.wait(timeout=2.0)

    holder = threading.Thread(target=hold_operation)
    holder.start()
    assert entered.wait(timeout=2.0)

    def close_primary() -> None:
        try:
            registry.shutdown(primary_context, timeout=2.0, certify=True)
        except BaseException as exc:
            shutdown_errors.append(exc)

    closer = threading.Thread(target=close_primary)
    closer.start()
    deadline = time.monotonic() + 2.0
    while primary_runtime.state != "closing" and time.monotonic() < deadline:
        time.sleep(0.01)
    assert primary_runtime.state == "closing"
    try:
        registry.acquire(projection.storage_context(secondary_identity))
        raise AssertionError("secondary identity bound before primary quiesced")
    except RuntimeError:
        pass
    release.set()
    holder.join(timeout=2.0)
    closer.join(timeout=2.0)
    assert not holder.is_alive() and not closer.is_alive()
    assert not shutdown_errors
    assert registry.bound_identity() is None

    secondary_context = projection.storage_context(secondary_identity)
    secondary_runtime = registry.acquire(secondary_context)
    assert secondary_runtime.get("same") is None
    secondary_runtime.upsert({"id": "same", "value": "secondary"})
    assert secondary_runtime.get("same")["value"] == "secondary"

    secondary_runtime.upsert({"id": "ordered", "value": "old"})
    old_sequence = secondary_runtime.debug_snapshot()["sequence"]
    secondary_runtime.delete(("ordered",))
    secondary_runtime._write_batch({
        "ordered": (old_sequence, {"id": "ordered", "value": "resurrected"}),
    })
    assert secondary_runtime.get("ordered") is None

    secondary_runtime.upsert({"id": "newer-live", "value": "new"})
    newer_sequence = secondary_runtime.debug_snapshot()["sequence"]
    secondary_runtime._write_rebuild(
        newer_sequence - 1,
        {"newer-live": {"id": "newer-live", "value": "stale"}},
        {},
    )
    registry.shutdown(secondary_context, timeout=2.0)
    secondary_runtime = registry.acquire(secondary_context)
    assert secondary_runtime.get("ordered") is None
    assert secondary_runtime.get("newer-live")["value"] == "new"

    original_write_batch = secondary_runtime._write_batch
    failed_once = False

    def fail_once(batch):
        nonlocal failed_once
        if not failed_once:
            failed_once = True
            raise RuntimeError("injected writer failure")
        return original_write_batch(batch)

    secondary_runtime._write_batch = fail_once
    secondary_runtime.upsert({"id": "retry", "value": 1}, durable=False)
    _wait_for_failure(secondary_runtime)
    secondary_runtime._write_batch = original_write_batch
    secondary_runtime.retry_failed_writes()
    assert secondary_runtime.flush(timeout=2.0)
    assert secondary_runtime.get("retry")["value"] == 1
    registry.shutdown(secondary_context, timeout=2.0)

    secondary_runtime = registry.acquire(secondary_context)
    original_load_candidate = secondary_runtime._load_candidate
    load_entered = threading.Event()
    release_load = threading.Event()

    def paused_load(epoch):
        load_entered.set()
        assert release_load.wait(timeout=2.0)
        return original_load_candidate(epoch)

    secondary_runtime._load_candidate = paused_load
    started = time.monotonic()
    projection.upsert_record_background(
        {"id": "nonblocking", "value": 1},
        storage_identity=secondary_identity,
    )
    assert time.monotonic() - started < 0.1
    assert load_entered.wait(timeout=2.0)
    projection.upsert_record_background(
        {"id": "nonblocking", "value": 2},
        storage_identity=secondary_identity,
    )
    assert projection.dispatcher(secondary_identity).overlay(
        ("nonblocking",),
    )["nonblocking"]["value"] == 2
    release_load.set()
    projection.dispatcher(secondary_identity).drain(timeout=2.0)
    assert secondary_runtime.get("nonblocking")["value"] == 2
    secondary_runtime._load_candidate = original_load_candidate
    projection.shutdown(storage_identity=secondary_identity, timeout=2.0)

    writer_entered = threading.Event()
    release_writer = threading.Event()
    writer_errors: list[BaseException] = []
    root = {
        "id": "producer-race",
        "messages": [],
        "queued_prompts": [{"id": "producer-prompt"}],
        "forks": [],
    }

    def paused_writer() -> None:
        try:
            with projection.producer_lease(secondary_identity):
                (secondary_identity / "producer-race.json").write_text(
                    json.dumps(root),
                    encoding="utf-8",
                )
                writer_entered.set()
                assert release_writer.wait(timeout=2.0)
                projection.note_persisted_tree(
                    root,
                    storage_identity=secondary_identity,
                )
        except BaseException as exc:
            writer_errors.append(exc)

    writer = threading.Thread(target=paused_writer)
    writer.start()
    assert writer_entered.wait(timeout=2.0)
    close_errors: list[BaseException] = []

    def close_projection() -> None:
        try:
            projection.shutdown(
                storage_identity=secondary_identity,
                timeout=2.0,
                certify=True,
            )
        except BaseException as exc:
            close_errors.append(exc)

    closer = threading.Thread(target=close_projection)
    closer.start()
    time.sleep(0.05)
    assert closer.is_alive()
    release_writer.set()
    writer.join(timeout=2.0)
    closer.join(timeout=2.0)
    assert not writer_errors and not close_errors
    assert not writer.is_alive() and not closer.is_alive()
    rebound = projection.runtime(secondary_identity)
    assert rebound.projection_is_current()
    assert rebound.get("producer-race")["queued_prompts"] == [
        {"id": "producer-prompt"},
    ]
    projection.shutdown(storage_identity=secondary_identity, timeout=2.0)

    routine_path = (
        secondary_home / "routine-sessions" / "routine-a" / "routine-root.json"
    )
    _write_root(routine_path, "routine")
    secondary_runtime = registry.acquire(secondary_context)
    assert secondary_runtime.rebuild(projection._project_root) == 2
    assert secondary_runtime.flush(timeout=2.0)
    assert secondary_runtime.get("routine-root") is not None
    registry.shutdown(secondary_context, timeout=2.0, certify=True)
    assert registry.bound_identity() is None

    delete_home = Path(tempfile.mkdtemp(prefix="ba-test-queue-projection-delete-"))
    paths.engage_test_home(str(delete_home))
    delete_identity = (delete_home / "sessions").resolve()
    deleted = session_manager.create(
        name="delete-race",
        model="test",
        cwd="/tmp",
        orchestration_mode="native",
        source="cli",
    )
    delete_sid = deleted["id"]
    session_manager.add_queued_prompt(delete_sid, {"id": "delete-prompt"})
    session_manager.flush_pending_persists()
    projection.flush_pending_writes(
        timeout=2.0,
        storage_identity=delete_identity,
    )
    original_delete_records = projection.delete_records
    delete_entered = threading.Event()
    release_delete = threading.Event()
    delete_errors: list[BaseException] = []

    def paused_delete_records(session_ids, *, storage_identity=None):
        delete_entered.set()
        assert release_delete.wait(timeout=2.0)
        return original_delete_records(
            session_ids,
            storage_identity=storage_identity,
        )

    projection.delete_records = paused_delete_records

    def delete_session() -> None:
        try:
            assert session_manager.delete(delete_sid)
        except BaseException as exc:
            delete_errors.append(exc)

    delete_thread = threading.Thread(target=delete_session)
    delete_thread.start()
    assert delete_entered.wait(timeout=2.0)
    assert not Path(session_store.session_file_path(delete_sid)).exists()
    delete_close_errors: list[BaseException] = []

    def close_deleted_projection() -> None:
        try:
            projection.shutdown(
                storage_identity=delete_identity,
                timeout=2.0,
                certify=True,
            )
        except BaseException as exc:
            delete_close_errors.append(exc)

    delete_closer = threading.Thread(target=close_deleted_projection)
    delete_closer.start()
    time.sleep(0.05)
    assert delete_closer.is_alive()
    release_delete.set()
    delete_thread.join(timeout=2.0)
    delete_closer.join(timeout=2.0)
    projection.delete_records = original_delete_records
    assert not delete_errors and not delete_close_errors
    assert not delete_thread.is_alive() and not delete_closer.is_alive()
    assert projection.runtime(delete_identity).get(delete_sid) is None
    projection.shutdown(storage_identity=delete_identity, timeout=2.0)

    print("PASS session queue projection storage identity")
    shutil.rmtree(delete_home, ignore_errors=True)
    shutil.rmtree(secondary_home, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
