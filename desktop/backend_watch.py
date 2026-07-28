from __future__ import annotations

import threading
from typing import Protocol


class BackendLifecycle(Protocol):
    def wait_exit(self) -> int: ...
    def restart_was_requested(self) -> bool: ...
    def record_requested_restart(self, exit_code: int) -> None: ...
    def restart(self) -> bool: ...
    def recover_unexpected_exit(self, exit_code: int) -> bool: ...


class WindowLifecycle(Protocol):
    def load_url(self, url: str) -> None: ...
    def destroy(self) -> None: ...


def watch_backend(
    supervisor: BackendLifecycle,
    window: WindowLifecycle,
    quitting: threading.Event,
    local_url: str,
) -> None:
    while True:
        exit_code = supervisor.wait_exit()
        if quitting.is_set():
            return
        try:
            restart_requested = supervisor.restart_was_requested()
        except OSError:
            window.destroy()
            return
        try:
            if restart_requested:
                supervisor.record_requested_restart(exit_code)
                recovered = supervisor.restart()
                if not recovered:
                    recovered = supervisor.recover_unexpected_exit(exit_code)
            else:
                recovered = supervisor.recover_unexpected_exit(exit_code)
        except Exception:
            window.destroy()
            return
        if recovered:
            window.load_url(local_url)
            continue
        window.destroy()
        return
