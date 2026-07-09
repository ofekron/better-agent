"""Regression: a context-window overflow whose machine label is wrong MUST
still be classified as `context_window_exceeded`.

Claude CLI (observed on 2.1.204 with a Z.AI base URL) stamps context-window
overflows on the synthetic assistant message as `error="max_output_tokens"`
with `stop_reason="stop_sequence"` — the only truthful signal is the text
block "API Error: The model has reached its context window limit." Before
the fix, runner.py surfaced the raw label, `is_context_overflow_error`
rejected it, and turn_manager's overflow→continuation branch never fired.
The fix normalizes the error message TEXT at the capture points.

Fails before the fix (error stays "max_output_tokens"), passes after.

Run with:
    cd backend && .venv/bin/python scripts/test_overflow_error_text_reclassified.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home  # noqa: E402
_TMP_HOME = _test_home.isolate("bc-test-overflow-text-")

from claude_agent_sdk import AssistantMessage, ResultMessage  # noqa: E402
from claude_agent_sdk.types import TextBlock  # noqa: E402

from continuation import CONTEXT_OVERFLOW_ERROR, is_context_overflow_error  # noqa: E402
from runner import _run_one_turn  # noqa: E402
from runs_dir import turn_dir  # noqa: E402

_OVERFLOW_TEXT = "API Error: The model has reached its context window limit."


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
        log=logging.getLogger("test-overflow-text"),
    )
    return result, run_root


def _mislabeled_overflow_messages():
    # Exact shape of the observed Z.AI overflow: synthetic assistant
    # message, wrong machine label, truthful text block.
    assistant = AssistantMessage(
        content=[TextBlock(text=_OVERFLOW_TEXT)],
        model="<synthetic>",
        error="max_output_tokens",
        stop_reason="stop_sequence",
        usage={"input_tokens": 0, "output_tokens": 0},
    )
    result = ResultMessage(
        subtype="error_during_execution",
        duration_ms=0,
        duration_api_ms=0,
        is_error=True,
        num_turns=1,
        session_id="sess-overflow-text",
        stop_reason="stop_sequence",
        usage={"input_tokens": 0, "output_tokens": 0},
        result=_OVERFLOW_TEXT,
    )
    return [assistant, result]


def test_mislabeled_overflow_reclassified():
    result, run_root = asyncio.run(_run(_mislabeled_overflow_messages()))
    assert result["success"] is False, result
    assert result["error"] == CONTEXT_OVERFLOW_ERROR, (
        f"expected {CONTEXT_OVERFLOW_ERROR!r}, got {result['error']!r}"
    )
    assert is_context_overflow_error(result["error"]), result

    complete = json.loads((turn_dir(run_root, "turn-1") / "complete.json").read_text())
    assert complete["error"] == CONTEXT_OVERFLOW_ERROR, complete
    print("OK mislabeled overflow reclassified via assistant text")


def test_result_only_overflow_reclassified():
    # Overflow text present only on the ResultMessage error path.
    result_msg = ResultMessage(
        subtype="error_during_execution",
        duration_ms=0,
        duration_api_ms=0,
        is_error=True,
        num_turns=1,
        session_id="sess-overflow-result",
        stop_reason="stop_sequence",
        usage={"input_tokens": 0, "output_tokens": 0},
        result=_OVERFLOW_TEXT,
    )
    result, _ = asyncio.run(_run([result_msg]))
    assert result["success"] is False, result
    assert result["error"] == CONTEXT_OVERFLOW_ERROR, result
    print("OK result-only overflow reclassified via result text")


def test_genuine_max_output_tokens_untouched():
    # A real per-turn max-tokens stop must NOT be misclassified as overflow.
    assistant = AssistantMessage(
        content=[TextBlock(text="Streaming fell short of expected output.")],
        model="glm-5.2",
        error="max_output_tokens",
        stop_reason="max_tokens",
        usage={"input_tokens": 10, "output_tokens": 4096},
    )
    result_msg = ResultMessage(
        subtype="error_during_execution",
        duration_ms=0,
        duration_api_ms=0,
        is_error=True,
        num_turns=1,
        session_id="sess-max-out",
        stop_reason="max_tokens",
        usage={"input_tokens": 10, "output_tokens": 4096},
        result="Streaming fell short of expected output.",
    )
    result, _ = asyncio.run(_run([assistant, result_msg]))
    assert result["success"] is False, result
    assert result["error"] == "max_output_tokens", result
    assert not is_context_overflow_error(result["error"]), result
    print("OK genuine max_output_tokens label preserved")


def main():
    try:
        test_mislabeled_overflow_reclassified()
        test_result_only_overflow_reclassified()
        test_genuine_max_output_tokens_untouched()
        print("\nALL TESTS PASSED")
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    main()
