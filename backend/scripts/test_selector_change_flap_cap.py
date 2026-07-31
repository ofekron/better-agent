"""Regression: the "loop of switching" bug.

turn_manager's drive loop restarts the provider subprocess whenever
`session.model`/`provider_id`/`runner` drifts from the last-spawned
subprocess's identity (`_should_preempt_selector_change_continuation`).
Every OTHER retry branch in the same loop that can revisit the top
repeatedly (rate-limit, transient-error) is bounded by an attempt cap; this
branch had none. When an external writer keeps flipping `session.model`
(e.g. two clients racing `PATCH /selectors` on the same session) faster
than a fresh subprocess's sid-discovery can re-sync `last_active_model`,
each "back to top" revisit (driven by any of the loop's other retry
mechanisms) piled on an EXTRA, unbounded selector-change restart —
producing an unbounded stream of "Model switched" events with the turn
stuck at "Running...".

Uses the real rate-limit retry branch (capped generously high here, so it
never itself becomes the limiting factor) purely as the "back to top"
driver, with a provider that flips `session.model` on every attempt. Counts
the selector-change restart log line directly (turn_manager logs one INFO
per restart) rather than total provider spawns, because total spawns are
NOT a clean signal here: a preemption-driven respawn and a genuine
rate-limit retry both increment the same counter, so gating success on a
spawn-count threshold would make extra preemptions reach that threshold
*sooner*, masking the bug. Counting the restart log line measures exactly
what the user sees (one "Model switched" event per restart), independent
of that confound.

Run with:
    cd backend && .venv/bin/python scripts/test_selector_change_flap_cap.py
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
from datetime import datetime, timezone

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-selector-flap-")
os.environ["BETTER_CLAUDE_TEST_AUTH_BYPASS"] = "1"

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import turn_manager  # noqa: E402
import installation_profile  # noqa: E402
import session_manager as _sm_mod  # noqa: E402
import session_queue_projection  # noqa: E402
import config_store  # noqa: E402
from orchestrator import Coordinator  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402
from provider import StreamEvent, prepare_execution  # noqa: E402
from lifecycle_command_model import UserTurnIdentity  # noqa: E402

_sm_mod.PERSIST_DEBOUNCE_S = 0.0
installation_profile.integrations_enabled = lambda: True
installation_profile.assert_orchestration_mode_allowed = lambda _mode: None
session_queue_projection.note_persisted_tree = lambda *_args, **_kwargs: 0
session_manager.flush_root_persist = lambda _root_id: None

_MODEL_A = "claude-opus-5"
_MODEL_B = "claude-sonnet-5"
_RESTART_MARKER = "preempting due to provider/model change"
_coordinator: Coordinator


class _RestartCounter(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.count = 0

    def emit(self, record: logging.LogRecord) -> None:
        if _RESTART_MARKER in record.getMessage():
            self.count += 1


async def _noop_ws(_event: dict) -> None:
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
            prompt="continue", cli_prompt="continue", app_session_id=sid,
            model=_MODEL_A, cwd="/tmp", ws_callback=_noop_ws, images=None,
            trace_step_name="test", session_id_field="agent_session_id",
            mode="native",
        )
    finally:
        manager.get_in_flight_lifecycle_msg_id = original


class _FlappingModelProvider:
    """Every attempt fails with a (near-instant) rate limit so the real
    rate-limit branch drives repeated top-of-loop revisits. Each attempt
    also reports a fresh discovered sid (stamping `last_active_model` to
    whatever model was active when the attempt STARTED), then immediately
    flips `session.model` to the other of two values — guaranteeing the
    next revisit's selector-change drift check mismatches."""

    supports_rewind = False
    supports_semantic_alter = False
    rewind_requires_agent_identity = False
    defunct = False
    KIND = "claude"
    _runs: dict = {}

    def __init__(self, sid: str, provider_id: str, *, fail_count: int) -> None:
        self.id = provider_id
        self.record = {"name": self.id}
        self.sid = sid
        self.fail_count = fail_count
        self.start_calls = 0

    def is_running(self, run_id: str) -> bool:
        return False

    def parse_rate_limit(self, error, events):
        return datetime.now(timezone.utc)

    def prepare_run(self, **arguments):
        return prepare_execution(
            {
                "id": self.id,
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
        fresh_sid = f"disc-{self.start_calls}"
        failed = self.start_calls <= self.fail_count

        loop.call_soon_threadsafe(
            queue.put_nowait,
            StreamEvent("session_discovered", {"session_id": fresh_sid}),
        )
        current = session_manager.get(self.sid) or {}
        flipped = _MODEL_B if current.get("model") == _MODEL_A else _MODEL_A
        session_manager.set_selectors(self.sid, model=flipped)

        loop.call_soon_threadsafe(
            queue.put_nowait,
            StreamEvent("complete", {
                "success": not failed,
                "error": "rate_limit" if failed else None,
                "session_id": fresh_sid,
                "token_usage": None if failed else {"input_tokens": 1},
            }),
        )
        return True


async def test_selector_change_flapping_is_bounded() -> bool:
    shutil.rmtree(os.path.join(_TMP_HOME, "sessions"), ignore_errors=True)

    selector_cap = 2
    rate_limit_rounds = 10  # drive-loop revisits from the (generously capped) rate-limit branch

    original_selector_cap = turn_manager._SELECTOR_CHANGE_MAX_ATTEMPTS
    original_rate_limit_cap = turn_manager._RATE_LIMIT_MAX_ATTEMPTS
    original_provider_for_session = _coordinator.provider_for_session
    original_provider_for_run = _coordinator.provider_for_run
    turn_manager._SELECTOR_CHANGE_MAX_ATTEMPTS = selector_cap
    turn_manager._RATE_LIMIT_MAX_ATTEMPTS = rate_limit_rounds + 5

    provider_id = config_store.get_default_provider()["id"]
    root = session_manager.create(
        name="selector-flap", cwd="/tmp", model=_MODEL_A, provider_id=provider_id,
    )
    sid = root["id"]
    # Seed last_active_* to match the session's OWN starting selectors so the
    # only mismatches that occur are the ones this test deliberately creates.
    session_manager.set_agent_sid(
        sid, "native", "seed-sid", provider_id=provider_id, model=_MODEL_A,
    )

    provider = _FlappingModelProvider(sid, provider_id, fail_count=rate_limit_rounds)
    _coordinator.provider_for_session = lambda _sid: provider
    _coordinator.provider_for_run = lambda *_args, **_kwargs: provider

    restart_counter = _RestartCounter()
    original_logger_level = turn_manager.logger.level
    # The restart line logs at INFO; the app's ambient logging config (root
    # at WARNING) would otherwise suppress it before it reaches the handler.
    turn_manager.logger.setLevel(logging.INFO)
    turn_manager.logger.addHandler(restart_counter)
    try:
        await asyncio.wait_for(
            _run_turn_with_lifecycle(sid=sid, lifecycle_id="selector-flap-user"),
            timeout=90.0,
        )
    except asyncio.TimeoutError:
        print("\033[31mFAIL\033[0m selector-change flapping looped forever (no cap)")
        return False
    finally:
        turn_manager.logger.removeHandler(restart_counter)
        turn_manager.logger.setLevel(original_logger_level)
        turn_manager._SELECTOR_CHANGE_MAX_ATTEMPTS = original_selector_cap
        turn_manager._RATE_LIMIT_MAX_ATTEMPTS = original_rate_limit_cap
        _coordinator.provider_for_session = original_provider_for_session
        _coordinator.provider_for_run = original_provider_for_run

    # session.model flips on every one of the `rate_limit_rounds` rounds, so
    # an uncapped loop would fire one selector-change restart per round
    # (restart_counter.count == rate_limit_rounds). Capped, it must stop at
    # `_SELECTOR_CHANGE_MAX_ATTEMPTS` regardless of how many rounds occur.
    ok = restart_counter.count == selector_cap
    print(
        f"\033[32m{('PASS','FAIL')[not ok]}\033[0m "
        f"selector-change restarts capped at {selector_cap} "
        f"(observed={restart_counter.count}, rounds={rate_limit_rounds})"
    )
    return ok


async def run_tests() -> bool:
    global _coordinator
    _coordinator = Coordinator()
    await _coordinator.turn_manager.lifecycle.bind()
    await _coordinator.lifecycle_commands.bind()
    try:
        return await test_selector_change_flapping_is_bounded()
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
