"""E2E babysitter linger for BACKGROUND SUBAGENTS — real runner.py + claude CLI.

Regression lock for the bug: a subagent launched with the Task tool's
`run_in_background` runs IN-PROCESS inside the claude CLI, so it has NO
detached OS process group. The babysitter's reap signal
(`has_detached_descendants`) therefore read False and the runner reaped the
CLI the instant the main turn's complete.json landed — SIGKILLing the still
-running background subagent ("subagents killed while waiting in the
background").

Contract locked here:
  S1  a turn that launches a run_in_background subagent writes complete.json
      AND the runner STAYS alive (lingering) while the subagent runs — the
      subagent's marker does NOT exist yet at complete time (proves it's
      genuinely backgrounded, not a foreground Task).
  S2  the background subagent RUNS TO COMPLETION (writes its marker) instead
      of being killed, and only THEN does the lingering runner self-reap.

Pre-fix: the runner reaps at complete.json, the subagent is SIGKILLed, the
marker never appears → S2 fails. Post-fix: the runner tracks the SDK
TaskStarted/TaskNotification lifecycle and lingers until the subagent ends.

Requires an authenticated `claude` on PATH; costs ~1 small turn + ~30s wall.
Run: cd backend && .venv/bin/python scripts/test_babysitter_subagent_linger.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-subagent-linger-")

RUNNER = Path(_BACKEND) / "runner.py"
PY = Path(_BACKEND) / ".venv" / "bin" / "python"
if not PY.exists():
    PY = Path(sys.executable)

failures: list[str] = []


def check(cond, msg):
    print(("  PASS" if cond else "  FAIL") + f": {msg}")
    if not cond:
        failures.append(msg)


def _bg_agent_prompt(marker: str) -> str:
    done = f"/tmp/{marker}.done"
    return (
        "Use the Task tool to launch a subagent with run_in_background set to "
        "true (a BACKGROUND subagent — do not wait for it). Give the subagent "
        "this exact instruction and nothing else: 'Run exactly this one Bash "
        f"command in the foreground, then stop: bash -c \"sleep 30 && echo DONE "
        f"> {done}\"'. As soon as you have launched the background subagent, "
        "reply with exactly BG_AGENT_LAUNCHED and stop. Do NOT wait for the "
        "subagent to finish."
    )


def _spawn(run_id: str, prompt: str) -> tuple[subprocess.Popen, Path]:
    run_dir = Path(_TMP_HOME) / "runs" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "input.json").write_text(json.dumps({
        "prompt": prompt,
        "images": [],
        "cwd": _TMP_HOME,
        "mode": "native",
        "app_session_id": f"subagent-linger-{run_id}",
        "model": None,
        "session_id": None,
        "disallowed_tools": [
            "AskUserQuestion", "EnterPlanMode", "ExitPlanMode",
            "CronCreate", "CronDelete", "CronList", "ScheduleWakeup",
        ],
        "setting_sources": ["user"],
        "backend_url": "http://localhost:8000",
        "internal_token": "subagent-linger-token",
        "fork": False,
        "supervised": False,
        "browser_harness_enabled": False,
        "open_file_panel_enabled": False,
    }))
    stdout = (run_dir / "stdout.log").open("ab")
    stderr = (run_dir / "stderr.log").open("ab")
    proc = subprocess.Popen(
        [str(PY), str(RUNNER), "--run-dir", str(run_dir)],
        stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
        env=os.environ.copy(), start_new_session=True,
    )
    stdout.close(); stderr.close()
    return proc, run_dir


def _wait_complete(run_dir: Path, timeout: float = 180) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if (run_dir / "complete.json").exists():
            return True
        time.sleep(0.5)
    return False


def _wait_exit(proc: subprocess.Popen, timeout: float = 25) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return True
        time.sleep(0.3)
    return False


def _wait_file(path: Path, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.5)
    return False


def _cleanup(marker: str) -> None:
    subprocess.run(["pkill", "-f", marker], capture_output=True)
    try:
        os.unlink(f"/tmp/{marker}.done")
    except OSError:
        pass


def main() -> int:
    marker = f"bcsub-{uuid.uuid4().hex[:8]}"
    done = Path(f"/tmp/{marker}.done")
    p, d = _spawn("run1", _bg_agent_prompt(marker))
    try:
        # ---------------- S1: main turn completes, subagent still running ----
        print("S1 turn launches bg subagent → complete.json while it runs")
        if not _wait_complete(d):
            check(False, "S1 main turn never wrote complete.json")
            return 1
        c = json.loads((d / "complete.json").read_text())
        check(c.get("success"), f"S1 main turn succeeded ({c.get('error')})")
        # The subagent sleeps 30s; at complete time it must NOT be done yet,
        # else the Task ran foreground and the bug window never opened.
        check(not done.exists(),
              "S1 subagent still running at complete time (genuinely background)")
        time.sleep(3)
        check(p.poll() is None,
              "S1 runner ALIVE after complete.json (lingering for subagent)")
        check((d / "runner_alive").exists(),
              "S1 heartbeat present during linger")
        check((d / "lingering").exists(),
              "S1 lingering sentinel published")

        # ---------------- S2: subagent completes, THEN runner reaps ----------
        print("S2 bg subagent runs to completion (not killed) → runner reaps")
        check(_wait_file(done, timeout=90),
              "S2 background subagent wrote its marker (NOT killed mid-run)")
        check(p.poll() is None or done.exists(),
              "S2 subagent finished before/at runner reap")
        check(_wait_exit(p, timeout=25),
              "S2 runner self-reaped after subagent ended")
        check(not (d / "runner_alive").exists(),
              "S2 runner_alive removed on exit")
    finally:
        try:
            p.kill()
        except Exception:
            pass
        _cleanup(marker)

    print()
    if failures:
        print(f"FAILED: {len(failures)}")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("OK: babysitter background-subagent linger e2e")
    return 0


if __name__ == "__main__":
    sys.exit(main())
