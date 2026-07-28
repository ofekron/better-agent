from __future__ import annotations

import json
import shutil
import tempfile
import threading
from pathlib import Path

import _test_home

_PRIMARY_HOME = Path(_test_home.isolate("ba-test-root-change-primary-"))

import paths  # noqa: E402
import session_store  # noqa: E402


def _write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"id": path.stem, "value": value}), encoding="utf-8")


def main() -> int:
    secondary_home = Path(tempfile.mkdtemp(prefix="ba-test-root-change-secondary-"))
    primary_identity = (_PRIMARY_HOME / "sessions").resolve()
    secondary_identity = (secondary_home / "sessions").resolve()
    primary_path = primary_identity / "same-root.json"
    secondary_path = secondary_identity / "same-root.json"
    _write(primary_path, "primary")
    _write(secondary_path, "secondary")
    starter: threading.Thread | None = None
    reader: threading.Thread | None = None
    release_start = threading.Event()
    handle = None
    original_owner_type = session_store.RootChangeOwner

    try:
        session_store.start_root_change_owner()
        binding = session_store._root_change_binding
        assert binding is not None
        assert binding.identity == primary_identity
        assert binding.state == "open"

        paths.engage_test_home(str(secondary_home))
        sessions_dir_before_rejection = session_store._SESSIONS_DIR
        try:
            session_store.start_root_change_owner()
            raise AssertionError("different-identity owner start was accepted")
        except RuntimeError:
            pass
        assert session_store._SESSIONS_DIR == sessions_dir_before_rejection

        generation = binding.owner.observation_generation
        _write(primary_path, "primary-updated")
        assert binding.owner.wait_for_observation(generation, 2.0)
        assert json.loads(secondary_path.read_text(encoding="utf-8"))["value"] == "secondary"

        (primary_identity / "same-root.summary.json").write_text(
            json.dumps({"id": "same-root"}),
            encoding="utf-8",
        )
        generation = binding.owner.observation_generation
        primary_path.unlink()
        assert binding.owner.wait_for_observation(generation, 2.0)
        assert not (primary_identity / "same-root.summary.json").exists()
        assert secondary_path.exists()
        assert not (secondary_home / "indexes" / "root-changes.sqlite3").exists()

        _write(primary_path, "local")
        with session_store._storage_identity_scope(primary_identity):
            rows_before = len(binding.owner._wal.read_after(0, 10_000))
            try:
                session_store._begin_root_change(
                    "upsert",
                    "same-root",
                    secondary_path,
                )
                raise AssertionError("cross-identity local path was accepted")
            except ValueError:
                pass
            symlink_path = primary_identity / "escaped-root.json"
            symlink_path.symlink_to(secondary_path)
            try:
                session_store._begin_root_change(
                    "upsert",
                    "escaped-root",
                    symlink_path,
                )
                raise AssertionError("symlink escape was accepted")
            except ValueError:
                pass
            assert len(binding.owner._wal.read_after(0, 10_000)) == rows_before
            assert binding.active_handles == 0
            handle = session_store._begin_root_change(
                "upsert",
                "same-root",
                primary_path,
            )
        assert handle is not None

        try:
            session_store.shutdown_root_change_owner(timeout=0.01)
            raise AssertionError("active local change did not bound shutdown")
        except TimeoutError:
            pass
        assert session_store._root_change_binding is binding
        assert binding.state == "closing"
        assert binding.active_handles == 1
        with session_store._storage_identity_scope(primary_identity):
            assert session_store._root_change_binding_for_read() is None
            try:
                session_store._begin_root_change(
                    "upsert",
                    "same-root",
                    primary_path,
                )
                raise AssertionError("local change was admitted during shutdown")
            except RuntimeError:
                pass
        session_store._complete_root_change(handle)
        try:
            session_store._complete_root_change(handle)
            raise AssertionError("root change handle was consumed twice")
        except RuntimeError:
            pass
        assert binding.active_handles == 0
        session_store.shutdown_root_change_owner()
        assert session_store._root_change_binding is None
        assert (_PRIMARY_HOME / "indexes" / "root-changes.sqlite3").exists()

        paths.engage_test_home(str(secondary_home))
        session_store.start_root_change_owner()
        secondary_binding = session_store._root_change_binding
        assert secondary_binding is not None
        assert secondary_binding.identity == secondary_identity
        original_stop = secondary_binding.owner.stop

        def fail_stop(timeout=5.0):
            raise RuntimeError("injected stop failure")

        secondary_binding.owner.stop = fail_stop
        try:
            try:
                session_store.shutdown_root_change_owner()
                raise AssertionError("stop failure was hidden")
            except RuntimeError as exc:
                assert str(exc) == "injected stop failure"
        finally:
            secondary_binding.owner.stop = original_stop
        assert session_store._root_change_binding is secondary_binding
        assert secondary_binding.state == "closing"
        session_store.shutdown_root_change_owner()
        assert (secondary_home / "indexes" / "root-changes.sqlite3").exists()

        start_entered = threading.Event()
        stopped_starting_owner = threading.Event()

        class BlockingOwner:
            def __init__(self, **kwargs):
                pass

            def start(self):
                start_entered.set()
                assert release_start.wait(timeout=2.0)

            def wait_ready(self, timeout=None):
                return None

            def stop(self, timeout=5.0):
                stopped_starting_owner.set()

        session_store.RootChangeOwner = BlockingOwner
        paths.engage_test_home(str(_PRIMARY_HOME))
        starter = threading.Thread(
            target=session_store.start_root_change_owner,
            name="test-root-change-start",
        )
        starter.start()
        assert start_entered.wait(timeout=2.0)
        starting_binding = session_store._root_change_binding
        assert starting_binding is not None
        assert starting_binding.state == "starting"
        reader_started = threading.Event()
        read_bindings = []

        def read_binding() -> None:
            reader_started.set()
            read_bindings.append(session_store._root_change_binding_for_read())

        reader = threading.Thread(
            target=read_binding,
            name="test-root-change-read-during-start",
        )
        reader.start()
        assert reader_started.wait(timeout=2.0)
        try:
            session_store._begin_root_change("upsert", "same-root", primary_path)
            raise AssertionError("local change was admitted during startup")
        except RuntimeError:
            pass
        release_start.set()
        starter.join(timeout=2.0)
        assert not starter.is_alive()
        reader.join(timeout=2.0)
        assert not reader.is_alive()
        assert session_store._root_change_binding is starting_binding
        assert starting_binding.state == "open"
        assert read_bindings == [starting_binding]
        session_store.shutdown_root_change_owner()
        assert stopped_starting_owner.is_set()

        stopped = threading.Event()

        class FailingOwner:
            def __init__(self, **kwargs):
                pass

            def start(self):
                raise RuntimeError("injected startup failure")

            def stop(self, timeout=5.0):
                stopped.set()

        session_store.RootChangeOwner = FailingOwner
        try:
            session_store.start_root_change_owner()
            raise AssertionError("startup failure was hidden")
        except RuntimeError as exc:
            assert str(exc) == "injected startup failure"
        assert stopped.is_set()
        assert session_store._root_change_binding is None

        class FailingStartAndStopOwner:
            def __init__(self, **kwargs):
                pass

            def start(self):
                raise RuntimeError("injected start failure")

            def stop(self, timeout=5.0):
                raise RuntimeError("injected cleanup failure")

        session_store.RootChangeOwner = FailingStartAndStopOwner
        try:
            session_store.start_root_change_owner()
            raise AssertionError("startup cleanup failure was hidden")
        except RuntimeError as exc:
            assert str(exc) == "injected cleanup failure"
        failed_cleanup_binding = session_store._root_change_binding
        assert failed_cleanup_binding is not None
        assert failed_cleanup_binding.state == "closing"
        failed_cleanup_binding.owner.stop = lambda timeout=5.0: None
        session_store.shutdown_root_change_owner()
        assert session_store._root_change_binding is None
        print("PASS session root change storage identity")
        return 0
    finally:
        release_start.set()
        if starter is not None:
            starter.join(timeout=2.0)
            assert not starter.is_alive()
        if reader is not None:
            reader.join(timeout=2.0)
            assert not reader.is_alive()
        session_store.RootChangeOwner = original_owner_type
        if handle is not None and not handle.consumed:
            session_store._abandon_root_change(handle)
        binding = session_store._root_change_binding
        if binding is not None:
            session_store.shutdown_root_change_owner()
        paths.engage_test_home(str(_PRIMARY_HOME))
        session_store._sessions_dir()
        shutil.rmtree(secondary_home, ignore_errors=True)
        shutil.rmtree(_PRIMARY_HOME, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
