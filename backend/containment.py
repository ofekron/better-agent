"""Escape-proof process containment for runner trees.

Tracks EVERY descendant of a runner — nested to infinity, including
processes that double-fork / setsid / daemonize and reparent to init — so
the backend can keep a session's running + monitoring state accurate. The
ppid walk in ``proc_control.py`` is BLIND to a reparented orphan (its parent
link to the runner is gone); OS containment is not:

  * Linux   — cgroup v2. Every descendant is in the run's cgroup;
              ``cgroup.procs`` enumerates them regardless of how they
              detach. A process cannot leave a cgroup without write access
              to another, which it does not have. GUARANTEED.
  * Windows — a named Job Object. Descendants cannot break away (we never
              set ``JOB_OBJECT_LIMIT_BREAKAWAY_OK``); the job's process-id
              list enumerates them. The handle is re-openable by name.
              GUARANTEED (modulo the spawn-assign race, see ``after_spawn``).
  * macOS   — NO containment primitive exists without EndpointSecurity
              (entitlement + signed system extension + root). Falls back to
              the ppid walk — BEST-EFFORT, can miss a reparented orphan.
              ``guaranteed`` is False so callers surface a "degraded" state.

Keyed by ``run_id`` so a backend restart can ``reattach`` to a container
that outlived it (Linux: the cgroup path; Windows: the job name) — the exact
moment a reparented orphan matters most. macOS cannot reattach to a
reparented orphan (no handle survives) — that is the documented best-effort
gap, surfaced via ``guaranteed``.

Fail-closed: if the guaranteed mechanism is unavailable on a platform that
should have it (e.g. Linux without a writable/delegated cgroup), ``create``
raises ``ContainmentUnavailable`` — it MUST NOT silently degrade to the
blind ppid walk, which would reopen the orphan hole undetected.
"""

from __future__ import annotations

import abc
import logging
import os
import subprocess
from typing import Callable, Optional

logger = logging.getLogger(__name__)


class ContainmentUnavailable(RuntimeError):
    """The guaranteed containment mechanism is unavailable on this host and
    the platform offers no acceptable substitute. Callers must fail closed."""


class Containment(abc.ABC):
    """Per-platform escape-proof membership tracking, keyed by run_id."""

    #: True iff this backend enumerates EVERY descendant (escape-proof).
    #: False = best-effort (macOS ppid walk) — a reparented orphan can be
    #: missed; callers surface "degraded".
    guaranteed: bool = False

    @abc.abstractmethod
    def create(self, run_id: str) -> None:
        """Prepare a container for ``run_id`` BEFORE the runner is spawned.
        Raises ``ContainmentUnavailable`` if the guaranteed mechanism can't
        be set up (fail closed)."""

    @abc.abstractmethod
    def spawn_kwargs(self, run_id: str) -> dict:
        """Extra ``subprocess.Popen`` kwargs that enroll the child (and thus
        all its descendants) into the container at spawn — e.g. a Linux
        ``preexec_fn`` that joins the cgroup before exec. Compose with
        ``proc_control.detach_spawn_kwargs``; never collide keys."""

    @abc.abstractmethod
    def after_spawn(self, run_id: str, runner_pid: int) -> None:
        """Finalize enrollment once the runner pid is known (and release any
        transient resources opened in ``create``)."""

    @abc.abstractmethod
    def reattach(self, run_id: str, runner_pid: Optional[int]) -> None:
        """Re-acquire a container that outlived a backend restart, by
        ``run_id`` alone (Linux: cgroup path; Windows: job name). ``runner_pid``
        is required only for the macOS best-effort backend."""

    @abc.abstractmethod
    def enumerate(self, run_id: str) -> list[int]:
        """Every live pid in the container. Complete + escape-proof where
        ``guaranteed``; best-effort ppid walk on macOS. ``[]`` if gone."""

    @abc.abstractmethod
    def teardown(self, run_id: str) -> None:
        """Release the container. NEVER kills members (never-kill rule) —
        only drops handles / removes the cgroup dir if already empty."""

    @abc.abstractmethod
    def force_kill_all(self, run_id: str) -> int:
        """Hard-kill EVERY live member of the container. Idempotent;
        tolerant of an already-gone run_id (returns 0, no raise). Returns
        the count of pids signalled. ONLY for delete-tier paths
        (cancel_session) — distinct from `teardown` which preserves the
        never-kill invariant."""

    # ---- shared, platform-independent --------------------------------
    def has_background_work(self, run_id: str, runner_pid: int) -> bool:
        """True iff a live member is *background work* — a descendant the
        CLI setsid'd into its own process group (run_in_background bash,
        Monitor loop), as opposed to infrastructure (the CLI, its MCP
        servers, transient foreground tools) which stays in the runner's
        group. Sourced from the COMPLETE container membership, so a
        reparented orphan is included (unlike the old ppid walk).

        POSIX-only group semantics; the Windows backend overrides this."""
        try:
            own = os.getpgid(runner_pid)
        except (ProcessLookupError, PermissionError, OSError):
            return False
        for pid in self.enumerate(run_id):
            if pid == runner_pid:
                continue
            try:
                if os.getpgid(pid) != own:
                    return True
            except (ProcessLookupError, PermissionError, OSError):
                continue
        return False


# ======================================================================
# Linux — cgroup v2
# ======================================================================
class _LinuxCgroupContainment(Containment):
    guaranteed = True

    #: Root under which per-run cgroups are created. Must be a delegated /
    #: writable cgroup-v2 subtree (systemd ``Delegate=yes`` for the service,
    #: or root). We do NOT mkdir the unified root itself.
    _BASE = "/sys/fs/cgroup/better-agent"

    def __init__(self) -> None:
        self._procs_fd: dict[str, int] = {}

    def _dir(self, run_id: str) -> str:
        # run_id is backend-generated (uuid-ish); reject path tricks anyway.
        safe = os.path.basename(run_id)
        if safe != run_id or not safe or safe in (".", ".."):
            raise ContainmentUnavailable(f"unsafe run_id for cgroup: {run_id!r}")
        return os.path.join(self._BASE, safe)

    def create(self, run_id: str) -> None:
        d = self._dir(run_id)
        try:
            os.makedirs(d, exist_ok=True)
            # Open cgroup.procs now so the child's preexec_fn can join with
            # an async-signal-safe os.write (no open() in the forked child).
            fd = os.open(os.path.join(d, "cgroup.procs"), os.O_WRONLY)
        except (FileNotFoundError, PermissionError, OSError) as e:
            raise ContainmentUnavailable(
                f"cgroup v2 unavailable/undelegated at {self._BASE}: {e}"
            ) from e
        self._procs_fd[run_id] = fd

    def spawn_kwargs(self, run_id: str) -> dict:
        fd = self._procs_fd[run_id]

        def _join_cgroup() -> None:
            # Runs in the forked child before exec. Writing "0" enrolls the
            # calling process; every descendant inherits the cgroup and
            # cannot leave it. os.write is async-signal-safe.
            os.write(fd, b"0")

        return {"preexec_fn": _join_cgroup}

    def after_spawn(self, run_id: str, runner_pid: int) -> None:
        fd = self._procs_fd.pop(run_id, None)
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

    def reattach(self, run_id: str, runner_pid: Optional[int]) -> None:
        # Stateless: enumeration reads the cgroup path, which survived the
        # restart. Nothing to rebuild.
        self._dir(run_id)  # validates run_id

    def enumerate(self, run_id: str) -> list[int]:
        try:
            with open(os.path.join(self._dir(run_id), "cgroup.procs"),
                      encoding="ascii") as f:
                return [int(line) for line in f if line.strip()]
        except (FileNotFoundError, PermissionError, OSError, ValueError):
            return []

    def teardown(self, run_id: str) -> None:
        fd = self._procs_fd.pop(run_id, None)
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        # rmdir only succeeds when the cgroup is empty — i.e. no members
        # left. Never kills (never-kill rule); a non-empty cgroup stays.
        try:
            os.rmdir(self._dir(run_id))
        except (FileNotFoundError, OSError):
            pass

    def force_kill_all(self, run_id: str) -> int:
        # cgroup v2: writing "1" to cgroup.kill SIGKILLs every member
        # atomically. Idempotent — an already-empty/missing cgroup is a
        # no-op. Counts members enumerated just before the write for the
        # return value (best-effort; the kill itself is atomic).
        try:
            members = self.enumerate(run_id)
        except (FileNotFoundError, PermissionError, OSError):
            members = []
        try:
            with open(os.path.join(self._dir(run_id), "cgroup.kill"),
                      "w", encoding="ascii") as f:
                f.write("1")
        except (FileNotFoundError, PermissionError, OSError, ContainmentUnavailable):
            return 0
        return len(members)


# ======================================================================
# Windows — named Job Object
# ======================================================================
class _WindowsJobContainment(Containment):
    guaranteed = True

    def __init__(self) -> None:
        self._handles: dict[str, int] = {}

    @staticmethod
    def _name(run_id: str) -> str:
        return f"Local\\better-agent-{os.path.basename(run_id)}"

    def _k32(self):
        import ctypes
        return ctypes.windll.kernel32  # type: ignore[attr-defined]

    def create(self, run_id: str) -> None:
        import ctypes
        k32 = self._k32()
        h = k32.CreateJobObjectW(None, self._name(run_id))
        if not h:
            raise ContainmentUnavailable(
                f"CreateJobObjectW failed: {ctypes.get_last_error()}"
            )
        self._handles[run_id] = h

    def spawn_kwargs(self, run_id: str) -> dict:
        # No spawn-time enrollment hook on Windows; assignment happens in
        # after_spawn. (A fully race-free design needs CREATE_SUSPENDED +
        # ResumeThread via a raw CreateProcess; subprocess.Popen does not
        # expose the primary thread handle. The runner does Python startup
        # before it spawns the CLI, so the assign-after-spawn window holds
        # only the runner itself — no descendants escape in practice. This
        # residual race is the one gap vs. cgroups; see module docstring.)
        return {}

    def after_spawn(self, run_id: str, runner_pid: int) -> None:
        import ctypes
        k32 = self._k32()
        PROCESS_SET_QUOTA = 0x0100
        PROCESS_TERMINATE = 0x0001
        hproc = k32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE,
                                False, runner_pid)
        if not hproc:
            raise ContainmentUnavailable(
                f"OpenProcess({runner_pid}) failed: {ctypes.get_last_error()}"
            )
        try:
            if not k32.AssignProcessToJobObject(self._handles[run_id], hproc):
                raise ContainmentUnavailable(
                    f"AssignProcessToJobObject failed: {ctypes.get_last_error()}"
                )
        finally:
            k32.CloseHandle(hproc)

    def reattach(self, run_id: str, runner_pid: Optional[int]) -> None:
        import ctypes
        k32 = self._k32()
        JOB_OBJECT_QUERY = 0x0004
        h = k32.OpenJobObjectW(JOB_OBJECT_QUERY, False, self._name(run_id))
        if not h:
            raise ContainmentUnavailable(
                f"OpenJobObjectW({run_id}) failed: {ctypes.get_last_error()}"
            )
        self._handles[run_id] = h

    def enumerate(self, run_id: str) -> list[int]:
        import ctypes
        from ctypes import wintypes
        h = self._handles.get(run_id)
        if not h:
            return []

        # JOBOBJECT_BASIC_PROCESS_ID_LIST with room for many pids.
        class _IDLIST(ctypes.Structure):
            _fields_ = [
                ("NumberOfAssignedProcesses", wintypes.DWORD),
                ("NumberOfProcessIdsInList", wintypes.DWORD),
                ("ProcessIdList", ctypes.c_void_p * 4096),
            ]

        k32 = self._k32()
        info = _IDLIST()
        JobObjectBasicProcessIdList = 3
        ok = k32.QueryInformationJobObject(
            h, JobObjectBasicProcessIdList, ctypes.byref(info),
            ctypes.sizeof(info), None,
        )
        if not ok:
            return []
        n = info.NumberOfProcessIdsInList
        return [int(info.ProcessIdList[i]) for i in range(min(n, 4096))]

    def teardown(self, run_id: str) -> None:
        # Close our handle WITHOUT KILL_ON_JOB_CLOSE (never set), so members
        # keep running — the job persists while any assigned process lives.
        h = self._handles.pop(run_id, None)
        if h:
            self._k32().CloseHandle(h)

    def force_kill_all(self, run_id: str) -> int:
        # TerminateJobObject kills every assigned process atomically.
        # Idempotent: an already-closed handle is a no-op.
        h = self._handles.get(run_id)
        if not h:
            return 0
        try:
            members = self.enumerate(run_id)
        except Exception:
            members = []
        try:
            # TerminateJobObject(handle, exit_code). Best-effort; closing
            # the handle is left to teardown.
            self._k32().TerminateJobObject(h, 1)
        except Exception:
            return 0
        return len(members)

    def has_background_work(self, run_id: str, runner_pid: int) -> bool:
        # Windows has no process-group concept; the "own group vs detached"
        # heuristic does not apply. Coarse signal: any member besides the
        # runner is treated as live work. (Refinement is a follow-up; this
        # backend is unverified on a real Windows host.)
        return any(pid != runner_pid for pid in self.enumerate(run_id))


# ======================================================================
# macOS — best-effort ppid walk (NO real containment)
# ======================================================================
class _DarwinBestEffortContainment(Containment):
    guaranteed = False

    def __init__(self) -> None:
        self._runner_pid: dict[str, int] = {}

    def create(self, run_id: str) -> None:
        pass

    def spawn_kwargs(self, run_id: str) -> dict:
        # start_new_session (proc_control.detach_spawn_kwargs) already roots
        # the tree; the ppid walk traverses it. No extra kwargs.
        return {}

    def after_spawn(self, run_id: str, runner_pid: int) -> None:
        self._runner_pid[run_id] = runner_pid

    def reattach(self, run_id: str, runner_pid: Optional[int]) -> None:
        if runner_pid is None:
            return
        self._runner_pid[run_id] = runner_pid

    def enumerate(self, run_id: str) -> list[int]:
        pid = self._runner_pid.get(run_id)
        if pid is None:
            return []
        from proc_control import process_control
        return process_control().group_member_pids(pid)

    def teardown(self, run_id: str) -> None:
        self._runner_pid.pop(run_id, None)

    def force_kill_all(self, run_id: str) -> int:
        # No OS containment on macOS — fall back to the ppid walk via
        # proc_control. Requires the runner pid to be live; if the runner
        # already exited, the descendant chain is broken and reparented
        # orphans are unreachable. Documented best-effort gap.
        pid = self._runner_pid.get(run_id)
        if pid is None:
            return 0
        from proc_control import process_control
        pc = process_control()
        if not pc.pid_alive(pid):
            return 0
        # Two passes: (1) kill detached descendant groups (setsid'd bg
        # shells), (2) SIGKILL the runner's own pgroup to take down the
        # runner, CLI, MCP servers, and same-group children together.
        swept = pc.kill_detached_descendant_groups(pid)
        try:
            pc.force_kill(pid)
        except Exception:
            pass
        return swept + 1


_INSTANCE: Optional[Containment] = None


def containment() -> Containment:
    """The Containment backend for this platform (cached singleton)."""
    global _INSTANCE
    if _INSTANCE is None:
        import sys
        if os.name == "nt":
            _INSTANCE = _WindowsJobContainment()
        elif sys.platform == "linux":
            _INSTANCE = _LinuxCgroupContainment()
        else:
            _INSTANCE = _DarwinBestEffortContainment()
    return _INSTANCE
