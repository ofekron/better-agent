"""Regression: a per-turn `complete` must NOT fire until the tailer has
drained this turn's jsonl — otherwise a late-flushed final assistant line
lands AFTER finalize as a msg_id=None orphan (the bug behind the "stuck
mid-thinking" render divergence).

Drives the exact race: `complete.json` for a turn appears while the tailer
cursor (`rs.processed_byte`) is BEHIND the jsonl byte size.

Pre-fix (fixed sleep(0.2)): complete is enqueued ~0.2s later regardless of
the tailer → FAILS the "no complete while behind" assertion.
Post-fix (_await_tailer_drained): complete waits until the cursor catches
up → PASSES, and the tailer_drained_prev_turn sentinel is touched only then.
"""

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

import _test_home
_test_home.isolate("bc_drain_")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from provider_claude import ClaudeProvider, RunState  # noqa: E402

failures = []


def _check(cond, msg):
    print(("  PASS" if cond else "  FAIL") + f": {msg}")
    if not cond:
        failures.append(msg)


async def _run():
    prov = ClaudeProvider({"id": "drain-test-prov"})
    tmp = Path(tempfile.mkdtemp(prefix="bc_drain_run_"))
    run_dir = tmp / "run"
    run_dir.mkdir()
    jsonl = tmp / "session.jsonl"
    jsonl.write_text("\n".join(["{}"] * 5) + "\n", encoding="utf-8")  # 5 lines

    q: asyncio.Queue = asyncio.Queue()
    rs = RunState(
        run_id="r1", run_dir=run_dir, popen=object(), mode="native",
        app_session_id="s1", queue=q,
    )
    rs.jsonl_path = jsonl
    rs.processed_byte = 3  # tailer BEHIND: consumed 3 bytes of the file

    tid = "turn-1"
    td = run_dir / "turns" / tid
    td.mkdir(parents=True)
    (td / "complete.json").write_text(
        json.dumps({"success": True, "turn_id": tid, "session_id": "cs"}),
        encoding="utf-8",
    )
    sentinel = td / "tailer_drained_prev_turn"

    task = asyncio.create_task(prov._await_tailer_drained(rs, timeout=2.0))
    await asyncio.sleep(0.4)
    _check(not task.done(),
           "drain wait does NOT finish while tailer is behind")

    rs.processed_byte = jsonl.stat().st_size
    drained = await task
    _check(drained, "drain wait finishes once byte cursor reaches file size")


def main():
    asyncio.run(_run())
    print(f"\n{'PASS' if not failures else 'FAIL'}: {len(failures)} failed checks")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
