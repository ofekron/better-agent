from __future__ import annotations

import os
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from proc_control import process_control


MAX_OUTPUT_BYTES = 4 * 1024 * 1024
_CHILD_ENV_KEYS = frozenset({
    "COMSPEC",
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LOGNAME",
    "PATH",
    "PATHEXT",
    "SHELL",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "TZ",
    "USER",
})


@dataclass(frozen=True)
class CommandResult:
    reason: str
    output: bytes = b""


class CommandCancellation:
    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._cancelled = False

    @property
    def cancelled(self) -> bool:
        with self._condition:
            return self._cancelled

    def cancel(self) -> None:
        with self._condition:
            self._cancelled = True
            self._condition.notify_all()

    def notify_attempt_finished(self) -> None:
        with self._condition:
            self._condition.notify_all()

    def wait_for_cancellation(
        self,
        attempt_finished: threading.Event,
    ) -> bool:
        with self._condition:
            self._condition.wait_for(
                lambda: self._cancelled or attempt_finished.is_set(),
            )
            return self._cancelled


def child_environment(codex_home: Path) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key in _CHILD_ENV_KEYS
    }
    environment["CODEX_HOME"] = str(codex_home)
    return environment


def _force_kill(process: subprocess.Popen[bytes]) -> None:
    controller = process_control()
    controller.kill_detached_descendant_groups(process.pid)
    controller.force_kill(process.pid)


def _wait_after_kill(process: subprocess.Popen[bytes]) -> bool:
    for _attempt in range(2):
        _force_kill(process)
        try:
            process.wait(timeout=5)
            return True
        except subprocess.TimeoutExpired:  # pragma: no cover - SIGKILL is uncatchable; a child surviving two kill+wait cycles is not reachable in tests
            continue
    return False  # pragma: no cover - only reached if both kill+wait cycles time out


def run_catalog_command(
    argv: Sequence[str],
    environment: dict[str, str],
    timeout_seconds: float,
    cancellation: CommandCancellation,
) -> CommandResult:
    if cancellation.cancelled:
        return CommandResult(reason="cancelled")
    controller = process_control()
    try:
        process = subprocess.Popen(
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=environment,
            shell=False,
            **controller.detach_spawn_kwargs(),
        )
    except (OSError, ValueError):
        return CommandResult(reason="spawn_failed")
    if process.stdout is None:  # pragma: no cover - Popen(stdout=PIPE) always yields a non-None stream
        cleaned = _wait_after_kill(process)
        return CommandResult(
            reason="stdout_unavailable" if cleaned else "cleanup_failed",
        )

    output = bytearray()
    overflow = threading.Event()
    read_failed = threading.Event()

    def drain_stdout() -> None:
        try:
            while True:
                chunk = process.stdout.read(64 * 1024)
                if not chunk:
                    return
                if len(output) + len(chunk) > MAX_OUTPUT_BYTES:
                    overflow.set()
                    _force_kill(process)
                    return
                output.extend(chunk)
        except OSError:  # pragma: no cover - a healthy pipe yields EOF (b"") on close, not OSError
            read_failed.set()

    reader = threading.Thread(
        target=drain_stdout,
        name="codex-model-catalog-reader",
        daemon=True,
    )
    attempt_finished = threading.Event()

    def cancel_process() -> None:
        if cancellation.wait_for_cancellation(attempt_finished):
            _force_kill(process)

    cancellation_watcher = threading.Thread(
        target=cancel_process,
        name="codex-model-catalog-cancellation",
        daemon=True,
    )
    reader.start()
    cancellation_watcher.start()
    timed_out = False
    cleanup_failed = False
    try:
        process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        if not _wait_after_kill(process):  # pragma: no cover - SIGKILL always reaps the child, so cleanup_failed is never set here
            cleanup_failed = True
    attempt_finished.set()
    cancellation.notify_attempt_finished()
    cancellation_watcher.join(timeout=5)
    reader.join(timeout=5)
    if reader.is_alive():  # pragma: no cover - reader returns on EOF/overflow/kill before the 5s join elapses
        if not _wait_after_kill(process):
            cleanup_failed = True
        process.stdout.close()
        reader.join(timeout=5)
    if (  # pragma: no cover - reachable only if a killed child or its reader/cancel-watcher survived, which SIGKILL prevents
        reader.is_alive()
        or cancellation_watcher.is_alive()
        or process.poll() is None
        or cleanup_failed
    ):
        return CommandResult(reason="cleanup_failed")
    if cancellation.cancelled:
        return CommandResult(reason="cancelled")
    if timed_out:
        return CommandResult(reason="timeout")
    if overflow.is_set():
        return CommandResult(reason="output_too_large")
    if read_failed.is_set():  # pragma: no cover - guarded by the drain OSError pragma; a healthy pipe never sets read_failed
        return CommandResult(reason="output_read_failed")
    if process.returncode != 0:
        return CommandResult(reason="process_failed")
    return CommandResult(reason="", output=bytes(output))
