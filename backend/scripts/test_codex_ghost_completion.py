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
        print("Test 3 — guard narrowness")
        test_guard_narrowness(tmp / "3")
        print("Test 4 — network-retry attempt isolation")
        test_retry_attempt_isolation(tmp / "4")

    failed = [r for r in _results if not r[1]]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
