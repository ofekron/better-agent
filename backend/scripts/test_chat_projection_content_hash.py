"""Bug-fix tests: chat projection commit_hash must be content-only.

Regression bug: `chat_projection_ingestion.admit_canonical_fact` computed
`commit_hash` over the FULL persisted fact, including attribution/routing
fields (`provider`, `event_id`, `turn_id`, and `source_event_id` — the
original-fact key that `event_id` is copied from). A misattributed replay
that re-delivers the SAME semantic content under a DIFFERENT turn_id/
event_id (e.g. from a crash-recovery bug electing a different run as the
owner of a range of events — fixed separately, out of scope here) therefore
hashed to a different value, so the hash could not identify it as the same
content.

Fix: `chat_projection_store_sqlite.content_only_hash` hashes the fact minus
those attribution fields. `chat_projection_ingestion.admit_canonical_fact`
and every independent hash recomputation in `chat_projection_store_sqlite`
(`_validate_commit`, `read_facts`, `read_revisions`, and the owner-RPC
`read_facts` validation) now share this single definition.

  A. `content_only_hash` returns the same digest for two facts whose only
     difference is `turn_id`/`event_id`/`source_event_id`/`provider`.
  B. `content_only_hash` returns a different digest when the payload itself
     differs.
  C. `chat_projection_ingestion.admit_canonical_fact`, exercised end to end
     through the real service + stores, persists matching `content_hash`
     values for two wire facts with identical payload delivered under
     different `turn_id`/`source_event_id` (and stores a different value for
     a wire fact with a different payload).

Run with:
    cd backend && .venv/bin/python scripts/test_chat_projection_content_hash.py
"""
from __future__ import annotations

import os
import shutil
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-chat-projection-content-hash-")

import chat_projection_ingestion  # noqa: E402
from chat_projection_store_sqlite import content_only_hash  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"
_failures: list[str] = []


def check(label: str, condition: bool) -> None:
    print(f"{PASS if condition else FAIL}  {label}")
    if not condition:
        _failures.append(label)


def test_content_only_hash_ignores_attribution_fields() -> None:
    base = {
        "root_id": "root", "sid": "root", "source": "provider_stream",
        "source_stream_id": "run-claude", "content_hash": "c" * 64,
        "payload_type": "assistant_output",
        "payload": {"message_id": "message", "text": "same content"},
    }
    replay_a = {**base, "provider": "claude", "event_id": "event-1", "turn_id": "turn-1", "source_event_id": "event-1"}
    replay_b = {**base, "provider": "codex", "event_id": "event-2", "turn_id": "turn-2", "source_event_id": "event-2"}
    check(
        "same payload under different provider/turn_id/event_id hashes identically",
        content_only_hash(replay_a) == content_only_hash(replay_b),
    )


def test_content_only_hash_distinguishes_different_payload() -> None:
    base = {
        "root_id": "root", "sid": "root", "source": "provider_stream",
        "source_stream_id": "run-claude", "content_hash": "c" * 64,
        "payload_type": "assistant_output", "provider": "claude",
        "event_id": "event-1", "turn_id": "turn-1", "source_event_id": "event-1",
    }
    fact_a = {**base, "payload": {"message_id": "message", "text": "content A"}}
    fact_b = {**base, "payload": {"message_id": "message", "text": "content B"}}
    check(
        "different payload hashes differently",
        content_only_hash(fact_a) != content_only_hash(fact_b),
    )


def _wire_fact(*, source_event_id: str, turn_id: str, text: str) -> dict:
    return {
        "root_id": "root-misattribution",
        "sid": "root-misattribution",
        "source": "provider_stream",
        "source_stream_id": "run-claude",
        "source_event_id": source_event_id,
        "content_hash": content_only_hash({"payload_type": "assistant_output", "payload": {"message_id": "message", "text": text}}),
        "payload_type": "assistant_output",
        "payload": {"message_id": "message", "text": text},
        "turn_id": turn_id,
    }


def test_admit_canonical_fact_content_hash_survives_misattributed_replay() -> None:
    root_id = "root-misattribution"
    chat_projection_ingestion.admit_canonical_fact(
        _wire_fact(source_event_id="event-owned-by-turn-1", turn_id="turn-1", text="same content"),
        provider="claude",
    )
    chat_projection_ingestion.admit_canonical_fact(
        _wire_fact(source_event_id="event-owned-by-turn-2", turn_id="turn-2", text="same content"),
        provider="claude",
    )
    chat_projection_ingestion.admit_canonical_fact(
        _wire_fact(source_event_id="event-owned-by-turn-3", turn_id="turn-3", text="different content"),
        provider="claude",
    )

    service, catalog = chat_projection_ingestion._instances()
    generation = catalog.root_generation(root_id)
    authority = service.register(
        provider="claude", session_id=root_id, root_id=root_id,
        root_generation=generation, store_kind="jsonl",
    )
    facts = service.read_facts(authority)
    check("all three facts were persisted", len(facts) == 3)
    same_content = [fact for fact in facts if fact.canonical_fact["payload"]["text"] == "same content"]
    different_content = [fact for fact in facts if fact.canonical_fact["payload"]["text"] == "different content"]
    check("two facts carry the same content", len(same_content) == 2)
    check(
        "content_hash matches across facts re-delivered under different turn_id/event_id",
        same_content[0].content_hash == same_content[1].content_hash,
    )
    check(
        "content_hash differs for a fact with genuinely different content",
        len(different_content) == 1 and different_content[0].content_hash != same_content[0].content_hash,
    )


if __name__ == "__main__":
    try:
        test_content_only_hash_ignores_attribution_fields()
        test_content_only_hash_distinguishes_different_payload()
        test_admit_canonical_fact_content_hash_survives_misattributed_replay()
    finally:
        chat_projection_ingestion.close()
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
    if _failures:
        print(f"{len(_failures)} test(s) FAILED")
        sys.exit(1)
    print("all chat projection content-hash tests passed")
