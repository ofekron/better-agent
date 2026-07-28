from __future__ import annotations

import shutil
import tempfile
import threading
from pathlib import Path

import _test_home

_PRIMARY_HOME = Path(_test_home.isolate("ba-test-storage-identity-primary-"))

import paths  # noqa: E402
import session_store  # noqa: E402


class _InstrumentedLock:
    def __init__(self, scope_attempted: threading.Event, events: list[str]) -> None:
        self._lock = threading.Lock()
        self._scope_attempted = scope_attempted
        self._events = events

    def __enter__(self):
        name = threading.current_thread().name
        self._events.append(f"{name}:attempt")
        if name == "identity-scope":
            self._scope_attempted.set()
        self._lock.acquire()
        self._events.append(f"{name}:acquired")
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        name = threading.current_thread().name
        self._events.append(f"{name}:released")
        self._lock.release()


def _assert_cache_transition_is_lease_atomic(
    primary_identity: Path,
    secondary_home: Path,
    secondary_identity: Path,
) -> None:
    original_lock = session_store._STORAGE_IDENTITY_LEASE_LOCK
    original_reset = session_store._reset_home_scoped_caches
    reset_entered = threading.Event()
    release_reset = threading.Event()
    scope_attempted = threading.Event()
    events: list[str] = []
    errors: list[BaseException] = []
    instrumented = _InstrumentedLock(scope_attempted, events)

    def blocked_reset() -> None:
        events.append("reset:entered")
        reset_entered.set()
        assert release_reset.wait(timeout=2.0)
        original_reset()
        events.append("reset:completed")

    def switch_dynamic_home() -> None:
        try:
            assert session_store._sessions_dir() == secondary_identity
        except BaseException as exc:
            errors.append(exc)

    def enter_primary_scope() -> None:
        try:
            with session_store._storage_identity_scope(primary_identity):
                events.append("scope:entered")
        except BaseException as exc:
            errors.append(exc)

    session_store._STORAGE_IDENTITY_LEASE_LOCK = instrumented
    session_store._reset_home_scoped_caches = blocked_reset
    paths.engage_test_home(str(secondary_home))
    dynamic_thread = threading.Thread(
        target=switch_dynamic_home,
        name="dynamic-switch",
    )
    scope_thread = threading.Thread(
        target=enter_primary_scope,
        name="identity-scope",
    )
    dynamic_thread.start()
    try:
        assert reset_entered.wait(timeout=2.0)
        scope_thread.start()
        assert scope_attempted.wait(timeout=2.0)
        assert "identity-scope:acquired" not in events
        release_reset.set()
        dynamic_thread.join(timeout=2.0)
        scope_thread.join(timeout=2.0)
        assert not dynamic_thread.is_alive()
        assert not scope_thread.is_alive()
        assert not errors
        assert events.index("reset:completed") < events.index("identity-scope:acquired")
    finally:
        release_reset.set()
        dynamic_thread.join(timeout=2.0)
        if scope_thread.ident is not None:
            scope_thread.join(timeout=2.0)
        assert not dynamic_thread.is_alive()
        assert not scope_thread.is_alive()
        session_store._reset_home_scoped_caches = original_reset
        session_store._STORAGE_IDENTITY_LEASE_LOCK = original_lock


def main() -> int:
    secondary_home = Path(tempfile.mkdtemp(prefix="ba-test-storage-identity-secondary-"))
    primary_identity = (_PRIMARY_HOME / "sessions").resolve()
    secondary_identity = (secondary_home / "sessions").resolve()
    entered = threading.Event()
    release = threading.Event()
    errors: list[BaseException] = []

    def hold_primary() -> None:
        try:
            with session_store._storage_identity_scope(primary_identity):
                assert session_store._sessions_dir() == primary_identity
                assert session_store._root_file_path("root").parent == primary_identity
                assert session_store._fork_index_path().parent == primary_identity
                with session_store._storage_identity_scope(primary_identity):
                    assert session_store._sessions_dir() == primary_identity
                entered.set()
                assert release.wait(timeout=2.0)
                assert session_store._sessions_dir() == primary_identity
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=hold_primary)
    worker.start()
    try:
        assert entered.wait(timeout=2.0)
        with session_store._storage_identity_scope(primary_identity):
            assert session_store._active_storage_identity == primary_identity
            assert session_store._active_storage_identity_leases == 2
        assert session_store._active_storage_identity_leases == 1
        paths.engage_test_home(str(secondary_home))
        before = session_store._SESSIONS_DIR
        root_dirs_before = dict(session_store._root_file_dirs)
        try:
            with session_store._storage_identity_scope(secondary_identity):
                raise AssertionError("overlapping storage identity was accepted")
        except RuntimeError:
            pass
        assert session_store._SESSIONS_DIR == before == primary_identity
        assert session_store._root_file_dirs == root_dirs_before
        try:
            session_store._sessions_dir()
            raise AssertionError("dynamic storage identity bypassed active lease")
        except RuntimeError:
            pass
        release.set()
        worker.join(timeout=2.0)
        assert not worker.is_alive()
        assert not errors
        assert session_store._sessions_dir() == secondary_identity
        try:
            with session_store._storage_identity_scope(Path("relative")):
                raise AssertionError("relative storage identity was accepted")
        except ValueError:
            pass
        try:
            with session_store._storage_identity_scope(
                Path.home() / ".better-claude" / "sessions"
            ):
                raise AssertionError("production storage identity was accepted")
        except RuntimeError:
            pass
        try:
            with session_store._storage_identity_scope(secondary_identity):
                raise LookupError("scope body failed")
        except LookupError:
            pass
        assert session_store._STORAGE_IDENTITY.get() is None
        assert session_store._active_storage_identity is None
        assert session_store._active_storage_identity_leases == 0
        paths.engage_test_home(str(_PRIMARY_HOME))
        assert session_store._sessions_dir() == primary_identity
        _assert_cache_transition_is_lease_atomic(
            primary_identity,
            secondary_home,
            secondary_identity,
        )
        with session_store._storage_identity_scope(secondary_identity):
            assert session_store._sessions_dir() == secondary_identity
            try:
                with session_store._storage_identity_scope(primary_identity):
                    raise AssertionError("mismatched nested identity was accepted")
            except RuntimeError:
                pass
            assert session_store._sessions_dir() == secondary_identity
        assert session_store._STORAGE_IDENTITY.get() is None
        assert session_store._active_storage_identity is None
        assert session_store._active_storage_identity_leases == 0
        print("PASS session storage identity scope")
        return 0
    finally:
        release.set()
        worker.join(timeout=2.0)
        assert not worker.is_alive()
        paths.engage_test_home(str(_PRIMARY_HOME))
        session_store._sessions_dir()
        shutil.rmtree(secondary_home, ignore_errors=True)
        shutil.rmtree(_PRIMARY_HOME, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
