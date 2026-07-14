import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from canonical_event_adapter import canonical_facts_from_journal_row


def test_agent_message_projects_text_tools_and_results():
    assistant = canonical_facts_from_journal_row({
        "root_id": "r", "sid": "r", "seq": 1, "type": "agent_message", "source": "claude", "msg_id": "a1",
        "data": {"uuid": "e1", "type": "assistant", "message": {"content": [
            {"type": "text", "text": "hello"},
            {"type": "tool_use", "id": "t1", "name": "Read", "input": {"path": "x"}},
        ]}},
    })
    assert [fact.payload_type for fact in assistant] == ["assistant_output", "tool_call"]
    result = canonical_facts_from_journal_row({
        "root_id": "r", "sid": "r", "seq": 2, "type": "agent_message", "source": "claude", "msg_id": "a1",
        "data": {"uuid": "e2", "type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "ok"},
        ]}},
    })
    assert result[0].payload_type == "tool_result"
    assert result[0].payload["tool_use_id"] == "t1"


if __name__ == "__main__":
    test_agent_message_projects_text_tools_and_results()
    print("canonical event adapter tests passed")
