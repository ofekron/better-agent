from __future__ import annotations

from typing import Any, Iterable

from canonical_event import CanonicalFact, SourceOrder
from event_shape import normalize_agent_event


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


def subagent_scope_source_event_id(tool_use_id: str) -> str:
    """Deterministic source_event_id for a tool_use that may spawn a nested
    subagent scope (Agent/Task/Workflow and provider-equivalent calls).

    Shared, single-source naming convention: this module stamps it as the
    `source_event_id` of the spawning tool_call fact; `chat_canonical_adapter`
    and `chat_projection_ingestion` both derive the identical value from a
    sidechain event's `payload["parent_tool_use_id"]` alone, with no
    cross-fact lookup required.
    """
    return f"tool_use:{tool_use_id}"


def _parent_tool_use_id(data: dict[str, Any]) -> str | None:
    value = data.get("parent_tool_use_id")
    return value if isinstance(value, str) and value else None


def canonical_facts_from_journal_row(row: dict[str, Any]) -> list[CanonicalFact]:
    root_id = str(row.get("root_id") or row.get("sid") or "")
    sid = str(row.get("sid") or root_id)
    seq = row.get("seq")
    if not root_id or not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
        raise ValueError("journal row requires root/sid and non-negative seq")
    raw_type = str(row.get("type") or "unknown")
    raw_data = row.get("data")
    normalized = normalize_agent_event({
        "type": raw_type,
        "data": raw_data if isinstance(raw_data, dict) else {},
    })
    event_type = str(normalized.get("type") or "unknown")
    data = normalized.get("data")
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
    parent_tool_use_id = _parent_tool_use_id(data)
    parent_payload = {"parent_tool_use_id": parent_tool_use_id} if parent_tool_use_id else {}
    facts: list[CanonicalFact] = []
    if role == "assistant":
        text = _text_blocks(content)
        if text:
            facts.append(CanonicalFact.create(
                **common,
                source_event_id=event_id,
                payload_type="assistant_output",
                payload={"message_id": message_id, "text": text, "final": data.get("final_answer") is True, **parent_payload},
                update_semantics="final" if data.get("final_answer") is True else "snapshot",
            ))
        for index, block in enumerate(content if isinstance(content, list) else []):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "thinking":
                thought = block.get("thinking") or block.get("text")
                if isinstance(thought, str) and thought:
                    facts.append(CanonicalFact.create(
                        **common,
                        source_event_id=f"{event_id}:think:{index}",
                        payload_type="thinking",
                        payload={"message_id": message_id, "text": thought, **parent_payload},
                        update_semantics="snapshot",
                    ))
                continue
            if block.get("type") == "tool_use":
                tool_use_id = block.get("id")
                facts.append(CanonicalFact.create(
                    **common,
                    source_event_id=(
                        subagent_scope_source_event_id(tool_use_id)
                        if isinstance(tool_use_id, str) and tool_use_id
                        else f"{event_id}:tool:{index}"
                    ),
                    payload_type="tool_call",
                    payload={
                        "message_id": message_id, "tool_use_id": tool_use_id,
                        "tool": block.get("name"), "args": block.get("input"),
                        **parent_payload,
                    },
                    update_semantics="snapshot",
                ))
                continue
            if block.get("type") == "text":
                continue
            # Unknown assistant content block: emit a typed fact rather
            # than dropping it, so new provider block types stay visible
            # all the way through the rendering pipeline until handled.
            facts.append(CanonicalFact.create(
                **common,
                source_event_id=f"{event_id}:block:{index}",
                payload_type="unsupported_block",
                payload={
                    "message_id": message_id,
                    "block_type": str(block.get("type") or "(none)"),
                    "block": block,
                    **parent_payload,
                },
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
                payload={
                    "message_id": message_id, "tool_use_id": block.get("tool_use_id"),
                    "output": block.get("content"), **parent_payload,
                },
                update_semantics="snapshot",
            ))
    return facts


def canonical_facts_from_rows(rows: Iterable[dict[str, Any]]) -> list[CanonicalFact]:
    return [fact for row in rows for fact in canonical_facts_from_journal_row(row)]


def walk_session_nodes(session: dict[str, Any]) -> list[dict[str, Any]]:
    """Root-first walk over the session tree: root, then every nested fork.

    Root-first order is load-bearing for fact dedup: a fork's copied
    message prefix produces facts with the same fact_id as the root's
    originals (sid is not part of fact identity), and first-write-wins
    dedup must keep the root-scoped fact.
    """
    nodes = [session]
    for fork in session.get("forks") or []:
        if isinstance(fork, dict):
            nodes.extend(walk_session_nodes(fork))
    return nodes


def session_message_heads(session: dict[str, Any]) -> dict[str, int]:
    """Highest message seq per session-tree node id (message seqs are
    per-node counters; forks continue their own after the fork point)."""
    heads: dict[str, int] = {}
    for node in walk_session_nodes(session):
        node_id = str(node.get("id") or "")
        if not node_id:
            continue
        for message in reversed(node.get("messages") or []):
            if not isinstance(message, dict):
                continue
            seq = message.get("seq")
            if isinstance(seq, int) and not isinstance(seq, bool):
                heads[node_id] = max(heads.get(node_id, -1), seq)
                break
    return heads


def _node_message_facts(
    root_id: str,
    root_generation: int,
    node: dict[str, Any],
    after_seq: int,
) -> list[CanonicalFact]:
    facts: list[CanonicalFact] = []
    current_prompt = ""
    messages = node.get("messages") or []
    start = len(messages)
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        seq = message.get("seq") if isinstance(message, dict) else None
        if isinstance(seq, int) and not isinstance(seq, bool) and seq <= after_seq:
            break
        start = index
    for message in reversed(messages[:start]):
        if isinstance(message, dict) and message.get("role") == "user" and message.get("id"):
            current_prompt = str(message["id"])
            break
    for message in messages[start:]:
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
                root_id=root_id, root_generation=root_generation, sid=str(node.get("id") or root_id), source="session",
                source_stream_id=f"session:{root_id}", source_event_id=message_id,
                source_order=SourceOrder(sequence=message_seq), payload_type="user_prompt",
                payload={"message_id": message_id, "text": str(message.get("content") or "")},
                update_semantics="snapshot", turn_id=message_id,
            ))
        elif role == "assistant" and current_prompt:
            facts.append(CanonicalFact.create(
                root_id=root_id, root_generation=root_generation, sid=str(node.get("id") or root_id), source="session",
                source_stream_id=f"session:{root_id}", source_event_id=f"owner:{message_id}",
                source_order=SourceOrder(sequence=message_seq), payload_type="message_ownership_declared",
                payload={"message_id": message_id, "prompt_message_id": current_prompt},
                update_semantics="snapshot", turn_id=current_prompt,
            ))
    return facts


def canonical_message_facts(
    root_id: str,
    session: dict[str, Any],
    *,
    heads: dict[str, int] | None = None,
) -> list[CanonicalFact]:
    """Message-scaffold facts for every session-tree node (root + forks).

    `heads` gates emission per node id (a node's messages with
    seq <= heads[node_id] are already covered); None emits everything.
    """
    root_generation = int(session.get("generation", 0))
    facts: list[CanonicalFact] = []
    for node in walk_session_nodes(session):
        node_id = str(node.get("id") or root_id)
        after_seq = (heads or {}).get(node_id, -1)
        facts.extend(
            _node_message_facts(root_id, root_generation, node, after_seq)
        )
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
