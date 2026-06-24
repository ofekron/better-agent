#!/usr/bin/env python3
"""Integration test: codex fork mode forks a previous session into a new,
isolated thread that inherits the parent's conversation (codex app-server
`thread/fork`).

Before the fix, `ask(run_mode='fork')` on a codex worker raised
NotImplementedError (supports_fork was False) → HTTP 500. Now codex
forks. This test proves fork semantics end-to-end through the real
runner:

  * the fork run produces a DIFFERENT codex session id than its parent
    (a resume would reuse the parent's id; a fresh start would not
    inherit);
  * the fork's rollout is its own file (not the parent's); and
  * the fork's rollout CONTAINS the parent's prompt — i.e. it branched
    the parent's history (inheritance), which distinguishes a real fork
    from a brand-new session.

Self-contained: spawns `runner_codex.py` twice — first to seed a parent
rollout, then with fork=true — and inspects each run's state.json +
rollout. Requires the codex CLI on PATH (skips with a clear message).

Run:  cd backend && .venv/bin/python scripts/integration_test_codex_fork.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
RUNNER = ROOT / "backend" / "runner_codex.py"
MODEL = os.environ.get("BA_CODEX_FORK_TEST_MODEL", "gpt-5.4-mini")
TURN_TIMEOUT_S = 240
# Distinctive token planted in the parent prompt; a real fork's rollout
# inherits the parent's user message, so this token must appear in the
# fork's rollout. A fresh (non-inheriting) session would not have it.
PARENT_SENTINEL = "BCFORKSENTINEL7"


def _run_codex(home: str, run_dir: Path, inputs: dict) -> dict:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "input.json").write_text(json.dumps(inputs), encoding="utf-8")
    env = os.environ.copy()
    env["BETTER_CLAUDE_HOME"] = home
    proc = subprocess.Popen(
        [sys.executable, str(RUNNER), "--run-dir", str(run_dir)],
        env=env,
    )
    try:
        proc.wait(timeout=TURN_TIMEOUT_S)
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
    state_path = run_dir / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    return {"exit": proc.returncode, "state": state}


def main() -> None:
    if not shutil.which("codex"):
        print("SKIP: codex CLI not on PATH")
        return

    with tempfile.TemporaryDirectory(prefix="bc-codex-fork-") as home:
        cwd = str(ROOT)

        # 1) Seed a parent codex session with one turn so it has a rollout.
        parent = _run_codex(home, Path(home) / "runs" / "parent", {
            "prompt": f"Note the token {PARENT_SENTINEL}. Reply with exactly: ok",
            "cwd": cwd,
            "model": MODEL,
            "session_id": None,
            "mode": "native",
            "app_session_id": "codex-fork-test",
            "fork": False,
        })
        if parent["exit"] != 0 or not parent["state"].get("complete"):
            raise AssertionError(f"parent run failed: exit={parent['exit']} state={parent['state']}")
        parent_sid = parent["state"].get("session_id")
        parent_rollout = parent["state"].get("rollout_path")
        if not parent_sid or not parent_rollout:
            raise AssertionError(f"parent run produced no session/rollout: {parent['state']}")

        # 2) Fork the parent.
        fork = _run_codex(home, Path(home) / "runs" / "fork", {
            "prompt": "Reply with exactly: ok",
            "cwd": cwd,
            "model": MODEL,
            "session_id": parent_sid,
            "mode": "native",
            "app_session_id": "codex-fork-test",
            "fork": True,
        })
        if fork["exit"] != 0 or not fork["state"].get("complete"):
            raise AssertionError(f"fork run failed: exit={fork['exit']} state={fork['state']}")
        fork_sid = fork["state"].get("session_id")
        fork_rollout = fork["state"].get("rollout_path")
        if not fork_sid or not fork_rollout:
            raise AssertionError(f"fork run produced no session/rollout: {fork['state']}")

        # Fork = a NEW isolated thread (resume would reuse parent_sid).
        assert fork_sid != parent_sid, (
            f"fork did not create an isolated thread (fork_sid == parent_sid == {parent_sid})"
        )
        # The fork writes its own rollout, never the parent's.
        assert fork_rollout != parent_rollout, "fork wrote to the parent's rollout (not isolated)"
        # Inheritance: the fork branched the parent's history, so the
        # sentinel planted in the parent prompt must appear in the fork's
        # rollout. This is what separates a fork from a fresh session.
        fork_rollout_text = Path(fork_rollout).read_text(encoding="utf-8", errors="replace")
        assert PARENT_SENTINEL in fork_rollout_text, (
            "fork did not inherit the parent's history "
            f"(sentinel {PARENT_SENTINEL!r} absent from fork rollout)"
        )

        print(f"PASS: codex fork isolated parent {parent_sid} -> fork {fork_sid} (inherited history)")


if __name__ == "__main__":
    main()
