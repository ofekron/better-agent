import os
import shutil
import sys
import tempfile
from pathlib import Path

import _test_home
_test_home.isolate("bc_test_root_events_once_")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import event_journal  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402


def main() -> int:
    original_read_events = event_journal.event_journal_reader.read_events
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

        calls = 0

        def counted_read_events(*args, **kwargs):
            nonlocal calls
            calls += 1
            return [], 0, False

        event_journal.event_journal_reader.read_events = counted_read_events
        tree = session_manager.get_root_tree_stubbed(
            sid,
            msg_limit=10,
            exchange_count=None,
        )
        assert tree is not None
        assert calls == 0, calls
        print("PASS: stubbed tree uses cached root-event projection")
        return 0
    finally:
        event_journal.event_journal_reader.read_events = original_read_events
        shutil.rmtree(os.environ["BETTER_CLAUDE_HOME"], ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
