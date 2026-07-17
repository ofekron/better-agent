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


def wire_fact(root_id: str, index: int, text: str | None = None) -> dict:
    text = text if text is not None else f"text-{index}"
    return {
        "root_id": root_id,
        "sid": root_id,
        "source": "provider_stream",
        "source_stream_id": f"run-{root_id}",
        "source_event_id": f"event-{index}",
        "content_hash": digest(text),
        "payload_type": "assistant_output",
        "payload": {"message_id": "m1", "text": text},
        "turn_id": "m1",
    }


class FakeSource:
    def __init__(self, pages_by_cursor: dict[int, dict]) -> None:
        self.pages = pages_by_cursor
        self.calls: list[int] = []

    async def __call__(self, root_id: str, *, after_seq: int = 0, limit: int = 500) -> dict:
        self.calls.append(after_seq)
        return self.pages[after_seq]


class SlowSource(FakeSource):
    def __init__(self, pages_by_cursor: dict[int, dict]) -> None:
        super().__init__(pages_by_cursor)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, root_id: str, *, after_seq: int = 0, limit: int = 500) -> dict:
        self.calls.append(after_seq)
        self.started.set()
        await self.release.wait()
        return self.pages[after_seq]


class FailingSource:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, root_id: str, *, after_seq: int = 0, limit: int = 500) -> dict:
        from bff_runtime_service import RuntimeServiceError

        self.calls += 1
        raise RuntimeServiceError(503, "runtime unavailable")


def admitted_facts(root_id: str) -> list:
    service, catalog = chat_projection_ingestion._instances()
    generation = catalog.root_generation(root_id)
    authority = service.register(
        provider="claude", session_id=root_id, root_id=root_id,
        root_generation=generation, store_kind="jsonl",
    )
    return service.read_facts(authority)


def admitted_count(root_id: str) -> int:
    return len(admitted_facts(root_id))


def admitted_texts(root_id: str) -> list[str]:
    return [
        fact.canonical_fact.get("payload", {}).get("text")
        for fact in admitted_facts(root_id)
    ]


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


def test_pull_now_coalesces_and_cleans_up() -> None:
    async def run() -> tuple[list[int], bool, int, bool]:
        root = "coalesced"
        source = SlowSource({
            0: {
                "found": True, "provider_kind": "claude",
                "facts": [wire_fact(root, 1)],
                "next_seq": 1, "has_more": False,
            },
        })
        client = bff_chat_feed.ChatFeedClient(source_reader=source)
        first = asyncio.create_task(client.pull_now(root))
        await source.started.wait()
        second = asyncio.create_task(client.pull_now(root))
        shared = len(client._pull_tasks) == 1
        source.release.set()
        await asyncio.gather(first, second)
        return source.calls, shared, admitted_count(root), not client._pull_tasks

    calls, shared, count, cleaned = asyncio.run(run())
    check("pull_now shares one in-flight pull per root", calls == [0] and shared)
    check("pull_now admits facts and cleans task map", count == 1 and cleaned)


def test_pull_now_cleans_up_after_failure() -> None:
    async def run() -> tuple[int, bool, str]:
        source = FailingSource()
        client = bff_chat_feed.ChatFeedClient(source_reader=source)
        try:
            await client.pull_now("failing")
        except Exception as exc:
            return source.calls, not client._pull_tasks, type(exc).__name__
        return source.calls, not client._pull_tasks, ""

    calls, cleaned, exc_name = asyncio.run(run())
    check("pull_now propagates failures", calls == 1 and exc_name == "RuntimeServiceError")
    check("pull_now cleans task map after failure", cleaned)


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
    async def run() -> None:
        client = bff_chat_feed.ChatFeedClient(source_reader=FakeSource({}))
        await client._handle_frame(json.dumps({"type": "canonical_advance", "roots": ["r1", "", 3, "r2"]}))
        check("advance frame marks valid roots dirty", client._dirty == {"r1", "r2"})
        await client._handle_frame("{not json")
        await client._handle_frame(json.dumps({"type": "other", "roots": ["r3"]}))
        await client._handle_frame(json.dumps({"type": "canonical_advance", "roots": "r4"}))
        check("malformed and foreign frames are ignored", client._dirty == {"r1", "r2"})
        status = client.status("r1")
        check("status reports pending pull and cursor",
              status["pending_pull"] is True and status["cursor"] == 0
              and status["connected"] is False)

    asyncio.run(run())


def test_rewrite_behind_cursor_resets_projection_and_rebuilds() -> None:
    async def run() -> None:
        root = "rewriteroot"
        stale = FakeSource({
            0: {
                "found": True, "provider_kind": "claude",
                "facts": [wire_fact(root, 1, text="stale")],
                "next_seq": 1, "has_more": False,
            },
        })
        client = bff_chat_feed.ChatFeedClient(source_reader=stale)
        await client._pull_root(root)
        check("stale content admitted before rewrite", admitted_texts(root) == ["stale"])

        await client._handle_frame(json.dumps(
            {"type": "canonical_rewrite", "rewrites": {root: 5}}
        ))
        check("rewrite ahead of cursor is not a reset",
              root not in client._pending_resets and client._cursors.get(root) == 1)

        await client._handle_frame(json.dumps(
            {"type": "canonical_rewrite", "rewrites": {root: 1}}
        ))
        check("rewrite at/behind cursor drops cursor and flags reset",
              root in client._pending_resets and root not in client._cursors
              and root in client._dirty)
        persisted = json.loads(bff_chat_feed._resets_path().read_text(encoding="utf-8"))
        check("pending reset is durable", persisted == [root])

        # The runtime now serves the rewritten content for the same
        # source identity; without the projection reset, the durable
        # watermark would skip it and the stale text would survive.
        client._source_reader = FakeSource({
            0: {
                "found": True, "provider_kind": "claude",
                "facts": [wire_fact(root, 1, text="rewritten")],
                "next_seq": 1, "has_more": False,
            },
        })
        await client.pull_now(root)
        check("projection rebuilt with rewritten content only",
              admitted_texts(root) == ["rewritten"])
        check("reset consumed and cursor re-persisted",
              root not in client._pending_resets and client._cursors.get(root) == 1)
        persisted = json.loads(bff_chat_feed._resets_path().read_text(encoding="utf-8"))
        check("durable reset cleared after rebuild", persisted == [])

    asyncio.run(run())


if __name__ == "__main__":
    try:
        test_pull_admits_pages_and_persists_cursor()
        test_pull_now_coalesces_and_cleans_up()
        test_pull_now_cleans_up_after_failure()
        test_missing_provider_kind_fails_closed()
        test_deleted_root_drops_cursor()
        test_frames_mark_dirty_and_malformed_frames_drop()
        test_rewrite_behind_cursor_resets_projection_and_rebuilds()
        chat_projection_ingestion.close()
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
    if _failures:
        print(f"{len(_failures)} test(s) FAILED")
        sys.exit(1)
    print("all bff chat feed tests passed")
