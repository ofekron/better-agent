"""Regression test: the runner must NOT remove its `runner_alive`
liveness sentinel before the run-level `complete.json` is durable.

The output-loss incident: `_run`'s finally unlinked `runner_alive`
(runner.py) BEFORE the run-level complete.json write, with a slow
`client.disconnect()` in between. A healthy, just-succeeded runner thus
had a window with no heartbeat sentinel but no completion artifact yet —
the stuck-runner watchdog SIGKILLed it there, the run-level complete.json
never landed, and the real output was discarded.

Fix: keep the heartbeat ticking through disconnect, write complete.json
FIRST, then stop the heartbeat and unlink the sentinel LAST.

Asserts (FAILS on pre-fix ordering):
  - at the moment `runner_alive` is unlinked, run-level complete.json
    already exists on disk.
  - end state: complete.json present, runner_alive gone.

Both paths run the runner in a CHILD PROCESS, because that is what the
runner is: `_run` and `main` terminate the process themselves rather
than returning (see runner_exit.hard_exit), so the exit status is the
only return value there is.

Run with:
    cd backend && .venv/bin/python scripts/test_runner_alive_outlives_complete.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-alive-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import runs_dir  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"

_INPUTS = {
    "mode": "native",
    "prompt": "hi",
    "images": [],
    "cwd": _TMP_HOME,
    "model": "glm-5.1",
    "session_id": None,
    # The runner refuses to spawn unless the backend stripped the
    # in-process timer AND background tools (fail-closed gates).
    "disallowed_tools": [
        "AskUserQuestion", "EnterPlanMode", "ExitPlanMode",
        "CronCreate", "CronDelete", "CronList", "ScheduleWakeup",
        "BashOutput", "KillShell", "TaskOutput", "TaskStop", "Monitor",
    ],
}

# The child patches the runner's collaborators, then hands control to the
# real `main`. `_SpyAlivePath` records, at unlink time, whether the
# run-level complete.json already existed — the ordering invariant — and
# writes it where the parent can read it after the child exits.
_CHILD = r'''
import asyncio, json, sys
from pathlib import Path
sys.path.insert(0, {backend!r})

import runner, runner_operation_host, runs_dir

# The operation host is not under test, and its bootstrap would dial a
# real broker socket inherited from the ambient environment.
runner_operation_host.hydrate_runner_inputs = lambda inputs, run_dir: inputs

RUN_DIR = Path({run_dir!r})
OBS = Path({obs!r})


class _SpyAlivePath(Path):
    def unlink(self, missing_ok: bool = False):
        OBS.write_text(json.dumps({{
            "unlink_seen": True,
            "complete_existed_at_unlink": (self.parent / "complete.json").exists(),
        }}), encoding="utf-8")
        return super().unlink(missing_ok=missing_ok)


class _FakeClient:
    def __init__(self, *a, **k):
        pass

    async def connect(self):
        # Yield so the heartbeat task gets to write runner_alive at least
        # once (mimics a real connect that awaits I/O).
        await asyncio.sleep(0)

    async def disconnect(self):
        await asyncio.sleep(0)


class _RaisingClient(_FakeClient):
    async def connect(self):
        # Let the heartbeat tick once (so runner_alive exists), THEN fail
        # the way a real `claude --resume <bogus>` connect would.
        await asyncio.sleep(0.05)
        raise RuntimeError("connect boom")


async def _fake_run_one_turn(**kwargs):
    return {{
        "success": True,
        "cancelled": False,
        "error": None,
        "discovered_sid": "sess-x",
        "total_usage": None,
        "sdk_output_parts": ["the real answer"],
        "final_success": True,
        "used_tools": [],
        "outstanding_tasks": set(),
    }}


runs_dir.runner_alive_path = lambda rd: _SpyAlivePath(rd / "runner_alive")
runner._resolve_claude_cli = lambda: Path("/usr/bin/true")
if {raising!r}:
    runner.ClaudeSDKClient = _RaisingClient
else:
    runner.ClaudeSDKClient = _FakeClient
    runner._run_one_turn = _fake_run_one_turn

runner.main(RUN_DIR)
'''


def _run_child(name: str, *, raising: bool) -> tuple[int, Path, dict]:
    run_dir = runs_dir.runs_root() / name
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "input.json").write_text(json.dumps(_INPUTS), encoding="utf-8")

    obs = run_dir / "observation.json"
    script = run_dir / "child.py"
    script.write_text(
        _CHILD.format(
            backend=_BACKEND, run_dir=str(run_dir), obs=str(obs), raising=raising
        ),
        encoding="utf-8",
    )
    proc = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True, text=True, timeout=120, env=dict(os.environ),
    )
    observed = {}
    if obs.exists():
        observed = json.loads(obs.read_text(encoding="utf-8"))
    return proc.returncode, run_dir, observed


def test_success_path() -> int:
    """Success: runner_alive must be unlinked AFTER complete.json exists."""
    failures = 0
    rc, run_dir, obs = _run_child("ordering-run", raising=False)

    if not obs.get("unlink_seen"):
        print(f"{FAIL}  runner_alive was never unlinked (rc={rc})")
        failures += 1
    elif obs.get("complete_existed_at_unlink") is True:
        print(f"{PASS}  runner_alive unlinked AFTER complete.json was durable")
    else:
        print(f"{FAIL}  runner_alive unlinked BEFORE complete.json existed "
              f"— the kill-race window (rc={rc})")
        failures += 1

    end_ok = (run_dir / "complete.json").exists() and not (run_dir / "runner_alive").exists()
    if end_ok:
        print(f"{PASS}  end state: complete.json present, runner_alive removed")
    else:
        print(f"{FAIL}  end state wrong: complete={(run_dir / 'complete.json').exists()} "
              f"alive={(run_dir / 'runner_alive').exists()}")
        failures += 1

    if rc == 0:
        print(f"{PASS}  success path exits 0")
    else:
        print(f"{FAIL}  success path exit status {rc}, expected 0")
        failures += 1
    return failures


def test_exception_path() -> int:
    """Exception (connect raises after the heartbeat ticked once): the
    runner must NOT leave a stale runner_alive — main()'s except writes
    the error complete.json then removes the sentinel."""
    failures = 0
    rc, run_dir, _ = _run_child("exc-run", raising=True)

    alive_gone = not (run_dir / "runner_alive").exists()
    complete_present = (run_dir / "complete.json").exists()
    if rc == 1 and alive_gone and complete_present:
        print(f"{PASS}  exception path: error complete.json written, runner_alive removed")
    else:
        print(f"{FAIL}  exception path leftover: rc={rc} "
              f"alive_gone={alive_gone} complete={complete_present}")
        failures += 1
    return failures


def _run_all() -> int:
    failures = 0
    try:
        failures += test_success_path()
        failures += test_exception_path()
        return 1 if failures else 0
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(_run_all())
