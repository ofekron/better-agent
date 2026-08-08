"""Response-caching machinery for the sessions list/summaries endpoints:
gzip negotiation, TTL response caches, and the session-event-meta /
machine-nodes-enabled memoizations that back them.

Depends on the backend state projection only through its invalidation
version capability, bound by the composition root (see `configure`).
"""
from __future__ import annotations

import asyncio
import gzip
import json
import logging
import re
import time
from typing import Callable, Optional

from fastapi import Response

import extension_store
import perf
from hot_path_executor import HotPathExecutor, session_list_path
import session_search
import session_store
import user_input_store
import user_prefs
import virtual_session_store
from event_ingester import event_ingester
from session_manager import manager as session_manager

logger = logging.getLogger(__name__)

_projected_state_version: Optional[Callable[[], int]] = None


def configure(projected_state_version: Callable[[], int]) -> None:
    """Bind the coordinator capability this module needs."""
    global _projected_state_version
    _projected_state_version = projected_state_version


def _require_configured() -> Callable[[], int]:
    if _projected_state_version is None:
        raise RuntimeError("session_list_cache is not configured")
    return _projected_state_version


_session_event_meta_cache: dict[
    str,
    tuple[tuple[int, int], bool, int, dict[str, int]],
] = {}
_session_event_meta_warm_inflight: set[str] = set()
_SESSION_EVENT_META_WARM_LIMIT = 20
_sessions_list_response_cache: dict[
    tuple,
    tuple[float, bytes, tuple[int, int, int]],
] = {}
_session_summaries_response_cache: dict[
    tuple,
    tuple[float, bytes, tuple[int, int, int]],
] = {}
_virtual_sessions_recent_refresh_task: asyncio.Task | None = None
_session_list_user_prefs_cache: tuple[float, tuple[bool, str, bool]] | None = None
_SESSIONS_LIST_RESPONSE_TTL_SECONDS = 15.0
_SESSION_LIST_USER_PREFS_TTL_SECONDS = 1.0
_SESSION_LIST_CONTENT_SEARCH_MAX_WAIT_SECONDS = 0.05
_SESSION_LIST_SEARCH_MIN_CANDIDATES = 200
_machine_nodes_enabled_cache: tuple[float, bool] | None = None
_machine_nodes_enabled_refresh_task: asyncio.Task | None = None
_MACHINE_NODES_ENABLED_TTL_SECONDS = 2.0


def _session_event_file_fingerprint(root_id: str) -> tuple[int, int]:
    path = event_ingester._events_path(root_id)
    try:
        st = path.stat()
    except FileNotFoundError:
        return (0, 0)
    return (int(st.st_mtime_ns), int(st.st_size))


def _session_event_meta(root_id: str) -> tuple[bool, int, dict[str, int]]:
    fingerprint = _session_event_file_fingerprint(root_id)
    cached = _session_event_meta_cache.get(root_id)
    if cached is not None and cached[0] == fingerprint:
        return cached[1], cached[2], dict(cached[3])

    has_events, barrier_seq, max_context = event_ingester.session_event_meta(root_id)
    _session_event_meta_cache[root_id] = (
        fingerprint,
        has_events,
        barrier_seq,
        dict(max_context),
    )
    return has_events, barrier_seq, dict(max_context)


def _session_event_meta_cache_fresh(root_id: str) -> bool:
    cached = _session_event_meta_cache.get(root_id)
    return cached is not None and cached[0] == _session_event_file_fingerprint(root_id)


def _session_event_meta_roots_for_page(page: list[dict]) -> list[str]:
    root_ids: list[str] = []
    seen: set[str] = set()
    for session in page:
        if len(root_ids) >= _SESSION_EVENT_META_WARM_LIMIT:
            break
        if session.get("node_id") not in (None, "primary"):
            continue
        root_id = session.get("id")
        if not isinstance(root_id, str) or not root_id or root_id in seen:
            continue
        if int(session.get("message_count") or 0) <= 0:
            continue
        seen.add(root_id)
        root_ids.append(root_id)
    return root_ids


async def _warm_session_event_meta_roots(root_ids: list[str]) -> None:
    pending: list[str] = []
    for root_id in root_ids:
        if root_id in _session_event_meta_warm_inflight:
            continue
        _session_event_meta_warm_inflight.add(root_id)
        pending.append(root_id)
    if not pending:
        return

    try:
        await asyncio.to_thread(_warm_session_event_meta_roots_sync, pending)
    finally:
        for root_id in pending:
            _session_event_meta_warm_inflight.discard(root_id)


def _warm_session_event_meta_roots_sync(root_ids: list[str]) -> None:
    for root_id in root_ids:
        try:
            _session_event_meta(root_id)
        except Exception:
            logger.debug("session event meta warm failed for %s", root_id, exc_info=True)


def _schedule_session_event_meta_warm(page: list[dict]) -> None:
    root_ids = _session_event_meta_roots_for_page(page)
    if root_ids:
        task = asyncio.create_task(_warm_session_event_meta_roots(root_ids))
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)


def _machine_nodes_enabled_cached() -> bool:
    global _machine_nodes_enabled_cache, _machine_nodes_enabled_refresh_task
    now = time.monotonic()
    cached = _machine_nodes_enabled_cache
    if (
        cached is not None
        and now - cached[0] <= _MACHINE_NODES_ENABLED_TTL_SECONDS
    ):
        return cached[1]
    if cached is not None:
        if _machine_nodes_enabled_refresh_task is None or _machine_nodes_enabled_refresh_task.done():
            async def _refresh() -> None:
                global _machine_nodes_enabled_cache
                try:
                    enabled = await asyncio.to_thread(
                        extension_store.is_extension_runtime_ready,
                        extension_store.extension_id_for_role('machine-nodes'),
                    )
                except Exception:
                    logger.debug("machine nodes enabled refresh failed", exc_info=True)
                    return
                _machine_nodes_enabled_cache = (time.monotonic(), enabled)

            _machine_nodes_enabled_refresh_task = asyncio.create_task(_refresh())
        return cached[1]
    enabled = extension_store.is_extension_runtime_ready(
        extension_store.extension_id_for_role('machine-nodes'),
    )
    _machine_nodes_enabled_cache = (now, enabled)
    return enabled


def _accepts_gzip(accept_encoding: str) -> bool:
    gzip_quality: float | None = None
    wildcard_quality: float | None = None
    for entry in accept_encoding.lower().split(","):
        encoding, *params = entry.strip().split(";")
        if encoding not in {"gzip", "*"}:
            continue
        quality = 1.0
        quality_seen = False
        for param in params:
            key, separator, value = param.strip().partition("=")
            if key != "q":
                continue
            if quality_seen or not separator:
                return False
            quality_seen = True
            if not re.fullmatch(r"(?:0(?:\.\d{0,3})?|1(?:\.0{0,3})?)", value):
                return False
            quality = float(value)
        if encoding == "gzip":
            gzip_quality = quality
        else:
            wildcard_quality = quality
    if gzip_quality is not None:
        return gzip_quality > 0
    return wildcard_quality is not None and wildcard_quality > 0


def _json_response_maybe_gzip(
    content: bytes, accept_encoding: str, *, perf_prefix: str = "sessions.list",
) -> Response:
    if len(content) < 1024:
        return Response(content=content, media_type="application/json")
    if not _accepts_gzip(accept_encoding):
        return Response(
            content=content,
            media_type="application/json",
            headers={"Vary": "Accept-Encoding"},
        )
    with perf.timed(f"{perf_prefix}.response_gzip"):
        compressed = gzip.compress(content, compresslevel=4, mtime=0)
    perf.record_count(f"{perf_prefix}.response_gzip.input_bytes", len(content))
    perf.record_count(f"{perf_prefix}.response_gzip.output_bytes", len(compressed))
    return Response(
        content=compressed,
        media_type="application/json",
        headers={
            "Content-Encoding": "gzip",
            "Vary": "Accept-Encoding",
        },
    )


def _serialize_response(
    value: dict,
    accept_encoding: str,
    *,
    perf_prefix: str = "sessions.list",
) -> tuple[bytes, Response]:
    with perf.timed(f"{perf_prefix}.response_serialize"):
        content = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    return content, _json_response_maybe_gzip(
        content, accept_encoding, perf_prefix=perf_prefix,
    )


async def json_response_off_loop(
    value: dict,
    accept_encoding: str,
    *,
    executor: HotPathExecutor = session_list_path,
    perf_prefix: str = "sessions.list",
) -> Response:
    """Serialize and gzip-negotiate a JSON payload on a worker thread.

    Response serialization of large payloads is CPU-bound and must never
    run on the event-loop thread. Serialization overlaps event-loop
    execution: callers must hand over a payload whose nested values are
    owned or replaced wholesale, never mutated in place, while this runs.
    """
    _content, response = await executor.run(
        f"{perf_prefix}.response_serialize.worker",
        _serialize_response,
        value,
        accept_encoding,
        perf_prefix=perf_prefix,
    )
    return response


async def gzip_response_off_loop(
    content: bytes,
    accept_encoding: str,
    *,
    executor: HotPathExecutor = session_list_path,
    perf_prefix: str = "sessions.list",
) -> Response:
    """Gzip-negotiate already-serialized JSON bytes on a worker thread."""
    if len(content) < 1024 or not _accepts_gzip(accept_encoding):
        return _json_response_maybe_gzip(
            content, accept_encoding, perf_prefix=perf_prefix,
        )
    return await executor.run(
        f"{perf_prefix}.response_gzip.worker",
        _json_response_maybe_gzip,
        content,
        accept_encoding,
        perf_prefix=perf_prefix,
    )


async def _sessions_list_response_maybe_cache(
    cache_key: tuple,
    value: dict,
    *,
    cache_response: bool,
    accept_encoding: str,
) -> Response:
    if cache_response:
        return await _sessions_list_cache_put(cache_key, value, accept_encoding)
    return await json_response_off_loop(value, accept_encoding)


def _sessions_snapshot_payload(value: dict) -> dict:
    snapshot_complete = session_store.summary_index_snapshot_complete()
    index_warming = (
        not snapshot_complete
        and session_store.summary_index_has_roots_on_disk()
    )
    return {
        **value,
        "snapshot_complete": snapshot_complete or not index_warming,
        "index_warming": index_warming,
    }


def _sessions_list_transient_state_version() -> tuple[int, int, int]:
    return (
        _require_configured()(),
        session_manager.unread_counts_version(),
        user_input_store.pending_counts_version_loaded(),
    )


async def _sessions_list_cache_get(key: tuple, accept_encoding: str) -> Response | None:
    cached = _sessions_list_response_cache.get(key)
    if cached is None:
        return None
    if time.monotonic() - cached[0] > _SESSIONS_LIST_RESPONSE_TTL_SECONDS:
        _sessions_list_response_cache.pop(key, None)
        return None
    if cached[2] != _sessions_list_transient_state_version():
        _sessions_list_response_cache.pop(key, None)
        return None
    return await gzip_response_off_loop(cached[1], accept_encoding)


async def _sessions_list_cache_put(
    key: tuple,
    value: dict,
    accept_encoding: str,
) -> Response:
    # Version captured before the worker hop: `value` reflects state from
    # before the await, so stamping a post-await version could serve stale
    # data as fresh. The pre-await stamp can only over-invalidate.
    state_version = _sessions_list_transient_state_version()
    content, response = await session_list_path.run(
        "sessions.list.response_serialize.worker",
        _serialize_response,
        value,
        accept_encoding,
    )
    if len(_sessions_list_response_cache) >= 64:
        oldest = min(
            _sessions_list_response_cache,
            key=lambda item: _sessions_list_response_cache[item][0],
        )
        _sessions_list_response_cache.pop(oldest, None)
    _sessions_list_response_cache[key] = (
        time.monotonic(),
        content,
        state_version,
    )
    return response


async def _session_summaries_cache_get(
    key: tuple,
    accept_encoding: str,
) -> Response | None:
    cached = _session_summaries_response_cache.get(key)
    if cached is None:
        return None
    if time.monotonic() - cached[0] > _SESSIONS_LIST_RESPONSE_TTL_SECONDS:
        _session_summaries_response_cache.pop(key, None)
        return None
    return await gzip_response_off_loop(cached[1], accept_encoding)


async def _session_summaries_cache_put(
    key: tuple,
    value: dict,
    accept_encoding: str,
) -> Response:
    content, response = await session_list_path.run(
        "sessions.list.response_serialize.worker",
        _serialize_response,
        value,
        accept_encoding,
    )
    if len(_session_summaries_response_cache) >= 64:
        oldest = min(
            _session_summaries_response_cache,
            key=lambda item: _session_summaries_response_cache[item][0],
        )
        _session_summaries_response_cache.pop(oldest, None)
    _session_summaries_response_cache[key] = (
        time.monotonic(),
        content,
        0,
    )
    return response


def _sessions_list_cache_version(search_query: str, search_fields: set[str]) -> tuple[int, int | None] | int:
    if search_query:
        content_generation = None
        if session_store.SEARCH_FIELD_CONTENT in search_fields:
            import session_search_index
            content_generation = session_search_index.generation()
        return (session_store.search_metadata_version(), content_generation)
    return (session_store.summary_version(), virtual_session_store.version_token())


def _sessions_list_content_search_ready(
    search_query: str,
    search_fields: set[str],
    *,
    offset: int,
    limit: int,
) -> bool:
    if (
        not search_query
        or session_store.SEARCH_FIELD_CONTENT not in search_fields
    ):
        return True
    import session_search_index
    return session_search_index.has_cached_result(
        search_query,
        _session_search_candidate_limit(offset, limit),
    )


def _schedule_virtual_sessions_recent_refresh(limit: int) -> None:
    global _virtual_sessions_recent_refresh_task
    existing = _virtual_sessions_recent_refresh_task
    if existing is not None and not existing.done():
        return

    async def _refresh() -> None:
        await asyncio.to_thread(
            virtual_session_store.list_recent,
            limit,
            exclude_id=session_search.ASK_SINGLETON_ID,
        )

    _virtual_sessions_recent_refresh_task = asyncio.create_task(_refresh())


def _session_list_user_prefs() -> tuple[bool, str, bool]:
    global _session_list_user_prefs_cache
    now = time.monotonic()
    cached = _session_list_user_prefs_cache
    if cached is not None and now - cached[0] <= _SESSION_LIST_USER_PREFS_TTL_SECONDS:
        return cached[1]
    prefs = user_prefs.get_all()
    folder_view_enabled = prefs.get(
        "folder_view_enabled",
        user_prefs.DEFAULT_FOLDER_VIEW_ENABLED,
    )
    session_sort = prefs.get("session_sort", user_prefs.DEFAULT_SESSION_SORT)
    if session_sort not in user_prefs.SESSION_SORT_VALUES:
        session_sort = user_prefs.DEFAULT_SESSION_SORT
    session_status_sort = prefs.get(
        "session_status_sort",
        user_prefs.DEFAULT_SESSION_STATUS_SORT,
    )
    resolved = (
        bool(folder_view_enabled),
        session_sort,
        bool(session_status_sort),
    )
    _session_list_user_prefs_cache = (now, resolved)
    return resolved


def _invalidate_session_list_user_prefs_cache() -> None:
    global _session_list_user_prefs_cache
    _session_list_user_prefs_cache = None


def _session_search_candidate_limit(offset: int, limit: int) -> int:
    return max(offset + limit, _SESSION_LIST_SEARCH_MIN_CANDIDATES)
