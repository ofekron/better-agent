"""Locks the session-bridge fail-closed gating (INV-3/4/8) and search
fallback. Pure decision logic — no real CLI subprocess.

Run: python backend/scripts/test_session_bridge_gating.py
"""

import asyncio
import os
import sys
import tempfile
import types
from pathlib import Path

# State isolation FIRST (CLAUDE.md): fresh tempdir home before backend imports.
import _test_home
_TMP = _test_home.isolate("bc-sbtest-")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import session_bridge  # noqa: E402
import coordination  # noqa: E402
import session_search  # noqa: E402
import session_store  # noqa: E402
import user_prefs  # noqa: E402
import config_store  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402
from stores import worker_store  # noqa: E402
from event_bus import BusEvent, bus  # noqa: E402

_FAILURES: list[str] = []
_ORIG_RUN = session_bridge._run
_ORIG_RUN_TURN = session_bridge._run_turn
_ORIG_SM_GET = session_manager.get


def check(cond: bool, msg: str):
    if not cond:
        _FAILURES.append(msg)
        print(f"  FAIL: {msg}")
    else:
        print(f"  ok:   {msg}")


# ── Harness: stub the listable-session index, the run, and the picker ────

_KNOWN = {"sess-A": {"id": "sess-A", "name": "A", "cwd": "/x",
                     "project_name": "p", "first_user_prompt": "hi"}}


def _install_stubs(*, picker_returns, caller_in_flight=True, target_busy=False):
    calls = {"run": [], "picker": []}

    session_search.index_stub_map = lambda: dict(_KNOWN)  # type: ignore
    session_bridge._caller_in_flight_msg_id = (  # type: ignore
        lambda sid: "msg-1" if caller_in_flight else None
    )
    session_bridge._target_busy = lambda sid: target_busy  # type: ignore

    async def _fake_run(target_sid, prompt, run_mode, **kwargs):
        calls["run"].append((target_sid, prompt, run_mode, kwargs))
        return {"session_id": target_sid, "run_mode": run_mode,
                "final_message": "done", "turn_id": "t1"}

    async def _fake_picker(caller_sid, caller_msg_id, target_sid, prompt, run_mode, **_kwargs):
        calls["picker"].append((caller_sid, caller_msg_id, target_sid, prompt, run_mode))
        return picker_returns

    session_bridge._run = _fake_run  # type: ignore
    session_bridge._await_picker = _fake_picker  # type: ignore
    return calls


async def _run_tests():
    # 0. session_bridge.session_manager must be the manager INSTANCE (which
    #    has get/fork/set_msg_ask_result), not the bare module. Locks the
    #    real delegate paths against an AttributeError at runtime.
    sm = session_bridge.session_manager
    check(all(hasattr(sm, n) for n in ("get", "fork", "set_msg_ask_result")),
          "session_bridge.session_manager exposes get/fork/set_msg_ask_result")

    # 1. Unknown target → rejected, nothing runs (INV-3 fail closed).
    calls = _install_stubs(picker_returns="sess-A")
    r = await session_bridge.delegate(
        caller_sid="caller", target_sid="ghost", prompt="p",
        run_mode="fork", approval="auto")
    check(r.get("error") == "unknown_session", "unknown target rejected")
    check(not calls["run"] and not calls["picker"], "unknown target: no run/picker")

    # 2. auto + fork + flag OFF → picker (default fail closed, INV-4).
    user_prefs.set_cross_session_delegate_auto(False)
    calls = _install_stubs(picker_returns="sess-A")
    await session_bridge.delegate(
        caller_sid="caller", target_sid="sess-A", prompt="p",
        run_mode="fork", approval="auto")
    check(len(calls["picker"]) == 1 and len(calls["run"]) == 1,
          "auto+fork+flagOFF routed through picker")

    # 3. auto + fork + flag ON → direct run, no picker (INV-4).
    user_prefs.set_cross_session_delegate_auto(True)
    calls = _install_stubs(picker_returns="sess-A")
    await session_bridge.delegate(
        caller_sid="caller", target_sid="sess-A", prompt="p",
        run_mode="fork", approval="auto")
    check(len(calls["picker"]) == 0 and len(calls["run"]) == 1,
          "auto+fork+flagON runs without picker")

    # 4. auto + continue + flag ON → picker anyway (INV-8 stricter).
    calls = _install_stubs(picker_returns="sess-A")
    await session_bridge.delegate(
        caller_sid="caller", target_sid="sess-A", prompt="p",
        run_mode="continue", approval="auto")
    check(len(calls["picker"]) == 1, "continue always picker-gated even with flag ON")
    user_prefs.set_cross_session_delegate_auto(False)

    # 4a. continue-mode can carry explicit model selectors into the run.
    calls = _install_stubs(picker_returns="sess-A")
    await session_bridge.delegate(
        caller_sid="caller",
        target_sid="sess-A",
        prompt="p",
        run_mode="continue",
        approval="require",
        provider_id="provider-1",
        model="model-1",
        reasoning_effort="high",
    )
    run_kwargs = calls["run"][0][3]
    check(
        run_kwargs.get("provider_id") == "provider-1"
        and run_kwargs.get("model") == "model-1"
        and run_kwargs.get("reasoning_effort") == "high",
        "session-bridge continue passes explicit model selectors",
    )

    # 4b. auto + fork to a registered worker → direct run without enabling
    # broad cross-session auto delegation.
    _KNOWN["worker-1"] = {"id": "worker-1", "name": "worker", "cwd": "/x",
                          "project_name": "p", "first_user_prompt": "worker"}
    session_bridge.session_manager.get = (  # type: ignore
        lambda sid: {"id": sid, "cwd": "/x"} if sid == "caller" else {"id": sid}
    )
    worker_store.upsert_worker("/x", "worker-1", "native", "agent-1")
    calls = _install_stubs(picker_returns="worker-1")
    await session_bridge.delegate(
        caller_sid="caller", target_sid="worker-1", prompt="p",
        run_mode="fork", approval="auto")
    check(len(calls["picker"]) == 0 and len(calls["run"]) == 1,
          "registered worker auto+fork runs without picker")
    session_bridge.session_manager.get = _ORIG_SM_GET  # type: ignore

    # 5. require + fork → picker; cancel (picker None) aborts, no run.
    calls = _install_stubs(picker_returns=None)
    r = await session_bridge.delegate(
        caller_sid="caller", target_sid="sess-A", prompt="p",
        run_mode="fork", approval="require")
    check(r.get("cancelled") is True and not calls["run"],
          "picker cancel aborts with no run")

    # 5b. caller not in-flight → rejected on EVERY path (auto included).
    calls = _install_stubs(picker_returns="sess-A", caller_in_flight=False)
    r = await session_bridge.delegate(
        caller_sid="caller", target_sid="sess-A", prompt="p",
        run_mode="fork", approval="auto")
    check(r.get("error") == "caller_not_in_flight" and not calls["run"]
          and not calls["picker"], "caller not in-flight rejected (auto path)")
    user_prefs.set_cross_session_delegate_auto(True)
    calls = _install_stubs(picker_returns="sess-A", caller_in_flight=False)
    r = await session_bridge.delegate(
        caller_sid="caller", target_sid="sess-A", prompt="p",
        run_mode="fork", approval="auto")
    check(r.get("error") == "caller_not_in_flight" and not calls["run"],
          "caller not in-flight rejected even with flag ON")
    user_prefs.set_cross_session_delegate_auto(False)

    # 6. invalid params rejected.
    r = await session_bridge.delegate(
        caller_sid="caller", target_sid="sess-A", prompt="p",
        run_mode="bogus", approval="auto")
    check(r.get("error") == "invalid_run_mode", "invalid run_mode rejected")
    r = await session_bridge.delegate(
        caller_sid="caller", target_sid="sess-A", prompt="",
        run_mode="fork", approval="auto")
    check(r.get("error") == "prompt_required", "empty prompt rejected")

    # 6b. coordination lock_ops acquires by key, rejects competing holders, requires the
    # holder token to release, and expires after the fixed lease window.
    coordination._clear_for_tests()  # type: ignore[attr-defined]
    now = {"value": 100.0}
    original_now = coordination._now  # type: ignore[attr-defined]
    coordination._now = lambda: now["value"]  # type: ignore[attr-defined]
    try:
        r = await coordination.lock_ops(key="file-a")
        token = r.get("holder_token")
        check(r.get("success") is True and isinstance(token, str) and token,
              "lock_ops acquires lock and returns holder token")
        r = await coordination.lock_ops(key="file-a")
        check(r.get("success") is False and r.get("error") == "locked",
              "lock_ops rejects second acquire before expiry")
        r = await coordination.lock_ops(
            key="file-a", release=True, holder_token="wrong")
        check(r.get("success") is False and r.get("error") == "invalid_holder_token",
              "lock_ops rejects release with wrong token")
        r = await coordination.lock_ops(
            key="file-a", release=True, holder_token=token)
        check(r.get("success") is True and r.get("released") is True,
              "lock_ops releases with holder token")
        await coordination.lock_ops(key="file-a")
        now["value"] += 181.0
        r = await coordination.lock_ops(key="file-a")
        check(r.get("success") is True and r.get("holder_token") != token,
              "lock_ops replaces expired lock")
    finally:
        coordination._now = original_now  # type: ignore[attr-defined]
        coordination._clear_for_tests()  # type: ignore[attr-defined]

    # 7. resolve_delegation security: only a proposed id (or None) is accepted.
    session_bridge.session_manager.set_msg_ask_result = (  # type: ignore
        lambda *a, **k: None
    )
    fut = asyncio.get_running_loop().create_future()
    session_bridge._pending["d1"] = {
        "future": fut, "caller_sid": "c", "caller_msg_id": "m",
        "target_sid": "sess-A", "prompt": "p", "run_mode": "fork",
        "proposed_ids": ["sess-A"]}
    check(session_bridge.resolve_delegation("d1", "evil") is False,
          "resolve rejects non-proposed target")
    check(not fut.done(), "future untouched after rejected resolve")
    check(session_bridge.resolve_delegation("d1", "sess-A") is True,
          "resolve accepts proposed target")
    check(fut.result() == "sess-A", "future resolved with chosen id")
    check(session_bridge.resolve_delegation("d1", "sess-A") is False,
          "resolve no-op after already resolved")
    check(session_bridge.resolve_delegation("ghost", None) is False,
          "resolve unknown delegation id is no-op")

    # 7b. real _run rejects a busy `continue` target (H1) before any turn.
    ran = {"turn": 0}

    async def _fake_turn(sid, prompt, **_kwargs):
        ran["turn"] += 1
        return {"text": "x", "turn_id": "t"}

    session_bridge._run_turn = _fake_turn  # type: ignore
    session_bridge._target_busy = lambda sid: True  # type: ignore
    r = await _ORIG_RUN("sess-A", "p", "continue")
    check(r.get("error") == "target_busy" and ran["turn"] == 0,
          "real _run refuses busy continue target with no turn")
    session_bridge._target_busy = lambda sid: False  # type: ignore
    r = await _ORIG_RUN("sess-A", "p", "continue")
    check(r.get("final_message") == "x" and ran["turn"] == 1,
          "real _run runs continue when target idle")
    session_bridge._run_turn = _ORIG_RUN_TURN  # type: ignore

    # 7c. _run_turn must unblock from the lifecycle bus even if the direct
    # websocket callback never receives the terminal frame.
    sid = session_manager.create(name="bus-run", cwd="/x", orchestration_mode="native")["id"]

    class BusOnlyCoordinator:
        def register_ws(self, app_session_id, ws_callback, *, from_seq=0):
            pass

        def unregister_ws(self, app_session_id, ws_callback=None):
            pass

        def submit_prompt(self, app_session_id, params):
            lifecycle_msg_id = params["lifecycle_msg_id"]

            async def _finish():
                session_manager.append_assistant_msg(
                    app_session_id,
                    {
                        "id": "assistant-bus",
                        "role": "assistant",
                        "content": "bus done",
                        "events": [],
                        "isStreaming": False,
                    },
                )
                await bus.publish(BusEvent(
                    type="user_message_done",
                    root_id=app_session_id,
                    sid=app_session_id,
                    msg_id=lifecycle_msg_id,
                    payload={
                        "lifecycle_msg_id": lifecycle_msg_id,
                        "success": True,
                    },
                ))

            asyncio.create_task(_finish())

    fake_main = types.SimpleNamespace(coordinator=BusOnlyCoordinator())
    old_main = sys.modules.get("main")
    sys.modules["main"] = fake_main
    try:
        r = await _ORIG_RUN_TURN(sid, "p")
    finally:
        if old_main is None:
            sys.modules.pop("main", None)
        else:
            sys.modules["main"] = old_main
    check(r.get("text") == "bus done" and r.get("turn_id") == "assistant-bus",
          "_run_turn unblocks from lifecycle bus terminal event")

    # (Search-result confinement — dropping non-listable ids — now lives in
    # `session_search.validate_proposed`, covered by test_session_search_unit.
    # session_bridge no longer does grep search; the cross-session search runs
    # as a search worker via session_search.run_search_sessions_session.)


def main():
    try:
        asyncio.run(_run_tests())
    finally:
        from shutil import rmtree
        rmtree(_TMP, ignore_errors=True)
    if _FAILURES:
        print(f"\n{len(_FAILURES)} FAILURE(S)")
        sys.exit(1)
    print("\nALL PASS")


if __name__ == "__main__":
    main()
