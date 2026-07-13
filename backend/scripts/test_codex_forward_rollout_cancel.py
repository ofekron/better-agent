from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
import sys

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

import json

from runner_codex import _forward_rollout_terminal


class _RolloutProc:
    returncode = None

    def __init__(self) -> None:
        self._mapped: asyncio.Queue[bytes] = asyncio.Queue()


async def test_forward_rollout_terminal_ignores_cancel_without_path() -> None:
    """Baseline: with no cancel_path wired up, the loop never notices a
    cancel sentinel — this is the pre-fix behavior for the call site
    before `cancel_path` was threaded through."""
    with tempfile.TemporaryDirectory() as tmp:
        rollout = Path(tmp) / "rollout.jsonl"
        rollout.write_text("", encoding="utf-8")
        cancel_path = Path(tmp) / "cancel"
        proc = _RolloutProc()
        task = asyncio.create_task(
            _forward_rollout_terminal(proc, str(rollout), byte_offset=0),
        )
        try:
            cancel_path.touch()
            await asyncio.sleep(0.3)
            assert not task.done(), "loop should not self-terminate without cancel_path wired up"
            assert proc._mapped.empty()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_forward_rollout_terminal_honors_cancel_sentinel() -> None:
    """A rollout that never reports terminal state (a ghost completion the
    scan can't classify, or any other stall) must not poll forever once
    cancel_turn() touches the sentinel — the loop must forward a synthetic
    terminal event so the stdout consumer can unblock and reap the
    process."""
    with tempfile.TemporaryDirectory() as tmp:
        rollout = Path(tmp) / "rollout.jsonl"
        rollout.write_text("", encoding="utf-8")
        cancel_path = Path(tmp) / "cancel"
        proc = _RolloutProc()
        task = asyncio.create_task(
            _forward_rollout_terminal(
                proc, str(rollout), byte_offset=0, cancel_path=cancel_path,
            ),
        )
        try:
            await asyncio.sleep(0.3)
            assert not task.done()
            assert proc._mapped.empty()

            cancel_path.touch()

            row = json.loads(await asyncio.wait_for(proc._mapped.get(), timeout=2))
            assert row["type"] == "turn.failed"
            assert row["error"]["message"] == "cancelled"
            assert row["rollout_terminal"] is True
            await asyncio.wait_for(task, timeout=1)
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def main() -> None:
    await test_forward_rollout_terminal_ignores_cancel_without_path()
    await test_forward_rollout_terminal_honors_cancel_sentinel()


if __name__ == "__main__":
    asyncio.run(main())
    print("PASS: codex forward-rollout loop honors cancel sentinel")
