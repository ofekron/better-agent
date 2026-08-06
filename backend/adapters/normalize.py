"""Pure journal-row -> contract Node mapping (ADR 0006). No I/O, no bus.

Mirrors frontend/src/utils/agentMessages.ts::flattenClaudeMessages, but
produces the typed backend.surface_contract.nodes vocabulary instead of
frontend WSEvent shapes. Total mapping: every branch reaches a Node: known
metadata/envelope rows are intentionally dropped (chat-panel.md: "Metadata
events ... never enter the render tree"); everything else that isn't
explicitly handled becomes NodeKind.UNKNOWN with the raw payload preserved.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime
from functools import partial
from typing import NamedTuple

from backend.surface_contract.identity import CONTRACT_VERSION, NodeId, SurfaceId, TurnId
from backend.surface_contract.nodes import (
    Attachment,
    AssistantTextPayload,
    CompactionOrigin,
    CompactionPayload,
    ContentStatus,
    DiagnosticCode,
    DiagnosticPayload,
    LifecycleNoticeKind,
    LifecycleNoticePayload,
    ModelChangePayload,
    ModelChangeSource,
    Node,
    NodeKind,
    PromptOrigin,
    ResultKind,
    ResultPayload,
    SendMode,
    ThinkingPayload,
    ToolInteractionPayload,
    TypedPromptPayload,
    UnknownPayload,
    WorkerInteractionPayload,
)

# Control-plane / non-content row types; intentionally never enter the
# render tree (chat-panel.md "Ingestion and rendering" / "Metadata events").
_DROPPED_METADATA_TYPES = frozenset(
    {
        "system", "queue-operation", "last-prompt", "attachment",
        "ai-title", "file-history-snapshot", "file-history-delta", "mode",
    }
)

# Raw provider protocol envelopes that leak through un-normalized (Codex
# rollout line types). Never valid chat content.
_DROPPED_ENVELOPE_TYPES = frozenset(
    {"response_item", "event_msg", "session_meta", "turn_context", "compacted", "thread.started"}
)

_WORKER_FACT_TYPES = frozenset({"worker_start", "worker_event", "worker_complete"})
_TODO_TOOL_NAMES = frozenset({"TodoWrite", "TaskCreate", "TaskUpdate"})
_LIFECYCLE_KIND_VALUES = {k.value for k in LifecycleNoticeKind}


def _parse_ts(row: dict) -> float:
    ts = row.get("ts")
    if isinstance(ts, (int, float)):
        return float(ts)
    if isinstance(ts, str):
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0
    return 0.0


def _seq(row: dict) -> int:
    seq = row.get("seq")
    return seq if isinstance(seq, int) else 0


def _uuid_of(row: dict) -> str | None:
    data = row.get("data")
    candidates = [row.get("uuid"), row.get("uid")]
    if isinstance(data, dict):
        candidates += [data.get("uuid"), data.get("uid")]
        inner = data.get("data")
        if isinstance(inner, dict):
            candidates += [inner.get("uuid"), inner.get("uid")]
    for c in candidates:
        if isinstance(c, str) and c:
            return c
    return None


def _fallback_id(row: dict, label: str) -> str:
    return f"seq:{_seq(row)}:{label}"


def typed_prompt_node_id(uuid_value: str | None) -> str | None:
    """Contract node_id for a TYPED_PROMPT node given a known row uuid —
    identity passthrough when non-empty (matching `_uuid_of`'s validity
    contract), None when not derivable. Shared so any caller holding a
    bare uuid (not a full row, e.g. a lifecycle fact's `prompt_uuid`)
    derives the SAME node_id `_handle_user` would from the row itself,
    instead of duplicating the validity check ad hoc."""
    return uuid_value if isinstance(uuid_value, str) and uuid_value else None


_PROMPT_ORIGIN_VALUES = {o.value for o in PromptOrigin}
_SEND_MODE_VALUES = {s.value for s in SendMode}


def parse_prompt_origin(value: object) -> PromptOrigin:
    return PromptOrigin(value) if value in _PROMPT_ORIGIN_VALUES else PromptOrigin.USER


def parse_send_mode(value: object) -> SendMode:
    return SendMode(value) if value in _SEND_MODE_VALUES else SendMode.QUEUE


def enrich_typed_prompt_node(node: Node, *, row_data: dict, meta: dict | None) -> Node:
    """Fold a joined `prompt_meta` fact (backend.turn_manager, ADR Phase C)
    onto a TYPED_PROMPT node's origin/send_mode.

    The CLI-authored row's OWN `data.origin`/`data.send_mode` (if a future
    write path ever stamps them directly) always wins over the
    backend-authored meta fact — meta only fills in fields the row itself
    is silent on. Idempotent: re-applying to an already-enriched node is a
    no-op (`origin`/`send_mode` already match, so the identity check below
    skips the `replace`), so callers may re-run this on every observation
    of the same row (seed + live upsert) without accumulating drift.
    """
    if node.kind != NodeKind.TYPED_PROMPT or not isinstance(node.payload, TypedPromptPayload):
        return node
    origin = (
        parse_prompt_origin(row_data.get("origin"))
        if row_data.get("origin") is not None
        else parse_prompt_origin(meta.get("origin")) if meta and meta.get("origin") is not None
        else node.payload.origin
    )
    send_mode = (
        parse_send_mode(row_data.get("send_mode"))
        if row_data.get("send_mode") is not None
        else parse_send_mode(meta.get("send_mode")) if meta and meta.get("send_mode") is not None
        else node.payload.send_mode
    )
    if origin == node.payload.origin and send_mode == node.payload.send_mode:
        return node
    return replace(node, payload=replace(node.payload, origin=origin, send_mode=send_mode))


def _tool_node_id(tool_use_id: str) -> str:
    return f"tool:{tool_use_id}"


def _tool_result_node_id(tool_use_id: str) -> str:
    return f"tool:{tool_use_id}:result"


def _block_node_id(base: str, idx: int, total: int) -> str:
    return base if total <= 1 else f"{base}:{idx}"


def _normalize_tool_name(name: str) -> str:
    if name.startswith("mcp__") and name.endswith("__delegate"):
        return "delegate"
    return name


def _content_to_text(content: object) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text" and isinstance(block.get("text"), str):
                    parts.append(block["text"])
                else:
                    try:
                        parts.append(json.dumps(block))
                    except (TypeError, ValueError):
                        parts.append(str(block))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    try:
        return json.dumps(content)
    except (TypeError, ValueError):
        return str(content)


def _node(
    *, row: dict, surface_id: SurfaceId, turn_id: TurnId, cv: int,
    node_id: NodeId, kind: NodeKind, status: ContentStatus | None, payload: object,
) -> Node:
    return Node(
        cv=cv, node_id=node_id, parent_id=None, turn_id=turn_id, surface_id=surface_id,
        kind=kind, ts=_parse_ts(row), seq=_seq(row), status=status, payload=payload,
    )


def normalize_journal_row(
    row: dict, *, surface_id: SurfaceId, turn_id: TurnId, cv: int = CONTRACT_VERSION
) -> list[Node]:
    row_type = row.get("type")
    data = row.get("data")
    data = data if isinstance(data, dict) else {}
    node = partial(_node, row=row, surface_id=surface_id, turn_id=turn_id, cv=cv)

    if row_type == "prompt_meta":
        # Backend-authored provenance fact (turn_manager.py) joined onto
        # the matching TYPED_PROMPT node by msg_id in chat_adapter.py —
        # never a node of its own (chat-panel.md "Metadata events").
        return []
    if row_type == "agent_message":
        return _handle_agent_message(row, data, node)
    if row_type in _WORKER_FACT_TYPES:
        uuid = _uuid_of(row) or _fallback_id(row, row_type)
        return [
            node(
                node_id=f"worker:{uuid}", kind=NodeKind.WORKER_INTERACTION, status=ContentStatus.COMPLETE,
                payload=WorkerInteractionPayload(fact_kind=row_type, fact=data),
            )
        ]

    uuid = _uuid_of(row) or _fallback_id(row, "unknown")
    return [
        node(
            node_id=uuid, kind=NodeKind.UNKNOWN, status=ContentStatus.COMPLETE,
            payload=UnknownPayload(label=f"row.{row_type or '(none)'}", payload=row),
        )
    ]


def _handle_agent_message(row: dict, data: dict, node: partial) -> list[Node]:
    mtype = data.get("type")

    if mtype in _DROPPED_METADATA_TYPES or mtype in _DROPPED_ENVELOPE_TYPES:
        return []
    if mtype == "lifecycle_notice":
        return [_handle_lifecycle_notice(row, data, node)]
    if mtype == "user":
        return _handle_user(row, data, node)
    if mtype == "assistant":
        return _handle_assistant(row, data, node)
    if mtype == "result":
        return [_handle_result(row, data, node)]

    uuid = _uuid_of(row) or _fallback_id(row, "diagnostic")
    return [
        node(
            node_id=uuid, kind=NodeKind.DIAGNOSTIC, status=ContentStatus.COMPLETE,
            payload=DiagnosticPayload(
                severity="info", code=DiagnosticCode.OTHER,
                text=f"agent_message.{mtype or '(none)'}", data=data,
            ),
        )
    ]


def _handle_lifecycle_notice(row: dict, data: dict, node: partial) -> Node:
    notice = data.get("data")
    notice = notice if isinstance(notice, dict) else {}
    uuid = _uuid_of(row) or _fallback_id(row, "lifecycle_notice")
    kind_str = notice.get("kind")

    if kind_str == "compacted":
        origin_str = notice.get("origin")
        origin = (
            CompactionOrigin(origin_str)
            if origin_str in {o.value for o in CompactionOrigin}
            else CompactionOrigin.NATIVE
        )
        return node(
            node_id=uuid, kind=NodeKind.COMPACTION, status=ContentStatus.COMPLETE,
            payload=CompactionPayload(origin=origin, summary=notice.get("summary") or ""),
        )

    if kind_str in _LIFECYCLE_KIND_VALUES:
        return node(
            node_id=uuid, kind=NodeKind.LIFECYCLE_NOTICE, status=ContentStatus.COMPLETE,
            payload=LifecycleNoticePayload(kind=LifecycleNoticeKind(kind_str), data=notice),
        )

    return node(
        node_id=uuid, kind=NodeKind.UNKNOWN, status=ContentStatus.COMPLETE,
        payload=UnknownPayload(label=f"lifecycle_notice.{kind_str or '(none)'}", payload=notice),
    )


def _handle_result(row: dict, data: dict, node: partial) -> Node:
    """Provider-emitted terminal `result` row (Claude CLI session jsonl
    `type: "result"`, journaled via the primary CLI tailer's `ingest_orphan`
    backup path — the live SDK-callback path consumes the SDK's
    `ResultMessage` internally in `runner.py` and never forwards it to
    `save_ws_callback`, so this is the only channel that reaches
    events.jsonl). Maps to a structural RESULT/PROVIDER node so
    `derive.resolve_result`'s provider branch (`_is_marked_final`) activates
    instead of falling back to the trailing-assistant-text heuristic."""
    uuid = _uuid_of(row) or _fallback_id(row, "result")
    return node(
        node_id=uuid, kind=NodeKind.RESULT, status=None,
        payload=ResultPayload(result_kind=ResultKind.PROVIDER),
    )


def _handle_user(row: dict, data: dict, node: partial) -> list[Node]:
    inner = data.get("message")
    content = inner.get("content") if isinstance(inner, dict) else None

    if isinstance(content, list) and any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in content
    ):
        nodes: list[Node] = []
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            tool_use_id = block.get("tool_use_id")
            if not isinstance(tool_use_id, str):
                continue
            nodes.append(
                node(
                    node_id=_tool_result_node_id(tool_use_id), kind=NodeKind.TOOL_INTERACTION,
                    status=ContentStatus.COMPLETE,
                    payload=ToolInteractionPayload(
                        tool_name="", args={}, result={"output": _content_to_text(block.get("content"))},
                    ),
                )
            )
        return nodes

    text = _content_to_text(content if content is not None else inner)
    origin = parse_prompt_origin(data.get("origin"))
    send_mode = parse_send_mode(data.get("send_mode"))
    attachments = tuple(
        Attachment(name=a.get("name", ""), media_type=a.get("media_type", ""), ref=a.get("ref", ""))
        for a in data.get("attachments", [])
        if isinstance(a, dict)
    )
    status = ContentStatus.QUEUED if origin == PromptOrigin.QUEUED else ContentStatus.COMPLETE
    uuid = typed_prompt_node_id(_uuid_of(row)) or _fallback_id(row, "typed_prompt")
    return [
        node(
            node_id=uuid, kind=NodeKind.TYPED_PROMPT, status=status,
            payload=TypedPromptPayload(
                text=text, attachments=attachments, send_mode=send_mode, origin=origin,
                source_session_ref=data.get("source_session_ref"),
                sent_text=data.get("sent_text"), intent_id=data.get("intent_id"),
            ),
        )
    ]


# Per-block dispatch for assistant content; a block-level None return
# skips a structurally invalid block (never a recognized one).
def _handle_assistant(row: dict, data: dict, node: partial) -> list[Node]:
    inner = data.get("message")
    content = inner.get("content") if isinstance(inner, dict) else None
    if not isinstance(content, list):
        return []

    base_uuid = _uuid_of(row) or _fallback_id(row, "assistant")
    total = len(content)
    nodes: list[Node] = []

    for idx, raw in enumerate(content):
        if not isinstance(raw, dict):
            continue
        n = _assistant_block_node(raw, node, _block_node_id(base_uuid, idx, total))
        if n is not None:
            nodes.append(n)
    return nodes


def _assistant_block_node(raw: dict, node: partial, block_id: str) -> Node | None:
    btype = raw.get("type")

    if btype == "text":
        text = raw.get("text")
        if not isinstance(text, str):
            return None
        return node(
            node_id=block_id, kind=NodeKind.ASSISTANT_TEXT,
            status=ContentStatus.COMPLETE, payload=AssistantTextPayload(text=text),
        )
    if btype == "thinking":
        thinking = raw.get("thinking")
        if not isinstance(thinking, str):
            return None
        return node(
            node_id=block_id, kind=NodeKind.THINKING, status=ContentStatus.COMPLETE,
            payload=ThinkingPayload(text=thinking, redacted=False),
        )
    if btype == "redacted_thinking":
        return node(
            node_id=block_id, kind=NodeKind.THINKING, status=ContentStatus.COMPLETE,
            payload=ThinkingPayload(text="", redacted=True),
        )
    if btype in ("tool_use", "server_tool_use"):
        name, tool_use_id = raw.get("name"), raw.get("id")
        if not isinstance(name, str) or not isinstance(tool_use_id, str):
            return None
        return node(
            node_id=_tool_node_id(tool_use_id), kind=NodeKind.TOOL_INTERACTION,
            status=ContentStatus.STREAMING,
            payload=ToolInteractionPayload(
                tool_name=_normalize_tool_name(name), args=raw.get("input") or {}, result=None,
                derived_view="todo_snapshot" if name in _TODO_TOOL_NAMES else None,
            ),
        )
    if btype == "tool_result":
        tool_use_id = raw.get("tool_use_id")
        if not isinstance(tool_use_id, str):
            return None
        return node(
            node_id=_tool_result_node_id(tool_use_id), kind=NodeKind.TOOL_INTERACTION,
            status=ContentStatus.COMPLETE,
            payload=ToolInteractionPayload(
                tool_name="", args={}, result={"output": _content_to_text(raw.get("content"))},
            ),
        )
    if btype == "fallback":
        frm = raw.get("from") if isinstance(raw.get("from"), dict) else {}
        to = raw.get("to") if isinstance(raw.get("to"), dict) else {}
        from_model = frm.get("model") if isinstance(frm.get("model"), str) else None
        to_model = to.get("model") if isinstance(to.get("model"), str) else ""
        return node(
            node_id=block_id, kind=NodeKind.MODEL_CHANGE, status=ContentStatus.COMPLETE,
            payload=ModelChangePayload(from_run_ref=from_model, to_run_ref=to_model, source=ModelChangeSource.PROVIDER),
        )
    return node(
        node_id=block_id, kind=NodeKind.UNKNOWN, status=ContentStatus.COMPLETE,
        payload=UnknownPayload(label=f"block.{btype or '(none)'}", payload=raw),
    )


def pair_tool_results(nodes: list[Node]) -> list[Node]:
    """Merge a tool_use node with its later tool_result node (same tool
    call id, ":result"-suffixed node_id) into one tool_interaction node."""
    by_id = {n.node_id: n for n in nodes}
    result_suffix = ":result"
    consumed: set[NodeId] = set()
    merged: dict[NodeId, Node] = {}

    for n in nodes:
        if not n.node_id.endswith(result_suffix) or n.kind != NodeKind.TOOL_INTERACTION:
            continue
        use_id = n.node_id[: -len(result_suffix)]
        use_node = by_id.get(use_id)
        if use_node is None or use_node.kind != NodeKind.TOOL_INTERACTION:
            continue
        consumed.add(n.node_id)
        merged[use_id] = replace(
            use_node, status=ContentStatus.COMPLETE,
            payload=replace(use_node.payload, result=n.payload.result),
        )

    return [merged.get(n.node_id, n) for n in nodes if n.node_id not in consumed]


class ParentLink(NamedTuple):
    parent_uuid: str | None
    is_sidechain: bool
    parent_tool_use_id: str | None


def derive_link(row: dict) -> ParentLink:
    data = row.get("data")
    data = data if isinstance(data, dict) else {}
    parent_uuid = data.get("parentUuid")
    parent_tool_use_id = data.get("parent_tool_use_id")
    return ParentLink(
        parent_uuid=parent_uuid if isinstance(parent_uuid, str) else None,
        is_sidechain=bool(data.get("isSidechain")),
        parent_tool_use_id=parent_tool_use_id if isinstance(parent_tool_use_id, str) else None,
    )


def resolve_parents(nodes: list[Node], links: dict[NodeId, ParentLink]) -> list[Node]:
    """Second pass: fill in Node.parent_id from row-derived linkage. A
    parent_tool_use_id resolves to the tool_interaction node that spawned
    it; otherwise a parent_uuid resolves to the row-level node it replied
    to. Unresolvable links (target not present in this batch) leave
    parent_id unset rather than guessing."""
    node_ids = {n.node_id for n in nodes}
    out: list[Node] = []
    for n in nodes:
        link = links.get(n.node_id)
        parent_id: NodeId | None = n.parent_id
        if link is not None:
            if link.parent_tool_use_id:
                candidate = _tool_node_id(link.parent_tool_use_id)
                if candidate in node_ids and candidate != n.node_id:
                    parent_id = candidate
            elif link.parent_uuid and link.parent_uuid in node_ids and link.parent_uuid != n.node_id:
                parent_id = link.parent_uuid
        out.append(n if parent_id == n.parent_id else replace(n, parent_id=parent_id))
    return out
