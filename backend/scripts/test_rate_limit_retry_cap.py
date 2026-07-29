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

import turn_manager  # noqa: E402
import installation_profile  # noqa: E402
import session_manager as _sm_mod  # noqa: E402
import session_queue_projection  # noqa: E402
from orchestrator import Coordinator  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402
from provider import Provider, StreamEvent, prepare_execution  # noqa: E402
from provider_agy import AgyProvider  # noqa: E402
from provider_claude import ClaudeProvider  # noqa: E402
from provider_codex import CodexProvider  # noqa: E402
from lifecycle_command_states import LifecycleCommandRejected  # noqa: E402
from lifecycle_command_model import UserTurnIdentity  # noqa: E402

_sm_mod.PERSIST_DEBOUNCE_S = 0.0
installation_profile.integrations_enabled = lambda: True
installation_profile.assert_orchestration_mode_allowed = lambda _mode: None
session_queue_projection.note_persisted_tree = lambda *_args, **_kwargs: 0
session_manager.flush_root_persist = lambda _root_id: None

_ERR = 'Runtime 429: {"error":{"message":"Subscription window is exceeded"}}'
_coordinator: Coordinator


async def _noop_ws(event: dict) -> None:
    return None


async def _run_turn_with_lifecycle(*, sid: str, lifecycle_id: str):
    await _coordinator.lifecycle_commands.begin_turn(
        request_id=f"{lifecycle_id}:begin",
        session_id=sid,
        identity=UserTurnIdentity(lifecycle_id, lifecycle_id),
        execution_policy="sequential",
    )
    manager = _coordinator.user_prompt_manager
    original = manager.get_in_flight_lifecycle_msg_id
    manager.get_in_flight_lifecycle_msg_id = lambda _sid: lifecycle_id
    try:
        return await _coordinator.turn_manager.run_turn(
            session=session_manager.get(sid),
            prompt="hi", cli_prompt="hi", app_session_id=sid,
            model="m", cwd="/tmp", ws_callback=_noop_ws, images=None,
            trace_step_name="test", session_id_field="agent_session_id",
            mode="native",
        )
    finally:
        manager.get_in_flight_lifecycle_msg_id = original


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

    def __init__(self, kind: str = "openai", *, fail_count: int | None = None) -> None:
        self.id = f"retry-{kind}"
        self.record = {"name": self.id}
        self.KIND = kind
        self.start_calls = 0
        self.fail_count = fail_count
        self.prepared: list[dict] = []

    def is_running(self, run_id: str) -> bool:
        return False

    def parse_rate_limit(self, error, events):
        from datetime import datetime, timedelta, timezone
        # reset ~now → orchestrator wait floor (5s) keeps each retry short.
        return datetime.now(timezone.utc)

    def prepare_run(self, **arguments):
        self.prepared.append(dict(arguments))
        return prepare_execution(
            {
                "id": f"retry-{self.KIND}",
                "kind": self.KIND,
                "generation": "00000000-0000-4000-8000-000000000001",
                "revision": 1,
            },
            **arguments,
        )

    def start_run(self, *, execution, loop, queue):
        self.start_calls += 1
        execution._try_commit_spawn()
        execution._mark_spawn_completed()
        failed = self.fail_count is None or self.start_calls <= self.fail_count
        loop.call_soon_threadsafe(
            queue.put_nowait,
            StreamEvent("complete", {
                "success": not failed, "error": _ERR if failed else None,
                "session_id": None, "token_usage": None,
            }),
        )
        return True


async def test_persistent_rate_limit_terminates_at_cap() -> bool:
    shutil.rmtree(os.path.join(_TMP_HOME, "sessions"), ignore_errors=True)
    cap = 2
    provider = _RateLimitProvider()

    original_cap = turn_manager._RATE_LIMIT_MAX_ATTEMPTS
    original_provider_for = _coordinator.provider_for_session
    turn_manager._RATE_LIMIT_MAX_ATTEMPTS = cap
    _coordinator.provider_for_session = lambda _sid: provider

    root = session_manager.create(name="rl-cap", cwd="/tmp")
    sid = root["id"]

    try:
        await asyncio.wait_for(
            _run_turn_with_lifecycle(sid=sid, lifecycle_id="rl-cap-user"),
            timeout=90.0,
        )
    except asyncio.TimeoutError:
        print("\033[31mFAIL\033[0m rate-limit retry looped forever (no cap)")
        return False
    finally:
        turn_manager._RATE_LIMIT_MAX_ATTEMPTS = original_cap
        _coordinator.provider_for_session = original_provider_for

    # cap retries + 1 terminal attempt. Bounded, not unbounded.
    ok = provider.start_calls == cap + 1
    print(
        f"\033[32m{('PASS','FAIL')[not ok]}\033[0m "
        f"persistent 429 terminated at cap (start_run calls={provider.start_calls}, "
        f"expected {cap + 1})"
    )
    return ok


async def test_outer_retry_identity_parity() -> bool:
    for provider_type in (ClaudeProvider, CodexProvider, AgyProvider):
        assert provider_type.start_run is Provider.start_run
    original_provider_for = _coordinator.provider_for_session
    original_wait = turn_manager._rate_limit_wait_seconds
    original_elect = _coordinator.lifecycle_commands.elect_execution_attempt
    turn_manager._rate_limit_wait_seconds = lambda _reset: 0.0
    try:
        for kind in ("claude", "codex", "agy"):
            shutil.rmtree(os.path.join(_TMP_HOME, "sessions"), ignore_errors=True)
            provider = _RateLimitProvider(kind, fail_count=1)
            _coordinator.provider_for_session = lambda _sid, p=provider: p
            elections: list[tuple[object, str]] = []

            async def elect(session_id, *, execution_identity, provider_run_id):
                previous_run_id = elections[-1][1] if elections else None
                result = await original_elect(
                    session_id,
                    execution_identity=execution_identity,
                    provider_run_id=provider_run_id,
                )
                elections.append((execution_identity, provider_run_id))
                if previous_run_id is not None:
                    try:
                        await _coordinator.lifecycle_commands.finish_execution(
                            session_id,
                            execution_identity=execution_identity,
                            provider_run_id=previous_run_id,
                            outcome="failed",
                        )
                    except LifecycleCommandRejected:
                        pass
                    else:
                        raise AssertionError("stale outer attempt terminal was accepted")
                return result

            _coordinator.lifecycle_commands.elect_execution_attempt = elect
            root = session_manager.create(name=f"retry-{kind}", cwd="/tmp")
            sid = root["id"]
            await asyncio.wait_for(
                _run_turn_with_lifecycle(
                    sid=sid,
                    lifecycle_id=f"retry-{kind}-user",
                ),
                timeout=30.0,
            )
            assert len(provider.prepared) == 2
            run_ids = [item["run_id"] for item in provider.prepared]
            assert run_ids[0] != run_ids[1]
            assert [entry[1] for entry in elections] == run_ids
            assert elections[0][0] == elections[1][0]
    finally:
        _coordinator.provider_for_session = original_provider_for
        _coordinator.lifecycle_commands.elect_execution_attempt = original_elect
        turn_manager._rate_limit_wait_seconds = original_wait
    return True


async def run_tests() -> bool:
    global _coordinator
    _coordinator = Coordinator()
    await _coordinator.turn_manager.lifecycle.bind()
    await _coordinator.lifecycle_commands.bind()
    try:
        return (
            await test_persistent_rate_limit_terminates_at_cap()
            and await test_outer_retry_identity_parity()
        )
    finally:
        await _coordinator.quiesce_prompt_processors()
        await _coordinator.lifecycle_commands.close()
        await _coordinator.turn_manager.lifecycle.close()


if __name__ == "__main__":
    try:
        ok = asyncio.run(run_tests())
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
    raise SystemExit(0 if ok else 1)
