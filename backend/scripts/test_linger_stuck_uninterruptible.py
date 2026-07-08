"""Regression: a detached descendant stuck in an UNINTERRUPTIBLE kernel
wait (ps state U/D) must not pin the babysitter runner — and with it the
claude CLI + its MCP pool — forever.

Incident: a `run_in_background` shell whose child entered uninterruptible
disk wait (state U) kept `_linger_for_background_work` alive indefinitely.
SIGKILL can't reap a U-state process until the kernel call returns, and a
call against a hung mount may never return — so `has_detached_descendants`
stayed True forever and the only escape was the user's manual cancel.

The deterministic fix: a sustained U/D descendant is provably not
background work (a legit shell or `sleep` is in interruptible S, never
U/D; transient I/O-D clears between probes). After a bounded window the
linger sweeps the detached groups and exits, freeing claude + MCP.

Run:
    cd backend && .venv/bin/python scripts/test_linger_stuck_uninterruptible.py
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import _test_home  # noqa: E402

_TMP_HOME = _test_home.isolate("bc-test-linger-stuck-")

import proc_control  # noqa: E402
import runner  # noqa: E402

PASS = "  PASS"
FAIL = "  FAIL"


class _StuckUninterruptiblePC:
    """A detached descendant that is ALWAYS in uninterruptible wait — the
    pathological case. `has_detached_descendants` staying True forever
    must NOT keep the linger alive once the stuck window elapses."""

    def __init__(self) -> None:
        self.swept = 0

    def has_detached_descendants(self, *_a, **_k) -> bool:
        return True

    def has_uninterruptible_detached_descendant(self, *_a, **_k):
        return True

    def kill_detached_descendant_groups(self, *_a, **_k) -> int:
        self.swept += 1
        return 1


class _RunnableSleeperPC:
    """A detached descendant in INTERRUPTIBLE sleep (state S) — e.g. a
    legit `sleep N` bg shell. The stuck sweep must NOT fire: this is
    real (if idle) background work and must keep the babysitter alive
    until the shell exits on its own."""

    def __init__(self, busy_polls: int = 6) -> None:
        self._remaining = busy_polls
        self.swept = 0

    def has_detached_descendants(self, *_a, **_k) -> bool:
        if self._remaining > 0:
            self._remaining -= 1
            return True
        return False

    def has_uninterruptible_detached_descendant(self, *_a, **_k):
        return False

    def kill_detached_descendant_groups(self, *_a, **_k) -> int:
        self.swept += 1
        return 0


class _Log:
    def info(self, *_a, **_k) -> None: ...
    def warning(self, *_a, **_k) -> None: ...
    def exception(self, *_a, **_k) -> None: ...


async def _run_case() -> list[tuple[str, bool, str]]:
    results: list[tuple[str, bool, str]] = []
    original_pc = proc_control.process_control
    original_window = runner._DETACHED_UNINTERRUPTIBLE_STUCK_S
    runner._DETACHED_UNINTERRUPTIBLE_STUCK_S = 0.1
    try:
        # Case 1: sustained uninterruptible descendant → sweep + exit.
        proc_control.process_control = lambda: _stuck_pc  # type: ignore[assignment]
        _stuck_pc = _StuckUninterruptiblePC()
        stuck_dir = Path(_TMP_HOME) / "stuck"
        stuck_dir.mkdir()
        start = time.monotonic()
        await runner._linger_for_background_work(
            stuck_dir,
            _Log(),
            client=None,
            outstanding_tasks=set(),
            poll_interval_s=0.02,
        )
        elapsed = time.monotonic() - start
        results.append((
            "sustained uninterruptible descendant is swept and frees the linger",
            _stuck_pc.swept >= 1 and elapsed < 2.0,
            f"swept={_stuck_pc.swept} elapsed={elapsed:.2f}s",
        ))

        # Case 2: interruptible sleeper → NOT swept; exits via normal path.
        proc_control.process_control = lambda: _sleeper_pc  # type: ignore[assignment]
        _sleeper_pc = _RunnableSleeperPC(busy_polls=6)
        sleeper_dir = Path(_TMP_HOME) / "sleeper"
        sleeper_dir.mkdir()
        start = time.monotonic()
        await runner._linger_for_background_work(
            sleeper_dir,
            _Log(),
            client=None,
            outstanding_tasks=set(),
            poll_interval_s=0.02,
        )
        elapsed = time.monotonic() - start
        results.append((
            "interruptible (S) sleeper is NOT swept — kept alive until it exits",
            _sleeper_pc.swept == 0 and elapsed < 2.0,
            f"swept={_sleeper_pc.swept} elapsed={elapsed:.2f}s",
        ))
    finally:
        proc_control.process_control = original_pc  # type: ignore[assignment]
        runner._DETACHED_UNINTERRUPTIBLE_STUCK_S = original_window
    return results


def main() -> int:
    results = asyncio.run(_run_case())
    failed = 0
    for name, ok, detail in results:
        print(f"{'  PASS' if ok else '  FAIL'}  {name}  ({detail})")
        if not ok:
            failed += 1
    print(f"\n{len(results) - failed}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
