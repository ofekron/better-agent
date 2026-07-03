"""Empirical test: claude binary process lifecycle under BC's exact usage.

BC uses `ClaudeSDKClient` which spawns claude with:
    claude --output-format stream-json --verbose --input-format stream-json \
           --permission-mode bypassPermissions --system-preset-partial claude_code \
           --tools default --enable-file-checkpointing \
           --cwd <dir> --resume <session_id> ...
No `-p` flag. The process reads JSON from stdin, writes JSON to stdout.
It stays alive as long as stdin is open (the SDK's multi-turn contract).

BC's runner is per-turn: connect → _run_one_turn → complete.json →
(babysitter linger iff detached background work) → disconnect → exit.
T2/T11/T12 also document the raw binary's multi-turn capability over one
stdin, which BC no longer uses.

T14-T18 lock the per-turn/babysitter facts: reap-signal tracking on a
real claude (T14), Monitor process-tree visibility (T15), --resume
behavior while the original instance is alive (T16), --disallowedTools
stripping the timer tools (T17), and prompt-cache behavior across the
--resume process boundary (T18).

Requires an authenticated `claude` on PATH; costs ~13 small turns.
"""

import json
import os
import signal
import subprocess
import sys
import time
import uuid

from live_llm_test_guard import require_live_llm_tests

# Exact flags the SDK uses (SubprocessCLITransport._build_command)
SDK_FLAGS = [
    "--output-format", "stream-json",
    "--verbose",
    "--input-format", "stream-json",
    "--permission-mode", "bypassPermissions",
]


def _cleanup(*markers):
    for m in markers:
        subprocess.run(["pkill", "-f", m], capture_output=True)


def _drain_until(p, predicate, timeout=60):
    """Read stream-json lines from p.stdout until predicate(ev) returns True."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = p.stdout.readline()
        if not line:
            return None
        try:
            ev = json.loads(line)
            if predicate(ev):
                return ev
        except ValueError:
            continue
    return None


def _send_control(p, subtype, **extra):
    """Send an SDK-style control request (matching _send_control_request)."""
    req_id = f"req_{uuid.uuid4().hex[:8]}"
    msg = json.dumps({
        "type": "control_request",
        "request_id": req_id,
        "request": {"subtype": subtype, **extra},
    }) + "\n"
    p.stdin.write(msg)
    p.stdin.flush()
    return req_id


def _send_user(p, content, session_id="default"):
    """Send an SDK-style user message (matching client.connect/query)."""
    msg = json.dumps({
        "type": "user",
        "message": {"role": "user", "content": content},
        "parent_tool_use_id": None,
        "session_id": session_id,
    }) + "\n"
    p.stdin.write(msg)
    p.stdin.flush()


results = []


def check(name, cond, detail=""):
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {name}{(' — ' + detail) if detail else ''}")
    results.append(bool(cond))
    return cond


def fail(msg):
    print(f"ABORT: {msg}")
    sys.exit(1)


# ---------------------------------------------------------------------------
# T1: SDK-mode single turn — process exits after disconnect (non-persistent)
# Mirrors BC's _run_one_turn: connect → query → receive_response → disconnect.
# ---------------------------------------------------------------------------
def test_sdk_single_turn_exits():
    print("\n=== T1: SDK single-turn: process exits after disconnect ===")
    p = subprocess.Popen(
        ["claude"] + SDK_FLAGS,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True,
    )
    claude_pid = p.pid

    # SDK protocol: initialize
    req_id = _send_control(p, "initialize")
    init_resp = _drain_until(
        p, lambda ev: ev.get("type") == "control_response", timeout=30
    )
    check("initialize handshake succeeds", init_resp is not None,
          f"resp={str(init_resp)[:100] if init_resp else 'None'}")
    check("claude process alive after init",
          p.poll() is None, f"poll={p.poll()}")

    if p.poll() is not None:
        fail(f"claude exited after initialize (rc={p.returncode})")

    # SDK protocol: send user message (like client.connect with prompt)
    _send_user(p, "Reply with exactly the word T1DONE.")
    result = _drain_until(p, lambda ev: ev.get("type") == "result", timeout=60)
    check("received result for turn 1", result is not None,
          f"subtype={result.get('subtype') if result else 'N/A'}")
    check("claude process alive after turn (before disconnect)",
          p.poll() is None, f"poll={p.poll()}")

    # SDK protocol: disconnect (close stdin, like SubprocessCLITransport.close)
    p.stdin.close()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and p.poll() is None:
        time.sleep(0.3)
    check("claude process exits after stdin close (disconnect)",
          p.poll() is not None, f"rc={p.returncode} pid={claude_pid}")


# ---------------------------------------------------------------------------
# T2: SDK-mode multi-turn — same PID across turns (persistent runner)
# Mirrors BC's _main_loop: connect → query → idle → query → idle → ...
# ---------------------------------------------------------------------------
def test_sdk_multi_turn_same_pid():
    print("\n=== T2: SDK multi-turn: same PID across turns ===")
    p = subprocess.Popen(
        ["claude"] + SDK_FLAGS,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True,
    )
    claude_pid = p.pid

    # Initialize
    _send_control(p, "initialize")
    init_resp = _drain_until(
        p, lambda ev: ev.get("type") == "control_response", timeout=30
    )
    check("initialize succeeds", init_resp is not None)
    if p.poll() is not None:
        fail(f"claude exited after initialize (rc={p.returncode})")

    # Turn 1
    _send_user(p, "Reply with exactly the word TURN1_OK.")
    result1 = _drain_until(p, lambda ev: ev.get("type") == "result", timeout=60)
    check("turn 1 completed", result1 is not None)
    check("same PID alive after turn 1",
          p.poll() is None, f"pid={claude_pid} poll={p.poll()}")

    # Idle gap (persistent runner polls at idle_poll_seconds=3.0)
    time.sleep(5)
    check("same PID alive after 5s idle",
          p.poll() is None, f"pid={claude_pid} poll={p.poll()}")

    # Turn 2 on same process
    _send_user(p, "Reply with exactly the word TURN2_OK.")
    result2 = _drain_until(p, lambda ev: ev.get("type") == "result", timeout=60)
    check("turn 2 completed on same process", result2 is not None)
    check("same PID alive after turn 2",
          p.poll() is None, f"pid={claude_pid} poll={p.poll()}")

    # Turn 3 — proves persistence isn't a fluke
    _send_user(p, "Reply with exactly the word TURN3_OK.")
    result3 = _drain_until(p, lambda ev: ev.get("type") == "result", timeout=60)
    check("turn 3 completed on same process", result3 is not None)
    check("same PID alive after turn 3",
          p.poll() is None, f"pid={claude_pid} poll={p.poll()}")

    # Disconnect
    p.stdin.close()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and p.poll() is None:
        time.sleep(0.3)
    check("claude exits on disconnect after 3 turns",
          p.poll() is not None, f"rc={p.returncode}")


# ---------------------------------------------------------------------------
# T3: SDK-mode + undeclared background work — process does NOT supervise orphans
# A turn spawns a nohup daemon, then the turn completes. The claude process
# does NOT keep itself alive to supervise the orphan. It exits on disconnect.
# ---------------------------------------------------------------------------
def test_sdk_orphan_daemon():
    print("\n=== T3: SDK mode does not supervise orphan daemons ===")
    marker = f"bc-orphan-{uuid.uuid4().hex[:8]}"
    daemon_log = f"/tmp/{marker}.log"

    p = subprocess.Popen(
        ["claude"] + SDK_FLAGS,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True,
    )

    # Initialize
    _send_control(p, "initialize")
    _drain_until(p, lambda ev: ev.get("type") == "control_response", timeout=30)
    if p.poll() is not None:
        fail(f"claude exited after initialize (rc={p.returncode})")

    # Turn: spawn an undeclared daemon
    _send_user(p,
        f"Run this exact foreground Bash command (NOT run_in_background): "
        f"nohup bash -c 'for i in $(seq 1 180); do echo {marker} >> {daemon_log}; "
        f"sleep 1; done' >/dev/null 2>&1 &  "
        "Reply with exactly ORPHAN_SPAWNED."
    )
    result = _drain_until(p, lambda ev: ev.get("type") == "result", timeout=60)
    check("turn completed", result is not None)

    # Check daemon is alive
    time.sleep(2)
    out = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,command="], capture_output=True, text=True
    ).stdout
    daemon_pid = None
    for ln in out.splitlines():
        if marker in ln and "grep" not in ln and "claude" not in ln:
            parts = ln.split(None, 2)
            daemon_pid = int(parts[0])
            daemon_ppid = int(parts[1])
            break

    if daemon_pid:
        check("undeclared daemon is running", True, f"pid={daemon_pid} ppid={daemon_ppid}")
        check("daemon ppid is NOT the claude process",
              daemon_ppid != p.pid,
              f"daemon_ppid={daemon_ppid} claude_pid={p.pid}")
    else:
        # Daemon may have exited — check log
        log_exists = os.path.exists(daemon_log)
        log_lines = open(daemon_log).read().strip().splitlines() if log_exists else []
        check("daemon ran (log file proof)", log_exists and len(log_lines) >= 2,
              f"lines={len(log_lines)}")

    # Now disconnect — claude should exit regardless of the orphan
    p.stdin.close()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and p.poll() is None:
        time.sleep(0.3)
    check("claude exits on disconnect despite orphan running",
          p.poll() is not None, f"rc={p.returncode}")

    _cleanup(marker)
    try:
        os.unlink(daemon_log)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# T4: SDK-mode SIGTERM — process dies, no respawn/cron/daemon behavior
# Proves claude is NOT a daemon — SIGTERM kills it, it doesn't respawn.
# ---------------------------------------------------------------------------
def test_sdk_sigterm_no_respawn():
    print("\n=== T4: SIGTERM kills claude, no respawn/cron behavior ===")
    p = subprocess.Popen(
        ["claude"] + SDK_FLAGS,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True,
    )
    claude_pid = p.pid

    # Initialize and do one turn
    _send_control(p, "initialize")
    _drain_until(p, lambda ev: ev.get("type") == "control_response", timeout=30)
    if p.poll() is not None:
        fail(f"claude exited after initialize (rc={p.returncode})")

    _send_user(p, "Reply with exactly the word SIGTEST.")
    _drain_until(p, lambda ev: ev.get("type") == "result", timeout=60)
    check("turn completed before SIGTERM", p.poll() is None)

    # Keep stdin open (persistent runner scenario) and SIGTERM the process
    os.kill(claude_pid, signal.SIGTERM)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and p.poll() is None:
        time.sleep(0.3)
    check("claude dies on SIGTERM", p.poll() is not None,
          f"rc={p.returncode}")

    # Wait and verify no respawn — only check if THIS process came back.
    # There may be other BC-managed claude processes; we only care that
    # the one we killed didn't respawn with the same PID.
    time.sleep(3)
    # Check the specific PID didn't come back
    still_alive = subprocess.run(
        ["ps", "-o", "pid=", "-p", str(claude_pid)],
        capture_output=True, text=True,
    ).stdout.strip()
    check("killed PID did not respawn", not still_alive,
          f"pid={claude_pid} still in process table")


# ---------------------------------------------------------------------------
# Helpers for background work tests
# ---------------------------------------------------------------------------
def _find_process(marker):
    """Find (pid, ppid) of a process whose command contains the marker."""
    out = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,command="], capture_output=True, text=True
    ).stdout
    for ln in out.splitlines():
        if marker in ln and "grep" not in ln and "claude" not in ln:
            parts = ln.split(None, 2)
            return int(parts[0]), int(parts[1])
    return None, None


def _wait_for_process(marker, timeout=30):
    """Poll until a process matching marker appears."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pid, ppid = _find_process(marker)
        if pid:
            return pid, ppid
        time.sleep(0.5)
    return None, None


# ---------------------------------------------------------------------------
# T5: Multi-turn + run_in_background — does claude keep bg work alive across
#     turns? Does it reap between turns?
# Turn 1: launch a declared bg shell (run_in_background=true).
# Turn 2: check if the bg shell is still alive.
# Disconnect: check if the bg shell gets reaped.
# ---------------------------------------------------------------------------
def test_multiturn_declared_bg_work():
    print("\n=== T5: multi-turn + run_in_background: bg work across turns ===")
    marker = f"bc-bg-mt-{uuid.uuid4().hex[:8]}"

    p = subprocess.Popen(
        ["claude"] + SDK_FLAGS,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True,
    )
    claude_pid = p.pid

    _send_control(p, "initialize")
    _drain_until(p, lambda ev: ev.get("type") == "control_response", timeout=30)
    if p.poll() is not None:
        fail(f"claude exited after initialize (rc={p.returncode})")

    # Turn 1: launch declared bg work
    _send_user(p,
        f"Using Bash with run_in_background=true, run exactly: "
        f"bash -c 'for i in $(seq 1 180); do echo {marker} >> /tmp/{marker}.log; "
        f"sleep 1; done'  "
        "Reply with exactly BG_LAUNCHED."
    )
    result1 = _drain_until(p, lambda ev: ev.get("type") == "result", timeout=60)
    check("turn 1 completed (bg work launched)", result1 is not None)

    # Check bg shell is alive after turn 1 — and its PPID traces back to claude
    bg_pid, bg_ppid = _wait_for_process(marker, timeout=15)
    check("declared bg shell is running after turn 1",
          bg_pid is not None, f"bg_pid={bg_pid}")
    if bg_pid:
        # The bg shell's parent chain should trace back to claude
        # (claude → zsh → bash). Its immediate ppid may be an intermediate
        # shell, not claude directly, so we walk the tree.
        _walk = bg_ppid
        _found = False
        for _ in range(5):
            if _walk == claude_pid:
                _found = True
                break
            _out = subprocess.run(
                ["ps", "-o", "ppid=", "-p", str(_walk)],
                capture_output=True, text=True,
            ).stdout.strip()
            if not _out.isdigit():
                break
            _walk = int(_out)
        check("bg shell's parent chain includes claude",
              _found, f"bg_pid={bg_pid} bg_ppid={bg_ppid} claude_pid={claude_pid}")

    if p.poll() is not None:
        _cleanup(marker)
        fail(f"claude died after turn 1 (rc={p.returncode})")

    # Turn 2: do something unrelated while bg work is running
    _send_user(p, "Reply with exactly TURN2_PROBE.")
    result2 = _drain_until(p, lambda ev: ev.get("type") == "result", timeout=60)
    check("turn 2 completed (bg work still running)", result2 is not None)

    # Check bg shell status after turn 2
    bg_pid2, _ = _find_process(marker)
    if bg_pid:
        check("declared bg shell survived across turns",
              bg_pid2 is not None, f"still running as pid={bg_pid2}")
        check("bg shell same PID across turns",
              bg_pid2 == bg_pid, f"turn1_pid={bg_pid} turn2_pid={bg_pid2}")
    else:
        check("declared bg shell survived across turns", False,
              "bg shell was not found after turn 1")

    # Disconnect — claude should exit. Does it reap the bg shell?
    p.stdin.close()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and p.poll() is None:
        time.sleep(0.3)
    check("claude exits on disconnect", p.poll() is not None,
          f"rc={p.returncode}")

    # Check bg shell after claude exit
    time.sleep(3)
    bg_pid3, _ = _find_process(marker)
    check("declared bg shell reaped on claude exit (disconnect)",
          bg_pid3 is None, f"bg_pid after exit={bg_pid3}")

    _cleanup(marker)
    try:
        os.unlink(f"/tmp/{marker}.log")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# T6: Multi-turn + undeclared daemon — does it survive across turns?
# Turn 1: spawn an undeclared daemon via nohup.
# Turn 2: check if daemon is still alive.
# Disconnect: daemon should be orphaned (ppid→1).
# ---------------------------------------------------------------------------
def test_multiturn_undeclared_daemon():
    print("\n=== T6: multi-turn + undeclared daemon: daemon leaks across turns ===")
    marker = f"bc-daemon-mt-{uuid.uuid4().hex[:8]}"
    daemon_log = f"/tmp/{marker}.log"

    p = subprocess.Popen(
        ["claude"] + SDK_FLAGS,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True,
    )

    _send_control(p, "initialize")
    _drain_until(p, lambda ev: ev.get("type") == "control_response", timeout=30)
    if p.poll() is not None:
        fail(f"claude exited after initialize (rc={p.returncode})")

    # Turn 1: spawn undeclared daemon
    _send_user(p,
        f"Run this foreground Bash command (NOT run_in_background): "
        f"nohup bash -c 'for i in $(seq 1 180); do echo {marker} >> {daemon_log}; "
        f"sleep 1; done' >/dev/null 2>&1 &  "
        "Reply with exactly DAEMON_MT_SPAWNED."
    )
    result1 = _drain_until(p, lambda ev: ev.get("type") == "result", timeout=60)
    check("turn 1 completed (daemon launched)", result1 is not None)

    d_pid1, d_ppid1 = _wait_for_process(marker, timeout=15)
    check("undeclared daemon running after turn 1",
          d_pid1 is not None, f"pid={d_pid1} ppid={d_ppid1}")

    # Turn 2: do something unrelated
    _send_user(p, "Reply with exactly TURN2_PROBE.")
    result2 = _drain_until(p, lambda ev: ev.get("type") == "result", timeout=60)
    check("turn 2 completed", result2 is not None)

    # Check daemon still alive after turn 2
    d_pid2, d_ppid2 = _find_process(marker)
    if d_pid1:
        check("undeclared daemon survived across turns",
              d_pid2 is not None, f"pid={d_pid2}")
    else:
        check("undeclared daemon survived across turns", False,
              "daemon not found after turn 1")

    # Disconnect
    p.stdin.close()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and p.poll() is None:
        time.sleep(0.3)
    check("claude exits on disconnect", p.poll() is not None,
          f"rc={p.returncode}")

    # Check daemon after claude exit — should be orphaned (ppid→1)
    time.sleep(2)
    d_pid3, d_ppid3 = _find_process(marker)
    if d_pid3:
        check("undeclared daemon still running after claude exit (orphaned)",
              True, f"pid={d_pid3} ppid={d_ppid3}")
        check("orphan daemon ppid=1 (reparented to launchd)",
              d_ppid3 == 1, f"ppid={d_ppid3}")
    else:
        # Daemon exited — check log for proof
        log_exists = os.path.exists(daemon_log)
        log_lines = open(daemon_log).read().strip().splitlines() if log_exists else []
        check("undeclared daemon ran past claude exit (log proof)",
              log_exists and len(log_lines) >= 4,
              f"lines={len(log_lines)}")

    _cleanup(marker)
    try:
        os.unlink(daemon_log)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# T7: SIGTERM with active declared bg work — does the bg shell die?
# ---------------------------------------------------------------------------
def test_sigterm_with_declared_bg():
    print("\n=== T7: SIGTERM with declared bg work running ===")
    marker = f"bc-bg-sig-{uuid.uuid4().hex[:8]}"

    p = subprocess.Popen(
        ["claude"] + SDK_FLAGS,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True,
    )
    claude_pid = p.pid

    _send_control(p, "initialize")
    _drain_until(p, lambda ev: ev.get("type") == "control_response", timeout=30)
    if p.poll() is not None:
        fail(f"claude exited after initialize (rc={p.returncode})")

    # Launch declared bg work
    _send_user(p,
        f"Using Bash with run_in_background=true, run exactly: "
        f"bash -c 'for i in $(seq 1 180); do echo {marker} >> /tmp/{marker}.log; "
        f"sleep 1; done'  "
        "Reply with exactly BG_SIG_LAUNCHED."
    )
    _drain_until(p, lambda ev: ev.get("type") == "result", timeout=60)
    check("turn completed (bg launched)", p.poll() is None)

    bg_pid, _ = _wait_for_process(marker, timeout=15)
    check("bg shell running", bg_pid is not None, f"bg_pid={bg_pid}")

    # SIGTERM claude while bg work is active
    os.kill(claude_pid, signal.SIGTERM)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and p.poll() is None:
        time.sleep(0.3)
    check("claude dies on SIGTERM", p.poll() is not None,
          f"rc={p.returncode}")

    # Check bg shell — does SIGTERM to claude also kill it?
    time.sleep(3)
    bg_pid_after, _ = _find_process(marker)
    if bg_pid:
        check("declared bg shell reaped when claude SIGTERM'd",
              bg_pid_after is None, f"bg_pid_after={bg_pid_after}")
    else:
        check("declared bg shell reaped when claude SIGTERM'd", True,
              "bg shell wasn't found initially")

    _cleanup(marker)
    try:
        os.unlink(f"/tmp/{marker}.log")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# T8: SIGTERM with active undeclared daemon — daemon survives
# ---------------------------------------------------------------------------
def test_sigterm_with_undeclared_daemon():
    print("\n=== T8: SIGTERM with undeclared daemon running ===")
    marker = f"bc-daemon-sig-{uuid.uuid4().hex[:8]}"

    p = subprocess.Popen(
        ["claude"] + SDK_FLAGS,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True,
    )
    claude_pid = p.pid

    _send_control(p, "initialize")
    _drain_until(p, lambda ev: ev.get("type") == "control_response", timeout=30)
    if p.poll() is not None:
        fail(f"claude exited after initialize (rc={p.returncode})")

    # Spawn undeclared daemon
    _send_user(p,
        f"Run this foreground Bash command (NOT run_in_background): "
        f"nohup bash -c 'for i in $(seq 1 180); do echo {marker} >> /tmp/{marker}.log; "
        f"sleep 1; done' >/dev/null 2>&1 &  "
        "Reply with exactly DAEMON_SIG_SPAWNED."
    )
    _drain_until(p, lambda ev: ev.get("type") == "result", timeout=60)
    check("turn completed (daemon launched)", p.poll() is None)

    d_pid, _ = _wait_for_process(marker, timeout=15)
    check("undeclared daemon running", d_pid is not None, f"pid={d_pid}")

    # SIGTERM claude
    os.kill(claude_pid, signal.SIGTERM)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and p.poll() is None:
        time.sleep(0.3)
    check("claude dies on SIGTERM", p.poll() is not None,
          f"rc={p.returncode}")

    # Check daemon — should survive
    time.sleep(2)
    d_pid_after, d_ppid_after = _find_process(marker)
    if d_pid:
        check("undeclared daemon survives claude SIGTERM (orphaned)",
              d_pid_after is not None, f"pid={d_pid_after} ppid={d_ppid_after}")
        if d_pid_after:
            check("orphan daemon ppid=1",
                  d_ppid_after == 1, f"ppid={d_ppid_after}")
    else:
        check("undeclared daemon survives claude SIGTERM", True,
              "daemon not found initially, can't verify")

    _cleanup(marker)
    try:
        os.unlink(f"/tmp/{marker}.log")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# T9: SIGKILL (hard crash) — claude dies instantly, no cleanup.
# Declared bg shells get orphaned (no graceful reap). Undeclared daemons
# also survive. This is the hard-crash scenario BC's recovery handles.
# ---------------------------------------------------------------------------
def test_sigkill_hard_crash():
    print("\n=== T9: SIGKILL hard crash — no graceful cleanup ===")
    marker_bg = f"bc-bg-kill9-{uuid.uuid4().hex[:8]}"
    marker_daemon = f"bc-daemon-kill9-{uuid.uuid4().hex[:8]}"

    p = subprocess.Popen(
        ["claude"] + SDK_FLAGS,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True,
    )
    claude_pid = p.pid

    _send_control(p, "initialize")
    _drain_until(p, lambda ev: ev.get("type") == "control_response", timeout=30)
    if p.poll() is not None:
        fail(f"claude exited after initialize (rc={p.returncode})")

    # Launch both declared bg work and undeclared daemon in one turn
    _send_user(p,
        f"Do two things in this order: "
        f"(1) Using Bash with run_in_background=true, run: "
        f"bash -c 'for i in $(seq 1 180); do echo {marker_bg} >> /tmp/{marker_bg}.log; sleep 1; done'  "
        f"(2) As a foreground Bash command (NOT run_in_background), run: "
        f"nohup bash -c 'for i in $(seq 1 180); do echo {marker_daemon} >> /tmp/{marker_daemon}.log; "
        f"sleep 1; done' >/dev/null 2>&1 &  "
        "Reply with exactly CRASH_SETUP_DONE."
    )
    _drain_until(p, lambda ev: ev.get("type") == "result", timeout=60)
    check("turn completed (both bg + daemon launched)", p.poll() is None)

    # Wait for both to appear
    bg_pid, _ = _wait_for_process(marker_bg, timeout=15)
    daemon_pid, _ = _wait_for_process(marker_daemon, timeout=15)
    check("declared bg shell running before SIGKILL",
          bg_pid is not None, f"bg_pid={bg_pid}")
    check("undeclared daemon running before SIGKILL",
          daemon_pid is not None, f"daemon_pid={daemon_pid}")

    # SIGKILL — no graceful shutdown, no reap
    os.kill(claude_pid, signal.SIGKILL)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and p.poll() is None:
        time.sleep(0.3)
    check("claude dies on SIGKILL", p.poll() is not None,
          f"rc={p.returncode}")

    # After SIGKILL: declared bg shell is NOT reaped (no graceful cleanup)
    time.sleep(3)
    bg_pid_after, bg_ppid_after = _find_process(marker_bg)
    daemon_pid_after, daemon_ppid_after = _find_process(marker_daemon)

    if bg_pid:
        check("declared bg shell survives SIGKILL (orphaned, no graceful reap)",
              bg_pid_after is not None, f"bg_pid={bg_pid_after} ppid={bg_ppid_after}")
        if bg_pid_after:
            # After SIGKILL, claude is dead but the bg shell may have an
            # intermediate parent (zsh wrapper) that's also orphaned. The
            # key fact is: the bg shell is NOT reaped (unlike SIGTERM).
            check("orphaned bg shell ppid is NOT claude (reparented or intermediate)",
                  bg_ppid_after != claude_pid,
                  f"ppid={bg_ppid_after} claude_pid={claude_pid}")
    else:
        check("declared bg shell survives SIGKILL", True, "bg not found initially")

    if daemon_pid:
        check("undeclared daemon survives SIGKILL",
              daemon_pid_after is not None, f"daemon_pid={daemon_pid_after} ppid={daemon_ppid_after}")
    else:
        check("undeclared daemon survives SIGKILL", True, "daemon not found initially")

    _cleanup(marker_bg, marker_daemon)
    for m in [marker_bg, marker_daemon]:
        try:
            os.unlink(f"/tmp/{m}.log")
        except OSError:
            pass


# ---------------------------------------------------------------------------
# T10: Resume creates a NEW process (different PID).
# BC uses --resume to reconnect to an existing session. This proves that
# resume spawns a brand-new claude process — the old PID is dead and a new
# one takes over. Critical for TestApe: PID binding must track the CURRENT
# process, not a stale one.
# ---------------------------------------------------------------------------
def test_resume_is_new_process():
    print("\n=== T10: resume creates a NEW process (different PID) ===")
    # Phase 1: start a session, do one turn, capture the session_id
    p1 = subprocess.Popen(
        ["claude"] + SDK_FLAGS,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True,
    )
    p1_pid = p1.pid

    _send_control(p1, "initialize")
    _drain_until(p1, lambda ev: ev.get("type") == "control_response", timeout=30)
    if p1.poll() is not None:
        fail(f"p1 exited after initialize (rc={p1.returncode})")

    _send_user(p1, "Reply with exactly RESUME_PHASE1.")
    result1 = _drain_until(p1, lambda ev: ev.get("type") == "result", timeout=60)
    check("phase 1 turn completed", result1 is not None)

    # Extract session_id from the stream (SystemMessage with subtype="init")
    # The session_id is in the result message
    session_id = result1.get("session_id") if result1 else None
    check("captured session_id from result",
          session_id is not None, f"session_id={session_id}")

    # Disconnect p1
    p1.stdin.close()
    p1.wait(timeout=15)
    check("p1 exited after disconnect",
          p1.returncode is not None, f"rc={p1.returncode}")

    if not session_id:
        fail("no session_id — cannot test resume")

    # Phase 2: resume the session with a NEW process
    p2 = subprocess.Popen(
        ["claude"] + SDK_FLAGS + ["--resume", session_id],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True,
    )
    p2_pid = p2.pid

    _send_control(p2, "initialize")
    _drain_until(p2, lambda ev: ev.get("type") == "control_response", timeout=30)
    if p2.poll() is not None:
        fail(f"p2 exited after initialize (rc={p2.returncode})")

    _send_user(p2, "Reply with exactly RESUME_PHASE2.")
    result2 = _drain_until(p2, lambda ev: ev.get("type") == "result", timeout=60)
    check("phase 2 turn completed on resumed session",
          result2 is not None)
    check("p2 PID is DIFFERENT from p1 PID",
          p2_pid != p1_pid, f"p1={p1_pid} p2={p2_pid}")

    # Verify p1 is truly dead
    p1_alive = subprocess.run(
        ["ps", "-o", "pid=", "-p", str(p1_pid)],
        capture_output=True, text=True,
    ).stdout.strip()
    check("p1 is dead (not in process table)",
          not p1_alive, f"p1_pid={p1_pid} still alive")

    p2.stdin.close()
    p2.wait(timeout=15)


# ---------------------------------------------------------------------------
# T11: CronCreate fires a new turn on the SAME process while stdin is open.
# The cron is in-memory — it fires on the live process, not a new one.
# The fired turn produces a result just like a user-initiated turn.
# On disconnect, the cron is lost.
# ---------------------------------------------------------------------------
def test_cron_fires_on_live_process():
    print("\n=== T11: CronCreate fires new turn on same process ===")
    p = subprocess.Popen(
        ["claude"] + SDK_FLAGS,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True,
    )
    claude_pid = p.pid

    _send_control(p, "initialize")
    _drain_until(p, lambda ev: ev.get("type") == "control_response", timeout=30)
    if p.poll() is not None:
        fail(f"claude exited after initialize (rc={p.returncode})")

    # Create a recurring cron that fires every minute
    _send_user(p,
        "Use CronCreate to schedule a recurring job: every 1 minute, "
        "prompt: 'Reply with exactly CRON_TICK'. "
        "Then reply: CRON_SET"
    )
    result1 = _drain_until(p, lambda ev: ev.get("type") == "result", timeout=60)
    check("turn 1 completed (cron scheduled)", result1 is not None)

    # Process alive with cron
    check("claude alive with scheduled cron",
          p.poll() is None, f"poll={p.poll()} pid={claude_pid}")

    # Wait for the cron to fire (up to 70s — cron fires every 60s)
    print("  waiting for cron to fire (up to 70s)...", flush=True)
    cron_result = _drain_until(
        p, lambda ev: ev.get("type") == "result", timeout=70
    )
    check("cron fired and produced a result on same process",
          cron_result is not None,
          f"result={str(cron_result)[:100] if cron_result else 'None'}")

    # Same PID — cron doesn't spawn a new process
    check("same PID after cron fired",
          p.poll() is None, f"poll={p.poll()} pid={claude_pid}")

    # Disconnect — cron is lost
    p.stdin.close()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and p.poll() is None:
        time.sleep(0.3)
    check("claude exits on disconnect (cron is in-memory, dies with process)",
          p.poll() is not None, f"rc={p.returncode}")


# ---------------------------------------------------------------------------
# T13: ScheduleWakeup — in-memory timer for /loop mode. Like CronCreate,
# it does NOT keep the process alive past stdin close. Verified empirically:
# process exits in ~1s after stdin close despite pending wakeup.
# Also: Agent and Workflow tools are IN-PROCESS (no new OS processes).
# Verified empirically: no new claude PIDs appear during Agent/Workflow use.
# ---------------------------------------------------------------------------
def test_schedule_wakeup_no_lifecycle_impact():
    print("\n=== T13: ScheduleWakeup does not keep process alive ===")
    p = subprocess.Popen(
        ["claude"] + SDK_FLAGS,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True,
    )
    claude_pid = p.pid

    _send_control(p, "initialize")
    _drain_until(p, lambda ev: ev.get("type") == "control_response", timeout=30)
    if p.poll() is not None:
        fail(f"claude exited after initialize (rc={p.returncode})")

    _send_user(p,
        "Use ScheduleWakeup to schedule a wake-up in 120 seconds from now, "
        "with prompt: 'Reply with exactly WAKEUP_OK'. Then reply: WAKEUP_SET"
    )
    result = _drain_until(p, lambda ev: ev.get("type") == "result", timeout=60)
    check("turn completed (wakeup scheduled)", result is not None)
    check("claude alive with pending wakeup",
          p.poll() is None, f"poll={p.poll()}")

    # Close stdin — process should exit immediately (unlike Monitor)
    t0 = time.monotonic()
    p.stdin.close()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and p.poll() is None:
        time.sleep(0.3)
    elapsed = time.monotonic() - t0
    check("claude exits on disconnect despite pending wakeup",
          p.poll() is not None, f"rc={p.returncode} elapsed={elapsed:.1f}s")
    check("exit is fast (<5s), not waiting for wakeup",
          elapsed < 5.0, f"elapsed={elapsed:.1f}s")


# ---------------------------------------------------------------------------
# T12: Monitor keeps process alive past stdin EOF, dies with process.
# Unlike run_in_background, Monitor streams events and keeps the process
# alive even after stdin is closed. The process stays alive until the
# monitor completes or is SIGKILL'd.
# ---------------------------------------------------------------------------
def test_monitor_keeps_process_alive():
    print("\n=== T12: Monitor keeps process alive, dies on disconnect ===")
    monitor_marker = f"bc-monitor-{uuid.uuid4().hex[:8]}"

    p = subprocess.Popen(
        ["claude"] + SDK_FLAGS,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True,
    )
    claude_pid = p.pid

    _send_control(p, "initialize")
    _drain_until(p, lambda ev: ev.get("type") == "control_response", timeout=30)
    if p.poll() is not None:
        fail(f"claude exited after initialize (rc={p.returncode})")

    # Start a monitor on a slow-running script
    script = f"/tmp/{monitor_marker}.sh"
    with open(script, "w") as f:
        f.write(f"for i in $(seq 1 60); do echo {monitor_marker}_tick; sleep 1; done\n")
    os.chmod(script, 0o755)

    _send_user(p,
        f"Use the Monitor tool to watch this command: {script}  "
        f"Set timeout_ms=60000. Reply: MONITOR_STARTED"
    )

    # Wait for the monitor to start (we'll see the first result)
    result = _drain_until(p, lambda ev: ev.get("type") == "result", timeout=60)
    check("turn completed (monitor started)", result is not None)

    # Process should be alive with monitor running
    check("claude alive with active monitor",
          p.poll() is None, f"poll={p.poll()}")

    # Wait a few seconds — the monitor script should still be running
    time.sleep(5)
    check("claude still alive 5s after monitor start",
          p.poll() is None, f"poll={p.poll()}")

    # Check the monitor script is actually running
    script_pid, _ = _find_process(monitor_marker)
    check("monitor script is running",
          script_pid is not None, f"pid={script_pid}")

    # Disconnect mid-monitor. Monitor keeps the process alive past stdin EOF.
    # Unlike all other operations (CronCreate, ScheduleWakeup, run_in_background),
    # Monitor keeps the claude process alive even after stdin is closed.
    # Verify: process must still be alive 10s after stdin close (no other
    # operation survives this long after stdin close — T1 exits in <1s).
    p.stdin.close()
    time.sleep(10)
    alive_after_10s = p.poll() is None
    check("monitor keeps claude alive 10s past stdin close",
          alive_after_10s,
          f"poll={p.poll()} (process {'alive' if alive_after_10s else 'dead'} after 10s)")

    # Force kill to clean up
    try:
        os.kill(claude_pid, signal.SIGKILL)
    except (ProcessLookupError, OSError):
        pass
    p.wait(timeout=5)

    # Check monitor script after claude exit
    time.sleep(2)
    script_pid_after, script_ppid_after = _find_process(monitor_marker)
    if script_pid:
        check("monitor script status after claude exit",
              True, f"pid={script_pid_after} ppid={script_ppid_after} "
                    f"({'still running' if script_pid_after else 'dead'})")
    else:
        check("monitor script was running before disconnect", True,
              "verified above")

    _cleanup(monitor_marker)
    try:
        os.unlink(script)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# T14: reap-signal — proc_control.has_detached_descendants on a REAL claude.
# The hybrid per-turn design exits the runner at turn end iff this signal is
# False. It must be: False before any bg work, True while a run_in_background
# shell lives (from both the claude-pid and runner-pid positions), and drop
# back to False once the bg work ends (the reap loop's exit condition).
# ---------------------------------------------------------------------------
def test_reap_signal_bg_shell():
    print("\n=== T14: has_detached_descendants tracks run_in_background ===")
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from proc_control import _PosixProcessControl
    pc = _PosixProcessControl()
    marker = f"bc-reap-{uuid.uuid4().hex[:8]}"

    p = subprocess.Popen(
        ["claude"] + SDK_FLAGS,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True,
    )
    claude_pid = p.pid

    _send_control(p, "initialize")
    _drain_until(p, lambda ev: ev.get("type") == "control_response", timeout=30)
    if p.poll() is not None:
        fail(f"claude exited after initialize (rc={p.returncode})")

    check("signal False before any bg work (leader=claude)",
          pc.has_detached_descendants(claude_pid) is False)

    _send_user(p,
        f"Using Bash with run_in_background=true, run exactly: "
        f"bash -c 'for i in $(seq 1 180); do echo {marker} >> /tmp/{marker}.log; "
        f"sleep 1; done'  "
        "Reply with exactly BG_LAUNCHED."
    )
    result = _drain_until(p, lambda ev: ev.get("type") == "result", timeout=60)
    check("turn completed (bg work launched)", result is not None)

    bg_pid, _ = _wait_for_process(marker, timeout=15)
    check("bg shell is running", bg_pid is not None, f"bg_pid={bg_pid}")

    if bg_pid:
        claude_pgid = os.getpgid(claude_pid)
        bg_pgid = os.getpgid(bg_pid)
        check("bg shell is setsid'd into its OWN group",
              bg_pgid != claude_pgid,
              f"bg_pgid={bg_pgid} claude_pgid={claude_pgid}")
        check("signal True while bg shell alive (leader=claude)",
              pc.has_detached_descendants(claude_pid) is True)
        check("signal True from the runner position (leader=this process)",
              pc.has_detached_descendants(os.getpid()) is True)

        # End the bg work; the signal must drop — reap loop exit condition.
        try:
            os.kill(bg_pid, signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass
        dropped = False
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if pc.has_detached_descendants(claude_pid) is False:
                dropped = True
                break
            time.sleep(0.5)
        check("signal drops to False after bg work ends", dropped)

    p.stdin.close()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and p.poll() is None:
        time.sleep(0.3)
    check("claude exits on disconnect", p.poll() is not None,
          f"rc={p.returncode}")

    _cleanup(marker)
    try:
        os.unlink(f"/tmp/{marker}.log")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# T15: Monitor visibility in the process tree. proc_control's docstring
# claims Monitor watchers count as detached descendants — this locks the
# empirical answer, because the hybrid reap signal must either see an active
# Monitor in the tree or fall back to tool-use events for it.
# ---------------------------------------------------------------------------
def test_monitor_tree_visibility():
    print("\n=== T15: Monitor watcher visibility in the process tree ===")
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from proc_control import _PosixProcessControl
    pc = _PosixProcessControl()
    marker = f"bc-reapmon-{uuid.uuid4().hex[:8]}"

    script = f"/tmp/{marker}.sh"
    with open(script, "w") as f:
        f.write(f"for i in $(seq 1 60); do echo {marker}_tick; sleep 1; done\n")
    os.chmod(script, 0o755)

    p = subprocess.Popen(
        ["claude"] + SDK_FLAGS,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True,
    )
    claude_pid = p.pid

    _send_control(p, "initialize")
    _drain_until(p, lambda ev: ev.get("type") == "control_response", timeout=30)
    if p.poll() is not None:
        fail(f"claude exited after initialize (rc={p.returncode})")

    _send_user(p,
        f"Use the Monitor tool to watch this command: {script}  "
        f"Set timeout_ms=60000. Reply: MONITOR_STARTED"
    )
    result = _drain_until(p, lambda ev: ev.get("type") == "result", timeout=60)
    check("turn completed (monitor started)", result is not None)

    mon_pid, _ = _wait_for_process(marker, timeout=15)
    check("monitor watcher process is running",
          mon_pid is not None, f"pid={mon_pid}")

    if mon_pid:
        claude_pgid = os.getpgid(claude_pid)
        mon_pgid = os.getpgid(mon_pid)
        detached = mon_pgid != claude_pgid
        signal_seen = pc.has_detached_descendants(claude_pid)
        check("monitor watcher is in its OWN group (detached)",
              detached, f"mon_pgid={mon_pgid} claude_pgid={claude_pgid}")
        check("has_detached_descendants sees the active monitor",
              signal_seen is True, f"signal={signal_seen}")

    try:
        os.kill(claude_pid, signal.SIGKILL)
    except (ProcessLookupError, OSError):
        pass
    p.wait(timeout=5)

    _cleanup(marker)
    try:
        os.unlink(script)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# T16: --resume while the ORIGINAL instance is still alive (the babysitter
# scenario: instance A lingers to keep bg work alive while a fresh --resume
# instance B runs the next turn). Locked empirical facts: --resume CONTINUES
# the same session_id and APPENDS to the same jsonl file — the two live
# instances are CONCURRENT WRITERS to one file. Each instance's in-memory
# history excludes the other's turns, so the shared file accumulates an
# interleaved/branched history (lines stay valid JSON; parentUuid chains
# diverge). Consequence: a lingering babysitter instance must never run a
# turn of its own (cron/wakeup firing) while a fresh --resume instance is
# active, or the histories interleave in one file.
# ---------------------------------------------------------------------------
def test_resume_while_original_alive():
    print("\n=== T16: --resume while the original instance is still alive ===")
    import glob as _glob

    def _session_files(sid):
        return sorted(_glob.glob(
            os.path.expanduser(f"~/.claude/projects/*/{sid}.jsonl")))

    def _wait_session_file(sid, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            files = _session_files(sid)
            if files:
                return files
            time.sleep(0.5)
        return []

    def _line_count(path):
        with open(path) as f:
            return sum(1 for _ in f)

    p1 = subprocess.Popen(
        ["claude"] + SDK_FLAGS,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True,
    )
    _send_control(p1, "initialize")
    _drain_until(p1, lambda ev: ev.get("type") == "control_response", timeout=30)
    if p1.poll() is not None:
        fail(f"p1 exited after initialize (rc={p1.returncode})")

    _send_user(p1, "Reply with exactly FORK_P1.")
    r1 = _drain_until(p1, lambda ev: ev.get("type") == "result", timeout=60)
    check("p1 turn 1 completed", r1 is not None)
    sid1 = r1.get("session_id") if r1 else None
    check("captured p1 session_id", sid1 is not None, f"sid1={sid1}")
    if not sid1:
        p1.stdin.close()
        fail("no session_id — cannot test resume-while-alive")

    files1 = _wait_session_file(sid1)
    check("p1 session jsonl exists on disk",
          bool(files1), f"files={files1}")
    lines1_before = _line_count(files1[0]) if files1 else 0

    # p1 stays ALIVE (stdin open) — the babysitter scenario.
    p2 = subprocess.Popen(
        ["claude"] + SDK_FLAGS + ["--resume", sid1],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True,
    )
    _send_control(p2, "initialize")
    _drain_until(p2, lambda ev: ev.get("type") == "control_response", timeout=30)
    if p2.poll() is not None:
        fail(f"p2 exited after initialize (rc={p2.returncode})")

    _send_user(p2, "Reply with exactly FORK_P2.")
    r2 = _drain_until(p2, lambda ev: ev.get("type") == "result", timeout=60)
    check("p2 turn completed via --resume while p1 alive", r2 is not None)
    sid2 = r2.get("session_id") if r2 else None
    check("captured p2 session_id", sid2 is not None, f"sid2={sid2}")

    if sid2:
        check("--resume CONTINUES the same session_id (no fork)",
              sid2 == sid1, f"sid1={sid1} sid2={sid2}")

    lines1_after_p2 = _line_count(files1[0]) if files1 else 0
    check("p2's turn APPENDS to p1's jsonl (shared-file hazard is real)",
          lines1_after_p2 > lines1_before,
          f"before={lines1_before} after={lines1_after_p2}")

    # p1 runs another turn while p2 is still alive — both instances live.
    _send_user(p1, "Reply with exactly FORK_P1B.")
    r1b = _drain_until(p1, lambda ev: ev.get("type") == "result", timeout=60)
    check("p1 turn 2 completed while p2 alive", r1b is not None)
    check("p1 keeps its own session_id",
          bool(r1b) and r1b.get("session_id") == sid1,
          f"sid={r1b.get('session_id') if r1b else None}")

    # Integrity: every line of both files parses as JSON after the
    # interleaved turns.
    all_valid = True
    for path in dict.fromkeys(
            files1[:1] + (_session_files(sid2) if sid2 else [])):
        try:
            with open(path) as f:
                for ln in f:
                    if ln.strip():
                        json.loads(ln)
        except ValueError:
            all_valid = False
    check("both session files are line-valid JSON after interleaved turns",
          all_valid)

    for proc in (p1, p2):
        try:
            proc.stdin.close()
            proc.wait(timeout=15)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# T17: --disallowedTools strips the in-process timer tools. The BC-owned
# scheduler design replaces CronCreate/ScheduleWakeup with backend MCP tools;
# this locks that the native ones can actually be removed — claude never
# creates an in-process timer on a disallowed instance, so a lingering
# (babysitter) instance can never start a turn of its own.
# ---------------------------------------------------------------------------
def test_disallowed_timer_tools():
    print("\n=== T17: --disallowedTools removes CronCreate/ScheduleWakeup ===")
    p = subprocess.Popen(
        ["claude"] + SDK_FLAGS + [
            "--disallowedTools",
            "CronCreate,CronDelete,CronList,ScheduleWakeup",
        ],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True,
    )
    _send_control(p, "initialize")
    _drain_until(p, lambda ev: ev.get("type") == "control_response", timeout=30)
    if p.poll() is not None:
        fail(f"claude exited after initialize (rc={p.returncode})")

    tool_uses = []

    def _collect_until_result(ev):
        if ev.get("type") == "assistant":
            for block in (ev.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tool_uses.append(block.get("name"))
        return ev.get("type") == "result"

    _send_user(p,
        "Use the CronCreate tool to schedule a recurring job: every 1 minute, "
        "prompt: 'Reply with exactly CRON_TICK'. If the CronCreate tool is "
        "not available to you, do not use any other tool and reply with "
        "exactly: NO_CRON_TOOL"
    )
    r1 = _drain_until(p, _collect_until_result, timeout=60)
    check("turn 1 completed", r1 is not None)
    check("model reports CronCreate unavailable",
          bool(r1) and "NO_CRON_TOOL" in str(r1.get("result", "")),
          f"result={str(r1.get('result'))[:80] if r1 else None}")

    _send_user(p,
        "Use the ScheduleWakeup tool to schedule a wake-up in 60 seconds, "
        "prompt: 'Reply with exactly WAKEUP_OK'. If the ScheduleWakeup tool "
        "is not available to you, do not use any other tool and reply with "
        "exactly: NO_WAKEUP_TOOL"
    )
    r2 = _drain_until(p, _collect_until_result, timeout=60)
    check("turn 2 completed", r2 is not None)
    check("model reports ScheduleWakeup unavailable",
          bool(r2) and "NO_WAKEUP_TOOL" in str(r2.get("result", "")),
          f"result={str(r2.get('result'))[:80] if r2 else None}")

    timer_calls = [n for n in tool_uses
                   if n in ("CronCreate", "CronDelete", "CronList",
                            "ScheduleWakeup")]
    check("no timer tool_use emitted across both turns",
          not timer_calls, f"tool_uses={tool_uses}")

    p.stdin.close()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and p.poll() is None:
        time.sleep(0.3)
    check("claude exits on disconnect", p.poll() is not None,
          f"rc={p.returncode}")


# ---------------------------------------------------------------------------
# T18: prompt-cache behavior across the --resume process boundary — the
# measured cost of per-turn spawn. Deterministic invariant (asserted):
# within ONE process, turn 2 reads the conversation prefix from cache.
# Cross-boundary behavior is INTERMITTENT (measured 2026-06-11, three
# runs): full hit (read ≈ prior read+creation, creation ≈ 0), partial
# hit (read ≈ static system prefix only), and zero hit have all been
# observed — prefix identity across spawns sometimes survives and
# sometimes diverges at per-spawn dynamic content. So the cross-boundary
# delta is MEASURED AND PRINTED, not asserted; worst case is one full
# prefix re-creation per turn. Keep CLAUDE_PROCESS_LIFECYCLE.md's cost
# section in sync with what this prints.
# ---------------------------------------------------------------------------
def test_resume_cache_cost():
    print("\n=== T18: prompt cache across the --resume process boundary ===")

    def _turn(p, prompt):
        _send_user(p, prompt)
        r = _drain_until(p, lambda ev: ev.get("type") == "result", timeout=120)
        u = (r or {}).get("usage") or {}
        return r, u.get("cache_read_input_tokens"), u.get("cache_creation_input_tokens")

    p1 = subprocess.Popen(
        ["claude"] + SDK_FLAGS,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True,
    )
    _send_control(p1, "initialize")
    _drain_until(p1, lambda ev: ev.get("type") == "control_response", timeout=30)
    if p1.poll() is not None:
        fail(f"p1 exited after initialize (rc={p1.returncode})")

    r1, read1, create1 = _turn(p1, "Reply with exactly CACHE_T1.")
    check("turn 1 completed", r1 is not None)
    sid = (r1 or {}).get("session_id")
    check("captured session_id", sid is not None, f"sid={sid}")

    r2, read2, create2 = _turn(p1, "Reply with exactly CACHE_T2.")
    check("turn 2 (same process) completed", r2 is not None)
    check("in-process turn 2 READS the prefix from cache",
          isinstance(read2, int) and read2 > 0,
          f"cache_read={read2} cache_creation={create2}")

    p1.stdin.close()
    p1.wait(timeout=15)
    if not sid:
        fail("no session_id — cannot test resumed cache")

    p2 = subprocess.Popen(
        ["claude"] + SDK_FLAGS + ["--resume", sid],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True,
    )
    _send_control(p2, "initialize")
    _drain_until(p2, lambda ev: ev.get("type") == "control_response", timeout=30)
    if p2.poll() is not None:
        fail(f"p2 exited after initialize (rc={p2.returncode})")

    r3, read3, create3 = _turn(p2, "Reply with exactly CACHE_T3.")
    check("turn 3 (--resume, new process) completed", r3 is not None)
    check("resumed process reports cache usage",
          isinstance(read3, int) and isinstance(create3, int),
          f"resumed read={read3} create={create3} "
          f"(in-process read={read2} create={create2})")
    hit = (
        "full" if isinstance(read3, int) and isinstance(read2, int) and read3 >= read2
        else "partial" if isinstance(read3, int) and read3 > 0
        else "none"
    )
    check(f"cross-boundary cache hit measured: {hit}", True,
          f"read={read3} create={create3} — intermittent by design, "
          "see T18 header")

    p2.stdin.close()
    p2.wait(timeout=15)


def main():
    if os.name == "nt":
        print("POSIX-only")
        return 0
    if not require_live_llm_tests("real Claude binary process lifecycle tests"):
        return 0

    print("=== claude binary process lifecycle tests (BC SDK usage) ===")
    print(f"  claude version: ", end="", flush=True)
    subprocess.run(["claude", "--version"])

    try:
        test_sdk_single_turn_exits()
        test_sdk_multi_turn_same_pid()
        test_sdk_orphan_daemon()
        test_sdk_sigterm_no_respawn()
        test_multiturn_declared_bg_work()
        test_multiturn_undeclared_daemon()
        test_sigterm_with_declared_bg()
        test_sigterm_with_undeclared_daemon()
        test_sigkill_hard_crash()
        test_resume_is_new_process()
        test_cron_fires_on_live_process()
        test_monitor_keeps_process_alive()
        test_schedule_wakeup_no_lifecycle_impact()
        test_reap_signal_bg_shell()
        test_monitor_tree_visibility()
        test_resume_while_original_alive()
        test_disallowed_timer_tools()
        test_resume_cache_cost()
    finally:
        for prefix in ["bc-orphan", "bc-bg-mt", "bc-daemon-mt", "bc-bg-sig",
                       "bc-daemon-sig", "bc-bg-kill9", "bc-daemon-kill9",
                       "bc-monitor", "bc-reap", "bc-reapmon"]:
            _cleanup(prefix)

    ok = sum(results)
    total = len(results)
    print(f"\n{'PASS' if ok == total else 'FAIL'}: {ok}/{total} lifecycle claims validated")
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
