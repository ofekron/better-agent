"""Session sidebar/list projection, filtering, search, and organization
(folders/tags) routes.

Depends on the coordinator only through the capabilities it actually
needs, bound by the composition root (see `configure`). Response
caching lives in `session_list_cache`. Session deletion (used by the
folder delete-with-cascade paths) is injected rather than owned here:
the session lifecycle itself is still owned by main.py (until a later
extraction) and is also called from not-yet-extracted routes.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from fastapi import APIRouter, Body, Header, HTTPException, Query, Request

import extension_store
import internal_guards
import perf
import session_organization_store
import session_search
import session_status
import session_store
import user_input_store
import user_prefs
import virtual_session_store
from event_bus import BusEvent
from hot_path_executor import hot_path, session_list_path
from i18n import t
from paths import ba_home
from remote_sessions_cache import cache as remote_sessions_cache
import session_list_cache
from session_helpers import session_exists as _session_exists
from session_manager import manager as session_manager, session_matches_project

router = APIRouter()
logger = logging.getLogger(__name__)

_broadcast_global: Optional[Callable[[str, dict], Any]] = None
_cached_state_snapshot: Optional[Callable[[], tuple]] = None
_delete_session_tree: Optional[Callable[[str], Awaitable[bool]]] = None


def configure(
    broadcast_global: Callable[[str, dict], Any],
    cached_state_snapshot: Callable[[], tuple],
    delete_session_tree: Callable[[str], Awaitable[bool]],
) -> None:
    """Bind the coordinator capabilities this router needs."""
    global _broadcast_global, _cached_state_snapshot, _delete_session_tree
    _broadcast_global = broadcast_global
    _cached_state_snapshot = cached_state_snapshot
    _delete_session_tree = delete_session_tree


def _require_configured() -> tuple[
    Callable[[str, dict], Any],
    Callable[[], tuple],
    Callable[[str], Awaitable[bool]],
]:
    if (
        _broadcast_global is None
        or _cached_state_snapshot is None
        or _delete_session_tree is None
    ):
        raise HTTPException(status_code=503, detail="session listing API is not configured")
    return _broadcast_global, _cached_state_snapshot, _delete_session_tree


# ── Sidebar/session-organization broadcast + initial-organization helpers ──

_session_organization_refresh_task: asyncio.Task | None = None
_session_organization_refresh_pending = False


async def broadcast_session_organization_changed(session_ids: list[str] | None = None) -> None:
    broadcast_global, _, _ = _require_configured()
    if session_ids:
        await asyncio.to_thread(session_store.refresh_organization_projection, session_ids)
        await broadcast_global("session_organization_changed", {})
        return

    global _session_organization_refresh_pending, _session_organization_refresh_task
    _session_organization_refresh_pending = True
    if _session_organization_refresh_task is not None and not _session_organization_refresh_task.done():
        return

    async def _refresh_loop() -> None:
        global _session_organization_refresh_pending, _session_organization_refresh_task
        try:
            while _session_organization_refresh_pending:
                _session_organization_refresh_pending = False
                await asyncio.to_thread(session_store.refresh_organization_projection)
                await broadcast_global("session_organization_changed", {})
        except Exception:
            logger.warning("session organization projection refresh failed", exc_info=True)
        finally:
            _session_organization_refresh_task = None
            if _session_organization_refresh_pending:
                _session_organization_refresh_task = asyncio.create_task(_refresh_loop())

    _session_organization_refresh_task = asyncio.create_task(_refresh_loop())


async def apply_initial_session_folder(session_id: str | None, folder_id: str | None) -> None:
    """Assign a folder chosen at creation time. Best-effort: a deleted
    folder or stale id (e.g. an offline-queued create replayed after the
    folder was removed) must never fail the session creation itself."""
    if not session_id or not folder_id:
        return
    try:
        await asyncio.to_thread(
            session_organization_store.set_session_folder,
            session_id,
            folder_id,
        )
        await broadcast_session_organization_changed([session_id])
    except ValueError as e:
        logger.warning("initial folder assignment failed for %s: %s", session_id[:8], e)


def session_organization_input_from_body(
    body: dict,
) -> tuple[str | None, list[str]]:
    folder_id = body.get("folder_id")
    tag_ids = body.get("tag_ids")
    if folder_id is not None and not isinstance(folder_id, str):
        raise HTTPException(status_code=400, detail="folder_id must be a string")
    if tag_ids is not None and not isinstance(tag_ids, list):
        raise HTTPException(status_code=400, detail="tag_ids must be a list")
    if tag_ids is not None and any(not isinstance(tag_id, str) for tag_id in tag_ids):
        raise HTTPException(status_code=400, detail="tag_ids must contain strings")
    return folder_id, tag_ids or []


async def initial_session_organization_from_body(
    body: dict,
) -> tuple[str | None, list[str]]:
    folder_id, tag_ids = session_organization_input_from_body(body)
    try:
        return await asyncio.to_thread(
            session_organization_store.validate_session_organization,
            folder_id,
            tag_ids,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


async def apply_initial_session_organization(
    session_id: str,
    folder_id: str | None,
    tag_ids: list[str],
) -> None:
    if folder_id is None and not tag_ids:
        return
    await asyncio.to_thread(
        session_organization_store.set_session_organization,
        session_id,
        folder_id,
        tag_ids,
    )
    await broadcast_session_organization_changed([session_id])


async def forward_requirement_tags_refreshed(event: BusEvent) -> None:
    await broadcast_session_organization_changed()


# ── Sidebar projection (local session summaries → decorated rows) ──

_SESSION_LIST_SUMMARY_WARM_WAIT_SECONDS = 0.08
_SESSION_LIST_SUMMARY_WARM_MIN_PUBLISHED = 50
_SIDEBAR_PAYLOAD_CACHE_MAX = 4096
_SIDEBAR_DECORATED_CACHE_MAX = 1024

_sidebar_payload_cache: dict[int, tuple[str, dict]] = {}
_sidebar_decorated_cache: dict[tuple, dict] = {}
_sidebar_state_snapshot_cache: tuple[
    tuple[int, int, int],
    tuple[set[str], dict[str, str], dict[str, int], dict[str, int]],
] | None = None
_local_visible_order_cache: dict[
    tuple[str, str | None, int, int, int],
    tuple[list[str], int],
] = {}


def _local_session_summaries_for_sidebar() -> list[dict]:
    # Hide ephemeral working-mode sessions from the sidebar.
    import working_mode as _wm
    with perf.timed("sessions.list.local.summary_warm_wait"):
        session_store.wait_for_summary_index(
            _SESSION_LIST_SUMMARY_WARM_WAIT_SECONDS,
            min_published=_SESSION_LIST_SUMMARY_WARM_MIN_PUBLISHED,
        )
    with perf.timed("sessions.list.local.session_manager"):
        summaries = session_manager.list()
    with perf.timed("sessions.list.local.hide_filter"):
        return [s for s in summaries if not _wm.should_hide_from_sidebar(s)]


def _local_session_summaries_by_ids_for_sidebar(session_ids: list[str]) -> list[dict]:
    import working_mode as _wm
    with perf.timed("sessions.list.search_summary_lookup"):
        summaries = session_store.get_session_summaries_by_ids(session_ids)
    with perf.timed("sessions.list.search_hide_filter"):
        return [s for s in summaries if not _wm.should_hide_from_sidebar(s)]


def _local_session_summaries_by_ids(session_ids: list[str]) -> list[dict]:
    with perf.timed("sessions.list.summary_lookup_by_ids"):
        return session_store.get_session_summaries_by_ids(session_ids)


def _can_page_default_local_visible_order(
    *,
    project_path: str | None,
    search: str | None,
    show_archived: bool,
    file_edit_mode: bool | None,
    folder_ids: set[str],
    tag_ids: set[str],
    provider_ids: set[str],
    model_ids: set[str],
    modes: set[str],
    sources: set[str],
    content_scores: dict[str, int],
    status_filter: bool,
) -> bool:
    return (
        not status_filter
        and not (search or "").strip()
        and not show_archived
        and file_edit_mode is None
        and not folder_ids
        and not tag_ids
        and not provider_ids
        and not model_ids
        and not modes
        and not sources
        and not content_scores
    )


def _local_visible_order_page_ids(
    sort_by: str,
    project_path: str | None,
    offset: int,
    limit: int,
    expected_summary_index_version: int,
    expected_summary_order_version: int,
) -> tuple[list[str], int] | None:
    import working_mode as _wm
    key = (sort_by, project_path, offset, limit, expected_summary_order_version)
    cached = _local_visible_order_cache.get(key)
    if cached is not None:
        perf.record("sessions.list.local.visible_order_cache.hit", 1.0)
        return cached
    perf.record("sessions.list.local.visible_order_cache.miss", 1.0)
    ordered_ids = session_manager.ordered_summary_ids(sort_by)
    page_ids: list[str] = []
    total = 0
    end = offset + limit
    with perf.timed("sessions.list.local.visible_order_build"):
        for ordered_id in ordered_ids:
            summary = session_store.get_indexed_session_summary_if_current(
                ordered_id,
                expected_summary_index_version,
            )
            if summary is None:
                return None
            if project_path is not None and not session_matches_project(summary, project_path):
                continue
            if summary.get("archived") or _wm.should_hide_from_sidebar(summary):
                continue
            if offset <= total < end:
                sid = summary.get("id")
                if sid:
                    page_ids.append(str(sid))
            total += 1
    if len(_local_visible_order_cache) >= 8:
        _local_visible_order_cache.pop(next(iter(_local_visible_order_cache)), None)
    cached = (page_ids, total)
    _local_visible_order_cache[key] = cached
    return cached


def _local_session_page_for_sidebar_preserving_order(
    *,
    sort_by: str,
    offset: int,
    limit: int,
    project_path: str | None,
    search: str | None,
    show_archived: bool,
    file_edit_mode: bool | None,
    folder_ids: set[str],
    tag_ids: set[str],
    provider_ids: set[str],
    model_ids: set[str],
    modes: set[str],
    sources: set[str],
    content_scores: dict[str, int],
    status_gate: Callable[[dict], bool] | None = None,
) -> tuple[list[dict], int]:
    import working_mode as _wm
    if _can_page_default_local_visible_order(
        project_path=project_path,
        search=search,
        show_archived=show_archived,
        file_edit_mode=file_edit_mode,
        folder_ids=folder_ids,
        tag_ids=tag_ids,
        provider_ids=provider_ids,
        model_ids=model_ids,
        modes=modes,
        sources=sources,
        content_scores=content_scores,
        status_filter=status_gate is not None,
    ):
        with perf.timed("sessions.list.local.visible_order_page"):
            expected_summary_index_version = session_store.summary_index_version()
            expected_summary_order_version = session_store.summary_order_version()
            visible_page = _local_visible_order_page_ids(
                sort_by,
                project_path,
                offset,
                limit,
                expected_summary_index_version,
                expected_summary_order_version,
            )
            if visible_page is None:
                perf.record("sessions.list.local.visible_order_page.indexed_miss", 1.0)
            else:
                page_ids, total = visible_page
                indexed_page = session_store.get_indexed_session_summaries_by_ids_if_current(
                    page_ids,
                    expected_summary_index_version,
                )
                if indexed_page is not None:
                    perf.record("sessions.list.local.visible_order_page.indexed_hit", 1.0)
                    return indexed_page, total
                perf.record("sessions.list.local.visible_order_page.indexed_miss", 1.0)
                return session_store.get_session_summaries_by_ids(page_ids), total
    with perf.timed("sessions.list.local.ordered_ids"):
        ordered_ids = session_manager.ordered_summary_ids(sort_by)
    page_ids: list[str] = []
    total = 0
    end = offset + limit
    with perf.timed("sessions.list.local.ordered_filter"):
        for session in session_store.get_indexed_session_summaries_by_ids(ordered_ids):
            if _wm.should_hide_from_sidebar(session):
                continue
            if not _session_matches_list_filters(
                session,
                project_path=project_path,
                search=search,
                show_archived=show_archived,
                file_edit_mode=file_edit_mode,
                folder_ids=folder_ids,
                tag_ids=tag_ids,
                provider_ids=provider_ids,
                model_ids=model_ids,
                modes=modes,
                sources=sources,
                content_scores=content_scores,
                status_gate=status_gate,
            ):
                continue
            if offset <= total < end:
                sid = session.get("id")
                if sid:
                    page_ids.append(str(sid))
            total += 1
    with perf.timed("sessions.list.local.ordered_page_lookup"):
        return session_store.get_session_summaries_by_ids(page_ids), total


def _root_session_file_path(session_id: str) -> str:
    return f"{_root_sessions_dir_path()}/{session_id}.json"


_root_sessions_dir_path_cache: tuple[str, str] | None = None


def _root_sessions_dir_path() -> str:
    global _root_sessions_dir_path_cache
    home = str(ba_home())
    cached = _root_sessions_dir_path_cache
    if cached is not None and cached[0] == home:
        return cached[1]
    sessions_dir = str(Path(home) / "sessions")
    _root_sessions_dir_path_cache = (home, sessions_dir)
    return sessions_dir


_SIDEBAR_WORKING_MODE_META_KEYS = {
    "project_cwd",
    "file_paths",
    "temp_file_path",
    "parent_session_id",
    "mode",
    "persistent",
}


def _sidebar_session_payload(session: dict) -> dict:
    sid = session.get("id")
    cache_key = id(session)
    if isinstance(sid, str):
        cached = _sidebar_payload_cache.get(cache_key)
        if cached is not None and cached[0] == sid:
            return cached[1]
    payload = {
        key: value
        for key, value in session.items()
        if key != "first_prompt"
    }
    meta = payload.get("working_mode_meta")
    if isinstance(meta, dict):
        payload["working_mode_meta"] = {
            key: meta[key]
            for key in _SIDEBAR_WORKING_MODE_META_KEYS
            if key in meta
        }
    if isinstance(sid, str):
        if len(_sidebar_payload_cache) >= _SIDEBAR_PAYLOAD_CACHE_MAX:
            _sidebar_payload_cache.pop(next(iter(_sidebar_payload_cache)), None)
        _sidebar_payload_cache[cache_key] = (sid, payload)
    return payload


def _sidebar_state_snapshot() -> tuple[set[str], dict[str, str], dict[str, int], dict[str, int]]:
    global _sidebar_state_snapshot_cache
    _, cached_state_snapshot, _ = _require_configured()
    version = session_list_cache._sessions_list_transient_state_version()
    cached = _sidebar_state_snapshot_cache
    if cached is not None and cached[0] == version:
        return cached[1]
    running_sids, monitoring_by_sid = cached_state_snapshot()
    unread_by_sid = session_manager.unread_counts_snapshot()
    pending_input_by_sid = user_input_store.pending_counts_by_session()
    snapshot = running_sids, monitoring_by_sid, unread_by_sid, pending_input_by_sid
    _sidebar_state_snapshot_cache = (
        session_list_cache._sessions_list_transient_state_version(),
        snapshot,
    )
    return snapshot


def _decorate_local_sidebar_sessions(
    sessions: list[dict],
    state_snapshot: tuple[set[str], dict[str, str], dict[str, int], dict[str, int]] | None = None,
) -> list[dict]:
    local: list[dict] = []
    with perf.timed("sessions.list.local.decorate"):
        with perf.timed("sessions.list.local.decorate.state"):
            if state_snapshot is None:
                running_sids, monitoring_by_sid, unread_by_sid, pending_input_by_sid = (
                    _sidebar_state_snapshot()
                )
            else:
                running_sids, monitoring_by_sid, unread_by_sid, pending_input_by_sid = (
                    state_snapshot
                )
            sessions_dir = _root_sessions_dir_path()
            summary_version = session_store.summary_index_version()
        for s in sessions:
            with perf.timed("sessions.list.local.decorate.payload"):
                sidebar_session = _sidebar_session_payload(s)
            node_id = s.get("node_id") or "primary"
            if node_id != "primary" or s.get("source") == "virtual":
                local.append(sidebar_session)
                continue
            sid = s.get("id")
            if not sid:
                local.append(sidebar_session)
                continue
            running = sid in running_sids
            monitoring_state = monitoring_by_sid.get(sid, "stopped")
            unread_count = unread_by_sid.get(sid, 0)
            pending_user_input_count = pending_input_by_sid.get(sid, 0)
            has_error = bool(s.get("unseen_error"))
            file_path = f"{sessions_dir}/{sid}.json"
            decorated_cache_key = (
                sid,
                summary_version,
                running,
                monitoring_state,
                unread_count,
                pending_user_input_count,
                has_error,
                file_path,
            )
            cached_decorated = _sidebar_decorated_cache.get(decorated_cache_key)
            if cached_decorated is not None:
                perf.record("sessions.list.local.decorate.row_cache.hit", 1.0)
                local.append(cached_decorated)
                continue
            perf.record("sessions.list.local.decorate.row_cache.miss", 1.0)
            # Enrich with transient running flag + lazy-hydrated unread.
            # `peek_unread_count` returns None on cache miss — we surface
            # 0 in that case so the sidebar renders immediately.
            # `is_running_cached` / `monitoring_state_cached` read from
            # the background-tick cache — no os.kill PID probing on the
            # event loop. Cache is refreshed every 2 s by the background
            # tick thread; stale by up to 2 s, acceptable for badges.
            decorated = {
                **sidebar_session,
                "is_running": running,
                "monitoring_state": monitoring_state,
                "unread_count": unread_count,
                "pending_user_input_count": pending_user_input_count,
                "has_error": has_error,
                "file_path": f"{sessions_dir}/{sid}.json",
            }
            if len(_sidebar_decorated_cache) >= _SIDEBAR_DECORATED_CACHE_MAX:
                _sidebar_decorated_cache.pop(next(iter(_sidebar_decorated_cache)), None)
            _sidebar_decorated_cache[decorated_cache_key] = decorated
            local.append(decorated)
    return local


def sidebar_stats_payload(session: dict) -> dict:
    return {
        "token_usage_total": session.get("token_usage_total"),
        "token_usage_last": session.get("token_usage_last"),
        "context_window": session.get("context_window"),
    }


def _local_sessions_for_sidebar() -> list[dict]:
    return _decorate_local_sidebar_sessions(_local_session_summaries_for_sidebar())


_session_org_facets_cache: dict[
    tuple[str | None, int, tuple[int, int] | None],
    dict[str, Any],
] = {}


_TAG_SOURCE_OWNERS = {
    session_organization_store.TAG_SOURCE_AUTO_TAGGING: extension_store.BUILTIN_AUTO_TAGGING_EXTENSION_ID,
    session_organization_store.TAG_SOURCE_REQUIREMENT_ANALYSIS: extension_store.extension_id_for_role('requirements'),
}


def _require_tag_source_owner(source: object, token: str) -> None:
    source_name = str(source or session_organization_store.TAG_SOURCE_MANUAL).strip()
    owner = _TAG_SOURCE_OWNERS.get(source_name)
    if owner and internal_guards.internal_authority_extension_id() != owner:
        raise HTTPException(status_code=403, detail=f"{source_name} tag source is owned by {owner}")


# ── Status buckets, filtering, sorting, and search-score plumbing ──

#: Status buckets a sidebar session can fall into, highest priority first.
#: The single source of truth for both the status sort order (rank = reverse
#: index) and the status include/exclude filter.
SESSION_STATUS_KEYS: tuple[str, ...] = (
    "error",
    "needs_decision",
    "unread",
    "open_work",
    "running",
    "all_done",
    "idle",
)


def _session_status_key(
    session: dict,
    monitoring_by_sid: dict[str, str],
    unread_by_sid: dict[str, int],
    pending_input_by_sid: dict[str, int] | None = None,
) -> str:
    """Which SESSION_STATUS_KEYS bucket this session is in.

    A priority projection of the independent dimensions in
    `session_status.compute` — the bucket is what the sidebar sorts and
    filters on, so exactly one wins per session.
    """
    status = session_status.compute(
        session, monitoring_by_sid, unread_by_sid, pending_input_by_sid
    )
    if status.errored:
        return "error"
    if status.waiting_for_user:
        return "needs_decision"
    if status.unread and not status.busy:
        return "unread"
    if status.open_work:
        return "open_work"
    if status.busy:
        return "running"
    if status.is_done:
        return "all_done"
    return "idle"


def _session_status_rank(
    session: dict,
    monitoring_by_sid: dict[str, str],
    unread_by_sid: dict[str, int],
    pending_input_by_sid: dict[str, int] | None = None,
) -> int:
    """Status bucket for the status-sort option. Higher sorts first."""
    key = _session_status_key(
        session,
        monitoring_by_sid,
        unread_by_sid,
        pending_input_by_sid,
    )
    return len(SESSION_STATUS_KEYS) - 1 - SESSION_STATUS_KEYS.index(key)


def _session_list_sort_key(
    session: dict,
    folder_view: bool,
    sort_by: str,
    *,
    status_sort: bool = False,
    monitoring_by_sid: dict[str, str] | None = None,
    unread_by_sid: dict[str, int] | None = None,
    pending_input_by_sid: dict[str, int] | None = None,
) -> tuple:
    # empty-new and pinned stay above status; status is the strongest key
    # below them, time the tie-break: (isEmpty, pinned, [status], ts).
    inner: tuple = (
        int(session.get("message_count", 0) or 0) == 0,
        bool(session.get("pinned", False)),
    )
    if status_sort:
        inner += (
            _session_status_rank(
                session,
                monitoring_by_sid or {},
                unread_by_sid or {},
                pending_input_by_sid or {},
            ),
        )
    inner += (session_store.timestamp_sort_value(session.get(sort_by)),)
    if not folder_view:
        return inner
    # folderized sessions first when folder view is on
    return (bool(session.get("folder_id")),) + inner


def _split_session_filter(value: str | None) -> set[str]:
    if not value:
        return set()
    return {item.strip() for item in value.split(",") if item.strip()}


def _split_session_statuses(value: str | None) -> frozenset[str]:
    """Parse a comma-separated status list, dropping unknown buckets."""
    return frozenset(
        item for item in _split_session_filter(value) if item in SESSION_STATUS_KEYS
    )


def _session_status_gate(
    include: frozenset[str],
    exclude: frozenset[str],
    snapshot: tuple[set[str], dict[str, str], dict[str, int], dict[str, int]],
) -> Callable[[dict], bool] | None:
    """Predicate keeping only sessions whose status bucket is shown.

    `include` empty means "every bucket"; `exclude` always wins. Returns
    None when nothing is filtered, which every caller treats as "no status
    filter" (and which keeps the store-order fast paths eligible).
    """
    if not include and not exclude:
        return None
    _, monitoring_by_sid, unread_by_sid, pending_input_by_sid = snapshot

    def gate(session: dict) -> bool:
        key = _session_status_key(
            session,
            monitoring_by_sid,
            unread_by_sid,
            pending_input_by_sid,
        )
        if key in exclude:
            return False
        return not include or key in include

    return gate


def _split_session_search_fields(value: str | None) -> set[str]:
    if value is None:
        return set(session_store.DEFAULT_SEARCH_FIELDS)
    return {
        item
        for item in (part.strip() for part in value.split(","))
        if item in session_store.SEARCH_FIELDS
    }


def _session_filter_list_from_body(body: dict, key: str) -> set[str]:
    value = body.get(key)
    if value is None:
        return set()
    if not isinstance(value, list):
        raise HTTPException(status_code=400, detail=f"{key} must be a list")
    out: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            raise HTTPException(status_code=400, detail=f"{key} must contain strings")
        stripped = item.strip()
        if stripped:
            out.add(stripped)
    return out


def _session_filter_str_from_body(body: dict, key: str) -> str | None:
    value = body.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise HTTPException(status_code=400, detail=f"{key} must be a string")
    stripped = value.strip()
    return stripped or None


def _session_filter_bool_from_body(body: dict, key: str) -> bool:
    value = body.get(key, False)
    if not isinstance(value, bool):
        raise HTTPException(status_code=400, detail=f"{key} must be a boolean")
    return value


def _session_filter_optional_bool_from_body(body: dict, key: str) -> bool | None:
    value = body.get(key)
    if value is None:
        return None
    if not isinstance(value, bool):
        raise HTTPException(status_code=400, detail=f"{key} must be a boolean")
    return value


def _session_list_filter_args_from_body(body: dict | None) -> dict[str, Any]:
    if body is None:
        body = {}
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")
    allowed = {
        "project_path",
        "search",
        "show_archived",
        "file_edit_mode",
        "folder_id",
        "tag_ids",
        "provider_ids",
        "model_ids",
        "modes",
        "sources",
    }
    unknown = set(body) - allowed
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"unexpected fields: {', '.join(sorted(unknown))}",
        )
    return {
        "project_path": _session_filter_str_from_body(body, "project_path"),
        "search": _session_filter_str_from_body(body, "search"),
        "show_archived": _session_filter_bool_from_body(body, "show_archived"),
        "file_edit_mode": _session_filter_optional_bool_from_body(body, "file_edit_mode"),
        "folder_id": _session_filter_str_from_body(body, "folder_id"),
        "tag_ids": _session_filter_list_from_body(body, "tag_ids"),
        "provider_ids": _session_filter_list_from_body(body, "provider_ids"),
        "model_ids": _session_filter_list_from_body(body, "model_ids"),
        "modes": _session_filter_list_from_body(body, "modes"),
        "sources": _session_filter_list_from_body(body, "sources"),
    }


def _session_matches_list_filters(
    session: dict,
    *,
    project_path: str | None,
    search: str | None,
    show_archived: bool,
    file_edit_mode: bool | None,
    folder_ids: set[str],
    tag_ids: set[str],
    provider_ids: set[str],
    model_ids: set[str],
    modes: set[str],
    sources: set[str],
    content_scores: dict[str, int] | None = None,
    status_gate: Callable[[dict], bool] | None = None,
) -> bool:
    if not show_archived and session.get("archived"):
        return False
    if status_gate is not None and not status_gate(session):
        return False
    if file_edit_mode is not None:
        is_file_edit_mode = session.get("working_mode") == "file_editing"
        if is_file_edit_mode != file_edit_mode:
            return False
    if not session_matches_project(session, project_path):
        return False
    if folder_ids and (session.get("folder_id") or "") not in folder_ids:
        return False
    if provider_ids and (session.get("provider_id") or "") not in provider_ids:
        return False
    if model_ids and (session.get("model") or "") not in model_ids:
        return False
    if modes and (session.get("orchestration_mode") or "team") not in modes:
        return False
    if sources:
        source = session.get("source") or "web"
        user_aware_bucket = "user" if session.get("user_initiated") else "system"
        if source not in sources and user_aware_bucket not in sources:
            return False
    if tag_ids:
        filter_ids = session.get("tag_filter_ids")
        if not isinstance(filter_ids, list):
            filter_ids = _session_tag_filter_ids(session)
        if not tag_ids.issubset(filter_ids):
            return False
    q = (search or "").strip().lower()
    if q:
        if not (content_scores and session.get("id") in content_scores):
            return False
    return True


def _session_tag_filter_ids(session: dict) -> set[str]:
    ids: set[str] = set()
    for tag in session.get("session_tags") or []:
        if isinstance(tag, dict) and isinstance(tag.get("id"), str):
            ids.add(tag["id"])
    for tag in session.get("requirement_tags") or []:
        if not isinstance(tag, dict):
            continue
        kind = tag.get("kind")
        tag_id = tag.get("id")
        if isinstance(kind, str) and isinstance(tag_id, str):
            ids.add(f"req:{kind}:{tag_id}")
    return ids


def _session_filtered_sort_key(
    session: dict,
    *,
    folder_view: bool,
    search: str | None,
    content_scores: dict[str, int],
    sort_by: str,
    status_sort: bool = False,
    monitoring_by_sid: dict[str, str] | None = None,
    unread_by_sid: dict[str, int] | None = None,
    pending_input_by_sid: dict[str, int] | None = None,
) -> tuple:
    # In search mode relevance dominates; status only breaks ties below the
    # search score: (pinned, score>0, score, [status], ts).
    search_score = content_scores.get(str(session.get("id") or ""), 0)
    inner: tuple = (
        bool(session.get("pinned", False)),
        search_score > 0,
        search_score,
    )
    if status_sort:
        inner += (
            _session_status_rank(
                session,
                monitoring_by_sid or {},
                unread_by_sid or {},
                pending_input_by_sid or {},
            ),
        )
    inner += (session_store.timestamp_sort_value(session.get(sort_by)),)
    if not folder_view:
        return inner
    # folderized sessions first when folder view is on
    return (bool(session.get("folder_id")),) + inner


def _filter_sort_sessions_for_list(
    sessions: list[dict],
    *,
    project_path: str | None,
    search: str | None,
    show_archived: bool,
    file_edit_mode: bool | None,
    folder_ids: set[str],
    folder_view: bool,
    tag_ids: set[str],
    provider_ids: set[str],
    model_ids: set[str],
    modes: set[str],
    sources: set[str],
    content_scores: dict[str, int],
    sort_by: str,
    status_sort: bool = False,
    status_gate: Callable[[dict], bool] | None = None,
    state_snapshot: tuple[set[str], dict[str, str], dict[str, int], dict[str, int]] | None = None,
) -> list[dict]:
    out = [
        session for session in sessions
        if _session_matches_list_filters(
            session,
            project_path=project_path,
            search=search,
            show_archived=show_archived,
            file_edit_mode=file_edit_mode,
            folder_ids=folder_ids,
            tag_ids=tag_ids,
            provider_ids=provider_ids,
            model_ids=model_ids,
            modes=modes,
            sources=sources,
            content_scores=content_scores,
            status_gate=status_gate,
        )
    ]
    # Snapshots read ONCE per request (not per-session) — the same cheap
    # caches the decorate step uses. monitoring is the 2s background-tick
    # cache; the frontend's live registry rank is the authoritative interim
    # view between fetches (see useSession debounced refetch).
    monitoring_by_sid: dict[str, str] = {}
    unread_by_sid: dict[str, int] = {}
    pending_input_by_sid: dict[str, int] = {}
    if status_sort:
        if state_snapshot is None:
            state_snapshot = _sidebar_state_snapshot()
        _, monitoring_by_sid, unread_by_sid, pending_input_by_sid = state_snapshot
    out.sort(
        key=(
            (lambda session: _session_filtered_sort_key(
                session,
                folder_view=folder_view,
                search=search,
                content_scores=content_scores,
                sort_by=sort_by,
                status_sort=status_sort,
                monitoring_by_sid=monitoring_by_sid,
                unread_by_sid=unread_by_sid,
                pending_input_by_sid=pending_input_by_sid,
            ))
            if search and search.strip()
            else (lambda session: _session_list_sort_key(
                session,
                folder_view,
                sort_by,
                status_sort=status_sort,
                monitoring_by_sid=monitoring_by_sid,
                unread_by_sid=unread_by_sid,
                pending_input_by_sid=pending_input_by_sid,
            ))
        ),
        reverse=True,
    )
    return out


def _filter_sort_page_for_list(
    sessions: list[dict],
    *,
    offset: int,
    limit: int,
    project_path: str | None,
    search: str | None,
    show_archived: bool,
    file_edit_mode: bool | None,
    folder_ids: set[str],
    folder_view: bool,
    tag_ids: set[str],
    provider_ids: set[str],
    model_ids: set[str],
    modes: set[str],
    sources: set[str],
    content_scores: dict[str, int],
    sort_by: str,
    status_sort: bool = False,
    status_gate: Callable[[dict], bool] | None = None,
    state_snapshot: tuple[set[str], dict[str, str], dict[str, int], dict[str, int]] | None = None,
) -> tuple[list[dict], int]:
    import heapq

    monitoring_by_sid: dict[str, str] = {}
    unread_by_sid: dict[str, int] = {}
    pending_input_by_sid: dict[str, int] = {}
    if status_sort:
        if state_snapshot is None:
            state_snapshot = _sidebar_state_snapshot()
        _, monitoring_by_sid, unread_by_sid, pending_input_by_sid = state_snapshot

    def _sort_key(session: dict) -> tuple:
        if search and search.strip():
            return _session_filtered_sort_key(
                session,
                folder_view=folder_view,
                search=search,
                content_scores=content_scores,
                sort_by=sort_by,
                status_sort=status_sort,
                monitoring_by_sid=monitoring_by_sid,
                unread_by_sid=unread_by_sid,
                pending_input_by_sid=pending_input_by_sid,
            )
        return _session_list_sort_key(
            session,
            folder_view,
            sort_by,
            status_sort=status_sort,
            monitoring_by_sid=monitoring_by_sid,
            unread_by_sid=unread_by_sid,
            pending_input_by_sid=pending_input_by_sid,
        )

    total = 0
    end = offset + limit
    selected: list[tuple[tuple, int, dict]] = []
    for idx, session in enumerate(sessions):
        if not _session_matches_list_filters(
            session,
            project_path=project_path,
            search=search,
            show_archived=show_archived,
            file_edit_mode=file_edit_mode,
            folder_ids=folder_ids,
            tag_ids=tag_ids,
            provider_ids=provider_ids,
            model_ids=model_ids,
            modes=modes,
            sources=sources,
            content_scores=content_scores,
            status_gate=status_gate,
        ):
            continue
        total += 1
        item = (_sort_key(session), -idx, session)
        if 0 < end <= len(selected):
            if (item[0], item[1]) > (selected[0][0], selected[0][1]):
                heapq.heapreplace(selected, item)
        else:
            heapq.heappush(selected, item)

    selected.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [session for _, __, session in selected[offset:end]], total


def _filter_sessions_for_list_preserving_order(
    sessions: list[dict],
    *,
    project_path: str | None,
    search: str | None,
    show_archived: bool,
    file_edit_mode: bool | None,
    folder_ids: set[str],
    tag_ids: set[str],
    provider_ids: set[str],
    model_ids: set[str],
    modes: set[str],
    sources: set[str],
    content_scores: dict[str, int],
    status_gate: Callable[[dict], bool] | None = None,
) -> list[dict]:
    return [
        session for session in sessions
        if _session_matches_list_filters(
            session,
            project_path=project_path,
            search=search,
            show_archived=show_archived,
            file_edit_mode=file_edit_mode,
            folder_ids=folder_ids,
            tag_ids=tag_ids,
            provider_ids=provider_ids,
            model_ids=model_ids,
            modes=modes,
            sources=sources,
            content_scores=content_scores,
            status_gate=status_gate,
        )
    ]


def _filter_page_for_list_preserving_order(
    sessions: list[dict],
    *,
    offset: int,
    limit: int,
    project_path: str | None,
    search: str | None,
    show_archived: bool,
    file_edit_mode: bool | None,
    folder_ids: set[str],
    tag_ids: set[str],
    provider_ids: set[str],
    model_ids: set[str],
    modes: set[str],
    sources: set[str],
    content_scores: dict[str, int],
    status_gate: Callable[[dict], bool] | None = None,
) -> tuple[list[dict], int]:
    page: list[dict] = []
    total = 0
    end = offset + limit
    for session in sessions:
        if not _session_matches_list_filters(
            session,
            project_path=project_path,
            search=search,
            show_archived=show_archived,
            file_edit_mode=file_edit_mode,
            folder_ids=folder_ids,
            tag_ids=tag_ids,
            provider_ids=provider_ids,
            model_ids=model_ids,
            modes=modes,
            sources=sources,
            content_scores=content_scores,
            status_gate=status_gate,
        ):
            continue
        if offset <= total < end:
            page.append(session)
        total += 1
    return page, total


def _can_preserve_summary_order(
    *,
    search_query: str,
    appended_virtual_sessions: bool,
    folder_view: bool,
    sort_by: str,
    status_sort: bool,
    status_filter: bool,
) -> bool:
    return (
        not status_filter
        and not search_query
        and not appended_virtual_sessions
        and not folder_view
        and sort_by in {"updated_at", "last_user_prompt_at", "last_opened_at"}
        and not status_sort
    )


def _can_page_local_summary_order(
    *,
    search_query: str,
    folder_view: bool,
    sort_by: str,
    status_sort: bool,
    status_filter: bool,
) -> bool:
    return (
        not status_filter
        and not search_query
        and not folder_view
        and sort_by in {"updated_at", "last_user_prompt_at", "last_opened_at"}
        and not status_sort
    )


def _can_page_default_updated_at_with_virtual(
    *,
    search_query: str,
    project_path: str | None,
    show_archived: bool,
    file_edit_mode: bool | None,
    folder_ids: set[str],
    folder_view: bool,
    tag_ids: set[str],
    provider_ids: set[str],
    model_ids: set[str],
    modes: set[str],
    sources: set[str],
    sort_by: str,
    status_sort: bool,
    status_filter: bool,
) -> bool:
    return (
        not status_filter
        and not search_query
        and project_path is None
        and not show_archived
        and file_edit_mode is None
        and not folder_ids
        and not folder_view
        and not tag_ids
        and not provider_ids
        and not model_ids
        and not modes
        and not sources
        and sort_by == "updated_at"
        and not status_sort
    )


def _merge_updated_at_page(
    local_sessions: list[dict],
    secondary_sessions: list[dict],
    *,
    offset: int,
    limit: int,
) -> tuple[list[dict], int]:
    page: list[dict] = []
    total = 0
    local_index = 0
    virtual_index = 0
    end = offset + limit
    while local_index < len(local_sessions) or virtual_index < len(secondary_sessions):
        if local_index >= len(local_sessions):
            session = secondary_sessions[virtual_index]
            virtual_index += 1
        elif virtual_index >= len(secondary_sessions):
            session = local_sessions[local_index]
            local_index += 1
        else:
            local_session = local_sessions[local_index]
            virtual_session = secondary_sessions[virtual_index]
            if (
                session_store.timestamp_sort_value(local_session.get("updated_at"))
                >= session_store.timestamp_sort_value(virtual_session.get("updated_at"))
            ):
                session = local_session
                local_index += 1
            else:
                session = virtual_session
                virtual_index += 1
        if session.get("archived"):
            continue
        if offset <= total < end:
            page.append(session)
        total += 1
    return page, total


def _session_filters_may_include_virtual(
    *,
    file_edit_mode: bool | None,
    folder_ids: set[str],
    tag_ids: set[str],
    modes: set[str],
    sources: set[str],
) -> bool:
    if file_edit_mode is True:
        return False
    if folder_ids or tag_ids:
        return False
    if modes and "virtual" not in modes:
        return False
    if sources and not ({"extension", "system"} & sources):
        return False
    return True


def _can_page_local_search_scores(
    *,
    project_path: str | None,
    show_archived: bool,
    file_edit_mode: bool | None,
    folder_ids: set[str],
    folder_view: bool,
    tag_ids: set[str],
    provider_ids: set[str],
    model_ids: set[str],
    modes: set[str],
    sources: set[str],
    sort_by: str,
    status_sort: bool,
    status_filter: bool,
    connected: tuple[str, ...],
) -> bool:
    return (
        not status_filter
        and project_path is None
        and not show_archived
        and file_edit_mode is None
        and not folder_ids
        and not tag_ids
        and not provider_ids
        and not model_ids
        and not modes
        and not sources
        and sort_by in {"updated_at", "last_user_prompt_at", "last_opened_at"}
        and not status_sort
        and not connected
    )


def _build_local_search_page_for_sidebar(
    *,
    offset: int,
    limit: int,
    search_query: str,
    search_fields: str | None,
    sort_by: str,
    folder_view: bool,
) -> tuple[list[dict], int, dict[str, int]]:
    selected_search_fields = _split_session_search_fields(search_fields)
    content_max_wait_seconds = (
        session_list_cache._SESSION_LIST_CONTENT_SEARCH_MAX_WAIT_SECONDS
        if session_store.SEARCH_FIELD_CONTENT in selected_search_fields
        else None
    )
    with perf.timed("sessions.list.search_score_page"):
        score_page, total = session_store.grep_session_score_page(
            search_query,
            selected_search_fields,
            offset=offset,
            limit=limit,
            sort_by=sort_by,
            folder_view=folder_view,
            content_limit=session_list_cache._session_search_candidate_limit(offset, limit),
            content_max_wait_seconds=content_max_wait_seconds,
        )
    scores = dict(score_page)
    page_source = _local_session_summaries_by_ids_for_sidebar(
        [sid for sid, _score in score_page]
    )
    return page_source, total, scores


def _build_local_sessions_page_for_list(
    *,
    offset: int,
    limit: int,
    project_path: str | None,
    search: str | None,
    show_archived: bool,
    file_edit_mode: bool | None,
    folder_ids: set[str],
    folder_view: bool,
    tag_ids: set[str],
    provider_ids: set[str],
    model_ids: set[str],
    modes: set[str],
    sources: set[str],
    search_fields: str | None,
    sort_by: str,
    status_sort: bool = False,
    status_gate: Callable[[dict], bool] | None = None,
) -> tuple[list[dict], int]:
    content_scores: dict[str, int] = {}
    state_snapshot = _sidebar_state_snapshot() if status_sort else None
    search_query = (search or "").strip()
    appended_virtual_sessions = False
    default_virtual_page = _can_page_default_updated_at_with_virtual(
        search_query=search_query,
        project_path=project_path,
        show_archived=show_archived,
        file_edit_mode=file_edit_mode,
        folder_ids=folder_ids,
        folder_view=folder_view,
        tag_ids=tag_ids,
        provider_ids=provider_ids,
        model_ids=model_ids,
        modes=modes,
        sources=sources,
        sort_by=sort_by,
        status_sort=status_sort,
        status_filter=status_gate is not None,
    )
    can_page_local_order = _can_page_local_summary_order(
        search_query=search_query,
        folder_view=folder_view,
        sort_by=sort_by,
        status_sort=status_sort,
        status_filter=status_gate is not None,
    )
    may_include_virtual = _session_filters_may_include_virtual(
        file_edit_mode=file_edit_mode,
        folder_ids=folder_ids,
        tag_ids=tag_ids,
        modes=modes,
        sources=sources,
    )
    if can_page_local_order and (not may_include_virtual or sort_by == "last_user_prompt_at"):
        with perf.timed("sessions.list.local_order_page"):
            page_source, local_total = _local_session_page_for_sidebar_preserving_order(
                sort_by=sort_by,
                offset=offset,
                limit=limit,
                project_path=project_path,
                search=search,
                show_archived=show_archived,
                file_edit_mode=file_edit_mode,
                folder_ids=folder_ids,
                tag_ids=tag_ids,
                provider_ids=provider_ids,
                model_ids=model_ids,
                modes=modes,
                sources=sources,
                content_scores=content_scores,
                status_gate=status_gate,
            )
        virtual_total = 0
        if may_include_virtual and sort_by == "last_user_prompt_at":
            with perf.timed("sessions.list.virtual_count"):
                cached_virtual = virtual_session_store.list_recent_cached(
                    1,
                    exclude_id=session_search.ASK_SINGLETON_ID,
                )
                if cached_virtual is None:
                    _virtual_page, virtual_total = virtual_session_store.list_recent(
                        1,
                        exclude_id=session_search.ASK_SINGLETON_ID,
                    )
                else:
                    _virtual_page, virtual_total = cached_virtual
        if len(page_source) >= limit or not may_include_virtual:
            total = local_total + virtual_total
            with perf.timed("sessions.list.page_decorate"):
                page = _decorate_local_sidebar_sessions(page_source, state_snapshot)
            return page, total
    if search_query:
        if _can_page_local_search_scores(
            project_path=project_path,
            show_archived=show_archived,
            file_edit_mode=file_edit_mode,
            folder_ids=folder_ids,
            folder_view=folder_view,
            tag_ids=tag_ids,
            provider_ids=provider_ids,
            model_ids=model_ids,
            modes=modes,
            sources=sources,
            sort_by=sort_by,
            status_sort=status_sort,
            connected=(),
            status_filter=status_gate is not None,
        ):
            page_source, total, content_scores = _build_local_search_page_for_sidebar(
                offset=offset,
                limit=limit,
                search_query=search_query,
                search_fields=search_fields,
                sort_by=sort_by,
                folder_view=folder_view,
            )
            with perf.timed("sessions.list.page_decorate"):
                page = _decorate_local_sidebar_sessions(page_source, state_snapshot)
            if content_scores:
                page = [
                    {**session, "search_score": content_scores.get(str(session.get("id") or ""), 0)}
                    for session in page
                ]
            return page, total
        selected_search_fields = _split_session_search_fields(search_fields)
        content_max_wait_seconds = (
            session_list_cache._SESSION_LIST_CONTENT_SEARCH_MAX_WAIT_SECONDS
            if session_store.SEARCH_FIELD_CONTENT in selected_search_fields
            else None
        )
        with perf.timed("sessions.list.search_scores"):
            content_scores = session_store.grep_session_scores(
                search_query,
                selected_search_fields,
                content_limit=session_list_cache._session_search_candidate_limit(offset, limit),
                content_max_wait_seconds=content_max_wait_seconds,
            )
        with perf.timed("sessions.list.search_local"):
            out = _local_session_summaries_by_ids_for_sidebar(list(content_scores))
    else:
        if may_include_virtual:
            with perf.timed("sessions.list.virtual"):
                if default_virtual_page:
                    with perf.timed("sessions.list.local_order_page"):
                        out, local_total = _local_session_page_for_sidebar_preserving_order(
                            sort_by=sort_by,
                            offset=0,
                            limit=max(offset + limit, 1),
                            project_path=project_path,
                            search=search,
                            show_archived=show_archived,
                            file_edit_mode=file_edit_mode,
                            folder_ids=folder_ids,
                            tag_ids=tag_ids,
                            provider_ids=provider_ids,
                            model_ids=model_ids,
                            modes=modes,
                            sources=sources,
                            content_scores=content_scores,
                            status_gate=status_gate,
                        )
                    virtual_limit = max(offset + limit, 1)
                    cached_virtual = virtual_session_store.list_recent_cached(
                        virtual_limit,
                        exclude_id=session_search.ASK_SINGLETON_ID,
                    )
                    if cached_virtual is None:
                        virtual_sessions, virtual_total = virtual_session_store.list_recent(
                            virtual_limit,
                            exclude_id=session_search.ASK_SINGLETON_ID,
                        )
                    else:
                        virtual_sessions, virtual_total = cached_virtual
                else:
                    with perf.timed("sessions.list.local"):
                        out = _local_session_summaries_for_sidebar()
                    virtual_sessions = virtual_session_store.list_all()
                    virtual_total = len([
                        session for session in virtual_sessions
                        if session.get("id") != session_search.ASK_SINGLETON_ID
                    ])
            virtual_sidebar_sessions = [
                session
                for session in virtual_sessions
                if session.get("id") != session_search.ASK_SINGLETON_ID
            ]
            if default_virtual_page:
                with perf.timed("sessions.list.default_virtual_merge"):
                    page_source, _merged_count = _merge_updated_at_page(
                        out,
                        virtual_sidebar_sessions,
                        offset=offset,
                        limit=limit,
                    )
                total = local_total + virtual_total
                with perf.timed("sessions.list.page_decorate"):
                    page = _decorate_local_sidebar_sessions(page_source, state_snapshot)
                return page, total
            if virtual_sidebar_sessions:
                out.extend(virtual_sidebar_sessions)
                appended_virtual_sessions = True
        else:
            with perf.timed("sessions.list.local"):
                out = _local_session_summaries_for_sidebar()
            perf.record("sessions.list.virtual.skipped", 1.0)
    with perf.timed("sessions.list.filter_sort"):
        if search_query:
            page_source, total = _filter_sort_page_for_list(
                out,
                offset=offset,
                limit=limit,
                project_path=project_path,
                search=search,
                show_archived=show_archived,
                file_edit_mode=file_edit_mode,
                folder_ids=folder_ids,
                folder_view=folder_view,
                tag_ids=tag_ids,
                provider_ids=provider_ids,
                model_ids=model_ids,
                modes=modes,
                sources=sources,
                content_scores=content_scores,
                sort_by=sort_by,
                status_sort=status_sort,
                state_snapshot=state_snapshot,
                status_gate=status_gate,
            )
            with perf.timed("sessions.list.page_decorate"):
                page = _decorate_local_sidebar_sessions(page_source, state_snapshot)
            if content_scores:
                page = [
                    {**session, "search_score": content_scores.get(str(session.get("id") or ""), 0)}
                    for session in page
                ]
            return page, total
        if _can_preserve_summary_order(
            search_query=search_query,
            appended_virtual_sessions=appended_virtual_sessions,
            folder_view=folder_view,
            sort_by=sort_by,
            status_sort=status_sort,
            status_filter=status_gate is not None,
        ):
            page_source, total = _filter_page_for_list_preserving_order(
                out,
                offset=offset,
                limit=limit,
                project_path=project_path,
                search=search,
                show_archived=show_archived,
                file_edit_mode=file_edit_mode,
                folder_ids=folder_ids,
                tag_ids=tag_ids,
                provider_ids=provider_ids,
                model_ids=model_ids,
                modes=modes,
                sources=sources,
                content_scores=content_scores,
                status_gate=status_gate,
            )
            with perf.timed("sessions.list.page_decorate"):
                page = _decorate_local_sidebar_sessions(page_source, state_snapshot)
            return page, total
        else:
            out = _filter_sort_sessions_for_list(
                out,
                project_path=project_path,
                search=search,
                show_archived=show_archived,
                file_edit_mode=file_edit_mode,
                folder_ids=folder_ids,
                folder_view=folder_view,
                tag_ids=tag_ids,
                provider_ids=provider_ids,
                model_ids=model_ids,
                modes=modes,
                sources=sources,
                content_scores=content_scores,
                sort_by=sort_by,
                status_sort=status_sort,
                state_snapshot=state_snapshot,
                status_gate=status_gate,
            )
    total = len(out)
    end = offset + limit
    with perf.timed("sessions.list.page_decorate"):
        page = _decorate_local_sidebar_sessions(out[offset:end], state_snapshot)
    if content_scores:
        page = [
            {**session, "search_score": content_scores.get(str(session.get("id") or ""), 0)}
            for session in page
        ]
    return page, total


async def _sidebar_search_scores(
    search_query: str,
    search_fields: str | None,
    *,
    content_limit: int,
) -> dict[str, int]:
    selected_search_fields = _split_session_search_fields(search_fields)
    content_max_wait_seconds = (
        session_list_cache._SESSION_LIST_CONTENT_SEARCH_MAX_WAIT_SECONDS
        if session_store.SEARCH_FIELD_CONTENT in selected_search_fields
        else None
    )
    return await asyncio.to_thread(
        session_store.grep_session_scores,
        search_query,
        selected_search_fields,
        content_limit=content_limit,
        content_max_wait_seconds=content_max_wait_seconds,
    )


# ── Routes ──


@router.get("/api/sessions")
async def get_sessions(
    request: Request,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    project_path: str | None = Query(None),
    search: str | None = Query(None),
    show_archived: bool = Query(False),
    file_edit_mode: bool | None = Query(None),
    folder_ids: str | None = Query(None),
    folder_view: bool | None = Query(None),
    tag_ids: str | None = Query(None),
    provider_ids: str | None = Query(None),
    model_ids: str | None = Query(None),
    modes: str | None = Query(None),
    sources: str | None = Query(None),
    search_fields: str | None = Query(None),
    sort_by: str | None = Query(None),
    statuses: str | None = Query(None),
    exclude_statuses: str | None = Query(None),
):
    accept_encoding = request.headers.get("accept-encoding", "")
    search_query = (search or "").strip()
    connected_version = 0
    connected: tuple[str, ...] = ()
    if not search_query:
        with perf.timed("sessions.list.connected_nodes"):
            try:
                import node_store as _ns
                connected_version, connected = _ns.connected_worker_node_ids_snapshot()
            except Exception:
                logger.debug("get_sessions: connected node snapshot failed", exc_info=True)
    if connected:
        with perf.timed("sessions.list.nodes_ready"):
            if not session_list_cache._machine_nodes_enabled_cached():
                connected = ()
    with perf.timed("sessions.list.filters"):
        (
            default_folder_view,
            default_sort_by,
            effective_status_sort,
        ) = session_list_cache._session_list_user_prefs()
        effective_folder_view = (
            folder_view if folder_view is not None else default_folder_view
        )
        effective_sort_by = (
            sort_by if sort_by in user_prefs.SESSION_SORT_VALUES
            else default_sort_by
        )
        effective_search_fields = _split_session_search_fields(search_fields)
        include_statuses = _split_session_statuses(statuses)
        exclude_statuses_set = _split_session_statuses(exclude_statuses)
        filters = {
            "offset": offset,
            "limit": limit,
            "project_path": project_path,
            "search": search,
            "show_archived": show_archived,
            "file_edit_mode": file_edit_mode,
            "folder_ids": _split_session_filter(folder_ids),
            "folder_view": effective_folder_view,
            "tag_ids": _split_session_filter(tag_ids),
            "provider_ids": _split_session_filter(provider_ids),
            "model_ids": _split_session_filter(model_ids),
            "modes": _split_session_filter(modes),
            "sources": _split_session_filter(sources),
            "search_fields": search_fields,
            "sort_by": effective_sort_by,
            "status_sort": effective_status_sort,
        }
    cache_key = (
        offset,
        limit,
        project_path,
        search_query,
        show_archived,
        file_edit_mode,
        tuple(sorted(filters["folder_ids"])),
        effective_folder_view,
        tuple(sorted(filters["tag_ids"])),
        tuple(sorted(filters["provider_ids"])),
        tuple(sorted(filters["model_ids"])),
        tuple(sorted(filters["modes"])),
        tuple(sorted(filters["sources"])),
        tuple(sorted(effective_search_fields)),
        effective_sort_by,
        effective_status_sort,
        tuple(sorted(include_statuses)),
        tuple(sorted(exclude_statuses_set)),
        connected_version,
        connected,
        remote_sessions_cache.version() if connected else 0,
        session_list_cache._sessions_list_cache_version(search_query, effective_search_fields),
    )
    cached_response = session_list_cache._sessions_list_cache_get(cache_key, accept_encoding)
    if cached_response is not None:
        perf.record("sessions.list.response_cache.hit", 1.0)
        return cached_response
    perf.record("sessions.list.response_cache.miss", 1.0)
    cache_response = session_list_cache._sessions_list_content_search_ready(
        search_query,
        effective_search_fields,
        offset=offset,
        limit=limit,
    )
    # Status buckets are derived from live snapshots, so the gate closes over
    # ONE snapshot per request — every path classifies a session identically.
    status_gate = (
        _session_status_gate(
            include_statuses,
            exclude_statuses_set,
            await asyncio.to_thread(_sidebar_state_snapshot),
        )
        if include_statuses or exclude_statuses_set
        else None
    )
    filters["status_gate"] = status_gate
    if search_query and _can_page_local_search_scores(
        project_path=project_path,
        show_archived=show_archived,
        file_edit_mode=file_edit_mode,
        folder_ids=filters["folder_ids"],
        folder_view=effective_folder_view,
        tag_ids=filters["tag_ids"],
        provider_ids=filters["provider_ids"],
        model_ids=filters["model_ids"],
        modes=filters["modes"],
        sources=filters["sources"],
        sort_by=effective_sort_by,
        status_sort=effective_status_sort,
        connected=(),
        status_filter=status_gate is not None,
    ):
        page_source, total, content_scores = await session_list_path.run(
            "sessions.list.search_local_page.worker",
            _build_local_search_page_for_sidebar,
            offset=offset,
            limit=limit,
            search_query=search_query,
            search_fields=search_fields,
            sort_by=effective_sort_by,
            folder_view=effective_folder_view,
        )
        state_snapshot = None
        with perf.timed("sessions.list.page_decorate"):
            page = await session_list_path.run(
                "sessions.list.page_decorate.worker",
                _decorate_local_sidebar_sessions,
                page_source,
                state_snapshot,
            )
        if content_scores:
            page = [
                {**session, "search_score": content_scores.get(str(session.get("id") or ""), 0)}
                for session in page
            ]
        session_list_cache._schedule_session_event_meta_warm(page)
        response_payload = session_list_cache._sessions_snapshot_payload(
            {
                "sessions": page,
                "offset": offset,
                "limit": limit,
                "total": total,
                "has_more": offset + limit < total,
                "sort_by": effective_sort_by,
                "status_sort": effective_status_sort,
            }
        )
        return session_list_cache._sessions_list_response_maybe_cache(
            cache_key,
            response_payload,
            cache_response=cache_response and response_payload.get("snapshot_complete") is True,
            accept_encoding=accept_encoding,
        )
    if not connected:
        page, total = await session_list_path.run(
            "sessions.list.local_page_thread",
            _build_local_sessions_page_for_list,
            **filters,
        )
        session_list_cache._schedule_session_event_meta_warm(page)
        response_payload = session_list_cache._sessions_snapshot_payload({
            "sessions": page,
            "offset": offset,
            "limit": limit,
            "total": total,
            "has_more": offset + limit < total,
            "sort_by": effective_sort_by,
            "status_sort": effective_status_sort,
        })
        return session_list_cache._sessions_list_response_maybe_cache(
            cache_key,
            response_payload,
            cache_response=cache_response and response_payload.get("snapshot_complete") is True,
            accept_encoding=accept_encoding,
        )

    content_scores: dict[str, int] = {}
    appended_virtual_sessions = False
    appended_remote_sessions = False
    handled_virtual_sessions = False
    handled_remote_sessions = False
    deferred_sidebar_projection = False
    local_total: int | None = None
    local_page_candidates: list[dict] | None = None
    projected_first_page_sessions: list[dict] = []
    can_page_remote_local_order = _can_page_local_summary_order(
        search_query=search_query,
        folder_view=effective_folder_view,
        sort_by=effective_sort_by,
        status_sort=effective_status_sort,
        status_filter=status_gate is not None,
    )
    may_include_virtual = _session_filters_may_include_virtual(
        file_edit_mode=file_edit_mode,
        folder_ids=filters["folder_ids"],
        tag_ids=filters["tag_ids"],
        modes=filters["modes"],
        sources=filters["sources"],
    )
    default_projected_first_page = _can_page_default_updated_at_with_virtual(
        search_query=search_query,
        project_path=project_path,
        show_archived=show_archived,
        file_edit_mode=file_edit_mode,
        folder_ids=filters["folder_ids"],
        folder_view=effective_folder_view,
        tag_ids=filters["tag_ids"],
        provider_ids=filters["provider_ids"],
        model_ids=filters["model_ids"],
        modes=filters["modes"],
        sources=filters["sources"],
        sort_by=effective_sort_by,
        status_sort=effective_status_sort,
        status_filter=status_gate is not None,
    )
    if search_query:
        with perf.timed("sessions.list.search_scores"):
            content_scores = await _sidebar_search_scores(
                search_query,
                search_fields,
                content_limit=session_list_cache._session_search_candidate_limit(offset, limit),
            )
        with perf.timed("sessions.list.search_local"):
            out = await asyncio.to_thread(
                _local_session_summaries_by_ids_for_sidebar,
                list(content_scores),
            )
    else:
        if can_page_remote_local_order:
            with perf.timed("sessions.list.remote.local_order_candidates"):
                out, local_total = await session_list_path.run(
                    "sessions.list.remote.local_order_candidates.worker",
                    _local_session_page_for_sidebar_preserving_order,
                    sort_by=effective_sort_by,
                    offset=0,
                    limit=max(offset + limit, 1),
                    project_path=project_path,
                    search=search,
                    show_archived=show_archived,
                    file_edit_mode=file_edit_mode,
                    folder_ids=filters["folder_ids"],
                    tag_ids=filters["tag_ids"],
                    provider_ids=filters["provider_ids"],
                    model_ids=filters["model_ids"],
                    modes=filters["modes"],
                    sources=filters["sources"],
                    content_scores=content_scores,
                    status_gate=status_gate,
                )
                local_page_candidates = out
        else:
            with perf.timed("sessions.list.local"):
                out = await session_list_path.run(
                    "sessions.list.local.worker",
                    _local_session_summaries_for_sidebar,
                )
        if (
            can_page_remote_local_order
            and local_total is not None
            and len(out) >= max(offset + limit, 1)
        ):
            if may_include_virtual:
                with perf.timed("sessions.list.virtual.cached_first_page"):
                    cached_virtual = await asyncio.to_thread(
                        virtual_session_store.list_recent_cached,
                        max(offset + limit, 1),
                        exclude_id=session_search.ASK_SINGLETON_ID,
                    )
                handled_virtual_sessions = True
                if cached_virtual is None:
                    deferred_sidebar_projection = True
                    session_list_cache._schedule_virtual_sessions_recent_refresh(max(offset + limit, 1))
                else:
                    virtual_sessions, virtual_total = cached_virtual
                    virtual_sidebar_sessions = [
                        session
                        for session in virtual_sessions
                        if session.get("id") != session_search.ASK_SINGLETON_ID
                    ]
                    if virtual_sidebar_sessions:
                        out.extend(virtual_sidebar_sessions)
                        projected_first_page_sessions.extend(virtual_sidebar_sessions)
                        appended_virtual_sessions = True
                    local_total += virtual_total
            with perf.timed("sessions.list.remote.cached_first_page"):
                for nid in connected:
                    cached_remote = remote_sessions_cache.for_sidebar_cached(
                        nid,
                        limit=max(offset + limit, 1),
                    )
                    if cached_remote is None:
                        deferred_sidebar_projection = True
                        continue
                    remote, remote_total = cached_remote
                    for rs in remote:
                        rs["node_id"] = nid
                        rs.setdefault("is_running", False)
                        rs.setdefault("unread_count", 0)
                        rs.setdefault("monitoring_state", "idle")
                        out.append(rs)
                        projected_first_page_sessions.append(rs)
                        appended_remote_sessions = True
                    local_total += remote_total
            handled_remote_sessions = True
            if deferred_sidebar_projection and not appended_virtual_sessions and not appended_remote_sessions:
                end = offset + limit
                with perf.timed("sessions.list.page_decorate"):
                    page = await session_list_path.run(
                        "sessions.list.page_decorate.worker",
                        _decorate_local_sidebar_sessions,
                        out[offset:end],
                        None,
                    )
                session_list_cache._schedule_session_event_meta_warm(page)
                return session_list_cache._json_response_maybe_gzip(
                    json.dumps(
                        {
                            "sessions": page,
                            "offset": offset,
                            "limit": limit,
                            "total": local_total,
                            "has_more": end < local_total,
                            "sort_by": effective_sort_by,
                            "status_sort": effective_status_sort,
                        },
                        ensure_ascii=False,
                        allow_nan=False,
                        separators=(",", ":"),
                    ).encode("utf-8"),
                    accept_encoding,
                )
        if may_include_virtual:
            if handled_virtual_sessions:
                pass
            else:
                with perf.timed("sessions.list.virtual"):
                    if can_page_remote_local_order:
                        virtual_sessions, virtual_total = await asyncio.to_thread(
                            virtual_session_store.list_recent,
                            max(offset + limit, 1),
                            exclude_id=session_search.ASK_SINGLETON_ID,
                        )
                    else:
                        virtual_sessions = await asyncio.to_thread(virtual_session_store.list_all)
                        virtual_total = len([
                            session for session in virtual_sessions
                            if session.get("id") != session_search.ASK_SINGLETON_ID
                        ])
                virtual_sidebar_sessions = [
                    session
                    for session in virtual_sessions
                    if session.get("id") != session_search.ASK_SINGLETON_ID
                ]
                if virtual_sidebar_sessions:
                    out.extend(virtual_sidebar_sessions)
                    appended_virtual_sessions = True
                if local_total is not None:
                    local_total += virtual_total
        else:
            perf.record("sessions.list.virtual.skipped", 1.0)

    if not handled_remote_sessions:
        try:
            with perf.timed("sessions.list.remote"):
                remote_results = await asyncio.gather(
                    *(
                        asyncio.wait_for(
                            remote_sessions_cache.for_sidebar(nid),
                            timeout=remote_sessions_cache.fetch_timeout_seconds + 0.05,
                        )
                        for nid in connected
                    ),
                    return_exceptions=True,
                )
            for nid, result in zip(connected, remote_results):
                if isinstance(result, Exception):
                    logger.warning("get_sessions: remote node merge timed out")
                    continue
                remote = result
                for rs in remote:
                    rs["node_id"] = nid
                    rs.setdefault("is_running", False)
                    rs.setdefault("unread_count", 0)
                    rs.setdefault("monitoring_state", "idle")
                    out.append(rs)
                    projected_first_page_sessions.append(rs)
                    appended_remote_sessions = True
                if local_total is not None:
                    local_total += len(remote)
        except Exception:
            logger.debug("get_sessions: node merge failed", exc_info=True)

    if (
        default_projected_first_page
        and local_page_candidates is not None
        and projected_first_page_sessions
        and local_total is not None
    ):
        end = offset + limit
        with perf.timed("sessions.list.projected_first_page_merge"):
            projected_first_page_sessions.sort(
                key=lambda session: session_store.timestamp_sort_value(session.get("updated_at")),
                reverse=True,
            )
            page_source, _merged_count = _merge_updated_at_page(
                local_page_candidates,
                projected_first_page_sessions,
                offset=offset,
                limit=limit,
            )
        with perf.timed("sessions.list.page_decorate"):
            page = await session_list_path.run(
                "sessions.list.page_decorate.worker",
                _decorate_local_sidebar_sessions,
                page_source,
                None,
            )
        session_list_cache._schedule_session_event_meta_warm(page)
        response_payload = session_list_cache._sessions_snapshot_payload({
            "sessions": page,
            "offset": offset,
            "limit": limit,
            "total": local_total,
            "has_more": end < local_total,
            "sort_by": effective_sort_by,
            "status_sort": effective_status_sort,
        })
        return session_list_cache._sessions_list_response_maybe_cache(
            cache_key,
            response_payload,
            cache_response=cache_response and response_payload.get("snapshot_complete") is True,
            accept_encoding=accept_encoding,
        )

    if (
        can_page_remote_local_order
        and not appended_virtual_sessions
        and not appended_remote_sessions
        and local_total is not None
    ):
        end = offset + limit
        with perf.timed("sessions.list.page_decorate"):
            page = await session_list_path.run(
                "sessions.list.page_decorate.worker",
                _decorate_local_sidebar_sessions,
                out[offset:end],
                None,
            )
        session_list_cache._schedule_session_event_meta_warm(page)
        response_payload = session_list_cache._sessions_snapshot_payload({
            "sessions": page,
            "offset": offset,
            "limit": limit,
            "total": local_total,
            "has_more": end < local_total,
            "sort_by": effective_sort_by,
            "status_sort": effective_status_sort,
        })
        return session_list_cache._sessions_list_response_maybe_cache(
            cache_key,
            response_payload,
            cache_response=cache_response and response_payload.get("snapshot_complete") is True,
            accept_encoding=accept_encoding,
        )

    state_snapshot = (
        await asyncio.to_thread(_sidebar_state_snapshot)
        if effective_status_sort
        else None
    )
    page_source: list[dict] | None = None
    filtered_total: int | None = None
    with perf.timed("sessions.list.filter_sort"):
        if can_page_remote_local_order or search_query:
            page_source, filtered_total = await asyncio.to_thread(
                _filter_sort_page_for_list,
                out,
                offset=offset,
                limit=limit,
                project_path=project_path,
                search=search,
                show_archived=show_archived,
                file_edit_mode=file_edit_mode,
                folder_ids=filters["folder_ids"],
                folder_view=effective_folder_view,
                tag_ids=filters["tag_ids"],
                provider_ids=filters["provider_ids"],
                model_ids=filters["model_ids"],
                modes=filters["modes"],
                sources=filters["sources"],
                content_scores=content_scores,
                sort_by=effective_sort_by,
                status_sort=effective_status_sort,
                state_snapshot=state_snapshot,
                status_gate=status_gate,
            )
        elif _can_preserve_summary_order(
            search_query=search_query,
            appended_virtual_sessions=appended_virtual_sessions,
            folder_view=effective_folder_view,
            sort_by=effective_sort_by,
            status_sort=effective_status_sort,
            status_filter=status_gate is not None,
        ):
            out = await asyncio.to_thread(
                _filter_sessions_for_list_preserving_order,
                out,
                project_path=project_path,
                search=search,
                show_archived=show_archived,
                file_edit_mode=file_edit_mode,
                folder_ids=filters["folder_ids"],
                tag_ids=filters["tag_ids"],
                provider_ids=filters["provider_ids"],
                model_ids=filters["model_ids"],
                modes=filters["modes"],
                sources=filters["sources"],
                content_scores=content_scores,
                status_gate=status_gate,
            )
        else:
            out = await asyncio.to_thread(
                _filter_sort_sessions_for_list,
                out,
                project_path=project_path,
                search=search,
                show_archived=show_archived,
                file_edit_mode=file_edit_mode,
                folder_ids=filters["folder_ids"],
                folder_view=effective_folder_view,
                tag_ids=filters["tag_ids"],
                provider_ids=filters["provider_ids"],
                model_ids=filters["model_ids"],
                modes=filters["modes"],
                sources=filters["sources"],
                content_scores=content_scores,
                sort_by=effective_sort_by,
                status_sort=effective_status_sort,
                state_snapshot=state_snapshot,
                status_gate=status_gate,
            )
    total = (
        local_total
        if local_total is not None
        else (filtered_total if filtered_total is not None else len(out))
    )
    end = offset + limit
    if page_source is None:
        page_source = out[offset:end]
    with perf.timed("sessions.list.page_decorate"):
        page = await session_list_path.run(
            "sessions.list.page_decorate.worker",
            _decorate_local_sidebar_sessions,
            page_source,
            state_snapshot,
        )
    if content_scores:
        page = [
            {**session, "search_score": content_scores.get(str(session.get("id") or ""), 0)}
            for session in page
        ]
    session_list_cache._schedule_session_event_meta_warm(page)
    response_payload = session_list_cache._sessions_snapshot_payload({
        "sessions": page,
        "offset": offset,
        "limit": limit,
        "total": total,
        "has_more": end < total,
        "sort_by": effective_sort_by,
        "status_sort": effective_status_sort,
    })
    if deferred_sidebar_projection:
        return session_list_cache._json_response_maybe_gzip(
            json.dumps(
                response_payload,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8"),
            accept_encoding,
        )
    return session_list_cache._sessions_list_response_maybe_cache(
        cache_key,
        response_payload,
        cache_response=cache_response and response_payload.get("snapshot_complete") is True,
        accept_encoding=accept_encoding,
    )


@router.post("/api/sessions/search-content")
async def search_session_content(body: dict = Body(default={})):
    """Grep-based session content search.

    Scans session JSON files for substring matches, counts occurrences
    per session, and returns results sorted by score descending.
    Used by the sidebar filter for "search in session content".

    Body: `{"query": "...", "limit"?: int, "fields"?: ["content"|"title"|"first_prompt"]}`.
    Returns: `{"results": [{"session_id": "...", "score": N}, ...]}`.
    """
    query = (body.get("query") or "").strip()
    if not query:
        return {"results": []}
    raw_fields = body.get("fields")
    if raw_fields is None:
        fields = set(session_store.DEFAULT_SEARCH_FIELDS)
    elif isinstance(raw_fields, list):
        fields = {
            field
            for field in raw_fields
            if isinstance(field, str) and field in session_store.SEARCH_FIELDS
        }
    else:
        raise HTTPException(status_code=400, detail="fields must be a list")
    limit = body.get("limit")
    if not isinstance(limit, int) or limit <= 0:
        limit = 50
    with perf.timed("sessions.search_content.query"):
        results = await asyncio.to_thread(session_store.grep_sessions, query, limit, fields)
    return {"results": results}


def _session_organization_snapshot_with_facets(project_id: str | None) -> dict:
    """Org snapshot plus the model filter universe for the project.

    Folder/provider/mode/source universes are known client-side (org
    folders, configured providers, static enums); models are open-ended,
    so the backend supplies the distinct models across ALL the project's
    sessions regardless of the active filter, keeping the filter options
    stable instead of collapsing to whatever the current page contains.
    """
    org_token = session_organization_store.version_token()
    cache_key = (project_id, session_store.summary_version(), org_token)
    cached = _session_org_facets_cache.get(cache_key)
    if cached is not None:
        return cached
    snapshot = session_organization_store.snapshot(project_id)
    models: set[str] = set()
    for session in _local_session_summaries_for_sidebar():
        if not session_matches_project(session, project_id):
            continue
        model = (session.get("model") or "").strip()
        if model:
            models.add(model)
    snapshot["models"] = sorted(models)
    if len(_session_org_facets_cache) >= 16:
        _session_org_facets_cache.pop(next(iter(_session_org_facets_cache)))
    _session_org_facets_cache[cache_key] = snapshot
    return snapshot


@router.get("/api/session-organization")
async def get_session_organization(project_id: str | None = Query(default=None)):
    try:
        return await asyncio.to_thread(
            _session_organization_snapshot_with_facets,
            project_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/api/session-organization/query")
async def query_session_organization(body: dict = Body(default={})):
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="request body must be an object")
    try:
        sessions = await asyncio.to_thread(_local_sessions_for_sidebar)
        results = await asyncio.to_thread(
            session_organization_store.query_sessions,
            sessions,
            body,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"sessions": results}


@router.post("/api/session-folders")
async def create_session_folder(body: dict = Body(default={})):
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="request body must be an object")
    try:
        folder = await asyncio.to_thread(
            session_organization_store.create_folder,
            project_id=body.get("project_id"),
            name=body.get("name"),
            parent_folder_id=body.get("parent_folder_id"),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await broadcast_session_organization_changed()
    return {"folder": folder}


@router.patch("/api/session-folders/{folder_id}")
async def update_session_folder(folder_id: str, body: dict = Body(default={})):
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="request body must be an object")
    try:
        folder = await asyncio.to_thread(
            session_organization_store.update_folder,
            folder_id,
            body,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="folder not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await broadcast_session_organization_changed()
    return {"folder": folder}


@router.delete("/api/session-folders/{folder_id}")
async def delete_session_folder(folder_id: str, mode: str | None = Query(None)):
    _, _, delete_session_tree = _require_configured()
    try:
        preview = await asyncio.to_thread(
            session_organization_store.folder_delete_preview,
            folder_id,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="folder not found")
    if preview["session_count"] > 0 and mode is None:
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "folder_contains_sessions",
                **preview,
            },
        )
    delete_mode = mode or "unassign"
    if delete_mode == "delete_sessions":
        for session_id in preview["session_ids"]:
            await delete_session_tree(session_id)
    try:
        deleted = await asyncio.to_thread(
            session_organization_store.delete_folder,
            folder_id,
            mode=delete_mode,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not deleted:
        raise HTTPException(status_code=404, detail="folder not found")
    await broadcast_session_organization_changed()
    return {"deleted": True, **preview}


@router.post("/api/session-tags")
async def create_session_tag(body: dict = Body(default={})):
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="request body must be an object")
    try:
        tag = await asyncio.to_thread(
            session_organization_store.create_tag,
            project_id=body.get("project_id"),
            name=body.get("name"),
            color=body.get("color"),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await broadcast_session_organization_changed()
    return {"tag": tag}


@router.patch("/api/session-tags/{tag_id}")
async def update_session_tag(tag_id: str, body: dict = Body(default={})):
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="request body must be an object")
    try:
        tag = await asyncio.to_thread(
            session_organization_store.update_tag,
            tag_id,
            body,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="tag not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await broadcast_session_organization_changed()
    return {"tag": tag}


@router.delete("/api/session-tags/{tag_id}")
async def delete_session_tag(tag_id: str):
    deleted = await asyncio.to_thread(
        session_organization_store.delete_tag,
        tag_id,
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="tag not found")
    await broadcast_session_organization_changed()
    return {"deleted": True}


@router.patch("/api/sessions/{session_id}/organization")
async def update_session_organization(session_id: str, body: dict = Body(default={})):
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="request body must be an object")
    if not await _session_exists(session_id):
        raise HTTPException(status_code=404, detail=t("error.session_not_found_retry"))
    allowed = {"folder_id", "tag_ids", "add_tag_ids", "remove_tag_ids"}
    unknown = set(body) - allowed
    if unknown:
        raise HTTPException(status_code=400, detail="unknown organization field")
    try:
        if "folder_id" in body:
            org = await asyncio.to_thread(
                session_organization_store.set_session_folder,
                session_id,
                body.get("folder_id"),
            )
        if "tag_ids" in body:
            org = await asyncio.to_thread(
                session_organization_store.set_session_tags,
                session_id,
                body.get("tag_ids"),
            )
        if "add_tag_ids" in body or "remove_tag_ids" in body:
            org = await asyncio.to_thread(
                session_organization_store.patch_session_tags,
                session_id,
                add=body.get("add_tag_ids"),
                remove=body.get("remove_tag_ids"),
            )
        if not body:
            org = await asyncio.to_thread(
                session_organization_store.organization_for_session,
                session_id,
            )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await broadcast_session_organization_changed([session_id])
    return {"session_id": session_id, "organization": org}


@router.post("/api/internal/session-organization/snapshot")
async def internal_session_organization_snapshot(body: dict = Body(default={})):
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="request body must be an object")
    try:
        return await asyncio.to_thread(
            session_organization_store.snapshot,
            body.get("project_id"),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/api/internal/session-organization/query")
async def internal_session_organization_query(body: dict = Body(default={})):
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="request body must be an object")
    try:
        sessions = await asyncio.to_thread(_local_sessions_for_sidebar)
        results = await asyncio.to_thread(
            session_organization_store.query_sessions,
            sessions,
            body,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"sessions": results}


@router.post("/api/internal/session-organization/create-folder")
async def internal_session_organization_create_folder(body: dict = Body(default={})):
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="request body must be an object")
    try:
        folder = await asyncio.to_thread(
            session_organization_store.create_folder,
            project_id=body.get("project_id"),
            name=body.get("name"),
            parent_folder_id=body.get("parent_folder_id"),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await broadcast_session_organization_changed()
    return {"folder": folder}


@router.post("/api/internal/session-organization/update-folder")
async def internal_session_organization_update_folder(body: dict = Body(default={})):
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="request body must be an object")
    try:
        folder = await asyncio.to_thread(
            session_organization_store.update_folder,
            body.get("folder_id"),
            body.get("patch") or {},
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="folder not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await broadcast_session_organization_changed()
    return {"folder": folder}


@router.post("/api/internal/session-organization/delete-folder")
async def internal_session_organization_delete_folder(body: dict = Body(default={})):
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="request body must be an object")
    _, _, delete_session_tree = _require_configured()
    folder_id = body.get("folder_id")
    try:
        preview = await asyncio.to_thread(
            session_organization_store.folder_delete_preview,
            folder_id,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="folder not found")
    mode = body.get("mode")
    if preview["session_count"] > 0 and mode is None:
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "folder_contains_sessions",
                **preview,
            },
        )
    delete_mode = mode or "unassign"
    if delete_mode == "delete_sessions":
        for session_id in preview["session_ids"]:
            await delete_session_tree(session_id)
    try:
        deleted = await asyncio.to_thread(
            session_organization_store.delete_folder,
            folder_id,
            mode=delete_mode,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not deleted:
        raise HTTPException(status_code=404, detail="folder not found")
    await broadcast_session_organization_changed()
    return {"deleted": True, **preview}


@router.post("/api/internal/session-organization/create-tag")
async def internal_session_organization_create_tag(body: dict = Body(default={})):
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="request body must be an object")
    try:
        tag = await asyncio.to_thread(
            session_organization_store.create_tag,
            project_id=body.get("project_id"),
            name=body.get("name"),
            color=body.get("color"),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await broadcast_session_organization_changed()
    return {"tag": tag}


@router.post("/api/internal/session-organization/update-tag")
async def internal_session_organization_update_tag(body: dict = Body(default={})):
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="request body must be an object")
    try:
        tag = await asyncio.to_thread(
            session_organization_store.update_tag,
            body.get("tag_id"),
            body.get("patch") or {},
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="tag not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await broadcast_session_organization_changed()
    return {"tag": tag}


@router.post("/api/internal/session-organization/delete-tag")
async def internal_session_organization_delete_tag(body: dict = Body(default={})):
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="request body must be an object")
    try:
        deleted = await asyncio.to_thread(
            session_organization_store.delete_tag,
            body.get("tag_id"),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not deleted:
        raise HTTPException(status_code=404, detail="tag not found")
    await broadcast_session_organization_changed()
    return {"deleted": True}


@router.post("/api/internal/session-organization/update-session")
async def internal_session_organization_update_session(
    body: dict = Body(default={}),
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    if not internal_guards.authority_is_valid():
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="request body must be an object")
    session_id = str(body.get("session_id") or "").strip()
    if not await _session_exists(session_id):
        raise HTTPException(status_code=404, detail=t("error.session_not_found_retry"))
    allowed = {"session_id", "folder_id", "tag_ids", "add_tag_ids", "remove_tag_ids", "tag_source", "sync_tag_source"}
    unknown = set(body) - allowed
    if unknown:
        raise HTTPException(status_code=400, detail="unknown organization field")
    if ("tag_source" in body or "sync_tag_source" in body) and not any(
        key in body for key in ("tag_ids", "add_tag_ids")
    ):
        raise HTTPException(status_code=400, detail="tag source requires tag_ids or add_tag_ids")
    if "tag_source" in body:
        _require_tag_source_owner(body.get("tag_source"), x_internal_token)
    if "sync_tag_source" in body:
        _require_tag_source_owner(body.get("sync_tag_source"), x_internal_token)
    try:
        if "folder_id" in body:
            org = await asyncio.to_thread(
                session_organization_store.set_session_folder,
                session_id,
                body.get("folder_id"),
            )
        if "tag_ids" in body:
            if body.get("sync_tag_source"):
                org = await asyncio.to_thread(
                    session_organization_store.sync_session_tags_by_source,
                    session_id,
                    tag_ids=body.get("tag_ids"),
                    source=body.get("sync_tag_source"),
                )
            else:
                org = await asyncio.to_thread(
                    session_organization_store.set_session_tags,
                    session_id,
                    body.get("tag_ids"),
                    source=body.get("tag_source") or session_organization_store.TAG_SOURCE_MANUAL,
                )
        if "add_tag_ids" in body or "remove_tag_ids" in body:
            org = await asyncio.to_thread(
                session_organization_store.patch_session_tags,
                session_id,
                add=body.get("add_tag_ids"),
                remove=body.get("remove_tag_ids"),
                add_source=body.get("tag_source") or session_organization_store.TAG_SOURCE_MANUAL,
            )
        if not any(k in body for k in ("folder_id", "tag_ids", "add_tag_ids", "remove_tag_ids")):
            org = await asyncio.to_thread(
                session_organization_store.organization_for_session,
                session_id,
            )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await broadcast_session_organization_changed([session_id])
    return {"session_id": session_id, "organization": org}


@router.get("/api/sessions/topbar-pinned")
async def get_topbar_pinned_sessions():
    sessions = await asyncio.to_thread(session_manager.list)
    pinned = [
        session
        for session in sessions
        if session.get("topbar_pinned")
    ]
    pinned.sort(
        key=lambda session: (
            session.get("topbar_pinned_at") or "",
            session.get("id") or "",
        ),
        reverse=True,
    )
    return {"sessions": pinned}


@router.get("/api/sessions/summaries")
async def get_session_summaries(request: Request, ids: str = Query("")):
    accept_encoding = request.headers.get("accept-encoding", "")
    requested_ids = [
        sid.strip()
        for sid in ids.split(",")
        if sid.strip()
    ]
    if not requested_ids:
        return {"sessions": []}
    cache_key = (
        tuple(requested_ids),
        session_store.summary_index_version(),
    )
    cached_response = session_list_cache._session_summaries_cache_get(cache_key, accept_encoding)
    if cached_response is not None:
        perf.record("sessions.summaries.response_cache.hit", 1.0)
        return cached_response
    perf.record("sessions.summaries.response_cache.miss", 1.0)
    summaries = await asyncio.to_thread(
        _local_session_summaries_by_ids,
        requested_ids,
    )
    by_id = {str(session.get("id")): session for session in summaries if session.get("id")}
    ordered = [by_id[sid] for sid in requested_ids if sid in by_id]
    page = await hot_path.run(
        "sessions.summaries.decorate.worker",
        _decorate_local_sidebar_sessions,
        ordered,
        None,
    )
    final_cache_key = (
        tuple(requested_ids),
        session_store.summary_index_version(),
    )
    return session_list_cache._session_summaries_cache_put(
        final_cache_key,
        {"sessions": page},
        accept_encoding,
    )
