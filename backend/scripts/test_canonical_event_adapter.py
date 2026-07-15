import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from canonical_event_adapter import canonical_facts_from_journal_row


def test_agent_message_projects_text_tools_and_results():
    assistant = canonical_facts_from_journal_row({
        "root_id": "r", "root_generation": 2, "sid": "r", "seq": 1, "type": "agent_message", "source": "claude", "msg_id": "a1", "timestamp": "2026-07-14T00:00:00Z",
        "data": {"uuid": "e1", "type": "assistant", "message": {"content": [
            {"type": "text", "text": "hello"},
            {"type": "tool_use", "id": "t1", "name": "Read", "input": {"path": "x"}},
        ]}},
    })
    assert [fact.payload_type for fact in assistant] == ["assistant_output", "tool_call"]
    assert assistant[0].root_generation == 2
    assert assistant[0].turn_id == "a1"
    assert assistant[0].source_timestamp == "2026-07-14T00:00:00Z"
    result = canonical_facts_from_journal_row({
        "root_id": "r", "sid": "r", "seq": 2, "type": "agent_message", "source": "claude", "msg_id": "a1",
        "data": {"uuid": "e2", "type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "ok"},
        ]}},
    })
    assert result[0].payload_type == "tool_result"
    assert result[0].payload["tool_use_id"] == "t1"


def test_legacy_manager_event_wrapper_unwraps_to_same_facts():
    wrapped = canonical_facts_from_journal_row({
        "root_id": "r", "sid": "r", "seq": 3, "type": "manager_event", "source": "claude", "msg_id": "a1",
        "data": {"event": {"type": "agent_message", "data": {"uuid": "e3", "type": "assistant", "message": {"content": [
            {"type": "text", "text": "from legacy wrapper"},
        ]}}}},
    })
    assert [fact.payload_type for fact in wrapped] == ["assistant_output"]
    assert wrapped[0].payload["text"] == "from legacy wrapper"


def test_thinking_blocks_become_thinking_facts():
    facts = canonical_facts_from_journal_row({
        "root_id": "r", "sid": "r", "seq": 4, "type": "agent_message", "source": "claude", "msg_id": "a1",
        "data": {"uuid": "e4", "type": "assistant", "message": {"content": [
            {"type": "thinking", "thinking": "let me plan"},
            {"type": "text", "text": "the answer"},
        ]}},
    })
    assert [fact.payload_type for fact in facts] == ["assistant_output", "thinking"]
    thinking = facts[1]
    assert thinking.payload == {"message_id": "a1", "text": "let me plan"}
    assert thinking.source_event_id == "e4:think:0"


if __name__ == "__main__":
    test_agent_message_projects_text_tools_and_results()
    test_legacy_manager_event_wrapper_unwraps_to_same_facts()
    test_thinking_blocks_become_thinking_facts()
    print("canonical event adapter tests passed")
