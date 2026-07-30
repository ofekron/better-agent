"""User preferences, per-machine UI selection, and shortcut-response picking.

Depends on the coordinator only through the broadcast capability and the
session-list cache invalidation hook it actually needs, bound by the
composition root (see `configure`).
"""
from __future__ import annotations

import asyncio
from typing import Any, Callable, Optional

from fastapi import APIRouter, Body, HTTPException, Request

import shortcut_picker
import ui_selection
import user_prefs
from hot_path_executor import hot_path

router = APIRouter()

_broadcast_global: Optional[Callable[[str, dict], Any]] = None
_invalidate_session_list_cache: Optional[Callable[[], None]] = None


def configure(
    broadcast_global: Callable[[str, dict], Any],
    invalidate_session_list_cache: Callable[[], None],
) -> None:
    """Bind the coordinator capabilities this router needs."""
    global _broadcast_global, _invalidate_session_list_cache
    _broadcast_global = broadcast_global
    _invalidate_session_list_cache = invalidate_session_list_cache


def _require_configured() -> tuple[Callable[[str, dict], Any], Callable[[], None]]:
    if _broadcast_global is None or _invalidate_session_list_cache is None:
        raise HTTPException(status_code=503, detail="user prefs API is not configured")
    return _broadcast_global, _invalidate_session_list_cache


# ---- User preferences ----


@router.get("/api/user-prefs")
async def get_user_prefs(request: Request):
    login_username = (request.session.get("user") or {}).get("username")
    return await asyncio.to_thread(user_prefs.get_all, login_username)


@router.patch("/api/user-prefs")
async def patch_user_prefs(request: Request, body: dict = Body(...)):
    broadcast_global, invalidate_session_list_cache = _require_configured()
    login_username = (request.session.get("user") or {}).get("username")

    def _patch_user_prefs_sync() -> dict:
        if "auto_restart_on_idle" in body:
            raise ValueError("auto_restart_on_idle is no longer supported")
        if "user_display_name" in body:
            user_prefs.set_user_display_name(body["user_display_name"])
        if "send_mode" in body:
            user_prefs.set_send_mode(body["send_mode"])
        if "language" in body:
            user_prefs.set_language(body["language"])
        if "shortcut_responses" in body:
            user_prefs.set_shortcut_responses(body["shortcut_responses"])
        if "cross_session_delegate_auto" in body:
            val = body["cross_session_delegate_auto"]
            if not isinstance(val, bool):
                raise ValueError("cross_session_delegate_auto must be a boolean")
            user_prefs.set_cross_session_delegate_auto(val)
        if "context_strategy" in body:
            user_prefs.set_context_strategy(body["context_strategy"])
        if "session_auto_delete_days" in body:
            val = body["session_auto_delete_days"]
            if val is not None and (
                isinstance(val, bool) or not isinstance(val, int) or val < 1
            ):
                raise ValueError("session_auto_delete_days must be null or a positive integer")
            user_prefs.set_session_auto_delete_days(val)
        if "font_family" in body:
            val = body["font_family"]
            if val not in ("system", "serif", "mono", "inter"):
                raise ValueError("font_family must be system, serif, mono, or inter")
            user_prefs.set_font_family(val)
        if "font_size" in body:
            val = body["font_size"]
            if (
                isinstance(val, bool)
                or not isinstance(val, int)
                or val < user_prefs.MIN_FONT_SIZE
                or val > user_prefs.MAX_FONT_SIZE
            ):
                raise ValueError(
                    f"font_size must be an integer between "
                    f"{user_prefs.MIN_FONT_SIZE} and {user_prefs.MAX_FONT_SIZE}"
                )
            user_prefs.set_font_size(val)
        if "appearance_theme" in body:
            val = body["appearance_theme"]
            if val not in user_prefs.APPEARANCE_THEME_VALUES:
                raise ValueError("appearance_theme must be default, nord, or dracula")
            user_prefs.set_appearance_theme(val)
        if "first_run_wizard_done" in body:
            val = body["first_run_wizard_done"]
            if not isinstance(val, bool):
                raise ValueError("first_run_wizard_done must be a boolean")
            user_prefs.set_first_run_wizard_done(val)
        if "network_bind_address" in body:
            val = body["network_bind_address"]
            if val not in ("127.0.0.1", "0.0.0.0"):
                raise ValueError("network_bind_address must be 127.0.0.1 or 0.0.0.0")
            user_prefs.set_network_bind_address(val)
        if "folder_view_enabled" in body:
            val = body["folder_view_enabled"]
            if not isinstance(val, bool):
                raise ValueError("folder_view_enabled must be a boolean")
            user_prefs.set_folder_view_enabled(val)
        if "session_sort" in body:
            val = body["session_sort"]
            if not isinstance(val, str):
                raise ValueError("session_sort must be a string")
            user_prefs.set_session_sort(val)
        if "session_status_sort" in body:
            val = body["session_status_sort"]
            if not isinstance(val, bool):
                raise ValueError("session_status_sort must be a boolean")
            user_prefs.set_session_status_sort(val)
        if "sessions_tabs_sort" in body:
            val = body["sessions_tabs_sort"]
            if not isinstance(val, str):
                raise ValueError("sessions_tabs_sort must be a string")
            user_prefs.set_session_tabs_sort(val)
        if "sessions_tabs_visible" in body:
            val = body["sessions_tabs_visible"]
            if not isinstance(val, bool):
                raise ValueError("sessions_tabs_visible must be a boolean")
            user_prefs.set_session_tabs_visible(val)
        if "voice_close_on_background" in body:
            val = body["voice_close_on_background"]
            if not isinstance(val, bool):
                raise ValueError("voice_close_on_background must be a boolean")
            user_prefs.set_voice_close_on_background(val)
        if "task_start_silence_seconds" in body:
            user_prefs.set_task_start_silence_seconds(
                body["task_start_silence_seconds"]
            )
        if "sync_wait_depth_cap" in body:
            user_prefs.set_sync_wait_depth_cap(body["sync_wait_depth_cap"])
        if "session_creation_depth_cap" in body:
            user_prefs.set_session_creation_depth_cap(
                body["session_creation_depth_cap"]
            )
        if "session_max_live_descendants" in body:
            user_prefs.set_session_max_live_descendants(
                body["session_max_live_descendants"]
            )
        return user_prefs.get_all(login_username)

    try:
        prefs = await asyncio.to_thread(_patch_user_prefs_sync)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    invalidate_session_list_cache()
    await broadcast_global("user_prefs_changed", prefs)
    return prefs


# ---- UI selection (per-machine navigation restore) ----


@router.get("/api/ui-selection")
async def get_ui_selection():
    return await hot_path.run("ui_selection.get_all", ui_selection.get_all)


@router.patch("/api/ui-selection")
async def patch_ui_selection(body: dict = Body(...)):
    broadcast_global, _ = _require_configured()

    def _patch_sync() -> dict:
        if "selected_project" in body:
            sel = body["selected_project"]
            if sel is None:
                ui_selection.set_selected_project("")
            elif isinstance(sel, dict):
                path = sel.get("path")
                if not isinstance(path, str):
                    raise ValueError("selected_project.path must be a string")
                node_id = sel.get("node_id", ui_selection.DEFAULT_NODE_ID)
                if not isinstance(node_id, str):
                    raise ValueError("selected_project.node_id must be a string")
                ui_selection.set_selected_project(path, node_id)
            else:
                raise ValueError("selected_project must be an object or null")
        if "remembered_session" in body:
            rem = body["remembered_session"]
            if not isinstance(rem, dict):
                raise ValueError("remembered_session must be an object")
            path = rem.get("path")
            session_id = rem.get("session_id")
            node_id = rem.get("node_id", ui_selection.DEFAULT_NODE_ID)
            if not isinstance(path, str) or not path:
                raise ValueError("remembered_session.path must be a non-empty string")
            if not isinstance(session_id, str) or not session_id:
                raise ValueError("remembered_session.session_id must be a non-empty string")
            if not isinstance(node_id, str):
                raise ValueError("remembered_session.node_id must be a string")
            ui_selection.set_remembered_session(path, node_id, session_id)
        if "open_session_tab_ids" in body:
            open_ids = body["open_session_tab_ids"]
            if not isinstance(open_ids, list):
                raise ValueError("open_session_tab_ids must be a list")
            if any(not isinstance(sid, str) or not sid for sid in open_ids):
                raise ValueError("open_session_tab_ids entries must be non-empty strings")
            ui_selection.set_open_session_tab_ids(open_ids)
        if "open_session_tab_joined_at" in body:
            joined_at = body["open_session_tab_joined_at"]
            if not isinstance(joined_at, dict):
                raise ValueError("open_session_tab_joined_at must be an object")
            if any(
                not isinstance(sid, str)
                or not sid
                or not isinstance(value, str)
                or not value
                for sid, value in joined_at.items()
            ):
                raise ValueError("open_session_tab_joined_at entries must be non-empty strings")
            ui_selection.set_open_session_tab_joined_at(joined_at)
        return ui_selection.get_all()

    try:
        snapshot = await hot_path.run("ui_selection.patch", _patch_sync)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await broadcast_global("ui_selection_changed", snapshot)
    return snapshot


# ---- Shortcut responses ----


@router.post("/api/shortcuts/pick")
async def pick_shortcuts(body: dict = Body(...)):
    assistant_text = body.get("assistant_text", "")
    shortcuts = await shortcut_picker.pick_shortcuts(assistant_text)
    return {"shortcuts": shortcuts}
