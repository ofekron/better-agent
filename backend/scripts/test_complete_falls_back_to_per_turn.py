"""Regression test: a turn that SUCCEEDED must surface its real output
even when the runner died before writing the run-level complete.json.

Incident: the stuck-runner watchdog SIGKILLed a healthy native runner
in the ~30ms gap AFTER the turn wrote its per-turn
`turns/<turn_id>/complete.json` (success + output, runner.py:1659) but
BEFORE it wrote the run-level `run_dir/complete.json` (runner.py:2070).
`_emit_complete_from_file` read ONLY the run-level file, so on its
absence it synthesized `{success:false, error:"runner exited without
writing complete.json"}` and DISCARDED the real output — the user saw
an empty errored assistant message ("we ran a turn and got nothing
back"). The recovery synth (recover_in_flight) had the same gap.

Fix: `runs_dir.read_best_complete` prefers the run-level file but falls
back to the latest per-turn complete.json; both the live emit path and
the recovery synth use it.

Asserts (each FAILS on pre-fix code):
  A. read_best_complete: run-level preferred; per-turn fallback;
     latest-by-mtime; None when neither.
  B. _emit_complete_from_file enqueues the recovered per-turn payload
     (success=True + real output), not the synthetic error.
  C. recover_in_flight on a dead orphan with a surviving per-turn
     complete.json promotes the real output to run-level — does NOT
     overwrite it with the synthetic "runner died" error.

Run with:
    cd backend && .venv/bin/python scripts/test_complete_falls_back_to_per_turn.py
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

# Per CLAUDE.md: isolate ~/.better-claude state BEFORE importing backend.
import _test_home
_TMP_HOME = _test_home.isolate("bc-test-perturn-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from runs_dir import read_best_complete, runs_root, turn_dir  # noqa: E402
from provider_claude import ClaudeProvider, RunState  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"

failures = 0


def _ok(cond: bool, label: str, detail: str = "") -> None:
    global failures
    if cond:
        print(f"{PASS}  {label}")
    else:
        print(f"{FAIL}  {label}  {detail}")
        failures += 1


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))


def test_read_best_complete() -> None:
    base = Path(tempfile.mkdtemp(prefix="rbc-", dir=_TMP_HOME))

    # neither → None
    d_none = base / "none"
    d_none.mkdir()
    _ok(read_best_complete(d_none) is None, "read_best_complete: neither → None")

    # only per-turn → returns it. Use the REAL field name the runner
    # writes (runner.py:1654) + that consumers read (orchestrator.py:2934,
    # run_recovery.py:244): `sdk_output`, not an arbitrary key.
    d_pt = base / "perturn"
    _write(turn_dir(d_pt, "t1") / "complete.json",
           {"success": True, "sdk_output": "hi", "session_id": "s"})
    got = read_best_complete(d_pt)
    _ok(got is not None and got.get("success") is True and got.get("sdk_output") == "hi",
        "read_best_complete: per-turn fallback when no run-level", f"got={got}")

    # run-level present → preferred over per-turn
    d_both = base / "both"
    _write(turn_dir(d_both, "t1") / "complete.json", {"success": True, "sdk_output": "turn"})
    _write(d_both / "complete.json", {"success": True, "sdk_output": "runlevel"})
    got = read_best_complete(d_both)
    _ok(got is not None and got.get("sdk_output") == "runlevel",
        "read_best_complete: run-level preferred over per-turn", f"got={got}")

    # multiple per-turns → latest by mtime wins
    d_multi = base / "multi"
    _write(turn_dir(d_multi, "old") / "complete.json", {"success": True, "sdk_output": "old"})
    time.sleep(0.02)
    _write(turn_dir(d_multi, "new") / "complete.json", {"success": True, "sdk_output": "new"})
    got = read_best_complete(d_multi)
    _ok(got is not None and got.get("sdk_output") == "new",
        "read_best_complete: latest per-turn by mtime wins", f"got={got}")

    # a FAILED/cancelled per-turn (success:false) must be surfaced as-is,
    # never masked as success — the fix promotes the runner's own verdict.
    d_fail = base / "failed"
    _write(turn_dir(d_fail, "t1") / "complete.json",
           {"success": False, "error": "boom", "sdk_output": None})
    got = read_best_complete(d_fail)
    _ok(got is not None and got.get("success") is False and got.get("error") == "boom",
        "read_best_complete: failed per-turn surfaced, not masked as success", f"got={got}")


async def test_emit_complete_from_file() -> None:
    rec = {"id": "test-claude", "kind": "claude"}
    provider = ClaudeProvider(rec)

    run_dir = Path(tempfile.mkdtemp(prefix="emit-", dir=_TMP_HOME))
    # Turn SUCCEEDED (per-turn complete written) but NO run-level file —
    # exactly the watchdog-SIGKILL-in-the-gap shape.
    _write(turn_dir(run_dir, run_dir.name) / "complete.json", {
        "success": True,
        "session_id": "sess-1",
        "sdk_output": "the real answer",
        "token_usage": {"output_tokens": 42},
    })

    q: asyncio.Queue = asyncio.Queue()
    rs = RunState(
        run_id=run_dir.name, run_dir=run_dir, popen=object(),  # popen unused here
        mode="native", app_session_id="app-1", queue=q, session_id="sess-1",
    )
    await provider._emit_complete_from_file(rs, run_dir / "complete.json")

    ev = q.get_nowait()
    _ok(
        ev.type == "complete"
        and ev.data.get("success") is True
        and ev.data.get("sdk_output") == "the real answer"
        and "runner exited without writing complete.json" not in str(ev.data.get("error")),
        "_emit_complete_from_file: surfaces per-turn sdk_output, not synthetic error",
        f"data={ev.data}",
    )


def test_recover_in_flight_promotes_per_turn() -> None:
    rec = {"id": "test-claude", "kind": "claude"}
    provider = ClaudeProvider(rec)

    run_dir = runs_root() / "deadorphan-run"
    run_dir.mkdir(parents=True, exist_ok=True)
    # Dead pid (no run-level complete.json) but a surviving successful
    # per-turn complete.json.
    (run_dir / "pid").write_text("2147480000")
    _write(run_dir / "backend_state.json",
           {"app_session_id": "app-1", "runner_pid": 2147480000, "mode": "native"})
    _write(turn_dir(run_dir, run_dir.name) / "complete.json", {
        "success": True,
        "session_id": "sess-1",
        "sdk_output": "recovered work",
        "token_usage": None,
    })

    provider.recover_in_flight(run_id_filter={run_dir.name})

    written = json.loads((run_dir / "complete.json").read_text())
    _ok(
        written.get("success") is True
        and written.get("sdk_output") == "recovered work"
        and "recovered at startup" not in str(written.get("error")),
        "recover_in_flight: promotes per-turn output, not synthetic dead-orphan error",
        f"written={written}",
    )


async def main() -> int:
    try:
        test_read_best_complete()
        await test_emit_complete_from_file()
        test_recover_in_flight_promotes_per_turn()
        return 1 if failures else 0
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
