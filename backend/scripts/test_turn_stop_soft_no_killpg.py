"""Bounded-graceful turn-stop: lock the contract.

Turn-stop is SOFT — it writes a cancel sentinel that the runner's
`_cancel_watcher` polls and turns into `client.interrupt()`. The runner
drains, sweeps its OWN setsid'd `run_in_background` shells (NOT the
CLI / MCP / tools, which share its pgroup), writes complete.json, and
exits cleanly. NO backend killpg, NO bg-sweep at the orchestrator on a
turn-stop. The hard `cancel_run` killpg + sweep path is reserved for
session-DELETE only.

Critical invariants this test locks (each = a bug that bit us / would
bite us):

  T1  `cancel_turn(run_id)` writes `run_dir/cancel`
  T4  unknown run_id → False (no raise, no side effect)
  T5  containment.force_kill_all idempotent + tolerant of missing run_id
  T6  bg sweep: same-pgroup descendants (CLI/MCP/tools) SURVIVE; setsid'd
      bg shells (run_in_background) get SIGKILLed by
      `kill_detached_descendant_groups(os.getpid())`
  T7  fanout routes `cancel_turn` (NOT `cancel_run`) for turn-stop

Run: python backend/scripts/test_turn_stop_soft_no_killpg.py
"""
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend"))

import tempfile as _tf
import _test_home
_test_home.isolate("ba-test-")

from containment import containment  # noqa: E402
from provider_claude import ClaudeProvider, RunState  # noqa: E402

failures: list[str] = []


def _check(cond: bool, msg: str) -> None:
    tag = "PASS" if cond else "FAIL"
    print(f"  {tag}: {msg}")
    if not cond:
        failures.append(msg)


def _make_run_state(run_dir: Path) -> RunState:
    popen = SimpleNamespace(pid=os.getpid(), poll=lambda: None)
    return RunState(
        run_id=run_dir.name,
        run_dir=run_dir,
        popen=popen,
        mode="manager",
        app_session_id="test-sid",
        queue=None,
        started_at="",
        persist_to="test-sid",
    )


# ---------------------------------------------------------------------
# T1-T4: cancel_turn API contract
# ---------------------------------------------------------------------
def test_cancel_turn_api() -> None:
    print("T1/T4 Provider.cancel_turn contract")
    prov = ClaudeProvider.__new__(ClaudeProvider)
    prov._runs = {}

    # T1 — writes run_dir/cancel
    with tempfile.TemporaryDirectory() as td:
        run_dir = Path(td) / "run-ephemeral"
        run_dir.mkdir()
        rs = _make_run_state(run_dir)
        prov._runs[rs.run_id] = rs
        result = prov.cancel_turn(rs.run_id)
        _check(result is True, "T1 cancel_turn returns True")
        _check((run_dir / "cancel").exists(),
               "T1 cancel_turn writes run_dir/cancel")

    # T4 — unknown run_id
    prov._runs.clear()
    result = prov.cancel_turn("never-existed")
    _check(result is False, "T4 unknown run_id returns False")


# ---------------------------------------------------------------------
# T5: containment.force_kill_all idempotent + tolerant of missing run_id
# ---------------------------------------------------------------------
def test_force_kill_all_idempotent() -> None:
    print("T5 containment.force_kill_all idempotent")
    c = containment()
    # Missing run_id — must NOT raise. Returns 0 on every backend.
    n1 = c.force_kill_all("never-existed-run-id")
    n2 = c.force_kill_all("never-existed-run-id")
    _check(n1 == 0, "T5 force_kill_all on missing run_id returns 0")
    _check(n2 == 0, "T5 second call also returns 0 (idempotent)")


# ---------------------------------------------------------------------
# T6: runner bg-sweep semantics — real subprocess tree.
# Spawns leader (its own session, like the real runner) → c1 same-group
# (CLI/MCP analogue, SURVIVES) and c2 setsid'd (run_in_background bash
# analogue, DIES). Calls the same proc_control method the runner does
# inside _run_one_turn before complete.json. Verifies the survive/die
# split.
# ---------------------------------------------------------------------
_LEADER_TREE = r"""
import os, sys, json, subprocess, time
c1 = subprocess.Popen(['sh', '-c', 'sleep 600 & wait'])
c2 = subprocess.Popen(['sh', '-c', 'sleep 600'], start_new_session=True)
sys.stdout.write(json.dumps({'leader': os.getpid(), 'c1': c1.pid, 'c2': c2.pid}) + '\n')
sys.stdout.flush()
time.sleep(600)
"""


def _pid_alive(pid: int) -> bool:
    """Live = exists AND not a zombie. `os.kill(pid, 0)` returns success
    for zombies (process structure exists until wait()), so a leader
    that doesn't reap its dead children would falsely report them
    alive. `ps -o stat=` shows 'Z' for zombies; anything else is a
    running/sleeping process."""
    try:
        out = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)],
            capture_output=True, text=True, timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    stat = out.stdout.strip()
    if not stat:
        return False
    return "Z" not in stat


def test_runner_bg_sweep_semantics() -> None:
    print("T6 runner bg-sweep: same-pgroup survives, setsid'd dies")
    from proc_control import process_control
    leader = subprocess.Popen(
        [sys.executable, "-c", _LEADER_TREE],
        stdout=subprocess.PIPE, text=True, start_new_session=True,
    )
    import json as _json
    line = leader.stdout.readline()
    pids = _json.loads(line)
    cli_analogue = pids["c1"]
    bg_analogue = pids["c2"]
    leader_pid = pids["leader"]

    # Wait for children to actually start
    time.sleep(0.3)
    _check(_pid_alive(cli_analogue), "T6 setup: same-group child alive")
    _check(_pid_alive(bg_analogue), "T6 setup: setsid'd bg child alive")

    try:
        swept = process_control().kill_detached_descendant_groups(leader_pid)
        _check(swept >= 1, "T6 sweep signalled at least one detached group")
        # Give SIGKILL a moment to land.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and _pid_alive(bg_analogue):
            time.sleep(0.05)
        _check(not _pid_alive(bg_analogue),
               "T6 setsid'd bg shell is DEAD after sweep")
        _check(_pid_alive(cli_analogue),
               "T6 same-pgroup child (CLI analogue) is ALIVE after sweep "
               "(sweep skips same-pgroup descendants by design)")
    finally:
        # Hard-cleanup the whole tree (test fixture, not under test).
        try:
            os.killpg(os.getpgid(leader_pid), signal.SIGKILL)
        except OSError:
            pass
        try:
            os.kill(bg_analogue, signal.SIGKILL)
        except OSError:
            pass
        try:
            leader.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass


# ---------------------------------------------------------------------
# T7: orchestrator fanout routes through cancel_turn (NOT cancel_run)
# Constructs a real ClaudeProvider, records which methods get called,
# verifies the fanout's choice.
# ---------------------------------------------------------------------
def test_fanout_routes_through_cancel_turn() -> None:
    print("T7 _cancel_turn_fanout calls cancel_turn, NOT cancel_run")
    from orchestrator import Coordinator

    calls = {"cancel_run": 0, "cancel_turn": 0}

    class _Spy:
        id = "spy"

        def cancel_run(self, rid: str) -> bool:
            calls["cancel_run"] += 1
            return True

        def cancel_turn(self, rid: str) -> bool:
            calls["cancel_turn"] += 1
            return True

    spy = _Spy()
    # Patch known_providers to return only the spy.
    import orchestrator as _orch
    real_kp = _orch.known_providers
    _orch.known_providers = lambda: [spy]
    try:
        Coordinator._cancel_turn_fanout("any-rid")
        _check(calls["cancel_turn"] == 1,
               "T7 _cancel_turn_fanout invoked provider.cancel_turn")
        _check(calls["cancel_run"] == 0,
               "T7 _cancel_turn_fanout did NOT invoke provider.cancel_run "
               "(the killpg path is delete-only)")
    finally:
        _orch.known_providers = real_kp


def main() -> int:
    test_cancel_turn_api()
    test_force_kill_all_idempotent()
    test_runner_bg_sweep_semantics()
    test_fanout_routes_through_cancel_turn()

    print()
    if failures:
        print(f"FAILED: {len(failures)} assertion(s)")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("OK: all assertions passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
