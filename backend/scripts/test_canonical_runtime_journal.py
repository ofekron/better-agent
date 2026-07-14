import os
import sys
import tempfile
from pathlib import Path

HOME = Path(tempfile.mkdtemp(prefix="ba-runtime-journal-"))
os.environ["BETTER_AGENT_HOME"] = str(HOME)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from canonical_runtime_journal import CanonicalRuntimeJournal


def test_import_cutover_mirror_and_page_read():
    root_id = "root"
    session = {
        "id": root_id,
        "messages": [
            {"id": "u1", "seq": 1, "role": "user", "content": "work"},
            {"id": "a1", "seq": 2, "role": "assistant", "content": "done"},
        ],
    }
    journal = CanonicalRuntimeJournal(HOME / "catalog.sqlite")
    rows = [{
        "root_id": root_id, "sid": root_id, "seq": 1, "type": "agent_message",
        "source": "claude", "msg_id": "a1",
        "data": {"uuid": "e1", "type": "assistant", "message": {"content": [{"type": "text", "text": "done"}]}},
    }]
    generation = journal.ensure_cutover(
        root_id, rows=rows, session=session,
    )
    first = journal.read_page(root_id, after_seq=0, limit=100)
    assert first["root_generation"] == generation
    assert {fact["payload_type"] for fact in first["facts"]} == {
        "user_prompt", "message_ownership_declared", "assistant_output",
    }
    journal.mirror_event(
        root_id=root_id, sid=root_id, seq=2, event_type="turn_complete",
        data={"message_id": "a1"}, source="claude", msg_id="a1",
        event_id="e2", turn_id="u1",
    )
    second = journal.read_page(
        root_id, after_seq=first["canonical_through_seq"], limit=100,
    )
    assert [fact["payload_type"] for fact in second["facts"]] == ["turn_complete"]
    journal.close()


if __name__ == "__main__":
    test_import_cutover_mirror_and_page_read()
    print("canonical runtime journal tests passed")
