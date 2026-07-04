from __future__ import annotations

import shutil
import sys
from pathlib import Path

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-communication-log-")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

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


if __name__ == "__main__":
    test_communication_log_projects_team_messages_and_chats()
    test_session_filter_includes_chat_room_participant_messages()
    test_empty_chat_room_is_projected_for_creator()
    test_queued_delegate_task_is_projected()
    print("ALL PASS")
