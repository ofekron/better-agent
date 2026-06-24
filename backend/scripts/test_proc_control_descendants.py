"""Unit test for ProcessControl.has_detached_descendants — the babysitter
runner's reap signal.

The CLI keeps infrastructure (itself, MCP servers) in the runner's process
group, but spawns `run_in_background` shells with setsid (own group). So
"background work alive" == a live ppid-descendant in a DIFFERENT process
group. Verifies (POSIX):
1. A SAME-group descendant (ordinary child) does NOT count → False
   (this is what an MCP server / foreground tool looks like).
2. A DETACHED descendant (own session) DOES count → True
   (this is what a run_in_background bash shell looks like).
3. Killing the detached descendant flips it back to False → reap.
4. `ignore_pgids` excludes a runner-spawned service (canvas auto-start)
   from BOTH the signal and the cancel sweep — without it, the
   babysitter would linger forever behind its own service spawn.
"""
import os
import signal
import subprocess
import sys
import time

from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proc_control import process_control

pc = process_control()
failures = []


def check(cond, msg):
    print(("  PASS" if cond else "  FAIL") + f": {msg}")
    if not cond:
        failures.append(msg)


def spawn_leader(detached_child: bool):
    """Leader (own session, like the runner) that spawns one child — either
    in the leader's own group (same-group, like MCP) or detached (setsid,
    like a bg shell). Writes the child pid to a file."""
    pidfile = f"/tmp/pc_desc_child_{int(detached_child)}.pid"
    if os.path.exists(pidfile):
        os.remove(pidfile)
    sns = "True" if detached_child else "False"
    leader = subprocess.Popen(
        [sys.executable, "-c",
         "import subprocess,time;"
         f"c=subprocess.Popen(['sleep','40'], start_new_session={sns});"
         f"open({pidfile!r},'w').write(str(c.pid));"
         "time.sleep(40)"],
        start_new_session=True,
    )
    time.sleep(0.8)
    child = int(open(pidfile).read().strip())
    return leader, child, pidfile


# --- Scenario 1: same-group child must NOT count as background work ---
leader1, child1, pf1 = spawn_leader(detached_child=False)
try:
    check(pc.pid_alive(child1), "same-group child alive")
    check(os.getpgid(child1) == os.getpgid(leader1.pid),
          "same-group child shares the leader's process group")
    check(not pc.has_detached_descendants(leader1.pid),
          "has_detached_descendants False for same-group child (infra-like)")
finally:
    try:
        os.killpg(os.getpgid(leader1.pid), signal.SIGKILL)
    except (ProcessLookupError, OSError):
        pass
    try:
        os.kill(child1, signal.SIGKILL)
    except (ProcessLookupError, OSError):
        pass
    if os.path.exists(pf1):
        os.remove(pf1)

# --- Scenario 2: detached child DOES count, and clears when killed ---
leader2, child2, pf2 = spawn_leader(detached_child=True)
try:
    check(pc.pid_alive(child2), "detached child alive")
    check(os.getpgid(child2) != os.getpgid(leader2.pid),
          "detached child is in its OWN process group")
    check(pc.has_detached_descendants(leader2.pid),
          "has_detached_descendants True for detached child (bg-shell-like)")
    os.kill(child2, signal.SIGKILL)
    time.sleep(0.4)
    check(not pc.has_detached_descendants(leader2.pid),
          "has_detached_descendants False after the bg shell exits → reap")
finally:
    try:
        os.killpg(os.getpgid(leader2.pid), signal.SIGKILL)
    except (ProcessLookupError, OSError):
        pass
    if os.path.exists(pf2):
        os.remove(pf2)

# --- Scenario 3: ignore_pgids excludes a deliberate service spawn ---
leader3, child3, pf3 = spawn_leader(detached_child=True)
try:
    svc_pgid = os.getpgid(child3)
    check(pc.has_detached_descendants(leader3.pid),
          "detached service child counts WITHOUT ignore_pgids")
    check(not pc.has_detached_descendants(
              leader3.pid, ignore_pgids=frozenset({svc_pgid})),
          "ignore_pgids excludes the recorded service group → signal False")
    swept = pc.kill_detached_descendant_groups(
        leader3.pid, ignore_pgids=frozenset({svc_pgid}),
    )
    time.sleep(0.3)
    check(swept == 0, "sweep with ignore_pgids signals zero groups")
    check(pc.pid_alive(child3),
          "ignored service survived the sweep")
finally:
    try:
        os.killpg(os.getpgid(leader3.pid), signal.SIGKILL)
    except (ProcessLookupError, OSError):
        pass
    try:
        os.kill(child3, signal.SIGKILL)
    except (ProcessLookupError, OSError):
        pass
    if os.path.exists(pf3):
        os.remove(pf3)

if failures:
    print(f"\n=== {len(failures)} FAILURE(S) ===")
    sys.exit(1)
print("\n=== ALL CHECKS PASSED ===")
