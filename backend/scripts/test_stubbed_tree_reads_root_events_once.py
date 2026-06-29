import os
import shutil
import sys
import tempfile
from pathlib import Path

import _test_home
_test_home.isolate("bc_test_root_events_once_")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import event_journal  # noqa: E402
from event_ingester import event_ingester  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402


def main() -> int:
    original_read_events = event_journal.event_journal_reader.read_events
    original_summaries = event_journal.event_journal_reader.message_event_summaries
    original_read_all = event_ingester._read_all_events_locked
    try:
        root = session_manager.create(
            name="root-events-once",
            cwd="/tmp",
            orchestration_mode="native",
        )
        sid = root["id"]
        session_manager.set_agent_sid(sid, "native", "agent-root")
        session_manager.append_user_msg(sid, {"content": "root"})
        child = session_manager.fork(sid, name="child")
        session_manager.append_user_msg(child["id"], {"content": "child"})
        event_ingester.ingest(
            sid,
            sid=sid,
            event_type="agent_message",
            data={
                "uuid": "owned-root-event",
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "owned"}]},
            },
            source="test",
            msg_id="owned-message",
        )

        read_calls = 0
        summary_calls = 0
        root_event_full_reads = 0

        def counted_read_events(*args, **kwargs):
            nonlocal read_calls
            read_calls += 1
            return [], 0, False

        def counted_summaries(*args, **kwargs):
            nonlocal summary_calls
            summary_calls += 1
            return original_summaries(*args, **kwargs)

        def counted_read_all(*args, **kwargs):
            nonlocal root_event_full_reads
            root_event_full_reads += 1
            return original_read_all(*args, **kwargs)

        event_journal.event_journal_reader.read_events = counted_read_events
        event_journal.event_journal_reader.message_event_summaries = counted_summaries
        event_ingester._read_all_events_locked = counted_read_all
        tree = session_manager.get_root_tree_stubbed(
            sid,
            msg_limit=10,
            exchange_count=None,
        )
        assert tree is not None
        assert read_calls == 0, read_calls
        assert summary_calls <= 2, summary_calls
        assert root_event_full_reads == 0, root_event_full_reads
        print("PASS: stubbed tree uses cached root-event projection")
        return 0
    finally:
        event_journal.event_journal_reader.read_events = original_read_events
        event_journal.event_journal_reader.message_event_summaries = original_summaries
        event_ingester._read_all_events_locked = original_read_all
        shutil.rmtree(os.environ["BETTER_CLAUDE_HOME"], ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
