"""Regression: a turn ending with stop_reason=model_context_window_exceeded
(zero usage, empty completion) MUST be classified as a failed turn.

Z.AI returns ResultMessage(is_error=False) for context-window overflow, so
runner.py's `success = not msg.is_error` recorded it as success=true /
error=null and silently produced an empty result. The fix inspects
stop_reason via `_context_overflow_error` and flips success=False.

Fails before the fix (ImportError / result["success"] is True),
passes after.

Run with:
    cd backend && .venv/bin/python scripts/test_context_overflow_classified_error.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-ctx-overflow-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from claude_agent_sdk import AssistantMessage, ResultMessage  # noqa: E402

from runner import _context_overflow_error, _run_one_turn  # noqa: E402
from runs_dir import turn_dir  # noqa: E402


class _FakeResp:
    def __init__(self, msgs):
        self._it = iter(msgs)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration


class _FakeClient:
    def __init__(self, msgs):
        self._msgs = list(msgs)

    async def query(self, *a, **k):
        return None

    def receive_response(self):
        return _FakeResp(self._msgs)

    async def interrupt(self):
        return None


def _overflow_messages():
    assistant = AssistantMessage(
        content=[],
        model="glm-5.1",
        stop_reason="model_context_window_exceeded",
        usage={"input_tokens": 0, "output_tokens": 0},
    )
    result = ResultMessage(
        subtype="success",
        duration_ms=0,
        duration_api_ms=0,
        is_error=False,
        num_turns=1,
        session_id="sess-overflow",
        stop_reason="model_context_window_exceeded",
        usage={"input_tokens": 0, "output_tokens": 0},
        result="",
    )
    return [assistant, result]


async def _run(msgs):
    run_root = Path(_TMP_HOME) / "runs" / "run-x"
    run_root.mkdir(parents=True, exist_ok=True)
    result = await _run_one_turn(
        client=_FakeClient(msgs),
        prompt="hi",
        images=[],
        files=[],
        run_dir=run_root,
        turn_id="turn-1",
        pre_query_byte_offset=0,
        state={},
        state_path=run_root / "state.json",
        cwd=str(Path(_TMP_HOME)),
        claude_config_dir=Path(_TMP_HOME) / "claude-cfg",
        log=logging.getLogger("test-ctx-overflow"),
    )
    return result, run_root


def test_helper_flags_overflow_reasons():
    for sr in (
        "model_context_window_exceeded",
        "context_length_exceeded",
        "context_window_exceeded",
    ):
        assert _context_overflow_error(sr), f"expected overflow for {sr!r}"
    print("OK helper flags overflow stop_reasons")


def test_helper_ignores_normal_reasons():
    for sr in (
        "end_turn",
        "tool_use",
        "max_tokens",
        "stop_sequence",
        "length",
        "refusal",
        "pause_turn",
        "",
        None,
    ):
        assert _context_overflow_error(sr) is None, f"false positive for {sr!r}"
    print("OK helper ignores normal stop_reasons")


def test_turn_reported_as_error():
    result, run_root = asyncio.run(_run(_overflow_messages()))
    assert result["success"] is False, f"overflow turn must NOT be success: {result!r}"
    assert result["final_success"] is False, result
    assert result["error"], "overflow turn must carry an error"
    assert "context" in result["error"].lower(), result

    complete = json.loads((turn_dir(run_root, "turn-1") / "complete.json").read_text())
    assert complete["success"] is False, complete
    assert complete["error"], complete
    print("OK overflow turn reported as error via _run_one_turn")


def test_normal_turn_still_succeeds():
    assistant = AssistantMessage(
        content=[],
        model="glm-5.1",
        stop_reason="end_turn",
        usage={"input_tokens": 5, "output_tokens": 3},
    )
    result_msg = ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="sess-ok",
        stop_reason="end_turn",
        usage={"input_tokens": 5, "output_tokens": 3},
        result="done",
    )
    result, _ = asyncio.run(_run([assistant, result_msg]))
    assert result["success"] is True, f"normal turn must succeed: {result!r}"
    assert not result["error"], result
    print("OK normal turn still classified as success")


def main():
    try:
        test_helper_flags_overflow_reasons()
        test_helper_ignores_normal_reasons()
        test_turn_reported_as_error()
        test_normal_turn_still_succeeds()
        print("\nALL TESTS PASSED")
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    main()
