from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-prompt-dedupe-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import runtime_ownership  # noqa: E402
runtime_ownership.register_current_process_writer()

import main  # noqa: E402
import session_queue_projection  # noqa: E402
import session_store  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402


PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _report(name: str, checks: list[tuple[str, bool, object]]) -> bool:
    ok = all(passed for _label, passed, _detail in checks)
    print(f"{PASS if ok else FAIL} {name}")
    if not ok:
        for label, passed, detail in checks:
            if not passed:
                print(f"    failed invariant: {label} — got {detail!r}")
    return ok


def _reset_home() -> None:
    sessions_dir = Path(_TMP_HOME) / "sessions"
    if sessions_dir.exists():
        shutil.rmtree(sessions_dir)
    sessions_dir.mkdir(parents=True, exist_ok=True)
    session_store._fork_index.clear()
    session_store._index_loaded = False
    session_manager._roots.clear()
    session_manager._node_root_id.clear()
    session_manager._root_locks.clear()
    session_manager._batches.clear()


def _create_session() -> str:
    sess = session_manager.create(name="prompt-dedupe", cwd="/tmp", orchestration_mode="native")
    return sess["id"]


def test_duplicate_client_id_dedupes_during_dequeue_gap() -> bool:
    _reset_home()
    sid = _create_session()
    coord = main.coordinator

    class FakeTask:
        def done(self) -> bool:
            return False

    coord._prompt_queues.pop(sid, None)
    coord._queued_ids.pop(sid, None)
    coord._active_prompt_client_ids.clear()
    coord._prompt_client_id_by_item.clear()
    coord._processor_tasks[sid] = FakeTask()
    try:
        first_id = coord.submit_prompt(sid, {
            "_queued_id": "queued-first",
            "prompt": "same prompt",
            "app_session_id": sid,
            "client_id": "client-race",
        })
        q = coord._prompt_queues[sid]
        first = q.get_nowait()
        ids = coord._queued_ids.get(sid, [])
        if first.get("_queued_id") in ids:
            ids.remove(first.get("_queued_id"))

        session_manager.add_queued_prompt(sid, {
            "id": "queued-second",
            "kind": "send",
            "content": "same prompt",
            "client_id": "client-race",
        })
        second_id = coord.submit_prompt(sid, {
            "_queued_id": "queued-second",
            "prompt": "same prompt",
            "app_session_id": sid,
            "client_id": "client-race",
        })
        session_manager.flush_pending_persists()
        raw = session_store.get_session(sid) or {}
        checks = [
            ("first submit returns its own id", first_id == "queued-first", first_id),
            ("second submit dedupes to first id", second_id == "queued-first", second_id),
            ("queue drained", q.empty(), q.qsize()),
            (
                "client id claim held by first item",
                coord._active_prompt_client_ids.get((sid, "client-race")) == "queued-first",
                coord._active_prompt_client_ids.get((sid, "client-race")),
            ),
            (
                "no queued_prompts persisted on disk",
                not (raw.get("queued_prompts") or []),
                raw.get("queued_prompts"),
            ),
        ]
    finally:
        coord._processor_tasks.pop(sid, None)
        coord._prompt_queues.pop(sid, None)
        coord._queued_ids.pop(sid, None)
        coord._active_prompt_client_ids.clear()
        coord._prompt_client_id_by_item.clear()

    return _report("duplicate client id dedupes during dequeue gap", checks)


def test_append_user_msg_dedupes_client_id() -> bool:
    _reset_home()
    sid = _create_session()
    first = {
        "id": "user-1",
        "role": "user",
        "content": "first",
        "events": [],
        "client_id": "client-final-guard",
    }
    second = {
        "id": "user-2",
        "role": "user",
        "content": "second",
        "events": [],
        "client_id": "client-final-guard",
    }
    stored_first = session_manager.append_user_msg(sid, first)
    stored_second = session_manager.append_user_msg(sid, second)
    session_manager.flush_pending_persists()
    raw = session_store.get_session(sid) or {}
    matches = [
        m for m in raw.get("messages") or []
        if m.get("client_id") == "client-final-guard"
    ]
    checks = [
        ("first append stored", bool(stored_first) and stored_first.get("id") == "user-1", stored_first),
        ("second append dedupes to first", bool(stored_second) and stored_second.get("id") == "user-1", stored_second),
        ("one persisted message for client id", len(matches) == 1, matches),
        ("persisted content is first", bool(matches) and matches[0].get("content") == "first", matches),
    ]
    return _report("append_user_msg dedupes client id", checks)


def test_queued_prompt_rejects_existing_user_client_id() -> bool:
    _reset_home()
    sid = _create_session()
    session_manager.append_user_msg(sid, {
        "id": "user-existing",
        "role": "user",
        "content": "already sent",
        "client_id": "client-admitted-user",
    })
    admission = session_manager.admit_queued_prompt(sid, {
        "id": "queued-duplicate",
        "kind": "queued_behind",
        "content": "already sent",
        "client_id": "client-admitted-user",
    })
    session_manager.flush_pending_persists()
    raw = session_store.get_session(sid) or {}
    checks = [
        ("admission rejected", admission.get("admitted") is False, admission.get("admitted")),
        (
            "existing user message surfaced",
            (admission.get("existing_user_message") or {}).get("id") == "user-existing",
            admission.get("existing_user_message"),
        ),
        (
            "no queued_prompts persisted on disk",
            not (raw.get("queued_prompts") or []),
            raw.get("queued_prompts"),
        ),
    ]
    return _report("queued prompt rejects existing user client id", checks)


def test_queued_prompt_rejects_existing_queued_client_id() -> bool:
    _reset_home()
    sid = _create_session()
    first = session_manager.admit_queued_prompt(sid, {
        "id": "queued-existing",
        "kind": "queued_behind",
        "content": "first",
        "client_id": "client-admitted-queued",
    })
    second = session_manager.admit_queued_prompt(sid, {
        "id": "queued-duplicate",
        "kind": "queued_behind",
        "content": "second",
        "client_id": "client-admitted-queued",
    })
    session_manager.flush_pending_persists()
    raw = session_store.get_session(sid) or {}
    queued = raw.get("queued_prompts") or []
    checks = [
        ("first admission accepted", first.get("admitted") is True, first.get("admitted")),
        ("second admission rejected", second.get("admitted") is False, second.get("admitted")),
        (
            "existing queued prompt surfaced",
            (second.get("existing_queued_prompt") or {}).get("id") == "queued-existing",
            second.get("existing_queued_prompt"),
        ),
        ("one queued prompt persisted", len(queued) == 1, queued),
        ("persisted content is first", bool(queued) and queued[0].get("content") == "first", queued),
    ]
    return _report("queued prompt rejects existing queued client id", checks)


def test_append_user_msg_queue_projection_uses_locked_snapshot() -> bool:
    _reset_home()
    sid = _create_session()
    original = session_queue_projection.upsert_from_session
    live_ref_seen = {"value": False}

    def spy(session: dict) -> None:
        live_ref_seen["value"] = session is session_manager.get_ref(sid)
        original(session)

    session_queue_projection.upsert_from_session = spy
    try:
        session_manager.append_user_msg(sid, {
            "id": "user-projection",
            "role": "user",
            "content": "projection",
            "client_id": "client-projection",
        })
    finally:
        session_queue_projection.upsert_from_session = original

    record = session_queue_projection.get(sid) or {}
    checks = [
        ("projection received a snapshot, not the live ref", live_ref_seen["value"] is False, live_ref_seen["value"]),
        (
            "projection ack recorded",
            (record.get("user_message_acks") or {}).get("client-projection", {}).get("id")
            == "user-projection",
            record.get("user_message_acks"),
        ),
    ]
    return _report("append_user_msg queue projection uses locked snapshot", checks)


def test_stale_tail_persist_cannot_resurrect_removed_prompt() -> bool:
    _reset_home()
    sid = _create_session()
    session_manager.add_queued_prompt(sid, {
        "id": "queued-stale",
        "kind": "send",
        "content": "stale",
        "client_id": "client-stale",
    })
    # Tail-persist snapshot taken while the prompt is still queued.
    stale_copy = session_store.copy_persistable_tree(session_manager.get_ref(sid))
    session_manager.remove_queued_prompt(sid, "queued-stale")
    # Delayed tail write lands after the remove: its note_persisted_tree
    # must not regress the projection past the newer remove.
    session_queue_projection.note_persisted_tree(stale_copy)
    session_manager.flush_pending_persists()
    raw = session_store.get_session(sid) or {}
    record = session_queue_projection.get(sid) or {}
    checks = [
        (
            "projection not regressed by stale tree",
            not (record.get("queued_prompts") or []),
            record.get("queued_prompts"),
        ),
        (
            "no queued_prompts persisted on disk",
            not (raw.get("queued_prompts") or []),
            raw.get("queued_prompts"),
        ),
    ]
    return _report("stale tail persist cannot resurrect removed prompt", checks)


def main_runner() -> int:
    tests = [
        test_duplicate_client_id_dedupes_during_dequeue_gap,
        test_stale_tail_persist_cannot_resurrect_removed_prompt,
        test_append_user_msg_dedupes_client_id,
        test_queued_prompt_rejects_existing_user_client_id,
        test_queued_prompt_rejects_existing_queued_client_id,
        test_append_user_msg_queue_projection_uses_locked_snapshot,
    ]
    results = [test() for test in tests]
    shutil.rmtree(_TMP_HOME, ignore_errors=True)
    failed = sum(1 for result in results if not result)
    print(f"{PASS if failed == 0 else FAIL} {len(results) - failed}/{len(results)} tests passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main_runner())
