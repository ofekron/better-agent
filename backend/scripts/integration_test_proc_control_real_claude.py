"""Validate proc_control + background-shell lifecycle against the REAL claude CLI.

This process plays the runner: it becomes a session/group leader (os.setsid)
and spawns the real `claude` CLI as a direct child WITHOUT a new session
(exactly how the SDK's anyio.open_process spawns it), so the CLI inherits
our group. We make claude launch a run_in_background server and keep itself
alive with a foreground stability poll, then prove on the LIVE tree:

  1. the run_in_background server is in its OWN process group (CLI setsid'd it),
     so a plain killpg on the leader's group would MISS it;
  2. proc_control.group_member_pids (ppid walk) FINDS it while claude is alive;
  3. proc_control.has_detached_descendants == True while claude is alive;
  4. when claude's TURN ENDS, the CLI itself reaps the run_in_background shell
     (its lifetime is bounded by the claude process, not orphaned).

No killpg of our own group (that would suicide the test). Requires an
authenticated `claude` on PATH and costs one small turn.
"""

import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from live_llm_test_guard import require_live_llm_tests  # noqa: E402
from proc_control import _PosixProcessControl  # noqa: E402

PC = _PosixProcessControl()
PORT = "48955"
PROMPT = (
    "Throwaway localhost-only static server for a stability check, empty temp "
    f"dir. (1) run: mkdir -p /tmp/bcsrv_{PORT}  (2) launch it in the background "
    "— Bash tool, run_in_background=true: "
    f"python3 -m http.server {PORT} --bind 127.0.0.1 --directory /tmp/bcsrv_{PORT}  "
    "(3) confirm it stays up by polling 10 times in the foreground: "
    f"for i in $(seq 1 10); do curl -s -o /dev/null http://127.0.0.1:{PORT} && "
    "echo ok$i; sleep 1.5; done  Then report stable and stop. "
    "localhost-only empty dir, so it is safe."
)


def _find_server():
    out = subprocess.run(["ps", "-axo", "pid=,pgid=,command="],
                         capture_output=True, text=True).stdout
    for line in out.splitlines():
        if PORT in line and "http.server" in line and "claude" not in line:
            parts = line.split(None, 2)
            return int(parts[0]), int(parts[1])
    return None


def _gone(pid):
    st = subprocess.run(["ps", "-o", "state=", "-p", str(pid)],
                        capture_output=True, text=True).stdout.strip()
    return st == "" or st.startswith("Z")


def main():
    if os.name == "nt":
        print("POSIX-only"); return 0
    if not require_live_llm_tests("real Claude proc_control lifecycle integration"):
        return 0
    try:
        os.setsid()
    except OSError:
        pass
    leader_pid = os.getpid()
    leader_pgid = os.getpgid(0)
    print(f"leader(runner) pid={leader_pid} pgid={leader_pgid}")

    c = subprocess.Popen(
        ["claude", "-p", PROMPT, "--dangerously-skip-permissions"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )  # inherits our group, like the SDK

    results = []

    def check(name, cond, detail=""):
        mark = "PASS" if cond else "FAIL"
        print(f"  [{mark}] {name}{(' — ' + detail) if detail else ''}")
        results.append(bool(cond))
        return cond

    try:
        srv = None
        end = time.monotonic() + 90
        while time.monotonic() < end:
            srv = _find_server()
            if srv or c.poll() is not None:
                break
            time.sleep(0.3)

        print("\n=== REAL claude run_in_background — live tree ===")
        if not srv:
            check("claude launched a visible run_in_background server", False,
                  f"marker never appeared (model refused?) rc={c.poll()}")
            return 1
        s_pid, s_pgid = srv
        print(f"bg server pid={s_pid} pgid={s_pgid}; claude_running={c.poll() is None}")

        check("server is in its OWN process group (CLI setsid'd it)",
              s_pgid != leader_pgid, f"server pgid={s_pgid} != leader {leader_pgid}")
        check("group_member_pids (ppid walk) FINDS the detached server",
              s_pid in PC.group_member_pids(leader_pid))
        check("has_detached_descendants == True (while claude alive)",
              PC.has_detached_descendants(leader_pid) is True)

        # let claude finish its turn naturally (c is our direct child).
        while c.poll() is None:
            time.sleep(0.3)
        time.sleep(2.0)
        check("CLI reaps its run_in_background shell when the turn ENDS",
              _gone(s_pid), f"server gone after turn end = {_gone(s_pid)}")

        ok = sum(results)
        print(f"\n{ok}/{len(results)} real-claude claims validated")
        return 0 if ok == len(results) else 1
    finally:
        try:
            c.kill()
        except Exception:
            pass
        subprocess.run(["pkill", "-f", f"http.server {PORT}"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    sys.exit(main())
