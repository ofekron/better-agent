from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

_TMP_HOME = tempfile.mkdtemp(prefix="ba-test-runner-watchdog-")
os.environ["BETTER_AGENT_HOME"] = _TMP_HOME

_HERE = Path(__file__).resolve().parent
_BACKEND = _HERE.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import runner  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


class _SilentResponse:
    async def __anext__(self):
        await asyncio.Event().wait()


class _OneResultResponse:
    def __init__(self) -> None:
        self._sent = False

    async def __anext__(self):
        if self._sent:
            raise StopAsyncIteration
        self._sent = True
        await asyncio.sleep(0.02)
        return _FakeResultMessage()


class _FakeResultMessage:
    is_error = False
    subtype = "success"
    result = "done"
    usage = None
    model_usage = None


class _FakeClient:
    def __init__(self, response) -> None:
        self._response = response
        self.queries = []
        self.interrupted = False

    async def query(self, prompt):
        self.queries.append(prompt)

    def receive_response(self):
        return self._response

    async def interrupt(self):
        self.interrupted = True


async def _run_turn(run_dir: Path, response, *, timeout_s: float) -> dict:
    state: dict = {}
    holder = [None]
    original_result_message = runner.ResultMessage
    runner.ResultMessage = _FakeResultMessage
    try:
        return await runner._run_one_turn(
            client=_FakeClient(response),
            prompt="hello",
            images=[],
            files=[],
            run_dir=run_dir,
            turn_id="turn-1",
            pre_query_byte_offset=0,
            state=state,
            state_path=run_dir / "state.json",
            cwd=str(run_dir),
            claude_config_dir=run_dir / "claude",
            log=runner.logger,
            current_turn_holder=holder,
            no_progress_timeout_s=timeout_s,
        )
    finally:
        runner.ResultMessage = original_result_message
        if holder[0] is not None:
            raise AssertionError("current turn holder was not cleared")


async def t_silent_receive_times_out() -> None:
    run_dir = Path(tempfile.mkdtemp(dir=_TMP_HOME))
    started = time.monotonic()
    result = await _run_turn(run_dir, _SilentResponse(), timeout_s=0.05)
    elapsed = time.monotonic() - started
    if elapsed > 1.0:
        raise AssertionError(f"silent receive took too long: {elapsed:.3f}s")
    if result["final_success"]:
        raise AssertionError("silent receive wrongly succeeded")
    if "no response progress" not in (result["error"] or ""):
        raise AssertionError(f"missing watchdog error: {result['error']!r}")
    complete = run_dir / "turns" / "turn-1" / "complete.json"
    if not complete.exists():
        raise AssertionError("turn complete.json was not written")


async def t_periodic_progress_is_not_timed_out() -> None:
    run_dir = Path(tempfile.mkdtemp(dir=_TMP_HOME))
    result = await _run_turn(run_dir, _OneResultResponse(), timeout_s=0.2)
    if not result["final_success"]:
        raise AssertionError(f"progressing turn failed: {result['error']!r}")


async def main_run() -> int:
    tests = [
        ("silent receive times out", t_silent_receive_times_out),
        ("periodic progress is not timed out", t_periodic_progress_is_not_timed_out),
    ]
    failed = 0
    for name, fn in tests:
        try:
            await fn()
            print(f"{PASS} {name}")
        except Exception as exc:
            failed += 1
            print(f"{FAIL} {name}: {exc}")
    return failed


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main_run()))
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
