"""BFF chat feed client tests.

Pins the BFF-side half of the canonical feed contract:

  A. `_pull_root` pages `projection-source` to head, admits every fact
     into the chat projection store, and persists the cursor.
  B. Re-delivery (cursor reset to 0) is idempotent — admission dedup
     collapses replayed facts to no-ops.
  C. Missing/invalid `provider_kind` fails closed: nothing admitted,
     cursor not advanced.
  D. A `found: false` page (root deleted) drops the cursor.
  E. Advance frames mark roots dirty; malformed frames are dropped.

Run with:
    cd backend && .venv/bin/python scripts/test_bff_chat_feed.py
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-bff-chat-feed-")

import bff_chat_feed  # noqa: E402
import chat_projection_ingestion  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"
_failures: list[str] = []


def check(label: str, condition: bool) -> None:
    print(f"{PASS if condition else FAIL}  {label}")
    if not condition:
        _failures.append(label)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def wire_fact(root_id: str, index: int) -> dict:
    return {
        "root_id": root_id,
        "sid": root_id,
        "source": "provider_stream",
        "source_stream_id": f"run-{root_id}",
        "source_event_id": f"event-{index}",
        "content_hash": digest(f"content-{index}"),
        "payload_type": "assistant_output",
        "payload": {"message_id": "m1", "text": f"text-{index}"},
        "turn_id": "m1",
    }


class FakeSource:
    def __init__(self, pages_by_cursor: dict[int, dict]) -> None:
        self.pages = pages_by_cursor
        self.calls: list[int] = []

    async def __call__(self, root_id: str, *, after_seq: int = 0, limit: int = 500) -> dict:
        self.calls.append(after_seq)
        return self.pages[after_seq]


def admitted_count(root_id: str) -> int:
    service, catalog = chat_projection_ingestion._instances()
    generation = catalog.root_generation(root_id)
    authority = service.register(
        provider="claude", session_id=root_id, root_id=root_id,
        root_generation=generation, store_kind="jsonl",
    )
    return len(service.read_facts(authority))


def test_pull_admits_pages_and_persists_cursor() -> None:
    root = "feedroot"
    source = FakeSource({
        0: {
            "found": True, "provider_kind": "claude",
            "facts": [wire_fact(root, 1), wire_fact(root, 2)],
            "next_seq": 2, "has_more": True,
        },
        2: {
            "found": True, "provider_kind": "claude",
            "facts": [wire_fact(root, 3)],
            "next_seq": 3, "has_more": False,
        },
    })
    client = bff_chat_feed.ChatFeedClient(source_reader=source)
    asyncio.run(client._pull_root(root))
    check("pull pages to head", source.calls == [0, 2])
    check("all facts admitted", admitted_count(root) == 3)
    cursors = json.loads(bff_chat_feed._cursors_path().read_text(encoding="utf-8"))
    check("cursor persisted at head", cursors.get(root) == 3)

    replay = bff_chat_feed.ChatFeedClient(source_reader=source)
    asyncio.run(replay._pull_root(root))
    check("re-delivery from seq 0 is idempotent", admitted_count(root) == 3)


def test_missing_provider_kind_fails_closed() -> None:
    root = "noprovider"
    source = FakeSource({
        0: {
            "found": True,
            "facts": [wire_fact(root, 1)],
            "next_seq": 1, "has_more": False,
        },
    })
    client = bff_chat_feed.ChatFeedClient(source_reader=source)
    asyncio.run(client._pull_root(root))
    check("no admission without provider identity", admitted_count(root) == 0)
    cursors = json.loads(bff_chat_feed._cursors_path().read_text(encoding="utf-8"))
    check("cursor not advanced without provider identity", root not in cursors)


def test_deleted_root_drops_cursor() -> None:
    root = "gone"
    client = bff_chat_feed.ChatFeedClient(source_reader=FakeSource({7: {"found": False}}))
    client._cursors[root] = 7
    asyncio.run(client._pull_root(root))
    cursors = json.loads(bff_chat_feed._cursors_path().read_text(encoding="utf-8"))
    check("deleted root drops cursor", root not in cursors)


def test_frames_mark_dirty_and_malformed_frames_drop() -> None:
    client = bff_chat_feed.ChatFeedClient(source_reader=FakeSource({}))
    client._handle_frame(json.dumps({"type": "canonical_advance", "roots": ["r1", "", 3, "r2"]}))
    check("advance frame marks valid roots dirty", client._dirty == {"r1", "r2"})
    client._handle_frame("{not json")
    client._handle_frame(json.dumps({"type": "other", "roots": ["r3"]}))
    client._handle_frame(json.dumps({"type": "canonical_advance", "roots": "r4"}))
    check("malformed and foreign frames are ignored", client._dirty == {"r1", "r2"})
    status = client.status("r1")
    check("status reports pending pull and cursor",
          status["pending_pull"] is True and status["cursor"] == 0
          and status["connected"] is False)


if __name__ == "__main__":
    try:
        test_pull_admits_pages_and_persists_cursor()
        test_missing_provider_kind_fails_closed()
        test_deleted_root_drops_cursor()
        test_frames_mark_dirty_and_malformed_frames_drop()
        chat_projection_ingestion.close()
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
    if _failures:
        print(f"{len(_failures)} test(s) FAILED")
        sys.exit(1)
    print("all bff chat feed tests passed")
