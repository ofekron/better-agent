"""Fix B: bounded initial read for the chat tree.

Locks the window-first read path:

  A. Store level — read_turn_window returns only the facts owned by the
     last-N pane turns (plus one extra older turn for has-older
     detection, plus the cursor turn on load-more); an unknown cursor
     turn reports cursor_found=False; pane prompts of other panes are
     excluded.
  B. Endpoint level — GET /api/chat-tree never pages the full fact log
     (read_facts is not called; the facts shipped are bounded by the
     window, not by history size).
  C. Equivalence — the bounded window's items/lookup are byte-identical
     to projecting ALL facts and windowing afterwards.

Run with:
    cd backend && .venv/bin/python scripts/test_bff_chat_tree_bounded_read.py
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
_TMP_HOME = _test_home.isolate("bc-test-bounded-read-")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import bff_chat_lookup  # noqa: E402
import bff_chat_tree  # noqa: E402
import chat_projection_ingestion  # noqa: E402
from bff_chat_render import render_chat  # noqa: E402
from bff_runtime_service import RuntimeServiceError, runtime_service  # noqa: E402
from chat_projection_service import CanonicalChatProjectionService  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"
_failures: list[str] = []

ROOT = "bigroot"
TURNS_SEEDED = 30
FACTS_PER_TURN = 3


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
        "root_id": ROOT,
        "sid": ROOT,
        "source": "provider_stream",
        "source_stream_id": "run-1",
        "content_hash": digest(f"content-{seq}"),
        "payload_type": payload_type,
        "payload": payload,
        "observed_at": f"2026-07-15T{10 + seq // 3600:02d}:{(seq // 60) % 60:02d}:{seq % 60:02d}Z",
        "source_timestamp": None,
        "turn_id": payload.get("prompt_message_id") or payload.get("message_id") or "u1",
    }


SESSION = {
    "id": ROOT,
    "provider_id": "claude",
    "model": "sonnet-4-6",
    "reasoning_effort": "high",
    "messages": [
        entry for turn in range(1, TURNS_SEEDED + 1) for entry in (
            {"id": f"u{turn}", "role": "user", "seq": turn * 2 - 1},
            {"id": f"a{turn}", "role": "assistant", "seq": turn * 2,
             "run_meta": {"provider_id": "claude", "model": "sonnet-4-6",
                          "reasoning_effort": "high"}},
        )
    ],
}


async def fake_session_tree(session_id: str, *, exchange_count=None):
    if session_id != ROOT:
        raise RuntimeServiceError(404, "session not found")
    return {"tree": SESSION, "provider_kind": "claude"}


def seed() -> None:
    seq = 0
    for turn in range(1, TURNS_SEEDED + 1):
        for payload_type, payload in (
            ("user_prompt", {"message_id": f"u{turn}", "text": f"prompt {turn}"}),
            ("message_ownership_declared",
             {"message_id": f"a{turn}", "prompt_message_id": f"u{turn}"}),
            ("assistant_output",
             {"message_id": f"a{turn}", "text": f"answer {turn}", "final": True}),
        ):
            seq += 1
            chat_projection_ingestion.admit_canonical_fact(
                wire_fact(seq, payload_type, payload), provider="claude",
            )


def turn_of(fact) -> str:
    payload = fact.canonical_fact.get("payload") or {}
    message_id = str(payload.get("message_id") or "")
    return message_id.replace("a", "u", 1) if message_id.startswith("a") else message_id


def registered_authority():
    service, catalog = chat_projection_ingestion._instances()
    authority = service.register(
        provider="claude", session_id=ROOT, root_id=ROOT,
        root_generation=catalog.root_generation(ROOT), store_kind="jsonl",
    )
    return service, authority


def test_store_level() -> None:
    service, authority = registered_authority()
    page = service.read_turn_window(authority, pane_id=ROOT, turns=5)
    turns = sorted({turn_of(fact) for fact in page.facts}, key=lambda t: int(t[1:]))
    check("latest window reads only the last turns+1 turns",
          page.cursor_found
          and turns == [f"u{t}" for t in range(25, 31)]
          and len(page.facts) == 6 * FACTS_PER_TURN)
    page = service.read_turn_window(authority, pane_id=ROOT, turns=5, before_turn="u25")
    turns = sorted({turn_of(fact) for fact in page.facts}, key=lambda t: int(t[1:]))
    check("load-more window reads cursor turn + turns+1 preceding",
          page.cursor_found
          and turns == [f"u{t}" for t in range(19, 26)]
          and len(page.facts) == 7 * FACTS_PER_TURN)
    page = service.read_turn_window(authority, pane_id=ROOT, turns=5, before_turn="ghost")
    check("unknown cursor turn reports cursor_found=False",
          page.cursor_found is False and page.facts == ())
    page = service.read_turn_window(authority, pane_id="not-a-pane", turns=5)
    check("foreign pane has no window facts but a live head",
          page.cursor_found and page.facts == () and page.projection_cursor > 0)


def test_endpoint_bounded_and_equivalent() -> None:
    service, authority = registered_authority()
    all_facts = []
    after = 0
    while True:
        rows = service.read_facts(authority, after=after, limit=1000)
        all_facts.extend(dict(row.canonical_fact) for row in rows)
        if len(rows) < 1000:
            break
        after = rows[-1].fact_sequence
    check("fixture seeded the full fact log",
          len(all_facts) == TURNS_SEEDED * FACTS_PER_TURN)
    full_rendered = render_chat(all_facts, SESSION, pane_id=None)
    expected_window, expected_older = bff_chat_tree._window_items(
        full_rendered.items, 5, None,
    )
    expected_lookup = bff_chat_lookup.build_lookup(
        expected_window, full_rendered.adapted.messages,
        full_rendered.adapted.events, SESSION,
    )

    read_facts_calls: list[int] = []
    window_rows: list[int] = []
    original_read_facts = CanonicalChatProjectionService.read_facts
    original_read_window = CanonicalChatProjectionService.read_turn_window

    def spy_read_facts(self, authority, *, after=0, limit=1000):
        rows = original_read_facts(self, authority, after=after, limit=limit)
        read_facts_calls.append(len(rows))
        return rows

    def spy_read_window(self, authority, **kwargs):
        page = original_read_window(self, authority, **kwargs)
        window_rows.append(len(page.facts))
        return page

    CanonicalChatProjectionService.read_facts = spy_read_facts
    CanonicalChatProjectionService.read_turn_window = spy_read_window
    original = runtime_service.session_tree
    runtime_service.session_tree = fake_session_tree
    app = FastAPI()
    app.include_router(bff_chat_tree.router)
    client = TestClient(app)
    try:
        response = client.get(f"/api/chat-tree/{ROOT}")
        body = response.json()
        check("windowed GET succeeds", response.status_code == 200)
        check("GET never pages the full fact log via read_facts",
              read_facts_calls == [])
        check("facts shipped are bounded by the window, not history",
              sum(window_rows) == 6 * FACTS_PER_TURN)
        check("bounded window items equal full-projection windowing",
              body["items"] == expected_window)
        check("bounded window has an older page exactly when full projection does",
              body["page"]["has_older"] is (expected_older is not None)
              and body["page"]["has_older"] is True)
        check("bounded window lookup equals full-projection lookup",
              body["lookup"] == expected_lookup)

        # Load-more through the signed cursor is equally bounded.
        window_rows.clear()
        response = client.get(
            f"/api/chat-tree/{ROOT}?cursor={body['page']['page_cursor']}",
        )
        older = response.json()
        older_turns = [item["id"] for item in older["items"] if item["type"] == "Turn"]
        check("load-more pages the exact preceding turns",
              response.status_code == 200
              and older_turns == [f"u{t}" for t in range(21, 26)])
        check("load-more is bounded too",
              read_facts_calls == [] and sum(window_rows) == 7 * FACTS_PER_TURN)
    finally:
        CanonicalChatProjectionService.read_facts = original_read_facts
        CanonicalChatProjectionService.read_turn_window = original_read_window
        runtime_service.session_tree = original


def main() -> None:
    seed()
    try:
        test_store_level()
        test_endpoint_bounded_and_equivalent()
    finally:
        chat_projection_ingestion.close()


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
    if _failures:
        print(f"{len(_failures)} test(s) FAILED")
        sys.exit(1)
    print("all bounded read tests passed")
