"""Owner-side additive-changes coverage (surface-migration worktree).

Locks three owner-side facts the chat-surface adapter layer projects from:

1. `session_manager.manager._fire` publishes an additive `session.fire.*`
   bus fact (distinct from the pre-existing `session.<kind>` WS-broadcaster
   path), via `publish_threadsafe` so it fires even with no loop bound.
2. `turn_manager.TurnManager._publish_turn_start_lifecycle` /
   `_publish_terminal_lifecycle` carry `prompt_uuid` on their payload when
   known (`TurnManager._resolve_prompt_uuid`), and
   `backend.adapters.chat_adapter.ChatSurfaceAdapter._on_lifecycle` derives
   `TurnLifecycle.turn_id` from it (via `normalize.typed_prompt_node_id`),
   falling back to the pre-existing execution/user_turn_id fields.
3. `backend.adapters.normalize.normalize_journal_row` maps a provider
   `result` row to a `NodeKind.RESULT` / `ResultKind.PROVIDER` node so
   `derive.resolve_result`'s provider branch activates.

Run:
    PYTHONPATH=. python3 -m pytest backend/scripts/test_owner_facts.py -q
    PYTHONPATH=. python3 backend/scripts/test_owner_facts.py   # __main__ fallback
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

_BACKEND_DIR = str(Path(__file__).resolve().parent.parent)
_REPO_ROOT = str(Path(_BACKEND_DIR).parent)
for _p in (_BACKEND_DIR, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import _test_home  # noqa: E402
_test_home.isolate("bc_test_owner_facts_")

# Bare imports — matches turn_manager.py's/session_manager.py's own
# top-level `from event_bus import ...` idiom (backend/ on sys.path).
import event_bus as bare_event_bus  # noqa: E402
from session_manager import manager as sm  # noqa: E402
from turn_manager import TurnManager  # noqa: E402

# Dotted imports — matches chat_adapter.py's/normalize.py's own
# `from backend.X import ...` idiom (repo root on sys.path). Per
# test_chat_adapter.py's docstring, bare `event_bus` and dotted
# `backend.event_bus` are two distinct singletons in this codebase, so
# item 2's chat_adapter coverage below calls `_on_lifecycle` directly
# instead of round-tripping through either bus.
from backend.adapters.chat_adapter import ChatSurfaceAdapter  # noqa: E402
from backend.adapters.normalize import normalize_journal_row, typed_prompt_node_id  # noqa: E402
from backend.event_bus import BusEvent as DottedBusEvent  # noqa: E402
from backend.surface_contract.frames import TurnLifecycle, TurnPhase  # noqa: E402
from backend.surface_contract.nodes import NodeKind, ResultKind  # noqa: E402


class _StubCoordinator:
    """Minimal Coordinator stub — the lifecycle-emit helpers under test
    reach only `session_manager._root_id_for` (module global)."""


_IDENTITY = {
    "user_turn_id": "user-turn",
    "execution_turn_id": "execution-turn",
    "lifecycle_message_id": "lifecycle-message",
    "assistant_message_id": "assistant-message",
}


# ---------------------------------------------------------------------------
# Item 4: session_manager._fire -> session.fire.* bus fact.
# ---------------------------------------------------------------------------
def test_fire_publishes_session_fire_bus_fact() -> None:
    async def _scenario() -> None:
        sid = f"sid-fire-{uuid.uuid4().hex}"
        sub_name = f"test-owner-facts-fire-{uuid.uuid4().hex}"
        captured: list = []

        async def _handler(ev) -> None:
            captured.append(ev)

        bare_event_bus.bus.subscribe(
            "session.fire.*", _handler, name=sub_name, bind_current_loop=True,
        )
        try:
            sm._fire(sid, {"kind": "probe_change", "value": 42})
            # publish_threadsafe hands off via call_soon_threadsafe; give
            # the loop a few ticks to drain the cross-thread target.
            for _ in range(200):
                if captured:
                    break
                await asyncio.sleep(0.01)
            assert captured, "session.fire.* bus fact was not delivered"
            ev = captured[0]
            assert ev.type == "session.fire.probe_change"
            assert ev.sid == sid
            assert ev.payload == {"kind": "probe_change", "value": 42}
            assert ev.persist is False
        finally:
            bare_event_bus.bus.unsubscribe(sub_name)

    asyncio.run(_scenario())


def test_fire_publishes_unknown_kind_when_change_has_none() -> None:
    async def _scenario() -> None:
        sid = f"sid-fire-unknown-{uuid.uuid4().hex}"
        sub_name = f"test-owner-facts-fire-unknown-{uuid.uuid4().hex}"
        captured: list = []

        async def _handler(ev) -> None:
            captured.append(ev)

        bare_event_bus.bus.subscribe(
            "session.fire.*", _handler, name=sub_name, bind_current_loop=True,
        )
        try:
            sm._fire(sid, {})
            for _ in range(200):
                if captured:
                    break
                await asyncio.sleep(0.01)
            assert captured, "session.fire.* bus fact was not delivered"
            assert captured[0].type == "session.fire.unknown"
        finally:
            bare_event_bus.bus.unsubscribe(sub_name)

    asyncio.run(_scenario())


def test_fire_swallows_bus_publish_failure() -> None:
    """`_fire` must never raise even if the bus publish blows up —
    session mutation is the caller's business, not bus health."""
    sid = f"sid-fire-broken-{uuid.uuid4().hex}"
    real_publish_threadsafe = bare_event_bus.bus.publish_threadsafe

    def _boom(event) -> None:
        raise RuntimeError("bus is on fire")

    bare_event_bus.bus.publish_threadsafe = _boom  # type: ignore[assignment]
    try:
        sm._fire(sid, {"kind": "probe_change"})  # must not raise
    finally:
        bare_event_bus.bus.publish_threadsafe = real_publish_threadsafe  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Item 1: prompt_uuid on lifecycle facts + chat_adapter turn_id derivation.
# ---------------------------------------------------------------------------
def test_resolve_prompt_uuid_reads_agent_message_uuid_or_none() -> None:
    tm = TurnManager(_StubCoordinator())
    assert tm._resolve_prompt_uuid({"agent_message_uuid": "uuid-123"}) == "uuid-123"
    assert tm._resolve_prompt_uuid({"agent_message_uuid": None}) is None
    assert tm._resolve_prompt_uuid({}) is None
    assert tm._resolve_prompt_uuid({"agent_message_uuid": ""}) is None


def _subscribe_capture(pattern: str, name: str):
    captured: list = []

    async def _handler(ev) -> None:
        captured.append(ev)

    bare_event_bus.bus.subscribe(pattern, _handler, name=name)
    return captured


def test_publish_turn_start_includes_prompt_uuid_when_present() -> None:
    sid = f"sid-start-puuid-{uuid.uuid4().hex}"
    sub_name = f"test-owner-facts-start-{uuid.uuid4().hex}"
    captured = _subscribe_capture("lifecycle.turn_start", sub_name)
    try:
        tm = TurnManager(_StubCoordinator())
        asyncio.run(tm._publish_turn_start_lifecycle(
            app_session_id=sid,
            prompt_uuid="prompt-uuid-1",
            **_IDENTITY,
        ))
        events = [e for e in captured if e.sid == sid]
        assert len(events) == 1
        assert events[0].payload.get("prompt_uuid") == "prompt-uuid-1"
    finally:
        bare_event_bus.bus.unsubscribe(sub_name)


def test_publish_turn_start_omits_prompt_uuid_when_none() -> None:
    """None is the honest "genuinely not determinable yet" signal (e.g. at
    turn_start, before the provider's user-row echo has been folded) — it
    must not appear as a stray null key on the payload."""
    sid = f"sid-start-no-puuid-{uuid.uuid4().hex}"
    sub_name = f"test-owner-facts-start-none-{uuid.uuid4().hex}"
    captured = _subscribe_capture("lifecycle.turn_start", sub_name)
    try:
        tm = TurnManager(_StubCoordinator())
        asyncio.run(tm._publish_turn_start_lifecycle(
            app_session_id=sid,
            **_IDENTITY,
        ))
        events = [e for e in captured if e.sid == sid]
        assert len(events) == 1
        assert "prompt_uuid" not in events[0].payload
    finally:
        bare_event_bus.bus.unsubscribe(sub_name)


def test_publish_terminal_includes_prompt_uuid_when_present() -> None:
    sid = f"sid-terminal-puuid-{uuid.uuid4().hex}"
    sub_name = f"test-owner-facts-terminal-{uuid.uuid4().hex}"
    captured = _subscribe_capture("lifecycle.turn_complete", sub_name)
    try:
        tm = TurnManager(_StubCoordinator())
        asyncio.run(tm._publish_terminal_lifecycle(
            "complete",
            app_session_id=sid,
            reason="success",
            prompt_uuid="prompt-uuid-2",
            **_IDENTITY,
        ))
        events = [e for e in captured if e.sid == sid]
        assert len(events) == 1
        assert events[0].payload.get("prompt_uuid") == "prompt-uuid-2"
    finally:
        bare_event_bus.bus.unsubscribe(sub_name)


def test_publish_terminal_omits_prompt_uuid_when_none() -> None:
    sid = f"sid-terminal-no-puuid-{uuid.uuid4().hex}"
    sub_name = f"test-owner-facts-terminal-none-{uuid.uuid4().hex}"
    captured = _subscribe_capture("lifecycle.turn_stopped", sub_name)
    try:
        tm = TurnManager(_StubCoordinator())
        asyncio.run(tm._publish_terminal_lifecycle(
            "stopped",
            app_session_id=sid,
            reason="error",
            **_IDENTITY,
        ))
        events = [e for e in captured if e.sid == sid]
        assert len(events) == 1
        assert "prompt_uuid" not in events[0].payload
    finally:
        bare_event_bus.bus.unsubscribe(sub_name)


def test_on_lifecycle_prefers_prompt_uuid_over_execution_turn_id() -> None:
    adapter = ChatSurfaceAdapter()
    root_id = f"root-{uuid.uuid4().hex}"
    state = adapter._get_or_create(root_id)
    captured: list = []
    state.projection.register(captured.append)

    event = DottedBusEvent(
        type="lifecycle.turn_complete",
        root_id=root_id,
        sid=root_id,
        payload={
            "user_turn_id": "user-turn-id",
            "execution_turn_id": "execution-turn-id",
            "prompt_uuid": "prompt-uuid-xyz",
            "reason": "success",
        },
    )
    asyncio.run(adapter._on_lifecycle(event))

    frames = [f for f in captured if isinstance(f, TurnLifecycle)]
    assert len(frames) == 1
    assert frames[0].turn_id == "prompt-uuid-xyz"
    assert frames[0].turn_id == typed_prompt_node_id("prompt-uuid-xyz")
    assert frames[0].phase == TurnPhase.COMPLETED


def test_on_lifecycle_falls_back_to_execution_turn_id_without_prompt_uuid() -> None:
    adapter = ChatSurfaceAdapter()
    root_id = f"root-{uuid.uuid4().hex}"
    state = adapter._get_or_create(root_id)
    captured: list = []
    state.projection.register(captured.append)

    event = DottedBusEvent(
        type="lifecycle.turn_start",
        root_id=root_id,
        sid=root_id,
        payload={
            "user_turn_id": "user-turn-id",
            "execution_turn_id": "execution-turn-id",
        },
    )
    asyncio.run(adapter._on_lifecycle(event))

    frames = [f for f in captured if isinstance(f, TurnLifecycle)]
    assert len(frames) == 1
    assert frames[0].turn_id == "execution-turn-id"


def test_on_lifecycle_falls_back_to_user_turn_id_without_execution_turn_id() -> None:
    adapter = ChatSurfaceAdapter()
    root_id = f"root-{uuid.uuid4().hex}"
    state = adapter._get_or_create(root_id)
    captured: list = []
    state.projection.register(captured.append)

    event = DottedBusEvent(
        type="lifecycle.turn_start",
        root_id=root_id,
        sid=root_id,
        payload={"user_turn_id": "user-turn-id"},
    )
    asyncio.run(adapter._on_lifecycle(event))

    frames = [f for f in captured if isinstance(f, TurnLifecycle)]
    assert len(frames) == 1
    assert frames[0].turn_id == "user-turn-id"


# ---------------------------------------------------------------------------
# Item 3: result-row normalization -> RESULT/PROVIDER node.
# ---------------------------------------------------------------------------
def test_result_row_normalizes_to_result_provider_node() -> None:
    row = {
        "type": "agent_message",
        "seq": 1,
        "ts": "2026-01-01T00:00:00+00:00",
        "data": {
            "type": "result",
            "uuid": "result-uuid-1",
            "subtype": "success",
            "is_error": False,
            "result": "final answer",
        },
    }
    nodes = normalize_journal_row(row, surface_id="surf", turn_id="turn")
    assert len(nodes) == 1
    assert nodes[0].kind == NodeKind.RESULT
    assert nodes[0].payload.result_kind == ResultKind.PROVIDER
    assert nodes[0].node_id == "result-uuid-1"


if __name__ == "__main__":
    failures = 0
    tests = [
        obj for name, obj in list(globals().items())
        if name.startswith("test_") and callable(obj)
    ]
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {t.__name__}: {e}")
    sys.exit(1 if failures else 0)
