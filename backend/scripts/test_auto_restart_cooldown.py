"""Regression tests for backend/auto_restart_cooldown.py.

RCA: `auto_restart_on_idle` fired 13+ times in 34 minutes because each
restart respawns a brand-new process with no memory of when the previous
process fired — there was no cross-process cooldown. This locks the
persisted-backoff arithmetic that closes that gap.

Run with:
    cd backend && .venv/bin/python scripts/test_auto_restart_cooldown.py
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-auto-restart-cooldown-")

import auto_restart_cooldown  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"

_results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, ok, detail))


def _reset_state() -> None:
    path = auto_restart_cooldown._state_path()
    if path.exists():
        path.unlink()


def main() -> int:
    _reset_state()

    # 1. No prior fire -> cooldown is zero (allowed immediately).
    check(
        "no cooldown before any fire",
        auto_restart_cooldown.restart_cooldown_remaining_seconds() == 0.0,
        "expected 0.0 remaining with no persisted state",
    )

    clock = [1_000_000.0]
    orig_time_time = auto_restart_cooldown.time.time
    try:
        auto_restart_cooldown.time.time = lambda: clock[0]  # type: ignore[attr-defined]

        # 2. Immediately after firing, remaining cooldown equals the base window.
        auto_restart_cooldown.record_restart_fired()
        remaining = auto_restart_cooldown.restart_cooldown_remaining_seconds()
        check(
            "cooldown active immediately after fire",
            remaining == auto_restart_cooldown.BASE_COOLDOWN_SECONDS,
            f"remaining={remaining}",
        )

        # 3. After the base cooldown elapses, a fresh process reads it as expired.
        clock[0] += auto_restart_cooldown.BASE_COOLDOWN_SECONDS + 1
        check(
            "cooldown expires after base window",
            auto_restart_cooldown.restart_cooldown_remaining_seconds() == 0.0,
            f"remaining={auto_restart_cooldown.restart_cooldown_remaining_seconds()}",
        )

        # 4. A second fire shortly after the first (within the fast-repeat
        #    window) doubles the required cooldown -- this is the restart-storm
        #    guard: repeated fast firing backs off instead of repeating at a
        #    fixed 5-minute cadence.
        _reset_state()
        clock[0] = 2_000_000.0
        auto_restart_cooldown.record_restart_fired()  # consecutive=0 -> base cooldown
        clock[0] += 60.0  # well within BACKOFF_RESET_SECONDS of the last fire
        auto_restart_cooldown.record_restart_fired()  # consecutive=1 -> 2x cooldown
        remaining = auto_restart_cooldown.restart_cooldown_remaining_seconds()
        check(
            "fast repeat doubles cooldown",
            remaining == auto_restart_cooldown.BASE_COOLDOWN_SECONDS * 2,
            f"remaining={remaining}",
        )

        # 5. Backoff is capped at MAX_COOLDOWN_SECONDS even with many fast repeats.
        for _ in range(10):
            clock[0] += 1.0
            auto_restart_cooldown.record_restart_fired()
        remaining = auto_restart_cooldown.restart_cooldown_remaining_seconds()
        check(
            "backoff caps at max cooldown",
            remaining == auto_restart_cooldown.MAX_COOLDOWN_SECONDS,
            f"remaining={remaining}",
        )

        # 6. Once a long gap (>= BACKOFF_RESET_SECONDS) passes since the last
        #    fire, the next fire resets back to the base cooldown instead of
        #    continuing to escalate -- a single healthy restart is never
        #    permanently throttled by an old incident.
        clock[0] += auto_restart_cooldown.BACKOFF_RESET_SECONDS + 1
        auto_restart_cooldown.record_restart_fired()
        remaining = auto_restart_cooldown.restart_cooldown_remaining_seconds()
        check(
            "backoff resets after a long healthy gap",
            remaining == auto_restart_cooldown.BASE_COOLDOWN_SECONDS,
            f"remaining={remaining}",
        )
    finally:
        auto_restart_cooldown.time.time = orig_time_time  # type: ignore[attr-defined]
        _reset_state()

    all_ok = True
    for name, ok, detail in _results:
        status = PASS if ok else FAIL
        suffix = f" ({detail})" if detail and not ok else ""
        print(f"{status}  {name}{suffix}")
        all_ok = all_ok and ok
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
