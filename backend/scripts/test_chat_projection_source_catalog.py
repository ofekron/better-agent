#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import multiprocessing
import os
import shutil
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
HOME = Path(os.environ.get("SOURCE_CATALOG_TEST_HOME") or tempfile.mkdtemp(
    prefix="better-agent-source-catalog-",
))
os.environ["SOURCE_CATALOG_TEST_HOME"] = str(HOME)
os.environ["BETTER_AGENT_HOME"] = str(HOME)
sys.path.insert(0, str(ROOT / "backend"))

from chat_projection_source_catalog import ChatProjectionSourceCatalog
import chat_projection_ingestion


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def allocate(path: str, provider: str, stream: str, event: str, content: str, start, output) -> None:
    catalog = ChatProjectionSourceCatalog(Path(path))
    start.wait()
    identity = catalog.admit(
        root_id="root", provider=provider, stream_id=stream,
        event_id=event, content_hash=digest(content),
    )
    output.put((identity.provider, identity.stream_id, identity.generation, identity.sequence))
    catalog.close()


def test_restart_switch_mutation_and_root_generation() -> None:
    path = HOME / "catalog.sqlite3"
    catalog = ChatProjectionSourceCatalog(path)
    first = catalog.admit(
        root_id="root", provider="claude", stream_id="run-claude",
        event_id="event", content_hash=digest("v1"),
    )
    assert (first.generation, first.sequence) == (1, 1)
    assert catalog.admit(
        root_id="root", provider="claude", stream_id="run-claude",
        event_id="event", content_hash=digest("v1"),
    ) == first
    mutated = catalog.admit(
        root_id="root", provider="claude", stream_id="run-claude",
        event_id="event", content_hash=digest("v2"),
    )
    switched = catalog.admit(
        root_id="root", provider="codex", stream_id="run-codex",
        event_id="codex-event", content_hash=digest("codex"),
    )
    assert (mutated.generation, mutated.sequence) == (1, 2)
    assert (switched.generation, switched.sequence) == (2, 1)
    assert catalog.root_generation("root") == 1
    assert catalog.advance_root_generation("root") == 2
    catalog.close()

    reopened = ChatProjectionSourceCatalog(path)
    assert reopened.root_generation("root") == 2
    assert reopened.admit(
        root_id="root", provider="codex", stream_id="run-codex",
        event_id="codex-event", content_hash=digest("codex"),
    ) == switched
    gemini = reopened.admit(
        root_id="root", provider="gemini", stream_id="run-gemini",
        event_id="gemini-event", content_hash=digest("gemini"),
    )
    assert (gemini.generation, gemini.sequence) == (3, 1)
    reopened.close()


def test_concurrent_exact_allocation() -> None:
    path = HOME / "concurrent.sqlite3"
    ChatProjectionSourceCatalog(path).close()
    context = multiprocessing.get_context("spawn")
    start, output = context.Event(), context.Queue()
    processes = [
        context.Process(
            target=allocate,
            args=(str(path), "claude", "shared-run", "event", "same", start, output),
        )
        for _ in range(4)
    ]
    for process in processes:
        process.start()
    start.set()
    values = [output.get(timeout=15) for _ in processes]
    for process in processes:
        process.join(15)
        assert process.exitcode == 0
    assert values == [("claude", "shared-run", 1, 1)] * 4


def wire_fact(provider: str, index: int) -> dict:
    return {
        "root_id": "provider-root",
        "sid": "provider-root",
        "source": "provider_stream",
        "source_stream_id": f"run-{provider}",
        "source_event_id": f"event-{index}",
        "content_hash": digest(f"content-{index}"),
        "payload_type": "assistant_output",
        "payload": {"message_id": "message", "text": f"text-{index}"},
        "turn_id": f"run-{provider}",
    }


def test_provider_switch_admits_one_neutral_root_and_missing_identity_fails_closed() -> None:
    for index, provider in enumerate(("claude", "codex", "gemini"), 1):
        chat_projection_ingestion.admit_canonical_fact(
            wire_fact(provider, index), provider=provider,
        )
    service, catalog = chat_projection_ingestion._instances()
    generation = catalog.root_generation("provider-root")
    authority = service.register(
        provider="claude", session_id="provider-root", root_id="provider-root",
        root_generation=generation, store_kind="jsonl",
    )
    facts = service.read_facts(authority)
    assert [fact.canonical_fact["provider"] for fact in facts] == ["claude", "codex", "gemini"]
    assert authority.provider == "neutral"
    before = len(facts)
    for provider in ("", "tampered"):
        try:
            chat_projection_ingestion.admit_canonical_fact(
                wire_fact("claude", 99), provider=provider,
            )
            raise AssertionError("missing or tampered provider was admitted")
        except ValueError:
            pass
    assert len(service.read_facts(authority)) == before
    chat_projection_ingestion.close()


def main() -> None:
    try:
        test_restart_switch_mutation_and_root_generation()
        print("PASS test_restart_switch_mutation_and_root_generation")
        test_concurrent_exact_allocation()
        print("PASS test_concurrent_exact_allocation")
        test_provider_switch_admits_one_neutral_root_and_missing_identity_fails_closed()
        print("PASS test_provider_switch_admits_one_neutral_root_and_missing_identity_fails_closed")
    finally:
        shutil.rmtree(HOME, ignore_errors=True)


if __name__ == "__main__":
    main()
