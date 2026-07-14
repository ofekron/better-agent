import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from canonical_event import CanonicalFact, CommittedFact, SourceOrder
from chat_forest_projection import ChatForestProjector


def committed(seq, kind, payload, *, event=None, order=None, turn="u1"):
    fact = CanonicalFact.create(
        root_id="root",
        root_generation=3,
        sid="root",
        source="claude",
        source_stream_id="stream",
        source_event_id=event or f"e{seq}",
        source_order=SourceOrder(sequence=order or seq),
        payload_type=kind,
        payload=payload,
        update_semantics="final" if payload.get("final") else "snapshot",
        turn_id=turn,
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
    assert forest.root_generation == 3
    assert tree.turn_id == "u1"
    assert tree.events_collapsed_by_default
    assert not tree.prompt_text_collapsed_by_default
    assert tree.collapsed_preview == "working"


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
    assert tree.has_late_output


def test_steer_queue_and_worker_identity_are_projection_facts():
    facts = [
        committed(1, "user_prompt", {"message_id": "u1", "text": "later", "queued": True}),
        committed(2, "steer_prompt", {"prompt_message_id": "u1", "text": "change it"}),
        committed(3, "worker_final", {"prompt_message_id": "u1", "parent_tool_use_id": "tool-1", "child_source": "child-a", "text": "done"}),
    ]
    tree = ChatForestProjector().project("root", facts).trees[0]
    assert tree.queued and tree.status == "queued"
    assert [node.kind for node in tree.work] == ["steer_prompt", "worker_final"]
    assert tree.work[1].id != stable_worker_id_for_other_parent(tree.work[1].id, facts)


def stable_worker_id_for_other_parent(actual, facts):
    changed = committed(3, "worker_final", {"prompt_message_id": "u1", "parent_tool_use_id": "tool-2", "child_source": "child-a", "text": "done"})
    return ChatForestProjector().project("root", [facts[0], changed]).trees[0].work[0].id


def test_snapshot_selection_keeps_standalone_facts_and_latest_update():
    facts = [
        committed(1, "assistant_output", {"text": "old"}, event="stream", order=1),
        committed(2, "assistant_output", {"text": "new"}, event="stream", order=2),
        *[
            committed(index + 3, "tool_call", {"tool_use_id": f"t{index}", "tool": "Read"})
            for index in range(2_000)
        ],
    ]
    tree = ChatForestProjector().project("root", facts).trees[0]
    assert [node.text for node in tree.explanations] == ["new"]
    assert len(tree.work) == 2_000


if __name__ == "__main__":
    test_forest_groups_prompt_explanation_and_work()
    test_source_order_beats_arrival_and_late_output_survives_terminal()
    test_steer_queue_and_worker_identity_are_projection_facts()
    test_snapshot_selection_keeps_standalone_facts_and_latest_update()
    print("chat forest projection tests passed")
