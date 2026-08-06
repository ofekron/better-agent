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

import runs_dir  # noqa: E402  (bare — store_access._resolve aliases onto this instance)
from stores import worker_store  # noqa: E402  (bare — matches main.py's `from stores import task_store`)

from backend.adapters.chat_adapter import ChatSurfaceAdapter  # noqa: E402
from backend.event_bus import BusEvent, bus  # noqa: E402
from backend.event_ingester import event_ingester  # noqa: E402
from backend.event_journal import EVENT_JOURNAL_WRITTEN  # noqa: E402
from backend.surface_contract.frames import NodeUpsert, ResyncRequired  # noqa: E402
from backend.surface_contract.identity import Focus, Ok, Rebuilding, StaleCursor, SurfaceCursor  # noqa: E402
from backend.surface_contract.intents import SendPrompt, SendMode, SendTarget, SendTargetKind  # noqa: E402
from backend.surface_contract.nodes import NodeKind  # noqa: E402


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


def test_search_finds_substring_case_insensitive() -> None:
    root_id = f"root-{uuid.uuid4().hex}"
    _ingest_prompt(root_id, "find the needle please")
    adapter = ChatSurfaceAdapter()
    result = adapter.search(root_id, "NEEDLE")
    assert isinstance(result, Ok)
    assert len(result.value) == 1
    match = result.value[0]
    assert match.node_id in match.path


_TESTS = [
    test_open_session_single_turn,
    test_subscribe_emits_node_upsert_with_bumped_render_rev,
    test_subscribe_wrong_incarnation_emits_resync_required,
    test_submit_rejects_all_intents,
    test_fetch_sidecar_is_rebuilding_for_unknown_ref,
    test_fetch_sidecar_maps_worker_record,
    test_fetch_sidecar_worker_status_reflects_latest_run_outcome,
    test_children_stale_cursor_on_render_rev_mismatch,
    test_search_finds_substring_case_insensitive,
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
