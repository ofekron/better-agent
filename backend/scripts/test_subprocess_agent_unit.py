#!/usr/bin/env python3
"""Unit owner for orchs/_subprocess_agent.py.

`_subprocess_agent.py` (SubprocessAgent) is the base for subprocess-backed agent
sessions. It is exercised only by standalone __main__ scripts
(test_subprocess_agent_init_errors, test_late_flush_application) which are
pytest-invisible, and by two pytest files that reference it only STATICALLY
(a `.read_text()` source scan and a string-literal path tuple) — zero runtime
coverage. This file is its pytest owner.

Strategy (no real provider/process spawned):
- `prepare_and_start_run` patched at the module boundary with a scripted double
  that feeds `StreamEvent`s into the run queue via `loop.call_soon_threadsafe`
  (the same thread→loop bridge the real provider uses).
- `session_manager` (the `manager` object), `perf`, and the lazy-imported
  collaborators (`startup_recovery_gate`, `env_compat`, `turn_manager`,
  `event_journal`, `orchs.get_strategy`, `turn_helpers`) patched at their
  source modules.
- The REAL `event_shape.is_synthetic_event` is used (no fraud) — synthetic vs
  non-synthetic branches are driven by constructing matching StreamEvents.
- Coordinator / provider are lightweight fakes; cancel/drain/retry branches are
  driven deterministically (the provider's `cancel_turn` seeds the drain event;
  rate-limit retry timing is capped via a test-local `asyncio.wait_for` shim).
"""
from __future__ import annotations

import asyncio
import uuid
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Optional

import pytest

import env_compat
import event_journal
import orchs
import startup_recovery_gate
import turn_helpers
import turn_manager
from orchs import _subprocess_agent as sa
from provider import StreamEvent


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class _Poison:
    """Object whose attribute access raises — drives the drain `except` branch."""

    @property
    def type(self):  # noqa: D401
        raise ValueError("poison")

    @property
    def data(self):  # noqa: D401
        return {}


class _FakeManager:
    def __init__(self, *, root_id: Optional[str] = "root", session=None) -> None:
        self._root_id = root_id
        self._session = session if session is not None else {}
        self.set_agent_sid_calls: list[tuple] = []
        self.streaming_calls: list[tuple] = []
        self.appended_user: list = []
        self.appended_assistant: list = []

    def _root_id_for(self, _sid):
        return self._root_id

    def get(self, _sid):
        return self._session

    def set_agent_sid(self, sid, mode, discovered):
        self.set_agent_sid_calls.append((sid, mode, discovered))

    def set_streaming(self, sid, mid, streaming):
        self.streaming_calls.append((sid, mid, streaming))

    def append_user_msg(self, sid, msg):
        self.appended_user.append((sid, msg))

    def append_assistant_msg(self, sid, msg):
        self.appended_assistant.append((sid, msg))


class _FakeProvider:
    def __init__(self, *, suspended: bool = False, drain_event=None) -> None:
        self.suspended = suspended
        self.drain_event = drain_event
        self._queue: Optional[asyncio.Queue] = None
        self.cancel_calls: list = []

    def cancel_turn(self, run_id):
        self.cancel_calls.append(run_id)
        if self.drain_event is not None and self._queue is not None:
            self._queue.put_nowait(self.drain_event)


class _FakeTurnManager:
    def __init__(self) -> None:
        self.current_assistant_msgs: dict = {}
        self.active_run_ids: dict[str, list] = {}


class _FakeCoordinator:
    internal_token = "internal-token"

    def __init__(self, provider: _FakeProvider) -> None:
        self.provider = provider
        self.turn_manager = _FakeTurnManager()
        self.dispatched: list = []

    def provider_for_session(self, _sid):
        return self.provider

    async def persist_and_dispatch_raw(self, _sid, event):
        self.dispatched.append(event)


class _FakeStrategy:
    def build_assistant_scaffold(self) -> dict:
        return {"id": str(uuid.uuid4()), "role": "assistant", "events": []}


class _ScriptedPrepare:
    """Fake prepare_and_start_run: serves one scripted event-list per call."""

    def __init__(self, attempts, cancel: Optional[asyncio.Event] = None) -> None:
        # attempts: list of event-lists, one per call (consumed in order).
        self.attempts = [list(a) for a in attempts]
        self.cancel = cancel
        self.calls: list[dict] = []
        self._cancel_fired = False

    def __call__(self, provider, **kw):
        self.calls.append(kw)
        provider._queue = kw["queue"]
        loop = kw["loop"]
        queue = kw["queue"]
        events = self.attempts.pop(0) if self.attempts else []
        for ev in events:
            loop.call_soon_threadsafe(queue.put_nowait, ev)
        if self.cancel is not None and not self._cancel_fired:
            loop.call_soon_threadsafe(self.cancel.set)
            self._cancel_fired = True


# --------------------------------------------------------------------------- #
# Event builders
# --------------------------------------------------------------------------- #
def _agent_msg(data=None) -> StreamEvent:
    return StreamEvent("agent_message", data or {"type": "assistant", "text": "hi"})


def _synth() -> StreamEvent:
    return StreamEvent(
        "agent_message",
        {"type": "assistant", "message": {"model": "<synthetic>", "content": "x"}},
    )


def _discovered(sid: str) -> StreamEvent:
    return StreamEvent("session_discovered", {"session_id": sid})


def _complete(success: bool, *, session_id=None, error=None, token_usage=None) -> StreamEvent:
    data = {"success": success}
    if session_id is not None:
        data["session_id"] = session_id
    if error is not None:
        data["error"] = error
    if token_usage is not None:
        data["token_usage"] = token_usage
    return StreamEvent("complete", data)


def _error(error: str) -> StreamEvent:
    return StreamEvent("error", {"error": error})


# --------------------------------------------------------------------------- #
# Fixture
# --------------------------------------------------------------------------- #
@pytest.fixture
def tools(monkeypatch):
    """Patch invariant collaborators; return recorders + factories."""
    @contextmanager
    def _cm(*_a, **_k):
        yield

    perf_ns = SimpleNamespace(
        timed=_cm,
        stamp_enq=lambda *a, **k: 0,
        record_lag=lambda *a, **k: None,
    )
    monkeypatch.setattr(sa, "perf", perf_ns)

    async def _noop_recovery():
        return None

    monkeypatch.setattr(startup_recovery_gate, "wait_for_recovery_ready", _noop_recovery)
    monkeypatch.setattr(env_compat, "get_env", lambda *a, **k: "http://localhost:8000")
    monkeypatch.setattr(turn_manager, "_release_abandoned_queue", lambda *a, **k: None)
    monkeypatch.setattr(orchs, "get_strategy", lambda mode: _FakeStrategy())

    publish_calls: list[dict] = []

    async def _publish(**kwargs):
        publish_calls.append(kwargs)

    monkeypatch.setattr(event_journal, "publish_event", _publish)
    monkeypatch.setattr(turn_helpers, "_is_rate_limit_attempt", lambda error, events: False)

    holder = SimpleNamespace(publish_calls=publish_calls)

    def make_manager(**kw):
        mgr = _FakeManager(**kw)
        monkeypatch.setattr(sa, "session_manager", mgr)
        return mgr

    def set_prepare(prepare):
        monkeypatch.setattr(sa, "prepare_and_start_run", prepare)

    def set_rate_limit(value: bool):
        monkeypatch.setattr(
            turn_helpers, "_is_rate_limit_attempt", lambda error, events: value
        )

    holder.make_manager = make_manager
    holder.set_prepare = set_prepare
    holder.set_rate_limit = set_rate_limit
    holder.coordinator = lambda provider: _FakeCoordinator(provider)
    return holder


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# _ingest_agent_event
# --------------------------------------------------------------------------- #
def test_ingest_skips_non_agent_message(tools):
    tools.make_manager()
    agent = sa.SubprocessAgent(agent_session_id="base", cwd="/repo")

    async def main():
        await agent._ingest_agent_event(StreamEvent("complete", {}))

    _run(main())
    assert tools.publish_calls == []


def test_ingest_skips_when_no_root(tools):
    tools.make_manager(root_id=None)
    agent = sa.SubprocessAgent(agent_session_id="base", cwd="/repo")

    async def main():
        await agent._ingest_agent_event(_agent_msg({"type": "assistant"}))

    _run(main())
    assert tools.publish_calls == []


def test_ingest_publishes_with_root(tools):
    tools.make_manager(root_id="root-1")
    agent = sa.SubprocessAgent(agent_session_id="base", cwd="/repo")

    async def main():
        await agent._ingest_agent_event(
            _agent_msg({"x": 1}), message_id="m1"
        )

    _run(main())
    assert len(tools.publish_calls) == 1
    call = tools.publish_calls[0]
    assert call["session_id"] == "root-1"
    assert call["context_id"] == "base"
    assert call["source"] == "subprocess_agent"
    assert call["message_id"] == "m1"


def test_ingest_swallows_publish_exception(tools, monkeypatch):
    tools.make_manager(root_id="root-1")

    async def _boom(**kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(event_journal, "publish_event", _boom)
    agent = sa.SubprocessAgent(agent_session_id="base", cwd="/repo")

    async def main():
        # Must not raise.
        await agent._ingest_agent_event(_agent_msg())

    _run(main())


# --------------------------------------------------------------------------- #
# _create_provisioning_messages
# --------------------------------------------------------------------------- #
def test_create_provisioning_messages_appends_and_scaffolds(tools):
    mgr = tools.make_manager()
    agent = sa.SubprocessAgent(agent_session_id="base", cwd="/repo")

    msg = agent._create_provisioning_messages(mode="native", prep_prompt="prep")

    assert msg["role"] == "assistant"
    assert msg["source"] == "provisioning"
    assert "id" in msg and "timestamp" in msg
    assert len(mgr.appended_user) == 1
    assert mgr.appended_user[0][1]["content"] == "prep"
    assert len(mgr.appended_assistant) == 1
    assert mgr.streaming_calls == [("base", msg["id"], True)]


# --------------------------------------------------------------------------- #
# init
# --------------------------------------------------------------------------- #
def _init_coro(agent, coord, **kw):
    return agent.init(
        coord,
        model="model",
        prep_prompt="prepare",
        cancel_event=asyncio.Event(),
        **kw,
    )


def test_init_raises_when_provider_suspended(tools):
    tools.make_manager()
    provider = _FakeProvider(suspended=True)
    coord = tools.coordinator(provider)
    agent = sa.SubprocessAgent(agent_session_id="base", cwd="/repo")

    with pytest.raises(RuntimeError, match="suspended"):
        _run(_init_coro(agent, coord))


def test_init_happy_discovers_sid(tools):
    mgr = tools.make_manager()
    provider = _FakeProvider()
    coord = tools.coordinator(provider)
    tools.set_prepare(_ScriptedPrepare([[_discovered("s1"), _complete(True, session_id="s1")]]))
    agent = sa.SubprocessAgent(agent_session_id="base", cwd="/repo")

    discovered = _run(_init_coro(agent, coord))

    assert discovered == "s1"
    assert agent.agent_sid == "s1"
    assert agent.initialized is True
    assert mgr.set_agent_sid_calls == [("base", "native", "s1")]
    types = [e["type"] for e in coord.dispatched]
    assert "agent_prep_start" in types
    assert "agent_prep_complete" in types


def test_init_provisioning_sets_streaming_false_on_complete(tools):
    mgr = tools.make_manager()
    provider = _FakeProvider()
    coord = tools.coordinator(provider)
    tools.set_prepare(_ScriptedPrepare([[_complete(True, session_id="s1")]]))
    agent = sa.SubprocessAgent(agent_session_id="base", cwd="/repo")

    discovered = _run(
        agent.init(
            coord,
            model="model",
            prep_prompt="prepare",
            cancel_event=asyncio.Event(),
            create_provisioning_messages=True,
        )
    )

    assert discovered == "s1"
    # provisioning created an assistant msg and cleared streaming at completion.
    assert len(mgr.appended_assistant) == 1
    mid = mgr.appended_assistant[0][1]["id"]
    assert (mgr.streaming_calls[-1]) == ("base", mid, False)


def test_init_complete_failure_raises_terminal_error(tools):
    tools.make_manager()
    coord = tools.coordinator(_FakeProvider())
    tools.set_prepare(_ScriptedPrepare([[_complete(False, error="boom")]]))
    agent = sa.SubprocessAgent(agent_session_id="base", cwd="/repo")

    with pytest.raises(RuntimeError, match="boom"):
        _run(_init_coro(agent, coord))


def test_init_error_event_raises_terminal_error(tools):
    tools.make_manager()
    coord = tools.coordinator(_FakeProvider())
    tools.set_prepare(_ScriptedPrepare([[_error("kaput")]]))
    agent = sa.SubprocessAgent(agent_session_id="base", cwd="/repo")

    with pytest.raises(RuntimeError, match="kaput"):
        _run(_init_coro(agent, coord))


def test_init_cancel_returns_none(tools):
    tools.make_manager()
    provider = _FakeProvider()
    coord = tools.coordinator(provider)
    tools.set_prepare(_ScriptedPrepare([[]]))  # no events
    agent = sa.SubprocessAgent(agent_session_id="base", cwd="/repo")

    async def main():
        cancel = asyncio.Event()
        cancel.set()
        return await agent.init(
            coord,
            model="model",
            prep_prompt="prepare",
            cancel_event=cancel,
        )

    result = _run(main())
    assert result is None
    assert provider.cancel_calls  # soft stop invoked
    types = [e["type"] for e in coord.dispatched]
    assert "agent_prep_cancelled" in types


def test_init_cancel_with_provisioning_clears_streaming(tools):
    mgr = tools.make_manager()

    async def main():
        cancel = asyncio.Event()
        cancel.set()
        coord = tools.coordinator(_FakeProvider())
        tools.set_prepare(_ScriptedPrepare([[]]))
        agent = sa.SubprocessAgent(agent_session_id="base", cwd="/repo")
        return await agent.init(
            coord,
            model="model",
            prep_prompt="prepare",
            cancel_event=cancel,
            create_provisioning_messages=True,
        )

    result = _run(main())
    assert result is None
    mid = mgr.appended_assistant[0][1]["id"]
    assert (mgr.streaming_calls[-1]) == ("base", mid, False)


def test_init_synthetic_event_skips_ingest_and_persist(tools):
    mgr = tools.make_manager(root_id="root")
    coord = tools.coordinator(_FakeProvider())
    tools.set_prepare(
        _ScriptedPrepare(
            [[_synth(), _discovered("s1"), _complete(True, session_id="s1")]]
        )
    )
    agent = sa.SubprocessAgent(agent_session_id="base", cwd="/repo")

    discovered = _run(_init_coro(agent, coord))

    assert discovered == "s1"
    # synthetic agent_message must NOT be published.
    assert tools.publish_calls == []


def test_init_non_terminal_event_ingests_and_persists(tools):
    tools.make_manager(root_id="root")
    coord = tools.coordinator(_FakeProvider())
    tools.set_prepare(
        _ScriptedPrepare(
            [[_agent_msg({"type": "assistant", "text": "hi"}), _complete(True, session_id="s1")]]
        )
    )
    agent = sa.SubprocessAgent(agent_session_id="base", cwd="/repo")

    _run(_init_coro(agent, coord))

    assert len(tools.publish_calls) == 1
    types = [e["type"] for e in coord.dispatched]
    assert "agent_prep_event" in types


def test_init_discovers_sid_from_complete_only(tools):
    mgr = tools.make_manager()
    coord = tools.coordinator(_FakeProvider())
    # No session_discovered event; sid arrives only on the complete event.
    tools.set_prepare(_ScriptedPrepare([[_complete(True, session_id="from-complete")]]))
    agent = sa.SubprocessAgent(agent_session_id="base", cwd="/repo")

    discovered = _run(_init_coro(agent, coord))

    assert discovered == "from-complete"
    assert agent.initialized is True


def test_init_complete_success_without_sid_leaves_uninitialized(tools):
    tools.make_manager()
    coord = tools.coordinator(_FakeProvider())
    # Success but no session_id anywhere → discovered stays None.
    tools.set_prepare(_ScriptedPrepare([[_complete(True)]]))
    agent = sa.SubprocessAgent(agent_session_id="base", cwd="/repo")

    discovered = _run(_init_coro(agent, coord))

    assert discovered is None
    assert agent.initialized is False
    assert agent.agent_sid is None


def test_init_prep_event_broadcast_failure_is_swallowed(tools, monkeypatch):
    tools.make_manager(root_id="root")
    coord = tools.coordinator(_FakeProvider())
    # A non-terminal, non-synth agent_message triggers a prep_event dispatch;
    # make that one dispatch raise so the except-branch fires.
    real_dispatch = coord.persist_and_dispatch_raw
    dispatched_types: list[str] = []

    async def flaky_dispatch(sid, event):
        etype = event["type"]
        dispatched_types.append(etype)
        if etype == "agent_prep_event":
            raise RuntimeError("broadcast failed")
        await real_dispatch(sid, event)

    coord.persist_and_dispatch_raw = flaky_dispatch
    tools.set_prepare(
        _ScriptedPrepare(
            [[_agent_msg({"type": "assistant", "text": "hi"}), _complete(True, session_id="s1")]]
        )
    )
    agent = sa.SubprocessAgent(agent_session_id="base", cwd="/repo")

    discovered = _run(_init_coro(agent, coord))

    # The failed prep_event broadcast did not abort init.
    assert discovered == "s1"
    assert "agent_prep_event" in dispatched_types


# --------------------------------------------------------------------------- #
# run_turn
# --------------------------------------------------------------------------- #
def _run_turn(agent, coord, *, ws_callback, cancel, **kw):
    return agent.run_turn(
        coord,
        prompt="p",
        model="model",
        ws_callback=ws_callback,
        cancel_event=cancel,
        **kw,
    )


def test_run_turn_raises_when_provider_suspended(tools):
    tools.make_manager()
    provider = _FakeProvider(suspended=True)
    coord = tools.coordinator(provider)
    agent = sa.SubprocessAgent(agent_session_id="base", cwd="/repo")

    async def main():
        return await agent.run_turn(
            coord,
            prompt="p",
            model="m",
            ws_callback=lambda _e: _noop_async(),
            cancel_event=asyncio.Event(),
        )

    with pytest.raises(RuntimeError, match="suspended"):
        _run(main())


async def _noop_async():
    return None


def test_run_turn_happy_returns_success(tools):
    tools.make_manager()
    provider = _FakeProvider()
    coord = tools.coordinator(provider)
    tools.set_prepare(
        _ScriptedPrepare([[_discovered("s1"), _complete(True, session_id="s1", token_usage={"in": 1})]])
    )
    agent = sa.SubprocessAgent(agent_session_id="base", cwd="/repo")
    seen: list = []

    async def ws(event):
        seen.append(event)

    async def main():
        return await _run_turn(agent, coord, ws_callback=ws, cancel=asyncio.Event())

    result = _run(main())
    assert result["success"] is True
    assert result["session_id"] == "s1"
    assert result["token_usage"] == {"in": 1}
    assert agent.agent_sid == "s1"
    assert coord.turn_manager.active_run_ids == {}  # run id dropped after turn


def test_run_turn_error_non_rate_limit(tools):
    tools.make_manager()
    coord = tools.coordinator(_FakeProvider())
    tools.set_prepare(_ScriptedPrepare([[_error("fail")]]))
    tools.set_rate_limit(False)
    agent = sa.SubprocessAgent(agent_session_id="base", cwd="/repo")

    async def main():
        return await _run_turn(agent, coord, ws_callback=lambda _e: _noop_async(), cancel=asyncio.Event())

    result = _run(main())
    assert result["success"] is False
    assert result["error"] == "fail"


def test_run_turn_cancel_drain_success(tools):
    tools.make_manager()
    drain = _agent_msg({"type": "assistant", "text": "drained"})
    provider = _FakeProvider(drain_event=drain)
    coord = tools.coordinator(provider)
    tools.set_prepare(_ScriptedPrepare([[]]))  # nothing for the inner loop

    async def main():
        cancel = asyncio.Event()
        cancel.set()
        agent = sa.SubprocessAgent(agent_session_id="base", cwd="/repo")
        return await _run_turn(
            agent, coord, ws_callback=lambda _e: _noop_async(), cancel=cancel
        )

    result = _run(main())
    assert result["success"] is False
    assert result["error"] == "cancelled"
    assert provider.cancel_calls  # soft stop invoked


def test_run_turn_cancel_drain_except_swallowed(tools):
    tools.make_manager()
    provider = _FakeProvider(drain_event=_Poison())
    coord = tools.coordinator(provider)
    tools.set_prepare(_ScriptedPrepare([[]]))

    async def main():
        cancel = asyncio.Event()
        cancel.set()
        agent = sa.SubprocessAgent(agent_session_id="base", cwd="/repo")
        return await _run_turn(
            agent, coord, ws_callback=lambda _e: _noop_async(), cancel=cancel
        )

    result = _run(main())
    assert result["error"] == "cancelled"


def test_run_turn_cancel_drain_synthetic_is_skipped(tools):
    tools.make_manager()
    provider = _FakeProvider(drain_event=_synth())
    coord = tools.coordinator(provider)
    tools.set_prepare(_ScriptedPrepare([[]]))

    async def main():
        cancel = asyncio.Event()
        cancel.set()
        agent = sa.SubprocessAgent(agent_session_id="base", cwd="/repo")
        return await _run_turn(
            agent, coord, ws_callback=lambda _e: _noop_async(), cancel=cancel
        )

    result = _run(main())
    # Synthetic drained event is not appended/ingested; turn still reports cancelled.
    assert result["success"] is False
    assert result["error"] == "cancelled"
    assert result["events"] == []


def test_run_turn_synthetic_agent_message_is_skipped(tools):
    tools.make_manager()
    coord = tools.coordinator(_FakeProvider())
    # A synthetic agent_message is skipped (not appended/ingested/broadcast),
    # but the loop continues to the real session_discovered + complete.
    tools.set_prepare(
        _ScriptedPrepare([[_synth(), _discovered("s1"), _complete(True, session_id="s1")]])
    )
    agent = sa.SubprocessAgent(agent_session_id="base", cwd="/repo")
    seen: list = []

    async def ws(event):
        seen.append(event)

    async def main():
        return await _run_turn(agent, coord, ws_callback=ws, cancel=asyncio.Event())

    result = _run(main())
    assert result["success"] is True
    # synthetic agent_message was not broadcast: only discovered + complete.
    assert len(seen) == 2


def test_run_turn_ws_callback_exception_drops_run_id_and_reraises(tools):
    tools.make_manager(root_id="root")
    coord = tools.coordinator(_FakeProvider())
    tools.set_prepare(_ScriptedPrepare([[_agent_msg({"type": "assistant", "text": "x"})]]))
    agent = sa.SubprocessAgent(agent_session_id="base", cwd="/repo")

    async def bad_ws(_event):
        raise ValueError("ws boom")

    async def main():
        return await _run_turn(agent, coord, ws_callback=bad_ws, cancel=asyncio.Event())

    with pytest.raises(ValueError, match="ws boom"):
        _run(main())
    # The abandoned run id was dropped from active tracking.
    assert coord.turn_manager.active_run_ids == {}


def test_run_turn_session_discovered_without_sid_keeps_prior(tools):
    tools.make_manager()
    coord = tools.coordinator(_FakeProvider())
    # session_discovered with no session_id → `disc` falsy → discovered unchanged.
    tools.set_prepare(
        _ScriptedPrepare(
            [[StreamEvent("session_discovered", {}), _complete(True, session_id="late")]]
        )
    )
    agent = sa.SubprocessAgent(agent_session_id="base", cwd="/repo")

    async def main():
        return await _run_turn(agent, coord, ws_callback=lambda _e: _noop_async(), cancel=asyncio.Event())

    result = _run(main())
    assert result["success"] is True
    assert result["session_id"] == "late"


def test_run_turn_drop_keeps_sibling_active_run_id(tools):
    tools.make_manager()
    coord = tools.coordinator(_FakeProvider())
    # Pre-existing sibling run id for the same session stays after this turn's
    # own id is dropped.
    coord.turn_manager.active_run_ids["base"] = ["sibling"]
    tools.set_prepare(
        _ScriptedPrepare([[_discovered("s1"), _complete(True, session_id="s1")]])
    )
    agent = sa.SubprocessAgent(agent_session_id="base", cwd="/repo")

    async def main():
        return await _run_turn(agent, coord, ws_callback=lambda _e: _noop_async(), cancel=asyncio.Event())

    result = _run(main())
    assert result["success"] is True
    # sibling survived; this turn's run id was removed but the entry was not popped.
    assert coord.turn_manager.active_run_ids.get("base") == ["sibling"]


def test_run_turn_rate_limit_retry_then_success(tools, monkeypatch):
    tools.make_manager()
    coord = tools.coordinator(_FakeProvider())
    tools.set_rate_limit(True)
    # attempt 1: rate-limited failure; attempt 2: success.
    tools.set_prepare(
        _ScriptedPrepare(
            [
                [_complete(False, error="rate_limit")],
                [_discovered("s2"), _complete(True, session_id="s2")],
            ]
        )
    )
    # Cap the retry wait timeout so the test does not sleep 5s.
    _cap_wait_for(monkeypatch)
    agent = sa.SubprocessAgent(agent_session_id="base", cwd="/repo")

    async def main():
        return await _run_turn(agent, coord, ws_callback=lambda _e: _noop_async(), cancel=asyncio.Event())

    result = _run(main())
    assert result["success"] is True
    assert result["session_id"] == "s2"


def test_run_turn_rate_limit_retry_cancelled(tools):
    tools.make_manager()
    coord = tools.coordinator(_FakeProvider())
    tools.set_rate_limit(True)
    agent = sa.SubprocessAgent(agent_session_id="base", cwd="/repo")

    async def main():
        cancel = asyncio.Event()
        # attempt 1 rate-limits; prepare sets cancel after serving the events.
        prepare = _ScriptedPrepare(
            [[_complete(False, error="rate_limit")]], cancel=cancel
        )
        tools.set_prepare(prepare)
        result = await _run_turn(
            agent, coord, ws_callback=lambda _e: _noop_async(), cancel=cancel
        )
        return result, prepare

    result, prepare = _run(main())
    # The rate-limit error stays set, but success is False and the retry was
    # cancelled before a second attempt could spawn.
    assert result["success"] is False
    assert result["error"] == "rate_limit"
    assert len(prepare.calls) == 1


def _cap_wait_for(monkeypatch) -> None:
    """Replace asyncio.wait_for with one that caps long timeouts (test-only)."""
    orig = asyncio.wait_for

    async def capped(aw, timeout):
        if timeout is not None and timeout >= 5:
            timeout = 0.05
        return await orig(aw, timeout)

    monkeypatch.setattr(asyncio, "wait_for", capped)
