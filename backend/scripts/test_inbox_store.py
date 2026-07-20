from __future__ import annotations

import sys
from pathlib import Path

import _test_home

_test_home.isolate("ba-test-inbox-store-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import inbox_store
import session_store


def _create(session_id: str) -> None:
    session_store.create_session(
        id=session_id,
        name=session_id,
        model="test-model",
        cwd="/tmp",
        orchestration_mode="native",
    )


def test_private_unread_delivery() -> None:
    for session_id in ("sender-a", "sender-c", "recipient-b"):
        _create(session_id)

    receipt = inbox_store.post_or_read(
        caller_session_id="sender-a",
        recipient_session_id="recipient-b",
        message="private message",
    )
    assert receipt == {
        "recipient_session_id": "recipient-b",
        "sent": True,
        "seq": 1,
    }
    assert inbox_store.post_or_read(caller_session_id="sender-a")["count"] == 0
    assert inbox_store.post_or_read(caller_session_id="sender-c")["count"] == 0

    received = inbox_store.post_or_read(caller_session_id="recipient-b")
    assert received["count"] == 1
    assert received["new_messages"][0]["sender_session_id"] == "sender-a"
    assert received["new_messages"][0]["text"] == "private message"
    assert inbox_store.post_or_read(caller_session_id="recipient-b")["count"] == 0


def test_history_does_not_advance_unread_cursor() -> None:
    inbox_store.send(
        sender_session_id="sender-c",
        recipient_session_id="recipient-b",
        message="second message",
    )
    history = inbox_store.read_history(recipient_session_id="recipient-b")
    assert [item["text"] for item in history["messages"]] == [
        "private message",
        "second message",
    ]
    unread = inbox_store.read_new(recipient_session_id="recipient-b")
    assert [item["text"] for item in unread["new_messages"]] == ["second message"]


def test_fail_closed_inputs() -> None:
    invalid_calls = (
        lambda: inbox_store.post_or_read(
            caller_session_id="sender-a",
            recipient_session_id="recipient-b",
        ),
        lambda: inbox_store.post_or_read(
            caller_session_id="sender-a",
            message="missing recipient",
        ),
        lambda: inbox_store.send(
            sender_session_id="sender-a",
            recipient_session_id="missing-session",
            message="orphan",
        ),
        lambda: inbox_store.send(
            sender_session_id="sender-a",
            recipient_session_id="../escape",
            message="escape",
        ),
        lambda: inbox_store.send(
            sender_session_id="sender-a",
            recipient_session_id="recipient-b",
            message="x" * (inbox_store.MAX_MESSAGE_CHARS + 1),
        ),
        lambda: inbox_store.send(
            sender_session_id="missing-sender",
            recipient_session_id="recipient-b",
            message="forged sender",
        ),
    )
    for call in invalid_calls:
        try:
            call()
            raise AssertionError("invalid inbox operation should fail")
        except inbox_store.InboxStoreError:
            pass


def test_symlink_inbox_is_rejected() -> None:
    path = inbox_store._path("recipient-b")
    path.unlink()
    target = path.parent / "outside.json"
    target.write_text("{}", encoding="utf-8")
    path.symlink_to(target)
    try:
        inbox_store.read_new(recipient_session_id="recipient-b")
        raise AssertionError("symlink inbox should fail")
    except inbox_store.InboxStoreError:
        pass


def test_symlink_root_is_rejected() -> None:
    root = inbox_store._root()
    target = root.with_name("real-inboxes")
    root.rename(target)
    root.symlink_to(target, target_is_directory=True)
    try:
        inbox_store.read_new(recipient_session_id="recipient-b")
        raise AssertionError("symlink inbox root should fail")
    except inbox_store.InboxStoreError:
        pass


if __name__ == "__main__":
    test_private_unread_delivery()
    test_history_does_not_advance_unread_cursor()
    test_fail_closed_inputs()
    test_symlink_inbox_is_rejected()
    test_symlink_root_is_rejected()
    print("inbox store tests: OK")
