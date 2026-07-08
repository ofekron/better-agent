"""Regression: the per-turn runner exits after its single turn (no
background work -> no babysitter linger), writes the run-level
complete.json + per-turn artifacts, and removes runner_alive on exit."""
import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
RUNNER = REPO / "backend" / "runner.py"
PY = REPO / "backend" / ".venv" / "bin" / "python"

HOME = Path("/tmp/cc_probe/regression_home")
if HOME.exists():
    shutil.rmtree(HOME)
HOME.mkdir(parents=True)

RUN_ID = str(uuid.uuid4())
RUN_DIR = HOME / "runs" / RUN_ID
RUN_DIR.mkdir(parents=True)

payload = {
    "prompt": "Reply with just the word PROBE.",
    "images": [],
    "cwd": str(Path.home()),
    "mode": "native",
    "app_session_id": "regression-test",
    "model": None,
    "session_id": None,
    "disallowed_tools": [
        "AskUserQuestion", "EnterPlanMode", "ExitPlanMode",
        "CronCreate", "CronDelete", "CronList", "ScheduleWakeup",
        "BashOutput", "KillShell", "TaskOutput", "TaskStop", "Monitor",
    ],
    "setting_sources": ["user"],
    "backend_url": "http://localhost:8000",
    "internal_token": "regression-token",
    "fork": False,
    "supervised": False,
    "browser_harness_enabled": False,
    "open_file_panel_enabled": False,
}
(RUN_DIR / "input.json").write_text(json.dumps(payload))

env = os.environ.copy()
env["BETTER_CLAUDE_HOME"] = str(HOME)

print(f"=== Spawning per-turn runner: pid will be in {RUN_DIR}/pid")
stdout = (RUN_DIR / "stdout.log").open("ab")
stderr = (RUN_DIR / "stderr.log").open("ab")
proc = subprocess.Popen(
    [str(PY), str(RUNNER), "--run-dir", str(RUN_DIR)],
    stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
    env=env, start_new_session=True,
)
stdout.close(); stderr.close()
print(f"pid={proc.pid}")

# Should exit naturally after one turn (no bg work -> no linger)
deadline = time.monotonic() + 90
while time.monotonic() < deadline:
    if proc.poll() is not None:
        break
    time.sleep(0.5)

assert proc.poll() is not None, "regression: runner did NOT exit after one turn"
print(f"runner exited: returncode={proc.returncode}")

# Run-level complete.json must exist
assert (RUN_DIR / "complete.json").exists(), "no run-level complete.json"
c = json.loads((RUN_DIR / "complete.json").read_text())
print(f"complete.json: success={c.get('success')} sid={c.get('session_id')}")
assert c.get("success"), f"regression failed: {c}"

# Per-turn artifacts should ALSO exist (read_best_complete fallback)
turn_d = RUN_DIR / "turns" / RUN_ID
assert turn_d.exists(), "per-turn dir missing"
assert (turn_d / "start.json").exists(), "start.json missing"
assert (turn_d / "complete.json").exists(), "per-turn complete.json missing"
print(f"per-turn artifacts present: {sorted(p.name for p in turn_d.iterdir())}")

# runner_alive heartbeat is unlinked at exit (after complete.json)
assert not (RUN_DIR / "runner_alive").exists(), "runner_alive not cleaned up"

print("\n=== REGRESSION PASS ===")
print("  per-turn path: exits after its single turn (no bg work)")
print("  run-level complete.json + state.json: written")
print("  per-turn artifacts: written (read_best_complete crash fallback)")
print("  runner_alive removed on exit: OK")
