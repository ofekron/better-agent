import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _test_home
_test_home.isolate("bc_test_hydrate_bulk_")

from event_ingester import event_ingester  # noqa: E402
from event_journal import event_journal_reader  # noqa: E402
from orchs import get_strategy  # noqa: E402
import render_tree_hydrate  # noqa: E402
from render_tree_hydrate import hydrate_msg_events_from_jsonl  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402


def main() -> int:
    try:
        session = session_manager.create(
            name="bulk", cwd="/tmp", orchestration_mode="native",
        )
        sid = session["id"]
        session_manager.get_ref(sid)["agent_session_id"] = "agent-root"
        msg_id = "msg-bulk"
        session_manager.append_assistant_msg(
            sid,
            {
                "id": msg_id,
                "role": "assistant",
                "content": "",
                "events": [],
                "timestamp": "2026-06-19T00:00:00",
                "isStreaming": False,
                "workers": [],
            },
        )
        for i in range(1000):
            event_ingester.ingest(
                sid,
                sid=sid,
                event_type="agent_message",
                data={
                    "uuid": str(uuid.uuid4()),
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": f"chunk {i}"}],
                    },
                },
                source="bulk-test",
                msg_id=msg_id,
            )
        event_ingester.close_all()

        root = session_manager.get_ref(sid)
        msg = next(m for m in root["messages"] if m["id"] == msg_id)
        msg["events"] = []
        msg.pop("_uid_idx", None)

        strategy = get_strategy("native")
        original_apply = strategy.apply_event
        calls = 0

        def counted_apply(*args, **kwargs):
            nonlocal calls
            calls += 1
            return original_apply(*args, **kwargs)

        strategy.apply_event = counted_apply
        try:
            hydrate_msg_events_from_jsonl(root)
        finally:
            strategy.apply_event = original_apply

        assert len(msg["events"]) == 1000, len(msg["events"])
        assert calls == 0, calls

        fork = session_manager.fork(sid, name="fork")
        fork_sid = fork["id"]
        fork_msg_id = "msg-fork"
        session_manager.append_assistant_msg(
            fork_sid,
            {
                "id": fork_msg_id,
                "role": "assistant",
                "content": "",
                "events": [],
                "timestamp": "2026-06-19T00:00:01",
                "isStreaming": False,
                "workers": [],
            },
        )
        event_ingester.ingest(
            sid,
            sid=fork_sid,
            event_type="agent_message",
            data={
                "uuid": str(uuid.uuid4()),
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "fork chunk"}],
                },
            },
            source="bulk-test",
            msg_id=fork_msg_id,
        )
        event_ingester.close_all()
        root = session_manager.get_ref(sid)
        fork_node = root["forks"][0]
        msg["events"] = []
        msg.pop("_uid_idx", None)
        fork_msg = next(m for m in fork_node["messages"] if m["id"] == fork_msg_id)
        fork_msg["events"] = []
        fork_msg.pop("_uid_idx", None)
        original_read_events = event_journal_reader.read_events
        read_calls = 0
        read_after_seqs = []

        def counted_read_events(*args, **kwargs):
            nonlocal read_calls
            read_calls += 1
            read_after_seqs.append(kwargs.get("after_seq", 0))
            return original_read_events(*args, **kwargs)

        event_journal_reader.read_events = counted_read_events
        try:
            hydrate_msg_events_from_jsonl(root)
        finally:
            event_journal_reader.read_events = original_read_events

        assert read_calls == 1, read_calls
        assert read_after_seqs == [0], read_after_seqs
        assert fork_msg["events"], fork_msg

        cursor = event_ingester.current_seq(sid) or 0
        tail_msg_id = "msg-tail"
        session_manager.append_assistant_msg(
            sid,
            {
                "id": tail_msg_id,
                "role": "assistant",
                "content": "",
                "events": [],
                "timestamp": "2026-06-19T00:00:02",
                "isStreaming": False,
                "workers": [],
            },
        )
        event_ingester.ingest(
            sid,
            sid=sid,
            event_type="agent_message",
            data={
                "uuid": str(uuid.uuid4()),
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "tail chunk"}],
                },
            },
            source="bulk-test",
            msg_id=tail_msg_id,
        )
        event_ingester.close_all()

        root = session_manager.get_ref(sid)
        tail_msg = next(m for m in root["messages"] if m["id"] == tail_msg_id)
        for node in (root, *root.get("forks", [])):
            for existing_msg in node.get("messages", []):
                if existing_msg.get("id") != tail_msg_id:
                    existing_msg["content"] = "already projected"
        read_calls = 0
        read_after_seqs = []
        project_calls = 0
        original_project_content_snapshot = render_tree_hydrate.project_content_snapshot

        def counted_project_content_snapshot(*args, **kwargs):
            nonlocal project_calls
            project_calls += 1
            return original_project_content_snapshot(*args, **kwargs)

        event_journal_reader.read_events = counted_read_events
        render_tree_hydrate.project_content_snapshot = counted_project_content_snapshot
        try:
            hydrate_msg_events_from_jsonl(root, after_seq=cursor)
        finally:
            event_journal_reader.read_events = original_read_events
            render_tree_hydrate.project_content_snapshot = original_project_content_snapshot

        assert read_calls == 1, read_calls
        assert read_after_seqs == [cursor], read_after_seqs
        assert project_calls == 1, project_calls
        assert len(tail_msg["events"]) == 1, tail_msg
        print("PASS: live hydrate bulk-loads ordinary render events")
        return 0
    finally:
        shutil.rmtree(os.environ["BETTER_CLAUDE_HOME"], ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
