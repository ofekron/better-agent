#!/usr/bin/env python3
"""Unit coverage for backend/adapter_api.py (ADR 0006 §2/§4/§5 transport).

Same isolation recipe as `backend/scripts/test_chat_adapter.py`
(`paths.engage_test_home` before any backend import). The bare/dotted
singleton aliasing is `backend/adapters/__init__.py`'s own responsibility
now (self-canonicalizing — see its module docstring), so this test doesn't
replicate it: importing `backend.adapters.chat_adapter` below is enough to
trigger it, and this file exercises the exact same "backend.event_bus.bus
IS event_bus.bus" invariant production wiring depends on via the
`test_aliasing_unifies_bare_and_dotted_event_bus` test below.

Run:
    PYTHONPATH=. python3 -m pytest backend/scripts/test_adapter_api.py -q
    PYTHONPATH=. python3 backend/scripts/test_adapter_api.py   # __main__ fallback
"""

from __future__ import annotations

import asyncio
import atexit
import shutil
import subprocess
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

# bare — used directly below (not for aliasing; backend/adapters/__init__.py
# handles that on its own import, triggered by the `backend.adapters.*`
# imports right below).
import event_bus  # noqa: E402
import event_ingester as bare_event_ingester  # noqa: E402

import auth  # noqa: E402
import adapter_api  # noqa: E402
import browser_trust  # noqa: E402
from backend.adapters.chat_adapter import ChatSurfaceAdapter  # noqa: E402
from backend.event_bus import BusEvent, bus  # noqa: E402
from backend.event_journal import EVENT_JOURNAL_WRITTEN  # noqa: E402
from backend.surface_contract.identity import Rebuilding  # noqa: E402
from backend.ws_outbox import WebSocketOutbox  # noqa: E402

from fastapi import FastAPI, WebSocketDisconnect  # noqa: E402
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
    """Process-order independent: explicitly (re-)triggers canonicalization
    (idempotent — safe even though module-level imports above already did
    it) instead of assuming some earlier import in this process already
    put `backend.adapters` first, then proves identity through BOTH dotted
    import forms. `from backend.event_bus import bus` re-resolves via a
    `sys.modules["backend.event_bus"]` dict lookup by fully-qualified name
    every time, so it would pass even if canonicalization only patched
    `sys.modules`. `import backend.event_bus as x` compiles to an
    IMPORT_FROM opcode that resolves via `getattr(sys.modules["backend"],
    "event_bus")` FIRST and only falls back to the dict lookup if that
    attribute is unset — so it is the one that actually catches a
    canonicalization which patched `sys.modules` but left the `backend`
    package's own attribute stale (see backend/adapters/__init__.py's
    docstring). Covering both forms is what makes this order-independent
    rather than order-lucky."""
    import backend.adapters  # noqa: F401  (idempotent; triggers canonicalization)
    import backend.event_bus as dotted_event_bus
    from backend.event_bus import bus as from_import_bus

    assert dotted_event_bus.bus is event_bus.bus
    assert from_import_bus is event_bus.bus


def test_aliasing_survives_real_dotted_import_before_canonicalization() -> None:
    """Regression for the actual order hazard: if ANYTHING does a real
    (non-canonicalization) `import backend.event_bus as x` BEFORE
    `backend.adapters` is first imported in the process, Python's import
    machinery sets `sys.modules["backend"].event_bus` to that real module.
    `backend/adapters/__init__.py`'s canonicalization must overwrite that
    package attribute too, not just the `sys.modules["backend.event_bus"]`
    dict entry — otherwise every later `import backend.event_bus as y`
    keeps resolving to the pre-canonicalization module forever, because the
    IMPORT_FROM opcode's `getattr` fast path never falls through to the
    dict lookup once the attribute exists (see backend/adapters/__init__.py
    docstring). Runs in a subprocess with a virgin sys.modules so it can
    force this exact hazardous ordering without poisoning the shared
    pytest process's global module state for other tests in this file/run."""
    backend_dir = str(Path(__file__).resolve().parents[1])
    repo_root = str(Path(backend_dir).parent)
    script = (
        "import sys; "
        f"sys.path.insert(0, {backend_dir!r}); sys.path.insert(0, {repo_root!r}); "
        "import backend.event_bus as poisoned_before_canonicalization; "
        "import event_bus as bare; "
        "import backend.adapters; "
        "import backend.event_bus as healed_after_canonicalization; "
        "assert healed_after_canonicalization is bare, "
        "'attribute-walk import stayed poisoned after canonicalization'; "
        "assert healed_after_canonicalization.bus is bare.bus"
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"


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


def test_snapshot_splits_image_content_block_into_attachment() -> None:
    """Regression: an Anthropic-shaped image content block (the exact
    shape runner.py's `_multimodal_msg` sends and the CLI/SDK echoes back
    into its own transcript) must become a TYPED_PROMPT `attachments`
    entry, never get JSON-dumped into `payload.text` alongside the prompt."""
    root_id = f"root-{uuid.uuid4().hex}"
    bare_event_ingester.event_ingester.ingest(
        root_id, root_id, "agent_message",
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": "image/png", "data": "ZmFrZQ=="},
                    },
                    {"type": "text", "text": "check this out"},
                ],
            },
        },
        source="test",
    )
    adapter = ChatSurfaceAdapter()
    adapter_api.configure(chat=adapter)

    client = TestClient(_build_app())
    resp = client.get(f"/api/v2/surface/sessions/{root_id}/snapshot")
    assert resp.status_code == 200, resp.text
    payload = resp.json()["turns"][0]["prompt"]["payload"]
    assert payload["text"] == "check this out"
    assert "ZmFrZQ==" not in payload["text"]
    assert len(payload["attachments"]) == 1
    assert payload["attachments"][0]["media_type"] == "image/png"


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


def test_attachment_route_serves_seeded_blob() -> None:
    """Seeds a file the SAME way `_save_message_images` (orchestrator.py)
    does — writing under `<ba_home>/sessions/images/<session_id>/` — and
    confirms the v2 route resolves it via the identical storage helper
    `session_detail_api.get_session_image` uses."""
    session_id = f"session-{uuid.uuid4().hex}"
    filename = "image_0.png"
    image_dir = paths.ba_home() / "sessions" / "images" / session_id
    image_dir.mkdir(parents=True, exist_ok=True)
    (image_dir / filename).write_bytes(b"\x89PNG\r\n fake bytes")

    client = TestClient(_build_app())
    resp = client.get(f"/api/v2/surface/sessions/{session_id}/attachments/{filename}")
    assert resp.status_code == 200, resp.text
    assert resp.content == b"\x89PNG\r\n fake bytes"
    assert resp.headers["content-type"] == "image/png"
    assert resp.headers["content-length"] == str(len(b"\x89PNG\r\n fake bytes"))


def test_attachment_route_rejects_foreign_session_ref() -> None:
    owner_session = f"session-{uuid.uuid4().hex}"
    other_session = f"session-{uuid.uuid4().hex}"
    filename = "image_0.png"
    image_dir = paths.ba_home() / "sessions" / "images" / owner_session
    image_dir.mkdir(parents=True, exist_ok=True)
    (image_dir / filename).write_bytes(b"only-in-owner-session")

    client = TestClient(_build_app())
    resp = client.get(f"/api/v2/surface/sessions/{other_session}/attachments/{filename}")
    assert resp.status_code == 404, resp.text


def test_attachment_route_rejects_malformed_ref() -> None:
    session_id = f"session-{uuid.uuid4().hex}"
    client = TestClient(_build_app())

    # Disallowed character (outside _ID_RE's [A-Za-z0-9_.:-]).
    resp = client.get(f"/api/v2/surface/sessions/{session_id}/attachments/bad%21name.png")
    assert resp.status_code == 400, resp.text

    # Explicit ".." rejection (_validate_id), independent of route matching.
    resp2 = client.get(f"/api/v2/surface/sessions/{session_id}/attachments/a..b.png")
    assert resp2.status_code == 400, resp2.text

    resp3 = client.get(f"/api/v2/surface/sessions/{session_id}/attachments/{'a' * 300}")
    assert resp3.status_code == 400, resp3.text


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


class _SlowConsumerWebSocket:
    """Duck-types just enough of `fastapi.WebSocket` for `ws_surface`/
    `_authenticate` to drive directly (bypassing the ASGI test-transport,
    which doesn't reliably reproduce backpressure — see
    `test_ws_outbox.py`'s own fakes for the same rationale). `session`
    carries an already-authenticated user so `_authenticate` never touches
    `query_params`. First `receive_json()` call hands back the `surfaces`
    subscribe message; every call after that blocks until `close()` is
    called (mirroring a client that goes idle after subscribing), then
    raises WebSocketDisconnect — the same signal a real dropped connection
    would deliver to a blocked `receive_json()`. `send_text` never
    returns — the permanently-stuck "slow consumer" `WebSocketOutbox`'s
    enqueue-timeout is meant to detect."""

    def __init__(self, subscribe_message: dict) -> None:
        self.session = {"user": "tester"}
        self.query_params: dict[str, str] = {}
        self._subscribe_message = subscribe_message
        self._receive_calls = 0
        self.subscribed = asyncio.Event()
        self._closed_event = asyncio.Event()
        self.closed = False
        self.sent: list[dict] = []

    async def accept(self) -> None:
        return None

    async def receive_json(self) -> dict:
        self._receive_calls += 1
        if self._receive_calls == 1:
            return self._subscribe_message
        self.subscribed.set()
        await self._closed_event.wait()
        raise WebSocketDisconnect()

    async def send_text(self, text: str) -> None:
        await asyncio.Event().wait()  # never returns: the permanent slow consumer

    async def send_bytes(self, payload: bytes) -> None:
        await asyncio.Event().wait()

    async def close(self, code: int | None = None) -> None:
        self.closed = True
        self._closed_event.set()


def test_ws_surface_disconnects_slow_consumer_via_bounded_outbox() -> None:
    """H3: /ws/v2/surface routes its live-frame sends through the SAME
    bounded-outbox/slow-consumer-disconnect mechanism as /ws/chat
    (backend/ws_outbox.py) — a consumer that never drains gets
    disconnected (not an unboundedly growing in-memory queue), and the
    route's cleanup (subscription.close()) still runs afterward."""
    root_id = f"root-{uuid.uuid4().hex}"
    _ingest_prompt(root_id, "hello")
    adapter = ChatSurfaceAdapter()
    adapter.bind()
    adapter_api.configure(chat=adapter)
    opened = adapter.open_session(root_id)
    identity = opened.snapshot

    original_validate = browser_trust.validate_websocket
    original_outbox_cls = adapter_api.WebSocketOutbox
    # Small bounds so the test is fast/deterministic — production defaults
    # (256 items, 2s) are already covered by test_ws_outbox.py's own suite
    # against this SAME shared class.
    adapter_api.browser_trust.validate_websocket = lambda ws: True
    adapter_api.WebSocketOutbox = lambda websocket, *, on_close: WebSocketOutbox(
        websocket, on_close=on_close, max_items=1, enqueue_timeout_s=0.02,
    )

    async def run() -> None:
        websocket = _SlowConsumerWebSocket({
            "surfaces": [{
                "surface_id": root_id,
                "incarnation": identity.incarnation,
                "render_rev": identity.render_rev,
            }],
            "focus": "opened",
        })
        route_task = asyncio.create_task(adapter_api.ws_surface(websocket))
        await asyncio.wait_for(websocket.subscribed.wait(), timeout=1.0)

        # Enough broadcasts to exceed max_items=1 and trip the enqueue
        # timeout against a writer permanently stuck in send_text.
        for i in range(5):
            seq = bare_event_ingester.event_ingester.ingest(
                root_id, root_id, "agent_message",
                {"type": "assistant", "message": {"content": [{"type": "text", "text": f"chunk {i}"}]}},
                source="test",
            )
            await bus.publish(BusEvent(
                type=EVENT_JOURNAL_WRITTEN, root_id=root_id, sid=root_id,
                payload={"event_type": "agent_message", "seq": seq, "data": {}, "source": "test", "event_id": str(uuid.uuid4())},
            ))

        await asyncio.wait_for(websocket._closed_event.wait(), timeout=2.0)
        assert websocket.closed is True
        # The route's own receive loop unblocks on the disconnect and its
        # finally: block runs (outbox.close()/wait_closed(), subscription
        # cleanup) without hanging.
        await asyncio.wait_for(route_task, timeout=2.0)

    try:
        asyncio.run(run())
    finally:
        adapter_api.browser_trust.validate_websocket = original_validate
        adapter_api.WebSocketOutbox = original_outbox_cls


_TESTS = [
    test_aliasing_unifies_bare_and_dotted_event_bus,
    test_aliasing_survives_real_dotted_import_before_canonicalization,
    test_snapshot_ok_envelope,
    test_snapshot_splits_image_content_block_into_attachment,
    test_fetch_sidecar_maps_to_rebuilding_envelope,
    test_children_stale_cursor_envelope,
    test_older_cursor_round_trips_through_opaque_token,
    test_invalid_session_id_rejected_400,
    test_attachment_route_serves_seeded_blob,
    test_attachment_route_rejects_foreign_session_ref,
    test_attachment_route_rejects_malformed_ref,
    test_ws_subscribe_receives_node_upsert_and_intent_is_rejected,
    test_ws_surface_disconnects_slow_consumer_via_bounded_outbox,
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
