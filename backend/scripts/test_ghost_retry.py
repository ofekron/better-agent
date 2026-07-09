"""Regression tests for the bounded live-retry on prompt_not_executed.

Bug: codex-cli (and the Gemini CLI) intermittently swallow an empty/failed
upstream response as a successful zero-usage turn — a "ghost completion"
flagged by ``apply_ghost_completion_guard`` as ``prompt_not_executed``. The
guard detected it but the runner's LIVE path never retried it, so each ghost
was a hard user-facing failure even though a resend usually succeeds.

Fix: ``runner_guard.should_retry_ghost`` decides (SSOT, shared by the Codex
and Gemini runners) whether to retry a ghost; the runners retry up to
``GHOST_RETRY_MAX`` times before failing closed.

Run with:
    cd backend && .venv/bin/python scripts/test_ghost_retry.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home  # noqa: E402
_TMP_HOME = _test_home.isolate("bc-test-ghost-retry-")  # noqa: E402

import runner_guard  # noqa: E402
from runner_guard import (  # noqa: E402
    GHOST_RETRY_MAX,
    apply_ghost_completion_guard,
    should_retry_ghost,
)

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"

_results: list[tuple[str, bool, str]] = []


def _check(name: str, cond: bool, detail: str = "") -> None:
    _results.append((name, bool(cond), detail))
    print(f"  {PASS if cond else FAIL} {name}" + (f" — {detail}" if detail and not cond else ""))


def _ghost_guard_result(prompt: str = "do the thing") -> tuple[bool, str]:
    """Mirror a finalized ghost turn: success + result_seen, zero usage,
    no assistant output -> apply_ghost_completion_guard flips it."""
    success, error = apply_ghost_completion_guard(
        success=True,
        cancelled=False,
        error=None,
        prompt=prompt,
        assistant_seen=False,
        total_usage={},
        result_seen=True,
    )
    return success, error


def test_guard_still_flags_ghost() -> None:
    """Precondition: the ghost guard still produces prompt_not_executed for
    a zero-usage empty success — retry builds on top of this."""
    success, error = _ghost_guard_result()
    _check("1a: ghost guard returns prompt_not_executed", error == "prompt_not_executed")
    _check("1b: ghost guard flips success to False", success is False)


def test_should_retry_bounds() -> None:
    """should_retry_ghost is True for a fresh ghost, False once the budget
    is exhausted, and ignores non-ghost errors and cancels."""
    _check("2a: first ghost retries", should_retry_ghost(
        "prompt_not_executed", cancelled=False, attempts=0) is True)
    _check("2b: last allowed retry at attempts=MAX-1", should_retry_ghost(
        "prompt_not_executed", cancelled=False, attempts=GHOST_RETRY_MAX - 1) is True)
    _check("2c: exhausted at attempts=MAX", should_retry_ghost(
        "prompt_not_executed", cancelled=False, attempts=GHOST_RETRY_MAX) is False)
    _check("2d: cancelled never retries", should_retry_ghost(
        "prompt_not_executed", cancelled=True, attempts=0) is False)
    _check("2e: non-ghost error never retries", should_retry_ghost(
        "some other error", cancelled=False, attempts=0) is False)
    _check("2f: None error never retries", should_retry_ghost(
        None, cancelled=False, attempts=0) is False)


def _simulate_runner_loop(attempt_results: list[str]) -> dict:
    """Drive the exact retry-control-flow the runners use: each attempt
    yields a finalized (success, error); a ghost (prompt_not_executed)
    retries while should_retry_ghost says so; any other outcome ends the
    turn. Returns the final error and how many ghost retries ran.

    attempt_results: per-attempt outcome tags — "ghost" or "ok"."""
    ghost_retries = 0
    final_error = None
    final_success = False
    for outcome in attempt_results:
        if outcome == "ok":
            final_success, final_error = True, None
            break
        # ghost: finalize like the runner does, then consult the helper.
        _, final_error = _ghost_guard_result()
        if should_retry_ghost(final_error, cancelled=False, attempts=ghost_retries):
            ghost_retries += 1
            continue
        # Budget exhausted (or non-retryable): turn fails here.
        final_success = False
        break
    return {
        "success": final_success,
        "error": final_error,
        "ghost_retries": ghost_retries,
    }


def test_retry_then_success() -> None:
    """Ghost, ghost, then ok: retries twice and the turn succeeds — the
    observed codex behavior where a resend works."""
    res = _simulate_runner_loop(["ghost", "ghost", "ok"])
    _check("3a: two ghosts retried", res["ghost_retries"] == 2, str(res))
    _check("3b: turn succeeds after retry", res["success"] is True, str(res))
    _check("3c: no error on success", res["error"] is None, str(res))


def test_all_ghosts_exhaust_and_fail() -> None:
    """A turn that ghosts forever exhausts the budget and fails closed as
    prompt_not_executed — never an infinite loop."""
    # Feed more ghosts than any plausible budget to prove termination.
    outcomes = ["ghost"] * (GHOST_RETRY_MAX + 5)
    res = _simulate_runner_loop(outcomes)
    _check("4a: retries bounded at GHOST_RETRY_MAX",
           res["ghost_retries"] == GHOST_RETRY_MAX, str(res))
    _check("4b: turn fails closed", res["success"] is False, str(res))
    _check("4c: failure reason is prompt_not_executed",
           res["error"] == "prompt_not_executed", str(res))


def test_constants_exported() -> None:
    """SSOT: the retry bounds live on runner_guard and are positive ints."""
    _check("5a: GHOST_RETRY_MAX is a positive int",
           isinstance(GHOST_RETRY_MAX, int) and GHOST_RETRY_MAX >= 1)
    _check("5b: GHOST_RETRY_BACKOFF_S exported and positive",
           isinstance(runner_guard.GHOST_RETRY_BACKOFF_S, (int, float))
           and runner_guard.GHOST_RETRY_BACKOFF_S > 0)


def _main() -> int:
    print("Test 1 — ghost guard still flags prompt_not_executed")
    test_guard_still_flags_ghost()
    print("Test 2 — should_retry_ghost decision bounds")
    test_should_retry_bounds()
    print("Test 3 — ghost-then-success retries and succeeds")
    test_retry_then_success()
    print("Test 4 — all-ghost exhausts budget and fails closed")
    test_all_ghosts_exhaust_and_fail()
    print("Test 5 — SSOT constants exported")
    test_constants_exported()

    failed = [r for r in _results if not r[1]]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
