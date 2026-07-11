from __future__ import annotations

import os
import sys
import uuid


_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home


_TMP_HOME = _test_home.isolate("ba-test-authority-fence-")

import session_store  # noqa: E402
from session_manager import MigratedRootWriteError, manager as session_manager  # noqa: E402
from stores.session_turn_import import cutover_root, revert_cutover  # noqa: E402
from stores.session_turn_store import SessionTurnStore  # noqa: E402


def _user_msg(content: str, *, client_id: str | None = None) -> dict:
    msg = {
        "id": str(uuid.uuid4()),
        "role": "user",
        "content": content,
        "timestamp": "2026-01-01T00:00:00",
        "events": [],
    }
    if client_id is not None:
        msg["client_id"] = client_id
    return msg


def _seed_session() -> str:
    sess = session_manager.create(
        name="fence", model="glm-5.1", cwd="/tmp", orchestration_mode="native",
    )
    sid = sess["id"]
    session_manager.append_user_msg(sid, _user_msg("first prompt"))
    session_manager.append_assistant_msg(
        sid,
        {
            "id": str(uuid.uuid4()),
            "role": "assistant",
            "content": "reply",
            "timestamp": "2026-01-01T00:00:01",
            "events": [],
            "isStreaming": False,
        },
    )
    session_manager.flush_pending_persists()
    return sid


def _expect_fenced(label: str, call) -> None:
    try:
        call()
    except MigratedRootWriteError:
        return
    raise AssertionError(f"{label} was not fenced on a sqlite-authority root")


def main() -> None:
    store = SessionTurnStore()
    fenced_sid = _seed_session()
    free_sid = _seed_session()
    fenced_msg_id = session_store.get_root_tree(fenced_sid)["messages"][0]["id"]

    # Mutations before cutover populate the authority cache with 'legacy';
    # the in-process listener must invalidate it on the flip.
    cutover_root(store, fenced_sid)

    _expect_fenced(
        "append_user_msg fast path",
        lambda: session_manager.append_user_msg(
            fenced_sid, _user_msg("nope", client_id="c-1"),
        ),
    )
    _expect_fenced(
        "append_user_msg via _run",
        lambda: session_manager.append_user_msg(fenced_sid, _user_msg("nope")),
    )
    _expect_fenced(
        "set_completed_at via _run",
        lambda: session_manager.set_completed_at(fenced_sid, fenced_msg_id, 123.0),
    )

    def _enter_message_batch() -> None:
        with session_manager.message_batch(fenced_sid, fenced_msg_id):
            raise AssertionError("message_batch body ran on a fenced root")

    _expect_fenced("message_batch", _enter_message_batch)
    _expect_fenced(
        "set_msg_recovering",
        lambda: session_manager.set_msg_recovering(fenced_sid, fenced_msg_id, True),
    )
    _expect_fenced("delete", lambda: session_manager.delete(fenced_sid))

    # Bulk-walk writer guard skips (never writes) instead of raising.
    guarded: list[str] = []
    session_manager.write_root_locked(fenced_sid, lambda: guarded.append("wrote"))
    assert guarded == [], "write_root_locked wrote to a fenced root"
    session_manager.reload_root_from_disk(free_sid)  # evict: resident roots always skip
    session_manager.write_root_locked(free_sid, lambda: guarded.append("wrote"))
    assert guarded == ["wrote"], "write_root_locked skipped a legacy root"

    # The async journal-projection pump (the primary streaming writer)
    # must SKIP migrated roots — no mutation, no persist, no raise.
    assert (
        session_manager.apply_written_journal_event(
            fenced_sid, fenced_sid, fenced_msg_id, "text",
            {"uuid": str(uuid.uuid4()), "text": "leak"}, 99,
        )
        is False
    ), "apply_written_journal_event projected into a migrated root"
    assert (
        session_manager.apply_journal_ownership_resolution(
            fenced_sid, fenced_sid, fenced_msg_id, 99,
        )
        is False
    ), "apply_journal_ownership_resolution mutated a migrated root"

    # Fork family mutates the live root in memory before the single
    # persist — entry-fenced so a migrated root never grows a phantom node.
    _expect_fenced("fork", lambda: session_manager.fork(fenced_sid, name="nope"))
    _expect_fenced(
        "create_sub_session",
        lambda: session_manager.create_sub_session(
            parent_session_id=fenced_sid, name="nope", model="glm-5.1",
        ),
    )

    # Authoritative structural fence: session_store.write_session_full is the
    # sole legacy→disk writer. A direct call on a migrated root is a no-op —
    # the on-disk snapshot must stay byte-identical.
    fenced_rid = session_manager._root_id_for(fenced_sid)
    root_file = session_store._root_file_path(fenced_rid)
    before_bytes = root_file.read_bytes()
    fenced_tree = session_store.get_root_tree(fenced_sid)
    session_store.write_session_full(fenced_tree, bump_updated_at=True)
    assert root_file.read_bytes() == before_bytes, (
        "write_session_full overwrote a migrated root's snapshot"
    )

    # Reads stay open, and untouched legacy roots stay fully mutable.
    assert session_manager.get_ref(fenced_sid) is not None
    assert session_manager.append_user_msg(free_sid, _user_msg("still fine")) is not None

    # Revert restores legacy writability via the same listener.
    revert_cutover(store, fenced_sid)
    assert session_manager.append_user_msg(fenced_sid, _user_msg("back")) is not None
    print("PASS integration_test_session_authority_fence")


if __name__ == "__main__":
    main()
