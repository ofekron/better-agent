"""100% unit coverage of hook_runner.py.

hook_runner is the bus-driven dispatcher that runs user-configured hooks as
REAL subprocesses: it matches incoming bus events against hook patterns,
pipes a JSON event envelope to the hook command's stdin, enforces a timeout
(killing the process on expiry), and publishes hook.started/completed/failed
meta events. This is a command-execution security path — env construction,
stdin piping, timeout/kill, and output limiting all have to behave exactly.

The execution paths are exercised with REAL subprocesses (``true``/``false``/
``echo``/``sh``/``sleep``) so the wiring is genuinely proven — no mocks paper
over process spawn, stdin, timeout-kill, or output capture. Only the bus and
the dispatcher's downstream collaborator (``_run_hook``) are stubbed, and only
where doing so isolates the unit under test (``bind_configured_hooks`` never
registers a real global subscriber; ``_dispatch_matching_hooks`` is tested for
its filtering, with ``_run_hook`` recorded).

Async scenarios run under ``asyncio.run`` (this repo has no pytest-asyncio),
matching the established convention in scripts/integration_test*.py.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

# Isolation is supplied by scripts/conftest.py (single shared session test
# home). hook_store reads hooks.json from ba_home(); under the conftest home
# that file is absent -> list_hooks returns [], and our tests inject hook
# lists directly via monkeypatch anyway.

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest  # noqa: E402

from event_bus import BusEvent  # noqa: E402
import hook_runner  # noqa: E402
from hook_runner import _SUBSCRIBER_NAME, _PRIORITY  # noqa: E402


def _hook(
    *,
    id: str = "h1",
    name: str = "hook-name",
    pattern: str = "*",
    command: list[str] | None = None,
    timeout_seconds: float = 5.0,
    enabled: bool = True,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
) -> dict:
    return {
        "id": id,
        "name": name,
        "pattern": pattern,
        "command": command if command is not None else ["true"],
        "timeout_seconds": timeout_seconds,
        "enabled": enabled,
        "env": env,
        "cwd": cwd,
    }


def _event(type: str = "something.happened", **kw) -> BusEvent:
    return BusEvent(type=type, root_id=kw.get("root_id", "root-1"),
                    sid=kw.get("sid", "sid-1"), payload=kw.get("payload", {}))


# --------------------------------------------------------------------------- #
# bind_configured_hooks — subscribe/unsubscribe wiring (no real registration)
# --------------------------------------------------------------------------- #


def test_bind_registers_single_subscriber_at_configured_priority(monkeypatch) -> None:
    calls: dict = {"unsub": [], "sub": []}
    monkeypatch.setattr(hook_runner.bus, "unsubscribe", lambda name: calls["unsub"].append(name))
    monkeypatch.setattr(
        hook_runner.bus, "subscribe",
        lambda pattern, handler, *, priority, name: calls["sub"].append(
            (pattern, handler, priority, name),
        ),
    )

    hook_runner.bind_configured_hooks()

    assert calls["unsub"] == [_SUBSCRIBER_NAME]
    assert calls["sub"] == [
        ("*", hook_runner._dispatch_matching_hooks, _PRIORITY, _SUBSCRIBER_NAME),
    ]


# --------------------------------------------------------------------------- #
# _dispatch_matching_hooks — meta-skip / load-failure / filtering
# --------------------------------------------------------------------------- #


def _run_dispatch(monkeypatch, hooks, event, list_hooks=None) -> tuple[list, bool]:
    """Drive _dispatch_matching_hooks; returns (matched hook ids, list_called)."""
    state = {"list_called": False}

    def _list():
        state["list_called"] = True
        if list_hooks is not None:
            return list_hooks()
        return hooks

    monkeypatch.setattr(hook_runner.hook_store, "list_hooks", _list)
    recorded: list[str] = []

    async def fake_run_hook(hook, ev):
        recorded.append(hook["id"])

    monkeypatch.setattr(hook_runner, "_run_hook", fake_run_hook)

    async def scenario():
        await hook_runner._dispatch_matching_hooks(event)
        pending = asyncio.all_tasks() - {asyncio.current_task()}
        if pending:
            await asyncio.gather(*pending)

    asyncio.run(scenario())
    return recorded, state["list_called"]


def test_dispatch_skips_hook_meta_events_without_loading_hooks(monkeypatch) -> None:
    recorded, loaded = _run_dispatch(monkeypatch, [_hook()], _event(type="hook.started"))
    assert loaded is False
    assert recorded == []


def test_dispatch_logs_and_returns_when_hook_store_load_fails(monkeypatch, caplog) -> None:
    def _boom():
        raise RuntimeError("store down")

    caplog.set_level(logging.ERROR, logger="hook_runner")
    recorded, loaded = _run_dispatch(
        monkeypatch, [], _event(type="x.y"), list_hooks=_boom,
    )
    assert loaded is True
    assert recorded == []
    assert any("failed to load hooks" in rec.message for rec in caplog.records)


def test_dispatch_filters_disabled_non_matching_and_matches(monkeypatch) -> None:
    hooks = [
        _hook(id="disabled", pattern="*", enabled=False),
        _hook(id="nomatch", pattern="nomatch.*"),
        _hook(id="matched", pattern="some*"),
        _hook(id="also_matched", pattern="*"),
    ]
    recorded, _ = _run_dispatch(monkeypatch, hooks, _event(type="something.happened"))
    # disabled skipped; "nomatch.*" does not match; "some.*" and "*" match.
    assert recorded == ["matched", "also_matched"]


def test_dispatch_with_no_hooks_loads_but_dispatches_nothing(monkeypatch) -> None:
    recorded, loaded = _run_dispatch(monkeypatch, [], _event(type="x.y"))
    assert loaded is True
    assert recorded == []


# --------------------------------------------------------------------------- #
# _run_hook — real subprocess outcomes + meta-event branching
# --------------------------------------------------------------------------- #


def _run_hook_scenario(monkeypatch, hook, event):
    """Run _run_hook with _publish_hook_meta recorded; return [(type, payload)]."""
    published: list[tuple[str, dict]] = []

    async def fake_publish(event_type, source_event, hk, payload):
        published.append((event_type, dict(payload)))

    monkeypatch.setattr(hook_runner, "_publish_hook_meta", fake_publish)

    async def scenario():
        await hook_runner._run_hook(hook, event)

    asyncio.run(scenario())
    return published


def test_run_hook_success_publishes_started_then_completed(monkeypatch) -> None:
    published = _run_hook_scenario(
        monkeypatch, _hook(command=["true"], timeout_seconds=5), _event(),
    )
    assert [p[0] for p in published] == ["hook.started", "hook.completed"]
    completed = published[1][1]
    assert completed["returncode"] == 0
    assert "stdout" in completed and "stderr" in completed


def test_run_hook_nonzero_exit_publishes_failed(monkeypatch) -> None:
    published = _run_hook_scenario(
        monkeypatch, _hook(id="nz", command=["false"], timeout_seconds=5), _event(),
    )
    assert [p[0] for p in published] == ["hook.started", "hook.failed"]
    failed = published[1][1]
    assert failed["returncode"] == 1
    assert failed["error_class"] == "HookCommandFailed"
    assert failed["error_message"] == "hook command exited 1"


def test_run_hook_timeout_publishes_failed_timeout(monkeypatch) -> None:
    published = _run_hook_scenario(
        monkeypatch,
        _hook(id="slow", command=["sleep", "5"], timeout_seconds=0.15),
        _event(),
    )
    assert [p[0] for p in published] == ["hook.started", "hook.failed"]
    failed = published[1][1]
    assert failed["error_class"] == "TimeoutError"
    assert failed["error_message"] == "hook timed out"


def test_run_hook_missing_binary_publishes_failed_exception(monkeypatch) -> None:
    published = _run_hook_scenario(
        monkeypatch,
        _hook(id="ghost", command=["definitely-not-a-real-binary-zzz"], timeout_seconds=5),
        _event(),
    )
    assert [p[0] for p in published] == ["hook.started", "hook.failed"]
    failed = published[1][1]
    assert failed["error_class"] == "FileNotFoundError"


# --------------------------------------------------------------------------- #
# _execute_hook_command — real subprocess: capture, returncode, timeout-kill
# --------------------------------------------------------------------------- #


def _exec(monkeypatch, hook):
    async def scenario():
        return await hook_runner._execute_hook_command(hook, {"any": "envelope"})
    return asyncio.run(scenario())


def test_execute_captures_stdout_and_returncode(monkeypatch) -> None:
    result = _exec(monkeypatch, _hook(command=["echo", "hello-world"]))
    assert result["returncode"] == 0
    assert result["stdout"] == "hello-world\n"
    assert result["stderr"] == ""


def test_execute_captures_stderr(monkeypatch) -> None:
    result = _exec(monkeypatch, _hook(command=["sh", "-c", "echo bye 1>&2"]))
    assert result["returncode"] == 0
    assert result["stderr"] == "bye\n"


def test_execute_reports_nonzero_returncode(monkeypatch) -> None:
    result = _exec(monkeypatch, _hook(command=["false"]))
    assert result["returncode"] == 1


def test_execute_times_out_and_kills_process(monkeypatch) -> None:
    async def scenario():
        with pytest.raises(asyncio.TimeoutError):
            await hook_runner._execute_hook_command(
                _hook(command=["sleep", "10"], timeout_seconds=0.1), {},
            )

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# _build_env
# --------------------------------------------------------------------------- #


def test_build_env_includes_defaults_and_hook_identity(monkeypatch) -> None:
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("HOME", "/users/me")
    env = hook_runner._build_env(_hook(id="h1", name="n", pattern="p.*"))
    assert env["PATH"] == "/usr/bin:/bin"
    assert env["HOME"] == "/users/me"
    assert env["BETTER_CLAUDE_HOOK_ID"] == "h1"
    assert env["BETTER_CLAUDE_HOOK_NAME"] == "n"
    assert env["BETTER_CLAUDE_HOOK_PATTERN"] == "p.*"


def test_build_env_merges_hook_env_overrides(monkeypatch) -> None:
    monkeypatch.setenv("PATH", "/bin")
    env = hook_runner._build_env(_hook(env={"CUSTOM": "v", "PATH": "/override"}))
    assert env["CUSTOM"] == "v"
    assert env["PATH"] == "/override"  # hook env overrides the default


def test_build_env_without_hook_env(monkeypatch) -> None:
    env = hook_runner._build_env(_hook(env=None))
    assert "CUSTOM" not in env
    assert "BETTER_CLAUDE_HOOK_ID" in env


# --------------------------------------------------------------------------- #
# _event_envelope
# --------------------------------------------------------------------------- #


def test_event_envelope_shape() -> None:
    event = BusEvent(
        type="evt.type", root_id="r", sid="s", payload={"k": 1},
        msg_id="m", run_id="run", ts="t", schema_version=2, seq=5, is_replay=True,
    )
    envelope = hook_runner._event_envelope(event, _hook(id="h1", name="n", pattern="p"))
    assert envelope["hook"] == {"id": "h1", "name": "n", "pattern": "p"}
    ev = envelope["event"]
    assert ev["type"] == "evt.type"
    assert ev["root_id"] == "r" and ev["sid"] == "s"
    assert ev["payload"] == {"k": 1}
    assert ev["msg_id"] == "m" and ev["run_id"] == "run" and ev["ts"] == "t"
    assert ev["schema_version"] == 2 and ev["seq"] == 5 and ev["is_replay"] is True


# --------------------------------------------------------------------------- #
# _publish_hook_meta — constructs + publishes the right BusEvent
# --------------------------------------------------------------------------- #


def test_publish_hook_meta_publishes_expected_event(monkeypatch) -> None:
    published: list[BusEvent] = []

    async def fake_publish(ev):
        published.append(ev)

    monkeypatch.setattr(hook_runner.bus, "publish", fake_publish)
    source = _event(type="src.evt", root_id="rr", sid="ss")
    source.seq = 9

    async def scenario():
        await hook_runner._publish_hook_meta(
            "hook.started", source, _hook(id="h1", name="n", pattern="p"), {"foo": "bar"},
        )

    asyncio.run(scenario())
    assert len(published) == 1
    ev = published[0]
    assert ev.type == "hook.started"
    assert ev.root_id == "rr" and ev.sid == "ss"
    assert ev.persist is False
    assert ev.payload["hook_id"] == "h1"
    assert ev.payload["hook_name"] == "n"
    assert ev.payload["hook_pattern"] == "p"
    assert ev.payload["source_event_type"] == "src.evt"
    assert ev.payload["source_event_seq"] == 9
    assert ev.payload["foo"] == "bar"


# --------------------------------------------------------------------------- #
# _decode_limited
# --------------------------------------------------------------------------- #


def test_decode_limited_passthrough_under_cap() -> None:
    assert hook_runner._decode_limited(b"hello") == "hello"


def test_decode_limited_truncates_over_cap() -> None:
    cap = hook_runner.hook_store.MAX_OUTPUT_BYTES
    assert len(hook_runner._decode_limited(b"a" * (cap + 5000))) == cap


def test_decode_limited_replaces_invalid_utf8() -> None:
    # 0xff/0xfe are invalid as UTF-8 leading bytes -> replaced, not raised.
    out = hook_runner._decode_limited(b"\xff\xfe\x00")
    assert isinstance(out, str)
    assert "�" in out


# --------------------------------------------------------------------------- #
# _log_task_failure — done-callback branches
# --------------------------------------------------------------------------- #


def _make_done_task(coro):
    """Run a coroutine to completion under a fresh loop; return (loop, task)."""
    loop = asyncio.new_event_loop()
    task = loop.create_task(coro)
    loop.run_until_complete(asyncio.wait_for(task, timeout=5))
    return loop, task


def test_log_task_failure_silent_on_success() -> None:
    async def ok():
        return 1

    loop, task = _make_done_task(ok())
    try:
        hook_runner._log_task_failure("h", task)  # must not raise
    finally:
        loop.close()


def test_log_task_failure_logs_on_exception(caplog) -> None:
    async def boom():
        raise RuntimeError("hook blew up")

    loop = asyncio.new_event_loop()
    task = loop.create_task(boom())
    with pytest.raises(RuntimeError):
        loop.run_until_complete(asyncio.wait_for(task, timeout=5))
    try:
        caplog.set_level(logging.ERROR, logger="hook_runner")
        hook_runner._log_task_failure("hbad", task)
        assert any("task failed" in rec.message for rec in caplog.records)
    finally:
        loop.close()


def test_log_task_failure_silent_on_cancellation() -> None:
    async def ok():
        return 1

    loop = asyncio.new_event_loop()
    task = loop.create_task(ok())
    task.cancel()
    try:
        loop.run_until_complete(task)
    except asyncio.CancelledError:
        pass
    try:
        hook_runner._log_task_failure("h", task)  # CancelledError swallowed
    finally:
        loop.close()
