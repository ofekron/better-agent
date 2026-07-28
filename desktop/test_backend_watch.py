from __future__ import annotations

import threading

from backend_watch import watch_backend


class FakeSupervisor:
    def __init__(
        self,
        *,
        exit_codes: list[int],
        requested: list[bool],
        recovery: list[bool],
        restart_error: OSError | None = None,
    ) -> None:
        self.exit_codes = iter(exit_codes)
        self.requested = iter(requested)
        self.recovery = iter(recovery)
        self.restart_error = restart_error
        self.refresh_records: list[int] = []
        self.refresh_restarts = 0
        self.crash_exits: list[int] = []

    def wait_exit(self) -> int:
        return next(self.exit_codes)

    def restart_was_requested(self) -> bool:
        if self.restart_error is not None:
            raise self.restart_error
        return next(self.requested)

    def record_requested_restart(self, exit_code: int) -> None:
        self.refresh_records.append(exit_code)

    def restart(self) -> bool:
        self.refresh_restarts += 1
        return next(self.recovery)

    def recover_unexpected_exit(self, exit_code: int) -> bool:
        self.crash_exits.append(exit_code)
        return next(self.recovery)


class FakeWindow:
    def __init__(self) -> None:
        self.loaded: list[str] = []
        self.destroyed = False

    def load_url(self, url: str) -> None:
        self.loaded.append(url)

    def destroy(self) -> None:
        self.destroyed = True


def test_unexpected_exit_recovers_without_destroying_window() -> None:
    supervisor = FakeSupervisor(
        exit_codes=[-9, 0],
        requested=[False],
        recovery=[True],
    )
    window = FakeWindow()
    quitting = threading.Event()

    def stop_after_reload(url: str) -> None:
        window.loaded.append(url)
        quitting.set()

    window.load_url = stop_after_reload
    watch_backend(supervisor, window, quitting, "http://localhost")

    assert supervisor.crash_exits == [-9]
    assert window.loaded == ["http://localhost"]
    assert window.destroyed is False


def test_refresh_restarts_once_and_records_exit() -> None:
    supervisor = FakeSupervisor(
        exit_codes=[0, 0],
        requested=[True],
        recovery=[True],
    )
    window = FakeWindow()
    quitting = threading.Event()

    def stop_after_reload(url: str) -> None:
        window.loaded.append(url)
        quitting.set()

    window.load_url = stop_after_reload
    watch_backend(supervisor, window, quitting, "http://localhost")

    assert supervisor.refresh_records == [0]
    assert supervisor.refresh_restarts == 1
    assert supervisor.crash_exits == []
    assert window.destroyed is False


def test_failed_refresh_uses_bounded_recovery() -> None:
    supervisor = FakeSupervisor(
        exit_codes=[0, 0],
        requested=[True],
        recovery=[False, True],
    )
    window = FakeWindow()
    quitting = threading.Event()

    def stop_after_reload(url: str) -> None:
        window.loaded.append(url)
        quitting.set()

    window.load_url = stop_after_reload
    watch_backend(supervisor, window, quitting, "http://localhost")

    assert supervisor.refresh_records == [0]
    assert supervisor.refresh_restarts == 1
    assert supervisor.crash_exits == [0]
    assert window.loaded == ["http://localhost"]
    assert window.destroyed is False


def test_quit_never_restarts_or_destroys_window() -> None:
    supervisor = FakeSupervisor(
        exit_codes=[0],
        requested=[],
        recovery=[],
    )
    window = FakeWindow()
    quitting = threading.Event()
    quitting.set()

    watch_backend(supervisor, window, quitting, "http://localhost")

    assert supervisor.refresh_restarts == 0
    assert supervisor.crash_exits == []
    assert window.destroyed is False


def test_open_circuit_destroys_window() -> None:
    supervisor = FakeSupervisor(
        exit_codes=[7],
        requested=[False],
        recovery=[False],
    )
    window = FakeWindow()

    watch_backend(
        supervisor,
        window,
        threading.Event(),
        "http://localhost",
    )

    assert supervisor.crash_exits == [7]
    assert window.loaded == []
    assert window.destroyed is True


def test_unreadable_restart_intent_fails_closed() -> None:
    supervisor = FakeSupervisor(
        exit_codes=[0],
        requested=[],
        recovery=[],
        restart_error=OSError("cannot consume restart intent"),
    )
    window = FakeWindow()

    watch_backend(
        supervisor,
        window,
        threading.Event(),
        "http://localhost",
    )

    assert supervisor.refresh_restarts == 0
    assert supervisor.crash_exits == []
    assert window.destroyed is True


def test_recovery_exception_fails_closed() -> None:
    supervisor = FakeSupervisor(
        exit_codes=[7],
        requested=[False],
        recovery=[],
    )
    window = FakeWindow()

    watch_backend(
        supervisor,
        window,
        threading.Event(),
        "http://localhost",
    )

    assert supervisor.crash_exits == [7]
    assert window.destroyed is True
