"""Pure journal-row -> contract Node mapping (ADR 0006). No I/O, no bus.

Mirrors frontend/src/utils/agentMessages.ts::flattenClaudeMessages, but
produces the typed backend.surface_contract.nodes vocabulary instead of
frontend WSEvent shapes. Total mapping: every branch reaches a Node: known
metadata/envelope rows are intentionally dropped (chat-panel.md: "Metadata
events ... never enter the render tree"); everything else that isn't
explicitly handled becomes NodeKind.UNKNOWN with the raw payload preserved.
"""

from __future__ import annotations

import base64
import binascii
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
    FailurePayload,
    FailureResolution,
    FailureSeverity,
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
_FAILURE_NODE_ID_PREFIX = "failure:"

# `user_message_failed` `reason` -> (code, severity, retryable, resolution).
# Single source of truth for this table — `backend.adapters.chat_adapter`
# imports `failure_payload_for_reason` rather than keeping its own copy.
# See backend/surface_commands.py:663/678/716 and backend/run_recovery.py
# :1430/1841/3759 for the emit sites this mirrors. Reasons absent here
# (including any future/unmapped one) fall through to `code=reason`
# verbatim with contract defaults (severity=error, retryable=False,
# resolution=none) — never invented text, never a raw-JSON dump.
_FAILURE_REASON_MAP: dict[str, tuple[str, FailureSeverity, bool, FailureResolution]] = {
    "orphaned_before_provider": (
        "recovery_unknown", FailureSeverity.ERROR, True, FailureResolution.RETRY,
    ),
    "missing_bound_provider_run": (
        "recovery_unknown", FailureSeverity.ERROR, True, FailureResolution.RETRY,
    ),
    "recovered_run_failed": (
        "recovery_unknown", FailureSeverity.ERROR, True, FailureResolution.RETRY,
    ),
    "interrupt_failed": (
        "admission_rejected", FailureSeverity.ERROR, False, FailureResolution.NONE,
    ),
    "alter_interrupt_failed": (
        "admission_rejected", FailureSeverity.ERROR, False, FailureResolution.NONE,
    ),
    "durable_admission_failed": (
        "admission_rejected", FailureSeverity.ERROR, False, FailureResolution.NONE,
    ),
}


def failure_payload_for_reason(reason: str, error: str | None) -> FailurePayload:
    """`user_message_failed`'s `reason` (+ optional `error` text) -> the
    taxonomized FailurePayload. Shared by the live bus-handler
    (`chat_adapter._on_user_message_failed`) and the journaled-row branch
    below (`_handle_user_message_failed`) so both paths — live-instant and
    reload-reconstructed — classify the SAME fact identically."""
    mapped = _FAILURE_REASON_MAP.get(reason)
    code, severity, retryable, resolution = (
        mapped if mapped is not None
        else (reason, FailureSeverity.ERROR, False, FailureResolution.NONE)
    )
    text = error if isinstance(error, str) and error else f"user message failed: {reason}"
    return FailurePayload(
        code=code, text=text, severity=severity, retryable=retryable, resolution=resolution,
    )


def user_message_failed_node_id(lifecycle_msg_id: str) -> NodeId:
    """Deterministic FAILURE node id for a `user_message_failed` fact —
    stable across the live broadcast and any later replay/reconstruction
    of the same `lifecycle_msg_id`."""
    return f"{_FAILURE_NODE_ID_PREFIX}{lifecycle_msg_id}"


def turn_error_meta_node_id(assistant_msg_id: str) -> NodeId:
    """Deterministic FAILURE node id for a `turn_error_meta` fact —
    namespaced `err:` (distinct from `user_message_failed`'s bare
    `failure:{lifecycle_msg_id}`) and keyed by the turn-owning assistant
    message id (`backend.turn_manager._publish_turn_error_meta`'s journal
    OWNERSHIP `msg_id`) so a retried prompt's second turn attempt gets its
    own FAILURE node rather than colliding with the first."""
    return f"{_FAILURE_NODE_ID_PREFIX}err:{assistant_msg_id}"


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


def is_canonical_prompt_row(row: dict) -> bool:
    """True iff `row` is the backend-authored canonical TYPED_PROMPT row
    (`turn_manager.TurnManager._publish_typed_prompt_journal`, journaled at
    turn dispatch, `data["uuid"] == user_msg["id"]`) rather than a raw
    provider-transcript ECHO of the same prompt — the CLI/SDK session
    jsonl's own `type: "user"` line, tailed into the journal via
    `jsonl_tailer.OwnedClaudeJsonlTailer`/`ingest_orphan` with a DIFFERENT
    uuid and no ownership `msg_id` of its own (native-mode turns: the
    SDK's live response stream never yields this line at all — only the
    backup file-tailer's orphan path ever journals it, always
    `msg_id=None`, on an unpredictable schedule — so `msg_id`/ownership
    equality can never be used to recognize it).

    The backend writer ALWAYS stamps `data["origin"]`; a raw provider
    transcript line never carries that field (verified: `_split_image_
    attachments`/`_content_to_text` are the only producers of a raw `type:
    "user"` row's `data`, from `orchs.base._normalize_for_render`'s
    pass-through of the provider's own line, which has no `origin` key).
    Presence of `origin` is therefore a purely structural discriminator —
    derivable from the row alone, independent of ingestion path or
    ownership-resolution timing — used by `chat_adapter._segment_turns`/
    `_on_event_written` to recognize an echo of the CURRENTLY OPEN turn's
    own prompt and drop it rather than misfile it as a new turn boundary.
    """
    data = row.get("data")
    return (
        isinstance(data, dict)
        and data.get("type") == "user"
        and data.get("origin") is not None
    )


_PROMPT_ORIGIN_VALUES = {o.value for o in PromptOrigin}
_SEND_MODE_VALUES = {s.value for s in SendMode}


def parse_prompt_origin(value: object) -> PromptOrigin:
    return PromptOrigin(value) if value in _PROMPT_ORIGIN_VALUES else PromptOrigin.USER


def parse_send_mode(value: object) -> SendMode:
    return SendMode(value) if value in _SEND_MODE_VALUES else SendMode.QUEUE


def _fill_image_attachment_refs(
    attachments: tuple[Attachment, ...], image_filenames: object,
) -> tuple[Attachment, ...]:
    """Fill `ref` on image-typed attachments that don't have one yet,
    positionally, from `prompt_meta`'s `image_filenames`
    (`turn_manager.TurnManager._image_filenames`) — same per-message
    ordering `_save_message_images` (orchestrator.py) used to name the
    saved files, and the same order `_split_image_attachments` produced
    them in. Only touches image/* attachments with an empty `ref`, so an
    already-filled attachment (idempotent re-application, or a future
    direct-attachment write path that supplies its own `ref`) is never
    overwritten. Extra/missing filenames (count mismatch) fill as many as
    line up positionally and leave the rest untouched — never a guessed
    mapping."""
    if not isinstance(image_filenames, list) or not image_filenames:
        return attachments
    filenames = iter(
        fname for fname in image_filenames if isinstance(fname, str) and fname
    )
    out: list[Attachment] = []
    changed = False
    for att in attachments:
        if att.media_type.startswith("image/") and not att.ref:
            fname = next(filenames, None)
            if fname is not None:
                att = replace(att, ref=fname)
                changed = True
        out.append(att)
    return tuple(out) if changed else attachments


def enrich_typed_prompt_node(node: Node, *, row_data: dict, meta: dict | None) -> Node:
    """Fold a joined `prompt_meta` fact (backend.turn_manager, ADR Phase C)
    onto a TYPED_PROMPT node's origin/send_mode/attachment refs.

    The CLI-authored row's OWN `data.origin`/`data.send_mode` (if a future
    write path ever stamps them directly) always wins over the
    backend-authored meta fact — meta only fills in fields the row itself
    is silent on. Idempotent: re-applying to an already-enriched node is a
    no-op (`origin`/`send_mode` already match and every image attachment
    already has a `ref`, so the identity check below skips the
    `replace`), so callers may re-run this on every observation of the
    same row (seed + live upsert) without accumulating drift.
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
    attachments = (
        _fill_image_attachment_refs(node.payload.attachments, meta.get("image_filenames"))
        if meta else node.payload.attachments
    )
    if (
        origin == node.payload.origin
        and send_mode == node.payload.send_mode
        and attachments is node.payload.attachments
    ):
        return node
    return replace(
        node, payload=replace(node.payload, origin=origin, send_mode=send_mode, attachments=attachments),
    )


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


# Anthropic-API `source.media_type` -> file extension, for synthesizing a
# display name on an image content block (which carries no filename of its
# own — only better_agent's OWN send path names saved files, and that
# happens in a wholly separate store this pure module has no reach into;
# see _split_image_attachments's docstring).
_IMAGE_EXT_BY_MEDIA_TYPE = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
}


def _split_image_attachments(content: object) -> tuple[object, tuple[Attachment, ...]]:
    """Pull `{"type":"image","source":{"type":"base64","media_type":...,
    "data":...}}` blocks (the shape runner.py's `_multimodal_msg` sends,
    which the CLI/SDK echoes back verbatim into its own transcript — the
    row this module reads) out of a user row's `content` list, returning
    the remaining blocks (for `_content_to_text`) plus one `Attachment`
    per image found.

    `ref` is deliberately empty here: the base64 bytes live ONLY in this
    journal row. `_save_message_images` (orchestrator.py) persists a
    served copy under `<ba_home>/sessions/images/<session_id>/`, keyed by
    session_manager's OWN generated message id — a value this module
    (journal-row-scoped, no I/O, no store access; see module docstring)
    has no way to learn on its own. `enrich_typed_prompt_node` fills it in
    afterward from the joined `prompt_meta` fact's `image_filenames`
    (`turn_manager.py`'s `TurnManager._image_filenames`), positionally, by
    the same per-message ordering `_save_message_images` used to name the
    files. Until that join runs, an empty `ref` degrades to a
    nameless/unresolvable badge client-side — never a wrong or
    synthesized location.

    `size` is the decoded byte length — cheap (small images, pure CPU, no
    I/O) and always derivable from the block itself. Malformed base64
    degrades to `size=None` rather than raising: a display nicety, never
    worth failing normalization over.

    Non-list `content` (plain string / None) has no blocks to split;
    returned unchanged with zero attachments.
    """
    if not isinstance(content, list):
        return content, ()
    remaining: list[object] = []
    attachments: list[Attachment] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "image":
            source = block.get("source")
            media_type = source.get("media_type", "") if isinstance(source, dict) else ""
            ext = _IMAGE_EXT_BY_MEDIA_TYPE.get(media_type, "bin")
            data = source.get("data") if isinstance(source, dict) else None
            size: int | None = None
            if isinstance(data, str):
                try:
                    size = len(base64.b64decode(data, validate=True))
                except (binascii.Error, ValueError):
                    size = None
            attachments.append(
                Attachment(
                    name=f"image_{len(attachments)}.{ext}", media_type=media_type,
                    ref="", size=size,
                )
            )
            continue
        remaining.append(block)
    return remaining, tuple(attachments)


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
    if row_type == "user_message_failed":
        return _handle_user_message_failed(row, data, node)
    if row_type == "turn_error_meta":
        return _handle_turn_error_meta(row, data, node)
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


def _handle_user_message_failed(row: dict, data: dict, node: partial) -> list[Node]:
    """Journaled `user_message_failed` BusEvent row (`backend/user_msg_
    lifecycle.py` `emit_failed`; persisted generically by the wildcard
    `event_bus_subscribers._persist_to_event_journal` subscriber — see
    `backend/event_ingester.py`'s `_emit` for the on-disk row shape this
    reads: `{"type": "user_message_failed", "data": {lifecycle_msg_id,
    reason, error}, "msg_id": lifecycle_msg_id, ...}`).

    Reload/replay counterpart to `chat_adapter.ChatSurfaceAdapter.
    _on_user_message_failed`'s live broadcast — both call
    `failure_payload_for_reason` and `user_message_failed_node_id`, so the
    SAME fact always produces the SAME node_id/payload whichever path
    observes it first."""
    lifecycle_msg_id = data.get("lifecycle_msg_id")
    if not isinstance(lifecycle_msg_id, str) or not lifecycle_msg_id:
        lifecycle_msg_id = row.get("msg_id")
    if not isinstance(lifecycle_msg_id, str) or not lifecycle_msg_id:
        return []
    reason = data.get("reason")
    reason = reason if isinstance(reason, str) and reason else "unknown"
    error = data.get("error")
    payload = failure_payload_for_reason(reason, error if isinstance(error, str) else None)
    return [
        node(
            node_id=user_message_failed_node_id(lifecycle_msg_id), kind=NodeKind.FAILURE,
            status=None, payload=payload,
        )
    ]


def _handle_turn_error_meta(row: dict, data: dict, node: partial) -> list[Node]:
    """Journaled `turn_error_meta` BusEvent row
    (`backend.turn_manager._publish_turn_error_meta`, fired from
    `run_turn`'s single error-terminal chokepoint whenever a failed turn
    carries structured `error_meta` — currently only
    `ProviderCredentialError`). Row shape: `{"type": "turn_error_meta",
    "data": {msg_id, error_text, error_meta}, "msg_id":
    assistant_message_id, ...}` — the top-level `row["msg_id"]` is the
    JOURNAL OWNERSHIP key (turn-owning assistant message id); `data
    ["msg_id"]` is the failed prompt's own id, carried for identification
    only (not used for node identity here).

    `error_meta.kind == "provider_credential"` maps to a credential-
    specific FAILURE node (retryable, `FIX_CREDENTIAL` resolution,
    structured `data={provider_id, credential_status}` — never anything
    secret-bearing, matching `ProviderCredentialError.error_meta()`'s
    closed field set). Any other kind falls back to the same
    contract-default shape `failure_payload_for_reason` uses for an
    unmapped reason (severity=error, retryable=False, resolution=none),
    with `code=kind` — so a future/unknown kind still renders instead of
    being dropped, just without a specific recovery action."""
    assistant_msg_id = row.get("msg_id")
    if not isinstance(assistant_msg_id, str) or not assistant_msg_id:
        return []
    error_meta = data.get("error_meta")
    error_meta = error_meta if isinstance(error_meta, dict) else {}
    error_text = data.get("error_text")
    kind = error_meta.get("kind")
    if kind == "provider_credential":
        text = error_text if isinstance(error_text, str) and error_text else (
            "provider credential error"
        )
        payload: FailurePayload = FailurePayload(
            code="provider_credential",
            text=text,
            data={
                "provider_id": error_meta.get("provider_id"),
                "credential_status": error_meta.get("credential_status"),
            },
            severity=FailureSeverity.ERROR,
            retryable=True,
            resolution=FailureResolution.FIX_CREDENTIAL,
        )
    else:
        code = kind if isinstance(kind, str) and kind else "unknown"
        text = error_text if isinstance(error_text, str) and error_text else (
            f"turn failed: {code}"
        )
        payload = FailurePayload(
            code=code, text=text,
            severity=FailureSeverity.ERROR, retryable=False,
            resolution=FailureResolution.NONE,
        )
    return [
        node(
            node_id=turn_error_meta_node_id(assistant_msg_id), kind=NodeKind.FAILURE,
            status=None, payload=payload,
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
    result_text = data.get("result")
    return node(
        node_id=uuid, kind=NodeKind.RESULT, status=None,
        payload=ResultPayload(
            result_kind=ResultKind.PROVIDER,
            text=result_text if isinstance(result_text, str) else None,
            is_error=bool(data.get("is_error")),
        ),
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

    text_content, image_attachments = _split_image_attachments(content)
    text = _content_to_text(text_content if content is not None else inner)
    origin = parse_prompt_origin(data.get("origin"))
    send_mode = parse_send_mode(data.get("send_mode"))
    # Direct typed attachments (not-yet-implemented write path — see
    # surface_commands.send_prompt's "unsupported_attachments" rejection)
    # merged with image blocks split out of the row's own content above,
    # so either source populates the same TypedPromptPayload.attachments.
    attachments = tuple(
        Attachment(
            name=a.get("name", ""), media_type=a.get("media_type", ""), ref=a.get("ref", ""),
            size=a.get("size") if isinstance(a.get("size"), int) else None,
        )
        for a in data.get("attachments", [])
        if isinstance(a, dict)
    ) + image_attachments
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
