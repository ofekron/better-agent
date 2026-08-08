"""Session detail reads, session CRUD/lifecycle, fork control, selector
updates, turn control, and rewind/retry routes.

The detail-response cache and the messages-replay assembly live here too:
they exist only to serve `GET /api/sessions/{id}` and the WS replay frames
built from the same snapshot. Coordinator access and the few main.py-owned
capabilities (session-tree deletion, project-change broadcast,
model-switch journaling, session image paths) are injected by the
composition root — see `configure`.
"""
from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import time
import uuid
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import FileResponse

import config_store
import runtime_profiles_api
import extension_store
import file_delivery
import file_editor
import harness_profile_resolver
import harness_profile_store
import internal_guards
import perf
import pre_send_advisory
import project_store
import runtime_profile
import session_list_cache
import session_listing_api
import session_migrate
import session_search
import session_store
import synthetic_messages
import ui_selection_projection
import virtual_session_store
from capability_contexts import normalize_capability_contexts
from event_bus import BusEvent, bus as event_bus
from event_ingester import event_ingester
from event_shape import (
    has_assistant_text,
    project_content_snapshot,
    strip_synthetic_events,
)
from harness_profiles_api import harness_profile_selection as _harness_profile_selection
from hot_path_executor import hot_path, session_detail_path
from i18n import t
from paths import ba_home
from provider_validation import (
    api_permission as _api_permission,
    api_reasoning_effort as _api_reasoning_effort,
    provider_for_required_model as _provider_for_required_model,
    provider_not_suspended as _provider_not_suspended,
    provider_permission as _provider_permission,
    provider_reasoning_effort as _provider_reasoning_effort,
    provider_runner as _provider_runner,
    required_model_from_body_or_provider as _required_model_from_body_or_provider,
    validate_provider_model as _validate_provider_model,
)

from session_helpers import session_exists as _session_exists, session_lite as _session_lite
from session_manager import manager as session_manager, strip_link_marker_syntax
from session_store import _session_path
from user_msg_lifecycle import new_lifecycle_msg_id

router = APIRouter()
logger = logging.getLogger(__name__)


_coordinator_ref: Any = None


def _coordinator() -> Any:
    if _coordinator_ref is None:
        raise HTTPException(status_code=503, detail="session detail API is not configured")
    return _coordinator_ref


_delete_session_tree: Optional[Callable[[str], Awaitable[bool]]] = None
_broadcast_projects_changed: Optional[Callable[[], Awaitable[None]]] = None
_record_model_switched_event: Optional[Callable[[str, dict, dict, dict], None]] = None


def configure(
    *,
    coordinator: Any,
    delete_session_tree: Callable[[str], Awaitable[bool]],
    notify_projects_changed: Callable[[], Awaitable[None]],
    record_model_switched_event: Callable[[str, dict, dict, dict], None],
) -> None:
    """Bind the collaborators this router needs."""
    global _coordinator_ref, _delete_session_tree, _broadcast_projects_changed
    global _record_model_switched_event
    _coordinator_ref = coordinator
    _delete_session_tree = delete_session_tree
    _broadcast_projects_changed = notify_projects_changed
    _record_model_switched_event = record_model_switched_event


def resolve_session_image_path(session_id: str, filename: str) -> Path:
    """Confine an image read to `<ba_home>/sessions/images/<session_id>/`;
    any traversal out of it is a 404, not a read."""
    image_root = (ba_home() / "sessions" / "images").resolve()
    session_dir = (image_root / session_id).resolve()
    img_path = (session_dir / filename).resolve()
    try:
        img_path.relative_to(session_dir)
        session_dir.relative_to(image_root)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=t("error.image_not_found")) from exc
    return img_path


_session_detail_response_cache: collections.OrderedDict[tuple, bytes] = (
    collections.OrderedDict()
)
_session_detail_response_cache_latest: dict[tuple[str, int, Optional[int]], tuple] = {}
_SESSION_DETAIL_RESPONSE_CACHE_MAX = 64


def _session_detail_watermarks(
    root_id: str,
    has_events: bool,
    barrier_seq: int,
    max_context: dict[str, int],
) -> dict[str, int]:
    if max_context:
        return dict(max_context)
    if has_events and barrier_seq > 0:
        return {root_id: barrier_seq}
    return {}


def _session_detail_cache_get(key: tuple) -> bytes | None:
    content = _session_detail_response_cache.get(key)
    if content is None:
        return None
    _session_detail_response_cache.move_to_end(key)
    return content


async def _session_detail_cache_put_async(key: tuple, value: dict) -> bytes:
    content_text = await asyncio.to_thread(
        json.dumps,
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )
    while len(_session_detail_response_cache) >= _SESSION_DETAIL_RESPONSE_CACHE_MAX:
        old_key, _ = _session_detail_response_cache.popitem(last=False)
        old_simple = _session_detail_simple_cache_key_from_full(old_key)
        if (
            old_simple is not None
            and _session_detail_response_cache_latest.get(old_simple) == old_key
        ):
            _session_detail_response_cache_latest.pop(old_simple, None)
    content = content_text.encode("utf-8")
    _session_detail_response_cache[key] = content
    simple_key = _session_detail_simple_cache_key_from_full(key)
    if simple_key is not None:
        _session_detail_response_cache_latest[simple_key] = key
    return content


def _session_detail_simple_cache_key_from_full(
    key: tuple,
) -> tuple[str, int, Optional[int]] | None:
    if (
        len(key) >= 2
        and isinstance(key[0], str)
        and isinstance(key[1], tuple)
        and len(key[1]) >= 3
        and isinstance(key[1][1], int)
        and (key[1][2] is None or isinstance(key[1][2], int))
    ):
        return (key[0], key[1][1], key[1][2])
    return None


def _tree_has_loaded_events(tree: dict) -> bool:
    stack = [tree]
    while stack:
        node = stack.pop()
        for m in node.get("messages") or []:
            events = m.get("events")
            if isinstance(events, list) and events:
                return True
            for worker in m.get("workers") or []:
                if not isinstance(worker, dict):
                    continue
                events = worker.get("events")
                if isinstance(events, list) and events:
                    return True
        stack.extend(node.get("forks", []) or [])
    return False


def _strip_synthetic_events_from_tree(tree: dict) -> None:
    """Walk every node in the tree and strip SDK continuation markers
    (model="<synthetic>", "No response requested.") from both
    `msg.events` and `msg.content`. Pure data hygiene — runs on every
    REST snapshot.

    (Was bundled with the zombie-streaming reaper until the streaming-
    source-of-truth refactor moved `isStreaming` ownership onto runner
    registration — runner add/remove now drives the flag via the hook
    in `_coordinator().turn_manager.run_state_add` / `_run_state_set_target` /
    `run_state_remove`, eliminating both the start-path race and the
    in-recovery false-positive that the gated reaper compensated for.)
    """

    def _visit(node: dict) -> None:
        sid = node.get("id")
        for m in node.get("messages", []):
            if m.get("role") != "assistant":
                continue
            events = m.get("events")
            if isinstance(events, list) and events:
                cleaned = strip_synthetic_events(events)
                if len(cleaned) != len(events):
                    session_manager.set_native_events(sid, m["id"], cleaned)
                    m["events"] = cleaned
                    # Invalidate uid_idx — direct list assignment bypassed
                    # the mutator's invalidation (same-length-different-
                    # uuids would survive the lazy len check in
                    # `orchs.base._uid_idx_for`). Pop forces a rebuild on
                    # the next apply_event.
                    m.pop("_uid_idx", None)
                    # `_has_final` has the same validity lifetime — the
                    # stripped synthetic events could have been the ones
                    # carrying `final_answer=True`; invalidate so the
                    # next read rescans instead of trusting a stale flag.
                    m.pop("_has_final", None)
                    # Re-extract content without synthetic text. Semantics:
                    # a message left with NO assistant text blanks (it was
                    # synthetic-only); a message whose remaining real events
                    # merely end on a tool/thinking boundary keeps its
                    # current snapshot (project_content_snapshot guard).
                    if has_assistant_text(cleaned):
                        new_content = project_content_snapshot(
                            cleaned, m.get("content"),
                        )
                    else:
                        new_content = ""
                    if new_content != (m.get("content") or ""):
                        session_manager.update_running_content(
                            sid, m["id"], new_content,
                        )
                        m["content"] = new_content
        for f in node.get("forks", []):
            _visit(f)

    _visit(tree)


def _strip_synthetic_events_from_message(msg: dict) -> None:
    """Pure per-message synthetic-event strip for the lazy-expand fetch.
    Operates on a DEEPCOPY returned by `get_message_full` — mutates only
    the copy (never live state, unlike `_strip_synthetic_events_from_tree`)
    and covers every events list (native, manager, worker panels)."""
    for owner in (msg, *(msg.get("workers") or [])):
        if not isinstance(owner, dict):
            continue
        events = owner.get("events")
        if isinstance(events, list) and events:
            owner["events"] = strip_synthetic_events(events)


def _session_detail_snapshot_sync(
    session_id: str,
    *,
    msg_limit: int,
    exchange_count: Optional[int],
    include_cache_key: bool = False,
) -> Optional[dict]:
    root_id_start = time.perf_counter()
    root_id = session_manager._root_id_for(session_id)
    root_id_ms = (time.perf_counter() - root_id_start) * 1000
    perf.record("sessions.detail.root_id", root_id_ms)
    barrier_seq = 0
    get_start = time.perf_counter()
    max_seq_ms = 0.0
    tree_ms = 0.0
    strip_ms = 0.0
    max_context_ms = 0.0
    has_events = False
    if isinstance(root_id, str):
        max_seq_start = time.perf_counter()
        has_events, barrier_seq, max_context = session_list_cache._session_event_meta(root_id)
        max_seq_ms = (time.perf_counter() - max_seq_start) * 1000
        perf.record("sessions.detail.event_meta", max_seq_ms)
    else:
        max_context = {}
    tree_start = time.perf_counter()
    detail_cache_key = None
    if include_cache_key:
        built = session_manager.get_root_tree_stubbed_with_cache_key(
            session_id,
            msg_limit=msg_limit,
            exchange_count=exchange_count,
            known_root_id=root_id if isinstance(root_id, str) else None,
        )
        if built is None:
            tree = None
        else:
            tree, tree_key = built
            detail_cache_key = (session_id, tree_key)
    else:
        tree = session_manager.get_root_tree_stubbed(
            session_id, msg_limit=msg_limit, exchange_count=exchange_count,
        )
    tree_ms = (time.perf_counter() - tree_start) * 1000
    perf.record("sessions.detail.tree", tree_ms)
    if not tree:
        return None

    strip_start = time.perf_counter()
    if _tree_has_loaded_events(tree):
        _strip_synthetic_events_from_tree(tree)
    strip_ms = (time.perf_counter() - strip_start) * 1000
    perf.record("sessions.detail.strip_synthetic", strip_ms)
    root_id = tree.get("id")
    if isinstance(root_id, str):
        if has_events:
            max_context_start = time.perf_counter()
            tree["max_seq_by_sid"] = _session_detail_watermarks(
                root_id, has_events, barrier_seq, max_context,
            )
            max_context_ms = (time.perf_counter() - max_context_start) * 1000
            perf.record("sessions.detail.max_context_copy", max_context_ms)
        else:
            tree["max_seq_by_sid"] = {}
        total_ms = (time.perf_counter() - get_start) * 1000
        perf.record("sessions.detail.total", total_ms)
        if total_ms >= 50 or root_id_ms >= 20 or max_context_ms >= 20 or strip_ms >= 20:
            logger.info(
                "GET session %s timings total=%.1fms root_id=%.1fms max_context=%.1fms strip=%.1fms has_events=%s",
                root_id[:8], total_ms, root_id_ms, max_context_ms, strip_ms, has_events,
            )
        file_path_start = time.perf_counter()
        tree["file_path"] = str(_session_path(root_id))
        perf.record("sessions.detail.file_path", (time.perf_counter() - file_path_start) * 1000)
    if detail_cache_key is not None:
        cache_marker_start = time.perf_counter()
        tree["_detail_response_cache_key_parts"] = (
            detail_cache_key[0],
            detail_cache_key[1],
            has_events,
            tuple(sorted(_session_detail_watermarks(
                root_id,
                has_events,
                barrier_seq,
                max_context,
            ).items())),
        )
        perf.record("sessions.detail.cache_marker", (time.perf_counter() - cache_marker_start) * 1000)
    return tree


def _session_detail_response_cache_key_sync(
    session_id: str,
    *,
    msg_limit: int,
    exchange_count: Optional[int],
    known_root_id: str | None = None,
) -> tuple | None:
    root_id = known_root_id or session_manager._root_id_for(session_id)
    if not isinstance(root_id, str):
        return None
    has_events, barrier_seq, max_context = session_list_cache._session_event_meta(root_id)
    watermarks = _session_detail_watermarks(
        root_id, has_events, barrier_seq, max_context,
    )
    if known_root_id:
        tree_key = session_manager.root_tree_stub_cache_key_for_root(
            root_id,
            msg_limit=msg_limit,
            exchange_count=exchange_count,
        )
    else:
        tree_key = session_manager.root_tree_stub_cache_key(
            session_id,
            msg_limit=msg_limit,
            exchange_count=exchange_count,
        )
    if tree_key is None:
        return None
    return (
        session_id,
        tree_key,
        has_events,
        tuple(sorted(watermarks.items())),
    )


def _session_detail_cached_key_still_current(
    key: tuple,
    session_id: str,
    *,
    msg_limit: int,
    exchange_count: Optional[int],
) -> bool:
    if len(key) != 4 or key[0] != session_id:
        return False
    cached_tree_key = key[1]
    root_id = (
        cached_tree_key[0]
        if isinstance(cached_tree_key, tuple)
        and cached_tree_key
        and isinstance(cached_tree_key[0], str)
        else None
    )
    if not isinstance(root_id, str):
        return False
    tree_key = session_manager.root_tree_stub_cache_key_for_root(
        root_id,
        msg_limit=msg_limit,
        exchange_count=exchange_count,
    )
    if tree_key is None or key[1] != tree_key:
        return False
    has_events, barrier_seq, max_context = session_list_cache._session_event_meta(root_id)
    watermarks = _session_detail_watermarks(
        root_id, has_events, barrier_seq, max_context,
    )
    return key[2] == has_events and key[3] == tuple(sorted(watermarks.items()))


def _floor_events_from_seq(
    app_session_id: str,
    requested_from_seq: int,
    *,
    cursor_known: bool,
) -> int:
    if cursor_known:
        return max(0, requested_from_seq)
    root_id = session_manager._root_id_for(app_session_id)
    if not isinstance(root_id, str):
        return 0
    # Render-projection head, not the raw journal head: flooring to the
    # raw head would push the resume cursor past the rendered tail and
    # drop a still-streaming turn (same defect as the REST watermark).
    floor = event_ingester.render_seq_for_sid(root_id, app_session_id)
    return max(0, floor)


def _total_replay_events(msg: dict) -> int:
    n = len(msg.get("events") or [])
    for w in msg.get("workers") or []:
        n += len(w.get("events") or [])
    return n


def _merge_in_flight_replay(
    replay_msgs: list[dict],
    in_flight: Optional[dict],
    *,
    app_session_id: str,
) -> list[dict]:
    if in_flight is None:
        return replay_msgs
    in_flight_id = in_flight.get("id")
    in_flight_evts = _total_replay_events(in_flight)
    new_replay = []
    for m in replay_msgs:
        if m.get("id") != in_flight_id:
            new_replay.append(m)
            continue
        cache_evts = _total_replay_events(m)
        if in_flight_evts >= cache_evts:
            new_replay.append(in_flight)
            continue
        merged = dict(m)
        merged["isStreaming"] = in_flight.get("isStreaming", True)
        logger.info(
            "WS replay %s: cache over in-flight (%d>%d evts), stamping isStreaming",
            app_session_id[:8], cache_evts, in_flight_evts,
        )
        new_replay.append(merged)
    return new_replay


def _build_messages_replay_delta(
    app_session_id: str,
    since_seq: int,
    *,
    limit: int,
    get_messages_since=session_manager.get_messages_since,
    get_in_flight=None,
) -> Optional[dict]:
    if get_in_flight is None:
        get_in_flight = _coordinator().turn_manager.get_in_flight_assistant_msg
    initial_in_flight = get_in_flight(app_session_id)
    used_exclusive = since_seq > 0 and initial_in_flight is None
    effective_since_seq = since_seq + 1 if used_exclusive else since_seq
    delta = get_messages_since(app_session_id, effective_since_seq, limit=limit)
    if delta is None:
        return None
    final_in_flight = get_in_flight(app_session_id)
    if used_exclusive and final_in_flight is not None:
        delta = get_messages_since(app_session_id, since_seq, limit=limit)
        if delta is None:
            return None
    replay_msgs = _merge_in_flight_replay(
        delta["messages"],
        final_in_flight,
        app_session_id=app_session_id,
    )
    return {
        "messages": replay_msgs,
        "next_seq": delta["next_seq"],
        "in_flight": final_in_flight,
    }


@router.get("/api/sessions/{session_id}/stats")


async def get_session_stats(session_id: str):
    if session_id == session_search.ASK_SINGLETON_ID:
        session = await session_search.ensure_ask_session()
    else:
        session = await asyncio.to_thread(virtual_session_store.get, session_id)
    if not session:
        session = await asyncio.to_thread(session_manager.get, session_id)
    if not session:
        raise HTTPException(status_code=404, detail=t("error.session_not_found"))
    return session_listing_api.sidebar_stats_payload(session)


@router.get("/api/communications")


async def get_communications(
    session_id: str | None = Query(None),
    limit: int = Query(default=200, ge=1, le=500),
):
    import communication_log

    return await asyncio.to_thread(
        communication_log.list_communications,
        session_id=session_id or "",
        limit=limit,
    )


@router.post("/api/chats/{chat_id}/messages")


async def post_chat_message(chat_id: str, body: dict = Body(default={})):
    import chat_store

    sender_session_id = str(body.get("sender_session_id") or "").strip()
    message = str(body.get("message") or "").strip()
    if not sender_session_id:
        raise HTTPException(status_code=400, detail="sender_session_id is required")
    if not message:
        raise HTTPException(status_code=400, detail="message is required")

    session = await asyncio.to_thread(session_manager.get, sender_session_id)
    if not session:
        raise HTTPException(status_code=404, detail=t("error.session_not_found"))

    try:
        return await asyncio.to_thread(
            chat_store.post_and_read,
            chat_id=chat_id,
            reader_id=sender_session_id,
            message=message,
        )
    except chat_store.ChatStoreError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/sessions/{session_id}")


async def get_session(
    request: Request,
    session_id: str,
    msg_limit: int = Query(default=50, ge=1, le=200),
    exchange_count: Optional[int] = Query(default=None, ge=1, le=100),
):
    """Return the root tree containing `session_id`, with messages
    paginated to the latest `msg_limit` per node (or `exchange_count`
    user→assistant exchanges when provided).

    `session_id` may be a root or any fork inside one — both resolve
    to the same root tree. The frontend uses this to load every pane
    of a split-fork view in one call (root + embedded forks all carry
    their own `messages`, `claude_session_id`s, drafts, etc., so the
    UI has all the data it needs to render every pane). For backward
    compat the response also exposes the requested `id` in `id` so
    callers that expect "the session they asked for" still work — but
    the meaningful content is at the root level.

    Each node carries a ``pagination`` dict: ``{total_messages,
    oldest_loaded_seq, has_older}``. Older messages can be loaded via
    ``GET /api/sessions/{id}/messages?before_seq=N``."""
    if session_id == session_search.ASK_SINGLETON_ID:
        virtual = await session_search.ensure_ask_session()
    else:
        virtual = await asyncio.to_thread(virtual_session_store.get, session_id)
    if virtual:
        return virtual

    simple_cache_key = (session_id, msg_limit, exchange_count)
    cached_full_key = _session_detail_response_cache_latest.get(simple_cache_key)
    cache_key = None
    if cached_full_key is not None:
        still_current = await asyncio.to_thread(
            _session_detail_cached_key_still_current,
            cached_full_key,
            session_id,
            msg_limit=msg_limit,
            exchange_count=exchange_count,
        )
        if still_current:
            cache_key = cached_full_key
        else:
            _session_detail_response_cache_latest.pop(simple_cache_key, None)
    accept_encoding = request.headers.get("accept-encoding", "")
    if cache_key is not None:
        cached = _session_detail_cache_get(cache_key)
        if cached is not None:
            perf.record("sessions.detail.response_cache.hit", 1.0)
            return await session_list_cache.gzip_response_off_loop(
                cached,
                accept_encoding,
                executor=session_detail_path,
                perf_prefix="sessions.detail",
            )
    perf.record("sessions.detail.response_cache.miss", 1.0)

    worker_start = time.perf_counter()
    tree = await session_detail_path.run(
        "sessions.detail.worker",
        _session_detail_snapshot_sync,
        session_id,
        msg_limit=msg_limit,
        exchange_count=exchange_count,
        include_cache_key=True,
    )
    perf.record("sessions.detail.worker.total", (time.perf_counter() - worker_start) * 1000)
    if not tree:
        raise HTTPException(status_code=404, detail=t("error.session_not_found"))
    if cache_key is None:
        cache_key_parts = tree.pop("_detail_response_cache_key_parts", None)
        if isinstance(cache_key_parts, tuple) and len(cache_key_parts) == 4:
            cache_key = cache_key_parts
    else:
        tree.pop("_detail_response_cache_key_parts", None)
    if cache_key is None:
        return await session_list_cache.json_response_off_loop(
            tree,
            accept_encoding,
            executor=session_detail_path,
            perf_prefix="sessions.detail",
        )
    content = await _session_detail_cache_put_async(cache_key, tree)
    return await session_list_cache.gzip_response_off_loop(
        content,
        accept_encoding,
        executor=session_detail_path,
        perf_prefix="sessions.detail",
    )


@router.get("/api/sessions/{session_id}/messages")


async def get_older_messages(
    session_id: str,
    before_seq: int = Query(...),
    limit: int = Query(default=50, ge=1, le=200),
    exchange_count: Optional[int] = Query(default=None, ge=1, le=100),
):
    """Load messages older than ``before_seq`` for a session node.
    When ``exchange_count`` is provided, pages by user→assistant exchanges.
    Returns ``{messages, has_older, oldest_loaded_seq, total_messages}``."""
    virtual = await asyncio.to_thread(virtual_session_store.get, session_id)
    if virtual:
        return {
            "messages": [],
            "has_older": False,
            "oldest_loaded_seq": None,
            "total_messages": len(virtual.get("messages") or []),
        }
    result = await asyncio.to_thread(
        session_manager.get_messages_before,
        session_id, before_seq, limit, exchange_count=exchange_count,
    )
    if result is None:
        raise HTTPException(status_code=404, detail=t("error.session_not_found"))
    return result


@router.get("/api/sessions/{session_id}/messages/{message_id}/events")


async def get_message_events(request: Request, session_id: str, message_id: str):
    """Lazy-expand fetch: return ONE message with its FULL events lists
    (Tier-1 lazy event fetch). The heavy load paths ship completed,
    non-latest assistant messages as stubs (`msg.stub`, empty events);
    the frontend calls this when the user expands such a turn.

    Reads the in-memory render tree (single source of truth), so
    `stub.event_count` matches the primary-stream renderable count of
    the returned message."""
    msg = await asyncio.to_thread(
        session_manager.get_message_full, session_id, message_id,
    )
    if msg is None:
        raise HTTPException(status_code=404, detail=t("error.session_not_found"))
    _strip_synthetic_events_from_message(msg)
    return await session_list_cache.json_response_off_loop(
        msg,
        request.headers.get("accept-encoding", ""),
        executor=session_detail_path,
        perf_prefix="sessions.detail",
    )


def _resolve_session_node_id(body: dict) -> str:
    """Validate the optional `node_id` on a session-create request.

    INVARIANT: `"primary"` is always accepted without touching
    topology.yaml so single-machine deploys (no topology configured)
    keep working. Any other id MUST appear in topology AND, if the
    spec declares `cwd_roots`, the requested `cwd` MUST sit under one
    of them — otherwise the worker would crash at delegation time on
    a directory that doesn't exist on the remote node's filesystem.

    file_editing mode now runs on any node — the file_editor pipeline
    routes baseline reads through `node_link.rpc_call` when the
    session targets a non-local node (see file_editor.start).
    """
    node_id = body.get("node_id") or "primary"
    if node_id == "primary":
        return node_id
    from topology import TopologyError, load_topology
    spec = None
    try:
        spec = load_topology().get(node_id)
    except (TopologyError, KeyError):
        pass
    if spec is None:
        from node_registry_store import to_spec as registry_to_spec
        spec = registry_to_spec(node_id)
    if spec is None:
        raise HTTPException(
            status_code=400,
            detail=f"node_id {node_id!r} is unknown — not in topology.yaml or approved nodes",
        )
    cwd = body.get("cwd") or ""
    if spec.cwd_roots:
        from node_path_authority import NodePathError, path_is_within_roots
        try:
            allowed = path_is_within_roots(cwd, spec.cwd_roots)
        except NodePathError:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"cwd {cwd!r} must be an absolute path when targeting "
                    f"node {node_id!r}"
                ),
            ) from None
        if not allowed:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"cwd {cwd!r} is not under any of node {node_id!r}'s "
                    f"cwd_roots: {list(spec.cwd_roots)}"
                ),
            )
    return node_id


_node_cwd_verified: set[tuple[str, str]] = set()


async def _node_cwd_error(node_id: str, cwd: str) -> Optional[str]:
    """Reject a cwd the node cannot actually use, before the turn starts.

    A session created for a node inherits the primary's cwd unless one is
    given, and nothing else notices until the runner spawns there — on a
    Windows node under a POSIX primary that surfaces as a bare
    `NotADirectoryError: [WinError 267]` mid-turn. Ask the node instead;
    it is the only authority on its own filesystem.

    Cached per (node, cwd): a directory that resolved once does not need
    re-checking on every send, and a transport failure is left to the
    offline/version checks rather than blocking the user here.
    """
    if not cwd:
        return None
    key = (node_id, cwd)
    if key in _node_cwd_verified:
        return None
    from node_rpc_handlers import call_local_or_remote

    try:
        await call_local_or_remote(node_id, "list_dir", {"path": cwd}, timeout=20)
    except Exception as exc:
        text = str(exc)
        if isinstance(exc, FileNotFoundError) or "not a directory" in text:
            return (
                f"cwd {cwd!r} does not exist on node {node_id!r}, which is where "
                f"this session runs. Set the session's directory to a path on "
                f"that machine and resend."
            )
        return None
    _node_cwd_verified.add(key)
    return None


async def _node_offline_error(session: Optional[dict]) -> Optional[str]:
    """Fail-closed gate for sends into a node-hosted session: a prompt
    accepted while the node is down would sit queued and error minutes
    later — reject upfront with a clear message instead."""
    node_id = (session or {}).get("node_id") or "primary"
    if node_id == "primary":
        return None
    nodes_not_ready = extension_store.runtime_not_ready_message(
        extension_store.extension_id_for_role('machine-nodes')
    )
    if nodes_not_ready is not None:
        return nodes_not_ready
    try:
        from topology import local_node_id
        if node_id == local_node_id():
            return None
    except Exception:
        pass
    import node_store
    conn = node_store.get_connection(node_id)
    if conn is None:
        return (
            f"node {node_id!r} is offline — this session runs there. "
            f"Reconnect the node and resend."
        )
    if node_store.connection_version_blocks_work(conn):
        primary_sha = node_store.app_version.current_commit_sha()
        node_sha = conn.app_commit_sha or "unknown"
        return (
            f"node {node_id!r} is running app commit {node_sha}, "
            f"but primary is running {primary_sha}. Update or restart the node "
            f"onto the primary version and resend."
        )
    return await _node_cwd_error(node_id, str((session or {}).get("cwd") or ""))


@router.post("/api/sessions")


async def create_session(body: Any = Body(default=None)):
    if body is None:
        body = {}
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="request body must be an object")
    client_session_id = body.get("client_session_id")
    if client_session_id is not None:
        if not isinstance(client_session_id, str):
            logger.warning("create_session rejected: client_session_id not a string type=%s", type(client_session_id).__name__)
            raise HTTPException(status_code=400, detail="client_session_id must be a UUID string")
        try:
            parsed_client_session_id = uuid.UUID(client_session_id)
        except ValueError:
            logger.warning("create_session rejected: client_session_id not a valid UUID value=%r", client_session_id[:80])
            raise HTTPException(status_code=400, detail="client_session_id must be a valid UUID")
        if str(parsed_client_session_id) != client_session_id:
            logger.warning("create_session rejected: client_session_id not canonical value=%r", client_session_id[:80])
            raise HTTPException(status_code=400, detail="client_session_id must be a canonical UUID")
        existing = await asyncio.to_thread(session_manager.get, client_session_id)
        if existing is not None:
            return existing

    requested_source = body.get("source", "web")
    if requested_source not in ("web", "cli"):
        requested_source = "web"
    worker_creation_policy = str(body.get("worker_creation_policy") or "ask")
    if worker_creation_policy not in ("ask", "approve", "deny"):
        raise HTTPException(status_code=400, detail="worker_creation_policy must be ask, approve, or deny")
    requested_orchestration_mode = str(body.get("orchestration_mode") or "").strip()
    if requested_orchestration_mode == "manager":
        requested_orchestration_mode = "team"
    if not requested_orchestration_mode:
        requested_orchestration_mode = (
            "team"
            if extension_store.is_extension_runtime_ready(
                extension_store.extension_id_for_role('team-orchestration')
            )
            else "native"
        )
    if requested_orchestration_mode == "team":
        team_not_ready = extension_store.runtime_not_ready_message(
            extension_store.extension_id_for_role('team-orchestration')
        )
        if team_not_ready is not None:
            raise HTTPException(status_code=404, detail=team_not_ready)
    bare_config = body.get("bare_config", False)
    if not isinstance(bare_config, bool):
        raise HTTPException(status_code=400, detail="bare_config must be a boolean")
    try:
        capability_contexts = normalize_capability_contexts(body.get("capability_contexts"))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    requested_profile_id = body.get("runtime_profile_id")
    if requested_profile_id is not None:
        if not isinstance(requested_profile_id, str) or not requested_profile_id.strip():
            raise HTTPException(
                status_code=400, detail="runtime_profile_id must be a string"
            )
        runtime_profile_record = await asyncio.to_thread(
            config_store.get_runtime_profile, requested_profile_id
        )
        if runtime_profile_record is None or runtime_profile_record.get("deleted_at"):
            raise HTTPException(
                status_code=400, detail="runtime profile is unknown or deleted"
            )
        if (
            body.get("provider_id")
            and body["provider_id"] != runtime_profile_record["provider_id"]
        ):
            raise HTTPException(
                status_code=400,
                detail="provider_id conflicts with runtime_profile_id",
            )
        if body.get("runner") and body["runner"] != runtime_profile_record["runner"]:
            raise HTTPException(
                status_code=400, detail="runner conflicts with runtime_profile_id"
            )
        # The chosen profile is an explicit selection: its provider+runner
        # win over harness pins; model/effort stay overridable below.
        body = {
            **body,
            "provider_id": runtime_profile_record["provider_id"],
            "runner": runtime_profile_record["runner"],
        }
    harness_profile_id = _harness_profile_selection(body)
    # A harness profile may pin provider/model/reasoning-effort defaults used
    # ONLY for whichever the caller omitted; explicit body values always win.
    profile_selectors = harness_profile_resolver.merge_selector_defaults(
        {
            "provider_id": body.get("provider_id") or None,
            "model": str(body.get("model") or "").strip() or None,
            "reasoning_effort": body.get("reasoning_effort"),
        },
        harness_profile_id,
    )
    if (
        requested_profile_id is None
        and profile_selectors.get("runtime_profile_id")
        and not body.get("provider_id")
        and not body.get("runner")
    ):
        # Harness pin: behaves like selecting that profile when the caller
        # pinned nothing themselves.
        requested_profile_id = profile_selectors["runtime_profile_id"]
        pinned = await asyncio.to_thread(
            config_store.get_runtime_profile, requested_profile_id
        )
        if pinned is not None:
            body = {**body, "runner": pinned["runner"]}
    requested_provider_id = profile_selectors["provider_id"]
    provider_record = await asyncio.to_thread(
        _provider_for_required_model,
        requested_provider_id,
    )
    model = _required_model_from_body_or_provider(
        {**body, "model": profile_selectors["model"] or ""}, provider_record
    )
    requested_runner = _provider_runner(
        requested_provider_id, body.get("runner"),
    )
    requested_effort = await asyncio.to_thread(
        _provider_reasoning_effort,
        requested_provider_id,
        _api_reasoning_effort(profile_selectors["reasoning_effort"]),
        requested_runner,
        model,
    )
    requested_permission = await asyncio.to_thread(
        _provider_permission,
        requested_provider_id,
        _api_permission(body.get("permission")),
        requested_runner,
    )
    node_id = _resolve_session_node_id(body)
    requested_folder_id = body.get("folder_id")
    if requested_folder_id is not None and not isinstance(requested_folder_id, str):
        raise HTTPException(status_code=400, detail="folder_id must be a string")
    if isinstance(requested_folder_id, str):
        requested_folder_id = requested_folder_id.strip() or None
    file_edit_path = body.get("file_edit_path")
    raw_file_edit_enabled = body.get("file_edit_enabled", False)
    if not isinstance(raw_file_edit_enabled, bool):
        raise HTTPException(status_code=400, detail="file_edit_enabled must be a boolean")
    file_edit_enabled = raw_file_edit_enabled or bool(file_edit_path)
    # File Edit and Browser Harness are folded into the harness-profile
    # extension_instances view (Default synthesis + any selected profile's
    # overrides) rather than standalone request params.
    try:
        _harness_snapshot = await asyncio.to_thread(
            harness_profile_resolver.resolve_profile,
            harness_profile_id or harness_profile_store.DEFAULT_PROFILE_ID,
        )
    except harness_profile_resolver.HarnessProfileResolutionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _harness_instances = _harness_snapshot.get("extension_instances") or {}
    _harness_disabled_extensions = set(
        _harness_snapshot.get("disabled_builtin_extensions", {}).get("resolved") or []
    )
    _file_edit_gate_open = (
        extension_store.BUILTIN_FILE_EDIT_EXTENSION_ID in _harness_instances
        and extension_store.BUILTIN_FILE_EDIT_EXTENSION_ID not in _harness_disabled_extensions
    )
    if file_edit_enabled and not _file_edit_gate_open:
        raise HTTPException(status_code=403, detail="File Edit is disabled for this harness")
    if file_edit_enabled:
        try:
            if file_edit_path:
                result = await file_editor.start(
                    file_edit_path,
                    cwd=body.get("cwd", ""),
                    model=model,
                    provider_id=requested_provider_id,
                    runner=requested_runner,
                    reasoning_effort=requested_effort,
                    persistent=True,
                    node_id=node_id,
                    session_id=client_session_id,
                )
            else:
                result = await file_editor.start_empty(
                    cwd=body.get("cwd", ""),
                    model=model,
                    provider_id=requested_provider_id,
                    runner=requested_runner,
                    reasoning_effort=requested_effort,
                    persistent=True,
                    node_id=node_id,
                    session_id=client_session_id,
                )
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        session = await _session_lite(result["session_id"]) or {}
        if capability_contexts:
            session = await asyncio.to_thread(
                session_manager.set_capability_contexts,
                result["session_id"], capability_contexts,
            ) or session
        await asyncio.to_thread(config_store.apply_env_vars)
        if result.get("user_ask") is not None:
            await synthetic_messages.append_assistant_message(
                result["session_id"],
                content=result["user_ask"],
                source="file_editor",
            )
            session = await _session_lite(result["session_id"]) or session
        elif result.get("meta_prompt") is not None:
            await synthetic_messages.inject(
                _coordinator(),
                result["session_id"],
                prompt=result["meta_prompt"],
                model=session.get("model") or "",
                cwd=session.get("cwd") or "",
                orchestration_mode=session.get("orchestration_mode") or "native",
                source="file_editor",
                capability_contexts=capability_contexts,
            )
        if session_store.should_auto_register_project(session):
            try:
                await asyncio.to_thread(
                    project_store.add_project,
                    session["cwd"],
                    node_id=session.get("node_id") or "primary",
                )
                await _broadcast_projects_changed()
            except Exception as e:
                logger.warning("auto add_project failed: %s", e)
        logger.info("create_session %s mode=file_editing(persistent)", result["session_id"][:8])
        await session_listing_api.apply_initial_session_folder(session.get("id"), requested_folder_id)
        return session
    _browser_harness_id = extension_store.extension_id_for_role("browser-harness")
    _browser_harness_instance = _harness_instances.get(_browser_harness_id) if _browser_harness_id else None
    _browser_harness_gate_open = bool(
        _browser_harness_id and _browser_harness_id not in _harness_disabled_extensions
    )
    browser_harness_enabled = bool(
        _browser_harness_gate_open
        and _browser_harness_instance
        and _browser_harness_instance["mcp_servers"]["resolved"]
    )
    browser_harness_headless = bool(
        (_browser_harness_instance or {}).get("headless", {}).get("resolved", False)
    )
    create_kwargs = dict(
        name=body.get("name", ""),
        model=model,
        cwd=body.get("cwd", ""),
        orchestration_mode=requested_orchestration_mode,
        source=requested_source,
        provider_id=requested_provider_id,
        runner=requested_runner,
        runtime_profile_id=requested_profile_id,
        reasoning_effort=requested_effort,
        permission=requested_permission or None,
        browser_harness_enabled=browser_harness_enabled,
        browser_harness_headless=browser_harness_headless,
        node_id=node_id,
        worker_creation_policy=worker_creation_policy,
        bare_config=bare_config,
        harness_profile_id=harness_profile_id,
        # The UI/CLI `POST /api/sessions` is always an explicit user
        # action — the user is aware of (and owns) this session.
        user_initiated=True,
        capability_contexts=capability_contexts,
        id=client_session_id,
    )
    try:
        session = await asyncio.to_thread(session_manager.create, **create_kwargs)
    except ValueError:
        # Idempotent retry: a concurrent request carrying the same
        # client_session_id won the create between our pre-check and this
        # call. Return that session instead of failing the retry, which the
        # client would classify as retryable and replay again.
        if client_session_id is None:
            raise
        existing = await asyncio.to_thread(session_manager.get, client_session_id)
        if existing is None:
            raise
        return existing
    backend_url = body.get("backend_url")
    if isinstance(backend_url, str) and (
        backend_url.startswith("http://127.0.0.1:")
        or backend_url.startswith("http://localhost:")
    ):
        session = await asyncio.to_thread(
            session_manager.set_backend_url,
            session["id"],
            backend_url.rstrip("/"),
        ) or session
    if session_store.should_auto_register_project(session):
        try:
            await asyncio.to_thread(
                project_store.add_project,
                session["cwd"],
                node_id=session.get("node_id") or "primary",
            )
            await _broadcast_projects_changed()
        except Exception as e:
            logger.warning("auto add_project failed: %s", e)
    session_profile_id = runtime_profiles_api.runtime_profile_id_for_session(session)
    if body.get("model"):
        await runtime_profiles_api.record_last_model(
            session_profile_id, session.get("model")
        )
    if requested_effort:
        await runtime_profiles_api.record_last_reasoning_effort(
            session_profile_id, session.get("reasoning_effort"),
        )
    logger.info("create_session %s mode=%s", session["id"][:8], session.get("orchestration_mode"))
    await session_listing_api.apply_initial_session_folder(session.get("id"), requested_folder_id)
    return session


@router.post("/api/sessions/{session_id}/fork")


async def fork_session_endpoint(session_id: str, body: dict = Body(default={})):
    try:
        return await asyncio.to_thread(
            session_manager.fork,
            session_id,
            name=(body or {}).get("name"),
        )
    except KeyError:
        raise HTTPException(status_code=404, detail=t("error.session_not_found"))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/api/sessions/{session_id}/fork_and_send")


async def fork_and_send(session_id: str, body: dict = Body(default={})):
    """Atomic fork-then-submit. Forks `session_id` off its current claude
    head, then enqueues `prompt` against the new child via the same
    coordinator path the WS send_message handler uses. The processor
    replaces ws_callback at dequeue time with a registry-based dispatcher,
    so any client that subscribes to the child's session id over WS will
    receive its live events."""
    body = body or {}
    prompt = (body.get("prompt") or "").strip()
    images = body.get("images") or []
    if not prompt and not images:
        raise HTTPException(status_code=400, detail=t("error.prompt_required"))
    try:
        child = await asyncio.to_thread(session_manager.fork, session_id, name=body.get("name"))
    except KeyError:
        raise HTTPException(status_code=404, detail=t("error.session_not_found"))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Sample provider env so the new fork's CLI spawn inherits the right
    # ANTHROPIC_API_KEY / BASE_URL / CONFIG_DIR. Mirrors the WS handler.
    await asyncio.to_thread(config_store.apply_env_vars)

    child_id = child["id"]
    images = body.get("images") or None
    await _coordinator().submit_prompt_async(child_id, {
        "prompt": prompt,
        "app_session_id": child_id,
        "model": body.get("model") or child.get("model"),
        "cwd": body.get("cwd") or child.get("cwd"),
        "ws_callback": None,
        "images": images,
        "orchestration_mode": body.get("orchestration_mode") or child.get("orchestration_mode"),
        "client_id": body.get("client_id"),
    })
    return {
        "child": child,
        "fork_point_seq": child.get("fork_point_seq"),
    }


@router.post("/api/sessions/{session_id}/close_fork")


async def close_fork(session_id: str):
    sess = await asyncio.to_thread(session_manager.set_fork_closed, session_id, True)
    if sess is None:
        raise HTTPException(status_code=404, detail=t("error.session_not_found"))
    return {"id": session_id, "fork_closed": True}


@router.post("/api/sessions/{session_id}/reopen_fork")


async def reopen_fork(session_id: str):
    """Inverse of /close_fork — flips `fork_closed` back to false so the
    pane becomes focusable and able to receive new prompts again. The
    pane was never destroyed; it just gets unlocked."""
    sess = await asyncio.to_thread(session_manager.set_fork_closed, session_id, False)
    if sess is None:
        raise HTTPException(status_code=404, detail=t("error.session_not_found"))
    return {"id": session_id, "fork_closed": False}


@router.post("/api/internal/supervisor/default-prompt")


async def internal_supervisor_default_prompt(_body: dict | None = Body(default=None)):
    internal_guards.require_builtin_runtime_extension(extension_store.extension_id_for_role('supervisor'))
    from orchs.supervisor._verdict import DEFAULT_SUPERVISOR_CUSTOM_PROMPT
    return {"prompt": DEFAULT_SUPERVISOR_CUSTOM_PROMPT}


@router.post("/api/internal/supervisor/toggle")


async def internal_supervisor_toggle(body: dict = Body(...)):
    internal_guards.require_builtin_runtime_extension(extension_store.extension_id_for_role('supervisor'))
    session_id = str(body.get("session_id") or "").strip()
    if not session_id:
        return {"success": False, "status": 400, "error": "session_id is required"}
    enabled = bool(body.get("enabled", False))
    custom_prompt = body.get("custom_prompt")
    if custom_prompt is not None and not isinstance(custom_prompt, str):
        return {"success": False, "status": 400, "error": "custom_prompt must be a string"}
    session = await asyncio.to_thread(
        session_manager.set_supervisor_enabled,
        session_id,
        enabled,
        custom_prompt=custom_prompt if enabled else None,
    )
    if session is None:
        return {"success": False, "status": 404, "error": t("error.session_not_found")}
    return {"success": True, "session": session}


@router.post("/api/internal/supervisor/review-last-work")


async def internal_supervisor_review_last_work(body: dict = Body(...)):
    internal_guards.require_builtin_runtime_extension(extension_store.extension_id_for_role('supervisor'))
    session_id = str(body.get("session_id") or "").strip()
    if not session_id:
        return {"success": False, "status": 400, "error": "session_id is required"}
    session = await _session_lite(session_id)
    if not session:
        return {"success": False, "status": 404, "error": t("error.session_not_found")}
    if not session.get("supervisor_enabled"):
        return {"success": False, "status": 409, "error": t("error.ws_review_supervisor_only")}
    await asyncio.to_thread(config_store.apply_env_vars)
    try:
        queued_id = await _coordinator().submit_prompt_async(session_id, {
            "_review": True,
            "app_session_id": session_id,
        })
    except RuntimeError as e:
        return {"success": False, "status": 409, "error": str(e)}
    return {"success": True, "queued_id": queued_id}


@router.post("/api/internal/supervisor/separate")


async def internal_supervisor_separate(body: dict = Body(...)):
    internal_guards.require_builtin_runtime_extension(extension_store.extension_id_for_role('supervisor'))
    session_id = str(body.get("session_id") or "").strip()
    if not session_id:
        return {"success": False, "status": 400, "error": "session_id is required"}
    if not await _session_exists(session_id):
        return {"success": False, "status": 404, "error": t("error.session_not_found")}
    if _coordinator().turn_manager.has_active_runs(session_id):
        return {
            "success": False,
            "status": 409,
            "error": "cannot separate supervisor while a turn is queued or in flight",
        }
    try:
        new_session = await asyncio.to_thread(session_manager.separate_supervisor, session_id)
    except KeyError:
        return {"success": False, "status": 404, "error": t("error.session_not_found")}
    except ValueError as e:
        msg = str(e)
        if "in flight" in msg or "queued" in msg:
            return {"success": False, "status": 409, "error": msg}
        return {"success": False, "status": 422, "error": msg}
    return {
        "success": True,
        "new_session_id": new_session["id"],
        "session": new_session,
    }


async def _publish_worker_fanout_required(
    session_id: str,
    *,
    op_label: str,
    caller_scope: bool,
    remove_worker: bool,
    outer_log_msg: str,
) -> None:
    await event_bus.publish(BusEvent(
        type="session.worker_fanout_required",
        root_id=session_id,
        sid=session_id,
        payload={
            "session_id": session_id,
            "op_label": op_label,
            "caller_scope": caller_scope,
            "remove_worker": remove_worker,
            "outer_log_msg": outer_log_msg,
        },
        persist=False,
    ))


@router.delete("/api/sessions/{session_id}")


async def delete_session(session_id: str):
    # Per-step timing instrumentation. The handler chains 6 steps
    # serially; user-perceived "delete hangs forever" needs us to
    # identify WHICH step is the offender on the next reproduction.
    # Logs a single line per step with elapsed ms — emitted even on
    # exception so a hang followed by Ctrl+C still surfaces the
    # last-completed step.
    import time as _time
    _steps: list[tuple[str, float]] = []
    _t_total = _time.perf_counter()

    def _mark(label: str, t0: float) -> None:
        _steps.append((label, (_time.perf_counter() - t0) * 1000.0))

    try:
        virtual = await asyncio.to_thread(virtual_session_store.get, session_id)
        if virtual:
            try:
                deleted = await asyncio.to_thread(
                    virtual_session_store.delete,
                    str(virtual.get("extension_id") or ""),
                    session_id,
                )
            except PermissionError:
                raise HTTPException(status_code=403, detail="extension does not own this virtual session")
            if deleted:
                await _coordinator().broadcast_global(
                    "session_deleted",
                    {"session_id": session_id},
                )
                await ui_selection_projection.close_tabs_for_deleted([session_id])
            return {"deleted": deleted}
        _t = _time.perf_counter()
        ok = await _delete_session_tree(session_id)
        _mark("delete_session_tree", _t)
        return {"deleted": ok}
    finally:
        total_ms = (_time.perf_counter() - _t_total) * 1000.0
        per_step = ", ".join(f"{name}={ms:.0f}ms" for name, ms in _steps)
        logger.info(
            "delete_session sid=%s total=%.0fms steps=[%s]",
            session_id, total_ms, per_step,
        )


@router.put("/api/sessions/{session_id}/rename")


async def rename_session(session_id: str, body: dict):
    new_name = strip_link_marker_syntax((body or {}).get("name", "").strip())
    if not new_name:
        return {"error": t("error.name_is_required_rename")}, 400
    virtual = await asyncio.to_thread(virtual_session_store.get, session_id)
    if virtual:
        virtual["name"] = new_name
        try:
            session = await asyncio.to_thread(
                virtual_session_store.upsert,
                str(virtual.get("extension_id") or ""),
                virtual,
            )
        except PermissionError:
            raise HTTPException(status_code=403, detail="extension does not own this virtual session")
        await _coordinator().broadcast_global(
            "session_metadata_updated",
            {"session_id": session_id, "patch": {"name": session.get("name")}},
        )
        return {"id": session_id, "name": new_name}
    session = await asyncio.to_thread(session_manager.rename, session_id, new_name)
    if not session:
        return {"error": t("error.session_not_found_rename")}, 404
    # rename() refuses locked sessions (e.g. the assistant singleton) and
    # returns the unchanged record; surface that as a clear 403, not a silent
    # success that would let the UI believe the rename took.
    if session.get("name_locked") and session.get("name") != new_name:
        return {"error": "session name is locked", "name_locked": True}, 403
    return {"id": session_id, "name": new_name}


@router.put("/api/sessions/{session_id}/pin")


async def pin_session(session_id: str, body: dict):
    pinned = bool((body or {}).get("pinned", True))
    session = await asyncio.to_thread(session_manager.set_pinned, session_id, pinned)
    if not session:
        raise HTTPException(status_code=404, detail=t("error.session_not_found_retry"))
    return {"id": session_id, "pinned": pinned}


@router.post("/api/sessions/{session_id}/move-to-project")


async def move_session_to_project(session_id: str, body: dict):
    """Move a session to another project continuation-style: create a NEW
    session in the target project's cwd whose first turn gets a continuation
    handoff prompt pointing at the old session's provider-native transcript,
    stamp moved_to/moved_from pointers on both, and archive the old session.
    The old session's data is never physically moved."""
    target_cwd = str((body or {}).get("cwd") or "").strip()
    if not target_cwd:
        raise HTTPException(status_code=400, detail="cwd is required")
    expanded_cwd = os.path.realpath(os.path.expanduser(target_cwd))
    if not os.path.isdir(expanded_cwd):
        raise HTTPException(status_code=400, detail="cwd must be an existing directory")
    old = await asyncio.to_thread(session_manager.get, session_id)
    if not old:
        raise HTTPException(status_code=404, detail=t("error.session_not_found_retry"))
    if old.get("id") != session_id:
        raise HTTPException(status_code=400, detail="only root sessions can be moved")
    if old.get("moved_to_session_id"):
        raise HTTPException(status_code=409, detail="session was already moved")
    old_cwd = os.path.realpath(os.path.expanduser(str(old.get("cwd") or "")))
    if old_cwd == expanded_cwd:
        raise HTTPException(status_code=400, detail="session is already in that project")
    if _coordinator().turn_manager.has_active_runs(session_id):
        raise HTTPException(
            status_code=409,
            detail="cannot move a session while a turn is running",
        )
    try:
        new_session = await asyncio.to_thread(
            session_manager.create,
            name=old.get("name") or "",
            model=old.get("model"),
            cwd=expanded_cwd,
            orchestration_mode=old.get("orchestration_mode") or "native",
            source=old.get("source") or "web",
            provider_id=old.get("provider_id"),
            reasoning_effort=old.get("reasoning_effort"),
            permission=old.get("permission"),
            browser_harness_enabled=bool(old.get("browser_harness_enabled", False)),
            browser_harness_headless=bool(old.get("browser_harness_headless", True)),
            node_id=old.get("node_id") or "primary",
            worker_creation_policy=old.get("worker_creation_policy") or "ask",
            bare_config=bool(old.get("bare_config", False)),
            user_initiated=True,
            capability_contexts=old.get("capability_contexts") or None,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    new_sid = new_session["id"]
    chain = [
        item.strip()
        for item in (old.get("continuation_chain") or [])
        if isinstance(item, str) and item.strip()
    ]
    old_agent_sid = str(old.get("agent_session_id") or "").strip()
    if old_agent_sid:
        chain.append(old_agent_sid)
    if chain:
        await asyncio.to_thread(
            session_manager.set_continuation_chain, new_sid, chain,
        )
    # Physically carry the source's render tree and summary snapshot into the
    # new session so the moved session shows its history instead of starting
    # blank. The first turn still gets a moved_project continuation handoff
    # (provider starts fresh; the handoff points it at the source transcript).
    await asyncio.to_thread(
        session_migrate.migrate_session_content, session_id, new_sid,
    )
    await asyncio.to_thread(
        session_manager.apply_migrated_fields, new_sid, old,
    )
    await asyncio.to_thread(session_manager.set_moved_from, new_sid, session_id)
    await asyncio.to_thread(session_manager.set_moved_to, session_id, new_sid)
    await asyncio.to_thread(session_manager.set_archived, session_id, True)
    await _broadcast_projects_changed()
    return await _session_lite(new_sid) or new_session


@router.put("/api/sessions/{session_id}/topbar-pin")


async def pin_session_to_topbar(session_id: str, body: dict):
    pinned = bool((body or {}).get("pinned", True))
    session = await asyncio.to_thread(session_manager.set_topbar_pinned, session_id, pinned)
    if not session:
        raise HTTPException(status_code=404, detail=t("error.session_not_found_retry"))
    return {
        "id": session_id,
        "topbar_pinned": pinned,
        "topbar_pinned_at": session.get("topbar_pinned_at"),
    }


@router.post("/api/sessions/{session_id}/opened")


async def mark_session_opened(session_id: str):
    """Stamp `last_opened_at` after a client opens this session's chat view.
    Server-generated timestamp; does not bump `updated_at`."""
    at = datetime.now().isoformat()
    session = await hot_path.run(
        "session.opened.set_last_opened_at",
        session_manager.set_last_opened_at,
        session_id,
        at,
        return_session=False,
    )
    if not session:
        raise HTTPException(status_code=404, detail=t("error.session_not_found_retry"))
    return {"id": session_id, "last_opened_at": at}


@router.post("/api/sessions/{session_id}/unpin-others")


async def unpin_other_sessions(session_id: str, body: dict = Body(default={})):
    unpinned_ids = await asyncio.to_thread(session_manager.unpin_others, session_id)
    if unpinned_ids is None:
        raise HTTPException(status_code=404, detail=t("error.session_not_found_retry"))
    return {
        "id": session_id,
        "unpinned_ids": unpinned_ids,
        "count": len(unpinned_ids),
    }


@router.put("/api/sessions/{session_id}/agent_rename_allowed")


async def set_agent_rename_allowed(session_id: str, body: dict):
    value = bool((body or {}).get("agent_rename_allowed", False))
    session = await asyncio.to_thread(
        session_manager.set_agent_rename_allowed, session_id, value,
    )
    if not session:
        raise HTTPException(status_code=404, detail=t("error.session_not_found_retry"))
    return {"id": session_id, "agent_rename_allowed": value}


@router.put("/api/sessions/{session_id}/worker_eligible")


async def set_worker_eligible(session_id: str, body: dict):
    internal_guards.require_builtin_runtime_extension(extension_store.extension_id_for_role('team-orchestration'))
    value = bool((body or {}).get("worker_eligible", True))
    session = await asyncio.to_thread(session_manager.set_worker_eligible, session_id, value)
    if not session:
        raise HTTPException(status_code=404, detail=t("error.session_not_found_retry"))
    return {"id": session_id, "worker_eligible": value}


@router.put("/api/sessions/{session_id}/worker_creation_policy")


async def set_worker_creation_policy(session_id: str, body: dict):
    internal_guards.require_builtin_runtime_extension(extension_store.extension_id_for_role('team-orchestration'))
    policy = str((body or {}).get("worker_creation_policy") or "ask")
    if policy not in ("ask", "approve", "deny"):
        raise HTTPException(status_code=400, detail="worker_creation_policy must be ask, approve, or deny")
    session = await asyncio.to_thread(
        session_manager.set_worker_creation_policy,
        session_id,
        policy,
    )
    if not session:
        raise HTTPException(status_code=404, detail=t("error.session_not_found_retry"))
    return {"id": session_id, "worker_creation_policy": policy}


@router.put("/api/sessions/{session_id}/archive")


async def archive_session(session_id: str, body: dict):
    archived = bool((body or {}).get("archived", True))
    session = await asyncio.to_thread(session_manager.set_archived, session_id, archived)
    if not session:
        raise HTTPException(status_code=404, detail=t("error.session_not_found_retry"))
    return {"id": session_id, "archived": archived}


async def _resolve_selector_updates(session_id: str, body: dict) -> dict:
    """Parse a selectors payload into a validated `updates` dict (model,
    provider_id, reasoning_effort, permission, cwd). Shared by the public
    /selectors endpoint and the internal session-control tool so the agent
    applies selectors through the SAME validation + fail-closed path."""
    body = body or {}
    updates: dict = {}
    session_snapshot = await _session_lite(session_id)
    if not session_snapshot:
        raise HTTPException(status_code=404, detail=t("error.session_not_found_retry"))
    requested_profile_id = None
    if "runtime_profile_id" in body:
        raw_profile_id = body.get("runtime_profile_id")
        if not isinstance(raw_profile_id, str) or not raw_profile_id.strip():
            raise HTTPException(
                status_code=400, detail="runtime_profile_id is required"
            )
        switch_profile = await asyncio.to_thread(
            config_store.get_runtime_profile, raw_profile_id
        )
        if switch_profile is None or switch_profile.get("deleted_at"):
            raise HTTPException(
                status_code=400, detail="runtime profile is unknown or deleted"
            )
        if (
            isinstance(body.get("provider_id"), str)
            and body["provider_id"].strip()
            and body["provider_id"].strip() != switch_profile["provider_id"]
        ):
            raise HTTPException(
                status_code=400,
                detail="provider_id conflicts with runtime_profile_id",
            )
        if (
            isinstance(body.get("runner"), str)
            and body["runner"].strip()
            and body["runner"].strip() != switch_profile["runner"]
        ):
            raise HTTPException(
                status_code=400, detail="runner conflicts with runtime_profile_id"
            )
        requested_profile_id = raw_profile_id
        # Profile switch restamps provider+runner through the shared
        # validation below; model/effort stay overridable.
        body = {
            **body,
            "provider_id": switch_profile["provider_id"],
            "runner": switch_profile["runner"],
        }
    requested_provider_id = (
        body.get("provider_id").strip()
        if isinstance(body.get("provider_id"), str) and body.get("provider_id").strip()
        else None
    )
    provider_record = None
    if "provider_id" in body:
        if not requested_provider_id:
            raise HTTPException(status_code=400, detail="provider_id is required")
        provider_record = await asyncio.to_thread(
            config_store.get_provider,
            requested_provider_id,
        )
        if not provider_record:
            if config_store.provider_suspended(requested_provider_id):
                raise HTTPException(
                    status_code=409,
                    detail=t("error.provider_suspended", action="select for a session"),
                )
            raise HTTPException(status_code=400, detail="Unknown provider")
        _provider_not_suspended(requested_provider_id, action="select for a session")
        updates["provider_id"] = requested_provider_id
    if "model" in body and isinstance(body["model"], str) and body["model"].strip():
        requested_model = body["model"].strip()
        provider_for_model = (
            requested_provider_id
            or session_snapshot.get("provider_id")
        )
        # Fail closed: with no resolvable provider, `available_models(None)`
        # would validate against the DEFAULT provider — letting a foreign
        # model (e.g. glm-5.2 onto a Claude session) slip through. Reject.
        if not provider_for_model:
            raise HTTPException(
                status_code=400, detail=t("error.session_not_found_retry"),
            )
        await asyncio.to_thread(
            _validate_provider_model, provider_for_model, requested_model, True,
        )
        updates["model"] = requested_model
    elif "provider_id" in updates:
        updates["model"] = await runtime_profiles_api.model_for_profile_switch(
            requested_provider_id,
            provider_record or {},
            body.get("runner"),
        )
    profile_provider_id = requested_provider_id or session_snapshot.get("provider_id")
    if "runner" in body:
        if not isinstance(body.get("runner"), str) or not body["runner"].strip():
            raise HTTPException(status_code=400, detail="runner is required")
        updates["runner"] = _provider_runner(profile_provider_id, body["runner"])
    elif requested_provider_id:
        updates["runner"] = _provider_runner(profile_provider_id)
    if requested_profile_id is not None:
        updates["runtime_profile_id"] = requested_profile_id
    elif "provider_id" in updates or "runner" in updates:
        # Raw provider/runner change: re-attach the resulting pair's live
        # profile, or detach (legacy) when the pair has none.
        pair_provider = updates.get("provider_id") or session_snapshot.get("provider_id")
        pair_runner = updates.get("runner") or session_snapshot.get("runner")
        matched = (
            await asyncio.to_thread(
                config_store.find_live_runtime_profile, pair_provider, pair_runner
            )
            if pair_provider
            else None
        )
        updates["runtime_profile_id"] = matched["id"] if matched else None
    profile_runner = updates.get("runner") or session_snapshot.get("runner")
    profile_model = updates.get("model") or session_snapshot.get("model") or ""
    profile_record = provider_record or config_store.get_provider(profile_provider_id)
    if "reasoning_effort" in body:
        requested_effort = _api_reasoning_effort(body.get("reasoning_effort"))
        if requested_effort is None:
            raise HTTPException(status_code=400, detail="reasoning_effort is required")
        provider_for_effort = (
            requested_provider_id
            or session_snapshot.get("provider_id")
        )
        updates["reasoning_effort"] = _provider_reasoning_effort(
            provider_for_effort, requested_effort, profile_runner, profile_model,
        )
    if "permission" in body:
        requested_permission = _api_permission(body.get("permission"))
        if requested_permission is None:
            raise HTTPException(status_code=400, detail="permission is required")
        provider_for_permission = (
            requested_provider_id
            or session_snapshot.get("provider_id")
        )
        updates["permission"] = _provider_permission(
            provider_for_permission, requested_permission, profile_runner,
        )
    if "harness_profile_id" in body:
        updates["harness_profile_id"] = _harness_profile_selection(body)
    if "cwd" in body and isinstance(body["cwd"], str) and body["cwd"].strip():
        # Fail closed, matching move-to-project: a session cwd becomes a CLI
        # subprocess working directory, so accept only an existing directory,
        # resolved to the same identity the project registry stores.
        requested_cwd = os.path.realpath(os.path.expanduser(body["cwd"].strip()))
        if not os.path.isdir(requested_cwd):
            raise HTTPException(status_code=400, detail="cwd must be an existing directory")
        updates["cwd"] = requested_cwd
    if requested_provider_id or "runner" in updates or "model" in updates:
        if "reasoning_effort" not in updates:
            default_effort = config_store.provider_execution_defaults(
                profile_provider_id, profile_runner
            )["default_reasoning_effort"]
            allowed_efforts = runtime_profile.reasoning_efforts(
                profile_record or {}, profile_runner, model=profile_model,
            )
            current_effort = session_snapshot.get("reasoning_effort") or ""
            updates["reasoning_effort"] = current_effort if current_effort in allowed_efforts else (
                default_effort if default_effort in allowed_efforts
                else (allowed_efforts[0] if allowed_efforts else "")
            )
    if requested_provider_id:
        if "permission" not in updates:
            updates["permission"] = {}
    if "orchestration_mode" in body:
        raise HTTPException(
            status_code=409,
            detail=t("error.orchestration_mode_frozen"),
        )
    if not updates:
        raise HTTPException(status_code=400, detail=t("error.no_recognized_selector"))
    return updates


@router.patch("/api/sessions/{session_id}/selectors")


async def update_session_selectors(session_id: str, body: dict):
    """Persist selector changes (model, cwd, provider_id) to the
    session record.

    `orchestration_mode` is FROZEN at session creation time and cannot
    be changed here — flipping modes mid-session orphans the claude
    session under the old mode.

    `provider_id` IS mutable at any time. The lifecycle selector authority
    persists the target and continuation handoff before projecting the
    change and invalidating incompatible native SID authority. `set_selectors` re-validates
    `orchestration_mode` against the new provider's capability (e.g.
    Claude→a non-manager-mode provider on a manager-mode session fails here with
    `IncompatibleOrchestrationMode` → 400)."""
    body = body or {}
    updates = await _resolve_selector_updates(session_id, body)
    client_id = body.get("client_id") if isinstance(body.get("client_id"), str) else None
    before = await asyncio.to_thread(session_manager.get, session_id)
    session = await _coordinator().lifecycle_commands.transition_selectors(
        session_id,
        updates=updates,
        client_id=client_id,
    )
    if not session:
        raise HTTPException(status_code=404, detail=t("error.session_not_found_retry"))
    _record_model_switched_event(session_id, before or {}, session, updates)
    session_profile_id = runtime_profiles_api.runtime_profile_id_for_session(session)
    if "model" in updates:
        await runtime_profiles_api.record_last_model(
            session_profile_id, updates["model"]
        )
    if "reasoning_effort" in updates and updates["reasoning_effort"]:
        await runtime_profiles_api.record_last_reasoning_effort(
            session_profile_id, updates["reasoning_effort"],
        )
    return {"id": session_id, "updates": updates}


# ── Project matching ───────────────────────────────────────────────

_project_match_executor: ProcessPoolExecutor | None = None
_project_match_ready = False
_project_match_warm_task: asyncio.Task | None = None


def _ensure_project_match_executor() -> ProcessPoolExecutor:
    global _project_match_executor, _project_match_ready
    if _project_match_executor is None:
        _project_match_executor = ProcessPoolExecutor(max_workers=1)
        _project_match_ready = False
    return _project_match_executor


def shutdown_project_match() -> None:
    """Tear down the project-match warm task and its process pool."""
    global _project_match_executor, _project_match_ready, _project_match_warm_task
    if _project_match_warm_task is not None:
        _project_match_warm_task.cancel()
        _project_match_warm_task = None
    if _project_match_executor is not None:
        _project_match_executor.shutdown(wait=False, cancel_futures=True)
        _project_match_executor = None
        _project_match_ready = False


@router.post("/api/sessions/{session_id}/project-suggestion")


async def project_suggestion(session_id: str, body: dict):
    """Read-only: does this prompt look like it belongs to a different
    project than the session's current cwd? Returns a switch suggestion
    or null. Called pre-send on a fresh session so the user can move it
    to the right project before the first turn spawns a CLI in the wrong
    cwd (cheap to move before it starts, costly after)."""
    body = body or {}
    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise HTTPException(status_code=400, detail=t("error.ws_empty_prompt"))
    session = await _session_lite(session_id)
    if not session:
        raise HTTPException(status_code=404, detail=t("error.session_not_found_retry"))
    current_cwd = session.get("cwd")
    if not current_cwd:
        return {"suggestion": None}
    if not _project_match_ready or _project_match_executor is None:
        _ensure_project_match_warm_task()
        return {"suggestion": None}
    from project_match.worker import suggest_project_payload

    try:
        sugg = await asyncio.get_running_loop().run_in_executor(
            _project_match_executor,
            suggest_project_payload,
            prompt.strip(),
            current_cwd,
        )
    except Exception:
        logger.exception("project_match: suggestion failed")
        return {"suggestion": None}
    if not sugg:
        return {"suggestion": None}
    return {
        "suggestion": {
            "target_cwd": sugg["target_cwd"],
            "score": sugg["score"],
            "margin": sugg["margin"],
        }
    }


async def _project_match_warm_loop():
    """Build the project-match index once at startup (model load ~34s),
    then rebuild every 15 min so suggestions track new prompts. Runs in a
    dedicated process so model load/indexing cannot starve uvicorn's event loop."""
    global _project_match_ready
    from project_match.worker import rebuild_index

    fingerprint = None
    while True:
        try:
            if _project_match_executor is None:
                logger.warning("project_match: executor unavailable")
                return
            result = await asyncio.get_running_loop().run_in_executor(
                _project_match_executor,
                rebuild_index,
                fingerprint,
            )
            fingerprint = result.get("fingerprint") if isinstance(result, dict) else None
            _project_match_ready = True
            if isinstance(result, dict) and result.get("rebuilt") is False:
                logger.info("project_match: index unchanged; rebuild skipped")
            else:
                logger.info("project_match: index rebuilt")
        except Exception:
            _project_match_ready = False
            logger.exception("project_match: index rebuild failed")
        await asyncio.sleep(15 * 60)


def _ensure_project_match_warm_task() -> None:
    global _project_match_warm_task
    if _project_match_warm_task is not None and not _project_match_warm_task.done():
        return
    _ensure_project_match_executor()
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _project_match_warm_task = loop.create_task(
        _project_match_warm_loop(),
        name="project-match-warm",
    )


@router.get("/api/processes")


async def list_processes():
    """List all active runner processes across every loaded provider."""
    from provider import known_providers
    runs: list[dict] = []
    for prov in known_providers():
        runs.extend(prov.active_runs())
    return {"processes": runs}


@router.get("/api/sessions/{session_id}/runs/{run_id}/details")


async def get_run_details(request: Request, session_id: str, run_id: str):
    """Diagnostic snapshot for one in-flight run — answers "why is this
    still considered running?".

    Returns the run_state entry the orchestrator holds for this run
    (kind, started_at, last_event_at, target_message_id, pid) plus the
    full descendant process tree (pid/ppid/state/CPU%/RSS/elapsed/cmd)
    walked from the runner PID. Also includes provider-side metadata
    (jsonl_path, run_dir, cancelled, popen_alive) when the provider
    that owns ``run_id`` still has bookkeeping for it.

    Computed on demand — heavy (forks ps/pgrep), so the frontend pulls
    on modal open + an explicit refresh, never on WS.
    """
    from provider import known_providers
    from process_inspect import inspect_process_tree

    runs = _coordinator().turn_manager.get_run_state(session_id)
    entry = next((r for r in runs if r.get("run_id") == run_id), None)
    if entry is None:
        raise HTTPException(
            status_code=404, detail="run not found for this session",
        )
    pid = entry.get("pid")

    # Provider-side metadata — search every loaded provider for this
    # run_id. Each provider keeps its own RunState shape, so we read
    # defensively.
    provider_info: Optional[dict] = None
    for prov in known_providers():
        rs = getattr(prov, "_runs", {}).get(run_id)
        if rs is None:
            continue
        popen = getattr(rs, "popen", None)
        jsonl_path = getattr(rs, "jsonl_path", None)
        run_dir = getattr(rs, "run_dir", None)
        provider_info = {
            "provider_kind": getattr(prov, "KIND", None),
            "mode": getattr(rs, "mode", None),
            "session_id": getattr(rs, "session_id", None),
            "jsonl_path": str(jsonl_path) if jsonl_path else None,
            "run_dir": str(run_dir) if run_dir else None,
            "cancelled": getattr(rs, "cancelled", False),
            "popen_alive": (
                popen.poll() is None if popen is not None else None
            ),
            "popen_pid": getattr(popen, "pid", None) if popen else None,
        }
        break

    processes = await asyncio.to_thread(inspect_process_tree, pid, run_id)

    return await session_list_cache.json_response_off_loop(
        {
            "run_id": run_id,
            "app_session_id": session_id,
            "kind": entry.get("kind"),
            "target_message_id": entry.get("target_message_id"),
            "delegation_id": entry.get("delegation_id"),
            "pid": pid,
            "started_at": entry.get("started_at"),
            "last_event_at": entry.get("last_event_at"),
            "provider_kind": entry.get("provider_kind"),
            "startup_phase": entry.get("startup_phase"),
            "startup_expected_activity": entry.get("startup_expected_activity"),
            "startup_phase_started_at": entry.get("startup_phase_started_at"),
            "startup_silence_threshold_seconds": entry.get(
                "startup_silence_threshold_seconds"
            ),
            "stalled_at": entry.get("stalled_at"),
            "last_activity_at": entry.get("last_activity_at"),
            "last_activity_kind": entry.get("last_activity_kind"),
            "provider": provider_info,
            "processes": processes,
        },
        request.headers.get("accept-encoding", ""),
        executor=session_detail_path,
        perf_prefix="sessions.detail",
    )


@router.post("/api/sessions/{session_id}/stop")


async def stop_session_turn(session_id: str):
    cancelled = await _coordinator().turn_manager.cancel_turn(session_id)
    if not cancelled:
        raise HTTPException(
            status_code=409,
            detail=t("error.ws_no_active_turn_to_stop"),
        )
    return {"stopped": True}


@router.get("/api/sessions/{session_id}/details")


async def get_session_details(request: Request, session_id: str):
    """Session-level "Details" snapshot for the menu panel: the live
    monitoring state, the provenance log (what ran + WHY), and the
    escape-proof process tree across all of the session's runs.

    Computed on demand (forks ps); the frontend pulls on panel open and
    refetches on the `session_monitoring_changed` /
    `session_provenance_changed` WS pings.
    """
    from process_inspect import inspect_process_tree
    from stores import provenance_store
    from containment import containment

    runs = _coordinator().turn_manager.get_run_state(session_id)
    trees = []
    for entry in runs:
        rid, pid = entry.get("run_id"), entry.get("pid")
        procs = await asyncio.to_thread(inspect_process_tree, pid, rid)
        trees.append({
            "run_id": rid,
            "kind": entry.get("kind"),
            "pid": pid,
            "started_at": entry.get("started_at"),
            "processes": procs,
        })
    provenance = await asyncio.to_thread(
        provenance_store.read, session_id, limit=500,
    )
    return await session_list_cache.json_response_off_loop(
        {
            "session_id": session_id,
            "monitoring_state": _coordinator().turn_manager.monitoring_state(session_id),
            "tracking_guaranteed": containment().guaranteed,
            "provenance": provenance,
            "runs": trees,
        },
        request.headers.get("accept-encoding", ""),
        executor=session_detail_path,
        perf_prefix="sessions.detail",
    )


@router.get("/api/sessions/{session_id}/changes")


async def get_session_changes(session_id: str):
    """Every file edit made in this session + the reasoning that preceded
    each, projected from the provenance log and grouped by the user→assistant
    turn that produced them. Backend owns the filter (file-edit detection
    across providers) and the turn grouping (it owns the render tree); the
    Changes right-panel just renders. Refetched on the
    ``session_provenance_changed`` WS ping."""
    from stores import provenance_store

    def _build():
        changes = provenance_store.read_file_changes(session_id)
        sess = session_manager.get(session_id) or {}
        messages = sess.get("messages") or []
        return provenance_store.group_changes_by_turn(messages, changes)

    turns = await asyncio.to_thread(_build)
    return {"session_id": session_id, "turns": turns}


@router.post("/api/sessions/{session_id}/seen")


async def mark_session_seen(session_id: str, body: Optional[dict] = None):
    """Frontend ack: the user has viewed this session. Persists
    `last_seen_event_uid` on the Session record and resets the
    transient unread counter to 0. Fires `session_unread_changed`
    (broadcast_global) so every open client (multi-tab) converges.

    Body: `{"uid": "<event-uuid>" | null}`. `null` / missing → ack the
    current head (mutator picks the most recent UUID found by walking
    msg.events). Idempotent — a re-ack with the same uid simply re-fires
    the WS frame.
    """
    uid = (body or {}).get("uid")
    sess = await asyncio.to_thread(
        session_manager.mark_seen,
        session_id,
        uid if isinstance(uid, str) and uid else None,
    )
    if sess is None:
        raise HTTPException(status_code=404, detail=t("error.session_not_found"))
    return {
        "session_id": session_id,
        "last_seen_event_uid": sess.get("last_seen_event_uid"),
        "unread_count": 0,
    }


@router.post("/api/sessions/{session_id}/unread")


async def mark_session_unread(session_id: str):
    """Frontend action: force this session into the "has new" state.
    Clears `last_seen_event_uid` and recomputes the unread set so the
    sidebar badge appears, persisting it. Fires `session_unread_changed`
    (broadcast_global) so every open client converges.
    """
    sess = await asyncio.to_thread(session_manager.mark_unread, session_id)
    if sess is None:
        raise HTTPException(status_code=404, detail=t("error.session_not_found"))
    return {
        "session_id": session_id,
        "last_seen_event_uid": sess.get("last_seen_event_uid"),
        "unread_count": session_manager.get_unread_count(session_id),
    }


@router.post("/api/sessions/{session_id}/rewind")


async def rewind_session(session_id: str, body: dict):
    message_id = (body or {}).get("message_id")
    if not message_id:
        raise HTTPException(status_code=400, detail=t("error.message_id_required"))
    try:
        result = await _coordinator().rewind_files(session_id, message_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    # Rewind moves the Better Agent session's agent_sid lineage. Any per-pair
    # forks pointing at this session as a worker are now stale (their
    # parent has shifted) — drop them so the next delegation re-forks
    # from the rewound head.
    await _publish_worker_fanout_required(
        session_id,
        op_label="rewind",
        caller_scope=False,
        remove_worker=False,
        outer_log_msg="clear_forks_for_worker_everywhere failed during rewind",
    )
    return result


@router.post("/api/sessions/{session_id}/rewind_and_retry")


async def rewind_and_retry(session_id: str, body: dict):
    """Discard a stopped/failed turn and atomically retry it server-side.

    Body: `{"assistant_message_id": <id>, "client_id"?: <id>}`. The caller
    points at the failed assistant bubble; this endpoint locates the user
    message immediately preceding it, DURABLY re-enqueues its prompt (with
    images/model/cwd/orchestration context) through the normal queued-prompt
    path, then REWINDS the session to before that user message — removing
    the failed user+assistant pair (and any worker forks) — and submits the
    queued prompt as a fresh turn. The durable enqueue commits before the
    rewind, so a crash or a disconnected client can never lose the prompt:
    startup re-enqueue recovers any admitted-but-unprocessed prompt.

    The retried turn flows through the canonical prompt path, so
    persistence, validation, the `user_message_persisted` WS emit (echoing
    `client_id` for optimistic-bubble resolution), and turn start behave
    exactly like a fresh user message. The response only acks the enqueue —
    the client never resends the prompt itself.

    When the failed user message has a provider rewind anchor
    (`agent_message_uuid`) the provider CLI is rewound too; otherwise the
    prompt never committed there, so only the render tree is truncated.
    """
    body = body or {}
    asst_id = body.get("assistant_message_id")
    if not asst_id:
        raise HTTPException(status_code=400, detail=t("error.assistant_message_id_required"))

    sess = await _session_lite(session_id)
    if not sess:
        raise HTTPException(status_code=404, detail=t("error.session_not_found_retry"))

    msgs = sess.get("messages") or []
    asst_idx = next(
        (i for i, m in enumerate(msgs) if m.get("id") == asst_id and m.get("role") == "assistant"),
        None,
    )
    if asst_idx is None:
        raise HTTPException(status_code=404, detail=t("error.assistant_message_not_found"))

    user_idx = next(
        (i for i in range(asst_idx - 1, -1, -1) if msgs[i].get("role") == "user"),
        None,
    )
    if user_idx is None:
        raise HTTPException(
            status_code=400,
            detail=t("error.no_preceding_user_message"),
        )

    user_msg = msgs[user_idx]
    retry_prompt = user_msg.get("content") or ""
    retry_images = []
    for img in user_msg.get("images") or []:
        if not isinstance(img, dict):
            continue
        filename = img.get("filename")
        media_type = img.get("media_type")
        if not isinstance(filename, str) or not isinstance(media_type, str):
            continue
        img_path = resolve_session_image_path(session_id, filename)
        if not img_path.exists():
            raise HTTPException(status_code=500, detail=t("error.image_not_found"))
        import base64
        raw = await asyncio.to_thread(img_path.read_bytes)
        retry_images.append({
            "data": base64.b64encode(raw).decode("ascii"),
            "media_type": media_type,
        })

    client_id = body.get("client_id") if isinstance(body.get("client_id"), str) else None
    lifecycle_msg_id = new_lifecycle_msg_id()
    orchestration_mode = sess.get("orchestration_mode") or "team"
    qp_id = str(uuid.uuid4())
    queued_prompt = {
        "id": qp_id,
        "lifecycle_msg_id": lifecycle_msg_id,
        "content": retry_prompt,
        "kind": "send",
        "queue_position": 0,
        "images_count": len(retry_images),
        "files_count": 0,
        "images": retry_images or None,
        "files": None,
        "orchestration_mode": orchestration_mode,
        "send_target": None,
        "cli_prompt": None,
        "disallowed_tools": None,
        "disabled_builtin_extensions": None,
        "client_id": client_id,
        "alter_rewind_latest": False,
        "capability_contexts": [],
        "created_at": datetime.now().isoformat(),
    }
    # Durable enqueue FIRST: once admitted, the prompt survives any crash
    # (startup re-enqueue picks it up), so the rewind below can never open
    # a loss window for the user's intent.
    admission = await asyncio.to_thread(
        session_manager.admit_queued_prompt, session_id, queued_prompt,
    )
    if not admission.get("session"):
        raise HTTPException(status_code=404, detail=t("error.session_not_found_retry"))
    if admission.get("existing_user_message") or admission.get("existing_queued_prompt"):
        # Same-client_id retry already admitted/persisted — idempotent ack.
        return {
            "ok": True,
            "enqueued": False,
            "duplicate": True,
            "client_id": client_id,
        }

    try:
        try:
            await _coordinator().rewind_files(session_id, user_msg["id"])
        except ValueError:
            # No provider rewind anchor (failed before commit) or rewind
            # unsupported — drop the failed pair from the render tree only
            # so the retry replaces the prompt instead of duplicating it.
            await _coordinator().rewind_files(
                session_id, user_msg["id"], provider_rewind=False
            )
    except (ValueError, RuntimeError) as e:
        # Rewind failed — the failed pair is still in place, so drop the
        # queued retry to avoid running a duplicate of the prompt.
        await asyncio.to_thread(
            session_manager.remove_queued_prompt, session_id, qp_id,
        )
        raise HTTPException(status_code=500, detail=str(e))

    params = {
        "prompt": retry_prompt,
        "app_session_id": session_id,
        "model": sess.get("model"),
        "cwd": sess.get("cwd"),
        "ws_callback": None,
        "images": retry_images or None,
        "files": None,
        "orchestration_mode": orchestration_mode,
        "send_target": None,
        "client_id": client_id,
        "lifecycle_msg_id": lifecycle_msg_id,
        "cli_prompt": None,
        "disallowed_tools": None,
        "disabled_builtin_extensions": None,
        "capability_contexts": [],
        "_queued_id": qp_id,
    }
    try:
        await _coordinator().submit_prompt_async(session_id, params)
    except HTTPException:
        raise
    except Exception as e:
        # Keep the durable queued prompt — startup re-enqueue recovers it —
        # but surface the submit failure to the caller.
        raise HTTPException(status_code=500, detail=str(e))

    return {
        "ok": True,
        "enqueued": True,
        "client_id": client_id,
        "lifecycle_msg_id": lifecycle_msg_id,
    }


@router.post("/api/sessions/{session_id}/pre-send-advisories")


async def pre_send_advisories_endpoint(session_id: str, body: dict):
    """Collect pre-send advisories from extensions declaring the
    ``pre_send_advisory`` hook. Advisories are signals shown to the user
    before a prompt is sent; collection failures never block sending."""
    body = body or {}
    sess = await _session_lite(session_id)
    if not sess:
        raise HTTPException(status_code=404, detail=t("error.session_not_found_retry"))
    provider_id = str(body.get("provider_id") or "").strip()
    provider = config_store.get_provider(provider_id) if provider_id else None
    provider_kind = str((provider or {}).get("kind") or "").strip()
    config_dir = str((provider or {}).get("config_dir") or "").strip()
    model = str(body.get("model") or "").strip()
    advisories = await pre_send_advisory.collect_pre_send_advisories(
        session_id,
        provider_id,
        provider_kind,
        config_dir,
        model,
        provider_mode=str((provider or {}).get("mode") or "").strip(),
        provider_base_url=str((provider or {}).get("base_url") or "").strip(),
        provider_name=str((provider or {}).get("name") or "").strip(),
    )
    return {"advisories": advisories}


@router.post("/api/sessions/{session_id}/rate-limit/continue")


async def continue_rate_limited_turn(session_id: str, body: dict):
    body = body or {}
    asst_id = body.get("assistant_message_id")
    if not asst_id:
        raise HTTPException(status_code=400, detail=t("error.assistant_message_id_required"))

    sess = await _session_lite(session_id)
    if not sess:
        raise HTTPException(status_code=404, detail=t("error.session_not_found_retry"))

    msgs = sess.get("messages") or []
    asst_idx = next(
        (i for i, m in enumerate(msgs) if m.get("id") == asst_id and m.get("role") == "assistant"),
        None,
    )
    if asst_idx is None:
        raise HTTPException(status_code=404, detail=t("error.assistant_message_not_found"))
    assistant_msg = msgs[asst_idx]
    if not assistant_msg.get("retrying_until"):
        raise HTTPException(status_code=409, detail="assistant message is not waiting on a rate limit")

    user_idx = next(
        (i for i in range(asst_idx - 1, -1, -1) if msgs[i].get("role") == "user"),
        None,
    )
    if user_idx is None:
        raise HTTPException(
            status_code=400,
            detail=t("error.no_preceding_user_message"),
        )

    updates = await _resolve_selector_updates(session_id, body)
    before = await asyncio.to_thread(session_manager.get, session_id)
    session = await _coordinator().lifecycle_commands.transition_selectors(
        session_id,
        updates=updates,
        client_id=(
            body.get("client_id")
            if isinstance(body.get("client_id"), str)
            else None
        ),
    )
    if not session:
        raise HTTPException(status_code=404, detail=t("error.session_not_found_retry"))
    _record_model_switched_event(session_id, before or {}, session, updates)
    session_profile_id = runtime_profiles_api.runtime_profile_id_for_session(session)
    if "model" in updates:
        await runtime_profiles_api.record_last_model(
            session_profile_id, updates["model"]
        )
    if updates.get("reasoning_effort"):
        await runtime_profiles_api.record_last_reasoning_effort(
            session_profile_id, updates["reasoning_effort"],
        )

    prompt = msgs[user_idx].get("content") or ""
    def start_continuation() -> dict:
        landed = _coordinator().turn_manager.request_immediate_continuation(
            session_id,
            prompt,
            reason="rate_limit_provider_switch",
        )
        if not landed:
            session_manager.set_continuation_requested(
                session_id,
                prompt,
                reason="rate_limit_provider_switch",
                when="next_turn",
            )
        return {
            "ok": True,
            "session_id": session_id,
            "updates": updates,
            "when": "now" if landed else "next_turn",
        }

    if not body.get("wait_for_completion"):
        return start_continuation()

    from rate_limit_recovery import continue_and_wait

    timeout_seconds = float(body.get("timeout_seconds") or 1200)
    if timeout_seconds <= 0 or timeout_seconds > 3600:
        raise HTTPException(status_code=400, detail="invalid recovery timeout")
    try:
        return await continue_and_wait(
            session_id,
            str(asst_id),
            start_continuation,
            timeout_seconds=timeout_seconds,
        )
    except TimeoutError as exc:
        raise HTTPException(
            status_code=504,
            detail="provider recovery did not complete before the deadline",
        ) from exc


def _latest_user_message_index(messages: list[dict]) -> Optional[int]:
    for idx in range(len(messages) - 1, -1, -1):
        if messages[idx].get("role") == "user":
            return idx
    return None


def _normalize_ws_send_mode_for_turn_state(send_mode: str, is_queued: bool) -> str:
    if send_mode == "steer" and not is_queued:
        return "queue"
    return send_mode


def _fallback_ws_send_mode_after_failed_steer(send_mode: str) -> str:
    if send_mode == "steer":
        return "queue"
    return send_mode


def _parse_ws_disallowed_tools(value: object) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValueError("disallowed_tools must be an array")
    parsed = []
    for tool in value:
        if not isinstance(tool, str) or not tool.strip():
            raise ValueError("disallowed_tools entries must be non-empty strings")
        parsed.append(tool.strip())
    return parsed


def _parse_ws_disabled_builtin_extensions(value: object) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValueError("disabled_builtin_extensions must be an array")
    parsed = []
    for extension_id in value:
        if not isinstance(extension_id, str) or not extension_id.strip():
            raise ValueError("disabled_builtin_extensions entries must be non-empty strings")
        parsed.append(extension_id.strip())
    return parsed


async def _rewind_latest_user_for_alter(session_id: str) -> dict:
    sess = await _session_lite(session_id)
    if not sess:
        raise HTTPException(status_code=404, detail=t("error.session_not_found_retry"))
    messages = sess.get("messages") or []
    user_idx = _latest_user_message_index(messages)
    if user_idx is None:
        raise HTTPException(status_code=400, detail=t("error.no_preceding_user_message"))
    user_msg = messages[user_idx]
    try:
        rewind_result = await _coordinator().rewind_files(
            session_id,
            user_msg["id"],
            semantic_alter=True,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    await _publish_worker_fanout_required(
        session_id,
        op_label="alter_latest",
        caller_scope=False,
        remove_worker=False,
        outer_log_msg="clear_forks_for_worker_everywhere failed during alter_latest",
    )
    return {
        "messages": rewind_result.get("messages") or [],
        "workers": rewind_result.get("workers"),
        "semantic_alter_previous_prompt": rewind_result.get("semantic_alter_previous_prompt"),
        "retry_model": sess.get("model"),
        "retry_cwd": sess.get("cwd"),
        "retry_orchestration_mode": sess.get("orchestration_mode") or "team",
    }


@router.post("/api/sessions/{sid}/messages/{msg_id}/ask-choice")
async def set_ask_choice(sid: str, msg_id: str, body: dict = Body(default={})):
    """Record the user's pick from a turn's session picker. Stamps
    `chosen_session_id` on the producing assistant message so the chosen
    row stays highlighted across reloads / tabs / previous turns. Pass
    `chosen_session_id: null` to clear."""
    body = body or {}
    chosen = body.get("chosen_session_id")
    chosen = str(chosen) if chosen else None
    if sid == session_search.ASK_SINGLETON_ID:
        updated = await session_search.set_ask_choice_async(msg_id, chosen)
    else:
        updated = await asyncio.to_thread(
            session_manager.set_msg_ask_choice,
            sid,
            msg_id,
            chosen,
        )
    if updated is None:
        raise HTTPException(status_code=404, detail="session or message not found")
    return {"success": True, "chosen_session_id": chosen}


@router.get("/api/sessions/{session_id}/images/{filename}")
async def get_session_image(session_id: str, filename: str):
    img_path = resolve_session_image_path(session_id, filename)
    if not await file_delivery.host.exists(img_path):
        raise HTTPException(status_code=404, detail=t("error.image_not_found"))
    return FileResponse(img_path)
