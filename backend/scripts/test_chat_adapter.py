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
import session_bridge  # noqa: E402  (bare — store_access._resolve aliases onto this instance)
import tool_approval  # noqa: E402  (bare — store_access._resolve aliases onto this instance)
import user_input_store  # noqa: E402  (bare — store_access._resolve aliases onto this instance)
from stores import pending_approvals, worker_store  # noqa: E402  (bare — matches main.py's `from stores import task_store`)

import backend.adapters.chat_adapter as chat_adapter_mod  # noqa: E402
import backend.adapters.chat_index as chat_index_mod  # noqa: E402
import backend.adapters.derive as derive_mod  # noqa: E402
from backend.adapters.chat_adapter import ChatSurfaceAdapter  # noqa: E402
from backend.event_bus import BusEvent, bus  # noqa: E402
from backend.event_ingester import event_ingester  # noqa: E402
from backend.event_journal import EVENT_JOURNAL_WRITTEN  # noqa: E402
from backend.surface_contract.frames import (  # noqa: E402
    NodeUpsert,
    ResyncRequired,
    TextDelta,
    TurnLifecycle,
    UserInteractionUpsert,
)
from backend.surface_contract.identity import Focus, Ok, StaleCursor, SurfaceCursor  # noqa: E402
from backend.surface_contract.intents import SendPrompt, SendMode, SendTarget, SendTargetKind  # noqa: E402
from backend.surface_contract.nodes import (  # noqa: E402
    ChildManifest,
    DELEGATE_CHOICE_REF_PREFIX,
    FailureResolution,
    FailureSeverity,
    NodeKind,
    TOOL_APPROVAL_REF_PREFIX,
    USER_INPUT_REF_PREFIX,
    UserInteractionKind,
    UserInteractionState,
    WORKER_APPROVAL_REF_PREFIX,
    interaction_fact_payload,
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


def _ingest_worker_fact(root_id: str, fact_type: str, delegation_id: str, **fields) -> int:
    """A `worker_start`/`worker_event`/`worker_complete` journal row — the
    SAME top-level `type` shape `orchestrator.py`'s team-message flow
    journals (`turn_save({"type": fact_type, "data": {...}})`)."""
    data = {"delegation_id": delegation_id, **fields}
    return event_ingester.ingest(root_id, root_id, fact_type, data, source="test")


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


def test_fetch_sidecar_is_not_found_for_unknown_ref() -> None:
    # A `sidecar_ref` with no matching `WorkerRecord` is a PERMANENT
    # absence for delegation-flow-originated refs (ask/delegate_task/
    # Task-tool targets are never registered via `workers_api.upsert_
    # worker` — see derive.build_subagent_panel_container's docstring),
    # not a transient race — `Rebuilding` would retry forever. An honest
    # terminal `Ok(Sidecar(status="not_found"))` instead.
    adapter = ChatSurfaceAdapter()
    result = adapter.fetch_sidecar("some-session", "some-sidecar-ref")
    assert isinstance(result, Ok), result
    sidecar = result.value
    assert sidecar.sidecar_ref == "some-sidecar-ref"
    assert sidecar.status == "not_found"
    assert sidecar.payload == {}


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


# ---- UserInteraction plane (ADR 0006 §5): fact -> UserInteractionUpsert --


def _publish_interaction_fact(fact_type: str, root_id: str, payload: dict) -> None:
    asyncio.run(bus.publish(BusEvent(
        type=fact_type, root_id=root_id, sid=root_id, payload=payload,
    )))


def _publish_ask_result_set(root_id: str, ask_result: dict) -> None:
    asyncio.run(bus.publish(BusEvent(
        type="session.fire.msg_ask_result_set", root_id=root_id, sid=root_id,
        payload={"kind": "msg_ask_result_set", "msg_id": "msg-1", "ask_result": ask_result},
    )))


def test_interaction_requested_fact_broadcasts_pending_user_interaction_upsert() -> None:
    """tool-approval and worker-approval mechanisms share the SAME generic
    `interaction.fire.requested`/`resolved` handler (`_on_interaction_fact`)
    — proven here with a tool-approval-shaped payload; the worker-approval
    shape is proven at the store_access/hydration level below since it has
    no live 'requested' producer this pass (see `pending_approvals_api.py`'s
    module comment)."""
    root_id = f"root-{uuid.uuid4().hex}"
    adapter = ChatSurfaceAdapter()
    adapter.bind()
    opened = adapter.open_session(root_id)
    assert isinstance(opened, Ok)
    identity = opened.snapshot

    received: list[object] = []
    cursor = SurfaceCursor(surface_id=root_id, incarnation=identity.incarnation, render_rev=identity.render_rev)
    sub = adapter.subscribe((cursor,), Focus.OPENED, received.append)
    try:
        ref = f"{TOOL_APPROVAL_REF_PREFIX}approval-1"
        _publish_interaction_fact(
            "interaction.fire.requested", root_id,
            interaction_fact_payload(
                ref, UserInteractionKind.APPROVAL,
                {"subject": "tool", "tool_name": "Bash", "summary": {}, "risk_scope": "claude"},
            ),
        )
        upserts = [f for f in received if isinstance(f, UserInteractionUpsert)]
        assert upserts, received
        ui = upserts[-1].user_interaction
        assert ui.interaction_ref == ref
        assert ui.kind == UserInteractionKind.APPROVAL
        assert ui.state == UserInteractionState.PENDING
        assert ui.response is None
        assert ui.request["tool_name"] == "Bash"
    finally:
        sub.close()


def test_interaction_resolved_fact_broadcasts_resolved_user_interaction_upsert() -> None:
    root_id = f"root-{uuid.uuid4().hex}"
    adapter = ChatSurfaceAdapter()
    adapter.bind()
    opened = adapter.open_session(root_id)
    assert isinstance(opened, Ok)
    identity = opened.snapshot

    received: list[object] = []
    cursor = SurfaceCursor(surface_id=root_id, incarnation=identity.incarnation, render_rev=identity.render_rev)
    sub = adapter.subscribe((cursor,), Focus.OPENED, received.append)
    try:
        ref = f"{WORKER_APPROVAL_REF_PREFIX}deleg-1"
        _publish_interaction_fact(
            "interaction.fire.resolved", root_id,
            interaction_fact_payload(
                ref, UserInteractionKind.APPROVAL, {"subject": "delegation"},
                state=UserInteractionState.RESOLVED, response={"decision": "approve"},
            ),
        )
        upserts = [f for f in received if isinstance(f, UserInteractionUpsert)]
        assert upserts, received
        ui = upserts[-1].user_interaction
        assert ui.interaction_ref == ref
        assert ui.state == UserInteractionState.RESOLVED
        assert ui.response == {"decision": "approve"}
    finally:
        sub.close()


def test_interaction_fact_with_malformed_payload_is_dropped_not_crashed() -> None:
    root_id = f"root-{uuid.uuid4().hex}"
    adapter = ChatSurfaceAdapter()
    adapter.bind()
    opened = adapter.open_session(root_id)
    assert isinstance(opened, Ok)

    received: list[object] = []
    identity = opened.snapshot
    cursor = SurfaceCursor(surface_id=root_id, incarnation=identity.incarnation, render_rev=identity.render_rev)
    sub = adapter.subscribe((cursor,), Focus.OPENED, received.append)
    try:
        _publish_interaction_fact("interaction.fire.requested", root_id, {"kind": "not_a_real_kind"})
        assert not [f for f in received if isinstance(f, UserInteractionUpsert)]
    finally:
        sub.close()


def test_ask_result_set_broadcasts_pending_then_resolved_choice_interaction() -> None:
    root_id = f"root-{uuid.uuid4().hex}"
    adapter = ChatSurfaceAdapter()
    adapter.bind()
    opened = adapter.open_session(root_id)
    assert isinstance(opened, Ok)
    identity = opened.snapshot

    received: list[object] = []
    cursor = SurfaceCursor(surface_id=root_id, incarnation=identity.incarnation, render_rev=identity.render_rev)
    sub = adapter.subscribe((cursor,), Focus.OPENED, received.append)
    try:
        delegation_id = "sbd-1"
        ref = f"{DELEGATE_CHOICE_REF_PREFIX}{delegation_id}"
        _publish_ask_result_set(root_id, {
            "purpose": "delegate_approval", "session_ids": ["target-sess"],
            "reasoning": "", "delegation_id": delegation_id, "run_mode": "fork",
            "prompt_preview": "do it", "create_new": False,
        })
        upserts = [f for f in received if isinstance(f, UserInteractionUpsert)]
        assert upserts, received
        pending_ui = upserts[-1].user_interaction
        assert pending_ui.interaction_ref == ref
        assert pending_ui.kind == UserInteractionKind.CHOICE
        assert pending_ui.state == UserInteractionState.PENDING
        assert pending_ui.request["candidates"] == ["target-sess"]

        _publish_ask_result_set(root_id, {
            "purpose": "delegate_approval", "session_ids": ["target-sess"],
            "reasoning": "", "delegation_id": delegation_id, "run_mode": "fork",
            "resolved": True, "chosen_session_id": "target-sess", "create_new": False,
        })
        resolved_upserts = [f for f in received if isinstance(f, UserInteractionUpsert)]
        resolved_ui = resolved_upserts[-1].user_interaction
        assert resolved_ui.state == UserInteractionState.RESOLVED
        assert resolved_ui.response == {"picked_ref": "target-sess"}
    finally:
        sub.close()


def test_ask_result_set_without_delegate_purpose_is_ignored() -> None:
    """The Ask-singleton's OWN `propose_sessions` picker reuses the same
    `msg_ask_result_set` fact shape but never sets `purpose` — must NOT be
    mistaken for the session-bridge delegation-picker mechanism (that flow
    isn't one of the 3 mechanisms this contract recast unifies)."""
    root_id = f"root-{uuid.uuid4().hex}"
    adapter = ChatSurfaceAdapter()
    adapter.bind()
    opened = adapter.open_session(root_id)
    assert isinstance(opened, Ok)

    received: list[object] = []
    identity = opened.snapshot
    cursor = SurfaceCursor(surface_id=root_id, incarnation=identity.incarnation, render_rev=identity.render_rev)
    sub = adapter.subscribe((cursor,), Focus.OPENED, received.append)
    try:
        _publish_ask_result_set(root_id, {"session_ids": ["s1"], "reasoning": ""})
        assert not [f for f in received if isinstance(f, UserInteractionUpsert)]
    finally:
        sub.close()


def test_open_session_snapshot_carries_pending_tool_approval_interaction() -> None:
    root_id = f"root-{uuid.uuid4().hex}"

    async def _create() -> "tool_approval.ToolApproval":
        return tool_approval.registry.create(
            app_session_id=root_id, run_id="run-1", provider_kind="claude",
            tool_name="Bash", summary={"command": "ls"},
        )

    rec = asyncio.run(_create())
    try:
        adapter = ChatSurfaceAdapter()
        opened = adapter.open_session(root_id)
        assert isinstance(opened, Ok)
        refs = {ui.interaction_ref: ui for ui in opened.value.interactions}
        ref = f"{TOOL_APPROVAL_REF_PREFIX}{rec.approval_id}"
        assert ref in refs, refs
        assert refs[ref].kind == UserInteractionKind.APPROVAL
        assert refs[ref].state == UserInteractionState.PENDING
        assert refs[ref].request["tool_name"] == "Bash"
    finally:
        tool_approval.registry._pending.pop(rec.approval_id, None)


def test_open_session_snapshot_carries_pending_worker_and_choice_interactions() -> None:
    root_id = f"root-{uuid.uuid4().hex}"
    worker_rec = pending_approvals.create(
        delegation_id=f"deleg-{uuid.uuid4().hex}", app_session_id=root_id, cwd="/tmp",
        justification="need it", proposed_description="a worker",
        proposed_orchestration_mode="native", instructions_preview="", model="claude",
    )
    delegation_id = f"sbd-{uuid.uuid4().hex}"
    session_bridge._pending[delegation_id] = {
        "caller_sid": root_id, "caller_msg_id": "msg-1", "target_sid": "target-sess",
        "prompt": "do it", "run_mode": "fork", "proposed_ids": ["target-sess"],
    }
    try:
        adapter = ChatSurfaceAdapter()
        opened = adapter.open_session(root_id)
        assert isinstance(opened, Ok)
        refs = {ui.interaction_ref: ui for ui in opened.value.interactions}

        worker_ref = f"{WORKER_APPROVAL_REF_PREFIX}{worker_rec['delegation_id']}"
        assert worker_ref in refs, refs
        assert refs[worker_ref].kind == UserInteractionKind.APPROVAL
        assert refs[worker_ref].request["subject"] == "delegation"

        choice_ref = f"{DELEGATE_CHOICE_REF_PREFIX}{delegation_id}"
        assert choice_ref in refs, refs
        assert refs[choice_ref].kind == UserInteractionKind.CHOICE
        assert refs[choice_ref].request["candidates"] == ["target-sess"]
    finally:
        pending_approvals.delete(worker_rec["delegation_id"])
        session_bridge._pending.pop(delegation_id, None)


def test_open_session_snapshot_carries_pending_user_input_interaction() -> None:
    """The 4th legacy mechanism (`user_input_store`, ADR 0006 §5's
    remaining `input` kind slot) — cold hydration via `store_access.
    list_pending_interactions`, mirroring the other 3 mechanisms' coverage
    above."""
    root_id = f"root-{uuid.uuid4().hex}"
    req = user_input_store.create_request(
        app_session_id=root_id,
        questions=[{"id": "q1", "header": "H", "question": "Proceed?", "options": []}],
        timeout_seconds=60,
    )
    try:
        adapter = ChatSurfaceAdapter()
        opened = adapter.open_session(root_id)
        assert isinstance(opened, Ok)
        refs = {ui.interaction_ref: ui for ui in opened.value.interactions}
        ref = f"{USER_INPUT_REF_PREFIX}{req['request_id']}"
        assert ref in refs, refs
        assert refs[ref].kind == UserInteractionKind.INPUT
        assert refs[ref].state == UserInteractionState.PENDING
        assert refs[ref].request["schema"]["kind"] == "questions"
        assert refs[ref].request["schema"]["questions"][0]["id"] == "q1"
    finally:
        user_input_store.cancel_request(req["request_id"])


def test_interaction_fact_broadcasts_pending_user_input_interaction() -> None:
    """Same generic `_on_interaction_fact` handler proven for tool/worker
    approval above — proven here with an `input`-kind payload, matching
    the shape `backend/user_input_api.py`'s `internal_request_user_input`
    publishes."""
    root_id = f"root-{uuid.uuid4().hex}"
    adapter = ChatSurfaceAdapter()
    adapter.bind()
    opened = adapter.open_session(root_id)
    assert isinstance(opened, Ok)
    identity = opened.snapshot

    received: list[object] = []
    cursor = SurfaceCursor(surface_id=root_id, incarnation=identity.incarnation, render_rev=identity.render_rev)
    sub = adapter.subscribe((cursor,), Focus.OPENED, received.append)
    try:
        ref = f"{USER_INPUT_REF_PREFIX}req-1"
        _publish_interaction_fact(
            "interaction.fire.requested", root_id,
            interaction_fact_payload(
                ref, UserInteractionKind.INPUT,
                {"schema": {"kind": "approval"}, "prompt": "Deploy?"},
            ),
        )
        upserts = [f for f in received if isinstance(f, UserInteractionUpsert)]
        assert upserts, received
        ui = upserts[-1].user_interaction
        assert ui.interaction_ref == ref
        assert ui.kind == UserInteractionKind.INPUT
        assert ui.state == UserInteractionState.PENDING
        assert ui.request["prompt"] == "Deploy?"
    finally:
        sub.close()


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
    """H5: `older()`'s FALLBACK pipeline (chat_index cold/stale) builds a
    `_TurnView` for just the page it serves — turns outside the requested
    page are segmented (cheap) but never passed through `_build_turn_view`
    (derive_turn/derive_body). The layer-2 chat_index fast path (see
    test_chat_index.py) does strictly better (zero `_build_turn_view`
    calls once warm); `invalidate()` here forces the fallback path so
    THIS test keeps measuring the bound it was written for."""
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
    chat_index_mod.invalidate(root_id)  # open_session's backfill already warmed it — force the fallback path

    built_turn_ids: list[str] = []
    original = chat_adapter_mod._build_turn_view

    def counting(surface_id, turn_id, rows, produced_by_row, prompt_meta=None, orchestration_mode=None):
        built_turn_ids.append(turn_id)
        return original(surface_id, turn_id, rows, produced_by_row, prompt_meta, orchestration_mode)

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


def test_live_path_recovers_content_journaled_before_its_own_anchor() -> None:
    """The field-evidenced journal-ordering inversion (session 881c2082,
    decisive-trace.json): provider-stream rows race the dispatch-time
    canonical prompt row into the journal, so a turn's own content
    carries a LOWER seq than its anchor. Before the fix,
    `_flush_event_written` permanently dropped those rows as
    `turn_id_none` — behind `state.last_seq` forever the instant the
    flush that saw them advanced past their seq, with the anchor's own
    later flush having no way back to them.

    This locks the self-healing catch-up: the SAME content recovers,
    correctly turn-attributed, the instant the anchor arrives — in the
    anchor's OWN flush cycle (event-driven off its arrival, no sleep/
    poll/timer) — even though the content was dropped in an EARLIER,
    now-closed flush. Also asserts cold-reload convergence: reopening the
    same (still journal-inverted on disk) root shows the identical
    turn-attributed content, proving `_segment_turns`'s matching
    pre-anchor buffering (not just the live path) resolved it too."""
    root_id = f"root-{uuid.uuid4().hex}"
    text_uuid = str(uuid.uuid4())
    user_msg_id = str(uuid.uuid4())

    adapter = ChatSurfaceAdapter()
    adapter.bind()
    opened = adapter.open_session(root_id)  # live BEFORE the turn exists, matching the field WS-subscribe timing
    assert isinstance(opened, Ok), opened
    identity = opened.snapshot
    received: list[object] = []
    cursor = SurfaceCursor(surface_id=root_id, incarnation=identity.incarnation, render_rev=identity.render_rev)
    sub = adapter.subscribe((cursor,), Focus.OPENED, received.append)
    try:
        # Field ordering: assistant content journaled FIRST, flushed on
        # its OWN (no turn open yet — dropped as turn_id_none, watermark
        # set).
        content_seq = _ingest_assistant_text_uuid(root_id, "PONG", text_uuid)
        _publish_written(root_id, content_seq)
        assert not [f for f in received if isinstance(f, NodeUpsert)], received

        # The canonical prompt row arrives LATER, in a SEPARATE flush —
        # exactly the "anchor rows only flushed at turn end" field shape.
        prompt_seq = event_ingester.ingest(
            root_id, root_id, "agent_message",
            {
                "type": "user", "uuid": user_msg_id,
                "message": {"role": "user", "content": [{"type": "text", "text": "Reply PONG"}]},
                "origin": "user",
            },
            source="test", msg_id=f"asst-{uuid.uuid4().hex}",
        )
        _publish_written(root_id, prompt_seq)

        upserts = [f for f in received if isinstance(f, NodeUpsert)]
        prompt_upsert = next((f for f in upserts if f.node.node_id == user_msg_id), None)
        assert prompt_upsert is not None, upserts
        content_upsert = next((f for f in upserts if f.node.kind == NodeKind.ASSISTANT_TEXT), None)
        assert content_upsert is not None, upserts  # recovered, not lost forever
        assert content_upsert.node.payload.text == "PONG"
        assert content_upsert.node.turn_id == prompt_upsert.node.node_id  # correct turn attribution
    finally:
        sub.close()

    # Cold-reload convergence: the SAME (still journal-inverted) root,
    # read fresh, shows the identical content under the same turn.
    reopened = adapter.open_session(root_id)
    assert isinstance(reopened, Ok), reopened
    assert reopened.value.turns != (), "expected a non-empty turns tuple on cold reload"
    turn = reopened.value.turns[-1]
    assert turn.prompt is not None
    assert turn.prompt.node_id == user_msg_id
    live_kinds = {n.kind for n in reopened.value.live_turn_nodes}
    assert NodeKind.ASSISTANT_TEXT in live_kinds, reopened.value.live_turn_nodes


def test_usage_from_payload_builds_usage_with_cache_fields():
    """Item 4: `ChatSurfaceAdapter._usage_from_payload` — the
    `trace_collector._normalize_token_usage`-shaped dict
    `_publish_terminal_lifecycle`'s bus event carries — must produce a
    `Usage` with cache-token counts, and `total_tokens` derived as
    input+output (cache tokens are context occupancy, not newly
    generated)."""
    usage = ChatSurfaceAdapter._usage_from_payload({
        "input_tokens": 100, "output_tokens": 40,
        "cache_creation_input_tokens": 5, "cache_read_input_tokens": 12,
    })
    assert usage.input_tokens == 100
    assert usage.output_tokens == 40
    assert usage.total_tokens == 140
    assert usage.cache_creation_input_tokens == 5
    assert usage.cache_read_input_tokens == 12


def test_usage_from_payload_none_when_absent_or_wrong_type():
    assert ChatSurfaceAdapter._usage_from_payload(None) is None
    assert ChatSurfaceAdapter._usage_from_payload("not-a-dict") is None
    assert ChatSurfaceAdapter._usage_from_payload([1, 2, 3]) is None


def test_usage_from_payload_partial_dict_omits_total_and_cache():
    usage = ChatSurfaceAdapter._usage_from_payload({"input_tokens": 10})
    assert usage.input_tokens == 10
    assert usage.output_tokens is None
    assert usage.total_tokens is None  # can't sum with a missing operand
    assert usage.cache_creation_input_tokens is None
    assert usage.cache_read_input_tokens is None


def test_on_lifecycle_attaches_usage_to_terminal_turn_lifecycle_frame():
    """End-to-end through the live bus: a `lifecycle.turn_complete`
    BusEvent carrying `payload["usage"]` (as `_publish_terminal_lifecycle`
    now stamps) must broadcast a `TurnLifecycle` frame whose `usage` field
    is populated — not the always-null placeholder this wire field used
    to be."""
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
        asyncio.run(bus.publish(BusEvent(
            type="lifecycle.turn_complete",
            root_id=root_id,
            sid=root_id,
            payload={
                "user_turn_id": "user-turn", "execution_turn_id": "exec-turn",
                "lifecycle_message_id": "lifecycle-msg", "assistant_message_id": "asst-msg",
                "reason": "success",
                "usage": {
                    "input_tokens": 7, "output_tokens": 3,
                    "cache_creation_input_tokens": 0, "cache_read_input_tokens": 2,
                },
            },
            persist=False,
        )))
        lifecycle_frames = [f for f in received if isinstance(f, TurnLifecycle)]
        assert lifecycle_frames, received
        frame = lifecycle_frames[-1]
        assert frame.usage is not None
        assert frame.usage.input_tokens == 7
        assert frame.usage.output_tokens == 3
        assert frame.usage.total_tokens == 10
        assert frame.usage.cache_read_input_tokens == 2
    finally:
        sub.close()


def test_on_lifecycle_running_phase_never_carries_usage():
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
        asyncio.run(bus.publish(BusEvent(
            type="lifecycle.turn_start",
            root_id=root_id,
            sid=root_id,
            payload={
                "user_turn_id": "user-turn", "execution_turn_id": "exec-turn",
                "lifecycle_message_id": "lifecycle-msg", "assistant_message_id": "asst-msg",
                # A RUNNING-phase event would never carry usage in
                # practice, but if one somehow did, it must still not
                # leak onto the frame — usage is a terminal-only concept.
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
            persist=False,
        )))
        lifecycle_frames = [f for f in received if isinstance(f, TurnLifecycle)]
        assert lifecycle_frames, received
        assert lifecycle_frames[-1].usage is None
    finally:
        sub.close()


def test_open_session_groups_worker_facts_into_worker_turn() -> None:
    """SubAgentTurn family, batch/cold path (`_build_turn_view`, exercised
    via `open_session`): every WORKER_INTERACTION fact for one delegation
    groups into ONE WORKER_TURN container. Two delegations in the same
    turn — only the CHRONOLOGICALLY LAST one's own facts are eagerly
    included in `live_turn_nodes` (same "trailing container only" bound
    NATIVE_SUBAGENT_TURN already has, now generalized to the whole
    SubAgentTurn family — see `test_live_path_only_expands_trailing_
    subagent_not_earlier_ones`); the earlier one's facts stay lazy,
    reachable one level down via `children()`."""
    root_id = f"root-{uuid.uuid4().hex}"
    _ingest_prompt(root_id, "hello")
    _ingest_worker_fact(
        root_id, "worker_start", "deleg-early", panel_kind="worker",
        worker_description="early delegation", worker_session_id="sess-e",
    )
    _ingest_worker_fact(
        root_id, "worker_start", "deleg-1", panel_kind="worker",
        worker_description="fix the bug", worker_session_id="sess-w",
    )
    _ingest_worker_fact(root_id, "worker_event", "deleg-1")
    _ingest_worker_fact(
        root_id, "worker_complete", "deleg-1",
        token_usage={"input_tokens": 12, "output_tokens": 8},
    )

    adapter = ChatSurfaceAdapter()
    result = adapter.open_session(root_id)
    assert isinstance(result, Ok), result
    snapshot = result.value
    assert len(snapshot.turns) == 1

    containers = [n for n in snapshot.live_turn_nodes if n.kind == NodeKind.WORKER_TURN]
    assert len(containers) == 2, snapshot.live_turn_nodes
    container = next(c for c in containers if c.node_id == derive_mod.worker_turn_container_id("deleg-1"))
    assert container.payload.label == "fix the bug"
    assert container.target_ref.session_id == "sess-w"
    assert container.target_ref.turn_id is None
    assert container.usage.input_tokens == 12
    assert container.usage.total_tokens == 20
    # Real journal ingestion timestamps aren't fixed values to assert
    # exactly — structural correctness (count/has_children, a real
    # started_ts<=ended_ts span) is what this test can check.
    assert container.child_manifest.renderable_child_count == 3
    assert container.child_manifest.has_children is True
    assert container.child_manifest.started_ts is not None
    assert container.child_manifest.ended_ts is not None
    assert container.child_manifest.started_ts <= container.child_manifest.ended_ts

    # The trailing delegation's own facts ARE eagerly included...
    trailing_facts = [
        n for n in snapshot.live_turn_nodes
        if n.kind == NodeKind.WORKER_INTERACTION and n.parent_id == container.node_id
    ]
    assert len(trailing_facts) == 3
    # ...the earlier delegation's are NOT — reachable one level down only.
    early_id = derive_mod.worker_turn_container_id("deleg-early")
    assert not any(
        n.kind == NodeKind.WORKER_INTERACTION and n.parent_id == early_id
        for n in snapshot.live_turn_nodes
    )
    level1 = adapter.children(root_id, early_id, at_render_rev=result.snapshot.render_rev)
    assert isinstance(level1, Ok)
    assert len(level1.value) == 1


def test_open_session_panel_kind_maps_to_sub_session_turn() -> None:
    root_id = f"root-{uuid.uuid4().hex}"
    _ingest_prompt(root_id, "hello")
    _ingest_worker_fact(
        root_id, "worker_start", "deleg-1", panel_kind="sub_session",
        worker_description="delegated task", worker_session_id="sess-s",
    )

    adapter = ChatSurfaceAdapter()
    result = adapter.open_session(root_id)
    assert isinstance(result, Ok), result
    containers = [n for n in result.value.live_turn_nodes if n.kind == NodeKind.SUB_SESSION_TURN]
    assert len(containers) == 1


def test_open_session_worker_turn_created_true_for_created_panel() -> None:
    # `emit_session_created_panel`'s "*_created" panels never emit a
    # `worker_complete` (nothing runs) — a single `worker_start` fact with
    # `is_new: True` is the ENTIRE group.
    root_id = f"root-{uuid.uuid4().hex}"
    _ingest_prompt(root_id, "hello")
    _ingest_worker_fact(
        root_id, "worker_start", "deleg-1", panel_kind="sub_session_created",
        worker_description="sess created", worker_session_id="sess-new", is_new=True,
    )

    adapter = ChatSurfaceAdapter()
    result = adapter.open_session(root_id)
    assert isinstance(result, Ok), result
    container = next(
        n for n in result.value.live_turn_nodes if n.kind == NodeKind.SUB_SESSION_TURN
    )
    assert container.payload.created is True


def test_open_session_worker_turn_target_turn_id_and_sidecar_ref_from_worker_complete() -> None:
    root_id = f"root-{uuid.uuid4().hex}"
    _ingest_prompt(root_id, "hello")
    _ingest_worker_fact(
        root_id, "worker_start", "deleg-1", panel_kind="sub_session",
        worker_description="delegated", worker_session_id="sess-corr",
    )
    _ingest_worker_fact(
        root_id, "worker_complete", "deleg-1",
        worker_session_id="sess-corr", target_turn_id="prompt-corr",
    )

    adapter = ChatSurfaceAdapter()
    result = adapter.open_session(root_id)
    assert isinstance(result, Ok), result
    container = next(
        n for n in result.value.live_turn_nodes if n.kind == NodeKind.SUB_SESSION_TURN
    )
    assert container.target_ref.session_id == "sess-corr"
    assert container.target_ref.turn_id == "prompt-corr"
    assert container.sidecar_ref == "sess-corr"


def test_live_path_incrementally_builds_worker_turn_container() -> None:
    """SubAgentTurn family, live path (`_process_row`/`bind`): the
    container is constructed on the delegation's first fact and rebuilt
    (SAME node_id) in place as later facts for it arrive."""
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
        seq1 = _ingest_worker_fact(
            root_id, "worker_start", "deleg-1", panel_kind="sub_session",
            worker_description="delegated", worker_session_id="sess-a",
        )
        _publish_written(root_id, seq1)

        upserts = [f for f in received if isinstance(f, NodeUpsert)]
        containers = [f.node for f in upserts if f.node.kind == NodeKind.SUB_SESSION_TURN]
        assert containers, received
        first_container = containers[-1]
        assert first_container.child_manifest.renderable_child_count == 1
        assert first_container.usage is None

        facts = [f.node for f in upserts if f.node.kind == NodeKind.WORKER_INTERACTION]
        assert facts and facts[-1].parent_id == first_container.node_id

        received.clear()
        seq2 = _ingest_worker_fact(
            root_id, "worker_complete", "deleg-1", worker_session_id="sess-a",
            token_usage={"input_tokens": 7, "output_tokens": 2},
        )
        _publish_written(root_id, seq2)

        upserts2 = [f for f in received if isinstance(f, NodeUpsert)]
        containers2 = [f.node for f in upserts2 if f.node.kind == NodeKind.SUB_SESSION_TURN]
        assert containers2, received
        updated_container = containers2[-1]
        assert updated_container.node_id == first_container.node_id
        assert updated_container.child_manifest.renderable_child_count == 2
        assert updated_container.usage.input_tokens == 7
        assert updated_container.usage.total_tokens == 9
    finally:
        sub.close()


def test_replay_groups_worker_facts_into_worker_turn() -> None:
    """SubAgentTurn family, resync-replay path (`_replay`, exercised via
    a resubscribing cursor behind the surface's current render_seq_
    history): the container is rebuilt and broadcast; its own
    WORKER_INTERACTION facts are NOT re-broadcast flat, same bound as
    sidechain descendants in `_replay`."""
    root_id = f"root-{uuid.uuid4().hex}"
    _ingest_prompt(root_id, "hello")

    adapter = ChatSurfaceAdapter()
    opened = adapter.open_session(root_id)
    assert isinstance(opened, Ok)
    identity = opened.snapshot

    _ingest_worker_fact(
        root_id, "worker_start", "deleg-1", panel_kind="worker",
        worker_description="fix it", worker_session_id="sess-z",
    )
    _ingest_worker_fact(
        root_id, "worker_complete", "deleg-1", worker_session_id="sess-z",
        token_usage={"input_tokens": 3, "output_tokens": 4},
    )

    received: list[object] = []
    cursor = SurfaceCursor(surface_id=root_id, incarnation=identity.incarnation, render_rev=0)
    sub = adapter.subscribe((cursor,), Focus.OPENED, received.append)
    try:
        upserts = [f for f in received if isinstance(f, NodeUpsert)]
        containers = [f.node for f in upserts if f.node.kind == NodeKind.WORKER_TURN]
        assert containers, received
        container = containers[-1]
        assert container.payload.label == "fix it"
        assert container.usage.input_tokens == 3
        assert not any(f.node.kind == NodeKind.WORKER_INTERACTION for f in upserts)
    finally:
        sub.close()


_TESTS = [
    test_open_session_single_turn,
    test_open_session_groups_worker_facts_into_worker_turn,
    test_open_session_panel_kind_maps_to_sub_session_turn,
    test_open_session_worker_turn_created_true_for_created_panel,
    test_open_session_worker_turn_target_turn_id_and_sidecar_ref_from_worker_complete,
    test_live_path_incrementally_builds_worker_turn_container,
    test_replay_groups_worker_facts_into_worker_turn,
    test_subscribe_emits_node_upsert_with_bumped_render_rev,
    test_subscribe_wrong_incarnation_emits_resync_required,
    test_submit_rejects_all_intents,
    test_fetch_sidecar_is_not_found_for_unknown_ref,
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
    test_live_path_recovers_content_journaled_before_its_own_anchor,
    test_usage_from_payload_builds_usage_with_cache_fields,
    test_usage_from_payload_none_when_absent_or_wrong_type,
    test_usage_from_payload_partial_dict_omits_total_and_cache,
    test_on_lifecycle_attaches_usage_to_terminal_turn_lifecycle_frame,
    test_on_lifecycle_running_phase_never_carries_usage,
    test_interaction_requested_fact_broadcasts_pending_user_interaction_upsert,
    test_interaction_resolved_fact_broadcasts_resolved_user_interaction_upsert,
    test_interaction_fact_with_malformed_payload_is_dropped_not_crashed,
    test_ask_result_set_broadcasts_pending_then_resolved_choice_interaction,
    test_ask_result_set_without_delegate_purpose_is_ignored,
    test_open_session_snapshot_carries_pending_tool_approval_interaction,
    test_open_session_snapshot_carries_pending_worker_and_choice_interactions,
    test_open_session_snapshot_carries_pending_user_input_interaction,
    test_interaction_fact_broadcasts_pending_user_input_interaction,
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
