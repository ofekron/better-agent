from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import threading
import types
from unittest.mock import MagicMock

import pytest

from backend_process_owner import JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, BackendProcessOwner

KILL_FLAG = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE


class FakeProc:
    """Minimal Popen stand-in for dispatch tests (no real process)."""

    def __init__(self, pid=99999, alive=True, handle=0xDEADBEEF):
        self.pid = pid
        self._alive = alive
        self._handle = handle
        self.sent: list = []

    def poll(self):
        return None if self._alive else 0

    def send_signal(self, sig):
        self.sent.append(sig)


def _force_build(proc=None, job=None, closed=False):
    """Bypass __init__ to assemble an owner in a known state."""
    owner = BackendProcessOwner.__new__(BackendProcessOwner)
    owner._process = proc if proc is not None else FakeProc()
    owner._job = job
    owner._closed = closed
    owner._lock = threading.Lock()
    return owner


# ---------------------------------------------------------------------------
# __init__
# ---------------------------------------------------------------------------

def test_init_posix_no_job(monkeypatch):
    monkeypatch.setattr(os, "name", "posix")
    proc = FakeProc()
    owner = BackendProcessOwner(proc)
    assert owner._process is proc
    assert owner._job is None
    assert owner._closed is False
    assert isinstance(owner._lock, type(threading.Lock()))


def test_init_windows_creates_job(monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    sentinel = object()
    captured = {}
    monkeypatch.setattr(
        BackendProcessOwner, "_create_windows_job",
        staticmethod(lambda process: captured.__setitem__("proc", process) or sentinel),
    )
    proc = FakeProc()
    owner = BackendProcessOwner(proc)
    assert owner._job is sentinel
    assert captured["proc"] is proc
    assert owner._closed is False


# ---------------------------------------------------------------------------
# spawn_kwargs
# ---------------------------------------------------------------------------

def test_spawn_kwargs_posix(monkeypatch):
    monkeypatch.setattr(os, "name", "posix")
    assert BackendProcessOwner.spawn_kwargs() == {"start_new_session": True}


def test_spawn_kwargs_windows_with_no_window(monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200, raising=False)
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    kw = BackendProcessOwner.spawn_kwargs()
    assert kw == {"creationflags": 0x00000200 | 0x08000000}


def test_spawn_kwargs_windows_missing_no_window_constant(monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200, raising=False)
    monkeypatch.delattr(subprocess, "CREATE_NO_WINDOW", raising=False)
    # Falls back to the documented default 0x08000000 when the constant is absent.
    assert BackendProcessOwner.spawn_kwargs() == {"creationflags": 0x00000200 | 0x08000000}


# ---------------------------------------------------------------------------
# signal
# ---------------------------------------------------------------------------

def test_signal_none_posix_kills_process_group(monkeypatch):
    monkeypatch.setattr(os, "name", "posix")
    calls = []
    monkeypatch.setattr(os, "killpg", lambda pid, sig: calls.append((pid, sig)))
    proc = FakeProc(pid=4242)
    owner = _force_build(proc)
    owner.signal(None)
    assert calls == [(4242, signal.SIGKILL)]


def test_signal_none_posix_swallows_kill_errors(monkeypatch):
    monkeypatch.setattr(os, "name", "posix")

    def raise_(err):
        def _kill(pid, sig):
            raise err
        return _kill

    proc = FakeProc()
    for err in (ProcessLookupError(), PermissionError(), OSError()):
        owner = _force_build(proc)
        monkeypatch.setattr(os, "killpg", raise_(err))
        owner.signal(None)  # must not propagate
    # Any other exception type WOULD propagate — guard the contract.
    owner = _force_build(FakeProc())
    monkeypatch.setattr(os, "killpg", raise_(RuntimeError()))
    try:
        owner.signal(None)
    except RuntimeError:
        pass
    else:
        raise AssertionError("non-(ProcessLookupError/PermissionError/OSError) must propagate")


def test_signal_none_windows_terminates_job(monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    calls = []
    owner = _force_build(FakeProc())
    monkeypatch.setattr(owner, "_terminate_windows_job", lambda: calls.append("term"))
    owner.signal(None)
    assert calls == ["term"]


def test_signal_nonnull_alive_forwards_to_process(monkeypatch):
    monkeypatch.setattr(os, "name", "posix")
    proc = FakeProc(alive=True)
    owner = _force_build(proc)
    owner.signal(signal.SIGTERM)
    assert proc.sent == [signal.SIGTERM]


def test_signal_nonnull_dead_does_not_signal(monkeypatch):
    monkeypatch.setattr(os, "name", "posix")
    proc = FakeProc(alive=False)
    owner = _force_build(proc)
    owner.signal(signal.SIGTERM)
    assert proc.sent == []


# ---------------------------------------------------------------------------
# close
# ---------------------------------------------------------------------------

def test_close_posix_kills_group_once(monkeypatch):
    monkeypatch.setattr(os, "name", "posix")
    calls = []
    monkeypatch.setattr(os, "killpg", lambda pid, sig: calls.append((pid, sig)))
    proc = FakeProc(pid=7)
    owner = _force_build(proc)
    owner.close()
    assert owner._closed is True
    assert calls == [(7, signal.SIGKILL)]
    # idempotent: second close is a no-op (no second killpg).
    owner.close()
    assert calls == [(7, signal.SIGKILL)]


def test_close_posix_swallows_kill_errors(monkeypatch):
    monkeypatch.setattr(os, "name", "posix")

    def raise_(err):
        def _kill(pid, sig):
            raise err
        return _kill

    for err in (ProcessLookupError(), PermissionError(), OSError()):
        owner = _force_build(FakeProc())
        monkeypatch.setattr(os, "killpg", raise_(err))
        owner.close()  # must not propagate
        assert owner._closed is True


def test_close_windows_terminates_and_closes_job(monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    calls = []
    owner = _force_build(FakeProc(), job="job-handle")
    monkeypatch.setattr(owner, "_terminate_windows_job", lambda: calls.append("term"))
    monkeypatch.setattr(owner, "_close_windows_job", lambda: calls.append("close"))
    owner.close()
    assert owner._closed is True
    assert calls == ["term", "close"]


# ---------------------------------------------------------------------------
# _terminate_windows_job / _close_windows_job (ctypes dispatch)
# ---------------------------------------------------------------------------

def _fake_kernel32(monkeypatch):
    kernel32 = MagicMock()
    monkeypatch.setattr(ctypes, "windll", types.SimpleNamespace(kernel32=kernel32), raising=False)
    return kernel32


def test_terminate_windows_job_no_job_is_noop(monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    kernel32 = _fake_kernel32(monkeypatch)
    owner = _force_build(FakeProc(), job=None)
    owner._terminate_windows_job()
    kernel32.TerminateJobObject.assert_not_called()


def test_terminate_windows_job_calls_terminate_with_exit_one(monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    kernel32 = _fake_kernel32(monkeypatch)
    owner = _force_build(FakeProc(), job="JOB")
    owner._terminate_windows_job()
    kernel32.TerminateJobObject.assert_called_once_with("JOB", 1)


def test_close_windows_job_no_job_is_noop(monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    kernel32 = _fake_kernel32(monkeypatch)
    owner = _force_build(FakeProc(), job=None)
    owner._close_windows_job()
    kernel32.CloseHandle.assert_not_called()
    assert owner._job is None


def test_close_windows_job_closes_handle_and_clears(monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    kernel32 = _fake_kernel32(monkeypatch)
    owner = _force_build(FakeProc(), job="JOB")
    owner._close_windows_job()
    kernel32.CloseHandle.assert_called_once_with("JOB")
    assert owner._job is None


# ---------------------------------------------------------------------------
# _create_windows_job (ctypes orchestration incl. error cleanup)
# ---------------------------------------------------------------------------

def _patch_windll_for_create(monkeypatch, create_ok=True, setinfo_ok=True, assign_ok=True):
    kernel32 = MagicMock()
    kernel32.CreateJobObjectW.return_value = "JOB" if create_ok else 0
    kernel32.SetInformationJobObject.return_value = 1 if setinfo_ok else 0
    kernel32.AssignProcessToJobObject.return_value = 1 if assign_ok else 0
    # byref() returns the structure itself so we can read LimitFlags off it.
    monkeypatch.setattr(ctypes, "byref", lambda obj: obj)
    monkeypatch.setattr(ctypes, "windll", types.SimpleNamespace(kernel32=kernel32), raising=False)
    return kernel32


def test_create_windows_job_success_orchestration(monkeypatch):
    from ctypes import wintypes

    kernel32 = _patch_windll_for_create(monkeypatch)
    proc = FakeProc(handle=0x1234)

    job = BackendProcessOwner._create_windows_job(proc)

    assert job == "JOB"
    # restype pinned to HANDLE so the kernel handle isn't truncated.
    assert kernel32.CreateJobObjectW.restype is wintypes.HANDLE
    kernel32.CreateJobObjectW.assert_called_once_with(None, None)
    # SetInformationJobObject: (job, JobObjectExtendedLimitInformation=9, info, sizeof)
    sargs = kernel32.SetInformationJobObject.call_args[0]
    assert sargs[0] == "JOB"
    assert sargs[1] == 9
    info = sargs[2]
    assert info.BasicLimitInformation.LimitFlags == KILL_FLAG
    assert sargs[3] == ctypes.sizeof(info)
    # AssignProcessToJobObject: (job, HANDLE(process._handle))
    aargs = kernel32.AssignProcessToJobObject.call_args[0]
    assert aargs[0] == "JOB"


def _patch_win_errors(monkeypatch):
    """ctypes.WinError / get_last_error are Windows-only; inject fakes."""
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 99, raising=False)
    win = MagicMock(return_value=OSError("win"))
    monkeypatch.setattr(ctypes, "WinError", win, raising=False)
    return win


def test_create_windows_job_create_returns_zero_raises(monkeypatch):
    win = _patch_win_errors(monkeypatch)
    kernel32 = _patch_windll_for_create(monkeypatch, create_ok=False)
    with pytest.raises(OSError):
        BackendProcessOwner._create_windows_job(FakeProc())
    win.assert_called_once_with(99)
    kernel32.CloseHandle.assert_not_called()  # nothing to clean up yet


def test_create_windows_job_setinfo_failure_closes_handle(monkeypatch):
    win = _patch_win_errors(monkeypatch)
    kernel32 = _patch_windll_for_create(monkeypatch, setinfo_ok=False)
    with pytest.raises(OSError):
        BackendProcessOwner._create_windows_job(FakeProc())
    win.assert_called_once_with(99)
    kernel32.CloseHandle.assert_called_once_with("JOB")


def test_create_windows_job_assign_failure_closes_handle(monkeypatch):
    win = _patch_win_errors(monkeypatch)
    kernel32 = _patch_windll_for_create(monkeypatch, assign_ok=False)
    with pytest.raises(OSError):
        BackendProcessOwner._create_windows_job(FakeProc())
    win.assert_called_once_with(99)
    kernel32.CloseHandle.assert_called_once_with("JOB")
