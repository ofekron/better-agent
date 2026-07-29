"""Escape-proof process containment for runner trees.

Tracks EVERY descendant of a runner — nested to infinity, including
processes that double-fork / setsid / daemonize and reparent to init — so
the backend can keep a session's running + monitoring state accurate. The
ppid walk in ``proc_control.py`` is BLIND to a reparented orphan (its parent
link to the runner is gone); OS containment is not:

  * Linux   — cgroup v2, or the cgroup-v1 pids hierarchy on hybrid
              kernels. Every descendant inherits the run's cgroup and is
              enumerated regardless of how it detaches. A process cannot
              leave without write access to another cgroup. GUARANTEED.
              WSL hosts without a delegated cgroup use an explicitly
              degraded process-tree tracker, surfaced as not guaranteed.
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
raises ``ContainmentUnavailable``. WSL without either cgroup hierarchy is
the explicit exception because its host kernel may not expose one; callers
receive ``guaranteed=False`` rather than a false containment guarantee.
"""

from __future__ import annotations

import abc
import logging
import os
import stat
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
# Linux — cgroup v2 / v1 pids
# ======================================================================
def _unescape_mountinfo_path(value: str) -> str:
    for escaped, literal in (
        ("\\040", " "),
        ("\\011", "\t"),
        ("\\012", "\n"),
        ("\\134", "\\"),
    ):
        value = value.replace(escaped, literal)
    return value


def _current_cgroup_v2_directory(
    mountinfo_path: str = "/proc/self/mountinfo",
    cgroup_path: str = "/proc/self/cgroup",
    *,
    pid: int | None = None,
) -> str:
    pid = os.getpid() if pid is None else pid
    membership = None
    try:
        with open(cgroup_path, encoding="ascii") as stream:
            for line in stream:
                hierarchy, controllers, path = line.rstrip("\n").split(":", 2)
                if hierarchy == "0" and not controllers:
                    membership = os.path.normpath(path)
                    break
        if membership is None or not membership.startswith("/"):
            raise ValueError("process has no cgroup v2 membership")

        with open(mountinfo_path, encoding="utf-8") as stream:
            for line in stream:
                before, separator, after = line.rstrip("\n").partition(" - ")
                fields = before.split()
                if not separator or len(fields) < 5 or not after.startswith("cgroup2 "):
                    continue
                mount_root = os.path.normpath(_unescape_mountinfo_path(fields[3]))
                mount_point = os.path.normpath(_unescape_mountinfo_path(fields[4]))
                if membership == mount_root:
                    relative = "."
                elif membership.startswith(mount_root.rstrip("/") + "/"):
                    relative = os.path.relpath(membership, mount_root)
                else:
                    continue
                resolved = os.path.normpath(os.path.join(mount_point, relative))
                if os.path.commonpath((mount_point, resolved)) != mount_point:
                    raise ValueError("cgroup v2 membership escapes mount")
                try:
                    with open(os.path.join(resolved, "cgroup.procs"), encoding="ascii") as procs:
                        members = {int(value) for value in procs.read().split()}
                    if pid not in members:
                        continue
                    if not os.path.isfile(os.path.join(resolved, "cgroup.controllers")):
                        continue
                    if not os.path.isfile(os.path.join(resolved, "cgroup.type")):
                        continue
                except (OSError, ValueError):
                    continue
                return resolved
    except (OSError, ValueError) as exc:
        raise ContainmentUnavailable(f"cannot discover cgroup v2 hierarchy: {exc}") from exc
    raise ContainmentUnavailable("cannot discover cgroup v2 hierarchy")


def _current_cgroup_v1_pids_directory(
    mountinfo_path: str = "/proc/self/mountinfo",
    cgroup_path: str = "/proc/self/cgroup",
    *,
    pid: int | None = None,
) -> str:
    pid = os.getpid() if pid is None else pid
    membership = None
    try:
        with open(cgroup_path, encoding="ascii") as stream:
            for line in stream:
                _, controllers, path = line.rstrip("\n").split(":", 2)
                if "pids" in controllers.split(","):
                    membership = os.path.normpath(path)
                    break
        if membership is None or not membership.startswith("/"):
            raise ValueError("process has no cgroup v1 pids membership")

        with open(mountinfo_path, encoding="utf-8") as stream:
            for line in stream:
                before, separator, after = line.rstrip("\n").partition(" - ")
                fields = before.split()
                after_fields = after.split()
                if (
                    not separator
                    or len(fields) < 5
                    or len(after_fields) < 3
                    or after_fields[0] != "cgroup"
                    or "pids" not in after_fields[2].split(",")
                ):
                    continue
                mount_root = os.path.normpath(_unescape_mountinfo_path(fields[3]))
                mount_point = os.path.normpath(_unescape_mountinfo_path(fields[4]))
                if membership == mount_root:
                    relative = "."
                elif membership.startswith(mount_root.rstrip("/") + "/"):
                    relative = os.path.relpath(membership, mount_root)
                else:
                    continue
                resolved = os.path.normpath(os.path.join(mount_point, relative))
                if os.path.commonpath((mount_point, resolved)) != mount_point:
                    raise ValueError("cgroup v1 membership escapes mount")
                try:
                    with open(os.path.join(resolved, "tasks"), encoding="ascii") as tasks:
                        members = {int(value) for value in tasks.read().split()}
                    if pid not in members:
                        continue
                    if not os.path.isfile(os.path.join(resolved, "pids.current")):
                        continue
                except (OSError, ValueError):
                    continue
                return resolved
    except (OSError, ValueError) as exc:
        raise ContainmentUnavailable(
            f"cannot discover cgroup v1 pids hierarchy: {exc}"
        ) from exc
    raise ContainmentUnavailable("cannot discover cgroup v1 pids hierarchy")


class _LinuxCgroupContainment(Containment):
    guaranteed = True

    def __init__(self, *, cgroup_directory: str | None = None) -> None:
        self._procs_fd: dict[str, int] = {}
        parent = cgroup_directory or _current_cgroup_v2_directory()
        self._base = os.path.join(parent, "better-agent")

    def _dir(self, run_id: str) -> str:
        # run_id is backend-generated (uuid-ish); reject path tricks anyway.
        safe = os.path.basename(run_id)
        if safe != run_id or not safe or safe in (".", ".."):
            raise ContainmentUnavailable(f"unsafe run_id for cgroup: {run_id!r}")
        return os.path.join(self._base, safe)

    def _can_create_run_group(self) -> bool:
        try:
            base = os.lstat(self._base)
        except FileNotFoundError:
            return os.access(os.path.dirname(self._base), os.W_OK | os.X_OK)
        except OSError:
            return False
        return (
            stat.S_ISDIR(base.st_mode)
            and base.st_uid == os.geteuid()
            and os.access(self._base, os.W_OK | os.X_OK)
        )

    def create(self, run_id: str) -> None:
        d = self._dir(run_id)
        base_fd = None
        run_fd = None
        try:
            try:
                os.mkdir(self._base, 0o700)
            except FileExistsError:
                pass
            base_stat = os.lstat(self._base)
            if not stat.S_ISDIR(base_stat.st_mode) or base_stat.st_uid != os.geteuid():
                raise OSError("cgroup base is not an owned directory")
            os.chmod(self._base, 0o700)
            base_fd = os.open(
                self._base,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            )
            safe = os.path.basename(d)
            try:
                os.mkdir(safe, 0o700, dir_fd=base_fd)
            except FileExistsError:
                pass
            run_fd = os.open(
                safe,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=base_fd,
            )
            run_stat = os.fstat(run_fd)
            if run_stat.st_uid != os.geteuid():
                raise OSError("run cgroup is not owned by the backend user")
            os.fchmod(run_fd, 0o700)
            # Open cgroup.procs now so the child's preexec_fn can join with
            # an async-signal-safe os.write (no open() in the forked child).
            fd = os.open(
                "cgroup.procs",
                os.O_WRONLY | os.O_NOFOLLOW,
                dir_fd=run_fd,
            )
        except (FileNotFoundError, PermissionError, OSError) as e:
            raise ContainmentUnavailable(
                f"cgroup v2 unavailable/undelegated at {self._base}: {e}"
            ) from e
        finally:
            if run_fd is not None:
                os.close(run_fd)
            if base_fd is not None:
                os.close(base_fd)
        self._procs_fd[run_id] = fd

    def spawn_kwargs(self, run_id: str) -> dict:
        fd = self._procs_fd[run_id]

        def _join_cgroup() -> None:
            # Runs in the forked child before exec. Writing "0" enrolls the
            # calling process; every descendant inherits the cgroup and
            # cannot leave it. os.write is async-signal-safe.
            os.write(fd, b"0")

        return {
            "preexec_fn": _join_cgroup,
            "pass_fds": (fd,),
        }

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


class _LinuxCgroupV1PidsContainment(_LinuxCgroupContainment):
    def __init__(self, *, cgroup_directory: str | None = None) -> None:
        self._procs_fd = {}
        parent = cgroup_directory or _current_cgroup_v1_pids_directory()
        self._base = os.path.join(parent, "better-agent")

    def create(self, run_id: str) -> None:
        d = self._dir(run_id)
        base_fd = None
        run_fd = None
        try:
            try:
                os.mkdir(self._base, 0o700)
            except FileExistsError:
                pass
            base_stat = os.lstat(self._base)
            if not stat.S_ISDIR(base_stat.st_mode) or base_stat.st_uid != os.geteuid():
                raise OSError("cgroup base is not an owned directory")
            os.chmod(self._base, 0o700)
            base_fd = os.open(
                self._base,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            )
            safe = os.path.basename(d)
            try:
                os.mkdir(safe, 0o700, dir_fd=base_fd)
            except FileExistsError:
                pass
            run_fd = os.open(
                safe,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=base_fd,
            )
            run_stat = os.fstat(run_fd)
            if run_stat.st_uid != os.geteuid():
                raise OSError("run cgroup is not owned by the backend user")
            os.fchmod(run_fd, 0o700)
            fd = os.open(
                "tasks",
                os.O_WRONLY | os.O_NOFOLLOW,
                dir_fd=run_fd,
            )
        except (FileNotFoundError, PermissionError, OSError) as exc:
            raise ContainmentUnavailable(
                f"cgroup v1 pids unavailable/undelegated at {self._base}: {exc}"
            ) from exc
        finally:
            if run_fd is not None:
                os.close(run_fd)
            if base_fd is not None:
                os.close(base_fd)
        self._procs_fd[run_id] = fd

    def enumerate(self, run_id: str) -> list[int]:
        try:
            with open(os.path.join(self._dir(run_id), "tasks"), encoding="ascii") as tasks:
                return [int(line) for line in tasks if line.strip()]
        except (FileNotFoundError, PermissionError, OSError, ValueError):
            return []

    def force_kill_all(self, run_id: str) -> int:
        signalled = set()
        for _ in range(1024):
            members = self.enumerate(run_id)
            if not members:
                break
            for pid in members:
                try:
                    os.kill(pid, 9)
                    signalled.add(pid)
                except (ProcessLookupError, PermissionError, OSError):
                    pass
        return len(signalled)


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
# POSIX fallback — best-effort ppid walk (NO real containment)
# ======================================================================
class _PosixBestEffortContainment(Containment):
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
        # No usable OS containment — fall back to the ppid walk via
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


class _LinuxWslBestEffortContainment(_PosixBestEffortContainment):
    """Explicit degraded containment for WSL hosts without usable cgroups."""


def _is_wsl(osrelease_path: str = "/proc/sys/kernel/osrelease") -> bool:
    if os.environ.get("WSL_INTEROP") or os.environ.get("WSL_DISTRO_NAME"):
        return True
    try:
        with open(osrelease_path, encoding="ascii") as handle:
            return "microsoft" in handle.read().lower()
    except OSError:
        return False


_INSTANCE: Optional[Containment] = None


def containment() -> Containment:
    """The Containment backend for this platform (cached singleton)."""
    global _INSTANCE
    if _INSTANCE is None:
        import sys
        if os.name == "nt":
            _INSTANCE = _WindowsJobContainment()
        elif sys.platform == "linux":
            try:
                candidate = _LinuxCgroupContainment()
                if not candidate._can_create_run_group():
                    raise ContainmentUnavailable(
                        f"cgroup v2 hierarchy is not delegated at {candidate._base}"
                    )
                _INSTANCE = candidate
            except ContainmentUnavailable:
                try:
                    candidate = _LinuxCgroupV1PidsContainment()
                    if not candidate._can_create_run_group():
                        raise ContainmentUnavailable(
                            f"cgroup v1 pids hierarchy is not delegated at {candidate._base}"
                        )
                    _INSTANCE = candidate
                except ContainmentUnavailable:
                    if not _is_wsl():
                        raise
                    _INSTANCE = _LinuxWslBestEffortContainment()
        else:
            _INSTANCE = _PosixBestEffortContainment()
    return _INSTANCE
