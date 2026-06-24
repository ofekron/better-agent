"""Containment contract test (Phase 2).

Proves the containment abstraction against a REAL process tree:
  * enumerate() returns the runner + same-group child + setsid'd bg child;
  * has_background_work() detects the setsid'd (own-group) descendant;
  * THE FIX — a reparented-to-init orphan (the T3 case the ppid walk loses)
    is STILL enumerated IFF the backend is `guaranteed` (Linux cgroup /
    Windows job). On macOS (`guaranteed is False`) it is honestly MISSED —
    this test LOCKS that documented best-effort gap so callers know to
    surface "degraded", and flips to the strict assertion automatically on
    a guaranteed platform.

Runs the real backend for THIS host (Darwin best-effort here; Linux/Windows
assertions activate when run there).
"""

import json
import os
import signal
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from containment import containment  # noqa: E402

C = containment()
RUN_ID = "containment-contract-test"

# leader (own session) → c1 same-group child ; → c2 setsid'd bg child
_TREE = r"""
import json, subprocess, os, sys, time
c1 = subprocess.Popen(["sh", "-c", "sleep 600 & wait"])                # same group
c2 = subprocess.Popen(["sh", "-c", "sleep 600"], start_new_session=True)  # detached
sys.stdout.write(json.dumps({"leader": os.getpid(), "c1": c1.pid, "c2": c2.pid}) + "\n")
sys.stdout.flush()
time.sleep(600)
"""

# leader → intermediate that spawns a detached orphan then EXITS (orphan
# reparents to init: the ppid walk loses it, a real container does not)
_ORPHAN = r"""
import subprocess, sys, json, time
inter = subprocess.Popen([sys.executable, "-c",
    "import subprocess,sys,json;"
    "g=subprocess.Popen(['sh','-c','sleep 600'], start_new_session=True);"
    "sys.stdout.write(json.dumps({'g':g.pid})+chr(10));sys.stdout.flush()"],
    stdout=subprocess.PIPE, text=True)
line = inter.stdout.readline()
inter.wait()
sys.stdout.write(line); sys.stdout.flush()
time.sleep(600)
"""

failures = []


def _check(cond, msg):
    print(("  PASS" if cond else "  FAIL") + f": {msg}")
    if not cond:
        failures.append(msg)


def _ppid(pid):
    out = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)],
                         capture_output=True, text=True)
    try:
        return int(out.stdout.strip())
    except ValueError:
        return -1


def _hard_cleanup(*pids):
    for pid in pids:
        for fn in (lambda p=pid: os.killpg(os.getpgid(p), signal.SIGKILL),
                   lambda p=pid: os.kill(p, signal.SIGKILL)):
            try:
                fn()
            except OSError:
                pass


def test_enumerate_and_bg_work():
    print(f"T1 enumerate + has_background_work ({type(C).__name__}, "
          f"guaranteed={C.guaranteed})")
    C.create(RUN_ID)
    p = subprocess.Popen([sys.executable, "-c", _TREE],
                         stdout=subprocess.PIPE, text=True, start_new_session=True)
    pids = json.loads(p.stdout.readline())
    leader, c1, c2 = pids["leader"], pids["c1"], pids["c2"]
    C.after_spawn(RUN_ID, leader)
    try:
        members = []
        end = time.monotonic() + 3
        while time.monotonic() < end:
            members = C.enumerate(RUN_ID)
            if leader in members and c1 in members and c2 in members:
                break
            time.sleep(0.1)
        _check(leader in members, "enumerate includes the runner")
        _check(c1 in members, "enumerate includes same-group child")
        _check(c2 in members, "enumerate includes setsid'd bg child")
        _check(C.has_background_work(RUN_ID, leader) is True,
               "has_background_work True with a detached bg shell alive")
    finally:
        _hard_cleanup(c2, leader)
        p.wait()
        C.teardown(RUN_ID)


def test_reparented_orphan():
    print("T2 reparented orphan: enumerated IFF guaranteed (else honest miss)")
    C.create(RUN_ID)
    leader = subprocess.Popen([sys.executable, "-c", _ORPHAN],
                              stdout=subprocess.PIPE, text=True, start_new_session=True)
    g = json.loads(leader.stdout.readline())["g"]
    C.after_spawn(RUN_ID, leader.pid)
    try:
        end = time.monotonic() + 5
        while time.monotonic() < end and _ppid(g) != 1:
            time.sleep(0.1)
        _check(_ppid(g) == 1, "orphan reparented to init (ppid==1)")
        found = g in C.enumerate(RUN_ID)
        if C.guaranteed:
            _check(found, "GUARANTEED backend STILL enumerates the orphan (the fix)")
        else:
            _check(not found and C.guaranteed is False,
                   "best-effort backend misses the orphan AND reports guaranteed=False "
                   "(documented gap — callers surface 'degraded')")
    finally:
        _hard_cleanup(g, leader.pid)
        leader.wait()
        C.teardown(RUN_ID)


def main():
    if os.name == "nt":
        print("POSIX-only harness (Windows job path needs a Windows host)")
        return 0
    test_enumerate_and_bg_work()
    test_reparented_orphan()
    print(f"\n{'PASS' if not failures else 'FAIL'}: {len(failures)} failed checks")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
