"""Is the claude OUTPUT STREAM a reliable source of "what background work runs"?

Empirical answer: NO. The stream only declares background work claude itself
launched through the run_in_background tool parameter (it returns a tracked
shell id). Background work spawned as a SIDE EFFECT of a foreground command
(`&`, nohup, setsid, a script that daemonizes) is never declared as a job —
and it LEAKS past the turn, while the declared shell is reaped on turn end.

One real claude turn launches both:
  A — a writer via Bash run_in_background=true   (declared; gets a shell id)
  B — a daemon via a foreground `nohup ... &`     (NOT declared)

We then assert from the captured stream-json + the live process table:
  * A is declared with run_in_background=True AND a background shell id;
  * B is NOT declared as a background job (no shell id; it appears only as the
    text of a foreground command);
  * after the turn: A is reaped by the CLI, B is STILL RUNNING (the leak).

Requires an authenticated `claude` on PATH; costs one small turn.
"""

import json
import os
import subprocess
import sys
import time

from live_llm_test_guard import require_live_llm_tests

A = "bcA7710"
B = "bcB7710"
PROMPT = (
    "I'm reproducing a background-process tracking issue; I need two short-lived "
    "local writers (temp logs only, no network, each self-terminates after ~3 min). "
    "Step 1 — using the Bash tool with run_in_background=true, run exactly: "
    f"bash -c 'for i in $(seq 1 180); do echo hb >> /tmp/{A}.log; sleep 1; done'  "
    "Step 2 — as a NORMAL foreground Bash command (run_in_background=false), launch "
    "a detached helper that returns immediately: "
    f"nohup bash -c 'for i in $(seq 1 180); do echo hb >> /tmp/{B}.log; sleep 1; done' "
    ">/dev/null 2>&1 &  Confirm both writers started, then stop."
)


def _pid_for(marker):
    out = subprocess.run(["ps", "-axo", "pid=,command="],
                         capture_output=True, text=True).stdout
    for ln in out.splitlines():
        if marker in ln and "sleep" in ln and "claude" not in ln:
            return int(ln.split(None, 1)[0])
    return None


def main():
    if os.name == "nt":
        print("POSIX-only"); return 0
    if not require_live_llm_tests("real Claude stream background visibility integration"):
        return 0
    p = subprocess.Popen(
        ["claude", "-p", PROMPT, "--dangerously-skip-permissions",
         "--output-format", "stream-json", "--verbose"],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True,
    )
    events = []
    for line in p.stdout:
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    p.wait()

    # ---- what the stream declared as background jobs ----
    bg_declared_cmds = []   # commands claude marked run_in_background=true
    shell_ids = []          # background shell ids the CLI handed back
    for ev in events:
        if ev.get("type") == "assistant":
            for c in ev["message"]["content"]:
                if c.get("type") == "tool_use" and c.get("name") == "Bash":
                    if c["input"].get("run_in_background") is True:
                        bg_declared_cmds.append(c["input"].get("command", ""))
        if ev.get("type") == "user":
            content = ev["message"].get("content")
            for c in (content if isinstance(content, list) else []):
                if c.get("type") == "tool_result":
                    txt = json.dumps(c.get("content"))
                    if "running in background with ID" in txt:
                        shell_ids.append(txt[:80])

    a_declared = any(A in cmd for cmd in bg_declared_cmds)
    b_declared = any(B in cmd for cmd in bg_declared_cmds)

    results = []

    def check(name, cond, detail=""):
        mark = "PASS" if cond else "FAIL"
        print(f"  [{mark}] {name}{(' — ' + detail) if detail else ''}")
        results.append(bool(cond))
        return cond

    print("=== stream-declared background jobs ===")
    for cmd in bg_declared_cmds:
        print(f"  run_in_background=True: {cmd[:80]!r}")
    print(f"  background shell ids handed back: {len(shell_ids)} -> {shell_ids}")

    if not bg_declared_cmds:
        check("claude launched A via run_in_background (precondition)", False,
              "model refused / did not use run_in_background")
        return 1

    check("A IS declared as a background job (run_in_background=True)", a_declared)
    check("the CLI handed back a background shell id for the declared job",
          len(shell_ids) >= 1)
    check("B is NOT declared as a background job (foreground-spawned daemon)",
          not b_declared)

    print("\n=== ground truth AFTER the turn ended (rc=%s) ===" % p.returncode)
    time.sleep(2)
    a_pid, b_pid = _pid_for(A), _pid_for(B)
    print(f"  A({A}) running={a_pid is not None}  B({B}) running={b_pid is not None}")
    check("declared job A is reaped by the CLI on turn end", a_pid is None)
    check("undeclared daemon B LEAKS — still running, never in the stream",
          b_pid is not None)

    for m in (A, B):
        subprocess.run(["pkill", "-f", m],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    ok = sum(results)
    print(f"\n{ok}/{len(results)} stream-reliability claims validated")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
