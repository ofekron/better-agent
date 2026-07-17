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
from dataclasses import dataclass
from typing import Any, Mapping

from fastapi import APIRouter, HTTPException

import re

import bff_chat_feed
import bff_chat_lookup
import chat_page_cursor
import chat_projection_ingestion
from bff_chat_render import render_chat
from bff_runtime_service import RuntimeServiceError, runtime_service
from bff_runtime_upstream import RuntimeUpstreamUnavailable
from chat_canonical_adapter import ChatAdapterError
from chat_models import CHAT_SCHEMA_VERSION
from chat_projection_store import StoredFact
from chat_projector import ChatProjectionInputError
from chat_projection_service import ProjectionServiceError

router = APIRouter()

_SESSION_ID = re.compile(r"^[A-Za-z0-9_-]+$")
_READ_PAGE = 1000
_PROVIDER_KINDS = {"claude", "codex", "gemini"}


def _raise_stale_cursor() -> None:
    raise HTTPException(
        status_code=409,
        detail={"code": "stale_turn_cursor",
                "message": "cursor turn is no longer in the tree"},
    )


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


@dataclass(frozen=True)
class _WindowRead:
    facts: tuple[StoredFact, ...]
    cursor_found: bool
    generation: int
    projection_cursor: int


def _read_window_facts(
    root_id: str, provider: str, pane_id: str, turns: int, before_turn: str | None,
) -> _WindowRead:
    """Bounded read: only the facts the requested turn window needs (the
    last-N pane turns plus one extra older turn for has-older detection,
    plus the cursor turn on load-more), resolved server-side by the
    projection store — never the full fact log."""
    service, catalog = chat_projection_ingestion._instances()
    generation = catalog.root_generation(root_id)
    authority = service.register(
        provider=provider, session_id=root_id, root_id=root_id,
        root_generation=generation, store_kind="jsonl",
    )
    facts: list[StoredFact] = []
    after = 0
    while True:
        page = service.read_turn_window(
            authority, pane_id=pane_id, turns=turns, before_turn=before_turn,
            after=after, limit=_READ_PAGE,
        )
        if not page.cursor_found:
            return _WindowRead((), False, generation, page.projection_cursor)
        facts.extend(page.facts)
        if len(page.facts) < _READ_PAGE:
            return _WindowRead(tuple(facts), True, generation, page.projection_cursor)
        after = page.facts[-1].fact_sequence


def _prompt_fact(facts: tuple[StoredFact, ...], turn_id: str) -> StoredFact | None:
    for fact in facts:
        canonical = fact.canonical_fact
        if str(canonical.get("payload_type") or "") != "user_prompt":
            continue
        payload = canonical.get("payload")
        payload = payload if isinstance(payload, Mapping) else {}
        if payload.get("message_id") == turn_id:
            return fact
    return None


def _raise_chat_tree_rebuilding(root_id: str) -> None:
    bff_chat_feed.feed_client.mark_dirty(root_id)
    raise HTTPException(
        status_code=503,
        detail={"code": "chat_tree_rebuilding",
                "message": "rendering cache is warming for this session"},
        headers={"Retry-After": "2"},
    )


def _pane_exists(session: dict[str, Any], pane_id: str) -> bool:
    if str(session.get("id") or "") == pane_id:
        return True
    return any(
        isinstance(fork, dict) and _pane_exists(fork, pane_id)
        for fork in session.get("forks") or []
    )


@router.get("/api/chat-tree/{session_id}")
async def get_chat_tree(
    session_id: str,
    turns: int = 5,
    cursor: str | None = None,
    pane: str | None = None,
):
    if not _SESSION_ID.fullmatch(session_id):
        raise HTTPException(status_code=400, detail="invalid session id")
    if not 1 <= turns <= 100:
        raise HTTPException(status_code=400, detail="invalid turns window")
    if pane is not None and not _SESSION_ID.fullmatch(pane):
        raise HTTPException(status_code=400, detail="invalid pane id")
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
    # the rendering cache keys facts by root. `pane` selects which
    # session-tree node's turns the window covers (default: the root).
    root_id = str(session.get("id") or session_id)
    if pane is not None and not _pane_exists(session, pane):
        raise HTTPException(
            status_code=404,
            detail={"code": "pane_not_found",
                    "message": "pane is not part of this session tree"},
        )
    pane_id = pane or root_id
    decoded_cursor: dict[str, Any] | None = None
    before_turn: str | None = None
    if cursor is not None:
        try:
            decoded_cursor = chat_page_cursor.decode_page_cursor(cursor)
        except chat_page_cursor.PageCursorError:
            _raise_stale_cursor()
        if decoded_cursor["root"] != root_id or decoded_cursor["pane"] != pane_id:
            _raise_stale_cursor()
        before_turn = decoded_cursor["turn"]
    try:
        read = await asyncio.to_thread(
            _read_window_facts, root_id, provider, pane_id, turns, before_turn,
        )
    except ProjectionServiceError as exc:
        raise HTTPException(
            status_code=503, detail={"code": exc.code, "message": exc.detail},
        ) from exc
    if decoded_cursor is not None:
        # Signed-cursor binding: catalog generation, non-regressed
        # projection head, and the window-start turn's prompt-fact anchor
        # must all still hold — anything else means the projection was
        # rebuilt under this cursor.
        if (
            not read.cursor_found
            or decoded_cursor["gen"] != read.generation
            or decoded_cursor["rev"] > read.projection_cursor
        ):
            _raise_stale_cursor()
        anchor = _prompt_fact(read.facts, before_turn)
        if (
            anchor is None
            or anchor.fact_sequence != decoded_cursor["turn_seq"]
            or anchor.content_hash != decoded_cursor["turn_hash"]
        ):
            _raise_stale_cursor()
    if not read.facts and read.projection_cursor == 0:
        try:
            await bff_chat_feed.feed_client.pull_now(root_id)
        except (RuntimeServiceError, RuntimeUpstreamUnavailable) as exc:
            _raise_chat_tree_rebuilding(root_id)
        try:
            read = await asyncio.to_thread(
                _read_window_facts, root_id, provider, pane_id, turns, before_turn,
            )
        except ProjectionServiceError as exc:
            raise HTTPException(
                status_code=503, detail={"code": exc.code, "message": exc.detail},
            ) from exc
        if not read.facts and read.projection_cursor == 0:
            # Cache miss with no source facts yet: keep this typed so the
            # client can show a warming state instead of an empty success.
            _raise_chat_tree_rebuilding(root_id)
    facts = [dict(fact.canonical_fact) for fact in read.facts]
    try:
        rendered = await asyncio.to_thread(
            lambda: render_chat(facts, session, pane_id=pane),
        )
    except (ChatAdapterError, ChatProjectionInputError) as exc:
        raise HTTPException(
            status_code=422, detail={"code": exc.code, "message": str(exc)},
        ) from exc
    window, older_cursor = _window_items(rendered.items, turns, before_turn)
    page_cursor: str | None = None
    if older_cursor is not None:
        older_anchor = _prompt_fact(read.facts, older_cursor)
        if older_anchor is not None:
            page_cursor = chat_page_cursor.encode_page_cursor(
                root_id=root_id, pane_id=pane_id, generation=read.generation,
                revision=read.projection_cursor, turn_id=older_cursor,
                turn_seq=older_anchor.fact_sequence,
                turn_hash=older_anchor.content_hash,
            )
    return {
        "session_id": session_id,
        "schema_version": CHAT_SCHEMA_VERSION,
        # Session metadata for the client's one-initial-request contract;
        # messages travel as tree + lookup, never as a second copy here.
        "session": {k: v for k, v in session.items() if k != "messages"},
        "items": window,
        "lookup": bff_chat_lookup.build_lookup(
            window, rendered.adapted.messages, rendered.adapted.events, session,
        ),
        # The signed page_cursor is the ONLY paging handle — no bare turn
        # ids ride along that could seed a second, unbound cursor source.
        "page": {
            "turns": turns,
            "pane": pane_id,
            "has_older": page_cursor is not None,
            "page_cursor": page_cursor,
        },
        "dropped": list(rendered.adapted.dropped),
    }
