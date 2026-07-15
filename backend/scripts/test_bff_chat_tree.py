"""BFF chat-tree endpoint contract.

Locks the read path: stored canonical facts in the BFF rendering cache
serve as the formal chat tree (parseProjection shape) through
adapt_chat_inputs -> project_chat -> chat_to_wire, with typed states:

  A. A cached root returns 200 with the formal tree items.
  B. A root with no cached facts returns typed 503 chat_tree_rebuilding
     (with Retry-After) and marks the root dirty on the feed client —
     never an empty-success tree.
  C. An unknown session returns 404; an invalid id returns 400.

Run with:
    cd backend && .venv/bin/python scripts/test_bff_chat_tree.py
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-chat-tree-")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import bff_chat_feed  # noqa: E402
import bff_chat_tree  # noqa: E402
import chat_projection_ingestion  # noqa: E402
from bff_runtime_service import runtime_service  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"
_failures: list[str] = []


def check(label: str, condition: bool) -> None:
    print(f"{PASS if condition else FAIL}  {label}")
    if not condition:
        _failures.append(label)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def wire_fact(seq: int, payload_type: str, payload: dict) -> dict:
    return {
        "canonical_seq": seq,
        "fact_id": f"fact-{seq}",
        "source_event_id": f"event-{seq}",
        "root_id": "root",
        "sid": "root",
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
    "id": "root",
    "provider_id": "claude",
    "model": "sonnet-4-6",
    "reasoning_effort": "high",
    "messages": [
        {"id": "u1", "role": "user"},
        {"id": "a1", "role": "assistant",
         "run_meta": {"provider_id": "claude", "model": "sonnet-4-6", "reasoning_effort": "high"}},
    ],
}


async def fake_projection_source(session_id: str, *, after_seq: int = 0, limit: int = 2000):
    if session_id in ("root", "empty-root"):
        return {"found": True, "session": {**SESSION, "id": session_id},
                "provider_kind": "claude", "facts": [], "next_seq": 0,
                "has_more": False, "canonical_through_seq": 0}
    return {"found": False}


def main() -> None:
    for seq, payload_type, payload in [
        (1, "user_prompt", {"message_id": "u1", "text": "Run it"}),
        (2, "message_ownership_declared", {"message_id": "a1", "prompt_message_id": "u1"}),
        (3, "assistant_output", {"message_id": "a1", "text": "All done.", "final": True}),
    ]:
        chat_projection_ingestion.admit_canonical_fact(
            wire_fact(seq, payload_type, payload), provider="claude",
        )

    original = runtime_service.projection_source
    runtime_service.projection_source = fake_projection_source
    app = FastAPI()
    app.include_router(bff_chat_tree.router)
    client = TestClient(app)
    try:
        response = client.get("/api/chat-tree/root")
        check("cached root serves the formal tree", response.status_code == 200)
        body = response.json()
        turn = next((item for item in body.get("items", []) if item.get("type") == "Turn"), None)
        check("tree contains the turn with its provider result",
              turn is not None and turn["prompt"] == "u1"
              and turn["result"] is not None and turn["result"]["text"] == "All done.")
        check("no typed drops for a clean root", body.get("dropped") == [])

        response = client.get("/api/chat-tree/empty-root")
        detail = response.json().get("detail")
        check("uncached root is a typed rebuilding state",
              response.status_code == 503
              and isinstance(detail, dict) and detail.get("code") == "chat_tree_rebuilding"
              and response.headers.get("retry-after") == "2")
        check("uncached root marks the feed dirty",
              "empty-root" in bff_chat_feed.feed_client._dirty)

        response = client.get("/api/chat-tree/missing")
        check("unknown session is 404", response.status_code == 404)
        response = client.get("/api/chat-tree/bad.id")
        check("invalid session id is 400", response.status_code == 400)
    finally:
        runtime_service.projection_source = original
        chat_projection_ingestion.close()


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
    if _failures:
        print(f"{len(_failures)} test(s) FAILED")
        sys.exit(1)
    print("all bff chat tree tests passed")
