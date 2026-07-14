import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from canonical_event import CanonicalFact, CommittedFact, SourceOrder
from chat_forest_projection import ChatForestProjector


def committed(seq, kind, payload, *, event=None, order=None):
    fact = CanonicalFact.create(
        root_id="root",
        sid="root",
        source="claude",
        source_stream_id="stream",
        source_event_id=event or f"e{seq}",
        source_order=SourceOrder(sequence=order or seq),
        payload_type=kind,
        payload=payload,
        update_semantics="final" if payload.get("final") else "snapshot",
    )
    return CommittedFact(canonical_seq=seq, acceptance_ticket=seq, fact=fact)


def test_forest_groups_prompt_explanation_and_work():
    facts = [
        committed(1, "user_prompt", {"message_id": "u1", "text": "do it"}),
        committed(2, "assistant_output", {"message_id": "a1", "prompt_message_id": "u1", "text": "working"}),
        committed(3, "tool_call", {"message_id": "a1", "prompt_message_id": "u1", "tool_use_id": "t1", "tool": "Read"}),
        committed(4, "tool_result", {"message_id": "a1", "prompt_message_id": "u1", "tool_use_id": "t1", "output": "ok"}),
    ]
    forest = ChatForestProjector().project("root", facts)
    assert len(forest.trees) == 1
    tree = forest.trees[0]
    assert tree.prompt.text == "do it"
    assert tree.explanations[0].text == "working"
    assert tree.work[0].kind == "tool_interaction"
    assert tree.work[0].payload["result"]["output"] == "ok"


def test_source_order_beats_arrival_and_late_output_survives_terminal():
    facts = [
        committed(1, "assistant_output", {"message_id": "a1", "text": "final", "final": True}, event="out", order=3),
        committed(2, "assistant_output", {"message_id": "a1", "text": "old"}, event="out", order=2),
        committed(3, "turn_failed", {"message_id": "a1", "error": "stopped"}),
        committed(4, "assistant_output", {"message_id": "a1", "text": "late", "late": True}, event="late", order=4),
    ]
    tree = ChatForestProjector().project("root", facts).trees[0]
    assert [item.text for item in tree.explanations] == ["final", "late"]
    assert tree.status == "failed"


if __name__ == "__main__":
    test_forest_groups_prompt_explanation_and_work()
    test_source_order_beats_arrival_and_late_output_survives_terminal()
    print("chat forest projection tests passed")
