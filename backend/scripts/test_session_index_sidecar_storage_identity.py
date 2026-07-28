from __future__ import annotations

import json
import shutil
import tempfile
import threading
from pathlib import Path

import _test_home

_PRIMARY_HOME = Path(_test_home.isolate("ba-test-index-identity-primary-"))

import session_store  # noqa: E402


def _payload(label: str) -> tuple:
    return (1, 0, 0, len(label), 1), {label: label}, {}, {}


def _schedule(identity: Path, label: str) -> None:
    with session_store._storage_identity_scope(identity):
        session_store._schedule_index_sidecar_write(*_payload(label))


def main() -> int:
    secondary_home = Path(tempfile.mkdtemp(prefix="ba-test-index-identity-secondary-"))
    primary_identity = (_PRIMARY_HOME / "sessions").resolve()
    secondary_identity = (secondary_home / "sessions").resolve()
    primary_identity.mkdir(parents=True)
    secondary_identity.mkdir(parents=True)
    original_start = session_store._start_index_sidecar_writer_unlocked
    original_write = session_store._write_index_sidecar_best_effort
    first_write_started = threading.Event()
    release_first_write = threading.Event()
    writes: list[tuple[Path, str]] = []
    write_count = 0
    shutdown: threading.Thread | None = None

    def tracked_write(fp, fork_index, root_forks, root_signatures):
        nonlocal write_count
        write_count += 1
        if write_count == 1:
            first_write_started.set()
            assert release_first_write.wait(timeout=2.0)
        writes.append((session_store._sessions_dir(), next(iter(fork_index))))

    session_store._start_index_sidecar_writer_unlocked = lambda: None
    session_store._write_index_sidecar_best_effort = tracked_write
    try:
        _schedule(primary_identity, "primary-old")
        _schedule(secondary_identity, "secondary")
        session_store._start_index_sidecar_writer_unlocked = original_start
        with session_store._index_sidecar_write_lock:
            original_start()
        assert first_write_started.wait(timeout=2.0)
        _schedule(primary_identity, "primary-latest")
        release_first_write.set()
        session_store._index_sidecar_write_queue.join()

        assert writes == [
            (primary_identity, "primary-old"),
            (secondary_identity, "secondary"),
            (primary_identity, "primary-latest"),
        ], writes

        session_store.shutdown_durability_writer()
        session_store._write_index_sidecar_best_effort = original_write
        session_store._start_index_sidecar_writer_unlocked = lambda: None
        _schedule(primary_identity, "primary-real")
        _schedule(secondary_identity, "secondary-real")
        session_store._start_index_sidecar_writer_unlocked = original_start
        with session_store._index_sidecar_write_lock:
            original_start()
        session_store.shutdown_durability_writer()
        primary_payload = json.loads(
            (primary_identity / ".fork-index.json").read_text(encoding="utf-8")
        )
        secondary_payload = json.loads(
            (secondary_identity / ".fork-index.json").read_text(encoding="utf-8")
        )
        assert primary_payload["fork_index"] == {"primary-real": "primary-real"}
        assert secondary_payload["fork_index"] == {"secondary-real": "secondary-real"}

        session_store._write_index_sidecar_best_effort = tracked_write
        first_write_started.clear()
        release_first_write.clear()
        write_count = 0
        _schedule(primary_identity, "shutdown")
        assert first_write_started.wait(timeout=2.0)
        shutdown = threading.Thread(target=session_store.shutdown_durability_writer)
        shutdown.start()
        with session_store._index_sidecar_write_lock:
            assert session_store._index_sidecar_write_lock.wait_for(
                lambda: not session_store._index_sidecar_accepting,
                timeout=2.0,
            )
        pending = dict(session_store._index_sidecar_pending)
        signaled = set(session_store._index_sidecar_signaled)
        try:
            _schedule(primary_identity, "rejected")
            raise AssertionError("enqueue during shutdown was accepted")
        except RuntimeError:
            pass
        assert session_store._index_sidecar_pending == pending
        assert session_store._index_sidecar_signaled == signaled
        release_first_write.set()
        shutdown.join(timeout=2.0)
        assert not shutdown.is_alive()
        assert session_store._index_sidecar_accepting
        assert session_store._index_sidecar_thread is None
        assert not session_store._index_sidecar_pending
        assert not session_store._index_sidecar_signaled
        assert session_store._index_sidecar_write_queue.unfinished_tasks == 0

        _schedule(primary_identity, "restart")
        session_store.shutdown_durability_writer()
        assert writes[-1] == (primary_identity, "restart")
        print("PASS session index sidecar storage identity")
        return 0
    finally:
        release_first_write.set()
        if shutdown is not None:
            shutdown.join(timeout=2.0)
            assert not shutdown.is_alive()
        session_store._start_index_sidecar_writer_unlocked = original_start
        session_store._write_index_sidecar_best_effort = original_write
        if session_store._index_sidecar_accepting:
            session_store.shutdown_durability_writer()
        shutil.rmtree(secondary_home, ignore_errors=True)
        shutil.rmtree(_PRIMARY_HOME, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
