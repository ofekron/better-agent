import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

import _test_home
_test_home.isolate("bc_test_hydrate_bulk_")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from event_ingester import event_ingester  # noqa: E402
from orchs import get_strategy  # noqa: E402
from render_tree_hydrate import hydrate_msg_events_from_jsonl  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402


def main() -> int:
    try:
        session = session_manager.create(
            name="bulk", cwd="/tmp", orchestration_mode="native",
        )
        sid = session["id"]
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
        print("PASS: live hydrate bulk-loads ordinary render events")
        return 0
    finally:
        shutil.rmtree(os.environ["BETTER_CLAUDE_HOME"], ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
