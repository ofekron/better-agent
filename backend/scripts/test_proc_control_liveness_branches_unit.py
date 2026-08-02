"""Unit-tier coverage of the process-liveness branches in ``proc_control``
the real-process tests leave out. Complementary to
``test_proc_control_parse_and_branches.py`` (happy-path ``kill_tree``) and the
integration-style descendants/detached_kill/walk_reparent files. Every
assertion is fail-on-bug: removing the guarded branch it targets makes the
test raise instead of pass. No real subprocess, no claude CLI, no
BETTER_AGENT_HOME state (proc_control owns none).

Two clusters:

  * ``_WindowsProcessControl.pid_alive`` (lines 338-361) — the Win32 handle
    probe. The real Win32 calls are platform-gated (``ctypes.windll`` exists
    only on Windows), so on a POSIX host the method is reached by injecting a
    minimal fake ``kernel32`` onto ``ctypes.windll``. The fake models the
    documented Win32 semantics the decision logic depends on
    (``OpenProcess`` returns 0 on failure; ``WaitForSingleObject`` returns
    ``WAIT_TIMEOUT`` while running) — exactly the mocking discipline the
    sibling file already uses for ``taskkill`` / creation flags. Three
    outcomes are locked, each a distinct security-relevant decision:
      - ``OpenProcess`` returns a falsy handle -> fail CLOSED to ``not alive``
        (a probe we can't open must never keep a dead run "alive") AND
        ``CloseHandle`` is NOT called (nothing was opened).
      - a real handle + ``WAIT_TIMEOUT`` -> ``True`` (running).
      - a real handle + any other wait code (signaled) -> ``False`` (exited).
      For both real-handle cases ``CloseHandle`` MUST run in the ``finally``
      (no Win32 handle leak — a leaked handle holds kernel resources and
      blocks the process from truly exiting).

  * ``ProcessControl.kill_tree`` wait-timeout (lines 133-134) — when a
    force-killed tree's ``popen.wait(timeout=5)`` still does not return, the
    ``subprocess.TimeoutExpired`` is swallowed so ``kill_tree`` returns
    cleanly instead of propagating. Exercised via a stub popen whose ``wait``
    raises, with ``force_kill`` neutralized (already covered by the real-
    process test; here we isolate the timeout branch).

POSIX host (macOS). Run with:

    cd backend && .venv/bin/python -m pytest scripts/test_proc_control_liveness_branches_unit.py -v
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import types

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import proc_control  # noqa: E402
from proc_control import _PosixProcessControl, _WindowsProcessControl  # noqa: E402

POSIX = _PosixProcessControl()
WIN = _WindowsProcessControl()

# WaitForSingleObject returns this when the object is still unsignaled
# (i.e. the process is still running). Sourced from the Microsoft docs.
_WAIT_TIMEOUT = 0x00000102


class _WinFunc:
    """Stand-in for a ctypes-exported kernel32 function: callable AND carries
    a settable ``restype`` attribute (the real code does
    ``k32.OpenProcess.restype = wintypes.HANDLE``). Records its last args so the
    tests can assert call / no-call without introspecting ctypes state."""

    def __init__(self, retval):
        self.restype = None
        self._retval = retval
        self.last_args = None
        self.call_count = 0

    def __call__(self, *args):
        self.last_args = args
        self.call_count += 1
        return self._retval


class _FakeKernel32:
    """Models the three kernel32 entry points ``pid_alive`` touches, with
    controllable outcomes per scenario."""

    def __init__(self, *, handle, wait_result):
        self.OpenProcess = _WinFunc(handle)
        self.WaitForSingleObject = _WinFunc(wait_result)
        self.CloseHandle = _WinFunc(None)


def _install_winapi(monkeypatch, fake_k32):
    """Make ``ctypes.windll.kernel32`` resolve to ``fake_k32`` on a POSIX host
    (where ``ctypes.windll`` does not exist). ``wintypes`` is real on all
    platforms and needs no stub."""
    # raising=False: ``ctypes.windll`` does not exist on a POSIX host, so the
    # attribute must be created (not just overwritten).
    monkeypatch.setattr(
        ctypes, "windll", types.SimpleNamespace(kernel32=fake_k32), raising=False
    )


# --------------------------------------------------------------------------- #
# _WindowsProcessControl.pid_alive (338-361)
# --------------------------------------------------------------------------- #


def test_win_pid_alive_unopenable_handle_fails_closed(monkeypatch):
    """Lines 353-356: when ``OpenProcess`` returns a falsy handle the process
    is treated as NOT alive (fail closed — an unopenable probe must never keep
    a dead run "alive") and ``CloseHandle`` is NOT invoked (nothing was
    opened). Removing the ``if not handle: return False`` guard makes the code
    fall through to ``WaitForSingleObject`` / ``CloseHandle`` on the 0 handle;
    the ``CloseHandle.call_count == 0`` assertion catches that."""
    fake = _FakeKernel32(handle=0, wait_result=_WAIT_TIMEOUT)
    _install_winapi(monkeypatch, fake)

    assert WIN.pid_alive(4321) is False
    assert fake.OpenProcess.call_count == 1
    assert fake.CloseHandle.call_count == 0  # nothing opened -> no close


def test_win_pid_alive_wait_timeout_means_running(monkeypatch):
    """Lines 357-359: a real handle whose wait returns ``WAIT_TIMEOUT`` means
    the process is still running -> ``True``. And ``CloseHandle`` MUST run in
    the ``finally`` (no handle leak). Removing the ``finally: CloseHandle``
    block makes ``CloseHandle.call_count`` stay 0 -> the leak assertion
    catches it."""
    fake = _FakeKernel32(handle=4242, wait_result=_WAIT_TIMEOUT)
    _install_winapi(monkeypatch, fake)

    assert WIN.pid_alive(4242) is True
    assert fake.WaitForSingleObject.call_count == 1
    assert fake.CloseHandle.call_count == 1  # handle released (no leak)


def test_win_pid_alive_signaled_means_exited(monkeypatch):
    """Lines 357-359: a real handle whose wait returns anything other than
    ``WAIT_TIMEOUT`` (e.g. ``WAIT_OBJECT_0`` == 0 — signaled/exited) means the
    process is gone -> ``False``. ``CloseHandle`` still runs in the
    ``finally``. Flipping the ``== WAIT_TIMEOUT`` comparison would make this
    return True; the assertion catches it."""
    fake = _FakeKernel32(handle=4242, wait_result=0)  # WAIT_OBJECT_0: signaled
    _install_winapi(monkeypatch, fake)

    assert WIN.pid_alive(4242) is False
    assert fake.CloseHandle.call_count == 1  # handle released even on exit


# --------------------------------------------------------------------------- #
# ProcessControl.kill_tree: wait-timeout is swallowed (133-134)
# --------------------------------------------------------------------------- #


class _StubPopen:
    """Minimal stand-in for a killed Popen whose ``wait`` never returns."""

    def __init__(self, pid):
        self.pid = pid

    def wait(self, timeout=None):
        raise subprocess.TimeoutExpired(cmd="stub", timeout=timeout)


def test_kill_tree_swallows_wait_timeout(monkeypatch):
    """Lines 133-134: after force-killing, if ``popen.wait(timeout=5)`` raises
    ``subprocess.TimeoutExpired`` (tree still draining), the exception is
    swallowed so ``kill_tree`` returns cleanly. ``force_kill`` is neutralized
    (its real semantics are covered by the integration tests) to isolate the
    timeout branch. Removing ``except subprocess.TimeoutExpired: pass`` lets
    the wait fault escape ``kill_tree`` -> the call raises instead of
    returning."""
    monkeypatch.setattr(POSIX, "force_kill", lambda _pid: None)

    # Must not raise.
    POSIX.kill_tree(_StubPopen(pid=999999))


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
