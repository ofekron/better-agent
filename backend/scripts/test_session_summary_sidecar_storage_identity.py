from __future__ import annotations

import json
import shutil
import tempfile
import threading
from pathlib import Path
from queue import Empty

import _test_home

_PRIMARY_HOME = Path(_test_home.isolate("ba-test-summary-identity-primary-"))

import paths  # noqa: E402
import session_store  # noqa: E402


def _write_root(identity: Path, root_id: str) -> None:
    identity.mkdir(parents=True, exist_ok=True)
    (identity / f"{root_id}.json").write_text(
        json.dumps({"id": root_id}),
        encoding="utf-8",
    )


def _summary_name(identity: Path, root_id: str) -> str:
    payload = json.loads(
        (identity / f"{root_id}.summary.json").read_text(encoding="utf-8")
    )
    return str(payload["name"])


def main() -> int:
    secondary_home = Path(tempfile.mkdtemp(prefix="ba-test-summary-identity-secondary-"))
    primary_identity = (_PRIMARY_HOME / "sessions").resolve()
    secondary_identity = (secondary_home / "sessions").resolve()
    root_id = "same-root"
    _write_root(primary_identity, root_id)
    _write_root(secondary_identity, root_id)
    original_ensure = session_store._ensure_summary_sidecar_writer
    condition = session_store._STORAGE_IDENTITY_LEASE_LOCK
    original_wait = condition.wait
    wait_entered = threading.Event()
    worker_errors: list[BaseException] = []
    worker: threading.Thread | None = None

    def observed_wait(timeout=None):
        wait_entered.set()
        return original_wait(timeout)

    def process_one() -> None:
        try:
            first = session_store._summary_sidecar_write_queue.get_nowait()
            if first is None:
                session_store._summary_sidecar_write_queue.task_done()
                raise AssertionError("unexpected summary writer sentinel")
            session_store._process_summary_sidecar_batch(first)
        except BaseException as exc:
            worker_errors.append(exc)

    def drain_remaining() -> None:
        while True:
            try:
                first = session_store._summary_sidecar_write_queue.get_nowait()
            except Empty:
                return
            if first is None:
                session_store._summary_sidecar_write_queue.task_done()
                continue
            session_store._process_summary_sidecar_batch(first)

    session_store._ensure_summary_sidecar_writer = lambda: None
    try:
        session_store._schedule_summary_sidecar_write(
            primary_identity, root_id, {"name": "primary-old"},
        )
        session_store._schedule_summary_sidecar_write(
            primary_identity, root_id, {"name": "primary-latest"},
        )
        session_store._schedule_summary_sidecar_write(
            secondary_identity, root_id, {"name": "secondary-old"},
        )
        paths.engage_test_home(str(secondary_home))
        first = session_store._summary_sidecar_write_queue.get_nowait()
        assert first is not None
        assert not session_store._process_summary_sidecar_batch(first)
        session_store._summary_sidecar_write_queue.join()
        primary_name = _summary_name(primary_identity, root_id)
        secondary_name = _summary_name(secondary_identity, root_id)
        assert primary_name == "primary-latest", primary_name
        assert secondary_name == "secondary-old", secondary_name

        condition.wait = observed_wait
        with session_store._storage_identity_scope(primary_identity):
            session_store._schedule_summary_sidecar_write(
                secondary_identity, root_id, {"name": "secondary-latest"},
            )
            worker = threading.Thread(target=process_one)
            worker.start()
            assert wait_entered.wait(timeout=2.0)
            assert _summary_name(secondary_identity, root_id) == "secondary-old"
            try:
                session_store._summary_sidecar_write_queue.join()
                raise AssertionError("scoped summary queue join was accepted")
            except RuntimeError:
                pass
        worker.join(timeout=2.0)
        assert not worker.is_alive()
        assert not worker_errors
        session_store._summary_sidecar_write_queue.join()
        assert _summary_name(secondary_identity, root_id) == "secondary-latest"
        assert session_store._summary_sidecar_write_queue.empty()
        assert session_store._summary_sidecar_write_queue.unfinished_tasks == 0
        assert session_store._active_storage_identity is None
        assert session_store._active_storage_identity_leases == 0
        print("PASS session summary sidecar storage identity")
        return 0
    finally:
        condition.wait = original_wait
        session_store._ensure_summary_sidecar_writer = original_ensure
        if worker is not None:
            worker.join(timeout=2.0)
            assert not worker.is_alive()
        drain_remaining()
        session_store._summary_sidecar_write_queue.join()
        paths.engage_test_home(str(_PRIMARY_HOME))
        session_store._sessions_dir()
        shutil.rmtree(secondary_home, ignore_errors=True)
        shutil.rmtree(_PRIMARY_HOME, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
