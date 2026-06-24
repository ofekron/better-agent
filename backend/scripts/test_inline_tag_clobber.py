"""Backend regression test for the inline_tags / draft_input clobber race.

Pins the contract that mutations on different session fields, issued
concurrently from different writers, do NOT clobber each other.

The original bug: orchestrator did `session = get_session(sid); ...;
update_session(sid, session)` — a read-modify-write that overlay the
whole in-memory snapshot, so a frontend DELETE /tags landing between
the read and write was overwritten. The fix is the SessionManager
single-owner architecture in `session_manager.py`: every mutation is a
typed method that only touches its own field(s), serialized on a
per-sid lock with an in-memory cache. The pre-fix workaround
(orchestrator popping ephemeral fields before write-back) is no longer
needed and has been removed.

Run with:
    cd backend && .venv/bin/python scripts/test_inline_tag_clobber.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-clobber-")
os.environ["BETTER_CLAUDE_API_ONLY"] = "1"

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from fastapi.testclient import TestClient  # noqa: E402

from auth_test_helpers import authenticate_client  # noqa: E402
import main  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402


PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _new_session() -> str:
    return session_manager.create(name="t", model="m", cwd="/tmp")["id"]


def _add_tag(client: TestClient, sid: str, tag_id: str, text: str, comment: str) -> None:
    r = client.post(
        f"/api/sessions/{sid}/tags",
        json={
            "id": tag_id,
            "messageId": "m1",
            "selectedText": text,
            "comment": comment,
            "timestamp": "2026-04-30T15:01:27.976Z",
        },
    )
    assert r.status_code == 200, f"add_tag failed: {r.status_code} {r.text}"


def test_typed_writes_on_different_fields_do_not_clobber(client: TestClient) -> bool:
    """Concurrent: REST DELETE /tags vs orchestrator-style writes
    (typed method on a different field). The DELETE must survive."""
    sid = _new_session()
    _add_tag(client, sid, "tag-A", "ephemeral", "yes")
    _add_tag(client, sid, "tag-B", "Replace the chat input area?", "yes")

    # Append a streaming assistant_msg the orchestrator hot-path would
    # write into. This is what the orchestrator does via lazy creation
    # in save_ws_callback.
    asst = session_manager.append_assistant_msg(sid, {
        "id": "asst-1",
        "role": "assistant",
        "content": "",
        "events": [],
        "isStreaming": True,
        "agent_session_id": None,
        "workers": [],
    })

    # Fire an orchestrator-style write (touches the assistant message's
    # flat events — a different field than inline_tags) at the same time
    # the frontend deletes the tag.
    def write_a_lot():
        for i in range(50):
            session_manager.append_native_event(
                sid, asst["id"], {"type": "output", "data": {"output": f"x{i}"}},
            )

    def delete_tags():
        r = client.delete(f"/api/sessions/{sid}/tags")
        assert r.status_code == 200, f"delete failed: {r.status_code}"

    t1 = threading.Thread(target=write_a_lot)
    t2 = threading.Thread(target=delete_tags)
    t1.start(); t2.start()
    t1.join(); t2.join()

    final = session_manager.get(sid)
    if final is None:
        print("  session vanished")
        return False
    if final.get("inline_tags") != []:
        print(f"  expected inline_tags=[], got {final.get('inline_tags')!r}")
        return False
    # And every event the orchestrator wrote should be present too.
    asst_after = next(
        (m for m in final["messages"] if m["id"] == asst["id"]), None,
    )
    events = (asst_after or {}).get("events") or []
    if len(events) != 50:
        print(f"  expected 50 events preserved, got {len(events)}")
        return False
    return True


def test_concurrent_draft_patches_and_typed_writes(client: TestClient) -> bool:
    """draft_input is its own field; concurrent typed writes on other
    fields must not overwrite the latest draft."""
    sid = _new_session()
    asst = session_manager.append_assistant_msg(sid, {
        "id": "asst-2",
        "role": "assistant",
        "content": "",
        "events": [],
        "isStreaming": True,
        "agent_session_id": None,
        "workers": [],
    })

    def patch_draft_repeatedly():
        for seq in range(1, 51):
            r = client.patch(
                f"/api/sessions/{sid}/draft",
                json={
                    "draft_input": f"text-{seq}",
                    "client_seq": seq,
                    "client_id": "tab-a",
                },
            )
            assert r.status_code == 200, r.text

    def write_manager_events():
        for i in range(50):
            session_manager.append_native_event(
                sid, asst["id"],
                {"type": "thinking", "data": {"thought": f"t{i}"}},
            )

    t1 = threading.Thread(target=patch_draft_repeatedly)
    t2 = threading.Thread(target=write_manager_events)
    t1.start(); t2.start()
    t1.join(); t2.join()

    final = session_manager.get(sid)
    if final.get("draft_input") != "text-50":
        print(
            "  expected draft_input='text-50' (last accepted seq), "
            f"got {final.get('draft_input')!r}"
        )
        return False
    if final.get("draft_input_seq") != 50:
        print(
            f"  expected draft_input_seq=50, got {final.get('draft_input_seq')!r}"
        )
        return False
    asst_after = next(
        (m for m in final["messages"] if m["id"] == asst["id"]), None,
    )
    if len((asst_after or {}).get("manager", {}).get("events") or []) != 50:
        print(f"  manager events lost — got {len((asst_after or {}).get('manager', {}).get('events') or [])}")
        return False
    return True


TESTS = [
    (
        "typed writes on different fields do not clobber (tags vs orch events)",
        test_typed_writes_on_different_fields_do_not_clobber,
    ),
    (
        "concurrent draft PATCHes survive concurrent orchestrator writes",
        test_concurrent_draft_patches_and_typed_writes,
    ),
]


def main_run() -> int:
    client = TestClient(main.app)
    authenticate_client(client)
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
