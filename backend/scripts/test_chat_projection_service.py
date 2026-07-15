#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
import threading
from dataclasses import replace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
HOME = Path(tempfile.mkdtemp(prefix="better-agent-projection-service-"))
os.environ["BETTER_AGENT_HOME"] = str(HOME)
sys.path.insert(0, str(ROOT / "backend"))

from chat_projection_authority import ProjectionAuthority, ProjectionAuthorityRegistry
from chat_projection_service import CanonicalChatProjectionService, ProjectionServiceError
from chat_projection_store import ProjectionCommit, SourceWatermark, TurnManifest
from chat_projection_store_jsonl import JsonlChatProjectionStore
from chat_projection_store_sqlite import SQLiteChatProjectionStore, canonical_json


def request(sequence: int, *, root: str = "root", generation: int = 0) -> ProjectionCommit:
    event_id = f"event-{sequence}"
    fact = {"event_id": event_id, "type": "assistant", "text": f"answer-{sequence}"}
    digest = hashlib.sha256(canonical_json(fact).encode("utf-8")).hexdigest()
    return ProjectionCommit(
        root_id=root, root_generation=generation, event_id=event_id, content_hash=digest,
        canonical_fact=fact, render_node={"type": "Explanation", "text": fact["text"]},
        turn_id="turn", message_id="message", parent_event_id=None, owner_scope="turn:turn",
        manifest=TurnManifest("turn", sequence, sequence),
        visible_delta={"append": event_id}, historical_revision={"event_id": event_id},
        watermark=SourceWatermark("provider", 0, sequence),
    )


def assert_error(code: str, callback) -> None:
    try:
        callback()
    except ProjectionServiceError as exc:
        assert exc.code == code, (exc.code, code)
        return
    raise AssertionError(f"expected {code}")


def registry() -> ProjectionAuthorityRegistry:
    return ProjectionAuthorityRegistry()


def test_authority_selection_provider_parity_and_fail_closed_mixes() -> None:
    service = CanonicalChatProjectionService(registry())
    authorities = []
    for provider in ("claude", "codex", "gemini"):
        authorities.append(service.register(
            provider=provider, session_id=f"session-{provider}", root_id=f"root-{provider}",
            root_generation=0, store_kind="jsonl",
        ))
    assert {item.provider for item in authorities} == {"claude", "codex", "gemini"}
    assert len({item.store_path for item in authorities}) == 3
    assert all(item.store_path.resolve().is_relative_to(HOME.resolve()) for item in authorities)
    same = service.register(
        provider="claude", session_id="session-claude", root_id="root-claude",
        root_generation=0, store_kind="jsonl",
    )
    assert same == authorities[0]
    assert_error("authority_conflict", lambda: service.register(
        provider="codex", session_id="session-claude", root_id="root-claude",
        root_generation=0, store_kind="jsonl",
    ))
    assert_error("authority_conflict", lambda: service.register(
        provider="claude", session_id="session-claude", root_id="another-root",
        root_generation=0, store_kind="jsonl",
    ))
    assert_error("invalid_authority", lambda: service.register(
        provider="unknown", session_id="bad", root_id="bad", root_generation=0,
        store_kind="jsonl",
    ))
    forged = replace(authorities[0], root_generation=1)
    assert_error("authority_mismatch", lambda: service.projection_cursor(forged))
    missing = ProjectionAuthority("f" * 64, "claude", "missing", "missing", 0, "jsonl", HOME / "x")
    assert_error("authority_missing", lambda: service.projection_cursor(missing))
    service.close()


def test_append_durability_restart_recovery_and_rebuild() -> None:
    service = CanonicalChatProjectionService(registry())
    authority = service.register(
        provider="claude", session_id="session", root_id="root",
        root_generation=0, store_kind="jsonl",
    )
    changes = []
    token = service.subscribe(authority, changes.append)
    result = service.append_apply(authority, request(1))
    assert result.projection_cursor == 1
    assert changes[-1].kind == "committed" and changes[-1].event_id == "event-1"
    service.unsubscribe(authority, token)
    assert_error("subscription_missing", lambda: service.unsubscribe(authority, token))
    service.close()

    reopened = CanonicalChatProjectionService(registry())
    authority = reopened.register(
        provider="claude", session_id="session", root_id="root",
        root_generation=0, store_kind="jsonl",
    )
    assert reopened.projection_cursor(authority) == 1
    assert [item.fact_sequence for item in reopened.read_facts(authority)] == [1]
    assert [item.revision for item in reopened.read_revisions(authority)] == [1]
    assert reopened.source_watermark(authority, "provider").sequence == 1
    assert reopened.read_projection(authority, "event-1").render_node["text"] == "answer-1"
    slots_before = set(authority.store_path.parent.glob(f"{authority.store_path.name}.index.*.sqlite3"))
    assert reopened.rebuild(authority) == 1
    slots_after = set(authority.store_path.parent.glob(f"{authority.store_path.name}.index.*.sqlite3"))
    assert len(slots_after) == len(slots_before) + 1 and slots_before < slots_after
    reopened.close()


def test_post_fsync_failure_recovers_through_same_apply_path() -> None:
    setup = CanonicalChatProjectionService(registry())
    authority = setup.register(
        provider="gemini", session_id="crash-session", root_id="crash-root",
        root_generation=0, store_kind="jsonl",
    )
    setup.close()

    faulted = CanonicalChatProjectionService(
        registry(), _store_factories={
            "jsonl": lambda selected: JsonlChatProjectionStore(
                selected.store_path, _test_owner_fault="post_append_failure",
            ),
            "sqlite": lambda selected: SQLiteChatProjectionStore(selected.store_path),
        },
    )
    authority = faulted.register(
        provider="gemini", session_id="crash-session", root_id="crash-root",
        root_generation=0, store_kind="jsonl",
    )
    assert_error("storage_write_failed", lambda: faulted.append_apply(
        authority, request(1, root="crash-root"),
    ))
    faulted.close()

    recovered = CanonicalChatProjectionService(registry())
    authority = recovered.register(
        provider="gemini", session_id="crash-session", root_id="crash-root",
        root_generation=0, store_kind="jsonl",
    )
    assert recovered.projection_cursor(authority) == 1
    assert recovered.read_projection(authority, "event-1").render_node["text"] == "answer-1"
    recovered.close()


def test_concurrency_sqlite_selection_and_lifecycle_errors() -> None:
    service = CanonicalChatProjectionService(registry())
    authority = service.register(
        provider="codex", session_id="concurrent", root_id="concurrent-root",
        root_generation=0, store_kind="jsonl",
    )
    results = []
    failures = []
    def commit(sequence: int) -> None:
        try:
            results.append(service.append_apply(
                authority, request(sequence, root="concurrent-root"),
            ))
        except BaseException as exc:
            failures.append(exc)
    threads = [threading.Thread(target=commit, args=(sequence,)) for sequence in range(1, 21)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not failures and len(results) == 20
    assert service.projection_cursor(authority) == 20

    sqlite_authority = service.register(
        provider="claude", session_id="sqlite", root_id="sqlite-root",
        root_generation=0, store_kind="sqlite",
    )
    assert service.append_apply(sqlite_authority, request(1, root="sqlite-root")).projection_cursor == 1
    assert_error("rebuild_unsupported", lambda: service.rebuild(sqlite_authority))
    assert_error("hash_mismatch", lambda: service.append_apply(
        authority, replace(request(21, root="concurrent-root"), content_hash="0" * 64),
    ))
    assert_error("authority_mismatch", lambda: service.append_apply(
        authority, request(21, root="wrong-root"),
    ))
    service.close()
    assert_error("service_closed", lambda: service.projection_cursor(authority))
    service.close()


def main() -> None:
    try:
        test_authority_selection_provider_parity_and_fail_closed_mixes()
        print("PASS test_authority_selection_provider_parity_and_fail_closed_mixes")
        test_append_durability_restart_recovery_and_rebuild()
        print("PASS test_append_durability_restart_recovery_and_rebuild")
        test_post_fsync_failure_recovers_through_same_apply_path()
        print("PASS test_post_fsync_failure_recovers_through_same_apply_path")
        test_concurrency_sqlite_selection_and_lifecycle_errors()
        print("PASS test_concurrency_sqlite_selection_and_lifecycle_errors")
    finally:
        shutil.rmtree(HOME, ignore_errors=True)


if __name__ == "__main__":
    main()
