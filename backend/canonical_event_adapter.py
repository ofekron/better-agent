from __future__ import annotations

from typing import Any, Iterable

from canonical_event import CanonicalFact, SourceOrder


def _uuid(data: dict[str, Any], fallback: str) -> str:
    value = data.get("uuid")
    return value if isinstance(value, str) and value else fallback


def _text_blocks(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        str(block.get("text") or "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    )


def canonical_facts_from_journal_row(row: dict[str, Any]) -> list[CanonicalFact]:
    root_id = str(row.get("root_id") or row.get("sid") or "")
    sid = str(row.get("sid") or root_id)
    seq = row.get("seq")
    if not root_id or not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
        raise ValueError("journal row requires root/sid and non-negative seq")
    event_type = str(row.get("type") or "unknown")
    data = row.get("data")
    if not isinstance(data, dict):
        data = {}
    source = str(row.get("source") or "legacy")
    stream = str(row.get("run_id") or f"{source}:{sid}")
    message_id = str(row.get("msg_id") or "")
    common = {
        "root_id": root_id,
        "root_generation": int(row.get("root_generation", 0)),
        "sid": sid,
        "source": source,
        "source_stream_id": stream,
        "source_order": SourceOrder(sequence=seq),
        "observed_at": str(row.get("observed_at") or row.get("timestamp") or "") or None,
        "source_timestamp": str(row.get("timestamp") or "") or None,
        "turn_id": str(row.get("turn_id") or row.get("msg_id") or "") or None,
    }
    if event_type != "agent_message":
        return [CanonicalFact.create(
            **common,
            source_event_id=_uuid(data, f"journal:{seq}"),
            payload_type=event_type,
            payload={**data, "message_id": message_id},
            update_semantics="snapshot",
        )]
    role = data.get("type")
    message = data.get("message")
    message = message if isinstance(message, dict) else {}
    content = message.get("content")
    event_id = _uuid(data, f"journal:{seq}")
    facts: list[CanonicalFact] = []
    if role == "assistant":
        text = _text_blocks(content)
        if text:
            facts.append(CanonicalFact.create(
                **common,
                source_event_id=event_id,
                payload_type="assistant_output",
                payload={"message_id": message_id, "text": text, "final": data.get("final_answer") is True},
                update_semantics="final" if data.get("final_answer") is True else "snapshot",
            ))
        for index, block in enumerate(content if isinstance(content, list) else []):
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            facts.append(CanonicalFact.create(
                **common,
                source_event_id=f"{event_id}:tool:{index}",
                payload_type="tool_call",
                payload={"message_id": message_id, "tool_use_id": block.get("id"), "tool": block.get("name"), "args": block.get("input")},
                update_semantics="snapshot",
            ))
    elif role == "user":
        for index, block in enumerate(content if isinstance(content, list) else []):
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            facts.append(CanonicalFact.create(
                **common,
                source_event_id=f"{event_id}:result:{index}",
                payload_type="tool_result",
                payload={"message_id": message_id, "tool_use_id": block.get("tool_use_id"), "output": block.get("content")},
                update_semantics="snapshot",
            ))
    return facts


def canonical_facts_from_rows(rows: Iterable[dict[str, Any]]) -> list[CanonicalFact]:
    return [fact for row in rows for fact in canonical_facts_from_journal_row(row)]


def canonical_message_facts(root_id: str, session: dict[str, Any]) -> list[CanonicalFact]:
    facts: list[CanonicalFact] = []
    root_generation = int(session.get("generation", 0))
    current_prompt = ""
    for message in session.get("messages") or []:
        if not isinstance(message, dict):
            continue
        message_id = str(message.get("id") or "")
        message_seq = message.get("seq")
        if not message_id or not isinstance(message_seq, int) or isinstance(message_seq, bool):
            continue
        role = message.get("role")
        if role == "user":
            current_prompt = message_id
            facts.append(CanonicalFact.create(
                root_id=root_id, root_generation=root_generation, sid=str(session.get("id") or root_id), source="session",
                source_stream_id=f"session:{root_id}", source_event_id=message_id,
                source_order=SourceOrder(sequence=message_seq), payload_type="user_prompt",
                payload={"message_id": message_id, "text": str(message.get("content") or "")},
                update_semantics="snapshot", turn_id=message_id,
            ))
        elif role == "assistant" and current_prompt:
            facts.append(CanonicalFact.create(
                root_id=root_id, root_generation=root_generation, sid=str(session.get("id") or root_id), source="session",
                source_stream_id=f"session:{root_id}", source_event_id=f"owner:{message_id}",
                source_order=SourceOrder(sequence=message_seq), payload_type="message_ownership_declared",
                payload={"message_id": message_id, "prompt_message_id": current_prompt},
                update_semantics="snapshot", turn_id=current_prompt,
            ))
    return facts


def fact_to_wire(fact: CanonicalFact, canonical_seq: int) -> dict[str, Any]:
    return {
        "canonical_seq": canonical_seq,
        "schema_version": fact.schema_version,
        "fact_id": fact.fact_id,
        "root_id": fact.root_id,
        "root_generation": fact.root_generation,
        "sid": fact.sid,
        "source": fact.source,
        "source_stream_id": fact.source_stream_id,
        "source_event_id": fact.source_event_id,
        "source_order": {"generation": fact.source_order.generation, "sequence": fact.source_order.sequence},
        "payload_type": fact.payload_type,
        "payload": fact.payload,
        "update_semantics": fact.update_semantics,
        "content_hash": fact.content_hash,
        "observed_at": fact.observed_at,
        "source_timestamp": fact.source_timestamp,
        "turn_id": fact.turn_id,
        "correction_of": fact.correction_of,
    }
