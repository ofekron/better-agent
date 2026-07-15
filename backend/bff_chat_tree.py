"""BFF chat-tree read path: rendering cache → formal chat tree.

Serves the formal chat tree (`chat-panel.md` grammar, the shape
`frontend/src/chat/parseProjection.ts` accepts) from the BFF's own
rendering cache: stored canonical facts adapt through
`chat_canonical_adapter` into `chat_projector.project_chat`, and the
result serializes via `chat_tree_wire`.

The runtime stays authoritative: the session snapshot (and provider
identity) comes from `projection-source`, and a root missing from the
cache is marked dirty on the feed client so the next read finds it.
Failures are typed — no stale success, no silent fallbacks.
"""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException

import re

import bff_chat_feed
import chat_projection_ingestion
from bff_runtime_service import RuntimeServiceError, runtime_service
from bff_runtime_upstream import RuntimeUpstreamUnavailable
from chat_canonical_adapter import ChatAdapterError, adapt_chat_inputs
from chat_models import CHAT_SCHEMA_VERSION
from chat_projector import ChatProjectionInputError, project_chat
from chat_projection_service import ProjectionServiceError
from chat_tree_wire import chat_to_wire

router = APIRouter()

_SESSION_ID = re.compile(r"^[A-Za-z0-9_-]+$")
_READ_PAGE = 1000
_PROVIDER_KINDS = {"claude", "codex", "gemini"}


def _window_items(
    items: list[dict[str, Any]], turns: int, before_turn: str | None,
) -> tuple[list[dict[str, Any]], str | None]:
    """Slice the wire item list to the requested turn window.

    Turns count Turn items only; each window keeps the ModelChange items
    that precede its first turn (they render before their affected turn),
    and only the latest window carries trailing ModelChanges (a switch
    whose affected turn does not exist yet). Returns (window,
    older_cursor) where older_cursor is the before_turn for the next
    older page, or None when nothing older exists.
    """
    turn_positions = [i for i, item in enumerate(items) if item["type"] == "Turn"]
    end = len(turn_positions)
    if before_turn is not None:
        cursor_pos = next(
            (k for k, i in enumerate(turn_positions) if items[i]["id"] == before_turn),
            None,
        )
        if cursor_pos is None:
            raise HTTPException(
                status_code=409,
                detail={"code": "stale_turn_cursor",
                        "message": "cursor turn is no longer in the tree"},
            )
        end = cursor_pos
    start = max(0, end - turns)
    if end <= 0 or start >= end:
        return [], None
    first = turn_positions[start]
    while first > 0 and items[first - 1]["type"] == "ModelChange":
        first -= 1
    is_latest = end == len(turn_positions)
    stop = len(items) if is_latest else turn_positions[end - 1] + 1
    older_cursor = items[turn_positions[start]]["id"] if start > 0 else None
    return items[first:stop], older_cursor


def _referenced_ids(items: list[dict[str, Any]]) -> set[str]:
    referenced: set[str] = set()
    stack: list[dict[str, Any]] = list(items)
    while stack:
        item = stack.pop()
        kind = item.get("type")
        if kind == "ModelChange":
            referenced.add(item["id"])
            continue
        if kind in ("Turn", "NativeSubagentTurn", "WorkerTurn"):
            if kind == "Turn":
                referenced.add(item["prompt"])
            else:
                referenced.add(item["id"])
                referenced.update(item.get("children") or [])
            result = item.get("result")
            if result:
                referenced.update(result.get("part_ids") or [])
            stack.extend(item.get("body") or [])
            continue
        if kind == "Explanation":
            referenced.update(item.get("text_event_ids") or [])
            referenced.update(item.get("item_ids") or [])
            continue
        if kind == "SteeringMessage":
            referenced.add(item["id"])
    return referenced


def _build_lookup(
    items: list[dict[str, Any]],
    adapted_messages: tuple[dict[str, Any], ...],
    adapted_events: tuple[dict[str, Any], ...],
    session: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Content sidecar for the ids the window references: the wire tree
    carries structure and ids; renderers look content up here
    (lookupForRender). Seqs come from the runtime session snapshot so
    live WS messages interleave correctly on the client."""
    referenced = _referenced_ids(items)
    snapshot_seq: dict[str, Any] = {}
    run_meta: dict[str, Any] = {}
    for message in session.get("messages") or []:
        if not isinstance(message, dict):
            continue
        message_id = message.get("id")
        if isinstance(message_id, str) and message_id:
            snapshot_seq[message_id] = message.get("seq")
            if isinstance(message.get("run_meta"), dict):
                run_meta[message_id] = message["run_meta"]
    snapshot_by_id: dict[str, dict[str, Any]] = {}
    for message in session.get("messages") or []:
        if isinstance(message, dict) and isinstance(message.get("id"), str):
            snapshot_by_id[message["id"]] = _strip_heavy_message_fields(message)
    lookup: dict[str, dict[str, Any]] = {}
    for message in adapted_messages:
        if message["id"] not in referenced:
            continue
        lookup[message["id"]] = {
            "kind": "message",
            "role": message["role"],
            "text": message["content"],
            "seq": snapshot_seq.get(message["id"], message["seq"]),
            # Runtime snapshot passthrough (minus heavy payloads): liveness,
            # hydration pointers, run_meta — everything the client's
            # ChatMessage needs beyond tree structure.
            "snapshot": snapshot_by_id.get(message["id"]),
        }
    owning_messages: set[str] = set()
    for event in adapted_events:
        if event["event_id"] not in referenced:
            continue
        message_id = event.get("message_id")
        if isinstance(message_id, str) and message_id:
            owning_messages.add(message_id)
        lookup[event["event_id"]] = {
            "kind": "event",
            "type": event["type"],
            "data": event["data"],
            "message_id": message_id,
            "timestamp": event["timestamp"],
            "message_seq": snapshot_seq.get(message_id) if isinstance(message_id, str) else None,
            "run_meta": run_meta.get(message_id) if isinstance(message_id, str) else None,
        }
    # Messages the window's events belong to (assistant rows): the tree
    # references event ids, but the client's message shape needs the
    # owning message's snapshot too.
    for message in adapted_messages:
        if message["id"] in owning_messages and message["id"] not in lookup:
            lookup[message["id"]] = {
                "kind": "message",
                "role": message["role"],
                "text": message["content"],
                "seq": snapshot_seq.get(message["id"], message["seq"]),
                "snapshot": snapshot_by_id.get(message["id"]),
            }
    return lookup


_HEAVY_MESSAGE_FIELDS = frozenset({"events", "workers", "manager"})


def _strip_heavy_message_fields(message: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in message.items() if k not in _HEAVY_MESSAGE_FIELDS}


def _read_stored_facts(root_id: str, provider: str) -> list[dict[str, Any]]:
    service, catalog = chat_projection_ingestion._instances()
    generation = catalog.root_generation(root_id)
    authority = service.register(
        provider=provider, session_id=root_id, root_id=root_id,
        root_generation=generation, store_kind="jsonl",
    )
    facts: list[dict[str, Any]] = []
    after = 0
    while True:
        page = service.read_facts(authority, after=after, limit=_READ_PAGE)
        facts.extend(dict(stored.canonical_fact) for stored in page)
        if len(page) < _READ_PAGE:
            return facts
        after = page[-1].fact_sequence


def _raise_chat_tree_rebuilding(root_id: str) -> None:
    bff_chat_feed.feed_client.mark_dirty(root_id)
    raise HTTPException(
        status_code=503,
        detail={"code": "chat_tree_rebuilding",
                "message": "rendering cache is warming for this session"},
        headers={"Retry-After": "2"},
    )


@router.get("/api/chat-tree/{session_id}")
async def get_chat_tree(
    session_id: str,
    turns: int = 5,
    before_turn: str | None = None,
):
    if not _SESSION_ID.fullmatch(session_id):
        raise HTTPException(status_code=400, detail="invalid session id")
    if not 1 <= turns <= 100:
        raise HTTPException(status_code=400, detail="invalid turns window")
    if before_turn is not None and not _SESSION_ID.fullmatch(before_turn):
        raise HTTPException(status_code=400, detail="invalid turn cursor")
    try:
        source = await runtime_service.session_tree(
            session_id, exchange_count=turns,
        )
    except RuntimeServiceError as exc:
        if exc.status_code == 404:
            raise HTTPException(status_code=404, detail="session not found") from exc
        raise HTTPException(status_code=503, detail="runtime unavailable") from exc
    except RuntimeUpstreamUnavailable as exc:
        raise HTTPException(status_code=503, detail="runtime unavailable") from exc
    session = source.get("tree")
    provider = source.get("provider_kind")
    if not isinstance(session, dict) or provider not in _PROVIDER_KINDS:
        raise HTTPException(
            status_code=422,
            detail={"code": "provider_identity_unresolvable",
                    "message": "session provider identity is unavailable"},
        )
    # The requested id may be a fork; the tree resolves to its root, and
    # the rendering cache keys facts by root.
    root_id = str(session.get("id") or session_id)
    try:
        facts = await asyncio.to_thread(_read_stored_facts, root_id, provider)
    except ProjectionServiceError as exc:
        raise HTTPException(
            status_code=503, detail={"code": exc.code, "message": exc.detail},
        ) from exc
    if not facts:
        try:
            await bff_chat_feed.feed_client.pull_now(root_id)
        except (RuntimeServiceError, RuntimeUpstreamUnavailable) as exc:
            _raise_chat_tree_rebuilding(root_id)
        try:
            facts = await asyncio.to_thread(_read_stored_facts, root_id, provider)
        except ProjectionServiceError as exc:
            raise HTTPException(
                status_code=503, detail={"code": exc.code, "message": exc.detail},
            ) from exc
    if not facts:
        # Cache miss with no source facts yet: keep this typed so the
        # client can show a warming state instead of an empty success.
        _raise_chat_tree_rebuilding(root_id)
    try:
        adapted = await asyncio.to_thread(adapt_chat_inputs, facts, session)
        chat = await asyncio.to_thread(
            project_chat, adapted.messages, adapted.events,
            schema_version=CHAT_SCHEMA_VERSION,
        )
    except (ChatAdapterError, ChatProjectionInputError) as exc:
        raise HTTPException(
            status_code=422, detail={"code": exc.code, "message": str(exc)},
        ) from exc
    all_items = chat_to_wire(chat)
    window, older_cursor = _window_items(all_items, turns, before_turn)
    return {
        "session_id": session_id,
        "schema_version": CHAT_SCHEMA_VERSION,
        # Session metadata for the client's one-initial-request contract;
        # messages travel as tree + lookup, never as a second copy here.
        "session": {k: v for k, v in session.items() if k != "messages"},
        "items": window,
        "lookup": _build_lookup(window, adapted.messages, adapted.events, session),
        "page": {
            "turns": turns,
            "before_turn": before_turn,
            "older_cursor": older_cursor,
            "has_older": older_cursor is not None,
        },
        "dropped": list(adapted.dropped),
    }
