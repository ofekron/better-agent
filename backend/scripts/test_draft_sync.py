"""Backend tests for the draft_input + session_metadata sync feature.

Pins the contract for PATCH /api/sessions/{id}/draft:
  * persists draft_input on the session record
  * stores draft_input_seq from client_seq
  * rejects PATCHes whose client_seq <= stored seq
  * tag mutations and the draft PATCH both broadcast
    `session_metadata_updated` over the WS with `originated_by`
    echoed from the request

Run with:
    cd backend && .venv/bin/python scripts/test_draft_sync.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

# Per CLAUDE.md, isolate ~/.better-claude state to a tempdir BEFORE
# importing any backend module so the dev's real session store is
# never touched.
import _test_home
_TMP_HOME = _test_home.isolate("bc-test-draft-")
os.environ["BETTER_CLAUDE_API_ONLY"] = "1"

# Add backend/ to sys.path so `import main` etc work when run from repo root.
_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from fastapi.testclient import TestClient  # noqa: E402

from auth_test_helpers import authenticate_client  # noqa: E402
import main  # noqa: E402
import session_store  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402


PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _new_session() -> str:
    sess = session_store.create_session(name="t", model="m", cwd="/tmp")
    return sess["id"]


def test_patch_draft_persists_value_and_seq(client: TestClient) -> bool:
    sid = _new_session()
    r = client.patch(
        f"/api/sessions/{sid}/draft",
        json={"draft_input": "hello", "client_seq": 100, "client_id": "tab-a"},
    )
    if r.status_code != 200:
        print(f"  expected 200, got {r.status_code}: {r.text}")
        return False
    body = r.json()
    if body.get("draft_input") != "hello":
        print(f"  draft_input mismatch: {body}")
        return False
    if body.get("draft_input_seq") != 100:
        print(f"  draft_input_seq mismatch: {body}")
        return False
    session_manager.drain_pending_drafts()
    on_disk = session_store.get_session(sid)
    if on_disk["draft_input"] != "hello" or on_disk["draft_input_seq"] != 100:
        print(f"  on-disk mismatch: {on_disk}")
        return False
    return True


def test_stale_client_seq_is_rejected(client: TestClient) -> bool:
    sid = _new_session()
    client.patch(
        f"/api/sessions/{sid}/draft",
        json={"draft_input": "fresh", "client_seq": 200, "client_id": "tab-a"},
    )
    r = client.patch(
        f"/api/sessions/{sid}/draft",
        json={
            "draft_input": "stale-late-arrival",
            "client_seq": 150,
            "client_id": "tab-a",
        },
    )
    if r.status_code != 200:
        print(f"  expected 200 (rejection still 200), got {r.status_code}")
        return False
    body = r.json()
    if not body.get("rejected"):
        print(f"  expected rejected=True, got {body}")
        return False
    if body.get("draft_input_seq") != 200:
        print(f"  rejection should echo stored seq, got {body}")
        return False
    session_manager.drain_pending_drafts()
    on_disk = session_store.get_session(sid)
    if on_disk["draft_input"] != "fresh":
        print(f"  on-disk overwritten: {on_disk}")
        return False
    return True


def test_equal_client_seq_is_rejected(client: TestClient) -> bool:
    """seq must be STRICTLY greater than stored. == is treated as a
    duplicate and rejected so a retried request with the exact same
    seq doesn't double-broadcast."""
    sid = _new_session()
    client.patch(
        f"/api/sessions/{sid}/draft",
        json={"draft_input": "first", "client_seq": 100, "client_id": "tab-a"},
    )
    r = client.patch(
        f"/api/sessions/{sid}/draft",
        json={"draft_input": "second", "client_seq": 100, "client_id": "tab-a"},
    )
    if not r.json().get("rejected"):
        print(f"  expected rejected, got {r.json()}")
        return False
    session_manager.drain_pending_drafts()
    on_disk = session_store.get_session(sid)
    if on_disk["draft_input"] != "first":
        print(f"  equal-seq should not overwrite, got {on_disk['draft_input']}")
        return False
    return True


def test_higher_client_seq_after_rejection_still_works(client: TestClient) -> bool:
    sid = _new_session()
    client.patch(
        f"/api/sessions/{sid}/draft",
        json={"draft_input": "fresh", "client_seq": 200, "client_id": "tab-a"},
    )
    # stale: rejected
    client.patch(
        f"/api/sessions/{sid}/draft",
        json={"draft_input": "stale", "client_seq": 150, "client_id": "tab-a"},
    )
    # newer than stored: accepted
    r = client.patch(
        f"/api/sessions/{sid}/draft",
        json={"draft_input": "newest", "client_seq": 250, "client_id": "tab-a"},
    )
    body = r.json()
    if body.get("rejected"):
        print(f"  unexpected rejection: {body}")
        return False
    session_manager.drain_pending_drafts()
    on_disk = session_store.get_session(sid)
    if on_disk["draft_input"] != "newest" or on_disk["draft_input_seq"] != 250:
        print(f"  on-disk wrong after recovery: {on_disk}")
        return False
    return True


def test_patch_draft_missing_client_seq_is_400(client: TestClient) -> bool:
    sid = _new_session()
    r = client.patch(
        f"/api/sessions/{sid}/draft",
        json={"draft_input": "x"},
    )
    if r.status_code != 400:
        print(f"  expected 400, got {r.status_code}: {r.text}")
        return False
    return True


def test_patch_draft_missing_session_is_404(client: TestClient) -> bool:
    r = client.patch(
        "/api/sessions/does-not-exist/draft",
        json={"draft_input": "x", "client_seq": 1, "client_id": "tab-a"},
    )
    if r.status_code != 404:
        print(f"  expected 404, got {r.status_code}")
        return False
    return True


def test_patch_draft_does_not_bump_updated_at(client: TestClient) -> bool:
    sid = _new_session()
    before = session_store.get_session(sid)["updated_at"]
    # Sleep so any inadvertent bump would produce a different timestamp.
    import time
    time.sleep(0.01)
    client.patch(
        f"/api/sessions/{sid}/draft",
        json={"draft_input": "typing", "client_seq": 1, "client_id": "tab-a"},
    )
    session_manager.drain_pending_drafts()
    after = session_store.get_session(sid)["updated_at"]
    if before != after:
        print(f"  updated_at moved: {before} -> {after}")
        return False
    return True


def _capture_metadata_event(coordinator, session_id: str, action) -> dict | None:
    """Register a fake WS callback against the coordinator, run `action`
    (synchronous — TestClient.* calls drive the FastAPI handler in
    their own per-request event loop, where `await coordinator.broadcast`
    completes before the response returns), then scan for a
    session_metadata_updated event addressed to `session_id`."""
    received: list[dict] = []

    async def cb(ev):
        received.append(ev)

    coordinator.register_ws(session_id, cb)
    try:
        action()
    finally:
        coordinator.unregister_ws(session_id, cb)
    for ev in received:
        if ev.get("type") == "session_metadata_updated":
            return ev
    return None


def test_draft_patch_emits_ws_event(client: TestClient) -> bool:
    sid = _new_session()

    def go():
        return client.patch(
            f"/api/sessions/{sid}/draft",
            json={
                "draft_input": "hi",
                "client_seq": 99,
                "client_id": "tab-a",
            },
        )

    ev = _capture_metadata_event(main.coordinator, sid, go)
    if not ev:
        print("  no session_metadata_updated event broadcast")
        return False
    data = ev.get("data") or {}
    if data.get("session_id") != sid:
        print(f"  session_id mismatch: {data}")
        return False
    if data.get("originated_by") != "tab-a":
        print(f"  originated_by not echoed: {data}")
        return False
    patch = data.get("patch") or {}
    if patch.get("draft_input") != "hi":
        print(f"  patch.draft_input wrong: {patch}")
        return False
    if patch.get("draft_input_seq") != 99:
        print(f"  patch.draft_input_seq missing: {patch}")
        return False
    return True


def test_tag_post_emits_ws_event_with_client_id(client: TestClient) -> bool:
    sid = _new_session()
    tag = {
        "id": "tag-1",
        "messageId": "m-1",
        "selectedText": "selected",
        "comment": "note",
        "timestamp": "2026-04-30T00:00:00Z",
        "client_id": "tab-b",
    }

    def go():
        return client.post(f"/api/sessions/{sid}/tags", json=tag)

    ev = _capture_metadata_event(main.coordinator, sid, go)
    if not ev:
        print("  no broadcast on tag POST")
        return False
    data = ev.get("data") or {}
    if data.get("originated_by") != "tab-b":
        print(f"  originated_by not echoed: {data}")
        return False
    if not isinstance(data.get("patch", {}).get("inline_tags"), list):
        print(f"  patch.inline_tags missing: {data}")
        return False
    return True


def test_tag_delete_emits_ws_event_with_client_id_query(client: TestClient) -> bool:
    sid = _new_session()
    client.post(
        f"/api/sessions/{sid}/tags",
        json={
            "id": "tag-x",
            "messageId": "m",
            "selectedText": "s",
            "comment": "c",
            "timestamp": "t",
        },
    )

    def go():
        return client.delete(f"/api/sessions/{sid}/tags/tag-x?client_id=tab-c")

    ev = _capture_metadata_event(main.coordinator, sid, go)
    if not ev:
        print("  no broadcast on tag DELETE")
        return False
    if (ev.get("data") or {}).get("originated_by") != "tab-c":
        print(f"  originated_by not echoed: {ev}")
        return False
    return True


def test_session_creation_seeds_draft_fields(client: TestClient) -> bool:
    r = client.post("/api/sessions", json={"name": "x", "cwd": "/tmp"})
    body = r.json()
    if body.get("draft_input") != "":
        print(f"  draft_input default not '': {body}")
        return False
    if body.get("draft_input_seq") != 0:
        print(f"  draft_input_seq default not 0: {body}")
        return False
    return True


def test_legacy_session_migrates_with_default_draft_fields(client: TestClient) -> bool:
    """A session JSON written by an older revision lacks draft_input /
    draft_input_seq; _migrate_session must backfill defaults on read."""
    from pathlib import Path

    legacy = {
        "id": "legacy-sid",
        "name": "legacy",
        "model": "claude-sonnet-4-6",
        "cwd": "/tmp",
        "created_at": "2026-01-01",
        "updated_at": "2026-01-01",
        "messages": [],
    }
    path = Path(_TMP_HOME) / "sessions" / "legacy-sid.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(legacy))

    sess = session_store.get_session("legacy-sid")
    if sess.get("draft_input") != "":
        print(f"  legacy draft_input default missing: {sess}")
        return False
    if sess.get("draft_input_seq") != 0:
        print(f"  legacy draft_input_seq default missing: {sess}")
        return False
    return True


TESTS = [
    ("PATCH /draft persists value and seq", test_patch_draft_persists_value_and_seq),
    ("stale client_seq is rejected", test_stale_client_seq_is_rejected),
    ("equal client_seq is rejected", test_equal_client_seq_is_rejected),
    ("higher seq after rejection still works", test_higher_client_seq_after_rejection_still_works),
    ("missing client_seq → 400", test_patch_draft_missing_client_seq_is_400),
    ("missing session → 404", test_patch_draft_missing_session_is_404),
    ("PATCH /draft does NOT bump updated_at", test_patch_draft_does_not_bump_updated_at),
    ("draft PATCH emits session_metadata_updated", test_draft_patch_emits_ws_event),
    ("tag POST emits session_metadata_updated", test_tag_post_emits_ws_event_with_client_id),
    ("tag DELETE emits session_metadata_updated", test_tag_delete_emits_ws_event_with_client_id_query),
    ("new session seeds draft_input + seq", test_session_creation_seeds_draft_fields),
    ("legacy session migrates with default draft fields", test_legacy_session_migrates_with_default_draft_fields),
]


def main_run() -> int:
    # `with` runs the app lifespan so session_manager binds the running
    # event loop — required for the WS-broadcast path (`_fire` schedules
    # the bus publish onto that loop).
    with TestClient(main.app, client=("127.0.0.1", 50000)) as client:
        authenticate_client(client)
        return _run_tests(client)


def _run_tests(client: TestClient) -> int:
    failed = 0
    try:
        for name, fn in TESTS:
            try:
                ok = fn(client)
            except Exception as e:
                ok = False
                import traceback
                traceback.print_exc()
                print(f"  exception: {e}")
            print(f"{PASS if ok else FAIL}  {name}")
            if not ok:
                failed += 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
    print()
    if failed:
        print(f"{failed} of {len(TESTS)} test(s) FAILED")
    else:
        print(f"all {len(TESTS)} tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main_run())
