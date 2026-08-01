"""Full unit coverage for routine_lock.py.

routine_lock guards a file-based per-task lock: the namespace segment and
task id are regex-validated, the lock directory is checked to be a real
directory (not a symlink swap), the lock file is opened with O_NOFOLLOW and
re-checked via fstat before the advisory exclusive lock is taken. These
tests cover every validation, symlink-rejection, fd-recheck, release, and the
O_NOFOLLOW platform branch.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home  # noqa: E402

_HOME = _test_home.isolate("ba-routine-lock-")

import routine_lock as rl  # noqa: E402
from paths import bc_home  # noqa: E402


def _root() -> Path:
    return bc_home() / "routine-locks"


# --------------------------------------------------------------------------- #
# _directory / namespace validation
# --------------------------------------------------------------------------- #
def test_directory_rejects_invalid_namespace():
    for bad in ("UPPER", "1leading-digit", "has/slash", "", "x" * 65, "dot."):
        try:
            rl._directory(bad)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for namespace {bad!r}")


def test_directory_creates_secure_directory():
    d = rl._directory("ns-ok")
    assert d == _root() / "ns-ok"
    # 0o700 permissions enforced.
    assert (d.stat().st_mode & 0o777) == 0o700
    assert (_root().stat().st_mode & 0o777) == 0o700


def test_directory_rejects_symlink_at_root(monkeypatch):
    # The guard fails closed when lstat (non-following) reports a symlink.
    # Force S_ISLNK True to simulate a symlink swap over the lock directory
    # without fighting the test-home deletion guard with real links.
    monkeypatch.setattr(rl.stat, "S_ISLNK", lambda mode: True)
    try:
        rl._directory("ns-x")
    except OSError:
        return
    raise AssertionError("expected OSError for symlinked routine-locks root")


# --------------------------------------------------------------------------- #
# acquire / task id validation
# --------------------------------------------------------------------------- #
def test_acquire_rejects_invalid_task_id():
    for bad in ("short", "Z00000000000", "g" * 12, "", "000000000000 "):
        try:
            rl.acquire("ns-ok", bad)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for task_id {bad!r}")


def test_acquire_returns_valid_fd_and_release_closes_it():
    fd = rl.acquire("ns-ok", "abcdef123456")
    assert isinstance(fd, int) and fd > 0
    rl.release(fd)
    # fd is now closed; a second close reports EBADF.
    try:
        os.close(fd)
    except OSError:
        return
    raise AssertionError("expected fd to be closed after release")


def test_acquire_rejects_existing_symlink_lock_file():
    ns = rl._directory("ns-symlink")
    lock = ns / "000000000001.lock"
    lock.symlink_to("/etc/passwd")
    try:
        rl.acquire("ns-symlink", "000000000001")
    except OSError:
        return
    raise AssertionError("expected OSError for symlinked lock file")


def test_acquire_rejects_non_regular_after_open(monkeypatch):
    # Defensive fstat re-check (TOCTOU guard): force fstat to report a
    # non-regular file so the lock is rejected and the fd is closed.
    class _FakeStat:
        st_mode = stat.S_IFLNK  # S_ISREG -> False

    monkeypatch.setattr(rl.os, "fstat", lambda fd: _FakeStat())
    # The guard raises and the except-branch closes the fd before re-raising.
    with pytest.raises(OSError):
        rl.acquire("ns-ok", "abcdef654321")


def test_acquire_works_without_o_nofollow(monkeypatch):
    # Windows lacks O_NOFOLLOW; the hasattr-false branch must still acquire.
    if not hasattr(os, "O_NOFOLLOW"):
        return  # already on a no-O_NOFOLLOW platform
    monkeypatch.delattr(rl.os, "O_NOFOLLOW")
    # monkeypatch restores O_NOFOLLOW on teardown; do not restore manually.
    fd = rl.acquire("ns-ok", "0123456789ab")
    rl.release(fd)


def test_release_unlocks_and_releases_fd():
    fd = rl.acquire("ns-ok", "fedcba987654")
    rl.release(fd)
    # Re-acquiring the same task id succeeds after release (fd was closed,
    # advisory lock released).
    fd2 = rl.acquire("ns-ok", "fedcba987654")
    assert fd2 > 0
    rl.release(fd2)
