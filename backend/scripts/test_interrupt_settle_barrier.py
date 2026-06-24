"""Regression test: interrupting turn N must NOT poison turn N+1 on the
ClaudeSDKClient's single output stream.

Incident (session d0e8eeb8, run 7925d1fb): the user interrupted a turn
mid-tool-call (emulator `wait-for-device` Bash loop). The runner's cancel
watcher called `client.interrupt()` on the client, then
the receive loop BROKE OUT of `receive_response()` immediately — abandoning
the interrupted turn's terminating ResultMessage in the stream. The very
next turn (the reply to the interrupt message) called `client.query()` on
that same client, read the stale tail, and the CLI returned
`error_during_execution` in 2ms with zero output. The user saw an errored,
empty assistant message and had to type "continue".

Root cause: `client.interrupt()` only ACKs the control request; it does NOT
wait for the CLI to finish winding the turn down. The shared client was left
mid-stream for the next turn.

Fix: `runner._drain_until_result` — after an interrupt, drain (discard) the
interrupted turn to its terminating ResultMessage before returning, so the
shared client is idle before the next turn's query().

This test models the CLI's single output stream as a shared queue. Turn 1 is
interrupted mid-stream; turn 2 then runs. Pre-fix, turn 1's stale
ResultMessage contaminates turn 2 → turn 2 fails. Post-fix, turn 1 drains
its own tail → turn 2 succeeds.

Run with:
    cd backend && .venv/bin/python scripts/test_interrupt_settle_barrier.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import tempfile
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-settle-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from claude_agent_sdk import (  # noqa: E402
    AssistantMessage,
    ResultMessage,
    SystemMessage,
)

import runner  # noqa: E402

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


def _sys_init(sid: str) -> SystemMessage:
    return SystemMessage(subtype="init", data={"subtype": "init", "session_id": sid})


def _assistant(text: str) -> AssistantMessage:
    return AssistantMessage(content=[{"type": "text", "text": text}], model="test")


def _result(*, is_error: bool, subtype: str) -> ResultMessage:
    return ResultMessage(
        subtype=subtype,
        duration_ms=1,
        duration_api_ms=1,
        is_error=is_error,
        num_turns=1,
        session_id="sid",
        result=subtype,
    )


class FakeClient:
    """Models the CLI's SINGLE output stream as one shared queue
    across turns — exactly the shared-client topology that caused the bug."""

    def __init__(self) -> None:
        self._q: asyncio.Queue = asyncio.Queue()
        self.turn_scripts: list[list] = []
        self._query_count = 0
        self.interrupted = False

    async def query(self, prompt) -> None:
        # Push this turn's scripted output onto the shared stream.
        script = self.turn_scripts[self._query_count]
        self._query_count += 1
        for m in script:
            await self._q.put(m)

    async def interrupt(self) -> None:
        # The CLI ACKs, then flushes the interrupted turn's tail + a
        # terminating (error) ResultMessage onto the SAME shared stream.
        self.interrupted = True
        await self._q.put(_assistant("(tool wind-down after interrupt)"))
        await self._q.put(_result(is_error=True, subtype="interrupted"))

    async def receive_response(self):
        while True:
            msg = await self._q.get()
            yield msg
            if isinstance(msg, ResultMessage):
                return


async def _drive_turn(client, run_dir, turn_id, cancel_path, trigger_cancel):
    """Run one turn; if trigger_cancel, write the cancel sentinel shortly
    after start so the watcher fires interrupt() mid-stream."""
    log = logging.getLogger("test")

    async def _fire():
        await asyncio.sleep(0.3)
        cancel_path.write_text("1")

    fire_task = asyncio.create_task(_fire()) if trigger_cancel else None
    result = await runner._run_one_turn(
        client=client,
        prompt="p",
        images=[],
        files=[],
        run_dir=run_dir,
        turn_id=turn_id,
        pre_query_byte_offset=0,
        state={},
        state_path=run_dir / "state.json",
        cwd="/tmp/x",
        claude_config_dir=run_dir / "cfg",
        log=log,
        cancel_path=cancel_path,
    )
    if fire_task:
        await fire_task
    return result


async def main() -> None:
    run_dir = Path(tempfile.mkdtemp(prefix="run-", dir=_TMP_HOME))
    client = FakeClient()
    # Turn 1: streams init + assistant + a tool, then HANGS (no result) —
    # the long-running tool. The interrupt supplies its terminating result.
    client.turn_scripts.append([
        _sys_init("sid"),
        _assistant("starting long tool"),
    ])
    # Turn 2 (the reply to the interrupt): a normal successful turn.
    client.turn_scripts.append([
        _sys_init("sid"),
        _assistant("done"),
        _result(is_error=False, subtype="success"),
    ])

    # -- Turn 1: interrupted mid-stream --
    r1 = await _drive_turn(
        client, run_dir, "turn1", run_dir / "cancel1", trigger_cancel=True
    )
    _ok(client.interrupted, "turn1: interrupt() was invoked by watcher")
    _ok(r1.get("cancelled") is True, "turn1: ends cancelled", f"r1={r1}")
    _ok(client._q.empty(),
        "turn1: settle barrier drained the interrupted turn's tail "
        "(shared stream is idle)",
        f"qsize={client._q.qsize()}")

    # -- Turn 2: the reply turn, on the SAME shared client --
    r2 = await _drive_turn(
        client, run_dir, "turn2", run_dir / "cancel2", trigger_cancel=False
    )
    # The crux: pre-fix, turn 2 reads turn 1's stale "interrupted"
    # ResultMessage and fails with error_during_execution-equivalent.
    _ok(r2.get("success") is True and not r2.get("error"),
        "turn2: reply turn runs cleanly after interrupt (NOT poisoned)",
        f"r2={r2}")
    _ok(r2.get("cancelled") is not True, "turn2: not spuriously cancelled",
        f"r2={r2}")

    print()
    if failures:
        print(f"{FAIL}  {failures} assertion(s) failed")
        sys.exit(1)
    print(f"{PASS}  all assertions passed")


if __name__ == "__main__":
    asyncio.run(main())
