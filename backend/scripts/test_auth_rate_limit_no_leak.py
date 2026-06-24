"""Test backend/auth.py rate limiter — stale IP entries are pruned so the
attempt table can't grow without bound by source-IP rotation (a pre-auth
memory-DoS, trivially reachable over IPv6 where one host owns a /64).

Fails before the fix: pre-fix the table retains every distinct IP forever,
so `test_stale_ips_are_pruned` finds 1001 entries instead of 1.

Run with:
    cd backend && .venv/bin/python scripts/test_auth_rate_limit_no_leak.py
"""

from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile

# State-dir isolation BEFORE importing backend modules (project rule).
import _test_home
_BC_HOME = _test_home.isolate("bc-rltest-")
atexit.register(lambda: shutil.rmtree(_BC_HOME, ignore_errors=True))

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import auth  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


class _FakeClock:
    """Controllable monotonic clock — the limiter's window is 300s, far too
    long to wait out in a test, so we drive time directly."""

    def __init__(self, t: float) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def _reset_state(t: float) -> _FakeClock:
    clock = _FakeClock(t)
    auth.time.monotonic = clock
    auth._rl_attempts.clear()
    if hasattr(auth, "_rl_last_sweep"):
        auth._rl_last_sweep = 0.0
    return clock


def test_stale_ips_are_pruned() -> bool:
    """1000 distinct IPs each make one allowed attempt; after the window
    elapses, a single further request prunes all 1000 stale entries. Pre-fix
    the table retained every IP forever (this assertion would see 1001)."""
    orig = auth.time.monotonic
    try:
        clock = _reset_state(10_000.0)
        n = 1000
        for i in range(n):
            ip = f"203.0.{i // 256}.{i % 256}"
            if not auth.rate_limit_check(ip):
                print(f"  unexpected lock-out on first attempt from {ip}")
                return False
        if len(auth._rl_attempts) != n:
            print(f"  setup: expected {n} entries, got {len(auth._rl_attempts)}")
            return False
        # Advance well past the window (and the once-per-window sweep gate).
        clock.t += auth._RL_WINDOW * 3 + 1
        # A single further request must trigger the amortized prune.
        auth.rate_limit_check("198.51.100.7")
        remaining = len(auth._rl_attempts)
        if remaining != 1:
            print(f"  LEAK: {remaining} entries retained after window "
                  f"(expected 1 — only the fresh IP)")
            return False
        return True
    finally:
        auth.time.monotonic = orig


def test_active_limiter_still_blocks() -> bool:
    """Pruning must not weaken the limiter: an IP exceeding _RL_MAX within
    the window is still locked out, and its entry is retained while active."""
    orig = auth.time.monotonic
    try:
        clock = _reset_state(20_000.0)
        ip = "203.0.113.9"
        for _ in range(auth._RL_MAX):
            if not auth.rate_limit_check(ip):
                print("  locked out before reaching _RL_MAX")
                return False
        if auth.rate_limit_check(ip):
            print("  limiter did NOT block after _RL_MAX attempts")
            return False
        # Still within the window a moment later → still blocked, entry kept.
        clock.t += 100.0
        if auth.rate_limit_check(ip):
            print("  limiter stopped blocking within the window")
            return False
        if ip not in auth._rl_attempts:
            print("  active IP wrongly dropped within the window")
            return False
        return True
    finally:
        auth.time.monotonic = orig


def test_reset_clears_entry() -> bool:
    """rate_limit_reset still drops an IP's history (successful-login path)."""
    orig = auth.time.monotonic
    try:
        _reset_state(30_000.0)
        ip = "203.0.113.50"
        auth.rate_limit_check(ip)
        if ip not in auth._rl_attempts:
            print("  attempt was not recorded")
            return False
        auth.rate_limit_reset(ip)
        if ip in auth._rl_attempts:
            print("  reset did not clear the IP entry")
            return False
        return True
    finally:
        auth.time.monotonic = orig


TESTS = [
    ("stale IP entries are pruned after the window (no memory leak)",
     test_stale_ips_are_pruned),
    ("limiter still blocks after _RL_MAX and keeps active IPs",
     test_active_limiter_still_blocks),
    ("rate_limit_reset still clears an IP entry",
     test_reset_clears_entry),
]


def main_run() -> int:
    failed = 0
    for name, fn in TESTS:
        try:
            ok = fn()
        except Exception as e:
            ok = False
            import traceback
            traceback.print_exc()
            print(f"  exception: {e}")
        print(f"{PASS if ok else FAIL}  {name}")
        if not ok:
            failed += 1
    print()
    print(f"{failed} of {len(TESTS)} test(s) FAILED" if failed
          else f"all {len(TESTS)} tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main_run())
