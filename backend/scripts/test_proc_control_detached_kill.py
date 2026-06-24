"""Unit test for ProcessControl.kill_detached_descendant_groups — the
explicit-stop sweep that kills `run_in_background` bash shells the CLI
spawned with setsid (their own session/process-group), which a plain
killpg on the runner's group can NOT reach.

Verifies (POSIX):
1. A detached descendant (own session, ppid still the leader) is alive.
2. killpg on the leader's OWN group does NOT kill it (proves the need).
3. kill_detached_descendant_groups(leader) DOES kill it.
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


def really_dead(pid: int) -> bool:
    """Gone or a zombie (killed, awaiting reap) — both mean 'not running'.
    `pid_alive` (os.kill(pid,0)) returns True for a zombie whose parent
    hasn't reaped it, so we check the process state instead."""
    r = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                       capture_output=True, text=True)
    st = r.stdout.strip()
    return st == "" or st.startswith("Z")


CHILD_PID_FILE = "/tmp/pc_detach_child.pid"
if os.path.exists(CHILD_PID_FILE):
    os.remove(CHILD_PID_FILE)

# Leader (mimics the runner) spawns a DETACHED child (mimics a
# run_in_background bash shell: start_new_session => own session/pgroup,
# but ppid stays the leader).
leader = subprocess.Popen(
    [sys.executable, "-c",
     "import subprocess,time;"
     "c=subprocess.Popen(['sleep','40'], start_new_session=True);"
     f"open({CHILD_PID_FILE!r},'w').write(str(c.pid));"
     "time.sleep(40)"],
    start_new_session=True,
)
try:
    time.sleep(0.8)
    child = int(open(CHILD_PID_FILE).read().strip())

    check(pc.pid_alive(child), "detached child is alive")
    check(os.getpgid(child) != os.getpgid(leader.pid),
          "detached child is in its OWN process group")

    # killpg on the leader's own group must NOT reach the detached child.
    os.killpg(os.getpgid(leader.pid), signal.SIGKILL)
    time.sleep(0.5)
    check(pc.pid_alive(child),
          "detached child SURVIVES killpg(leader group) — the gap")

    # Re-spawn the scenario (leader was just killed) to test the sweep
    # with the ppid chain intact.
finally:
    try:
        os.killpg(os.getpgid(leader.pid), signal.SIGKILL)
    except (ProcessLookupError, OSError):
        pass

# Clean up the surviving orphan from step 2.
try:
    if 'child' in dir():
        os.kill(child, signal.SIGKILL)
except (ProcessLookupError, OSError, NameError):
    pass

# --- Now test the sweep itself (leader alive during the sweep) ---
if os.path.exists(CHILD_PID_FILE):
    os.remove(CHILD_PID_FILE)
leader2 = subprocess.Popen(
    [sys.executable, "-c",
     "import subprocess,time;"
     "c=subprocess.Popen(['sleep','40'], start_new_session=True);"
     f"open({CHILD_PID_FILE!r},'w').write(str(c.pid));"
     "time.sleep(40)"],
    start_new_session=True,
)
try:
    time.sleep(0.8)
    child2 = int(open(CHILD_PID_FILE).read().strip())
    check(pc.pid_alive(child2), "second detached child alive before sweep")
    n = pc.kill_detached_descendant_groups(leader2.pid)
    check(n >= 1, f"sweep signalled >=1 detached group (got {n})")
    time.sleep(0.5)
    check(really_dead(child2),
          "detached child KILLED by kill_detached_descendant_groups")
finally:
    try:
        os.killpg(os.getpgid(leader2.pid), signal.SIGKILL)
    except (ProcessLookupError, OSError):
        pass
    if os.path.exists(CHILD_PID_FILE):
        os.remove(CHILD_PID_FILE)

if failures:
    print(f"\n=== {len(failures)} FAILURE(S) ===")
    sys.exit(1)
print("\n=== ALL CHECKS PASSED ===")
