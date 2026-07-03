"""Regression test for durable queued-prompt state.

Queued prompts must live in the session snapshot as `queued_prompts[]`
instead of `messages[]`, so a frontend reload can render a queue banner
without pretending the prompt has already been sent to the agent.

Run with:
    cd backend && .venv/bin/python scripts/test_queued_prompts_persistence.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from unittest import mock
import tempfile

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-queued-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

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

    session_manager.remove_queued_prompt_by_client_id(sid, "pending-1")
    session_manager.flush_pending_persists()
    raw = _read_raw(sid)
    results.append((
        "queued prompt clears only after client id is persisted",
        raw.get("queued_prompts") == [],
        f"queued={raw.get('queued_prompts')}",
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
    sessions_dir = session_queue_projection._sessions_dir()
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
    _write_json(projection_dir / "unchanged.json", unchanged)
    _write_json(projection_dir / "changed.json", changed_old)
    _write_json(projection_dir / "stale.json", stale)
    _write_json(projection_dir / "mismatch.json", {"id": "other"})
    (projection_dir / "malformed.json").write_text("{", encoding="utf-8")

    writes: list[str] = []
    original_loaded = session_queue_projection._loaded
    original_records = dict(session_queue_projection._records)
    with session_queue_projection._lock:
        session_queue_projection._loaded = False
        session_queue_projection._records.clear()

    try:
        with mock.patch.object(
            session_queue_projection,
            "_write_record_locked",
            side_effect=lambda record: writes.append(record["id"]),
        ):
            rebuilt = session_queue_projection.rebuild_from_disk()
        records = session_queue_projection.get_many(["unchanged", "changed", "new", "stale"])
    finally:
        with session_queue_projection._lock:
            session_queue_projection._records.clear()
            session_queue_projection._records.update(original_records)
            session_queue_projection._loaded = original_loaded

    results.append((
        "queue projection rebuild skips unchanged writes",
        rebuilt == 3
        and writes == ["changed", "new"]
        and set(records) == {"unchanged", "changed", "new"}
        and records["changed"]["cwd"] == "/tmp/new"
        and not (projection_dir / "stale.json").exists()
        and not (projection_dir / "mismatch.json").exists()
        and not (projection_dir / "malformed.json").exists(),
        f"rebuilt={rebuilt} writes={writes} records={sorted(records)}",
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
