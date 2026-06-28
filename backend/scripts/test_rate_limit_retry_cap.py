"""Regression: a persistent rate-limit (Sakana "Subscription window is
exceeded") TERMINATES at the rate-limit retry cap, not sleep-loops forever.

Pre-fix the rate-limit branch in turn_manager had no cap → a dead
subscription retried indefinitely. Post-fix it's bounded by
_RATE_LIMIT_MAX_ATTEMPTS; once exhausted the turn fails closed with the
real 429.

Keeps the real `_drive_cli_run` (which owns the retry loop) and fakes only
the event SOURCE: `provider.start_run` pushes one synthetic `complete`
StreamEvent(success=False, error=429) per attempt; `is_running` returns
False. The real drain loop picks it up → the real retry branch fires → the
real cap bounds it. Wrapped in asyncio.wait_for so a loop-forever
regression times out instead of hanging the suite.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-rate-limit-cap-")
os.environ["BETTER_CLAUDE_TEST_AUTH_BYPASS"] = "1"

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import main  # noqa: E402
import turn_manager  # noqa: E402
import session_manager as _sm_mod  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402
from provider import StreamEvent  # noqa: E402

_sm_mod.PERSIST_DEBOUNCE_S = 0.0

_ERR = 'Runtime 429: {"error":{"message":"Subscription window is exceeded"}}'


async def _noop_ws(event: dict) -> None:
    return None


class _RateLimitProvider:
    """Always-429 provider. Each start_run pushes one synthetic complete
    event (success=False, error=429) onto the queue; is_running is False so
    the drain loop exits after consuming it."""

    supports_rewind = False
    supports_semantic_alter = False
    rewind_requires_agent_identity = False
    defunct = False
    KIND = "openai"
    _runs: dict = {}

    def __init__(self) -> None:
        self.start_calls = 0

    def is_running(self, run_id: str) -> bool:
        return False

    def parse_rate_limit(self, error, events):
        from datetime import datetime, timedelta, timezone
        # reset ~now → orchestrator wait floor (5s) keeps each retry short.
        return datetime.now(timezone.utc)

    def start_run(self, *, run_id, loop, queue, **kw):
        self.start_calls += 1
        loop.call_soon_threadsafe(
            queue.put_nowait,
            StreamEvent("complete", {
                "success": False, "error": _ERR,
                "session_id": None, "token_usage": None,
            }),
        )


def test_persistent_rate_limit_terminates_at_cap() -> bool:
    shutil.rmtree(os.path.join(_TMP_HOME, "sessions"), ignore_errors=True)
    cap = 2
    provider = _RateLimitProvider()

    original_cap = turn_manager._RATE_LIMIT_MAX_ATTEMPTS
    original_provider_for = main.coordinator.provider_for_session
    turn_manager._RATE_LIMIT_MAX_ATTEMPTS = cap
    main.coordinator.provider_for_session = lambda _sid: provider

    root = session_manager.create(name="rl-cap", cwd="/tmp")
    sid = root["id"]

    try:
        asyncio.run(asyncio.wait_for(
            main.coordinator.turn_manager.run_turn(
                session=session_manager.get(sid),
                prompt="hi", cli_prompt="hi", app_session_id=sid,
                model="m", cwd="/tmp", ws_callback=_noop_ws, images=None,
                trace_step_name="test", session_id_field="agent_session_id",
                mode="native",
            ),
            timeout=90.0,
        ))
    except asyncio.TimeoutError:
        print("\033[31mFAIL\033[0m rate-limit retry looped forever (no cap)")
        return False
    finally:
        turn_manager._RATE_LIMIT_MAX_ATTEMPTS = original_cap
        main.coordinator.provider_for_session = original_provider_for

    # cap retries + 1 terminal attempt. Bounded, not unbounded.
    ok = provider.start_calls == cap + 1
    print(
        f"\033[32m{('PASS','FAIL')[not ok]}\033[0m "
        f"persistent 429 terminated at cap (start_run calls={provider.start_calls}, "
        f"expected {cap + 1})"
    )
    return ok


if __name__ == "__main__":
    try:
        ok = test_persistent_rate_limit_terminates_at_cap()
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
    raise SystemExit(0 if ok else 1)
