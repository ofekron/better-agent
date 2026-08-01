from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

import credential_build_lock as cbl


class _Execed(Exception):
    """Raised in place of the real os.execvpe, which would replace the process."""


@pytest.fixture
def stub_exec(monkeypatch):
    """Replace the process-replacing execvpe with a sentinel-raising recorder."""
    captured: dict[str, object] = {}

    def fake_execvpe(file: str, args: list[str], env: dict) -> None:
        captured["file"] = file
        captured["args"] = args
        captured["env"] = env
        raise _Execed()

    monkeypatch.setattr(os, "execvpe", fake_execvpe)
    return captured


def test_run_locked_creates_parent_locks_and_execs(
    tmp_path: Path, stub_exec: dict, monkeypatch
) -> None:
    lock_calls: list[int] = []
    set_inheritable_calls: list[tuple[int, bool]] = []
    monkeypatch.setattr(cbl.portable_lock, "lock_ex", lock_calls.append)
    monkeypatch.setattr(os, "set_inheritable", lambda fd, val: set_inheritable_calls.append((fd, val)))

    lock_path = tmp_path / "nested" / "missing" / "cred.lock"
    with pytest.raises(_Execed):
        cbl.run_locked(lock_path, ["buildtool", "--flag", "x"])

    # mkdir(parents=True, exist_ok=True) created the missing parent chain.
    assert lock_path.parent.is_dir()
    # Lock acquired and the descriptor marked inheritable.
    assert len(lock_calls) == 1
    fd = lock_calls[0]
    assert set_inheritable_calls == [(fd, True)]
    # exec received the exact command vector and the live environment.
    assert stub_exec["file"] == "buildtool"
    assert stub_exec["args"] == ["buildtool", "--flag", "x"]
    assert stub_exec["env"] is os.environ
    # The finally branch closed the real descriptor.
    with pytest.raises(OSError):
        os.fstat(fd)


def _rec_open(monkeypatch) -> list[int]:
    flags_seen: list[int] = []
    real_open = os.open

    def rec_open(path, flags, *args, **kwargs):
        flags_seen.append(flags)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", rec_open)
    return flags_seen


def test_run_locked_includes_ofollow_when_available(
    tmp_path: Path, stub_exec: dict, monkeypatch
) -> None:
    if not hasattr(os, "O_NOFOLLOW"):
        pytest.skip("O_NOFOLLOW not present on this platform")
    flags_seen = _rec_open(monkeypatch)
    monkeypatch.setattr(cbl.portable_lock, "lock_ex", lambda fd: None)
    monkeypatch.setattr(os, "set_inheritable", lambda fd, val: None)

    with pytest.raises(_Execed):
        cbl.run_locked(tmp_path / "a.lock", ["t"])

    assert flags_seen == [os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW]


def test_run_locked_omits_ofollow_when_absent(
    tmp_path: Path, stub_exec: dict, monkeypatch
) -> None:
    if not hasattr(os, "O_NOFOLLOW"):
        pytest.skip("O_NOFOLLOW not present on this platform")
    monkeypatch.delattr(os, "O_NOFOLLOW")
    flags_seen = _rec_open(monkeypatch)
    monkeypatch.setattr(cbl.portable_lock, "lock_ex", lambda fd: None)
    monkeypatch.setattr(os, "set_inheritable", lambda fd, val: None)

    with pytest.raises(_Execed):
        cbl.run_locked(tmp_path / "a.lock", ["t"])

    assert flags_seen == [os.O_CREAT | os.O_RDWR]


def test_main_raises_usage_when_insufficient_args(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["credential_build_lock.py", "only-lock"])
    with pytest.raises(SystemExit) as exc:
        cbl.main()
    assert str(exc.value) == "usage: credential_build_lock.py LOCK COMMAND [ARG ...]"


def test_main_dispatches_argv_to_run_locked(
    tmp_path: Path, stub_exec: dict, monkeypatch
) -> None:
    lock = tmp_path / "c.lock"
    # Exactly 3 argv elements: the boundary that distinguishes `< 3` from `<= 3`.
    monkeypatch.setattr(sys, "argv", ["credential_build_lock.py", str(lock), "build"])
    monkeypatch.setattr(cbl.portable_lock, "lock_ex", lambda fd: None)
    monkeypatch.setattr(os, "set_inheritable", lambda fd, val: None)

    with pytest.raises(_Execed):
        cbl.main()

    assert stub_exec["file"] == "build"
    assert stub_exec["args"] == ["build"]
    assert lock.exists()
