"""Cross-platform control of detached subprocess *trees*.

The runner/provider layer spawns detached CLI subprocesses (claude,
gemini, codex), then later needs to (a) kill the whole process *tree*
and (b) probe a runner's liveness by pid. POSIX does both with process
groups: spawn with ``start_new_session=True`` so the child leads its own
group, ``os.killpg`` the group, and ``os.kill(pid, 0)`` to probe. None of
that maps to Windows —

  * ``os.getpgid`` / ``os.killpg`` / ``signal.SIGKILL`` don't exist, and
  * ``os.kill(pid, 0)`` on Windows does NOT probe — it calls
    ``TerminateProcess`` and *kills* the target with exit code 0.

So a naïve port would turn every liveness check into a process killer.

This module hides the difference behind one small interface with a POSIX
and a Windows implementation, chosen once per process. The POSIX path is
byte-for-byte the prior behavior; only Windows is new. Add a third OS by
adding a third ``ProcessControl`` subclass — call sites never branch on
platform.
"""

from __future__ import annotations

import abc
import logging
import os
import signal
import subprocess
import time

logger = logging.getLogger(__name__)

# Default grace period (seconds) between the polite stop request and the
# forced kill in ``terminate_tree``. Mirrors the prior inline 30×0.1s.
_DEFAULT_GRACE = 3.0


class ProcessControl(abc.ABC):
    """Platform-agnostic operations on a detached subprocess tree.

    Subclasses implement four primitives; the synchronous ``Popen``
    conveniences (``terminate_tree`` / ``kill_tree``) are built on them
    here so every platform shares the same wait/grace logic. The
    primitives take a raw ``pid`` so both synchronous ``subprocess.Popen``
    callers and asyncio ``Process`` callers (which only expose ``.pid`` +
    ``.returncode``) can drive their own wait loops."""

    # ---- primitives (per-platform) ----------------------------------
    @abc.abstractmethod
    def detach_spawn_kwargs(self) -> dict:
        """Extra spawn kwargs (Popen or asyncio) that make the child a
        detached process-tree root we can later kill as a unit."""

    @abc.abstractmethod
    def pid_alive(self, pid: int) -> bool:
        """True if ``pid`` is a live process. MUST NOT affect the target."""

    @abc.abstractmethod
    def signal_stop(self, pid: int) -> None:
        """Politely ask the whole tree under ``pid`` to exit (POSIX:
        SIGTERM to the process group; Windows: ``taskkill /T``). Best
        effort — never raises for an already-dead target."""

    @abc.abstractmethod
    def force_kill(self, pid: int) -> None:
        """Forcibly kill the whole tree under ``pid`` (POSIX: SIGKILL to
        the group; Windows: ``taskkill /T /F``). Best effort."""

    @abc.abstractmethod
    def group_member_pids(self, leader_pid: int) -> list[int]:
        """All pids in the descendant tree rooted at ``leader_pid`` (walked
        by parent-pid), including ``leader_pid`` itself. A ppid walk — NOT a
        process-group query — because the claude CLI spawns
        `run_in_background` shells in their OWN session/process-group
        (setsid), so a group query misses them; the ppid parent link
        survives setsid, so the tree walk finds a bg shell while its
        ancestor CLI is alive. Used to detect live background work. Best
        effort — returns ``[]`` if the leader is already gone."""

    @abc.abstractmethod
    def kill_detached_descendant_groups(
        self, leader_pid: int, ignore_pgids: frozenset[int] = frozenset(),
    ) -> int:
        """SIGKILL the process groups of any descendant that escaped the
        leader's own group — e.g. `run_in_background` bash shells the CLI
        spawned with setsid. `force_kill` (killpg on the leader's group)
        does NOT reach these; they're in their own session. So an explicit
        stop must sweep them separately or they orphan. Call BEFORE killing
        the leader (the ppid walk needs the ancestor chain intact).
        `ignore_pgids`: groups the caller deliberately spawned itself
        (e.g. the runner's canvas auto-start) — skipped, never signalled.
        Returns the count of distinct groups signalled. Best effort."""

    @abc.abstractmethod
    def has_detached_descendants(
        self, leader_pid: int, ignore_pgids: frozenset[int] = frozenset(),
    ) -> bool:
        """True iff a live ppid-descendant of ``leader_pid`` runs in a
        DIFFERENT process group than the leader — i.e. a background shell
        the CLI spawned with setsid (`run_in_background` bash, Monitor) is
        still alive. Same-group descendants — the CLI itself, its MCP
        servers, transient foreground tool processes — are infrastructure
        and do NOT count. `ignore_pgids`: detached groups the caller
        deliberately spawned itself (e.g. the runner's canvas auto-start)
        — excluded, or a service spawn would read as background work and
        the babysitter linger would never end. This is the babysitter
        runner's reap signal: no detached descendants ⇒ no background
        work ⇒ exit at turn end. Robust against MCP servers
        spawning/restarting at any time (they stay in the runner's
        group), unlike a process snapshot baseline."""

    # ---- conveniences for synchronous Popen callers -----------------
    def kill_tree(self, popen: subprocess.Popen) -> None:
        """Force-kill the process and all descendants, immediately."""
        self.force_kill(popen.pid)
        try:
            popen.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass

    def terminate_tree(
        self, popen: subprocess.Popen, *, timeout: float = _DEFAULT_GRACE
    ) -> bool:
        """Politely stop the tree, wait up to ``timeout`` for it to exit,
        then force-kill anything still alive. Returns True iff a forced
        kill was needed (i.e. the polite request was ignored)."""
        if popen.poll() is not None:
            return False
        self.signal_stop(popen.pid)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if popen.poll() is not None:
                return False
            time.sleep(0.1)
        self.force_kill(popen.pid)
        return True


class _PosixProcessControl(ProcessControl):
    """Process-group based control — the original behavior."""

    def detach_spawn_kwargs(self) -> dict:
        # New session ⇒ the runner becomes its own process-group leader,
        # so killpg(pgid, …) later reaches the CLI and all its children.
        return {"start_new_session": True}

    def pid_alive(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            # Exists but owned by another user — treat as alive.
            return True
        except OSError:
            return False
        return True

    def signal_stop(self, pid: int) -> None:
        self._killpg(pid, signal.SIGTERM)

    def force_kill(self, pid: int) -> None:
        self._killpg(pid, signal.SIGKILL)

    @staticmethod
    def _killpg(pid: int, sig: int) -> None:
        try:
            os.killpg(os.getpgid(pid), sig)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    def group_member_pids(self, leader_pid: int) -> list[int]:
        # Walk the descendant tree by ppid (NOT the process group — bg
        # shells get their own session via the CLI's setsid, escaping a
        # group query, but their ppid link to the CLI survives).
        try:
            out = subprocess.run(
                ["ps", "-axo", "pid=,ppid="],
                capture_output=True, text=True, timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return [leader_pid]
        children: dict[int, list[int]] = {}
        for line in out.stdout.splitlines():
            parts = line.split()
            if len(parts) != 2:
                continue
            try:
                pid, ppid = int(parts[0]), int(parts[1])
            except ValueError:
                continue
            children.setdefault(ppid, []).append(pid)
        seen = {leader_pid}
        stack = [leader_pid]
        while stack:
            for child in children.get(stack.pop(), []):
                if child not in seen:
                    seen.add(child)
                    stack.append(child)
        return list(seen)

    def has_detached_descendants(
        self, leader_pid: int, ignore_pgids: frozenset[int] = frozenset(),
    ) -> bool:
        try:
            own = os.getpgid(leader_pid)
        except (ProcessLookupError, PermissionError, OSError):
            return False
        for pid in self.group_member_pids(leader_pid):
            if pid == leader_pid:
                continue
            try:
                pgid = os.getpgid(pid)
            except (ProcessLookupError, PermissionError, OSError):
                continue
            if pgid != own and pgid not in ignore_pgids:
                return True
        return False

    def kill_detached_descendant_groups(
        self, leader_pid: int, ignore_pgids: frozenset[int] = frozenset(),
    ) -> int:
        try:
            own = os.getpgid(leader_pid)
        except (ProcessLookupError, PermissionError, OSError):
            own = None
        groups: set[int] = set()
        for pid in self.group_member_pids(leader_pid):
            if pid == leader_pid:
                continue
            try:
                g = os.getpgid(pid)
            except (ProcessLookupError, PermissionError, OSError):
                continue
            if g != own and g not in ignore_pgids:
                groups.add(g)
        signalled = 0
        for g in groups:
            try:
                os.killpg(g, signal.SIGKILL)
                signalled += 1
            except (ProcessLookupError, PermissionError, OSError):
                pass
        return signalled


class _WindowsProcessControl(ProcessControl):
    """Windows control via ``taskkill /T`` for tree termination and a
    Win32 handle probe for liveness."""

    def detach_spawn_kwargs(self) -> dict:
        # CREATE_NEW_PROCESS_GROUP roots a new group (so a stray Ctrl-C in
        # our console can't reach the child) — Windows analogue of
        # start_new_session for the kill-tree story.
        #
        # CREATE_NO_WINDOW (NOT DETACHED_PROCESS) for the console story:
        # the runner itself is silent (stdin=DEVNULL, stdout/stderr → log
        # files), but it spawns `claude.exe` via the SDK (which uses
        # `anyio.open_process` with no creationflags). With
        # DETACHED_PROCESS the runner has NO console, so when it later
        # spawns a console child Windows ALLOCATES A NEW console window
        # for that child — a `cmd`-style window pops up on every turn.
        # CREATE_NO_WINDOW instead gives the runner a hidden console,
        # which console grandchildren inherit silently. Per MSDN the two
        # flags are mutually exclusive (DETACHED_PROCESS wins if both
        # are set), so this is a replacement, not an addition.
        flags = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
        flags |= getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        return {"creationflags": flags}

    def pid_alive(self, pid: int) -> bool:
        import ctypes
        from ctypes import wintypes

        # SYNCHRONIZE lets us WaitForSingleObject; QUERY_LIMITED_INFORMATION
        # is enough to open most processes without elevation.
        SYNCHRONIZE = 0x00100000
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        WAIT_TIMEOUT = 0x00000102

        k32 = ctypes.windll.kernel32
        k32.OpenProcess.restype = wintypes.HANDLE
        handle = k32.OpenProcess(
            SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if not handle:
            # Couldn't open ⇒ gone (or access-denied, which for our own
            # detached children shouldn't happen — treat as not alive).
            return False
        try:
            # Signaled ⇒ exited; still timing out after 0ms ⇒ running.
            return k32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
        finally:
            k32.CloseHandle(handle)

    def signal_stop(self, pid: int) -> None:
        # taskkill without /F posts WM_CLOSE / a polite termination request
        # to the tree; the forced /F follows in terminate_tree if ignored.
        self._taskkill(pid, force=False)

    def force_kill(self, pid: int) -> None:
        self._taskkill(pid, force=True)

    def group_member_pids(self, leader_pid: int) -> list[int]:
        # Walk the descendant tree by ParentProcessId (the Windows analogue
        # of the POSIX process group that taskkill /T would reap).
        try:
            out = subprocess.run(
                ["wmic", "process", "get", "ProcessId,ParentProcessId"],
                capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            return [leader_pid]
        children: dict[int, list[int]] = {}
        for line in out.stdout.splitlines():
            parts = line.split()
            if len(parts) != 2:
                continue
            try:
                ppid, pid = int(parts[0]), int(parts[1])
            except ValueError:
                continue
            children.setdefault(ppid, []).append(pid)
        seen = {leader_pid}
        stack = [leader_pid]
        while stack:
            for child in children.get(stack.pop(), []):
                if child not in seen:
                    seen.add(child)
                    stack.append(child)
        return list(seen)

    def has_detached_descendants(
        self, leader_pid: int, ignore_pgids: frozenset[int] = frozenset(),
    ) -> bool:
        # POSIX process-group semantics don't apply on Windows; the
        # babysitter runner (and its bg-shell decoupling) targets POSIX.
        # Returning False means a Windows runner exits as soon as the
        # turn ends — acceptable until Windows is in scope.
        return False

    def kill_detached_descendant_groups(
        self, leader_pid: int, ignore_pgids: frozenset[int] = frozenset(),
    ) -> int:
        # No-op: `taskkill /T` (force_kill) already walks the whole
        # descendant tree on Windows, so detached children are covered.
        return 0

    @staticmethod
    def _taskkill(pid: int, *, force: bool) -> None:
        # /T kills the whole tree (the process and its descendants),
        # which is the Windows analogue of killpg on a process group.
        args = ["taskkill", "/PID", str(pid), "/T"]
        if force:
            args.append("/F")
        try:
            subprocess.run(
                args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("taskkill failed for pid=%d: %s", pid, exc)


_INSTANCE: ProcessControl | None = None


def process_control() -> ProcessControl:
    """The ProcessControl for this platform (cached singleton)."""
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = (
            _WindowsProcessControl() if os.name == "nt" else _PosixProcessControl()
        )
    return _INSTANCE
