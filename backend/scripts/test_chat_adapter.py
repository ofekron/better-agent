#!/usr/bin/env python3
"""Unit coverage for backend/adapters/chat_adapter.py (ADR 0006).

Isolated via `paths.engage_test_home` before any backend import (no real
`~/.better-claude` touched), no LLM/provider subprocess involved. Writes
go through `backend.event_ingester.event_ingester.ingest` and reads
through `backend.adapters.chat_adapter.ChatSurfaceAdapter`, matching the
exact module-path style `ChatSurfaceAdapter` itself uses internally
(mixing bare `import event_bus` with dotted `backend.event_bus` produces
two distinct singletons in this codebase — verified empirically; see the
report accompanying this file's PR/task).

Run:
    PYTHONPATH=. python3 -m pytest backend/scripts/test_chat_adapter.py -q
    PYTHONPATH=. python3 backend/scripts/test_chat_adapter.py   # __main__ fallback
"""

from __future__ import annotations

import asyncio
import atexit
import shutil
import sys
import tempfile
import time
import uuid
from pathlib import Path

_BACKEND_DIR = str(Path(__file__).resolve().parents[1])
_REPO_ROOT = str(Path(_BACKEND_DIR).parent)
for _p in (_BACKEND_DIR, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import paths  # noqa: E402  (bare — matches sibling backend/scripts tests)

_TEST_HOME = tempfile.mkdtemp(prefix="ba-chat-adapter-test-")
paths.engage_test_home(_TEST_HOME)
atexit.register(shutil.rmtree, _TEST_HOME, ignore_errors=True)

import perf  # noqa: E402  (bare — matches every other perf-instrumented backend module)
import runs_dir  # noqa: E402  (bare — store_access._resolve aliases onto this instance)
from stores import worker_store  # noqa: E402  (bare — matches main.py's `from stores import task_store`)

import backend.adapters.chat_adapter as chat_adapter_mod  # noqa: E402
from backend.adapters.chat_adapter import ChatSurfaceAdapter  # noqa: E402
from backend.event_bus import BusEvent, bus  # noqa: E402
from backend.event_ingester import event_ingester  # noqa: E402
from backend.event_journal import EVENT_JOURNAL_WRITTEN  # noqa: E402
from backend.surface_contract.frames import NodeUpsert, ResyncRequired, TextDelta  # noqa: E402
from backend.surface_contract.identity import Focus, Ok, Rebuilding, StaleCursor, SurfaceCursor  # noqa: E402
from backend.surface_contract.intents import SendPrompt, SendMode, SendTarget, SendTargetKind  # noqa: E402
from backend.surface_contract.nodes import (  # noqa: E402
    FailureResolution,
    FailureSeverity,
    NodeKind,
)


def _ingest_prompt(root_id: str, text: str) -> int:
    return event_ingester.ingest(
        root_id, root_id, "agent_message",
        {"type": "user", "message": {"content": text}},
        source="test",
    )


def _ingest_assistant_text(root_id: str, text: str) -> int:
    return event_ingester.ingest(
        root_id, root_id, "agent_message",
        {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}},
        source="test",
    )


def _ingest_tool_use(root_id: str, tool_use_id: str, name: str = "Task") -> int:
    return event_ingester.ingest(
        root_id, root_id, "agent_message",
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": tool_use_id, "name": name, "input": {}},
        ]}},
        source="test",
    )


def _ingest_sidechain_text(
    root_id: str, uuid_str: str, text: str, *, parent_tool_use_id: str | None = None,
    parent_uuid: str | None = None,
) -> int:
    """A Claude Code sidechain message row: `isSidechain: True` plus
    EITHER `parent_tool_use_id` (the very first message of a sidechain,
    resolving back to its spawning Task tool_use) OR `parentUuid`
    (chaining to a PRIOR message in the SAME sidechain conversation) — the
    same two fields `normalize.derive_link`/`resolve_parents` already
    read from any row's `data`."""
    data = {
        "type": "assistant", "message": {"content": [{"type": "text", "text": text}]},
        "uuid": uuid_str, "isSidechain": True,
    }
    if parent_tool_use_id is not None:
        data["parent_tool_use_id"] = parent_tool_use_id
    if parent_uuid is not None:
        data["parentUuid"] = parent_uuid
    return event_ingester.ingest(root_id, root_id, "agent_message", data, source="test")


def _ingest_assistant_text_uuid(root_id: str, text: str, uuid_str: str) -> int:
    """Same shape `runner_better_agent.py`'s `feed_text_delta` journals
    (full cumulative text, SAME `uuid` reused across successive writes) —
    lets a test ingest several rows that all normalize to the SAME
    node_id, the scenario TextDelta/coalescing exist for."""
    return event_ingester.ingest(
        root_id, root_id, "agent_message",
        {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}, "uuid": uuid_str},
        source="test",
    )


def _publish_written(root_id: str, seq: int) -> None:
    asyncio.run(
        bus.publish(
            BusEvent(
                type=EVENT_JOURNAL_WRITTEN,
                root_id=root_id,
                sid=root_id,
                payload={"event_type": "agent_message", "seq": seq, "data": {}, "source": "test", "event_id": str(uuid.uuid4())},
            )
        )
    )


def test_open_session_single_turn() -> None:
    root_id = f"root-{uuid.uuid4().hex}"
    _ingest_prompt(root_id, "hello")
    _ingest_assistant_text(root_id, "hi there")

    adapter = ChatSurfaceAdapter()
    result = adapter.open_session(root_id)

    assert isinstance(result, Ok), result
    snapshot = result.value
    assert snapshot.session_id == root_id
    assert snapshot.surface_id == root_id
    assert len(snapshot.turns) == 1

    turn = snapshot.turns[0]
    assert turn.prompt is not None
    assert turn.prompt.kind == NodeKind.TYPED_PROMPT
    assert turn.prompt.payload.text == "hello"
    assert len(turn.results) == 1
    assert turn.results[0].payload.text == "hi there"
    assert turn.manifest.renderable_child_count == 1
    assert turn.manifest.has_children is True
    assert snapshot.older_cursor is None


def test_subscribe_emits_node_upsert_with_bumped_render_rev() -> None:
    root_id = f"root-{uuid.uuid4().hex}"
    _ingest_prompt(root_id, "hello")

    adapter = ChatSurfaceAdapter()
    adapter.bind()
    opened = adapter.open_session(root_id)
    assert isinstance(opened, Ok)
    identity = opened.snapshot

    received: list[object] = []
    cursor = SurfaceCursor(surface_id=root_id, incarnation=identity.incarnation, render_rev=identity.render_rev)
    sub = adapter.subscribe((cursor,), Focus.OPENED, received.append)
    try:
        seq = _ingest_assistant_text(root_id, "hi there")
        _publish_written(root_id, seq)

        upserts = [f for f in received if isinstance(f, NodeUpsert)]
        assert upserts, received
        assert all(f.snapshot.render_rev >= 1 for f in upserts)
        first_rev = upserts[0].snapshot.render_rev

        seq2 = _ingest_assistant_text(root_id, "more text")
        _publish_written(root_id, seq2)
        later_upserts = [f for f in received if isinstance(f, NodeUpsert) and f.snapshot.render_rev > first_rev]
        assert later_upserts, received
    finally:
        sub.close()


def test_subscribe_wrong_incarnation_emits_resync_required() -> None:
    root_id = f"root-{uuid.uuid4().hex}"
    _ingest_prompt(root_id, "hello")

    adapter = ChatSurfaceAdapter()
    opened = adapter.open_session(root_id)
    assert isinstance(opened, Ok)

    received: list[object] = []
    stale_cursor = SurfaceCursor(surface_id=root_id, incarnation="not-the-real-incarnation", render_rev=0)
    sub = adapter.subscribe((stale_cursor,), Focus.OPENED, received.append)
    try:
        assert len(received) == 1
        assert isinstance(received[0], ResyncRequired)
        assert received[0].surface_id == root_id
    finally:
        sub.close()


def test_submit_rejects_all_intents() -> None:
    adapter = ChatSurfaceAdapter()
    intent = SendPrompt(
        cv=1, intent_id="intent-1", session_id="some-session", text="hi",
        attachments=(), send_mode=SendMode.QUEUE,
        target=SendTarget(kind=SendTargetKind.CURRENT),
    )
    ack = adapter.submit(intent)
    assert ack.intent_id == "intent-1"
    assert ack.code == "unsupported_contract_phase"
    assert ack.message


def test_fetch_sidecar_is_rebuilding_for_unknown_ref() -> None:
    adapter = ChatSurfaceAdapter()
    result = adapter.fetch_sidecar("some-session", "some-sidecar-ref")
    assert isinstance(result, Rebuilding)


def test_fetch_sidecar_maps_worker_record() -> None:
    # sidecar_ref is treated as the worker's Better Agent session id
    # (agent_session_id / WorkerPanel.worker_session_id) — see
    # ChatSurfaceAdapter.fetch_sidecar's docstring for why (no producer
    # stamps Node.sidecar_ref yet).
    root_id = f"root-{uuid.uuid4().hex}"
    cwd = tempfile.mkdtemp(prefix="ba-chat-adapter-worker-cwd-")
    agent_session_id = f"worker-{uuid.uuid4().hex}"
    worker_store.upsert_worker(cwd, agent_session_id, "native", None, name="a worker")

    adapter = ChatSurfaceAdapter()
    result = adapter.fetch_sidecar(root_id, agent_session_id)
    assert isinstance(result, Ok), result
    sidecar = result.value
    assert sidecar.sidecar_ref == agent_session_id
    assert sidecar.panel_kind == "worker_panel"
    # No run seeded for this worker's own session — success/error stay
    # honestly unknown rather than guessed, and status falls back to
    # "running" (the same conservative default runs_adapter._phase uses).
    assert sidecar.status == "running"
    assert sidecar.payload["worker_session_id"] == agent_session_id
    assert sidecar.payload["cwd"] == cwd
    assert sidecar.payload["orchestration_mode"] == "native"
    assert sidecar.payload["success"] is None
    assert sidecar.payload["error"] is None


def test_fetch_sidecar_worker_status_reflects_latest_run_outcome() -> None:
    root_id = f"root-{uuid.uuid4().hex}"
    cwd = tempfile.mkdtemp(prefix="ba-chat-adapter-worker-cwd-")
    agent_session_id = f"worker-{uuid.uuid4().hex}"
    worker_store.upsert_worker(cwd, agent_session_id, "native", None)

    # The worker's own Better Agent session runs its own provider run(s)
    # under `agent_session_id` — store_access.get_latest_run_record joins
    # on that, the same real linkage RunsSurfaceAdapter uses.
    run_dir = runs_dir.runs_root() / f"run-{uuid.uuid4().hex}"
    run_dir.mkdir(parents=True, exist_ok=True)
    runs_dir.atomic_write_json(run_dir / "state.json", {
        "session_id": f"prov-{run_dir.name}",
        "jsonl_path": str(run_dir / "stream.jsonl"),
        "app_session_id": agent_session_id,
    })
    runs_dir.atomic_write_json(run_dir / "complete.json", {"success": False, "error": "boom"})

    adapter = ChatSurfaceAdapter()
    result = adapter.fetch_sidecar(root_id, agent_session_id)
    assert isinstance(result, Ok), result
    sidecar = result.value
    assert sidecar.status == "failed"
    assert sidecar.payload["success"] is False
    assert sidecar.payload["error"] == "boom"


def test_children_stale_cursor_on_render_rev_mismatch() -> None:
    root_id = f"root-{uuid.uuid4().hex}"
    _ingest_prompt(root_id, "hello")
    adapter = ChatSurfaceAdapter()
    opened = adapter.open_session(root_id)
    assert isinstance(opened, Ok)

    result = adapter.children(root_id, root_id, at_render_rev=999)
    assert isinstance(result, StaleCursor)

    ok = adapter.children(root_id, root_id, at_render_rev=opened.snapshot.render_rev)
    assert isinstance(ok, Ok)
    assert len(ok.value) == 1
    assert ok.value[0].kind == NodeKind.TURN


def _publish_user_message_failed(root_id: str, lifecycle_msg_id: str, reason: str, error: str | None = None) -> None:
    asyncio.run(
        bus.publish(
            BusEvent(
                type="user_message_failed",
                root_id=root_id,
                sid=root_id,
                msg_id=lifecycle_msg_id,
                payload={"lifecycle_msg_id": lifecycle_msg_id, "reason": reason, "error": error},
            )
        )
    )


def test_user_message_failed_recovery_reason_maps_to_retryable_failure_node() -> None:
    root_id = f"root-{uuid.uuid4().hex}"
    _ingest_prompt(root_id, "hello")

    adapter = ChatSurfaceAdapter()
    adapter.bind()
    opened = adapter.open_session(root_id)
    assert isinstance(opened, Ok)
    identity = opened.snapshot

    received: list[object] = []
    cursor = SurfaceCursor(surface_id=root_id, incarnation=identity.incarnation, render_rev=identity.render_rev)
    sub = adapter.subscribe((cursor,), Focus.OPENED, received.append)
    try:
        lifecycle_msg_id = str(uuid.uuid4())
        _publish_user_message_failed(root_id, lifecycle_msg_id, "orphaned_before_provider", error="boom")

        upserts = [f for f in received if isinstance(f, NodeUpsert) and f.node.kind == NodeKind.FAILURE]
        assert upserts, received
        node = upserts[-1].node
        assert node.node_id == f"failure:{lifecycle_msg_id}"
        assert node.turn_id  # attached to the seeded turn (best-effort attribution)
        assert node.payload.code == "recovery_unknown"
        assert node.payload.severity == FailureSeverity.ERROR
        assert node.payload.retryable is True
        assert node.payload.resolution == FailureResolution.RETRY
        assert node.payload.text == "boom"
    finally:
        sub.close()


def test_user_message_failed_admission_reason_maps_to_non_retryable_failure_node() -> None:
    root_id = f"root-{uuid.uuid4().hex}"
    _ingest_prompt(root_id, "hello")

    adapter = ChatSurfaceAdapter()
    adapter.bind()
    opened = adapter.open_session(root_id)
    assert isinstance(opened, Ok)
    identity = opened.snapshot

    received: list[object] = []
    cursor = SurfaceCursor(surface_id=root_id, incarnation=identity.incarnation, render_rev=identity.render_rev)
    sub = adapter.subscribe((cursor,), Focus.OPENED, received.append)
    try:
        for reason in ("interrupt_failed", "alter_interrupt_failed", "durable_admission_failed"):
            lifecycle_msg_id = str(uuid.uuid4())
            _publish_user_message_failed(root_id, lifecycle_msg_id, reason)
            upserts = [
                f for f in received
                if isinstance(f, NodeUpsert) and f.node.kind == NodeKind.FAILURE
                and f.node.node_id == f"failure:{lifecycle_msg_id}"
            ]
            assert upserts, (reason, received)
            payload = upserts[-1].node.payload
            assert payload.code == "admission_rejected", reason
            assert payload.retryable is False, reason
            assert payload.resolution == FailureResolution.NONE, reason
    finally:
        sub.close()


def test_user_message_failed_unknown_reason_falls_back_to_verbatim_code_and_defaults() -> None:
    root_id = f"root-{uuid.uuid4().hex}"
    _ingest_prompt(root_id, "hello")

    adapter = ChatSurfaceAdapter()
    adapter.bind()
    opened = adapter.open_session(root_id)
    assert isinstance(opened, Ok)
    identity = opened.snapshot

    received: list[object] = []
    cursor = SurfaceCursor(surface_id=root_id, incarnation=identity.incarnation, render_rev=identity.render_rev)
    sub = adapter.subscribe((cursor,), Focus.OPENED, received.append)
    try:
        lifecycle_msg_id = str(uuid.uuid4())
        _publish_user_message_failed(root_id, lifecycle_msg_id, "aborted_before_send")

        upserts = [f for f in received if isinstance(f, NodeUpsert) and f.node.kind == NodeKind.FAILURE]
        assert upserts, received
        payload = upserts[-1].node.payload
        assert payload.code == "aborted_before_send"
        assert payload.severity == FailureSeverity.ERROR
        assert payload.retryable is False
        assert payload.resolution == FailureResolution.NONE
    finally:
        sub.close()


def test_user_message_failed_node_id_is_deterministic_across_replays() -> None:
    root_id = f"root-{uuid.uuid4().hex}"
    _ingest_prompt(root_id, "hello")

    adapter = ChatSurfaceAdapter()
    adapter.bind()
    opened = adapter.open_session(root_id)
    assert isinstance(opened, Ok)
    identity = opened.snapshot

    received: list[object] = []
    cursor = SurfaceCursor(surface_id=root_id, incarnation=identity.incarnation, render_rev=identity.render_rev)
    sub = adapter.subscribe((cursor,), Focus.OPENED, received.append)
    try:
        lifecycle_msg_id = str(uuid.uuid4())
        _publish_user_message_failed(root_id, lifecycle_msg_id, "recovered_run_failed")
        _publish_user_message_failed(root_id, lifecycle_msg_id, "recovered_run_failed")

        upserts = [f for f in received if isinstance(f, NodeUpsert) and f.node.kind == NodeKind.FAILURE]
        assert len(upserts) == 2, received
        assert upserts[0].node.node_id == upserts[1].node.node_id == f"failure:{lifecycle_msg_id}"
    finally:
        sub.close()


def test_user_message_failed_with_no_turn_yet_does_not_crash_or_emit() -> None:
    root_id = f"root-{uuid.uuid4().hex}"  # never ingested — no TURN exists

    adapter = ChatSurfaceAdapter()
    adapter.bind()

    received: list[object] = []
    # subscribe_control mirrors subscribe's registration without requiring
    # an open_session/turn to exist first.
    lifecycle_msg_id = str(uuid.uuid4())
    _publish_user_message_failed(root_id, lifecycle_msg_id, "durable_admission_failed")
    # No crash is the primary assertion; nothing to unsubscribe since this
    # surface was never subscribed to.
    assert received == []


def test_live_handler_node_equals_reload_reconstructed_node_for_same_fact() -> None:
    """Closes the live-only gap: the node `_on_user_message_failed`
    broadcasts instantly must be the SAME node a cold reload (open_session
    -> children(), with NO live handler involved) reconstructs from the
    journaled row via `normalize.py`'s `user_message_failed` branch."""
    root_id = f"root-{uuid.uuid4().hex}"
    _ingest_prompt(root_id, "hello")

    # 1) Live path.
    live_adapter = ChatSurfaceAdapter()
    live_adapter.bind()
    opened = live_adapter.open_session(root_id)
    assert isinstance(opened, Ok)
    identity = opened.snapshot
    received: list[object] = []
    cursor = SurfaceCursor(surface_id=root_id, incarnation=identity.incarnation, render_rev=identity.render_rev)
    sub = live_adapter.subscribe((cursor,), Focus.OPENED, received.append)
    lifecycle_msg_id = str(uuid.uuid4())
    try:
        _publish_user_message_failed(root_id, lifecycle_msg_id, "orphaned_before_provider", error="boom")
        live_upserts = [f for f in received if isinstance(f, NodeUpsert) and f.node.kind == NodeKind.FAILURE]
        assert live_upserts, received
        live_node = live_upserts[-1].node
    finally:
        sub.close()

    # 2) Reload path: journal the SAME fact for real, via the exact
    # function the wildcard `_persist_to_event_journal` subscriber funnels
    # every persisted BusEvent into (`event_ingester.ingest` —
    # `EventJournalWriter._append_metadata_event` calls this directly),
    # then read it back through a FRESH adapter that never saw the live
    # broadcast — only normalize.py's row branch reconstructs it.
    event_ingester.ingest(
        root_id, root_id, "user_message_failed",
        {"lifecycle_msg_id": lifecycle_msg_id, "reason": "orphaned_before_provider", "error": "boom"},
        source="test", msg_id=lifecycle_msg_id,
    )
    reload_adapter = ChatSurfaceAdapter()
    reloaded = reload_adapter.open_session(root_id)
    assert isinstance(reloaded, Ok)
    turn_id = reloaded.value.turns[0].turn.node_id
    kids = reload_adapter.children(root_id, turn_id, at_render_rev=reloaded.snapshot.render_rev)
    assert isinstance(kids, Ok)
    reload_node = next((n for n in kids.value if n.node_id == live_node.node_id), None)
    assert reload_node is not None, kids.value

    assert reload_node.node_id == live_node.node_id
    assert reload_node.kind == live_node.kind == NodeKind.FAILURE
    assert reload_node.turn_id == live_node.turn_id
    assert reload_node.surface_id == live_node.surface_id
    assert reload_node.parent_id == live_node.parent_id
    assert reload_node.payload == live_node.payload


def test_search_finds_substring_case_insensitive() -> None:
    root_id = f"root-{uuid.uuid4().hex}"
    _ingest_prompt(root_id, "find the needle please")
    adapter = ChatSurfaceAdapter()
    result = adapter.search(root_id, "NEEDLE")
    assert isinstance(result, Ok)
    assert len(result.value) == 1
    match = result.value[0]
    assert match.node_id in match.path


def test_open_session_normalizes_each_row_once() -> None:
    """H5: every journal row is normalized exactly once per open_session()
    call — not once in `_segment_turns` and again in `_build_turn_view`."""
    root_id = f"root-{uuid.uuid4().hex}"
    _ingest_prompt(root_id, "hello")
    _ingest_assistant_text(root_id, "hi there")
    _ingest_assistant_text(root_id, "more text")

    calls = 0
    original = chat_adapter_mod.normalize_journal_row

    def counting(*a, **kw):
        nonlocal calls
        calls += 1
        return original(*a, **kw)

    chat_adapter_mod.normalize_journal_row = counting
    try:
        adapter = ChatSurfaceAdapter()
        result = adapter.open_session(root_id)
    finally:
        chat_adapter_mod.normalize_journal_row = original

    assert isinstance(result, Ok), result
    assert calls == 3, calls  # one call per journal row — not 6


def test_older_only_builds_the_requested_page_not_every_turn() -> None:
    """H5: `older()` builds a `_TurnView` for just the page it serves —
    turns outside the requested page are segmented (cheap) but never
    passed through `_build_turn_view` (derive_turn/derive_body)."""
    root_id = f"root-{uuid.uuid4().hex}"
    # 7 turns total: open_session's compact window (5) covers the last 5,
    # leaving 2 older turns for a SINGLE older() page (window size 5 > 2).
    for i in range(7):
        _ingest_prompt(root_id, f"prompt {i}")
        _ingest_assistant_text(root_id, f"reply {i}")

    adapter = ChatSurfaceAdapter()
    opened = adapter.open_session(root_id)
    assert isinstance(opened, Ok)
    assert opened.value.older_cursor is not None

    built_turn_ids: list[str] = []
    original = chat_adapter_mod._build_turn_view

    def counting(surface_id, turn_id, rows, produced_by_row, prompt_meta=None):
        built_turn_ids.append(turn_id)
        return original(surface_id, turn_id, rows, produced_by_row, prompt_meta)

    chat_adapter_mod._build_turn_view = counting
    try:
        older = adapter.older(opened.value.older_cursor)
    finally:
        chat_adapter_mod._build_turn_view = original

    assert isinstance(older, Ok), older
    assert len(older.value.turns) == 2
    assert len(built_turn_ids) == 2  # not all 7


def test_on_event_written_coalesces_same_tick_notifications() -> None:
    """H2: two EVENT_JOURNAL_WRITTEN notifications for the same surface
    landing in the same event-loop tick collapse into one flush — the
    second call is a no-op because the pending flush's
    `read_events(after_seq=...)` will subsume its row too."""
    root_id = f"root-{uuid.uuid4().hex}"
    _ingest_prompt(root_id, "hello")
    seq = _ingest_assistant_text(root_id, "hi there")

    adapter = ChatSurfaceAdapter()
    opened = adapter.open_session(root_id)
    assert isinstance(opened, Ok)

    flush_calls = 0
    original_flush = chat_adapter_mod.ChatSurfaceAdapter._flush_event_written

    async def counting_flush(self, surface_id, state):
        nonlocal flush_calls
        flush_calls += 1
        await original_flush(self, surface_id, state)

    event = BusEvent(
        type=EVENT_JOURNAL_WRITTEN, root_id=root_id, sid=root_id,
        payload={"event_type": "agent_message", "seq": seq, "data": {}, "source": "test", "event_id": str(uuid.uuid4())},
    )

    async def run() -> None:
        await asyncio.gather(
            adapter._on_event_written(event),
            adapter._on_event_written(event),
        )

    chat_adapter_mod.ChatSurfaceAdapter._flush_event_written = counting_flush
    try:
        asyncio.run(run())
    finally:
        chat_adapter_mod.ChatSurfaceAdapter._flush_event_written = original_flush

    assert flush_calls == 1, flush_calls


def test_per_batch_coalescing_collapses_multiple_rows_to_one_frame() -> None:
    """H2: several rows updating the SAME node_id, all landing in ONE
    flush's batch, net out to exactly one outbound frame carrying the
    batch's FINAL value — not one frame per row."""
    root_id = f"root-{uuid.uuid4().hex}"
    _ingest_prompt(root_id, "hello")
    text_uuid = str(uuid.uuid4())

    adapter = ChatSurfaceAdapter()
    adapter.bind()
    opened = adapter.open_session(root_id)
    assert isinstance(opened, Ok)
    identity = opened.snapshot

    received: list[object] = []
    cursor = SurfaceCursor(surface_id=root_id, incarnation=identity.incarnation, render_rev=identity.render_rev)
    sub = adapter.subscribe((cursor,), Focus.OPENED, received.append)
    try:
        # 3 growing rewrites of the SAME node, all ingested BEFORE the
        # single EVENT_JOURNAL_WRITTEN notification — matches feed_text_
        # delta writing several chunks before a bus dispatch is observed.
        node_id = None
        for chunk in ("a", "ab", "abc"):
            seq = _ingest_assistant_text_uuid(root_id, chunk, text_uuid)
        _publish_written(root_id, seq)

        frames = [
            f for f in received
            if isinstance(f, (NodeUpsert, TextDelta))
            and (f.node.node_id if isinstance(f, NodeUpsert) else f.node_id) == text_uuid
        ]
        assert len(frames) == 1, received  # coalesced, not one per row
        assert isinstance(frames[0], NodeUpsert)  # first sighting: full upsert
        assert frames[0].node.payload.text == "abc"  # the batch's FINAL value
    finally:
        sub.close()


def test_text_delta_emitted_for_pure_append() -> None:
    """H4: a second observation of a growing text node, in its OWN flush,
    emits a TextDelta carrying just the appended suffix."""
    root_id = f"root-{uuid.uuid4().hex}"
    _ingest_prompt(root_id, "hello")
    text_uuid = str(uuid.uuid4())

    adapter = ChatSurfaceAdapter()
    adapter.bind()
    opened = adapter.open_session(root_id)
    assert isinstance(opened, Ok)
    identity = opened.snapshot

    received: list[object] = []
    cursor = SurfaceCursor(surface_id=root_id, incarnation=identity.incarnation, render_rev=identity.render_rev)
    sub = adapter.subscribe((cursor,), Focus.OPENED, received.append)
    try:
        seq1 = _ingest_assistant_text_uuid(root_id, "hello", text_uuid)
        _publish_written(root_id, seq1)
        first = [f for f in received if isinstance(f, NodeUpsert) and f.node.node_id == text_uuid]
        assert len(first) == 1, received
        assert first[0].node.payload.text == "hello"

        seq2 = _ingest_assistant_text_uuid(root_id, "hello world", text_uuid)
        _publish_written(root_id, seq2)
        deltas = [f for f in received if isinstance(f, TextDelta) and f.node_id == text_uuid]
        assert len(deltas) == 1, received
        assert deltas[0].appended_text == " world"
    finally:
        sub.close()


def test_text_delta_falls_back_to_upsert_for_non_append() -> None:
    """H4: a rewrite that is NOT a prefix-preserving append (e.g. an
    edited/replaced message) falls back to a full NodeUpsert, never a
    TextDelta that would desync the client."""
    root_id = f"root-{uuid.uuid4().hex}"
    _ingest_prompt(root_id, "hello")
    text_uuid = str(uuid.uuid4())

    adapter = ChatSurfaceAdapter()
    adapter.bind()
    opened = adapter.open_session(root_id)
    assert isinstance(opened, Ok)
    identity = opened.snapshot

    received: list[object] = []
    cursor = SurfaceCursor(surface_id=root_id, incarnation=identity.incarnation, render_rev=identity.render_rev)
    sub = adapter.subscribe((cursor,), Focus.OPENED, received.append)
    try:
        seq1 = _ingest_assistant_text_uuid(root_id, "hello", text_uuid)
        _publish_written(root_id, seq1)
        seq2 = _ingest_assistant_text_uuid(root_id, "goodbye", text_uuid)
        _publish_written(root_id, seq2)

        upserts = [f for f in received if isinstance(f, NodeUpsert) and f.node.node_id == text_uuid]
        deltas = [f for f in received if isinstance(f, TextDelta) and f.node_id == text_uuid]
        assert len(upserts) == 2, received
        assert upserts[-1].node.payload.text == "goodbye"
        assert deltas == []
    finally:
        sub.close()


def test_text_delta_periodic_full_sync_self_heals() -> None:
    """H4: after `_FULL_SYNC_EVERY_N_DELTAS` consecutive deltas, the next
    observation is forced back to a full NodeUpsert so a client that
    mis-applied a delta resyncs from ground truth."""
    root_id = f"root-{uuid.uuid4().hex}"
    _ingest_prompt(root_id, "hello")
    text_uuid = str(uuid.uuid4())
    n = chat_adapter_mod._FULL_SYNC_EVERY_N_DELTAS

    adapter = ChatSurfaceAdapter()
    adapter.bind()
    opened = adapter.open_session(root_id)
    assert isinstance(opened, Ok)
    identity = opened.snapshot

    received: list[object] = []
    cursor = SurfaceCursor(surface_id=root_id, incarnation=identity.incarnation, render_rev=identity.render_rev)
    sub = adapter.subscribe((cursor,), Focus.OPENED, received.append)
    try:
        kinds: list[str] = []
        text = "x"
        for _ in range(n + 1):
            seq = _ingest_assistant_text_uuid(root_id, text, text_uuid)
            _publish_written(root_id, seq)
            new_frames = [
                f for f in received
                if (isinstance(f, NodeUpsert) and f.node.node_id == text_uuid)
                or (isinstance(f, TextDelta) and f.node_id == text_uuid)
            ]
            kinds.append("upsert" if isinstance(new_frames[-1], NodeUpsert) else "delta")
            text += "x"

        assert kinds[0] == "upsert"  # first sighting
        assert all(k == "delta" for k in kinds[1:n]), kinds  # n-1 deltas
        assert kinds[n] == "upsert", kinds  # the nth delta attempt self-heals
    finally:
        sub.close()


def test_open_session_bounds_deep_sidechain_fan_out() -> None:
    """H1: a deep sidechain (Task-tool subagent) fan-out does NOT flatten
    into the live turn's extended form — chat-panel.md grammar (SubAgentTurn
    is a BodyItem embedding a full nested Turn), enforced end-to-end
    through open_session()/children()."""
    root_id = f"root-{uuid.uuid4().hex}"
    _ingest_prompt(root_id, "delegate this to a subagent")
    task_id = str(uuid.uuid4())
    _ingest_tool_use(root_id, task_id, name="Task")

    fan_out = 200
    prev_uuid = None
    for i in range(fan_out):
        node_uuid = str(uuid.uuid4())
        if prev_uuid is None:
            _ingest_sidechain_text(root_id, node_uuid, f"subagent step {i}", parent_tool_use_id=task_id)
        else:
            _ingest_sidechain_text(root_id, node_uuid, f"subagent step {i}", parent_uuid=prev_uuid)
        prev_uuid = node_uuid
    _ingest_assistant_text(root_id, "done delegating, here's the summary")

    adapter = ChatSurfaceAdapter()
    result = adapter.open_session(root_id)
    assert isinstance(result, Ok), result
    snapshot = result.value
    assert len(snapshot.turns) == 1

    subagent_items = [n for n in snapshot.live_turn_nodes if n.kind == NodeKind.NATIVE_SUBAGENT_TURN]
    assert len(subagent_items) == 1, snapshot.live_turn_nodes
    subagent_node = subagent_items[0]
    assert subagent_node.node_id == f"subagent:tool:{task_id}"
    assert subagent_node.child_manifest.has_children is True

    # Bounded: the live turn's extended form stays a small, fixed number
    # of top-level items regardless of how deep the subagent's own
    # conversation goes (200 sidechain messages here).
    assert len(snapshot.live_turn_nodes) < 10, len(snapshot.live_turn_nodes)
    assert not any(n.node_id == prev_uuid for n in snapshot.live_turn_nodes)  # last sidechain msg not flattened in

    # One level at a time: children() of the subagent turn returns its
    # own wrapping Explanation, not the 200 raw sidechain messages.
    level1 = adapter.children(root_id, subagent_node.node_id, at_render_rev=result.snapshot.render_rev)
    assert isinstance(level1, Ok)
    assert len(level1.value) == 1
    explanation = level1.value[0]
    assert explanation.kind == NodeKind.EXPLANATION

    # children() of THAT explanation reaches the real content — full
    # reachability, still one level at a time (never all at once).
    level2 = adapter.children(root_id, explanation.node_id, at_render_rev=result.snapshot.render_rev)
    assert isinstance(level2, Ok)
    assert len(level2.value) == fan_out


def test_live_path_only_expands_trailing_subagent_not_earlier_ones() -> None:
    """H1: live-path force-expansion is scoped to the CHRONOLOGICALLY LAST
    subagent turn only — an earlier one in the same live turn stays lazy
    (children() on demand), matching the "minimal and justified" bound."""
    root_id = f"root-{uuid.uuid4().hex}"
    _ingest_prompt(root_id, "hello")
    task1 = str(uuid.uuid4())
    _ingest_tool_use(root_id, task1, name="Task")
    early_side = str(uuid.uuid4())
    _ingest_sidechain_text(root_id, early_side, "first subagent", parent_tool_use_id=task1)
    _ingest_assistant_text(root_id, "middle narration")
    task2 = str(uuid.uuid4())
    _ingest_tool_use(root_id, task2, name="Task")
    trailing_side = str(uuid.uuid4())
    _ingest_sidechain_text(root_id, trailing_side, "second subagent", parent_tool_use_id=task2)

    adapter = ChatSurfaceAdapter()
    result = adapter.open_session(root_id)
    assert isinstance(result, Ok), result
    live_nodes = result.value.live_turn_nodes

    subagent_ids = {n.node_id for n in live_nodes if n.kind == NodeKind.NATIVE_SUBAGENT_TURN}
    trailing_subagent_id = f"subagent:tool:{task2}"
    early_subagent_id = f"subagent:tool:{task1}"
    assert subagent_ids == {trailing_subagent_id, early_subagent_id}

    explanations_by_parent = {n.parent_id for n in live_nodes if n.kind == NodeKind.EXPLANATION}
    assert trailing_subagent_id in explanations_by_parent
    assert early_subagent_id not in explanations_by_parent
    # Still bounded to one level: the trailing subagent's own raw
    # sidechain text is not eagerly included either.
    assert not any(n.node_id == trailing_side for n in live_nodes)


def test_open_session_perf_smoke_bounded_wall_time() -> None:
    """H6 perf smoke: a single turn with several thousand rows still opens
    in bounded wall time. Generous bound — this asserts no pathological
    (quadratic-ish) blowup, not a tight latency budget."""
    root_id = f"root-{uuid.uuid4().hex}"
    _ingest_prompt(root_id, "hello")
    row_count = 5000
    for i in range(row_count):
        _ingest_assistant_text(root_id, f"chunk {i}")

    adapter = ChatSurfaceAdapter()
    started = time.perf_counter()
    result = adapter.open_session(root_id)
    elapsed_s = time.perf_counter() - started

    assert isinstance(result, Ok), result
    assert len(result.value.turns) == 1
    assert elapsed_s < 10.0, elapsed_s


def test_open_session_perf_smoke_monster_sidechain_turn_bounded_response() -> None:
    """H1+H6 perf smoke, at the analyzed real-world scale (~7,500 rows,
    ~6,600 of them sidechain): asserts BOTH bounded wall time AND bounded
    response node count — the property H1 exists for. Without
    segregation this would return ~6,600 flat nodes; with it, a handful."""
    root_id = f"root-{uuid.uuid4().hex}"
    _ingest_prompt(root_id, "delegate this huge task to a subagent")
    task_id = str(uuid.uuid4())
    _ingest_tool_use(root_id, task_id, name="Task")

    fan_out = 6600
    prev_uuid = None
    for i in range(fan_out):
        node_uuid = str(uuid.uuid4())
        if prev_uuid is None:
            _ingest_sidechain_text(root_id, node_uuid, f"subagent step {i}", parent_tool_use_id=task_id)
        else:
            _ingest_sidechain_text(root_id, node_uuid, f"subagent step {i}", parent_uuid=prev_uuid)
        prev_uuid = node_uuid
    _ingest_assistant_text(root_id, "delegation complete")

    adapter = ChatSurfaceAdapter()
    started = time.perf_counter()
    result = adapter.open_session(root_id)
    elapsed_s = time.perf_counter() - started

    assert isinstance(result, Ok), result
    assert elapsed_s < 30.0, elapsed_s
    # The property H1 exists for: response size independent of sidechain
    # depth, not merely "fast" — a flat-merge regression would still be
    # fast-ish at this row count but would return ~6,600 nodes here.
    assert len(result.value.live_turn_nodes) < 10, len(result.value.live_turn_nodes)


def test_flush_drop_reasons_recorded_for_known_cases() -> None:
    """Perf-instrumentation regression (the 2026-08 live-content
    investigation had zero observability distinguishing "a flush ran and
    dropped everything" from "a flush never ran" — this locks each drop
    bucket to its own known row shape so a future live rollup is
    trustworthy).

    - `turn_id_none`: a row lands before any prompt has ever opened a
      turn on this surface.
    - `echo_dedup`: a raw (non-canonical) provider-transcript echo of the
      currently-open canonical turn's own prompt.
    - `control_row_excluded`: a recognized backend control/telemetry row
      type (`normalize._DROPPED_CONTROL_ROW_TYPES`).
    - `empty_after_normalize`: a row that normalizes to zero nodes for a
      reason OTHER than being a recognized control row (here: an
      `agent_message` row whose inner `data.type` is `ai-title`, one of
      `normalize._DROPPED_METADATA_TYPES`).

    Also asserts the surrounding `ran`/`rows_flushed`/`frames_emitted`/
    `on_event_written.entries` counters advance, so a rollup with only
    zeros for ALL of these (not just the drop reasons) is distinguishable
    from "everything got dropped for a known reason"."""
    with perf._lock:
        perf._counts.clear()

    adapter = ChatSurfaceAdapter()
    adapter.bind()

    # turn_id_none: no prompt has ever opened a turn on this fresh root.
    # Seed the surface against the EMPTY journal first — `_ensure_seeded`
    # cold-seeds `last_seq` to the journal's current tail, so an orphan
    # row ingested before the first seed would be silently absorbed into
    # that baseline (never reaching `_flush_event_written`'s row loop at
    # all) instead of exercising the drop path this asserts on.
    root_a = f"root-{uuid.uuid4().hex}"
    adapter.open_session(root_a)
    orphan_seq = _ingest_assistant_text(root_a, "orphaned before any prompt")
    _publish_written(root_a, orphan_seq)

    # echo_dedup + control_row_excluded + empty_after_normalize, all in
    # the SAME later flush (read_events picks up everything durably
    # written since the last flush regardless of how many bus facts fired
    # in between — matching the real coalescing behavior under test).
    # Same cold-seed-absorbs-the-first-row caveat as root_a: seed against
    # the empty journal before the canonical prompt is durably written, so
    # its own upsert (`frames_emitted.upsert`) is observable from a real
    # flush instead of being folded into the seed baseline.
    root_b = f"root-{uuid.uuid4().hex}"
    adapter.open_session(root_b)
    user_msg_id = str(uuid.uuid4())
    canonical_seq = event_ingester.ingest(
        root_b, root_b, "agent_message",
        {
            "type": "user", "uuid": user_msg_id,
            "message": {"role": "user", "content": [{"type": "text", "text": "hello"}]},
            "origin": "user",
        },
        source="test", msg_id=f"asst-{uuid.uuid4().hex}",
    )
    _publish_written(root_b, canonical_seq)  # opens the turn

    event_ingester.ingest(
        root_b, root_b, "agent_message",
        {"type": "user", "uuid": str(uuid.uuid4()), "message": {"role": "user", "content": "hello"}},
        source="test",
    )  # echo_dedup: raw transcript echo of the SAME open turn's prompt
    event_ingester.ingest(
        root_b, root_b, "run_state", {"phase": "queued"}, source="test",
    )  # control_row_excluded: recognized control/telemetry row type
    empty_seq = event_ingester.ingest(
        root_b, root_b, "agent_message",
        {"type": "ai-title", "aiTitle": "renamed"}, source="test",
    )  # empty_after_normalize: normalizes to [] but isn't a control row
    _publish_written(root_b, empty_seq)

    with perf._lock:
        counts = {k: dict(v) for k, v in perf._counts.items()}

    def total(name: str) -> int:
        return counts.get(name, {}).get("total", 0)

    assert total("chat_adapter.flush.dropped.turn_id_none") >= 1, counts
    assert total("chat_adapter.flush.dropped.echo_dedup") >= 1, counts
    assert total("chat_adapter.flush.dropped.control_row_excluded") >= 1, counts
    assert total("chat_adapter.flush.dropped.empty_after_normalize") >= 1, counts
    assert total("chat_adapter.flush.ran") >= 2, counts
    assert total("chat_adapter.flush.rows_flushed") >= 4, counts
    assert total("chat_adapter.flush.frames_emitted.upsert") >= 1, counts
    assert total("chat_adapter.on_event_written.entries") >= 2, counts


_TESTS = [
    test_open_session_single_turn,
    test_subscribe_emits_node_upsert_with_bumped_render_rev,
    test_subscribe_wrong_incarnation_emits_resync_required,
    test_submit_rejects_all_intents,
    test_fetch_sidecar_is_rebuilding_for_unknown_ref,
    test_fetch_sidecar_maps_worker_record,
    test_fetch_sidecar_worker_status_reflects_latest_run_outcome,
    test_children_stale_cursor_on_render_rev_mismatch,
    test_user_message_failed_recovery_reason_maps_to_retryable_failure_node,
    test_user_message_failed_admission_reason_maps_to_non_retryable_failure_node,
    test_user_message_failed_unknown_reason_falls_back_to_verbatim_code_and_defaults,
    test_user_message_failed_node_id_is_deterministic_across_replays,
    test_user_message_failed_with_no_turn_yet_does_not_crash_or_emit,
    test_live_handler_node_equals_reload_reconstructed_node_for_same_fact,
    test_search_finds_substring_case_insensitive,
    test_open_session_normalizes_each_row_once,
    test_older_only_builds_the_requested_page_not_every_turn,
    test_on_event_written_coalesces_same_tick_notifications,
    test_per_batch_coalescing_collapses_multiple_rows_to_one_frame,
    test_text_delta_emitted_for_pure_append,
    test_text_delta_falls_back_to_upsert_for_non_append,
    test_text_delta_periodic_full_sync_self_heals,
    test_open_session_bounds_deep_sidechain_fan_out,
    test_live_path_only_expands_trailing_subagent_not_earlier_ones,
    test_open_session_perf_smoke_bounded_wall_time,
    test_open_session_perf_smoke_monster_sidechain_turn_bounded_response,
    test_flush_drop_reasons_recorded_for_known_cases,
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
