#!/usr/bin/env python3
"""Unit coverage for backend/adapter_api.py (ADR 0006 §2/§4/§5 transport).

Same isolation recipe as `backend/scripts/test_chat_adapter.py`
(`paths.engage_test_home` before any backend import), plus the
bare/dotted singleton aliasing `backend/main.py:_wire_surface_adapter`
performs — replicated here (not by importing `main.py`, which pulls in
the whole app's startup side effects) so this test exercises the exact
same "backend.event_bus.bus IS event_bus.bus" invariant production
wiring depends on.

Run:
    PYTHONPATH=. python3 -m pytest backend/scripts/test_adapter_api.py -q
    PYTHONPATH=. python3 backend/scripts/test_adapter_api.py   # __main__ fallback
"""

from __future__ import annotations

import asyncio
import atexit
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("httpx")

_BACKEND_DIR = str(Path(__file__).resolve().parents[1])
_REPO_ROOT = str(Path(_BACKEND_DIR).parent)
for _p in (_BACKEND_DIR, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import paths  # noqa: E402  (bare — matches sibling backend/scripts tests)

_TEST_HOME = tempfile.mkdtemp(prefix="ba-adapter-api-test-")
paths.engage_test_home(_TEST_HOME)
atexit.register(shutil.rmtree, _TEST_HOME, ignore_errors=True)

# ---- replicate backend/main.py:_wire_surface_adapter's bare<->dotted alias
import event_bus  # noqa: E402
import event_journal  # noqa: E402
import event_ingester as bare_event_ingester  # noqa: E402
import jsonl_tailer  # noqa: E402
import i18n  # noqa: E402
import user_msg_lifecycle  # noqa: E402

if "backend" not in sys.modules:
    import backend  # noqa: E402  (namespace package via _REPO_ROOT on sys.path)

sys.modules["backend.event_bus"] = event_bus
sys.modules["backend.event_journal"] = event_journal
sys.modules["backend.event_ingester"] = bare_event_ingester
sys.modules["backend.jsonl_tailer"] = jsonl_tailer
sys.modules["backend.paths"] = paths
sys.modules["backend.i18n"] = i18n
sys.modules["backend.user_msg_lifecycle"] = user_msg_lifecycle

import auth  # noqa: E402
import adapter_api  # noqa: E402
from backend.adapters.chat_adapter import ChatSurfaceAdapter  # noqa: E402
from backend.event_bus import BusEvent, bus  # noqa: E402
from backend.event_journal import EVENT_JOURNAL_WRITTEN  # noqa: E402
from backend.surface_contract.identity import Rebuilding  # noqa: E402

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from starlette.middleware.sessions import SessionMiddleware  # noqa: E402

_TEST_TOKEN = "test-bearer-token"


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-secret")
    app.include_router(adapter_api.router)
    return app


def _ingest_prompt(root_id: str, text: str) -> int:
    return bare_event_ingester.event_ingester.ingest(
        root_id, root_id, "agent_message",
        {"type": "user", "message": {"content": text}},
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


def test_aliasing_unifies_bare_and_dotted_event_bus() -> None:
    import backend.event_bus as dotted_event_bus
    assert dotted_event_bus.bus is event_bus.bus


def test_snapshot_ok_envelope() -> None:
    root_id = f"root-{uuid.uuid4().hex}"
    _ingest_prompt(root_id, "hello")
    adapter = ChatSurfaceAdapter()
    adapter_api.configure(chat=adapter)

    client = TestClient(_build_app())
    resp = client.get(f"/api/v2/surface/sessions/{root_id}/snapshot")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["kind"] == "ok"
    assert body["session_id"] == root_id
    assert body["surface_id"] == root_id
    assert len(body["turns"]) == 1
    assert body["turns"][0]["prompt"]["payload"]["text"] == "hello"
    assert "snapshot_identity" in body
    assert body["snapshot_identity"]["render_rev"] >= 0


def test_fetch_sidecar_maps_to_rebuilding_envelope() -> None:
    """No REST endpoint exposes fetch_sidecar (out of scope this phase) —
    exercise the real adapter method + the real envelope mapper directly,
    covering the `Rebuilding` branch of `_result_body`."""
    root_id = f"root-{uuid.uuid4().hex}"
    adapter = ChatSurfaceAdapter()
    result = adapter.fetch_sidecar(root_id, "some-sidecar-ref")
    assert isinstance(result, Rebuilding)
    assert adapter_api._result_body(result) == {"kind": "rebuilding", "retry_after_ms": None}


def test_children_stale_cursor_envelope() -> None:
    root_id = f"root-{uuid.uuid4().hex}"
    _ingest_prompt(root_id, "hello")
    adapter = ChatSurfaceAdapter()
    adapter_api.configure(chat=adapter)
    opened = adapter.open_session(root_id)

    client = TestClient(_build_app())
    resp = client.get(
        f"/api/v2/surface/sessions/{root_id}/nodes/{root_id}/children",
        params={"at_render_rev": 999},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"kind": "stale_cursor"}

    resp_ok = client.get(
        f"/api/v2/surface/sessions/{root_id}/nodes/{root_id}/children",
        params={"at_render_rev": opened.snapshot.render_rev},
    )
    assert resp_ok.status_code == 200
    assert resp_ok.json()["kind"] == "ok"


def test_older_cursor_round_trips_through_opaque_token() -> None:
    root_id = f"root-{uuid.uuid4().hex}"
    for i in range(7):
        _ingest_prompt(root_id, f"turn {i}")
    adapter = ChatSurfaceAdapter()
    adapter_api.configure(chat=adapter)

    client = TestClient(_build_app())
    snapshot = client.get(f"/api/v2/surface/sessions/{root_id}/snapshot").json()
    cursor_token = snapshot["older_cursor"]
    assert isinstance(cursor_token, str) and cursor_token

    older = client.get(f"/api/v2/surface/sessions/{root_id}/older", params={"cursor": cursor_token})
    assert older.status_code == 200, older.text
    assert older.json()["kind"] == "ok"

    bad = client.get(f"/api/v2/surface/sessions/{root_id}/older", params={"cursor": "not-a-valid-token"})
    assert bad.status_code == 400


def test_invalid_session_id_rejected_400() -> None:
    client = TestClient(_build_app())
    overlong = "a" * 300
    resp = client.get(f"/api/v2/surface/sessions/{overlong}/snapshot")
    assert resp.status_code == 400

    resp2 = client.get("/api/v2/surface/sessions/has..dots/snapshot")
    assert resp2.status_code == 400


def test_ws_subscribe_receives_node_upsert_and_intent_is_rejected() -> None:
    root_id = f"root-{uuid.uuid4().hex}"
    _ingest_prompt(root_id, "hello")
    adapter = ChatSurfaceAdapter()
    adapter.bind()
    adapter_api.configure(chat=adapter)
    opened = adapter.open_session(root_id)
    identity = opened.snapshot

    original_verify_token = auth.verify_token
    auth.verify_token = lambda tok: {"username": "tester"} if tok == _TEST_TOKEN else None
    try:
        client = TestClient(_build_app())
        with client.websocket_connect(f"/ws/v2/surface?token={_TEST_TOKEN}") as ws:
            ws.send_json({
                "surfaces": [{
                    "surface_id": root_id,
                    "incarnation": identity.incarnation,
                    "render_rev": identity.render_rev,
                }],
                "focus": "opened",
            })

            seq = bare_event_ingester.event_ingester.ingest(
                root_id, root_id, "agent_message",
                {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi there"}]}},
                source="test",
            )
            _publish_written(root_id, seq)

            frame = ws.receive_json()
            assert frame["type"] == "node_upsert", frame

            ws.send_json({"intent": {
                "kind": "stop", "intent_id": "intent-1", "session_id": root_id, "turn_id": "t1",
            }})
            # subscribe()'s replay and _on_event_written's live broadcast run
            # on independent threads/loops in this test (TestClient's portal
            # thread vs. the main thread's `_publish_written`), so a second
            # node_upsert for the same node can legitimately race in ahead of
            # the ack — real clients dedupe node_upsert by node_id (ADR 0006
            # §4), so drain any of those before asserting on the ack itself.
            ack = None
            for _ in range(10):
                msg = ws.receive_json()
                if msg["type"] == "intent_rejected":
                    ack = msg
                    break
                assert msg["type"] == "node_upsert", msg
            assert ack is not None, "did not receive intent_rejected"
            assert ack["intent_id"] == "intent-1"
            assert ack["code"] == "unsupported_contract_phase"
    finally:
        auth.verify_token = original_verify_token


_TESTS = [
    test_aliasing_unifies_bare_and_dotted_event_bus,
    test_snapshot_ok_envelope,
    test_fetch_sidecar_maps_to_rebuilding_envelope,
    test_children_stale_cursor_envelope,
    test_older_cursor_round_trips_through_opaque_token,
    test_invalid_session_id_rejected_400,
    test_ws_subscribe_receives_node_upsert_and_intent_is_rejected,
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
