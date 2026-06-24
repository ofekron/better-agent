"""Diagnostic per-PID inspection used by the "why is this run still
running?" modal.

Walks the descendant tree of a root PID and reports each process's
status/CPU/memory/cmdline. No persistence, no broadcast — pure
read-side diagnostic. Uses ``pgrep -P`` and ``ps`` so the only
dependency is POSIX userland (works on macOS and Linux without
``psutil``).
"""
from __future__ import annotations

import logging
import os
import subprocess
from typing import Optional

logger = logging.getLogger(__name__)


# Single-letter ps state codes → human-readable description. Sourced
# from ps(1) on macOS and Linux — overlap is enough for our needs;
# unknown letters fall through as-is.
_STATE_DESC = {
    "R": "running",
    "S": "sleeping",
    "I": "idle",
    "D": "uninterruptible sleep (blocked on I/O)",
    "Z": "zombie (defunct)",
    "T": "stopped",
    "t": "stopped (debugger)",
    "X": "dead",
    "W": "paging",
}


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def _children_of(pid: int) -> list[int]:
    """Direct children of ``pid``. Empty on error."""
    try:
        out = subprocess.run(
            ["pgrep", "-P", str(pid)],
            capture_output=True, text=True, timeout=2.0,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        logger.warning("pgrep -P %s failed: %s", pid, e)
        return []
    if out.returncode not in (0, 1):  # 1 = no children
        return []
    result: list[int] = []
    for line in out.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            result.append(int(line))
        except ValueError:
            continue
    return result


def _descendants(root: int) -> list[int]:
    """Root + every descendant, BFS order, deduped, capped at 200 to
    keep a pathological fork bomb from runaway-walking."""
    seen: set[int] = set()
    order: list[int] = []
    queue: list[int] = [root]
    while queue and len(order) < 200:
        pid = queue.pop(0)
        if pid in seen:
            continue
        seen.add(pid)
        order.append(pid)
        queue.extend(_children_of(pid))
    return order


def _describe_state(code: str) -> str:
    if not code:
        return "unknown"
    # ps state can include flags after the primary letter (e.g. "S+",
    # "R<"). Use the first character for the description, surface the
    # whole string as the raw code.
    primary = code[0]
    return _STATE_DESC.get(primary, primary)


def _ps_info(pids: list[int]) -> dict[int, dict]:
    """One ``ps`` call for all pids. Returns {pid: {...}}; missing pids
    (died between pgrep and ps) get an alive=False stub upstream."""
    if not pids:
        return {}
    pid_args = ",".join(str(p) for p in pids)
    # NOTE: command= MUST be last — it's the only field that can
    # contain spaces. macOS and Linux both honor this ordering.
    fmt = "pid=,ppid=,stat=,%cpu=,rss=,etime=,command="
    try:
        out = subprocess.run(
            ["ps", "-o", fmt, "-p", pid_args],
            capture_output=True, text=True, timeout=3.0,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        logger.warning("ps -p %s failed: %s", pid_args, e)
        return {}
    result: dict[int, dict] = {}
    for line in out.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        # Six leading whitespace-separated fields, then command (which
        # may contain spaces).
        parts = line.split(None, 6)
        if len(parts) < 7:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
        except ValueError:
            continue
        stat = parts[2]
        try:
            cpu = float(parts[3])
        except ValueError:
            cpu = 0.0
        try:
            rss_kb = int(parts[4])
        except ValueError:
            rss_kb = 0
        etime = parts[5]
        cmd = parts[6]
        result[pid] = {
            "pid": pid,
            "ppid": ppid,
            "stat": stat,
            "state_desc": _describe_state(stat),
            "cpu_percent": cpu,
            "rss_kb": rss_kb,
            "elapsed": etime,
            "command": cmd,
            "alive": True,
        }
    return result


def inspect_process_tree(
    root_pid: Optional[int], run_id: Optional[str] = None,
) -> list[dict]:
    """Return one entry per pid in the run's process tree.

    The pid set is sourced from the run's CONTAINMENT handle when
    ``run_id`` is given and the platform enumerates it (Linux cgroup /
    Windows job) — escape-proof, so a process that double-forks / setsid's
    / reparents to init is STILL listed (the ppid walk would lose it). On
    macOS (best-effort) or when the container is empty, falls back to the
    ppid descendant walk. The two sets are unioned so nothing is dropped.

    ``root_pid=None`` → empty list (the run is registered but the
    provider hasn't stamped the PID yet; the caller surfaces this
    state via the run-level fields, not the per-process list).

    A dead root reports a single alive=False entry so the modal can
    say "this run thinks it's alive but the runner pid is gone" —
    that's the most diagnostic outcome the user can see.
    """
    if root_pid is None:
        return []
    if not _pid_alive(root_pid):
        return [{
            "pid": root_pid,
            "ppid": None,
            "stat": "",
            "state_desc": "dead (process not found)",
            "cpu_percent": 0.0,
            "rss_kb": 0,
            "elapsed": "",
            "command": "",
            "alive": False,
        }]
    pids = _descendants(root_pid)
    if run_id is not None:
        try:
            from containment import containment
            contained = containment().enumerate(run_id)
        except Exception:
            contained = []
        # Union: containment may include reparented orphans the ppid walk
        # misses; the ppid walk may include something pre-enrollment.
        seen = set(pids)
        for p in contained:
            if p not in seen:
                seen.add(p)
                pids.append(p)
    info = _ps_info(pids)
    result: list[dict] = []
    for p in pids:
        entry = info.get(p)
        if entry is None:
            # Raced — pgrep saw it, ps didn't. Surface as dead.
            result.append({
                "pid": p,
                "ppid": None,
                "stat": "",
                "state_desc": "dead (process not found)",
                "cpu_percent": 0.0,
                "rss_kb": 0,
                "elapsed": "",
                "command": "",
                "alive": False,
            })
        else:
            result.append(entry)
    return result
