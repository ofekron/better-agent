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
