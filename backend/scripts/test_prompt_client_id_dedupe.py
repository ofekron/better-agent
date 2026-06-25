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

import main  # noqa: E402
import session_queue_projection  # noqa: E402
import session_store  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402


PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


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
        ok = (
            first_id == "queued-first"
            and second_id == "queued-first"
            and q.empty()
            and coord._active_prompt_client_ids.get((sid, "client-race")) == "queued-first"
            and not (raw.get("queued_prompts") or [])
        )
    finally:
        coord._processor_tasks.pop(sid, None)
        coord._prompt_queues.pop(sid, None)
        coord._queued_ids.pop(sid, None)
        coord._active_prompt_client_ids.clear()
        coord._prompt_client_id_by_item.clear()

    print(f"{PASS if ok else FAIL} duplicate client id dedupes during dequeue gap")
    return ok


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
    ok = (
        stored_first and stored_first.get("id") == "user-1"
        and stored_second and stored_second.get("id") == "user-1"
        and len(matches) == 1
        and matches[0].get("content") == "first"
    )
    print(f"{PASS if ok else FAIL} append_user_msg dedupes client id")
    return ok


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
    ok = (
        admission.get("admitted") is False
        and (admission.get("existing_user_message") or {}).get("id") == "user-existing"
        and not (raw.get("queued_prompts") or [])
    )
    print(f"{PASS if ok else FAIL} queued prompt rejects existing user client id")
    return ok


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
    ok = (
        first.get("admitted") is True
        and second.get("admitted") is False
        and (second.get("existing_queued_prompt") or {}).get("id") == "queued-existing"
        and len(queued) == 1
        and queued[0].get("content") == "first"
    )
    print(f"{PASS if ok else FAIL} queued prompt rejects existing queued client id")
    return ok


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
    ok = (
        live_ref_seen["value"] is False
        and [m.get("id") for m in record.get("user_messages") or []]
        == ["user-projection"]
    )
    print(
        f"{PASS if ok else FAIL} append_user_msg queue projection uses locked snapshot",
    )
    return ok


def main_runner() -> int:
    tests = [
        test_duplicate_client_id_dedupes_during_dequeue_gap,
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
