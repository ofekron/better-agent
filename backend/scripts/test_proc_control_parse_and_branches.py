"""Unit-tier coverage of proc_control.py parse / branch / exception logic
that the real-process tests (descendants / detached_kill / walk_reparent)
do NOT reach.

Complementary to those integration-style tests: this file exercises the
defensive branches and pure parsers via controlled monkeypatching, which
is legitimate at the unit tier (the integration tier stays mock-free).

POSIX host (macOS/Linux). No claude CLI, no API, no BETTER_AGENT_HOME
state — proc_control.py owns no durable state. Every real subprocess is
spawned with ``start_new_session=True`` (the platform's
``detach_spawn_kwargs``) so kill signals target only that child's group,
never the test runner's own group.
"""

import os
import signal
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import proc_control  # noqa: E402
from proc_control import (  # noqa: E402
    ProcessControl,
    _PosixProcessControl,
    _WindowsProcessControl,
)

POSIX = _PosixProcessControl()
WIN = _WindowsProcessControl()


class _RunResult:
    """Stand-in for the object returned by subprocess.run."""

    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _set_run(monkeypatch, payload, *, raises=None):
    """Redirect proc_control.subprocess.run to return ``payload`` (a stdout
    string) or raise ``raises``."""

    def _fake_run(*args, **kwargs):
        if raises is not None:
            raise raises
        return _RunResult(payload)

    monkeypatch.setattr(proc_control.subprocess, "run", _fake_run)


# -------------------------------------------------------------------- base class


class _Base(ProcessControl):
    """Minimal concrete ProcessControl recording calls to its primitives."""

    def __init__(self):
        self.calls = []

    def detach_spawn_kwargs(self):
        return {}

    def pid_alive(self, pid):
        return pid > 0

    def signal_stop(self, pid):
        self.calls.append(("signal_stop", pid))

    def force_kill(self, pid):
        self.calls.append(("force_kill", pid))

    def group_member_pids(self, leader_pid):
        return [leader_pid]

    def kill_detached_descendant_groups(self, leader_pid, ignore_pgids=frozenset()):
        return 0

    def has_detached_descendants(self, leader_pid, ignore_pgids=frozenset()):
        return False


class TestBaseClassDefaults:
    def test_owned_group_id_returns_pid(self):
        assert _Base().owned_group_id(4242) == 4242

    def test_pid_recoverable_delegates_to_pid_alive(self):
        b = _Base()
        assert b.pid_recoverable(7) is True  # pid>0 ⇒ alive
        assert b.pid_recoverable(-1) is False

    def test_signal_owned_group_delegates(self):
        b = _Base()
        b.signal_owned_group(99)
        assert b.calls == [("signal_stop", 99)]

    def test_force_kill_owned_group_delegates(self):
        b = _Base()
        b.force_kill_owned_group(99)
        assert b.calls == [("force_kill", 99)]


# --------------------------------------------------------------- posix primitives


class TestPosixDetach:
    def test_detach_kwargs_new_session(self):
        assert POSIX.detach_spawn_kwargs() == {"start_new_session": True}


class TestPosixPidAlive:
    def test_live_pid(self):
        assert POSIX.pid_alive(os.getpid()) is True

    def test_dead_pid_processlookup(self):
        child = subprocess.Popen(["sleep", "0.2"])
        dead = child.pid
        child.wait()  # reap ⇒ ProcessLookupError on os.kill
        assert POSIX.pid_alive(dead) is False

    def test_permissionerror_treated_as_alive(self, monkeypatch):
        def _perm(*a, **k):
            raise PermissionError()

        monkeypatch.setattr(proc_control.os, "kill", _perm)
        assert POSIX.pid_alive(1234) is True

    def test_other_oserror_treated_as_dead(self, monkeypatch):
        def _ose(*a, **k):
            raise OSError()

        monkeypatch.setattr(proc_control.os, "kill", _ose)
        assert POSIX.pid_alive(1234) is False


class TestPosixPidRecoverable:
    def test_dead_not_recoverable(self):
        child = subprocess.Popen(["sleep", "0.2"])
        dead = child.pid
        child.wait()
        assert POSIX.pid_recoverable(dead) is False

    def test_state_none_recoverable(self, monkeypatch):
        monkeypatch.setattr(POSIX, "_process_state", lambda pid: None)
        assert POSIX.pid_recoverable(os.getpid()) is True

    @pytest.mark.parametrize("state", ["Z", "X"])
    def test_zombie_or_dead_state_not_recoverable(self, monkeypatch, state):
        monkeypatch.setattr(POSIX, "_process_state", lambda pid: state)
        assert POSIX.pid_recoverable(os.getpid()) is False

    def test_darwin_E_state_not_recoverable(self, monkeypatch):
        monkeypatch.setattr(proc_control.sys, "platform", "darwin")
        monkeypatch.setattr(POSIX, "_process_state", lambda pid: "E")
        assert POSIX.pid_recoverable(os.getpid()) is False

    def test_running_state_recoverable(self, monkeypatch):
        monkeypatch.setattr(proc_control.sys, "platform", "darwin")
        monkeypatch.setattr(POSIX, "_process_state", lambda pid: "R")
        assert POSIX.pid_recoverable(os.getpid()) is True


class TestPosixProcessState:
    def test_darwin_live_pid(self, monkeypatch):
        monkeypatch.setattr(proc_control.sys, "platform", "darwin")
        assert POSIX._process_state(os.getpid()) is not None

    def test_darwin_dead_pid_returns_none(self, monkeypatch):
        monkeypatch.setattr(proc_control.sys, "platform", "darwin")
        child = subprocess.Popen(["sleep", "0.2"])
        dead = child.pid
        child.wait()
        assert POSIX._process_state(dead) is None

    def test_darwin_ps_subprocess_failure_returns_none(self, monkeypatch):
        monkeypatch.setattr(proc_control.sys, "platform", "darwin")
        _set_run(monkeypatch, "", raises=OSError("ps boom"))
        assert POSIX._process_state(os.getpid()) is None

    def test_linux_parses_state_field(self, monkeypatch):
        monkeypatch.setattr(proc_control.sys, "platform", "linux")

        class _FakePath:
            def __init__(self, content):
                self._content = content

            def read_text(self, encoding=None):
                if isinstance(self._content, Exception):
                    raise self._content
                return self._content

        def factory(content):
            return lambda target: _FakePath(content)

        # stat line: "pid (comm) state ..." ⇒ rsplit(')',1)[1].split()[0]
        monkeypatch.setattr(proc_control, "Path", factory("42 (bash) R 0 0\n"))
        assert POSIX._process_state(42) == "R"

        # No closing paren ⇒ IndexError ⇒ None.
        monkeypatch.setattr(proc_control, "Path", factory("no-paren-here"))
        assert POSIX._process_state(42) is None

        # read_text raises ⇒ OSError ⇒ None.
        monkeypatch.setattr(proc_control, "Path", factory(UnicodeError("bad")))
        assert POSIX._process_state(42) is None

    def test_other_platform_returns_none(self, monkeypatch):
        monkeypatch.setattr(proc_control.sys, "platform", "win32")
        assert POSIX._process_state(1) is None


class TestPosixGroupMemberPids:
    def test_subprocess_failure_returns_leader_only(self, monkeypatch):
        _set_run(monkeypatch, "", raises=OSError("boom"))
        assert POSIX.group_member_pids(5) == [5]

    def test_parse_skips_malformed_and_noninteger(self, monkeypatch):
        # Lines: a valid child of 5, a malformed (3 cols), a non-integer pair.
        stdout = "10 5\n11 5 99\nfoo bar\n12 5\n"
        _set_run(monkeypatch, stdout)
        result = set(POSIX.group_member_pids(5))
        assert result == {5, 10, 12}  # leader + two valid children

    def test_walk_descends_multiple_levels(self, monkeypatch):
        # Tree: 5 -> 10 -> 20 ; 5 -> 11
        stdout = "10 5\n20 10\n11 5\n"
        _set_run(monkeypatch, stdout)
        result = set(POSIX.group_member_pids(5))
        assert result == {5, 10, 11, 20}

    def test_cycle_safe(self, monkeypatch):
        # Defensive: a self-referencing ppid must terminate.
        stdout = "5 5\n"
        _set_run(monkeypatch, stdout)
        assert POSIX.group_member_pids(5) == [5]


class TestPosixKillpgStatics:
    def test_killpg_swallows_processlookup(self, monkeypatch):
        def _raise(pid):
            raise ProcessLookupError()

        monkeypatch.setattr(proc_control.os, "getpgid", _raise)
        # Must not raise.
        _PosixProcessControl._killpg(9999, signal.SIGTERM)

    def test_killpg_swallows_permission_and_oserror(self, monkeypatch):
        # _killpg resolves the group via os.getpgid (PermissionError) and
        # _kill_group_id signals via os.killpg (OSError); both must be caught.
        monkeypatch.setattr(proc_control.os, "getpgid", lambda pid: (_ for _ in ()).throw(PermissionError()))
        monkeypatch.setattr(proc_control.os, "killpg", lambda pgid, sig: (_ for _ in ()).throw(OSError()))
        _PosixProcessControl._killpg(9999, signal.SIGTERM)
        _PosixProcessControl._kill_group_id(9999, signal.SIGKILL)

    def test_kill_group_id_swallows_processlookup(self, monkeypatch):
        def _raise(pgid, sig):
            raise ProcessLookupError()

        monkeypatch.setattr(proc_control.os, "killpg", _raise)
        _PosixProcessControl._kill_group_id(9999, signal.SIGKILL)

    def test_owned_group_id_live(self):
        assert POSIX.owned_group_id(os.getpid()) == os.getpgid(os.getpid())

    def test_signal_and_force_kill_owned_group_delegate(self, monkeypatch):
        sent = []
        monkeypatch.setattr(proc_control.os, "killpg", lambda g, s: sent.append((g, s)))
        POSIX.signal_owned_group(4321)
        POSIX.force_kill_owned_group(4321)
        assert (4321, signal.SIGTERM) in sent
        assert (4321, signal.SIGKILL) in sent


class TestPosixHasDetachedDescendants:
    def test_leader_getpgid_failure_returns_false(self, monkeypatch):
        def _raise(pid):
            raise ProcessLookupError()

        monkeypatch.setattr(proc_control.os, "getpgid", _raise)
        assert POSIX.has_detached_descendants(5) is False

    def test_detached_descendant_present(self, monkeypatch):
        # leader group 100, one descendant in group 200.
        pgids = {5: 100, 10: 200}

        monkeypatch.setattr(proc_control.os, "getpgid", lambda p: pgids[p])
        monkeypatch.setattr(POSIX, "group_member_pids", lambda leader: [5, 10])
        assert POSIX.has_detached_descendants(5) is True

    def test_same_group_descendant_not_reported(self, monkeypatch):
        monkeypatch.setattr(proc_control.os, "getpgid", lambda p: 100)
        monkeypatch.setattr(POSIX, "group_member_pids", lambda leader: [5, 10])
        assert POSIX.has_detached_descendants(5) is False

    def test_ignored_pgid_not_reported(self, monkeypatch):
        monkeypatch.setattr(proc_control.os, "getpgid", lambda p: 200)
        monkeypatch.setattr(POSIX, "group_member_pids", lambda leader: [5, 10])
        assert POSIX.has_detached_descendants(5, frozenset({200})) is False

    def test_descendant_getpgid_failure_skipped(self, monkeypatch):
        def _getpgid(p):
            if p == 10:
                raise ProcessLookupError()
            return 100

        monkeypatch.setattr(proc_control.os, "getpgid", _getpgid)
        monkeypatch.setattr(POSIX, "group_member_pids", lambda leader: [5, 10])
        assert POSIX.has_detached_descendants(5) is False


class TestPosixKillDetachedDescendantGroups:
    def test_leader_getpgid_failure_own_none(self, monkeypatch):
        killed = []

        def _getpgid(p):
            if p == 5:
                raise ProcessLookupError()
            return 999  # descendant group differs from own(None) ⇒ signalled

        def _killpg(g, sig):
            killed.append(g)

        monkeypatch.setattr(proc_control.os, "getpgid", _getpgid)
        monkeypatch.setattr(proc_control.os, "killpg", _killpg)
        monkeypatch.setattr(POSIX, "group_member_pids", lambda leader: [5, 10])
        assert POSIX.kill_detached_descendant_groups(5) == 1
        assert killed == [999]

    def test_ignored_and_same_group_skipped(self, monkeypatch):
        killed = []
        monkeypatch.setattr(proc_control.os, "getpgid", lambda p: 100)
        monkeypatch.setattr(proc_control.os, "killpg", lambda g, s: killed.append(g))
        monkeypatch.setattr(POSIX, "group_member_pids", lambda leader: [5, 10])
        # own == 100, descendant also 100 ⇒ nothing killed
        assert POSIX.kill_detached_descendant_groups(5, frozenset({100})) == 0
        assert killed == []

    def test_descendant_getpgid_failure_skipped(self, monkeypatch):
        def _getpgid(p):
            if p == 10:
                raise ProcessLookupError()
            return 100

        monkeypatch.setattr(proc_control.os, "getpgid", _getpgid)
        monkeypatch.setattr(proc_control.os, "killpg", lambda g, s: None)
        monkeypatch.setattr(POSIX, "group_member_pids", lambda leader: [5, 10])
        assert POSIX.kill_detached_descendant_groups(5) == 0

    def test_killpg_failure_swallowed(self, monkeypatch):
        monkeypatch.setattr(proc_control.os, "getpgid", lambda p: 200 if p == 10 else 100)
        monkeypatch.setattr(
            proc_control.os, "killpg", lambda g, s: (_ for _ in ()).throw(ProcessLookupError())
        )
        monkeypatch.setattr(POSIX, "group_member_pids", lambda leader: [5, 10])
        # group signalled was attempted but failed ⇒ not counted
        assert POSIX.kill_detached_descendant_groups(5) == 0


# --------------------------------------------------------------- kill conveniences


def _spawn(args, **kw):
    return subprocess.Popen(args, **POSIX.detach_spawn_kwargs(), **kw)


class TestKillTree:
    def test_kill_tree_force_kills(self):
        child = _spawn(["sleep", "5"])
        POSIX.kill_tree(child)
        assert child.poll() is not None

    def test_terminate_tree_already_dead_returns_false(self):
        child = _spawn(["true"])
        child.wait()
        assert POSIX.terminate_tree(child, timeout=0.5) is False

    def test_terminate_tree_polite_exit_returns_false(self):
        child = _spawn(["sleep", "3"])
        assert POSIX.terminate_tree(child, timeout=2.0) is False
        assert child.poll() is not None

    def test_terminate_tree_force_needed_returns_true(self, monkeypatch):
        # Polite stop request deterministically ignored (no-op) ⇒ deadline
        # elapses ⇒ force_kill path taken. force_kill stays a real SIGKILL
        # against a live detached process.
        child = _spawn(["sleep", "5"])
        monkeypatch.setattr(POSIX, "signal_stop", lambda pid: None)
        assert POSIX.terminate_tree(child, timeout=0.3) is True
        # terminate_tree returns right after SIGKILL without waiting; reap to
        # confirm death and avoid leaving the child alive.
        child.wait(timeout=5)
        assert child.poll() is not None


# --------------------------------------------------------------- windows control


class TestWindowsControl:
    def test_has_detached_descendants_always_false(self):
        assert WIN.has_detached_descendants(1) is False

    def test_kill_detached_descendant_groups_noop(self):
        assert WIN.kill_detached_descendant_groups(1) == 0

    def test_detach_kwargs_composes_flags(self, monkeypatch):
        monkeypatch.setattr(proc_control.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, raising=False)
        monkeypatch.setattr(proc_control.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
        monkeypatch.delenv("BETTER_AGENT_RUN_SH_SERVICE_CHILD", raising=False)
        kw = WIN.detach_spawn_kwargs()
        assert kw == {"creationflags": 0x200 | 0x08000000}

    def test_detach_kwargs_service_child_flag(self, monkeypatch):
        monkeypatch.setattr(proc_control.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, raising=False)
        monkeypatch.setattr(proc_control.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
        monkeypatch.setattr(proc_control.subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000, raising=False)
        monkeypatch.setenv("BETTER_AGENT_RUN_SH_SERVICE_CHILD", "1")
        kw = WIN.detach_spawn_kwargs()
        assert kw == {"creationflags": 0x200 | 0x08000000 | 0x01000000}

    def test_signal_stop_taskkill_no_force(self, monkeypatch):
        captured = {}

        def _fake_run(args, **kwargs):
            captured["args"] = args
            return _RunResult()

        monkeypatch.setattr(proc_control.subprocess, "run", _fake_run)
        WIN.signal_stop(777)
        assert captured["args"] == ["taskkill", "/PID", "777", "/T"]
        assert "/F" not in captured["args"]

    def test_force_kill_taskkill_with_force(self, monkeypatch):
        captured = {}

        def _fake_run(args, **kwargs):
            captured["args"] = args
            return _RunResult()

        monkeypatch.setattr(proc_control.subprocess, "run", _fake_run)
        WIN.force_kill(777)
        assert captured["args"] == ["taskkill", "/PID", "777", "/T", "/F"]

    def test_taskkill_swallows_subprocess_error(self, monkeypatch, caplog):
        def _raise(*args, **kwargs):
            raise subprocess.SubprocessError("nope")

        monkeypatch.setattr(proc_control.subprocess, "run", _raise)
        with caplog.at_level("WARNING", logger="proc_control"):
            # Must not raise.
            WIN.force_kill(777)
        assert any("taskkill failed" in r.message for r in caplog.records)

    def test_windows_group_member_pids_walk(self, monkeypatch):
        # wmic rows are "ParentProcessId ProcessId" (parts[0]=ppid, [1]=pid).
        stdout = "5 10\n10 20\n5 11\n"  # 5->10->20, 5->11
        _set_run(monkeypatch, stdout)
        assert set(WIN.group_member_pids(5)) == {5, 10, 11, 20}

    def test_windows_group_member_pids_skips_malformed(self, monkeypatch):
        stdout = "foo bar baz\n5 10\nzz qq\n"
        _set_run(monkeypatch, stdout)
        assert set(WIN.group_member_pids(5)) == {5, 10}

    def test_windows_group_member_pids_failure_returns_leader(self, monkeypatch):
        _set_run(monkeypatch, "", raises=OSError("boom"))
        assert WIN.group_member_pids(5) == [5]

    def test_windows_group_member_pids_cycle_safe(self, monkeypatch):
        # A ppid cycle must terminate, exercising the already-seen branch.
        stdout = "5 10\n10 5\n"
        _set_run(monkeypatch, stdout)
        assert set(WIN.group_member_pids(5)) == {5, 10}


# --------------------------------------------------------------- singleton


class TestProcessControlSingleton:
    def test_returns_cached_instance(self, monkeypatch):
        # Force POSIX path and verify the singleton caches.
        monkeypatch.setattr(proc_control.os, "name", "posix")
        proc_control._INSTANCE = None
        first = proc_control.process_control()
        second = proc_control.process_control()
        assert first is second
        assert isinstance(first, _PosixProcessControl)
        # Restore so other tests are unaffected.
        proc_control._INSTANCE = None
