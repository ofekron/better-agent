"""A Codex turn abandoned mid-work must fail, not report success.

codex-cli ends an aborted turn (upstream stream death) with a normal
`task_complete` carrying `last_agent_message: null`. The turn has already
produced commentary and non-zero token usage, so the ghost-completion
guard — which needs zero usage AND no assistant output — passes it
through as a success. The session then goes idle mid-plan with no error,
which reads to the user as the turn stopping on its own.

Locks the discriminator and the shapes that must stay successful.
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import paths  # noqa: E402

_TMP = tempfile.mkdtemp(prefix="ba-aborted-turn-")
paths.engage_test_home(_TMP)

import runner_codex  # noqa: E402
from runner_guard import (  # noqa: E402
    ABORTED_ERROR,
    apply_aborted_turn_guard,
    apply_ghost_completion_guard,
)


def _rows(*items: dict) -> list[bytes]:
    return [json.dumps(i).encode("utf-8") for i in items]


def _assistant_item(text: str, phase: str | None = "final_answer") -> dict:
    payload = {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text}],
    }
    if phase is not None:
        payload["phase"] = phase
    return {"type": "response_item", "payload": payload}


def _tool_output(text: str = "ok") -> dict:
    return {
        "type": "response_item",
        "payload": {"type": "function_call_output", "output": text},
    }


def _task_complete(last_agent_message: str | None) -> dict:
    return {
        "type": "event_msg",
        "payload": {
            "type": "task_complete",
            "last_agent_message": last_agent_message,
        },
    }


def _usage(total: int) -> dict:
    return {
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {"total_token_usage": {
                "input_tokens": total, "output_tokens": total,
                "cached_input_tokens": 0, "total_tokens": total * 2,
            }},
        },
    }


def test_aborted_turn_is_detected() -> None:
    """The real incident: commentary + tool call, then the stream dies and
    codex reports task_complete with no final answer."""
    scan = runner_codex._scan_rollout_rows(_rows(
        _assistant_item("Next I'm reading the project map.", phase="commentary"),
        _usage(560),
        _tool_output("Plan updated"),
        _task_complete(None),
    ))
    assert scan.aborted is True, "mid-work abort must be flagged"
    assert scan.terminal is True, "codex still reports a successful terminal"
    assert scan.assistant_seen is True, "commentary output was produced"

    # The ghost guard cannot catch it: output was seen and usage is non-zero.
    success, error, _ = apply_ghost_completion_guard(
        success=True, cancelled=False, error=None, prompt="do the thing",
        assistant_seen=scan.assistant_seen, total_usage=scan.usage,
        result_seen=True,
    )
    assert success is True and error is None, "ghost guard is not the catcher"

    success, error = apply_aborted_turn_guard(
        success=success, cancelled=False, error=error,
        turn_aborted=scan.aborted,
    )
    assert success is False, "aborted turn must not report success"
    assert error == ABORTED_ERROR, error


def test_normal_completion_is_not_flagged() -> None:
    scan = runner_codex._scan_rollout_rows(_rows(
        _tool_output(),
        _usage(100),
        _assistant_item("Here is the answer."),
        _task_complete("Here is the answer."),
    ))
    assert scan.aborted is False
    success, error = apply_aborted_turn_guard(
        success=True, cancelled=False, error=None, turn_aborted=scan.aborted,
    )
    assert success is True and error is None


def test_old_rollout_format_is_not_flagged() -> None:
    """Older rollouts omit `last_agent_message` on turns that DID finish —
    127 of 466 real local rollouts. `last_agent_message is None` alone must
    therefore never condemn a turn."""
    scan = runner_codex._scan_rollout_rows(_rows(
        _tool_output(),
        _assistant_item("Done.", phase=None),
        _task_complete(None),
    ))
    assert scan.aborted is False, "old-format completion must stay successful"


def test_worker_turn_without_final_answer_is_not_flagged() -> None:
    """A worker turn delivers via inter-agent message and carries no
    `final_answer` phase; it still ends on an agent message and stays a
    success (runner_codex's documented exemption)."""
    scan = runner_codex._scan_rollout_rows(_rows(
        _tool_output("message sent"),
        _assistant_item("Relayed to the caller.", phase=None),
        _task_complete("Relayed to the caller."),
    ))
    assert scan.aborted is False
    success, error = apply_aborted_turn_guard(
        success=True, cancelled=False, error=None, turn_aborted=scan.aborted,
    )
    assert success is True and error is None


def test_abort_spanning_scan_chunks() -> None:
    """The incremental scanner sees the last response item and the
    `task_complete` in different reads; the verdict must survive the split."""
    first = runner_codex._scan_rollout_rows(_rows(
        _assistant_item("working", phase="commentary"), _tool_output(),
    ))
    assert first.aborted is False
    second = runner_codex._scan_rollout_rows(
        _rows(_task_complete(None)),
        last_item_assistant=first.last_item_assistant,
    )
    assert second.aborted is True, "split reads must not lose the abort"


def test_zero_output_ghost_stays_retryable() -> None:
    """A turn that produced NOTHING also reports `last_agent_message:
    null`, so the scan flags it too — 126 of 400 real local rollouts. The
    ghost guard must claim it FIRST and keep it retryable; the aborted
    guard is terminal and must not steal a turn that ran nothing."""
    scan = runner_codex._scan_rollout_rows(_rows(
        {"type": "response_item",
         "payload": {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": "hi"}]}},
        _task_complete(None),
    ))
    assert scan.aborted is True
    assert scan.assistant_seen is False

    success, error, retry_ghost = apply_ghost_completion_guard(
        success=True, cancelled=False, error=None, prompt="hi",
        assistant_seen=scan.assistant_seen, total_usage=scan.usage,
        result_seen=True,
    )
    assert retry_ghost is True, "empty turn must stay retryable"

    success, error = apply_aborted_turn_guard(
        success=success, cancelled=False, error=error,
        turn_aborted=scan.aborted,
    )
    assert error != ABORTED_ERROR, "ghost must keep its retryable outcome"


def test_cancelled_turn_keeps_its_own_outcome() -> None:
    success, error = apply_aborted_turn_guard(
        success=False, cancelled=True, error="cancelled", turn_aborted=True,
    )
    assert success is False and error == "cancelled"


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {test.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        import shutil
        shutil.rmtree(_TMP, ignore_errors=True)
