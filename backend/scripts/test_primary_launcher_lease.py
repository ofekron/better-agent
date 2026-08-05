from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import paths  # noqa: E402
from primary_launcher_lease import (  # noqa: E402
    PrimaryLauncherBusyError,
    PrimaryLauncherLease,
    PrimaryLauncherLeaseError,
)


BACKEND_ROOT = Path(__file__).resolve().parent.parent


def _private_home(root: Path, name: str = "home") -> Path:
    home = root / name
    home.mkdir(mode=0o700)
    paths.make_private_directory(home)
    return home


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL contract")
def test_unhardened_windows_temp_home_is_rejected(tmp_path: Path) -> None:
    home = tmp_path / "unhardened"
    home.mkdir()

    with pytest.raises(PrimaryLauncherLeaseError):
        PrimaryLauncherLease.acquire(home, checkout=tmp_path)


def _child_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(BACKEND_ROOT)
    return env


def _wait_for_path(path: Path, child: subprocess.Popen, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists() or path.stat().st_size == 0:
        if child.poll() is not None:
            stderr = child.stderr.read() if child.stderr is not None else ""
            raise AssertionError(f"child exited before readiness: {stderr}")
        if time.monotonic() >= deadline:
            raise AssertionError("child did not signal readiness")
        time.sleep(0.02)


def test_same_home_contends_and_release_reacquires() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        home = _private_home(Path(tmp))
        first = PrimaryLauncherLease.acquire(home, checkout=Path(tmp))
        try:
            with pytest.raises(PrimaryLauncherBusyError):
                PrimaryLauncherLease.acquire(home, checkout=Path(tmp))
            first.assert_owner(home)
        finally:
            first.release()

        second = PrimaryLauncherLease.acquire(home, checkout=Path(tmp))
        second.release()


def test_distinct_homes_can_hold_leases_concurrently() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        first = PrimaryLauncherLease.acquire(_private_home(root, "one"), checkout=root)
        try:
            second = PrimaryLauncherLease.acquire(
                _private_home(root, "two"), checkout=root
            )
            second.release()
        finally:
            first.release()


def test_process_death_releases_lease() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        home = _private_home(root)
        ready = root / "ready"
        code = """
import pathlib
from primary_launcher_lease import PrimaryLauncherLease
lease = PrimaryLauncherLease.acquire(pathlib.Path(__import__('sys').argv[1]))
pathlib.Path(__import__('sys').argv[2]).write_text(lease.lease_id)
__import__('time').sleep(60)
"""
        child = subprocess.Popen(
            [sys.executable, "-c", code, str(home), str(ready)],
            env=_child_env(),
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            _wait_for_path(ready, child)
            assert ready.read_text(encoding="utf-8")
            with pytest.raises(PrimaryLauncherBusyError):
                PrimaryLauncherLease.acquire(home)
        finally:
            child.terminate()
            child.wait(timeout=10)

        lease = PrimaryLauncherLease.acquire(home)
        lease.release()


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor handoff")
def test_adopted_exec_descriptor_preserves_lease_without_grandchild_inheritance() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        home = _private_home(root)
        lease = PrimaryLauncherLease.acquire(home, checkout=root)
        fd = lease.fileno
        handoff_token = lease.prepare_handoff()
        ready = root / "adopted"
        code = """
import os
import pathlib
import subprocess
import sys
from primary_launcher_lease import PrimaryLauncherLease
lease = PrimaryLauncherLease.adopt(
    int(sys.argv[1]), pathlib.Path(sys.argv[2]),
    lease_id=sys.argv[3], handoff_token=sys.argv[4]
)
probe = subprocess.run(
    [sys.executable, '-c', 'import os,sys; os.fstat(int(sys.argv[1]))', sys.argv[1]],
    close_fds=True,
)
pathlib.Path(sys.argv[5]).write_text(f'adopted:{probe.returncode != 0}')
sys.stdin.read(1)
lease.release()
"""
        os.set_inheritable(fd, True)
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                code,
                str(fd),
                str(home),
                lease.lease_id,
                handoff_token,
                str(ready),
            ],
            env=_child_env(),
            pass_fds=(fd,),
            stdin=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        os.set_inheritable(fd, False)
        lease.detach_after_transfer()
        try:
            _wait_for_path(ready, child)
            assert ready.read_text(encoding="utf-8") == "adopted:True"
            with pytest.raises(PrimaryLauncherBusyError):
                PrimaryLauncherLease.acquire(home)
            assert child.stdin is not None
            child.stdin.write("x")
            child.stdin.flush()
            assert child.wait(timeout=10) == 0
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)

        acquired = PrimaryLauncherLease.acquire(home)
        acquired.release()


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor handoff")
def test_environment_handoff_is_consumed_and_keeps_descriptor_private() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        home = _private_home(root)
        lease = PrimaryLauncherLease.acquire(home, checkout=root)
        transfer = lease.prepare_handoff_environment()
        transferred = os.dup(lease.fileno)
        os.set_inheritable(transferred, True)
        transfer["BETTER_AGENT_PRIMARY_LAUNCHER_HANDOFF_FD"] = str(transferred)
        os.environ.update(transfer)
        lease.detach_after_transfer()

        adopted = PrimaryLauncherLease.adopt_from_environment(home)
        assert adopted is not None
        assert not os.get_inheritable(adopted.fileno)
        assert all(key not in os.environ for key in transfer)
        adopted.release()


def test_lock_file_is_private_regular_and_metadata_is_diagnostic() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        home = _private_home(root)
        lease = PrimaryLauncherLease.acquire(home, checkout=root)
        try:
            lock_path = home / "primary-launcher.lock"
            observed = lock_path.lstat()
            assert stat.S_ISREG(observed.st_mode)
            paths.require_private_file(lock_path)
            if os.name != "nt":
                assert stat.S_IMODE(observed.st_mode) == 0o600
            metadata = json.loads(lock_path.read_text(encoding="utf-8"))
            assert metadata["lease_id"] == lease.lease_id
            assert metadata["home"] == lease.canonical_home
        finally:
            lease.release()


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL contract")
def test_existing_unsafe_windows_lock_is_rejected_without_repair(
    tmp_path: Path,
) -> None:
    home = _private_home(tmp_path)
    lock_path = home / "primary-launcher.lock"
    lock_path.write_text("{}", encoding="utf-8")
    assert not paths.windows_path_has_private_acl(lock_path)

    with pytest.raises(PrimaryLauncherLeaseError):
        PrimaryLauncherLease.acquire(home, checkout=tmp_path)

    assert not paths.windows_path_has_private_acl(lock_path)


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL contract")
def test_failed_windows_lock_hardening_removes_new_file(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = _private_home(tmp_path)
    lock_path = home / "primary-launcher.lock"

    def fail_hardening(_path: Path) -> None:
        raise PermissionError("hardening failed")

    monkeypatch.setattr("primary_launcher_lease.make_private_file", fail_hardening)
    with pytest.raises(PermissionError, match="hardening failed"):
        PrimaryLauncherLease.acquire(home, checkout=tmp_path)

    assert not lock_path.exists()


def test_rejects_redirecting_or_non_regular_lock_paths() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        home = _private_home(root)
        lock_path = home / "primary-launcher.lock"
        lock_path.mkdir()
        with pytest.raises(PrimaryLauncherLeaseError):
            PrimaryLauncherLease.acquire(home)

        lock_path.rmdir()
        if os.name != "nt":
            target = root / "target"
            target.write_text("", encoding="utf-8")
            lock_path.symlink_to(target)
            with pytest.raises(PrimaryLauncherLeaseError):
                PrimaryLauncherLease.acquire(home)


def test_canonical_home_matches_lexical_aliases() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        home = _private_home(root)
        nested = home / "nested"
        nested.mkdir()
        lexical_alias = nested / ".."
        lease = PrimaryLauncherLease.acquire(lexical_alias)
        try:
            assert lease.canonical_home == os.path.normcase(str(home.resolve()))
            with pytest.raises(PrimaryLauncherBusyError):
                PrimaryLauncherLease.acquire(home)
        finally:
            lease.release()


def test_released_or_wrong_home_lease_cannot_assert_ownership() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        home = _private_home(root, "one")
        other = _private_home(root, "two")
        lease = PrimaryLauncherLease.acquire(home)
        with pytest.raises(PrimaryLauncherLeaseError):
            lease.assert_owner(other)
        lease.release()
        with pytest.raises(PrimaryLauncherLeaseError):
            lease.assert_owner(home)


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor handoff")
@pytest.mark.parametrize("failure", ["wrong_home", "wrong_lease"])
def test_rejected_adoption_closes_descriptor_and_releases_handoff(failure: str) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        home = _private_home(root, "one")
        other = _private_home(root, "two")
        lease = PrimaryLauncherLease.acquire(home)
        token = lease.prepare_handoff()
        transferred = os.dup(lease.fileno)
        lease.detach_after_transfer()

        with pytest.raises(PrimaryLauncherLeaseError):
            PrimaryLauncherLease.adopt(
                transferred,
                other if failure == "wrong_home" else home,
                lease_id="wrong" if failure == "wrong_lease" else lease.lease_id,
                handoff_token=token,
            )
        with pytest.raises(OSError):
            os.fstat(transferred)

        acquired = PrimaryLauncherLease.acquire(home)
        acquired.release()


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor handoff")
def test_released_handoff_token_cannot_reactivate_stale_descriptor() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        home = _private_home(root)
        lease = PrimaryLauncherLease.acquire(home)
        token = lease.prepare_handoff()
        stale = os.dup(lease.fileno)
        lease_id = lease.lease_id
        lease.release()

        with pytest.raises(PrimaryLauncherLeaseError):
            PrimaryLauncherLease.adopt(
                stale,
                home,
                lease_id=lease_id,
                handoff_token=token,
            )
        with pytest.raises(OSError):
            os.fstat(stale)

        acquired = PrimaryLauncherLease.acquire(home)
        acquired.release()
