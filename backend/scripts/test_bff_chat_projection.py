import asyncio
import tempfile
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bff_chat_projection import ChatProjectionService
from bff_projection_registry import ProjectionRegistry
from canonical_event import CanonicalFact, SourceOrder
from canonical_event_adapter import canonical_message_facts, fact_to_wire


class Runtime:
    def __init__(self):
        self.calls = []
        self.extra = []

    async def projection_source(self, session_id, *, after_seq=0, limit=2000):
        self.calls.append(after_seq)
        fact = CanonicalFact.create(
            root_id=session_id, root_generation=4, sid=session_id, source="claude", source_stream_id="run",
            source_event_id="out", source_order=SourceOrder(1), payload_type="assistant_output",
            payload={"message_id": "a1", "text": "done", "final": True}, update_semantics="final",
        )
        session = {"id": session_id, "generation": 4, "messages": [
            {"id": "u1", "seq": 1, "role": "user", "content": "work"},
            {"id": "a1", "seq": 2, "role": "assistant", "content": "done"},
        ]}
        message_facts = canonical_message_facts(session_id, session)
        rows = [
            *[fact_to_wire(item, index) for index, item in enumerate(message_facts, 1)],
            fact_to_wire(fact, 3),
            *self.extra,
        ]
        page = [row for row in rows if int(row["canonical_seq"]) > after_seq]
        return {
            "found": True,
            "session": session,
            "root_generation": 4,
            "facts": page, "has_more": False,
            "next_seq": max((int(row["canonical_seq"]) for row in rows), default=after_seq),
        }

    def append_output(self):
        fact = CanonicalFact.create(
            root_id="root", root_generation=4, sid="root", source="claude", source_stream_id="run",
            source_event_id="late", source_order=SourceOrder(4), payload_type="assistant_output",
            payload={"message_id": "a1", "text": "late", "late": True}, update_semantics="final",
        )
        self.extra.append(fact_to_wire(fact, 4))


def test_bff_owns_prompt_tree_and_epoch():
    registry = ProjectionRegistry(Path(tempfile.mkdtemp()) / "registry.sqlite")
    runtime = Runtime()
    service = ChatProjectionService(runtime, registry)
    snapshot = asyncio.run(service.snapshot("root"))
    tree = snapshot["forest"]["trees"][0]
    assert tree["prompt"]["text"] == "work"
    assert tree["explanations"][0]["text"] == "done"
    assert snapshot["revision"] == 1 and snapshot["checksum"]
    assert snapshot["root_generation"] == 4
    unchanged = asyncio.run(service.updates(
        "root", epoch=snapshot["epoch"], after_revision=snapshot["revision"],
    ))
    assert unchanged["kind"] == "delta" and unchanged["upsert_trees"] == []
    assert runtime.calls == [0, 3]
    runtime.append_output()
    delta = asyncio.run(service.updates(
        "root", epoch=snapshot["epoch"], after_revision=snapshot["revision"],
    ))
    assert delta["kind"] == "delta" and delta["target_revision"] == 2
    assert delta["upsert_trees"][0]["has_late_output"]
    assert service.cache_stats()["hits"] >= 2
    registry.close()


if __name__ == "__main__":
    test_bff_owns_prompt_tree_and_epoch()
    print("BFF chat projection tests passed")
