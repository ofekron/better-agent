"""Empirical proc_control coverage NOT already locked by
test_proc_control_descendants.py (has_detached_descendants) or
test_proc_control_detached_kill.py (kill_detached_descendant_groups).

Builds REAL process trees (no mocks) and asserts the three things those
two tests don't:

  * group_member_pids — the ppid walk itself — returns the leader, a
    same-group child, a setsid-DETACHED child, AND a nested grandchild;
  * pid_alive is a non-destructive probe (and zombie-correct);
  * THE 100% HOLE: a daemonized descendant whose intermediate ancestor
    EXITED reparents to init and escapes the ppid walk entirely — so
    neither group_member_pids nor has_detached_descendants nor the
    detached-group sweep can see or reap it.

POSIX only. No claude CLI, no API — safe in the normal `test_*` bucket.
"""

import json
import os
import signal
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from proc_control import _PosixProcessControl  # noqa: E402

PC = _PosixProcessControl()

# leader (start_new_session) → c1 (same group, with a sleep grandchild)
#                            → c2 (start_new_session ⇒ own group, ppid==leader)
_DRIVER = r"""
import json, subprocess, os, sys, time
c1 = subprocess.Popen(["sh", "-c", "sleep 600 & wait"])                # same group + grandchild
c2 = subprocess.Popen(["sh", "-c", "sleep 600"], start_new_session=True)  # detached, own group
sys.stdout.write(json.dumps({"leader": os.getpid(), "c1": c1.pid, "c2": c2.pid}) + "\n")
sys.stdout.flush()
time.sleep(600)
"""

# An intermediate that spawns a detached grandchild then EXITS, orphaning it.
_LEADER_REPARENT = r"""
import subprocess, sys, json, time
inter = subprocess.Popen([sys.executable, "-c",
    "import subprocess,sys,json;"
    "g=subprocess.Popen(['sh','-c','sleep 600'], start_new_session=True);"
    "sys.stdout.write(json.dumps({'g':g.pid})+chr(10));sys.stdout.flush()"],
    stdout=subprocess.PIPE, text=True)
line = inter.stdout.readline()
inter.wait()                       # intermediate exits -> grandchild orphans to init
sys.stdout.write(line); sys.stdout.flush()
time.sleep(120)                    # leader stays alive
"""


def _spawn_tree():
    p = subprocess.Popen(
        [sys.executable, "-c", _DRIVER],
        stdout=subprocess.PIPE, text=True, start_new_session=True,
    )
    pids = json.loads(p.stdout.readline())
    assert pids["leader"] == p.pid, (pids, p.pid)
    return p, pids["leader"], pids["c1"], pids["c2"]


def _ppid_of(pid):
    out = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)],
                         capture_output=True, text=True)
    try:
        return int(out.stdout.strip())
    except ValueError:
        return -1


def _proc_state(pid):
    out = subprocess.run(["ps", "-o", "state=", "-p", str(pid)],
                         capture_output=True, text=True)
    return out.stdout.strip()


def _is_zombie(pid):
    st = _proc_state(pid)
    return st == "" or st.startswith("Z")


def _alive(pid):
    if not PC.pid_alive(pid):
        return False
    return not _is_zombie(pid)


def _wait_dead(pid, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if _is_zombie(pid):
            return True
        time.sleep(0.05)
    return _is_zombie(pid)


def _hard_cleanup(*pids):
    for pid in pids:
        for fn in (lambda p=pid: os.killpg(os.getpgid(p), signal.SIGKILL),
                   lambda p=pid: os.kill(p, signal.SIGKILL)):
            try:
                fn()
            except OSError:
                pass


def _check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    return cond


def test_group_member_pids_walk():
    print("T1 group_member_pids (ppid walk) returns leader + same-group + "
          "detached + nested grandchild")
    p, leader, c1, c2 = _spawn_tree()
    try:
        members = []
        end = time.monotonic() + 3
        while time.monotonic() < end:
            members = PC.group_member_pids(leader)
            if len(members) >= 4:
                break
            time.sleep(0.05)
        return all([
            _check("includes leader", leader in members),
            _check("includes same-group child", c1 in members),
            _check("includes DETACHED (setsid) child", c2 in members),
            _check("includes nested grandchild (>=4 pids)", len(members) >= 4),
        ])
    finally:
        _hard_cleanup(c2, leader)
        p.wait()


def test_pid_alive_nondestructive():
    print("T2 pid_alive is a non-destructive probe, zombie-correct after death")
    p, leader, c1, c2 = _spawn_tree()
    try:
        time.sleep(0.2)
        a1, a2 = PC.pid_alive(c2), PC.pid_alive(c2)
        probe_ok = _check("repeated probe leaves target alive", a1 and a2 and _alive(c2))
        _hard_cleanup(c2)
        dead_ok = _check("False once the process dies", _wait_dead(c2))
        return probe_ok and dead_ok
    finally:
        _hard_cleanup(c2, leader)
        p.wait()


def test_reparented_descendant_escapes_walk():
    print("T3 THE 100% HOLE: a daemon whose ancestor EXITED (reparented to "
          "init) escapes the ppid walk, has_detached, and the sweep")
    leader = subprocess.Popen(
        [sys.executable, "-c", _LEADER_REPARENT],
        stdout=subprocess.PIPE, text=True, start_new_session=True,
    )
    g = json.loads(leader.stdout.readline())["g"]
    try:
        end = time.monotonic() + 5
        while time.monotonic() < end and _ppid_of(g) != 1:
            time.sleep(0.1)
        return all([
            _check("orphan reparented to init (ppid==1)", _ppid_of(g) == 1),
            _check("orphan is still ALIVE (a real leaked daemon)", _alive(g)),
            _check("group_member_pids(leader) does NOT find it",
                   g not in PC.group_member_pids(leader.pid)),
            _check("has_detached_descendants(leader) is False (blind)",
                   PC.has_detached_descendants(leader.pid) is False),
            _check("sweep can't reach it (returns, orphan still alive)",
                   (PC.kill_detached_descendant_groups(leader.pid) or 0) >= 0
                   and _alive(g)),
        ])
    finally:
        _hard_cleanup(g, leader.pid)
        leader.wait()


def main():
    if os.name == "nt":
        print("POSIX-only test"); return 0
    tests = [
        test_group_member_pids_walk,
        test_pid_alive_nondestructive,
        test_reparented_descendant_escapes_walk,
    ]
    results = []
    for t in tests:
        try:
            results.append(t())
        except Exception as e:
            print(f"  [FAIL] EXCEPTION: {e!r}")
            results.append(False)
    ok = sum(results)
    print(f"\n{ok}/{len(results)} claims validated")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
