#!/usr/bin/env python3
"""Integration-layer proof for the P0 write-path fix: a real turn through
the v2 Chat Surface Contract now produces live `node_upsert` frames and a
non-empty `turns` snapshot for a native-mode turn.

Root cause (was): `orchestrator.py::Coordinator._init_turn_messages`
(called from `turn_manager.py::TurnManager.run_turn`) persisted the
user's own typed prompt ONLY into the legacy `session.messages` array via
`session_manager.append_user_msg` — it never wrote an `agent_message` row
to the event journal. For a `mode="native"` turn (the SDK-driven runner
path — `runner.py`'s `client.query()` / `client.receive_response()` never
echoes the outbound user message back through the response stream), the
ONLY journal writes for a turn were the assistant-stream rows written by
`turn_manager.py::TurnManager._publish_provider_stream_event`. No row
ever normalized to a `TYPED_PROMPT` node, so
`ChatSurfaceAdapter._segment_turns` never found a turn boundary:
`_on_event_written` dropped every row via its `if turn_id is None:
continue` guard (frames stayed empty every call, though
`state.projection.bump_render()` still ran unconditionally — exactly
matching the field evidence's `render_rev` advancing with zero
broadcasts), and `open_session`/`_all_turns`'s independent full-replay
rescan found no segments either, so `turns` stayed `()` forever, even
after the turn completed.

Fix: `turn_manager.py::TurnManager._publish_typed_prompt_journal`
journals a backend-authored, TYPED_PROMPT-shaped `agent_message` row at
turn dispatch (deterministic `data["uuid"] = user_msg["id"]`,
`data["origin"]` always stamped), called from `run_turn` alongside
`_publish_prompt_meta`. `backend.adapters.normalize.is_canonical_prompt_
row` + `chat_adapter._segment_turns`/`_on_event_written` recognize a
LATER raw provider-transcript echo of the SAME prompt (a DIFFERENT uuid,
no `origin`, no ownership `msg_id` — see `_ingest_provider_echo_row`
below) as the currently-open turn's own echo and drop it, rather than
misfiling it as a second turn boundary — see the convergence tests below.

Run:
    PYTHONPATH=. python3 -m pytest backend/scripts/test_v2_projection_end_to_end.py -q
    PYTHONPATH=. python3 backend/scripts/test_v2_projection_end_to_end.py   # __main__ fallback
"""

from __future__ import annotations

import asyncio
import atexit
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

_BACKEND_DIR = str(Path(__file__).resolve().parents[1])
_REPO_ROOT = str(Path(_BACKEND_DIR).parent)
for _p in (_BACKEND_DIR, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import paths  # noqa: E402  (bare — matches sibling backend/scripts tests)

_TEST_HOME = tempfile.mkdtemp(prefix="ba-v2-projection-e2e-test-")
paths.engage_test_home(_TEST_HOME)
atexit.register(shutil.rmtree, _TEST_HOME, ignore_errors=True)

from backend.adapters.chat_adapter import ChatSurfaceAdapter  # noqa: E402
from backend.event_bus import BusEvent, bus  # noqa: E402
from backend.event_ingester import event_ingester  # noqa: E402
from backend.event_journal import EVENT_JOURNAL_WRITTEN  # noqa: E402
from backend.surface_contract.frames import NodeUpsert  # noqa: E402
from backend.surface_contract.identity import Focus, Ok, SurfaceCursor  # noqa: E402
from backend.surface_contract.nodes import NodeKind  # noqa: E402


def _publish_written(root_id: str, seq: int) -> None:
    """Mirrors `EventJournalWriter`'s post-append bus publish (the real
    writer publishes this after every `event_ingester.ingest` call;
    driving `event_ingester.ingest` directly here — as the real
    provider-stream write path ultimately does too, several layers down
    — requires reproducing that publish by hand, same as
    test_chat_adapter.py's `_publish_written`)."""
    asyncio.run(
        bus.publish(
            BusEvent(
                type=EVENT_JOURNAL_WRITTEN,
                root_id=root_id,
                sid=root_id,
                payload={
                    "event_type": "agent_message", "seq": seq, "data": {},
                    "source": "test", "event_id": str(uuid.uuid4()),
                },
            )
        )
    )


def _ingest_assistant_stream_only(root_id: str) -> list[int]:
    """Exactly what `turn_manager.py::TurnManager._publish_provider_stream_event`
    writes for a native-mode turn's assistant response — an `assistant`
    row followed by a terminal `result` row — via
    `orchs/base.py::OrchestrationStrategy.prepare_provider_event_for_journal`'s
    `(etype="agent_message", data={"type": ..., ...})` shape. No `user`
    row: `runner.py`'s SDK `receive_response()` stream never yields one,
    and `orchestrator.py::_init_turn_messages` (the only writer of the
    user's prompt) persists it to `session.messages` only, never to the
    journal."""
    seqs = []
    seqs.append(
        event_ingester.ingest(
            root_id, root_id, "agent_message",
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi there"}]}},
            source="test",
        )
    )
    seqs.append(
        event_ingester.ingest(
            root_id, root_id, "agent_message",
            {"type": "result", "result": "hi there", "is_error": False},
            source="test",
        )
    )
    return seqs


def _ingest_user_prompt(root_id: str, text: str) -> int:
    """The row shape `backend.adapters.normalize._handle_user` requires to
    produce a TYPED_PROMPT node — a minimal single-user-row-per-turn
    fixture (no uuid/origin/ownership), distinct from `_ingest_typed_
    prompt_journal_row`'s production-accurate shape below."""
    return event_ingester.ingest(
        root_id, root_id, "agent_message",
        {"type": "user", "message": {"content": text}},
        source="test",
    )


def _ingest_typed_prompt_journal_row(
    root_id: str, *, user_msg_id: str, assistant_msg_id: str, text: str, origin: str = "user",
) -> int:
    """Exactly what `turn_manager.py::TurnManager._publish_typed_prompt_
    journal` now journals at turn dispatch (the fix for this module's
    defect) — `type: "agent_message"`, `data: {"type": "user", "uuid":
    user_msg_id, "message": {...}, "origin": origin}`, journal-ownership
    `msg_id=assistant_msg_id` (same convention `_publish_prompt_meta`
    uses). `data["origin"]` being present is exactly what `normalize.
    is_canonical_prompt_row` uses to recognize this as the backend-
    authored canonical row rather than a raw provider-transcript echo."""
    return event_ingester.ingest(
        root_id, root_id, "agent_message",
        {
            "type": "user", "uuid": user_msg_id,
            "message": {"role": "user", "content": [{"type": "text", "text": text}]},
            "origin": origin,
        },
        source="test", msg_id=assistant_msg_id,
    )


def _ingest_provider_echo_row(root_id: str, *, echo_uuid: str, text: str) -> int:
    """Exactly what `jsonl_tailer.OwnedClaudeJsonlTailer`/`ingest_orphan`
    journals for the CLI/SDK's own on-disk session-transcript `type:
    "user"` line — a DIFFERENT uuid than the canonical row, no `origin`
    key (a raw provider transcript line is never backend-authored), and
    no ownership `msg_id` (`ingest_orphan`'s documented `msg_id=None`
    convention — `orchs/base.py::OrchestrationStrategy.ingest_orphan`)."""
    return event_ingester.ingest(
        root_id, root_id, "agent_message",
        {"type": "user", "uuid": echo_uuid, "message": {"role": "user", "content": text}},
        source="test",
    )


def test_real_turn_write_path_renders_after_prompt_journaling_fix() -> None:
    """Asserts the CORRECT behavior of a completed turn (a live NodeUpsert
    broadcast during the turn, and a non-empty `turns` tuple on replay)
    driven through the exact rows the real production write path now
    emits for a native-mode turn, post-fix: `turn_manager.py::TurnManager.
    _publish_typed_prompt_journal`'s canonical prompt row FOLLOWED by
    `_publish_provider_stream_event`'s assistant-stream rows.

    This is the flipped counterpart of the original field-defect
    reproduction (renamed from `test_real_turn_write_path_should_render_
    but_currently_does_not`): before the fix, only the assistant-stream
    rows ever reached the journal for a native-mode turn (no `type: user`
    row existed anywhere in `runner.py`'s SDK response stream), so
    `_segment_turns` found no turn boundary and every row was dropped by
    `_on_event_written`'s `turn_id is None` guard — zero node_upsert
    frames, `turns: []` on replay, even after `render_rev` advanced.
    `test_journaling_the_user_prompt_row_fixes_the_projection` below is
    the minimal-shape counterpart proving the SAME fix with a bare
    (uuid/origin-less) row.
    """
    root_id = f"root-{uuid.uuid4().hex}"
    user_msg_id = f"user-{uuid.uuid4().hex}"
    assistant_msg_id = f"asst-{uuid.uuid4().hex}"

    adapter = ChatSurfaceAdapter()
    adapter.bind()  # real bus subscription, same as build_adapter() at startup

    opened = adapter.open_session(root_id)
    assert isinstance(opened, Ok), opened
    identity = opened.snapshot

    received: list[object] = []
    cursor = SurfaceCursor(surface_id=root_id, incarnation=identity.incarnation, render_rev=identity.render_rev)
    sub = adapter.subscribe((cursor,), Focus.OPENED, received.append)
    try:
        prompt_seq = _ingest_typed_prompt_journal_row(
            root_id, user_msg_id=user_msg_id, assistant_msg_id=assistant_msg_id, text="hello",
        )
        _publish_written(root_id, prompt_seq)
        for seq in _ingest_assistant_stream_only(root_id):
            _publish_written(root_id, seq)

        node_upserts = [f for f in received if isinstance(f, NodeUpsert)]
        assert node_upserts, (
            "expected live NodeUpsert frames for a completed turn's "
            "prompt + assistant response, got none"
        )
        prompt_upserts = [f for f in node_upserts if f.node.node_id == user_msg_id]
        assert prompt_upserts, node_upserts
    finally:
        sub.close()

    # A full cold replay (open_session -> _all_turns -> _segment_turns
    # over every persisted row) should also find the turn — matching what
    # the REST snapshot returns after the real turn completes.
    reopened = adapter.open_session(root_id)
    assert isinstance(reopened, Ok), reopened
    assert reopened.value.turns != (), (
        "expected a non-empty turns tuple on replay, got ()"
    )
    turn = reopened.value.turns[0]
    assert turn.prompt is not None
    assert turn.prompt.node_id == user_msg_id
    assert turn.prompt.payload.text == "hello"


def test_journaling_the_user_prompt_row_fixes_the_projection() -> None:
    """Counterpart proving the MINIMAL shape of the missing row: once ANY
    `TYPED_PROMPT`-shaped row (matching `normalize._handle_user`, no
    uuid/origin/ownership needed) is journaled BEFORE the assistant-stream
    rows, the identical assistant-only stream from the test above renders
    correctly: NodeUpsert frames broadcast live, and the turn is present
    on replay."""
    root_id = f"root-{uuid.uuid4().hex}"

    adapter = ChatSurfaceAdapter()
    adapter.bind()

    opened = adapter.open_session(root_id)
    assert isinstance(opened, Ok), opened
    identity = opened.snapshot

    received: list[object] = []
    cursor = SurfaceCursor(surface_id=root_id, incarnation=identity.incarnation, render_rev=identity.render_rev)
    sub = adapter.subscribe((cursor,), Focus.OPENED, received.append)
    try:
        prompt_seq = _ingest_user_prompt(root_id, "hello")
        _publish_written(root_id, prompt_seq)
        for seq in _ingest_assistant_stream_only(root_id):
            _publish_written(root_id, seq)

        node_upserts = [f for f in received if isinstance(f, NodeUpsert)]
        assert node_upserts, "expected live NodeUpsert frames once the prompt row is journaled"
    finally:
        sub.close()

    reopened = adapter.open_session(root_id)
    assert isinstance(reopened, Ok), reopened
    assert len(reopened.value.turns) == 1, reopened.value.turns
    turn = reopened.value.turns[0]
    assert turn.prompt is not None
    assert turn.prompt.payload.text == "hello"
    assert len(turn.results) == 1
    assert turn.results[0].payload.text == "hi there"


def _turn_prompt(adapter: "ChatSurfaceAdapter", root_id: str):
    reopened = adapter.open_session(root_id)
    assert isinstance(reopened, Ok), reopened
    assert len(reopened.value.turns) == 1, reopened.value.turns
    turn = reopened.value.turns[0]
    assert turn.prompt is not None
    return turn.prompt


def test_echo_row_live_does_not_open_a_second_turn() -> None:
    """Convergence scenario 1/3 — LIVE ONLY plus a later live echo: once
    the canonical row has opened the turn, a provider-transcript echo of
    the SAME prompt (different uuid, no origin, no ownership msg_id —
    `_ingest_provider_echo_row`) arriving on the SAME live subscription
    must NOT open a second turn or emit a second TYPED_PROMPT node; the
    turn's prompt stays exactly the canonical node."""
    root_id = f"root-{uuid.uuid4().hex}"
    user_msg_id = f"user-{uuid.uuid4().hex}"
    assistant_msg_id = f"asst-{uuid.uuid4().hex}"

    adapter = ChatSurfaceAdapter()
    adapter.bind()
    opened = adapter.open_session(root_id)
    assert isinstance(opened, Ok), opened
    identity = opened.snapshot

    received: list[object] = []
    cursor = SurfaceCursor(surface_id=root_id, incarnation=identity.incarnation, render_rev=identity.render_rev)
    sub = adapter.subscribe((cursor,), Focus.OPENED, received.append)
    try:
        prompt_seq = _ingest_typed_prompt_journal_row(
            root_id, user_msg_id=user_msg_id, assistant_msg_id=assistant_msg_id, text="hello",
        )
        _publish_written(root_id, prompt_seq)

        echo_seq = _ingest_provider_echo_row(
            root_id, echo_uuid=f"echo-{uuid.uuid4().hex}", text="hello",
        )
        _publish_written(root_id, echo_seq)

        for seq in _ingest_assistant_stream_only(root_id):
            _publish_written(root_id, seq)

        prompt_upserts = [
            f for f in received if isinstance(f, NodeUpsert) and f.node.kind == NodeKind.TYPED_PROMPT
        ]
        assert len(prompt_upserts) == 1, prompt_upserts
        assert prompt_upserts[0].node.node_id == user_msg_id
    finally:
        sub.close()

    turn_prompt = _turn_prompt(adapter, root_id)
    assert turn_prompt.node_id == user_msg_id
    assert turn_prompt.payload.text == "hello"


def test_echo_row_cold_reload_converges_to_same_single_prompt_node() -> None:
    """Convergence scenario 2/3 — COLD RELOAD of the SAME rows (canonical
    + echo, both journaled, no live handler ever involved): a fresh
    adapter's `open_session` -> `_all_turns` -> `_segment_turns` full
    rescan must ALSO find exactly one turn, anchored on the canonical
    node_id — proving the dedup is a property of the journal rows
    themselves, not of live-handler bookkeeping."""
    root_id = f"root-{uuid.uuid4().hex}"
    user_msg_id = f"user-{uuid.uuid4().hex}"
    assistant_msg_id = f"asst-{uuid.uuid4().hex}"

    _ingest_typed_prompt_journal_row(
        root_id, user_msg_id=user_msg_id, assistant_msg_id=assistant_msg_id, text="hello",
    )
    _ingest_provider_echo_row(root_id, echo_uuid=f"echo-{uuid.uuid4().hex}", text="hello")
    for _ in _ingest_assistant_stream_only(root_id):
        pass

    reload_adapter = ChatSurfaceAdapter()
    turn_prompt = _turn_prompt(reload_adapter, root_id)
    assert turn_prompt.node_id == user_msg_id
    assert turn_prompt.payload.text == "hello"


def test_live_only_and_live_plus_echo_and_cold_reload_agree_byte_identical() -> None:
    """Convergence scenario 3/3 — the full three-way proof the task
    requires: (a) live-only (no echo ever arrives — e.g. a non-native
    provider), (b) live + echo replay (the native-mode steady state), and
    (c) a cold reload of (b)'s rows with no live handler involved, all
    resolve to the exact SAME `typed_prompt` Node — same node_id, kind,
    turn_id, and payload."""
    user_msg_id = f"user-{uuid.uuid4().hex}"
    assistant_msg_id = f"asst-{uuid.uuid4().hex}"

    # (a) Live only, no echo.
    root_a = f"root-a-{uuid.uuid4().hex}"
    adapter_a = ChatSurfaceAdapter()
    adapter_a.bind()
    opened_a = adapter_a.open_session(root_a)
    assert isinstance(opened_a, Ok), opened_a
    cursor_a = SurfaceCursor(
        surface_id=root_a, incarnation=opened_a.snapshot.incarnation, render_rev=opened_a.snapshot.render_rev,
    )
    received_a: list[object] = []
    sub_a = adapter_a.subscribe((cursor_a,), Focus.OPENED, received_a.append)
    try:
        seq = _ingest_typed_prompt_journal_row(
            root_a, user_msg_id=user_msg_id, assistant_msg_id=assistant_msg_id, text="hello",
        )
        _publish_written(root_a, seq)
        node_a = next(
            f.node for f in received_a
            if isinstance(f, NodeUpsert) and f.node.node_id == user_msg_id
        )
    finally:
        sub_a.close()

    # (b) Live + echo replay.
    root_b = f"root-b-{uuid.uuid4().hex}"
    adapter_b = ChatSurfaceAdapter()
    adapter_b.bind()
    opened_b = adapter_b.open_session(root_b)
    assert isinstance(opened_b, Ok), opened_b
    cursor_b = SurfaceCursor(
        surface_id=root_b, incarnation=opened_b.snapshot.incarnation, render_rev=opened_b.snapshot.render_rev,
    )
    received_b: list[object] = []
    sub_b = adapter_b.subscribe((cursor_b,), Focus.OPENED, received_b.append)
    try:
        seq = _ingest_typed_prompt_journal_row(
            root_b, user_msg_id=user_msg_id, assistant_msg_id=assistant_msg_id, text="hello",
        )
        _publish_written(root_b, seq)
        echo_seq = _ingest_provider_echo_row(root_b, echo_uuid=f"echo-{uuid.uuid4().hex}", text="hello")
        _publish_written(root_b, echo_seq)
        node_b = next(
            f.node for f in received_b
            if isinstance(f, NodeUpsert) and f.node.node_id == user_msg_id
        )
    finally:
        sub_b.close()

    # (c) Cold reload of (b)'s exact rows — fresh adapter, no live handler.
    reload_adapter = ChatSurfaceAdapter()
    node_c = _turn_prompt(reload_adapter, root_b)

    for other in (node_b, node_c):
        assert node_a.node_id == other.node_id == user_msg_id
        assert node_a.kind == other.kind
        assert node_a.turn_id == other.turn_id
        assert node_a.payload == other.payload


_TESTS = [
    test_real_turn_write_path_renders_after_prompt_journaling_fix,
    test_journaling_the_user_prompt_row_fixes_the_projection,
    test_echo_row_live_does_not_open_a_second_turn,
    test_echo_row_cold_reload_converges_to_same_single_prompt_node,
    test_live_only_and_live_plus_echo_and_cold_reload_agree_byte_identical,
]


def _run_standalone() -> int:
    failures = 0
    for fn in _TESTS:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {exc}")
    return failures


if __name__ == "__main__":
    sys.exit(1 if _run_standalone() else 0)
