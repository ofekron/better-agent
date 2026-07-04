from __future__ import annotations

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-communication-log-")

import chat_store
import communication_log
from session_manager import manager as session_manager
import team_messaging


def teardown_module():
    shutil.rmtree(_TMP_HOME, ignore_errors=True)


def _create_pair():
    sender = session_manager.create(
        name="Sender",
        cwd="/repo",
        orchestration_mode="native",
    )
    target = session_manager.create(
        name="Target",
        cwd="/repo",
        orchestration_mode="native",
    )
    return sender, target


def test_communication_log_projects_team_messages_and_chats():
    sender, target = _create_pair()
    metadata = team_messaging.build_message_metadata(
        sender_session_id=sender["id"],
        target_session_id=target["id"],
    )
    user_msg = {
        "id": "msg-1",
        "role": "user",
        "source": team_messaging.SOURCE,
        "content": "hello target",
        "timestamp": "2026-01-01T00:00:00+00:00",
        "isStreaming": False,
        "events": [],
        "team_message": {"message": "hello target", "metadata": metadata},
    }
    session_manager.append_user_msg(target["id"], user_msg)

    chat_store.create_chat(chat_id="room", created_by=sender["id"], name="Room")
    chat_store.post_and_read(chat_id="room", reader_id=sender["id"], message="hello room")

    data = communication_log.list_communications(limit=20)
    kinds = {item["kind"] for item in data["items"]}

    assert team_messaging.SOURCE in kinds
    assert "chat" in kinds
    team_item = next(item for item in data["items"] if item["kind"] == team_messaging.SOURCE)
    assert team_item["from_session_id"] == sender["id"]
    assert team_item["from_name"] == "Sender"
    assert team_item["to_session_id"] == target["id"]
    assert team_item["to_name"] == "Target"
    assert team_item["body"] == "hello target"


def test_session_filter_includes_chat_room_participant_messages():
    sender, target = _create_pair()
    reader = session_manager.create(
        name="Reader",
        cwd="/repo",
        orchestration_mode="native",
    )
    observer = session_manager.create(
        name="Observer",
        cwd="/repo",
        orchestration_mode="native",
    )
    chat_store.create_chat(chat_id="team-room", created_by=sender["id"], name="Team Room")
    chat_store.post_and_read(chat_id="team-room", reader_id=sender["id"], message="from sender")
    chat_store.post_and_read(chat_id="team-room", reader_id=reader["id"], message="")
    chat_store.post_and_read(chat_id="team-room", reader_id=target["id"], message="from target")

    sender_data = communication_log.list_communications(session_id=sender["id"], limit=20)
    reader_data = communication_log.list_communications(session_id=reader["id"], limit=20)
    observer_data = communication_log.list_communications(session_id=observer["id"], limit=20)
    texts = {item["body"] for item in sender_data["items"] if item["kind"] == "chat"}
    reader_texts = {item["body"] for item in reader_data["items"] if item["kind"] == "chat"}
    observer_texts = {item["body"] for item in observer_data["items"] if item["kind"] == "chat"}
    item = next(item for item in reader_data["items"] if item["kind"] == "chat")
    participant_ids = {participant["session_id"] for participant in item["participants"]}

    assert texts == {"from sender", "from target"}
    assert reader_texts == {"from sender", "from target"}
    assert observer_texts == set()
    assert participant_ids == {sender["id"], target["id"], reader["id"]}


def test_empty_chat_room_is_projected_for_creator():
    sender, _target = _create_pair()
    chat_store.create_chat(chat_id="empty-room", created_by=sender["id"], name="Empty Room")

    data = communication_log.list_communications(session_id=sender["id"], limit=20)
    item = next(item for item in data["items"] if item["id"] == "chat:empty-room:empty")

    assert item["status"] == "open"
    assert item["chat_name"] == "Empty Room"
    assert item["body"] == ""
    assert item["participants"] == [{"session_id": sender["id"], "name": "Sender"}]


def test_queued_delegate_task_is_projected():
    sender, target = _create_pair()
    metadata = team_messaging.build_message_metadata(
        sender_session_id=sender["id"],
        target_session_id=target["id"],
    )
    metadata["target_selector"] = {
        "kind": "pool",
        "value": "review",
        "pool_affinity_key": "thread-1",
    }
    queued = team_messaging.queue_payload(
        queue_item_id="queued-1",
        sender_session_id=sender["id"],
        message="do work",
        metadata=metadata,
        lifecycle_msg_id="life-1",
        target_session_id=target["id"],
        source=team_messaging.DELEGATE_TASK_SOURCE,
    )
    session_manager.add_queued_prompt(target["id"], queued)

    data = communication_log.list_communications(session_id=target["id"], limit=20)
    item = next(item for item in data["items"] if item["kind"] == team_messaging.DELEGATE_TASK_SOURCE)

    assert item["status"] == "queued"
    assert item["from_session_id"] == sender["id"]
    assert item["to_session_id"] == target["id"]
    assert item["body"] == "do work"
    assert item["addressed_target"] == {
        "kind": "pool",
        "value": "review",
        "pool_affinity_key": "thread-1",
    }


def test_cached_empty_session_invalidates_after_queued_prompt():
    sender, target = _create_pair()
    empty = communication_log.list_communications(session_id=target["id"], limit=20)
    metadata = team_messaging.build_message_metadata(
        sender_session_id=sender["id"],
        target_session_id=target["id"],
    )
    queued = team_messaging.queue_payload(
        queue_item_id="queued-after-empty",
        sender_session_id=sender["id"],
        message="queued after empty",
        metadata=metadata,
        lifecycle_msg_id="life-after-empty",
        target_session_id=target["id"],
        source=team_messaging.DELEGATE_TASK_SOURCE,
    )
    session_manager.add_queued_prompt(target["id"], queued)

    data = communication_log.list_communications(session_id=target["id"], limit=20)

    assert empty["total"] == 0
    assert any(item["id"] == f"queued:{target['id']}:queued-after-empty" for item in data["items"])


def test_session_filter_does_not_hydrate_each_session_again():
    sender, target = _create_pair()
    unrelated = session_manager.create(
        name="Unrelated",
        cwd="/repo",
        orchestration_mode="native",
    )
    metadata = team_messaging.build_message_metadata(
        sender_session_id=sender["id"],
        target_session_id=target["id"],
    )
    session_manager.append_user_msg(target["id"], {
        "id": "receiver-stored-message",
        "role": "user",
        "source": team_messaging.SOURCE,
        "content": "from sender to target",
        "timestamp": "2026-01-02T00:00:00+00:00",
        "team_message": {
            "message": "from sender to target",
            "metadata": metadata,
        },
    })

    original_get = session_manager.get
    try:
        def fail_get(_sid):
            raise AssertionError("list_communications must not hydrate each session with get()")

        session_manager.get = fail_get
        sender_data = communication_log.list_communications(session_id=sender["id"], limit=20)
        unrelated_data = communication_log.list_communications(session_id=unrelated["id"], limit=20)
    finally:
        session_manager.get = original_get

    assert any(item["id"] == f"delivered:{target['id']}:receiver-stored-message" for item in sender_data["items"])
    assert unrelated_data["total"] == 0


def test_limit_returns_newest_items_and_preserves_filtered_total():
    sender = session_manager.create(
        name="Sender",
        cwd="/repo",
        orchestration_mode="native",
    )
    target_ids = []
    for index in range(6):
        target = session_manager.create(
            name=f"Target {index}",
            cwd="/repo",
            orchestration_mode="native",
        )
        target_ids.append(target["id"])
        metadata = team_messaging.build_message_metadata(
            sender_session_id=sender["id"],
            target_session_id=target["id"],
        )
        session_manager.append_user_msg(target["id"], {
            "id": f"ranked-{index}",
            "role": "user",
            "source": team_messaging.SOURCE,
            "content": f"message {index}",
            "timestamp": f"2026-01-03T00:00:0{index}+00:00",
            "team_message": {
                "message": f"message {index}",
                "metadata": metadata,
            },
        })

    data = communication_log.list_communications(session_id=sender["id"], limit=3)
    ids = [item["id"] for item in data["items"]]

    assert data["count"] == 3
    assert data["total"] == 6
    assert ids == [
        f"delivered:{target_ids[5]}:ranked-5",
        f"delivered:{target_ids[4]}:ranked-4",
        f"delivered:{target_ids[3]}:ranked-3",
    ]


if __name__ == "__main__":
    test_communication_log_projects_team_messages_and_chats()
    test_session_filter_includes_chat_room_participant_messages()
    test_empty_chat_room_is_projected_for_creator()
    test_queued_delegate_task_is_projected()
    test_cached_empty_session_invalidates_after_queued_prompt()
    test_session_filter_does_not_hydrate_each_session_again()
    test_limit_returns_newest_items_and_preserves_filtered_total()
    print("ALL PASS")
