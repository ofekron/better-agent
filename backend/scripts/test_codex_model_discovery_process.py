from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from scripts import _test_home  # noqa: E402

TEST_HOME = _test_home.TestHome.acquire("ba-test-codex-discovery-process-")

from codex_model_discovery_process import (  # noqa: E402
    MAX_OUTPUT_BYTES,
    CommandCancellation,
    CommandResult,
    child_environment,
    run_catalog_command,
)


# --------------------------------------------------------------------------- #
# CommandResult
# --------------------------------------------------------------------------- #


def test_command_result_defaults():
    result = CommandResult(reason="ok")
    assert result.reason == "ok"
    assert result.output == b""


def test_command_result_is_frozen():
    result = CommandResult(reason="ok")
    with pytest.raises(Exception):
        result.reason = "other"  # type: ignore[misc]


def test_command_result_equality():
    assert CommandResult(reason="ok", output=b"x") == CommandResult(
        reason="ok", output=b"x"
    )
    assert CommandResult(reason="ok") != CommandResult(reason="no")


# --------------------------------------------------------------------------- #
# child_environment
# --------------------------------------------------------------------------- #


def test_child_environment_filters_to_whitelist_and_sets_codex_home(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("USER", "ofek")
    monkeypatch.setenv("BADESK_SENTINEL", "must-be-dropped")
    env = child_environment(tmp_path)
    assert env["CODEX_HOME"] == str(tmp_path)
    assert env["USER"] == "ofek"
    assert "BADESK_SENTINEL" not in env


def test_child_environment_never_includes_codex_home_input_as_other_key(
    monkeypatch, tmp_path
):
    monkeypatch.delenv("CODEX_HOME", raising=False)
    env = child_environment(tmp_path)
    assert env["CODEX_HOME"] == str(tmp_path)


# --------------------------------------------------------------------------- #
# CommandCancellation
# --------------------------------------------------------------------------- #


def test_cancellation_starts_not_cancelled():
    assert CommandCancellation().cancelled is False


def test_cancellation_cancel_sets_flag():
    cancellation = CommandCancellation()
    cancellation.cancel()
    assert cancellation.cancelled is True


def test_wait_for_cancellation_returns_false_when_attempt_finishes_first():
    cancellation = CommandCancellation()
    attempt_finished = threading.Event()
    attempt_finished.set()
    assert cancellation.wait_for_cancellation(attempt_finished) is False


def test_wait_for_cancellation_returns_true_when_cancelled_while_waiting():
    cancellation = CommandCancellation()
    attempt_finished = threading.Event()
    outcome = {}

    def waiter():
        outcome["cancelled"] = cancellation.wait_for_cancellation(attempt_finished)

    thread = threading.Thread(target=waiter)
    thread.start()
    cancellation.cancel()
    thread.join(timeout=5)
    assert outcome["cancelled"] is True


# --------------------------------------------------------------------------- #
# run_catalog_command — driven by real subprocesses
# --------------------------------------------------------------------------- #


def _env(tmp_path) -> dict[str, str]:
    return child_environment(tmp_path)


def test_run_already_cancelled_short_circuits(tmp_path):
    cancellation = CommandCancellation()
    cancellation.cancel()
    result = run_catalog_command(
        ["/bin/echo", "x"], _env(tmp_path), timeout_seconds=5, cancellation=cancellation
    )
    assert result.reason == "cancelled"
    assert result.output == b""


def test_run_missing_binary_is_spawn_failed(tmp_path):
    result = run_catalog_command(
        ["/nonexistent/binary/does-not-exist"],
        _env(tmp_path),
        timeout_seconds=5,
        cancellation=CommandCancellation(),
    )
    assert result.reason == "spawn_failed"


def test_run_success_captures_output(tmp_path):
    result = run_catalog_command(
        ["/bin/echo", "hello"],
        _env(tmp_path),
        timeout_seconds=10,
        cancellation=CommandCancellation(),
    )
    assert result.reason == ""
    assert result.output == b"hello\n"


def test_run_nonzero_exit_is_process_failed(tmp_path):
    result = run_catalog_command(
        ["/bin/sh", "-c", "exit 3"],
        _env(tmp_path),
        timeout_seconds=10,
        cancellation=CommandCancellation(),
    )
    assert result.reason == "process_failed"


def test_run_timeout_kills_and_reports_timeout(tmp_path):
    result = run_catalog_command(
        ["/bin/sleep", "30"],
        _env(tmp_path),
        timeout_seconds=0.5,
        cancellation=CommandCancellation(),
    )
    assert result.reason == "timeout"


def test_run_output_too_large(tmp_path):
    # Emit far more than MAX_OUTPUT_BYTES; reader crosses the limit, force-kills
    # the child, and the run reports output_too_large.
    size = MAX_OUTPUT_BYTES + 1024 * 1024
    result = run_catalog_command(
        ["/bin/sh", "-c", f"python3 -c 'import sys; sys.stdout.buffer.write(b\"x\"*{size})'"],
        _env(tmp_path),
        timeout_seconds=30,
        cancellation=CommandCancellation(),
    )
    assert result.reason == "output_too_large"


def test_run_cancelled_during_execution(tmp_path):
    cancellation = CommandCancellation()
    outcome: dict[str, object] = {}

    def run_in_worker():
        outcome["result"] = run_catalog_command(
            ["/bin/sleep", "30"],
            _env(tmp_path),
            timeout_seconds=30,
            cancellation=cancellation,
        )

    worker = threading.Thread(target=run_in_worker)
    worker.start()
    # Give the worker time to spawn the child and block on process.wait, so the
    # cancellation watcher is mid-flight when we cancel (not a pre-spawn short-circuit).
    worker.join(timeout=0.5)
    cancellation.cancel()
    worker.join(timeout=10)
    result = outcome["result"]
    assert isinstance(result, CommandResult)
    assert result.reason == "cancelled"
