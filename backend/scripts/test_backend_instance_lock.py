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
    adopt_primary_backend_instance_lock,
    reserve_primary_backend_instance_lock,
    release_backend_instance_lock,
)
from primary_launcher_lease import PrimaryLauncherLease  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_lock_state():
    """No module-global lock state leaks between tests."""
    _test_home.engage(_TMP_HOME)
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
acquire_backend_instance_lock(role="node")
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


def test_node_acquire_writes_holder_and_releases() -> None:
    acquire_backend_instance_lock(role="node")
    assert bil._LOCK_FD is not None
    assert bil._LOCK_PATH == Path(_TMP_HOME) / "node-backend.lock"

    content = (Path(_TMP_HOME) / "node-backend.lock").read_text(encoding="utf-8")
    payload = __import__("json").loads(content)
    assert payload["pid"] == os.getpid()
    assert payload["role"] == "node"

    release_backend_instance_lock()
    assert bil._LOCK_FD is None
    assert bil._LOCK_PATH is None


def test_node_acquire_skips_primary_authority() -> None:
    with patch(
        "backend_launch_authority.consume_primary_backend_launch_authority",
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
                acquire_backend_instance_lock(role="node")
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
    acquire_backend_instance_lock(role="node")
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
    acquire_backend_instance_lock(role="node")
    acquire_backend_instance_lock(role="node")  # idempotent re-acquire

    blocked = _excluded_child_attempt()
    assert blocked.returncode == 7, blocked
    assert "already using" in blocked.stdout, blocked.stdout

    release_backend_instance_lock()
    acquired = _excluded_child_attempt()
    assert acquired.returncode == 0, acquired

    assert (Path(_TMP_HOME) / "node-backend.lock").exists()


def test_primary_reservation_is_owned_by_launcher_lease(tmp_path: Path) -> None:
    lease = PrimaryLauncherLease.acquire(Path(_TMP_HOME), checkout=tmp_path)
    try:
        reservation = reserve_primary_backend_instance_lock(lease)
        try:
            reservation.assert_owner(lease)
            assert reservation.canonical_home == lease.canonical_home
            assert len(reservation.reservation_id) == 32
        finally:
            reservation.release()
    finally:
        lease.release()


def test_primary_reservation_rejects_duck_typed_launcher(tmp_path: Path) -> None:
    class FakeLease:
        canonical_home = str(Path(_TMP_HOME).resolve())
        lease_id = "a" * 32

        def assert_owner(self, _root: Path) -> None:
            return None

    with pytest.raises(Exception, match="concrete launcher lease"):
        reserve_primary_backend_instance_lock(FakeLease())  # type: ignore[arg-type]


def test_transfer_never_makes_parent_originals_inheritable(tmp_path: Path) -> None:
    lease = PrimaryLauncherLease.acquire(Path(_TMP_HOME), checkout=tmp_path)
    reservation = reserve_primary_backend_instance_lock(lease)
    try:
        transfer_env = reservation.child_env()
        original_fds = (reservation._fd, reservation._ack_write_fd)
        assert all(value is not None for value in original_fds)
        with reservation.child_spawn_kwargs() as kwargs:
            if os.name == "nt":
                import msvcrt

                assert all(
                    not os.get_handle_inheritable(msvcrt.get_osfhandle(value))
                    for value in original_fds
                    if value is not None
                )
                assert all(
                    os.get_handle_inheritable(int(transfer_env[key]))
                    for key in (bil._LOCK_TRANSFER_ENV, bil._ACK_TRANSFER_ENV)
                )
                assert "handle_list" in kwargs["startupinfo"].lpAttributeList
            else:
                assert all(
                    not os.get_inheritable(value)
                    for value in original_fds
                    if value is not None
                )
                assert set(original_fds) <= set(kwargs["pass_fds"])
    finally:
        reservation.release()
        lease.release()


def test_primary_production_acquire_requires_launcher_reservation(
    monkeypatch,
) -> None:
    monkeypatch.delenv("BETTER_AGENT_TEST_MODE", raising=False)
    for key in bil.backend_instance_transfer_env_keys():
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(RuntimeError, match="reservation environment is missing"):
        acquire_backend_instance_lock()


def test_primary_reservation_transfers_to_child_and_parent_can_close(
    tmp_path: Path,
) -> None:
    checkout = Path(_BACKEND).parent
    lease = PrimaryLauncherLease.acquire(Path(_TMP_HOME), checkout=checkout)
    reservation = reserve_primary_backend_instance_lock(lease)
    from backend_launch_authority import issue_primary_backend_launch
    launch_env = issue_primary_backend_launch(
        checkout=checkout,
        state_root=Path(_TMP_HOME),
        launcher_lease=lease,
        backend_reservation=reservation,
    )
    env = {**os.environ, **launch_env}
    env["BETTER_AGENT_HOME"] = _TMP_HOME
    env["BETTER_CLAUDE_HOME"] = _TMP_HOME
    env.pop("BETTER_AGENT_TEST_MODE", None)
    env["BA_BACKEND_PATH"] = _BACKEND
    code = """
import os, sys
sys.path.insert(0, os.environ["BA_BACKEND_PATH"])
from backend_instance_lock import adopt_primary_backend_instance_lock
adopt_primary_backend_instance_lock()
assert all(key not in os.environ for key in __import__("backend_instance_lock").backend_instance_transfer_env_keys())
print("ADOPTED", flush=True)
sys.stdin.read(1)
"""
    child = None
    try:
        with reservation.child_spawn_kwargs() as popen_kwargs:
            child = subprocess.Popen(
                [sys.executable, "-c", code],
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                **popen_kwargs,
            )
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "ADOPTED"
        reservation.await_adoption(child)
        reservation.detach_after_transfer()
        probe_code = (
            "import os,sys\n"
            "try: fd=os.open(sys.argv[1],os.O_RDWR)\n"
            "except OSError: raise SystemExit(7)\n"
            if os.name == "nt"
            else
            "import fcntl,os,sys\nfd=os.open(sys.argv[1],os.O_RDWR)\n"
            "try: fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)\n"
            "except BlockingIOError: raise SystemExit(7)\n"
        )
        probe = subprocess.run(
            [sys.executable, "-c", probe_code, str(Path(_TMP_HOME) / "backend.lock")],
            check=False,
        )
        assert probe.returncode == 7, probe
    finally:
        if child is not None:
            if child.stdin is not None:
                child.stdin.write("x")
                child.stdin.flush()
            child.wait(timeout=10)
        reservation.release()
        lease.release()


def test_adoption_rejects_stale_reservation_identity() -> None:
    checkout = Path(_BACKEND).parent
    lease = PrimaryLauncherLease.acquire(Path(_TMP_HOME), checkout=checkout)
    reservation = reserve_primary_backend_instance_lock(lease)
    from backend_launch_authority import issue_primary_backend_launch

    env = {
        **os.environ,
        **issue_primary_backend_launch(
            checkout=checkout,
            state_root=Path(_TMP_HOME),
            launcher_lease=lease,
            backend_reservation=reservation,
        ),
        "BETTER_AGENT_HOME": _TMP_HOME,
        "BETTER_CLAUDE_HOME": _TMP_HOME,
        "BA_BACKEND_PATH": _BACKEND,
    }
    env.pop("BETTER_AGENT_TEST_MODE", None)
    env[bil._RESERVATION_ID_ENV] = "f" * 32
    code = (
        "import os,sys; sys.path.insert(0,os.environ['BA_BACKEND_PATH']); "
        "from backend_instance_lock import adopt_primary_backend_instance_lock; "
        "adopt_primary_backend_instance_lock()"
    )
    child = None
    try:
        with reservation.child_spawn_kwargs() as popen_kwargs:
            child = subprocess.Popen(
                [sys.executable, "-c", code],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                **popen_kwargs,
            )
        child.wait(timeout=10)
        assert child.returncode != 0
        assert child.stderr is not None
        assert "reservation identity does not match" in child.stderr.read()
        with pytest.raises(RuntimeError, match="acknowledgement is invalid"):
            reservation.await_adoption(child)
    finally:
        if child is not None and child.poll() is None:
            child.kill()
            child.wait()
        reservation.release()
        lease.release()
