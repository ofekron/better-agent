from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-backend-lock-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import backend_instance_lock as bil  # noqa: E402
from backend_instance_lock import (  # noqa: E402
    _read_lock_holder,
    acquire_backend_instance_lock,
    release_backend_instance_lock,
)


@pytest.fixture(autouse=True)
def _clean_lock_state():
    """No module-global lock state leaks between tests."""
    release_backend_instance_lock()
    yield
    release_backend_instance_lock()


def _holder_child(ready_path: str, done_path: str) -> subprocess.Popen:
    """Spawn a child that acquires the primary lock, signals readiness, and
    holds the lock until ``done_path`` appears — real flock contention for the
    parent process to measure."""
    code = """
import os, sys, time
sys.path.insert(0, os.environ["BA_BACKEND_PATH"])
from backend_instance_lock import acquire_backend_instance_lock, release_backend_instance_lock
acquire_backend_instance_lock()
open(os.environ["HOLDER_READY"], "w").close()
while not os.path.exists(os.environ["HOLDER_DONE"]):
    time.sleep(0.02)
release_backend_instance_lock()
"""
    env = os.environ.copy()
    env["BETTER_AGENT_HOME"] = _TMP_HOME
    env["BETTER_CLAUDE_HOME"] = _TMP_HOME
    env["BA_BACKEND_PATH"] = _BACKEND
    env["HOLDER_READY"] = ready_path
    env["HOLDER_DONE"] = done_path
    return subprocess.Popen(
        [sys.executable, "-c", code],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def test_unsupported_role_raises_value_error() -> None:
    with pytest.raises(ValueError, match="unsupported backend role"):
        acquire_backend_instance_lock(role="bogus")
    assert bil._LOCK_FD is None


def test_reacquire_same_role_is_noop() -> None:
    acquire_backend_instance_lock(role="node")
    original_fd = bil._LOCK_FD
    acquire_backend_instance_lock(role="node")  # same path -> early return
    assert bil._LOCK_FD is original_fd
    release_backend_instance_lock()
    assert bil._LOCK_FD is None


def test_role_switch_while_held_raises() -> None:
    acquire_backend_instance_lock(role="node")
    with pytest.raises(RuntimeError, match="already held"):
        acquire_backend_instance_lock(role="primary")
    # original node lock still intact
    assert bil._LOCK_PATH == Path(_TMP_HOME) / "node-backend.lock"


def test_release_when_not_held_is_noop() -> None:
    release_backend_instance_lock()
    release_backend_instance_lock()  # idempotent, no error
    assert bil._LOCK_FD is None


def test_primary_acquire_writes_holder_and_releases() -> None:
    acquire_backend_instance_lock()  # role="primary"
    assert bil._LOCK_FD is not None
    assert bil._LOCK_PATH == Path(_TMP_HOME) / "backend.lock"

    content = (Path(_TMP_HOME) / "backend.lock").read_text(encoding="utf-8")
    assert "pid=" in content
    assert "host=" in content
    assert "ba_home=" in content
    assert "generation=" in content

    release_backend_instance_lock()
    assert bil._LOCK_FD is None
    assert bil._LOCK_PATH is None


def test_node_acquire_skips_primary_authority() -> None:
    with patch(
        "backend_instance_lock.assert_primary_backend_launch_authorized",
        side_effect=AssertionError("node lock requested primary authority"),
    ):
        acquire_backend_instance_lock(role="node")
        release_backend_instance_lock()
    assert (Path(_TMP_HOME) / "node-backend.lock").exists()


def test_contention_warns_then_times_out() -> None:
    ready = Path(_TMP_HOME) / "holder.ready"
    done = Path(_TMP_HOME) / "holder.done"
    ready.unlink(missing_ok=True)
    done.unlink(missing_ok=True)

    child = _holder_child(str(ready), str(done))
    try:
        # Wait until the child actually holds the lock (event-driven via the
        # ready file, not a fixed sleep).
        deadline = time.monotonic() + 10.0
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists(), "holder child did not signal readiness"

        # Shrink the retry window so the deadline-exceeded path fires fast
        # while still allowing one warned iteration. This is test acceleration
        # of the retry constants, not a mock of locking behavior.
        with patch("backend_instance_lock._LOCK_ACQUIRE_RETRY_SECONDS", 0.05), \
                patch("backend_instance_lock._LOCK_ACQUIRE_POLL_INTERVAL", 0.01):
            with pytest.raises(RuntimeError, match="already using"):
                acquire_backend_instance_lock()
        assert bil._LOCK_FD is None
    finally:
        done.touch()
        child.wait(timeout=10)


def test_write_failure_unlocks_and_closes_fd() -> None:
    # fsync failing in the write block exercises the defensive cleanup path.
    with pytest.raises(OSError):
        with patch("backend_instance_lock.os.fsync", side_effect=OSError("disk")):
            acquire_backend_instance_lock(role="node")
    assert bil._LOCK_FD is None
    # fd was closed + unlocked, so a fresh acquire still works.
    acquire_backend_instance_lock(role="node")
    release_backend_instance_lock()


def test_read_lock_holder_returns_empty_on_missing_file() -> None:
    assert _read_lock_holder(Path(_TMP_HOME) / "does-not-exist") == ""


def _excluded_child_attempt() -> subprocess.CompletedProcess[str]:
    code = """
import os, sys
sys.path.insert(0, os.environ["BA_BACKEND_PATH"])
from backend_instance_lock import acquire_backend_instance_lock, release_backend_instance_lock
try:
    acquire_backend_instance_lock()
except RuntimeError as exc:
    print(str(exc))
    raise SystemExit(7)
else:
    release_backend_instance_lock()
"""
    env = os.environ.copy()
    env["BETTER_AGENT_HOME"] = _TMP_HOME
    env["BETTER_CLAUDE_HOME"] = _TMP_HOME
    env["BA_BACKEND_PATH"] = _BACKEND
    return subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_cross_process_same_home_exclusion() -> None:
    acquire_backend_instance_lock()
    acquire_backend_instance_lock()  # idempotent re-acquire

    blocked = _excluded_child_attempt()
    assert blocked.returncode == 7, blocked
    assert "already using" in blocked.stdout, blocked.stdout

    release_backend_instance_lock()
    acquired = _excluded_child_attempt()
    assert acquired.returncode == 0, acquired

    assert (Path(_TMP_HOME) / "backend.lock").exists()
