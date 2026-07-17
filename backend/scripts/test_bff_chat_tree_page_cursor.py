"""Fix D: signed load-more page cursors for the chat tree.

Locks the bound-cursor contract:

  A. chat_page_cursor round-trips and rejects tampering (fail closed).
  B. GET issues `page.page_cursor` (opaque, HMAC-signed); consuming it
     pages exactly like the old turn cursor; has_older tracks it.
  C. Any binding mismatch — tampered token, foreign root, wrong pane —
     is the existing typed 409 stale_turn_cursor.
  D. After a projection rebuild that changed content, a pre-rebuild
     cursor is detected (anchor mismatch / vanished turn) and 409s;
     a rebuild that reproduced identical content keeps the cursor valid.

Run with:
    cd backend && .venv/bin/python scripts/test_bff_chat_tree_page_cursor.py
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
_TMP_HOME = _test_home.isolate("bc-test-page-cursor-")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import bff_chat_tree  # noqa: E402
import chat_page_cursor  # noqa: E402
import chat_projection_ingestion  # noqa: E402
from bff_runtime_service import RuntimeServiceError, runtime_service  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"
_failures: list[str] = []

ROOT = "cursorroot"


def check(label: str, condition: bool) -> None:
    print(f"{PASS if condition else FAIL}  {label}")
    if not condition:
        _failures.append(label)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def wire_fact(seq: int, payload_type: str, payload: dict, *, stream: str) -> dict:
    return {
        "canonical_seq": seq,
        "fact_id": f"fact-{seq}",
        "source_event_id": f"event-{seq}",
        "root_id": ROOT,
        "sid": ROOT,
        "source": "provider_stream",
        "source_stream_id": stream,
        "content_hash": digest(f"content-{seq}"),
        "payload_type": payload_type,
        "payload": payload,
        "observed_at": f"2026-07-15T10:{seq // 60:02d}:{seq % 60:02d}Z",
        "source_timestamp": None,
        "turn_id": payload.get("prompt_message_id") or payload.get("message_id") or "u1",
    }


def admit_turns(
    turn_ids: list[int], *, prompt_text=lambda turn: f"prompt {turn}",
    stream: str = "run-1",
) -> None:
    seq = 0
    for turn in turn_ids:
        for payload_type, payload in (
            ("user_prompt", {"message_id": f"u{turn}", "text": prompt_text(turn)}),
            ("message_ownership_declared",
             {"message_id": f"a{turn}", "prompt_message_id": f"u{turn}"}),
            ("assistant_output",
             {"message_id": f"a{turn}", "text": f"answer {turn}", "final": True}),
        ):
            seq += 1
            chat_projection_ingestion.admit_canonical_fact(
                wire_fact(seq, payload_type, payload, stream=stream), provider="claude",
            )


def session_for(turn_ids: list[int]) -> dict:
    return {
        "id": ROOT,
        "provider_id": "claude",
        "model": "sonnet-4-6",
        "reasoning_effort": "high",
        "messages": [
            entry for index, turn in enumerate(turn_ids) for entry in (
                {"id": f"u{turn}", "role": "user", "seq": index * 2 + 1},
                {"id": f"a{turn}", "role": "assistant", "seq": index * 2 + 2,
                 "run_meta": {"provider_id": "claude", "model": "sonnet-4-6",
                              "reasoning_effort": "high"}},
            )
        ],
    }


SESSION = {"tree": session_for(list(range(1, 8))), "provider_kind": "claude"}


async def fake_session_tree(session_id: str, *, exchange_count=None):
    if session_id != ROOT:
        raise RuntimeServiceError(404, "session not found")
    return SESSION


def test_cursor_helper() -> None:
    token = chat_page_cursor.encode_page_cursor(
        root_id="r", pane_id="r", generation=1, revision=9,
        turn_id="u3", turn_seq=7, turn_hash="a" * 64,
    )
    payload = chat_page_cursor.decode_page_cursor(token)
    check("cursor round-trips its binding",
          payload == {"v": 1, "root": "r", "pane": "r", "gen": 1, "rev": 9,
                      "turn": "u3", "turn_seq": 7, "turn_hash": "a" * 64})
    for broken in (
        token[:-2] + ("AA" if not token.endswith("AA") else "BB"),
        token + "x",
        "",
        "!" * 40,
        "A" * 4096,
    ):
        try:
            chat_page_cursor.decode_page_cursor(broken)
        except chat_page_cursor.PageCursorError:
            continue
        check(f"tampered cursor rejected: {broken[:16]!r}", False)
        return
    check("tampered/malformed cursors are rejected", True)


def main() -> None:
    test_cursor_helper()
    admit_turns(list(range(1, 8)))
    original = runtime_service.session_tree
    runtime_service.session_tree = fake_session_tree
    app = FastAPI()
    app.include_router(bff_chat_tree.router)
    client = TestClient(app)
    try:
        body = client.get(f"/api/chat-tree/{ROOT}").json()
        page_cursor = body["page"]["page_cursor"]
        check("initial window issues an opaque page cursor",
              isinstance(page_cursor, str) and bool(page_cursor)
              and body["page"]["has_older"] is True)

        body = client.get(f"/api/chat-tree/{ROOT}?cursor={page_cursor}").json()
        turn_ids = [item["id"] for item in body["items"] if item["type"] == "Turn"]
        check("consuming the cursor pages to the exact preceding turns",
              turn_ids == ["u1", "u2"])
        check("last page has no cursor and has_older tracks it",
              body["page"]["page_cursor"] is None
              and body["page"]["has_older"] is False)

        tampered = page_cursor[:-2] + ("AA" if not page_cursor.endswith("AA") else "BB")
        response = client.get(f"/api/chat-tree/{ROOT}?cursor={tampered}")
        detail = response.json().get("detail")
        check("tampered cursor is a typed 409",
              response.status_code == 409
              and isinstance(detail, dict) and detail.get("code") == "stale_turn_cursor")

        foreign = chat_page_cursor.encode_page_cursor(
            root_id="other-root", pane_id="other-root", generation=1, revision=1,
            turn_id="u3", turn_seq=1, turn_hash="0" * 64,
        )
        response = client.get(f"/api/chat-tree/{ROOT}?cursor={foreign}")
        detail = response.json().get("detail")
        check("cursor bound to another root is a typed 409",
              response.status_code == 409
              and isinstance(detail, dict) and detail.get("code") == "stale_turn_cursor")

        # Rebuild reproducing IDENTICAL content: cursor stays valid.
        chat_projection_ingestion.reset_root_projection(ROOT, provider="claude")
        admit_turns(list(range(1, 8)))
        response = client.get(f"/api/chat-tree/{ROOT}?cursor={page_cursor}")
        check("identical rebuild keeps the cursor valid",
              response.status_code == 200
              and [item["id"] for item in response.json()["items"]
                   if item["type"] == "Turn"] == ["u1", "u2"])

        # Rebuild with CHANGED content at the window-start turn (a
        # rewritten provider stream): the anchor (prompt fact hash) no
        # longer matches — typed 409.
        chat_projection_ingestion.reset_root_projection(ROOT, provider="claude")
        admit_turns(
            list(range(1, 8)),
            prompt_text=lambda turn: f"rewritten prompt {turn}",
            stream="run-2",
        )
        response = client.get(f"/api/chat-tree/{ROOT}?cursor={page_cursor}")
        detail = response.json().get("detail")
        check("rebuild with changed content invalidates the cursor (409)",
              response.status_code == 409
              and isinstance(detail, dict) and detail.get("code") == "stale_turn_cursor")

        # Rebuild where the cursor's turn no longer exists: typed 409.
        fresh = client.get(f"/api/chat-tree/{ROOT}?turns=2").json()
        fresh_cursor = fresh["page"]["page_cursor"]
        chat_projection_ingestion.reset_root_projection(ROOT, provider="claude")
        admit_turns([1, 2])
        response = client.get(f"/api/chat-tree/{ROOT}?cursor={fresh_cursor}")
        detail = response.json().get("detail")
        check("rebuild that dropped the cursor turn is a typed 409",
              response.status_code == 409
              and isinstance(detail, dict) and detail.get("code") == "stale_turn_cursor")
    finally:
        runtime_service.session_tree = original
        chat_projection_ingestion.close()


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
    if _failures:
        print(f"{len(_failures)} test(s) FAILED")
        sys.exit(1)
    print("all chat tree page cursor tests passed")
