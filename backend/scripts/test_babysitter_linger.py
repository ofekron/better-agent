"""E2E babysitter linger — real runner.py + real claude CLI.

Locks the per-turn + babysitter contract end to end:
  T1  a turn that starts a run_in_background shell writes complete.json
      and the runner STAYS alive (lingering) with the bg shell running
  T2  a second runner (--resume, same agent session) runs a turn while
      the first lingers; the lingering instance writes NOTHING to the
      shared session jsonl while idle (single-writer invariant, lifecycle
      T16/T17)
  T3  bg shell dies → lingering runner self-reaps (runner_alive gone)
  T4  cancel sentinel during linger → bg work swept + runner exits
      (the user's kill lever)
  T5  hard kill (session-delete path, Provider.cancel_run) ends a
      lingering runner AND its background work

Requires an authenticated `claude` on PATH; costs ~4 small turns.
Run: cd backend && .venv/bin/python scripts/test_babysitter_linger.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-linger-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

RUNNER = Path(_BACKEND) / "runner.py"
PY = Path(_BACKEND) / ".venv" / "bin" / "python"
if not PY.exists():
    PY = Path(sys.executable)

failures: list[str] = []


def check(cond, msg):
    print(("  PASS" if cond else "  FAIL") + f": {msg}")
    if not cond:
        failures.append(msg)


def _bg_prompt(marker: str) -> str:
    return (
        f"Using Bash with run_in_background=true, run exactly: "
        f"bash -c 'for i in $(seq 1 300); do echo {marker} >> /tmp/{marker}.log; "
        f"sleep 1; done'  "
        "Reply with exactly BG_LAUNCHED."
    )


def _spawn(run_id: str, prompt: str, session_id: str | None = None) -> tuple[subprocess.Popen, Path]:
    run_dir = Path(_TMP_HOME) / "runs" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "input.json").write_text(json.dumps({
        "prompt": prompt,
        "images": [],
        "cwd": _TMP_HOME,
        "mode": "native",
        "app_session_id": f"linger-test-{run_id}",
        "model": None,
        "session_id": session_id,
        "disallowed_tools": [
            "AskUserQuestion", "EnterPlanMode", "ExitPlanMode",
            "CronCreate", "CronDelete", "CronList", "ScheduleWakeup",
        ],
        "setting_sources": ["user"],
        "backend_url": "http://localhost:8000",
        "internal_token": "linger-test-token",
        "fork": False,
        "supervised": False,
        "browser_test_enabled": False,
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


def _wait_exit(proc: subprocess.Popen, timeout: float = 20) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return True
        time.sleep(0.3)
    return False


def _find_marker_pid(marker: str) -> int | None:
    out = subprocess.run(["ps", "-axo", "pid=,command="],
                         capture_output=True, text=True).stdout
    for ln in out.splitlines():
        if marker in ln and "grep" not in ln and "claude" not in ln:
            return int(ln.split(None, 1)[0])
    return None


def _cleanup_marker(marker: str) -> None:
    subprocess.run(["pkill", "-f", marker], capture_output=True)
    try:
        os.unlink(f"/tmp/{marker}.log")
    except OSError:
        pass


def _linecount(path: Path) -> int:
    with open(path, "rb") as f:
        return sum(1 for _ in f)


def main() -> int:
    # ---------------- T1: linger with live bg shell ----------------
    print("T1 turn with bg shell → complete.json while runner lingers")
    m1 = f"bclinger-{uuid.uuid4().hex[:8]}"
    p1, d1 = _spawn("run1", _bg_prompt(m1))
    try:
        if not _wait_complete(d1):
            check(False, "T1 run1 never wrote complete.json")
            return 1
        c1 = json.loads((d1 / "complete.json").read_text())
        check(c1.get("success"), f"T1 turn succeeded ({c1.get('error')})")
        time.sleep(3)
        check(p1.poll() is None, "T1 runner ALIVE after complete.json (lingering)")
        check((d1 / "runner_alive").exists(),
              "T1 heartbeat still present during linger")
        bg1 = _find_marker_pid(m1)
        check(bg1 is not None, f"T1 bg shell running (pid={bg1})")

        # ---------------- T2: concurrent --resume turn ----------------
        print("T2 fresh --resume turn while run1 lingers; idle instance is silent")
        state1 = json.loads((d1 / "state.json").read_text())
        sid = state1.get("session_id")
        jsonl = Path(state1.get("jsonl_path") or "")
        check(bool(sid) and jsonl.exists(), f"T2 setup: sid={sid}")
        p2, d2 = _spawn("run2", "Reply with exactly SECOND_TURN.", session_id=sid)
        ok2 = _wait_complete(d2)
        check(ok2, "T2 second turn completed via --resume")
        if ok2:
            c2 = json.loads((d2 / "complete.json").read_text())
            check(c2.get("success"), f"T2 second turn success ({c2.get('error')})")
            check(c2.get("session_id") == sid,
                  "T2 --resume continued the same agent session")
        _wait_exit(p2)
        check(p1.poll() is None, "T2 run1 still lingering through run2's turn")
        lines_after_t2 = _linecount(jsonl)
        time.sleep(5)
        check(_linecount(jsonl) == lines_after_t2,
              "T2 lingering instance wrote NOTHING while idle (single writer)")

        # ---------------- T3: bg work ends → self-reap ----------------
        print("T3 bg shell dies → lingering runner self-reaps")
        if bg1:
            os.kill(bg1, 9)
        check(_wait_exit(p1, timeout=20), "T3 runner exited after bg work ended")
        check(not (d1 / "runner_alive").exists(),
              "T3 runner_alive removed on exit")
    finally:
        try:
            p1.kill()
        except Exception:
            pass
        _cleanup_marker(m1)

    # ---------------- T4: cancel sentinel sweeps + exits ----------------
    print("T4 cancel sentinel during linger → sweep + exit")
    m4 = f"bclinger-{uuid.uuid4().hex[:8]}"
    p4, d4 = _spawn("run4", _bg_prompt(m4))
    try:
        if not _wait_complete(d4):
            check(False, "T4 run4 never completed")
        else:
            time.sleep(2)
            check(p4.poll() is None and _find_marker_pid(m4) is not None,
                  "T4 setup: lingering with live bg shell")
            (d4 / "cancel").touch()
            check(_wait_exit(p4, timeout=15), "T4 runner exited on cancel")
            time.sleep(1)
            check(_find_marker_pid(m4) is None,
                  "T4 bg shell swept by the cancel (kill lever works)")
    finally:
        try:
            p4.kill()
        except Exception:
            pass
        _cleanup_marker(m4)

    # ---------------- T5: hard kill (session-delete path) ----------------
    print("T5 Provider.cancel_run hard-kills a lingering runner + bg work")
    m5 = f"bclinger-{uuid.uuid4().hex[:8]}"
    p5, d5 = _spawn("run5", _bg_prompt(m5))
    try:
        if not _wait_complete(d5):
            check(False, "T5 run5 never completed")
        else:
            time.sleep(2)
            from provider_claude import ClaudeProvider, RunState
            state5 = json.loads((d5 / "state.json").read_text())
            prov = ClaudeProvider.__new__(ClaudeProvider)
            prov._runs = {}
            prov.id = "linger-test"
            rs = RunState(
                run_id="run5", run_dir=d5, popen=p5, mode="native",
                app_session_id="linger-test-run5", queue=None,
                jsonl_path=Path(state5["jsonl_path"]) if state5.get("jsonl_path") else None,
                lingering=True,
            )
            prov._runs["run5"] = rs
            check(prov.lingering_runs("linger-test-run5") == ["run5"],
                  "T5 lingering_runs resolves the babysitter")
            prov.cancel_run("run5")
            check(_wait_exit(p5, timeout=15), "T5 runner dead after cancel_run")
            time.sleep(1)
            check(_find_marker_pid(m5) is None,
                  "T5 bg shell dead after cancel_run (hard kill sweeps)")
    finally:
        try:
            p5.kill()
        except Exception:
            pass
        _cleanup_marker(m5)

    print()
    if failures:
        print(f"FAILED: {len(failures)}")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("OK: babysitter linger e2e")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
