#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from live_llm_test_guard import require_live_llm_tests

ROOT = Path(__file__).resolve().parent.parent.parent


def main() -> None:
    if not require_live_llm_tests("live Codex app-server steering integration"):
        return

    with tempfile.TemporaryDirectory(prefix="bc-codex-steer-") as home:
        run_dir = Path(home) / "runs" / "test-run"
        run_dir.mkdir(parents=True)
        (run_dir / "input.json").write_text(json.dumps({
            "prompt": "Run sleep 8, then reply with BEFORE only unless I steer you.",
            "cwd": str(ROOT),
            "model": "gpt-5.4-mini",
            "session_id": None,
            "mode": "native",
            "app_session_id": "test-session",
        }), encoding="utf-8")
        env = os.environ.copy()
        env["BETTER_CLAUDE_HOME"] = home
        proc = subprocess.Popen(
            [sys.executable, str(ROOT / "backend" / "runner_codex.py"),
             "--run-dir", str(run_dir)],
            env=env,
        )
        try:
            state_path = run_dir / "state.json"
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if state_path.exists():
                    state = json.loads(state_path.read_text(encoding="utf-8"))
                    if state.get("turn_id"):
                        break
                time.sleep(0.05)
            else:
                raise AssertionError("Codex app-server did not publish an active turn id")
            (run_dir / "steer.jsonl").write_text(
                json.dumps({"prompt": "Reply with STEERED_OK instead."}) + "\n",
                encoding="utf-8",
            )
            assert proc.wait(timeout=90) == 0
            events = (run_dir / "session_events.jsonl").read_text(encoding="utf-8")
            assert "STEERED_OK" in events
            assert '"text": "BEFORE"' not in events
            print("PASS: Codex app-server accepted same-turn steering")
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)


if __name__ == "__main__":
    main()
