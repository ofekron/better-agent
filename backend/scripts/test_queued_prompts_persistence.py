"""Regression test for durable queued-prompt state.

Queued prompts must live in the session snapshot as `queued_prompts[]`
instead of `messages[]`, so a frontend reload can render a queue banner
without pretending the prompt has already been sent to the agent.

Run with:
    cd backend && .venv/bin/python scripts/test_queued_prompts_persistence.py
"""

from __future__ import annotations

import json
import asyncio
import os
import shutil
import sys
from unittest import mock
import tempfile
import threading
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-queued-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import runtime_ownership  # noqa: E402
runtime_ownership.register_current_process_writer()

import session_store  # noqa: E402
import session_queue_projection  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _read_raw(sid: str) -> dict:
    with open(session_store._session_path(sid)) as f:
        return json.load(f)


def _session_record(sid: str, *, cwd: str = "/tmp/test-queued") -> dict:
    return {
        "id": sid,
        "model": "sonnet",
        "cwd": cwd,
        "messages": [],
        "queued_prompts": [],
    }


def _write_json(path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _run() -> bool:
    results: list[tuple[str, bool, str]] = []
    sess = session_manager.create(
        name="queued", model="sonnet", cwd="/tmp/test-queued",
        orchestration_mode="native", source="web",
    )
    sid = sess["id"]

    prompt = {
        "id": "queue-1",
        "lifecycle_msg_id": "life-1",
        "content": "run this after the current turn",
        "kind": "queued_behind",
        "queue_position": 0,
        "images_count": 0,
        "images": [{"media_type": "image/png", "data": "aW1hZ2U="}],
        "files": [{"name": "notes.txt", "data": "bm90ZXM=", "size": 5}],
        "orchestration_mode": "native",
        "send_target": "supervisor",
        "cli_prompt": "model-facing prompt",
        "client_id": "pending-1",
        "created_at": "2026-06-05T01:18:10",
    }
    session_manager.add_queued_prompt(sid, prompt)
    session_manager.flush_pending_persists()

    raw = _read_raw(sid)
    queued = raw.get("queued_prompts") or []
    results.append((
        "queued prompt persisted in session JSON",
        len(queued) == 1 and queued[0] == prompt,
        f"queued={queued}",
    ))
    results.append((
        "queue admission advances the durable revision",
        raw.get("queue_revision") == 1,
        f"queue_revision={raw.get('queue_revision')}",
    ))
    results.append((
        "queued prompt is not a chat message",
        not raw.get("messages"),
        f"messages={raw.get('messages')}",
    ))

    session_manager.update_queued_prompt(
        sid, "queue-1", {"content": "edited queued prompt"},
    )
    session_manager.flush_pending_persists()
    raw = _read_raw(sid)
    queued = raw.get("queued_prompts") or []
    results.append((
        "queued prompt edit persisted",
        len(queued) == 1 and queued[0].get("content") == "edited queued prompt",
        f"queued={queued}",
    ))
    results.append((
        "queue edit advances the durable revision",
        raw.get("queue_revision") == 2,
        f"queue_revision={raw.get('queue_revision')}",
    ))

    session_manager.remove_queued_prompt_by_client_id(sid, "pending-1")
    session_manager.flush_pending_persists()
    raw = _read_raw(sid)
    results.append((
        "queued prompt clears only after client id is persisted",
        raw.get("queued_prompts") == [],
        f"queued={raw.get('queued_prompts')}",
    ))
    results.append((
        "queue removal advances the durable revision",
        raw.get("queue_revision") == 3,
        f"queue_revision={raw.get('queue_revision')}",
    ))

    prompt_no_client = dict(prompt)
    prompt_no_client.pop("client_id", None)
    session_manager.add_queued_prompt(sid, prompt_no_client)
    session_manager.remove_queued_prompt(sid, "queue-1")
    session_manager.flush_pending_persists()
    raw = _read_raw(sid)
    results.append((
        "queued prompt without client id clears by queue id",
        raw.get("queued_prompts") == [],
        f"queued={raw.get('queued_prompts')}",
    ))

    session_manager.add_queued_prompt(sid, prompt)
    session_manager.remove_queued_prompt(sid, "queue-1")
    session_manager.flush_pending_persists()
    raw = _read_raw(sid)
    results.append((
        "queued prompt cleared",
        raw.get("queued_prompts") == [],
        f"queued={raw.get('queued_prompts')}",
    ))

    session_manager.add_queued_prompt(sid, prompt)
    user_msg = {
        "id": "user-1",
        "role": "user",
        "content": prompt["content"],
        "client_id": prompt["client_id"],
        "lifecycle_msg_id": prompt["lifecycle_msg_id"],
    }
    session_manager.append_user_msg(sid, user_msg)
    stale = session_store.copy_persistable_tree(session_manager.get(sid))
    clean_projection = dict(stale)
    clean_projection["queued_prompts"] = []
    session_queue_projection.upsert_from_session(clean_projection)
    session_store.write_session_full(
        stale,
        bump_updated_at=False,
        preserve_projection_fields=True,
    )
    raw = _read_raw(sid)
    queued = raw.get("queued_prompts")
    results.append((
        "stale full-session write preserves cleaned queue projection",
        queued == [],
        f"queued={queued}",
    ))

    projected = session_queue_projection.project_session(stale) or {}
    results.append((
        "queue projection ignores prompts already persisted as user messages",
        projected.get("queued_prompts") == [],
        f"projected={projected.get('queued_prompts')}",
    ))

    fork_prompt = {
        **prompt,
        "id": "fork-queue",
        "client_id": "fork-client",
        "lifecycle_msg_id": "fork-life",
    }
    root = session_store.copy_persistable_tree(session_manager.get(sid))
    fork = {
        **root,
        "id": "fork-child",
        "parent_session_id": sid,
        "forks": [],
        "messages": [],
        "queued_prompts": [{"id": "stale-fork-queue"}],
    }
    root["forks"] = [fork]
    session_queue_projection.upsert_record({
        "id": "fork-child",
        "model": root.get("model"),
        "cwd": root.get("cwd"),
        "queued_prompts": [fork_prompt],
        # Newer than the fork node's snapshot: the overlay applies only when
        # the projection does not regress the node's queue_revision.
        "queue_revision": int(fork.get("queue_revision") or 0) + 1,
        "user_messages": [],
        "user_client_ids": [],
        "user_lifecycle_msg_ids": [],
    })
    original_get = session_queue_projection.get
    per_node_get_calls = 0

    def tracking_get(session_id: str):
        nonlocal per_node_get_calls
        per_node_get_calls += 1
        return original_get(session_id)

    session_queue_projection.get = tracking_get
    try:
        session_store.write_session_full(
            root,
            bump_updated_at=False,
            preserve_projection_fields=True,
        )
    finally:
        session_queue_projection.get = original_get
    raw = _read_raw(sid)
    fork_queued = ((raw.get("forks") or [{}])[0].get("queued_prompts") or [])
    results.append((
        "queue projection overlays fork records in bulk",
        per_node_get_calls == 0 and fork_queued == [fork_prompt],
        f"calls={per_node_get_calls} queued={fork_queued}",
    ))

    session_manager.flush_pending_persists()
    sessions_dir = Path(session_store.session_file_path("queue-probe")).parent
    projection_dir = session_queue_projection._projection_dir()
    shutil.rmtree(sessions_dir, ignore_errors=True)
    shutil.rmtree(projection_dir, ignore_errors=True)
    unchanged = session_queue_projection.project_session(_session_record("unchanged"))
    changed_old = session_queue_projection.project_session(
        _session_record("changed", cwd="/tmp/old")
    )
    stale = session_queue_projection.project_session(_session_record("stale"))
    assert unchanged and changed_old and stale

    for record in (
        _session_record("unchanged"),
        _session_record("changed", cwd="/tmp/new"),
        _session_record("new"),
    ):
        _write_json(sessions_dir / f"{record['id']}.json", record)
    original_loaded = session_queue_projection._loaded
    original_records = dict(session_queue_projection._records)
    with session_queue_projection._lock:
        session_queue_projection._loaded = False
        session_queue_projection._records.clear()
        session_queue_projection._journal.clear()
        session_queue_projection._mutation_log.clear()

    try:
        rebuilt = session_queue_projection.rebuild_from_disk()
        assert session_queue_projection.flush_pending_writes(timeout=5)
        records = session_queue_projection.get_many(["unchanged", "changed", "new", "stale"])
    finally:
        with session_queue_projection._lock:
            session_queue_projection._records.clear()
            session_queue_projection._records.update(original_records)
            session_queue_projection._loaded = original_loaded

    rebuild_skip_ok = (
        rebuilt >= 3
        and set(records) == {"unchanged", "changed", "new"}
        and records["changed"]["cwd"] == "/tmp/new"
        and session_queue_projection.projection_is_current()
    )
    results.append((
        "queue projection rebuild skips unchanged writes",
        rebuild_skip_ok,
        f"rebuilt={rebuilt} records={sorted(records)}",
    ))

    with session_queue_projection._lock:
        session_queue_projection._loaded = False
        session_queue_projection._records.clear()
    original_rebuild = session_queue_projection.rebuild_from_disk
    try:
        def fail_rebuild() -> int:
            raise AssertionError("current queue projection should not rebuild")

        session_queue_projection.rebuild_from_disk = fail_rebuild
        fast_rebuilt = session_queue_projection.ensure_current_or_rebuild()
        fast_records = session_queue_projection.list_queued_records()
    finally:
        session_queue_projection.rebuild_from_disk = original_rebuild
    results.append((
        "queue projection current manifest skips full session scan",
        rebuild_skip_ok and fast_rebuilt is False and isinstance(fast_records, list),
        f"fast_rebuilt={fast_rebuilt} fast_records={fast_records}",
    ))

    _write_json(sessions_dir / "changed.seen.json", {"seen": {"changed": "uid-1"}})
    original_rebuild = session_queue_projection.rebuild_from_disk
    try:
        def fail_sidecar_rebuild() -> int:
            raise AssertionError("session sidecars must not invalidate queue projection")

        session_queue_projection.rebuild_from_disk = fail_sidecar_rebuild
        sidecar_rebuilt = session_queue_projection.ensure_current_or_rebuild()
    finally:
        session_queue_projection.rebuild_from_disk = original_rebuild
    results.append((
        "queue projection manifest ignores session sidecar changes",
        rebuild_skip_ok and sidecar_rebuilt is False,
        f"sidecar_rebuilt={sidecar_rebuilt}",
    ))

    _write_json(sessions_dir / "changed.json", _session_record("changed", cwd="/tmp/stale"))
    stale_calls = 0
    original_rebuild = session_queue_projection.rebuild_from_disk

    def tracking_rebuild() -> int:
        nonlocal stale_calls
        stale_calls += 1
        return original_rebuild()

    session_queue_projection.rebuild_from_disk = tracking_rebuild
    try:
        stale_rebuilt = session_queue_projection.ensure_current_or_rebuild()
    finally:
        session_queue_projection.rebuild_from_disk = original_rebuild
    stale_record = session_queue_projection.get("changed") or {}
    results.append((
        "queue projection stale manifest falls back to full rebuild",
        stale_rebuilt is True and stale_calls == 1 and stale_record.get("cwd") == "/tmp/stale",
        f"stale_rebuilt={stale_rebuilt} calls={stale_calls} record={stale_record}",
    ))

    raced_record = dict(session_queue_projection.get("changed") or {})
    raced_record["cwd"] = "/tmp/post-cas-race"
    thread = threading.Thread(target=session_queue_projection.upsert_record, args=(raced_record,))
    thread.start()
    thread.join()
    session_queue_projection.flush_pending_writes(timeout=5)
    raced_current = session_queue_projection.get("changed") or {}
    results.append((
        "post-CAS concurrent upsert survives memory swap",
        raced_current.get("cwd") == "/tmp/post-cas-race",
        f"record={raced_current}",
    ))

    changed_path = sessions_dir / "changed.json"
    before_fingerprint = session_queue_projection._session_files_fingerprint()
    prior_stat = changed_path.stat()
    replacement = changed_path.with_suffix(".replacement")
    replacement.write_bytes(changed_path.read_bytes())
    os.replace(replacement, changed_path)
    os.utime(changed_path, ns=(prior_stat.st_atime_ns, prior_stat.st_mtime_ns))
    after_fingerprint = session_queue_projection._session_files_fingerprint()
    results.append((
        "same-size unsampled replacement invalidates corpus identity",
        before_fingerprint != after_fingerprint,
        "fingerprint unexpectedly unchanged",
    ))
    session_queue_projection.rebuild_from_disk()

    routine_dir = Path(_TMP_HOME) / "routine-sessions" / "routine-a"
    routine_dir.mkdir(parents=True, exist_ok=True)
    moved_path = routine_dir / changed_path.name
    os.replace(changed_path, moved_path)
    moved_rebuilt = session_queue_projection.ensure_current_or_rebuild()
    moved_record = session_queue_projection.get("changed") or {}
    results.append((
        "session move into routine corpus invalidates and reprojects",
        moved_rebuilt is True and moved_record.get("cwd") == "/tmp/stale",
        f"rebuilt={moved_rebuilt} record={moved_record}",
    ))

    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, msg in results:
        tag = PASS if ok else FAIL
        print(f"  {tag} {name}{'' if ok else ' - ' + msg}")
    print(f"\n{passed}/{len(results)} checks passed")
    return passed == len(results)


def main() -> int:
    try:
        return 0 if _run() else 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
