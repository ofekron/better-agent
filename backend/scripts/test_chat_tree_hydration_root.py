"""Fix A: the historical_hydration_root producer bridge.

Locks the previously dead three-dot gate end to end on the backend:

  A. Producer — session_manager's snapshot hydration stamps
     `historical_hydration_root` (frontend shape: id, type, revision,
     direct_child_count, display_summary) on completed assistant
     messages when the historical projection is current, and leaves the
     field absent when the projection is unavailable.
  B. Carrier — bff_chat_lookup.build_lookup hoists the manifest from the
     runtime snapshot to a top-level `historical_hydration_root` field on
     kind="message" lookup entries (the shape chatTreeClient maps onto
     ChatMessage), failing closed to None for malformed manifests.
  C. Wire — GET /api/chat-tree lookup entries carry the field.

Run with:
    cd backend && .venv/bin/python scripts/test_chat_tree_hydration_root.py
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sys
from unittest.mock import patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-hydration-root-")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import bff_chat_lookup  # noqa: E402
import bff_chat_tree  # noqa: E402
import chat_projection_ingestion  # noqa: E402
import historical_children_projection  # noqa: E402
from bff_runtime_service import RuntimeServiceError, runtime_service  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"
_failures: list[str] = []

MANIFEST = {
    "id": "turn-root:a1", "type": "turn_root", "revision": "rev-1",
    "direct_child_count": 4, "display_summary": "All done.",
}


def check(label: str, condition: bool) -> None:
    print(f"{PASS if condition else FAIL}  {label}")
    if not condition:
        _failures.append(label)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


# ── A. producer: session_manager snapshot hydration ────────────────────


def test_producer_stamps_manifest() -> None:
    node = {
        "next_seq": 2,
        "messages": [
            {"id": "u1", "role": "user", "content": "hi", "seq": 0,
             "events": [], "isStreaming": False},
            {"id": "a1", "role": "assistant", "content": "All done.", "seq": 1,
             "events": [], "isStreaming": False},
        ],
    }
    with patch.object(
        historical_children_projection, "root_manifest", return_value=dict(MANIFEST),
    ):
        snapshot = session_manager._compute_messages_snapshot("sid-a", "sid-a", node)
    assistant = next(m for m in snapshot["messages"] if m.get("id") == "a1")
    check("completed assistant message carries historical_hydration_root",
          assistant.get("historical_hydration_root") == MANIFEST)
    user = next(m for m in snapshot["messages"] if m.get("id") == "u1")
    check("user message carries no hydration root",
          "historical_hydration_root" not in user)


def test_producer_absent_when_projection_unavailable() -> None:
    node = {
        "next_seq": 2,
        "messages": [
            {"id": "u1", "role": "user", "content": "hi", "seq": 0,
             "events": [], "isStreaming": False},
            {"id": "a1", "role": "assistant", "content": "All done.", "seq": 1,
             "events": [], "isStreaming": False},
        ],
    }
    with patch.object(
        historical_children_projection, "root_manifest",
        side_effect=historical_children_projection.ProjectionUnavailable("rebuilding"),
    ):
        snapshot = session_manager._compute_messages_snapshot("sid-b", "sid-b", node)
    assistant = next(m for m in snapshot["messages"] if m.get("id") == "a1")
    check("unavailable projection leaves the field absent (fail closed)",
          "historical_hydration_root" not in assistant)


# ── B. carrier: bff_chat_lookup shape validation ───────────────────────


def test_lookup_shape_validation() -> None:
    check("valid manifest passes through",
          bff_chat_lookup.historical_hydration_root_of(
              {"historical_hydration_root": dict(MANIFEST)},
          ) == MANIFEST)
    check("missing manifest maps to None",
          bff_chat_lookup.historical_hydration_root_of({"id": "a1"}) is None)
    check("missing snapshot maps to None",
          bff_chat_lookup.historical_hydration_root_of(None) is None)
    for broken in (
        {**MANIFEST, "direct_child_count": "4"},
        {**MANIFEST, "direct_child_count": -1},
        {**MANIFEST, "direct_child_count": True},
        {k: v for k, v in MANIFEST.items() if k != "revision"},
        {**MANIFEST, "revision": 7},
        "not-an-object",
    ):
        if bff_chat_lookup.historical_hydration_root_of(
            {"historical_hydration_root": broken},
        ) is not None:
            check(f"malformed manifest fails closed: {broken!r}", False)
            return
    check("malformed manifests fail closed to None", True)
    extra = {**MANIFEST, "unexpected": "x"}
    check("extra fields are stripped to the exact frontend shape",
          bff_chat_lookup.historical_hydration_root_of(
              {"historical_hydration_root": extra},
          ) == MANIFEST)


# ── C. wire: GET /api/chat-tree lookup carries the field ───────────────


def wire_fact(seq: int, payload_type: str, payload: dict) -> dict:
    return {
        "canonical_seq": seq,
        "fact_id": f"fact-{seq}",
        "source_event_id": f"event-{seq}",
        "root_id": "hydroroot",
        "sid": "hydroroot",
        "source": "provider_stream",
        "source_stream_id": "run-1",
        "content_hash": digest(f"content-{seq}"),
        "payload_type": payload_type,
        "payload": payload,
        "observed_at": f"2026-07-15T10:00:{seq:02d}Z",
        "source_timestamp": None,
        "turn_id": "u1",
    }


SESSION = {
    "id": "hydroroot",
    "provider_id": "claude",
    "model": "sonnet-4-6",
    "reasoning_effort": "high",
    "messages": [
        {"id": "u1", "role": "user", "seq": 1},
        {"id": "a1", "role": "assistant", "seq": 2,
         "historical_hydration_root": dict(MANIFEST),
         "run_meta": {"provider_id": "claude", "model": "sonnet-4-6",
                      "reasoning_effort": "high"}},
    ],
}


async def fake_session_tree(session_id: str, *, exchange_count=None):
    if session_id != "hydroroot":
        raise RuntimeServiceError(404, "session not found")
    return {"tree": SESSION, "provider_kind": "claude"}


def test_wire_lookup_carries_manifest() -> None:
    for seq, payload_type, payload in [
        (1, "user_prompt", {"message_id": "u1", "text": "Run it"}),
        (2, "message_ownership_declared", {"message_id": "a1", "prompt_message_id": "u1"}),
        (3, "assistant_output", {"message_id": "a1", "text": "All done.", "final": True}),
    ]:
        chat_projection_ingestion.admit_canonical_fact(
            wire_fact(seq, payload_type, payload), provider="claude",
        )
    original = runtime_service.session_tree
    runtime_service.session_tree = fake_session_tree
    app = FastAPI()
    app.include_router(bff_chat_tree.router)
    client = TestClient(app)
    try:
        response = client.get("/api/chat-tree/hydroroot")
        check("hydration root serves 200", response.status_code == 200)
        body = response.json()
        assistant_entry = body["lookup"].get("a1")
        check("assistant lookup entry carries top-level historical_hydration_root",
              isinstance(assistant_entry, dict)
              and assistant_entry.get("historical_hydration_root") == MANIFEST)
        check("snapshot passthrough still carries the manifest too",
              assistant_entry is not None
              and assistant_entry["snapshot"]["historical_hydration_root"] == MANIFEST)
        user_entry = body["lookup"].get("u1")
        check("user lookup entry has an explicit null manifest",
              isinstance(user_entry, dict)
              and user_entry.get("historical_hydration_root") is None)
    finally:
        runtime_service.session_tree = original
        chat_projection_ingestion.close()


def main() -> None:
    test_producer_stamps_manifest()
    test_producer_absent_when_projection_unavailable()
    test_lookup_shape_validation()
    test_wire_lookup_carries_manifest()


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
    if _failures:
        print(f"{len(_failures)} test(s) FAILED")
        sys.exit(1)
    print("all chat tree hydration root tests passed")
