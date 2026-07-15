#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import multiprocessing
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from dataclasses import replace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
HOME = Path(tempfile.mkdtemp(prefix="better-agent-projection-service-"))
os.environ["BETTER_AGENT_HOME"] = str(HOME)
sys.path.insert(0, str(ROOT / "backend"))

from chat_projection_authority import ProjectionAuthority, ProjectionAuthorityRegistry
from chat_projection_authority import ProjectionAuthorityError
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


def register_worker(arguments: dict, ready, start, results) -> None:
    selected = None
    try:
        selected = ProjectionAuthorityRegistry()
        ready.put(True)
        start.wait()
        authority = selected.register(**arguments)
        results.put(("ok", authority.authority_id))
    except ProjectionAuthorityError as exc:
        results.put(("error", exc.code))
    finally:
        if selected is not None:
            selected.close()


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
    admission_key = (authority.authority_id, "provider", 0)
    service._admission(authority, admission_key, "provider", 0)
    results = []
    failures = []
    def commit(sequence: int) -> None:
        try:
            results.append(service.append_apply(
                authority, request(sequence, root="concurrent-root"),
            ))
        except BaseException as exc:
            failures.append(exc)
    def wait_until_buffered(sequence: int) -> None:
        deadline = time.monotonic() + 5
        admission = service._admissions[admission_key]
        with admission.condition:
            while sequence not in admission.pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError("sequence was not buffered")
                admission.condition.wait(remaining)
    threads = []
    for sequence in range(20, 0, -1):
        entered = threading.Event()
        thread = threading.Thread(target=lambda value=sequence, gate=entered: (
            gate.set(), commit(value),
        ))
        threads.append(thread)
        thread.start()
        assert entered.wait(1)
        if sequence > 1:
            wait_until_buffered(sequence)
    for thread in threads:
        thread.join()
    assert not failures and len(results) == 20
    assert service.projection_cursor(authority) == 20
    assert service.append_apply(
        authority, request(20, root="concurrent-root"),
    ).duplicate
    assert_error("sequence_conflict", lambda: service.append_apply(
        authority,
        replace(
            request(20, root="concurrent-root"), event_id="different-event",
            content_hash="1" * 64,
        ),
    ))
    assert_error("watermark_regression", lambda: service.append_apply(
        authority, request(19, root="concurrent-root"),
    ))

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


def test_registry_version_multiprocess_and_symlink_security() -> None:
    version_path = HOME / "version" / "authority.sqlite3"
    version_path.parent.mkdir(parents=True)
    connection = sqlite3.connect(version_path)
    connection.execute("PRAGMA user_version=99")
    connection.commit()
    connection.close()
    version_path.chmod(0o600)
    try:
        ProjectionAuthorityRegistry(version_path)
        raise AssertionError("unsupported empty version must fail")
    except ProjectionAuthorityError as exc:
        assert exc.code == "unsupported_authority_schema"
    connection = sqlite3.connect(version_path)
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 99
    assert not connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table'"
    ).fetchone()
    connection.close()

    context = multiprocessing.get_context("fork")
    base = {
        "provider": "claude", "session_id": "process-session", "root_id": "process-root",
        "root_generation": 0, "store_kind": "jsonl",
    }
    ready, results, start = context.Queue(), context.Queue(), context.Event()
    processes = [
        context.Process(target=register_worker, args=(base, ready, start, results))
        for _ in range(4)
    ]
    for process in processes:
        process.start()
    for _ in processes:
        assert ready.get(timeout=10)
    start.set()
    outcomes = [results.get(timeout=20) for _ in processes]
    for process in processes:
        process.join(20)
        assert process.exitcode == 0
    assert {status for status, _ in outcomes} == {"ok"}
    assert len({value for _, value in outcomes}) == 1

    conflict_a = {**base, "session_id": "race-session", "root_id": "race-root-a"}
    conflict_b = {**base, "session_id": "race-session", "root_id": "race-root-b"}
    ready, results, start = context.Queue(), context.Queue(), context.Event()
    processes = [
        context.Process(target=register_worker, args=(arguments, ready, start, results))
        for arguments in (conflict_a, conflict_b)
    ]
    for process in processes:
        process.start()
    for _ in processes:
        assert ready.get(timeout=10)
    start.set()
    outcomes = [results.get(timeout=20) for _ in processes]
    for process in processes:
        process.join(20)
        assert process.exitcode == 0
    assert sorted(status for status, _ in outcomes) == ["error", "ok"]
    assert {value for status, value in outcomes if status == "error"} == {"authority_conflict"}

    catalog = ProjectionAuthorityRegistry()
    catalog.register(
        provider="claude", session_id="mixed-a", root_id="mixed-root-a",
        root_generation=0, store_kind="jsonl",
    )
    catalog.register(
        provider="claude", session_id="mixed-b", root_id="mixed-root-b",
        root_generation=0, store_kind="jsonl",
    )
    try:
        catalog.register(
            provider="claude", session_id="mixed-a", root_id="mixed-root-b",
            root_generation=0, store_kind="jsonl",
        )
        raise AssertionError("mixed authority must fail")
    except ProjectionAuthorityError as exc:
        assert exc.code == "mixed_authority"
    catalog.close()

    outside = Path(tempfile.mkdtemp(prefix="better-agent-authority-outside-"))
    unsafe_parent = HOME / "unsafe-parent"
    unsafe_parent.symlink_to(outside, target_is_directory=True)
    try:
        ProjectionAuthorityRegistry(unsafe_parent / "authority.sqlite3")
        raise AssertionError("symlink parent must fail")
    except ProjectionAuthorityError as exc:
        assert exc.code == "path_escape"
    assert not list(outside.iterdir())
    unsafe_parent.unlink()

    anchored = ProjectionAuthorityRegistry()
    chat = HOME / "chat"
    held = HOME / "chat-held"
    outside_swap = Path(tempfile.mkdtemp(prefix="better-agent-authority-swap-"))
    chat.rename(held)
    chat.symlink_to(outside_swap, target_is_directory=True)
    anchored.register(
        provider="gemini", session_id="anchored", root_id="anchored-root",
        root_generation=0, store_kind="jsonl",
    )
    anchored.close()
    assert not list(outside_swap.iterdir())
    chat.unlink()
    held.rename(chat)
    shutil.rmtree(outside, ignore_errors=True)
    shutil.rmtree(outside_swap, ignore_errors=True)


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
        test_registry_version_multiprocess_and_symlink_security()
        print("PASS test_registry_version_multiprocess_and_symlink_security")
    finally:
        shutil.rmtree(HOME, ignore_errors=True)


if __name__ == "__main__":
    main()
