"""Regression tests for the babysitter-linger vs. new-prompt race.

Bug: while a Claude runner lingers for background work, its CLI still
runs continuation turns (task notifications). Spawning a second
--resume CLI on the SAME native session cross-process-enqueues the
prompt into the live instance and returns a ghost zero-token success
ResultMessage with no assistant output — the user message is orphaned
and the continuation output gets bound as the "reply".

Locks two fixes:

  A. `ClaudeProvider.start_run` serializes a new turn behind a live
     linger on the SAME native agent_session_id: cancels the linger,
     defers the spawn until the run's release event fires. Keyed on the
     native sid — a run on a different native sid (worker fork) with
     the same app_session_id is NOT serialized, and fork=True spawns
     are exempt.
  B. Runner ghost-completion guard: a zero-usage success ResultMessage
     with no assistant output for a non-empty prompt finalizes as
     success=False / error="prompt_not_executed" (retryable), while a
     legitimate turn with assistant output + usage stays success=True.

Run with:
    cd backend && .venv/bin/python scripts/test_linger_turn_serialization.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-linger-serialize-")

from provider_claude import ClaudeProvider  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"

_results: list[tuple[str, bool, str]] = []


def _check(name: str, cond: bool, detail: str = "") -> None:
    _results.append((name, bool(cond), detail))
    print(f"  {PASS if cond else FAIL} {name}" + (f" — {detail}" if detail and not cond else ""))


def _mk_provider() -> ClaudeProvider:
    return ClaudeProvider({"id": "test-provider", "mode": "subscription"})


def _mk_lingering_rs(tmp: Path, run_id: str, native_sid: str, app_sid: str):
    run_dir = tmp / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(
        run_id=run_id,
        run_dir=run_dir,
        session_id=native_sid,
        app_session_id=app_sid,
        lingering=True,
        released=asyncio.Event(),
        queue=asyncio.Queue(),
    )


def _start_run_kwargs(loop, queue, *, session_id, fork=False):
    return dict(
        run_id=f"new-run-{session_id}-{fork}",
        prompt="hello",
        cwd=".",
        loop=loop,
        queue=queue,
        model=None,
        reasoning_effort=None,
        session_id=session_id,
        mode="native",
        app_session_id="app-1",
        fork=fork,
    )


# ─── Test A — linger serialization gate ───────────────────────────

async def test_a_serializes_same_native_sid(tmp: Path) -> None:
    provider = _mk_provider()
    rs = _mk_lingering_rs(tmp, "linger-run-1", "sid-X", "app-1")
    provider._runs[rs.run_id] = rs

    calls: list[dict] = []
    provider._spawn_run = lambda **kw: calls.append(kw)

    loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue()
    provider.start_run(**_start_run_kwargs(loop, q, session_id="sid-X"))

    _check("A1: no second spawn while linger holds sid-X", len(calls) == 0)
    _check(
        "A2: lingering runner signalled to wind down (cancel sentinel)",
        (rs.run_dir / "cancel").exists(),
    )

    # Let the deferred waiter task start; it must still be blocked.
    for _ in range(5):
        await asyncio.sleep(0)
    _check("A3: spawn still deferred before release fires", len(calls) == 0)

    # Simulate the runner exiting: provider deregisters + fires release.
    provider._cleanup_run(rs.run_id)
    _check("A4: cleanup fires the release event", rs.released.is_set())

    for _ in range(20):
        if calls:
            break
        await asyncio.sleep(0.01)
    _check("A5: deferred prompt spawns after release", len(calls) == 1)
    if calls:
        _check("A6: deferred spawn kept its params", calls[0]["session_id"] == "sid-X" and calls[0]["prompt"] == "hello")


async def test_a_fork_and_other_sid_not_serialized(tmp: Path) -> None:
    provider = _mk_provider()
    rs = _mk_lingering_rs(tmp, "linger-run-2", "sid-X", "app-1")
    provider._runs[rs.run_id] = rs

    calls: list[dict] = []
    provider._spawn_run = lambda **kw: calls.append(kw)
    loop = asyncio.get_running_loop()

    # Worker fork: same app_session_id, DIFFERENT native agent_session_id.
    provider.start_run(**_start_run_kwargs(loop, asyncio.Queue(), session_id="sid-Y"))
    _check("A7: different native sid (same app sid) is NOT serialized", len(calls) == 1)

    # fork=True spawn on the lingering sid is exempt (new native session).
    provider.start_run(**_start_run_kwargs(loop, asyncio.Queue(), session_id="sid-X", fork=True))
    _check("A8: fork=True spawn is NOT serialized", len(calls) == 2)
    _check(
        "A9: non-blocking spawns did not cancel the linger",
        not (rs.run_dir / "cancel").exists(),
    )


# ─── Test B — ghost-completion guard ──────────────────────────────

def _mk_result_message(*, usage, result, is_error=False):
    from claude_agent_sdk import ResultMessage  # type: ignore
    msg = ResultMessage.__new__(ResultMessage)
    msg.__dict__.update(dict(
        subtype="success" if not is_error else "error",
        duration_ms=0,
        duration_api_ms=0,
        is_error=is_error,
        num_turns=1,
        session_id="sid-ghost",
        total_cost_usd=0.0,
        usage=usage,
        result=result,
        model_usage=None,
        stop_reason=None,
    ))
    return msg


def _mk_assistant_message(text: str, usage):
    from claude_agent_sdk import AssistantMessage  # type: ignore
    msg = AssistantMessage.__new__(AssistantMessage)
    msg.__dict__.update(dict(
        content=[{"type": "text", "text": text}],
        model="test-model",
        usage=usage,
        error=None,
        stop_reason=None,
        parent_tool_use_id=None,
    ))
    return msg


class _FakeClient:
    def __init__(self, messages):
        self._messages = list(messages)

    async def query(self, prompt):
        return None

    async def interrupt(self):
        return None

    def receive_response(self):
        msgs = self._messages

        async def _gen():
            for m in msgs:
                yield m

        return _gen()


async def _run_turn_with(messages, tmp: Path) -> dict:
    import runner
    run_dir = tmp / "runB"
    run_dir.mkdir(parents=True, exist_ok=True)
    return await runner._run_one_turn(
        client=_FakeClient(messages),
        prompt="do the thing",
        images=[],
        files=[],
        run_dir=run_dir,
        turn_id="turn-1",
        pre_query_byte_offset=0,
        state={},
        state_path=run_dir / "state.json",
        cwd=str(tmp),
        claude_config_dir=tmp / "claude-cfg",
        log=logging.getLogger("test-runner"),
    )


async def test_b_ghost_completion(tmp: Path) -> None:
    ghost = _mk_result_message(usage={"input_tokens": 0, "output_tokens": 0}, result="")
    res = await _run_turn_with([ghost], tmp)
    _check("B1: ghost zero-usage result is not success", res["success"] is False)
    _check("B2: ghost result flagged prompt_not_executed", res["error"] == "prompt_not_executed")
    _check("B3: ghost result final_success is False", res["final_success"] is False)

    # Narrowness control: a real turn (assistant output + usage) stays green.
    legit = [
        _mk_assistant_message("hi there", {"input_tokens": 10, "output_tokens": 4}),
        _mk_result_message(usage={"input_tokens": 10, "output_tokens": 4}, result="hi there"),
    ]
    res2 = await _run_turn_with(legit, tmp)
    _check("B4: legitimate turn still succeeds", res2["final_success"] is True, str(res2.get("error")))


async def _main() -> int:
    with tempfile.TemporaryDirectory(prefix="bc-linger-serialize-") as td:
        tmp = Path(td)
        print("Test A — linger serialization gate")
        await test_a_serializes_same_native_sid(tmp / "a1")
        await test_a_fork_and_other_sid_not_serialized(tmp / "a2")
        print("Test B — ghost-completion guard")
        await test_b_ghost_completion(tmp / "b")

    failed = [r for r in _results if not r[1]]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
