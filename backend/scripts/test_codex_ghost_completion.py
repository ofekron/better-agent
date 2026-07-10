"""Regression tests for the Codex ghost-completion guard.

Bug: Codex CLI occasionally completes a turn with ``task_complete`` but
``last_agent_message: null`` and ZERO assistant/response items in its
rollout (no agent_message, no error). BA mapped ANY ``task_complete``
to ``success=True`` with no check that assistant content was produced,
so an empty assistant bubble was recorded as success and refresh
faithfully returned empty forever.

Locks the Codex half of commit c55211af8's ghost-completion guard:
``_rollout_terminal_state`` now also reports whether any non-empty
``agent_message`` was seen, and the runner finalization fails closed as
retryable ``prompt_not_executed`` (shared with the Claude runner via
``apply_ghost_completion_guard``) when a task_complete produced no
assistant output for a non-empty prompt with zero token usage.

Run with:
    cd backend && .venv/bin/python scripts/test_codex_ghost_completion.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home  # noqa: E402
_TMP_HOME = _test_home.isolate("bc-test-codex-ghost-")  # noqa: E402

import runner_codex  # noqa: E402
from runner_guard import apply_ghost_completion_guard  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"

_results: list[tuple[str, bool, str]] = []


def _check(name: str, cond: bool, detail: str = "") -> None:
    _results.append((name, bool(cond), detail))
    print(f"  {PASS if cond else FAIL} {name}" + (f" — {detail}" if detail and not cond else ""))


def _rollout_line(payload_type: str, **fields) -> str:
    return json.dumps({"type": "event_msg", "payload": {"type": payload_type, **fields}})


def _response_message(
    text: str,
    *,
    role: str = "assistant",
    phase: str | None = None,
) -> str:
    return json.dumps({
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": role,
            "phase": phase,
            "content": [{"type": "output_text", "text": text}],
        },
    })


def _write_rollout(path: Path, lines: list[str]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


# ─── Test 1 — _rollout_terminal_state reports assistant_seen ──────

def test_rollout_assistant_seen(tmp: Path) -> None:
    # Ghost: task_complete, no agent_message, no tokens.
    ghost = _write_rollout(tmp / "ghost.jsonl", [
        _rollout_line("task_complete"),
    ])
    terminal, usage, assistant_seen = runner_codex._rollout_terminal_state(ghost)
    _check("1a: ghost rollout is terminal", terminal is True)
    _check("1b: ghost rollout saw no assistant content", assistant_seen is False)
    _check("1c: ghost rollout has zero usage", usage == {})

    # Legit: agent_message with text + task_complete + non-zero tokens.
    legit = _write_rollout(tmp / "legit.jsonl", [
        _rollout_line("agent_message", message="here is the answer"),
        _rollout_line(
            "token_count",
            info={"total_token_usage": {"input_tokens": 12, "output_tokens": 5}},
        ),
        _rollout_line("task_complete"),
    ])
    terminal, usage, assistant_seen = runner_codex._rollout_terminal_state(legit)
    _check("1d: legit rollout is terminal", terminal is True)
    _check("1e: legit rollout saw assistant content", assistant_seen is True)
    _check("1f: legit rollout captured usage", usage.get("input_tokens") == 12)

    # Empty agent_message text does NOT count as assistant content.
    empty_msg = _write_rollout(tmp / "empty.jsonl", [
        _rollout_line("agent_message", message="   "),
        _rollout_line("task_complete"),
    ])
    _, _, assistant_seen = runner_codex._rollout_terminal_state(empty_msg)
    _check("1g: whitespace-only agent_message is not assistant content", assistant_seen is False)

    current = _write_rollout(tmp / "current.jsonl", [
        _response_message("current primary answer"),
        _rollout_line("task_complete"),
    ])
    terminal, _, assistant_seen = runner_codex._rollout_terminal_state(current)
    _check("1h: current response_item primary output is recognized",
           terminal is True and assistant_seen is True)

    inter_agent = _write_rollout(tmp / "inter-agent.jsonl", [
        json.dumps({"type": "response_item", "payload": {
            "type": "agent_message", "author": "/root/child",
            "content": [{"type": "input_text", "text": "child answer"}],
        }}),
        _rollout_line("task_complete"),
    ])
    _, _, assistant_seen = runner_codex._rollout_terminal_state(inter_agent)
    _check("1i: inter-agent output is not primary assistant content", assistant_seen is False)


def test_rollout_cumulative_preamble(tmp: Path) -> None:
    """The Codex rollout is cumulative across resumed turns: prior turns'
    events sit before this run's pre_query_byte_offset. A byte-0 scan would
    let a PRIOR turn's agent_message/task_complete neuter the guard. The
    scan must start at the offset so only THIS turn counts."""
    # Build a file: a preamble (prior turn with agent_message + task_complete)
    # then THIS ghost turn (task_complete, no agent_message) after the offset.
    preamble = [
        _rollout_line("agent_message", message="prior turn output"),
        _rollout_line("task_complete"),
    ]
    this_turn = [
        _rollout_line("task_complete"),  # ghost: terminal, no assistant output
    ]
    path = tmp / "cumulative.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(preamble) + "\n", encoding="utf-8")
    offset = path.stat().st_size
    with path.open("a", encoding="utf-8") as f:
        f.write("\n".join(this_turn) + "\n")

    # Byte-0 scan (the bug): preamble agent_message poisons assistant_seen.
    _, _, poisoned = runner_codex._rollout_terminal_state(str(path))
    _check("1h: byte-0 scan sees preamble assistant content (the bug)", poisoned is True)

    # Offset scan (the fix): only THIS turn counts -> no assistant content.
    terminal, _, assistant_seen = runner_codex._rollout_terminal_state(
        str(path), byte_offset=offset,
    )
    _check("1i: offset scan ignores preamble assistant content", assistant_seen is False)
    _check("1j: offset scan still sees this turn's terminal", terminal is True)

    # And the full finalization path flags it as a ghost via the guard.
    res = _finalize_like_run(prompt="do the thing", rollout_path=str(path), byte_offset=offset)
    _check("1k: cumulative ghost flagged prompt_not_executed", res["error"] == "prompt_not_executed")


# ─── Test 2 — end-to-end guard behavior (what _run finalization does)

def _finalize_like_run(*, prompt, rollout_path, byte_offset=0):
    """Mirror the exact finalization sequence runner_codex._run runs
    after the rollout terminal check: terminal success + the shared
    ghost-completion guard."""
    terminal, rollout_usage, assistant_seen = runner_codex._rollout_terminal_state(
        rollout_path, byte_offset=byte_offset,
    )
    success = False
    error = None
    turn_completed_seen = False
    total_usage: dict = {}
    if terminal is True:
        turn_completed_seen = True
        success = True
        if rollout_usage:
            total_usage = rollout_usage
    success, error = apply_ghost_completion_guard(
        success=success,
        cancelled=False,
        error=error,
        prompt=prompt,
        assistant_seen=assistant_seen,
        total_usage=total_usage,
        result_seen=turn_completed_seen,
    )
    final_success = success and not error
    return {"success": success, "error": error, "final_success": final_success}


def test_guard_fails_ghost_and_passes_legit(tmp: Path) -> None:
    ghost = _write_rollout(tmp / "g.jsonl", [_rollout_line("task_complete")])
    res = _finalize_like_run(prompt="do the thing", rollout_path=ghost)
    _check("2a: ghost finalized as failure", res["final_success"] is False)
    _check("2b: ghost flagged prompt_not_executed", res["error"] == "prompt_not_executed")

    legit = _write_rollout(tmp / "l.jsonl", [
        _rollout_line("agent_message", message="done"),
        _rollout_line("task_complete"),
    ])
    res = _finalize_like_run(prompt="do the thing", rollout_path=legit)
    _check("2c: legit turn stays success", res["final_success"] is True, str(res.get("error")))


def test_parent_final_phase(tmp: Path) -> None:
    commentary_only = _write_rollout(tmp / "commentary-only.jsonl", [
        _rollout_line(
            "agent_message",
            message="Sending the revised diff back to the reviewer.",
            phase="commentary",
        ),
        _response_message(
            "Sending the revised diff back to the reviewer.",
            phase="commentary",
        ),
        json.dumps({
            "type": "response_item",
            "payload": {"type": "function_call", "name": "wait_agent"},
        }),
        json.dumps({
            "type": "response_item",
            "payload": {
                "type": "agent_message",
                "author": "/root/reviewer",
                "content": [{"type": "input_text", "text": "review complete"}],
            },
        }),
    ])
    terminal, _, assistant_seen = runner_codex._rollout_terminal_state(commentary_only)
    final_answer_seen = runner_codex._rollout_parent_final_seen(commentary_only)
    _check("2d: commentary-only subagent rollout has no terminal", terminal is None)
    _check("2e: commentary remains primary assistant content", assistant_seen is True)
    _check("2f: commentary is not a parent final answer", final_answer_seen is False)

    final_answer = _write_rollout(tmp / "final-answer.jsonl", [
        _rollout_line("agent_message", message="All work is complete.", phase="final_answer"),
        _response_message("All work is complete.", phase="final_answer"),
        _rollout_line("task_complete"),
    ])
    terminal, _, assistant_seen = runner_codex._rollout_terminal_state(final_answer)
    final_answer_seen = runner_codex._rollout_parent_final_seen(final_answer)
    _check(
        "2g: explicit parent final answer is accepted",
        terminal is True and assistant_seen is True and final_answer_seen is True,
    )

    success, error = runner_codex._apply_parent_final_guard(
        success=True,
        cancelled=False,
        error=None,
        prompt="review and finish",
        final_answer_seen=False,
        result_seen=True,
    )
    _check(
        "2h: commentary-only success fails closed",
        success is False and error == "parent_final_not_emitted",
    )
    success, error = runner_codex._apply_parent_final_guard(
        success=True,
        cancelled=False,
        error=None,
        prompt="review and finish",
        final_answer_seen=True,
        result_seen=True,
    )
    _check("2i: explicit parent final stays successful", success is True and error is None)


# ─── Test 4 — network-retry attempt isolation

def test_retry_attempt_isolation(tmp: Path) -> None:
    """A network-retried turn appends a fresh attempt to the SAME cumulative
    rollout. The guard must scan from THIS attempt's start (captured at
    thread.started), not the run start — otherwise a failed attempt's
    partial agent_message before the retry would neuter the guard."""
    path = tmp / "retry.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    run_start_offset = 0  # pre_query_byte_offset: file is empty at run start
    # Failed attempt 1: streamed a partial agent_message, then died (no
    # task_complete).
    path.write_text("\n".join([
        _rollout_line("agent_message", message="partial reply before net error"),
    ]) + "\n", encoding="utf-8")
    # Attempt 2 (the retry that "succeeds"): ghost — task_complete, no agent_message.
    attempt_start = path.stat().st_size
    with path.open("a", encoding="utf-8") as f:
        f.write("\n".join([_rollout_line("task_complete")]) + "\n")

    # Run-start offset (the bug): includes the failed attempt's agent_message.
    _, _, poisoned = runner_codex._rollout_terminal_state(str(path), byte_offset=run_start_offset)
    _check("4a: run-start scan sees failed attempt's content (the bug)", poisoned is True)

    # Per-attempt offset (the fix): excludes the failed attempt.
    terminal, _, assistant_seen = runner_codex._rollout_terminal_state(
        str(path), byte_offset=attempt_start,
    )
    _check("4b: attempt-start scan excludes failed attempt", assistant_seen is False)
    _check("4c: attempt-start scan sees this attempt's terminal", terminal is True)

    # Full finalization with the attempt offset flags the retry as a ghost.
    res = _finalize_like_run(prompt="do the thing", rollout_path=str(path), byte_offset=attempt_start)
    _check("4d: retried ghost flagged prompt_not_executed", res["error"] == "prompt_not_executed")


# ─── Test 5 — resumed-session cumulative usage isolation


def test_resumed_cumulative_usage(tmp: Path) -> None:
    """Rollout `token_count` events report usage CUMULATIVE across the whole
    native session. On a resumed turn (turn N>1) the slice after the attempt
    boundary re-reports the prior turns' totals, so raw slice usage is
    non-zero even when THIS turn produced nothing — neutering the guard's
    zero-usage condition AND overcounting the turn's token_usage. The slice
    usage must be the DELTA against the last cumulative usage before the
    boundary."""
    prior_totals = {"input_tokens": 1000, "output_tokens": 200, "cached_input_tokens": 700}
    preamble = [
        _rollout_line("agent_message", message="prior turn output"),
        _rollout_line("token_count", info={"total_token_usage": prior_totals}),
        _rollout_line("task_complete"),
    ]
    # Ghost resumed turn: re-emitted cumulative token_count (unchanged
    # totals), task_complete, no agent_message — the observed fugu/codex
    # silent-failure shape.
    path = tmp / "resumed_ghost.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(preamble) + "\n", encoding="utf-8")
    offset = path.stat().st_size
    with path.open("a", encoding="utf-8") as f:
        f.write("\n".join([
            _rollout_line("token_count", info={"total_token_usage": prior_totals}),
            _rollout_line("task_complete"),
        ]) + "\n")

    terminal, usage, assistant_seen = runner_codex._rollout_terminal_state(
        str(path), byte_offset=offset,
    )
    _check("5a: resumed ghost slice usage delta is zero",
           not usage or sum(usage.values()) == 0, f"usage={usage}")
    _check("5b: resumed ghost slice saw no assistant content", assistant_seen is False)
    res = _finalize_like_run(prompt="do the thing", rollout_path=str(path), byte_offset=offset)
    _check("5c: resumed ghost flagged prompt_not_executed",
           res["error"] == "prompt_not_executed", str(res))

    # Real resumed turn: cumulative totals advance; usage must be the
    # per-turn delta, not the session totals.
    path2 = tmp / "resumed_real.jsonl"
    path2.write_text("\n".join(preamble) + "\n", encoding="utf-8")
    offset2 = path2.stat().st_size
    with path2.open("a", encoding="utf-8") as f:
        f.write("\n".join([
            _rollout_line("agent_message", message="turn 2 answer"),
            _rollout_line("token_count", info={"total_token_usage": {
                "input_tokens": 1500, "output_tokens": 260, "cached_input_tokens": 1100,
            }}),
            _rollout_line("task_complete"),
        ]) + "\n")
    terminal, usage, assistant_seen = runner_codex._rollout_terminal_state(
        str(path2), byte_offset=offset2,
    )
    _check("5d: real resumed turn is terminal with assistant content",
           terminal is True and assistant_seen is True)
    _check("5e: real resumed turn usage is the per-turn delta",
           usage.get("input_tokens") == 500
           and usage.get("output_tokens") == 60
           and usage.get("cache_read_input_tokens") == 400,
           f"usage={usage}")
    res = _finalize_like_run(prompt="do the thing", rollout_path=str(path2), byte_offset=offset2)
    _check("5f: real resumed turn stays success", res["final_success"] is True, str(res.get("error")))

    # First turn of a fresh session (byte_offset=0): no baseline, usage
    # passes through untouched.
    fresh = _write_rollout(tmp / "fresh.jsonl", [
        _rollout_line("agent_message", message="answer"),
        _rollout_line("token_count", info={"total_token_usage": {"input_tokens": 12, "output_tokens": 5}}),
        _rollout_line("task_complete"),
    ])
    _, usage, _ = runner_codex._rollout_terminal_state(fresh)
    _check("5g: fresh-session usage passes through", usage.get("input_tokens") == 12, f"usage={usage}")


# ─── Test 3 — guard narrowness (does NOT fire on edge cases)

def test_guard_narrowness(tmp: Path) -> None:
    ghost = _write_rollout(tmp / "g.jsonl", [_rollout_line("task_complete")])

    # Empty prompt: a result-only turn is not a ghost even with no output.
    res = _finalize_like_run(prompt="", rollout_path=ghost)
    _check("3a: empty prompt is not flagged", res["error"] is None and res["success"] is True)

    # Cancelled turn: guard must not override a cancel.
    terminal, rollout_usage, assistant_seen = runner_codex._rollout_terminal_state(ghost)
    success, error = apply_ghost_completion_guard(
        success=True, cancelled=True, error=None, prompt="x",
        assistant_seen=assistant_seen, total_usage={},
        result_seen=terminal is True,
    )
    _check("3b: cancelled turn left as-is", success is True and error is None)

    # Non-empty prompt but the rollout never reached a terminal state:
    # result_seen is False, guard stays out (other error handling owns it).
    noterm = _write_rollout(tmp / "n.jsonl", [_rollout_line("agent_message", message="hi")])
    res = _finalize_like_run(prompt="x", rollout_path=noterm)
    _check("3c: no terminal state -> not a ghost success", res["success"] is False and res["error"] is None)


def _main() -> int:
    with tempfile.TemporaryDirectory(prefix="bc-codex-ghost-") as td:
        tmp = Path(td)
        print("Test 1 — _rollout_terminal_state assistant_seen")
        test_rollout_assistant_seen(tmp / "1")
        print("Test 1b — cumulative rollout preamble (offset scan)")
        test_rollout_cumulative_preamble(tmp / "1b")
        print("Test 2 — guard fails ghost, passes legit")
        test_guard_fails_ghost_and_passes_legit(tmp / "2")
        print("Test 2b — parent final phase")
        test_parent_final_phase(tmp / "2b")
        print("Test 3 — guard narrowness")
        test_guard_narrowness(tmp / "3")
        print("Test 4 — network-retry attempt isolation")
        test_retry_attempt_isolation(tmp / "4")
        print("Test 5 — resumed-session cumulative usage isolation")
        test_resumed_cumulative_usage(tmp / "5")

    failed = [r for r in _results if not r[1]]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
