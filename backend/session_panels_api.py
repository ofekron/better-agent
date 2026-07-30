"""Per-session UI annotation state: inline tags, notes, the right-panel
selection, the file/config panel stacks, and the chat-input draft."""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Body, Header, HTTPException, Query

import extension_store
import internal_guards
from i18n import t
from session_helpers import require_session_async as _require_session_async
from session_manager import manager as session_manager

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/api/sessions/{session_id}/tags")


async def add_inline_tag(session_id: str, body: dict):
    await _require_session_async(session_id)
    tag = {
        "id": body["id"],
        "messageId": body["messageId"],
        "selectedText": body["selectedText"],
        "comment": body["comment"],
        "timestamp": body["timestamp"],
    }
    # File-anchored tags carry an extra `fileAnchor`. Two flavors:
    #   - Monaco selection (eng overlay or FileViewer Monaco view) →
    #     line:col fields present.
    #   - Rendered-DOM selection (FileViewer markdown / CSV / TSV) →
    #     line:col absent; only `filePath` + `selectedText` carry the
    #     positional info.
    file_anchor = body.get("fileAnchor")
    if isinstance(file_anchor, dict):
        anchor: dict = {"filePath": str(file_anchor.get("filePath", ""))}
        for key in ("startLine", "endLine", "startCol", "endCol"):
            val = file_anchor.get(key)
            if isinstance(val, (int, float)):
                anchor[key] = int(val)
        tag["fileAnchor"] = anchor
    await asyncio.to_thread(
        session_manager.add_tag,
        session_id, tag, client_id=body.get("client_id"),
    )
    return tag


@router.patch("/api/sessions/{session_id}/tags/{tag_id}")


async def update_inline_tag(
    session_id: str, tag_id: str,
    body: dict = Body(default={}),
    client_id: str = Query(None),
):
    await _require_session_async(session_id)
    comment = body.get("comment")
    if not isinstance(comment, str):
        return {"updated": False, "error": "comment must be a string"}
    await asyncio.to_thread(
        session_manager.update_tag,
        session_id, tag_id, {"comment": comment}, client_id=client_id,
    )
    return {"updated": True}


@router.delete("/api/sessions/{session_id}/tags/{tag_id}")


async def remove_inline_tag(
    session_id: str, tag_id: str, client_id: str = Query(None)
):
    await _require_session_async(session_id)
    await asyncio.to_thread(
        session_manager.remove_tag,
        session_id,
        tag_id,
        client_id=client_id,
    )
    return {"deleted": True}


@router.delete("/api/sessions/{session_id}/tags")


async def clear_inline_tags(session_id: str, client_id: str = Query(None)):
    await _require_session_async(session_id)
    await asyncio.to_thread(session_manager.clear_tags, session_id, client_id=client_id)
    return {"cleared": True}


# ── Notes ──────────────────────────────────────────────────────────

@router.post("/api/sessions/{session_id}/notes")


async def add_note(session_id: str, body: dict):
    await _require_session_async(session_id)
    text = body.get("text", "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Note text is required")
    sess = await asyncio.to_thread(
        session_manager.add_note,
        session_id, text, client_id=body.get("client_id"),
    )
    if not sess:
        raise HTTPException(status_code=404, detail=t("error.session_not_found_retry"))
    return {"notes": sess.get("notes", [])}


@router.delete("/api/sessions/{session_id}/notes/{note_id}")


async def remove_note(session_id: str, note_id: str, client_id: str = Query(None)):
    await _require_session_async(session_id)
    await asyncio.to_thread(
        session_manager.remove_note,
        session_id,
        note_id,
        client_id=client_id,
    )
    return {"deleted": True}


@router.patch("/api/sessions/{session_id}/notes/{note_id}")


async def update_note(session_id: str, note_id: str, body: dict):
    await _require_session_async(session_id)
    text = body.get("text", "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Note text is required")
    sess = await asyncio.to_thread(
        session_manager.update_note,
        session_id, note_id, text, client_id=body.get("client_id"),
    )
    if not sess:
        raise HTTPException(status_code=404, detail=t("error.session_not_found_retry"))
    return {"notes": sess.get("notes", [])}


# ── Right panel ────────────────────────────────────────────────────

# Single source for tab validation across the public PATCH and the
# internal POST endpoints. Add new tab ids here, not at each handler.
_VALID_RIGHT_PANEL_TABS = {"files", "notes", "canvas", "comments", "todos", "screen", "changes", "communications", "board"}
_VALID_RIGHT_PANEL_AUTO_REASONS = {"files", "notes", "canvas", "comments", "todos", "navigate", "screen", "communications", "board"}


def _optional_positive_int(body: dict, key: str) -> int | None:
    if key not in body:
        return None
    value = body.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise HTTPException(status_code=400, detail=f"{key} must be a positive integer")
    return value


def _right_panel_patch_from_body(body: dict) -> dict:
    patch: dict = {}
    if "open" in body:
        if not isinstance(body.get("open"), bool):
            raise HTTPException(status_code=400, detail="open must be a boolean")
        patch["open"] = body["open"]
    if "tab" in body:
        tab_val = body.get("tab")
        if tab_val is not None and tab_val not in _VALID_RIGHT_PANEL_TABS:
            raise HTTPException(status_code=400, detail=f"Invalid tab: {tab_val!r}")
        patch["tab"] = tab_val
        patch["tab_set"] = True
    width = _optional_positive_int(body, "width")
    if width is not None:
        patch["width"] = width
    mobile_height = _optional_positive_int(body, "mobile_height")
    if mobile_height is not None:
        patch["mobile_height"] = mobile_height
    if "todos_dismissed" in body:
        if not isinstance(body.get("todos_dismissed"), bool):
            raise HTTPException(status_code=400, detail="todos_dismissed must be a boolean")
        patch["todos_dismissed"] = body["todos_dismissed"]
    if "auto_opened_by" in body:
        reasons = body.get("auto_opened_by")
        if not isinstance(reasons, list) or any(
            not isinstance(reason, str) or reason not in _VALID_RIGHT_PANEL_AUTO_REASONS
            for reason in reasons
        ):
            raise HTTPException(status_code=400, detail="auto_opened_by contains an invalid reason")
        patch["auto_opened_by"] = list(dict.fromkeys(reasons))
    if "sidebar_minimized" in body:
        if not isinstance(body.get("sidebar_minimized"), bool):
            raise HTTPException(status_code=400, detail="sidebar_minimized must be a boolean")
        patch["sidebar_minimized"] = body["sidebar_minimized"]
    if not patch:
        raise HTTPException(status_code=400, detail="At least one right-panel field must be present")
    return patch


def _right_panel_response(sess: dict) -> dict:
    return {
        "right_panel_open": sess.get("right_panel_open"),
        "right_panel_active_tab": sess.get("right_panel_active_tab"),
        "right_panel_width": sess.get("right_panel_width"),
        "right_panel_mobile_height": sess.get("right_panel_mobile_height"),
        "right_panel_todos_dismissed": sess.get("right_panel_todos_dismissed"),
        "right_panel_auto_opened_by": list(sess.get("right_panel_auto_opened_by") or []),
        "sidebar_minimized": sess.get("sidebar_minimized"),
    }


@router.patch("/api/sessions/{session_id}/right-panel")


async def patch_right_panel(session_id: str, body: dict):
    """Update right-panel UI state (open/closed + active tab).

    Body: `{open?: bool, tab?: 'files'|'notes'|'canvas'|'comments',
    client_id: str}`. At least one of open/tab must be present.
    Echoes via `session_metadata_updated` (kind: right_panel_set);
    originating tab drops its own echo via `client_id` match."""
    await _require_session_async(session_id)
    patch = _right_panel_patch_from_body(body)
    sess = await asyncio.to_thread(
        session_manager.set_right_panel,
        session_id,
        **patch,
        client_id=body.get("client_id"),
    )
    if not sess:
        raise HTTPException(
            status_code=404, detail=t("error.session_not_found_retry"),
        )
    return _right_panel_response(sess)


@router.post("/api/internal/sessions/{session_id}/right-panel")


async def internal_set_right_panel(
    session_id: str,
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    """Internal-token-authed twin of PATCH /api/sessions/{id}/right-panel.

    Lets extensions (via better_agent_sdk.Client.set_right_panel) open
    the right panel and switch its active tab without holding a user
    cookie. Same validation rules and same broadcast as the public
    endpoint. The ``client_id`` echoed on the broadcast is pinned to the
    calling extension id — extensions cannot suppress another tab's
    echo by spoofing client_id."""
    if not internal_guards.authority_is_valid():
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    extension_id = internal_guards.internal_authority_extension_id() or ""
    if not extension_id or not extension_store.is_extension_active(extension_id):
        raise HTTPException(status_code=403, detail="extension is not active")
    await _require_session_async(session_id)
    patch = _right_panel_patch_from_body(body)
    sess = await asyncio.to_thread(
        session_manager.set_right_panel,
        session_id,
        **patch,
        client_id=f"ext:{extension_id}",
    )
    if not sess:
        raise HTTPException(
            status_code=404, detail=t("error.session_not_found_retry"),
        )
    return _right_panel_response(sess)


def _sanitize_file_panel(raw: dict) -> dict:
    """Build a persisted open-file-panel dict from request input.

    Shape: {id, path, focus?: {startLine,endLine},
    selection?: {startLine,endLine}}. `path` is required; focus /
    selection are optional integer line ranges (the agent-/user-
    requested scroll + highlight — NOT the user's live viewport,
    which stays frontend-transient)."""
    path = str(raw.get("path") or "").strip()
    if not path:
        raise HTTPException(status_code=400, detail=t("error.file_panel_path_required"))

    def _range(val) -> Optional[dict]:
        if not isinstance(val, dict):
            return None
        s, e = val.get("startLine"), val.get("endLine")
        if not isinstance(s, (int, float)) or not isinstance(e, (int, float)):
            return None
        return {"startLine": int(s), "endLine": int(e)}

    return {
        "id": str(raw.get("id") or uuid.uuid4().hex[:12]),
        "path": path,
        "focus": _range(raw.get("focus")),
        "selection": _range(raw.get("selection")),
    }


@router.post("/api/sessions/{session_id}/file-panels")


async def add_file_panel(session_id: str, body: dict):
    await _require_session_async(session_id)
    panel = _sanitize_file_panel(body)
    await asyncio.to_thread(
        session_manager.add_open_file_panel,
        session_id, panel, client_id=body.get("client_id"),
    )
    return panel


@router.delete("/api/sessions/{session_id}/file-panels/{panel_id}")


async def remove_file_panel(
    session_id: str, panel_id: str, client_id: str = Query(None)
):
    await _require_session_async(session_id)
    await asyncio.to_thread(
        session_manager.remove_open_file_panel,
        session_id,
        panel_id,
        client_id=client_id,
    )
    return {"deleted": True}


@router.put("/api/sessions/{session_id}/file-panels")


async def set_file_panels(session_id: str, body: dict):
    """Replace the full ordered panel list (covers reorder + clear)."""
    await _require_session_async(session_id)
    raw_panels = body.get("panels")
    if not isinstance(raw_panels, list):
        raise HTTPException(status_code=400, detail=t("error.file_panels_list_required"))
    panels = [_sanitize_file_panel(p) for p in raw_panels]
    await asyncio.to_thread(
        session_manager.set_open_file_panels,
        session_id, panels, client_id=body.get("client_id"),
    )
    return {"panels": panels}


def _sanitize_config_panel(raw: dict) -> dict:
    """Build a persisted open-config-panel dict from request input.

    Shape: {id, capability_id, scope, cwd}. `capability_id` is required;
    `scope` is 'global' | 'project' (default 'project'); `cwd` is the
    project path for project-scope panels (empty for global)."""
    capability_id = str(raw.get("capability_id") or "").strip()
    if not capability_id:
        raise HTTPException(status_code=400, detail="capability_id is required")
    scope = str(raw.get("scope") or "project").strip()
    if scope not in ("global", "project"):
        scope = "project"
    return {
        "id": str(raw.get("id") or uuid.uuid4().hex[:12]),
        "capability_id": capability_id,
        "scope": scope,
        "cwd": str(raw.get("cwd") or "").strip(),
    }


@router.post("/api/sessions/{session_id}/config-panels")


async def add_config_panel(session_id: str, body: dict):
    await _require_session_async(session_id)
    panel = _sanitize_config_panel(body)
    await asyncio.to_thread(
        session_manager.add_open_config_panel,
        session_id, panel, client_id=body.get("client_id"),
    )
    return panel


@router.delete("/api/sessions/{session_id}/config-panels/{panel_id}")


async def remove_config_panel(
    session_id: str, panel_id: str, client_id: str = Query(None)
):
    await _require_session_async(session_id)
    await asyncio.to_thread(
        session_manager.remove_open_config_panel,
        session_id,
        panel_id,
        client_id=client_id,
    )
    return {"deleted": True}


@router.put("/api/sessions/{session_id}/config-panels")


async def set_config_panels(session_id: str, body: dict):
    """Replace the full ordered config-panel list (covers reorder + clear)."""
    await _require_session_async(session_id)
    raw_panels = body.get("panels")
    if not isinstance(raw_panels, list):
        raise HTTPException(status_code=400, detail="panels must be a list")
    panels = [_sanitize_config_panel(p) for p in raw_panels]
    await asyncio.to_thread(
        session_manager.set_open_config_panels,
        session_id, panels, client_id=body.get("client_id"),
    )
    return {"panels": panels}


@router.patch("/api/sessions/{session_id}/draft")


async def set_session_draft(session_id: str, body: dict):
    """Persist the in-progress chat input for this session. Called on a
    debounced cadence from every keystroke. `bump_updated_at=False` so
    typing doesn't reorder the sidebar.

    Stale-write guard: the body MUST carry `client_seq` (the client's
    monotonic timestamp at PATCH-send time, e.g. Date.now()). If
    `client_seq <= stored draft_input_seq` the PATCH is dropped — this
    prevents a slow-network typing-PATCH from arriving AFTER a
    send-PATCH that cleared the field and resurrecting stale text on
    disk. Returns the canonical state either way (so the caller can
    self-heal if rejected)."""
    session = await asyncio.to_thread(session_manager.get_lite, session_id)
    if not session:
        raise HTTPException(status_code=404, detail=t("error.session_not_found_retry"))
    draft = body.get("draft_input")
    if not isinstance(draft, str):
        raise HTTPException(status_code=400, detail=t("error.draft_input_must_be_string"))
    client_seq = body.get("client_seq")
    if not isinstance(client_seq, (int, float)):
        raise HTTPException(status_code=400, detail=t("error.client_seq_must_be_number"))
    client_seq = int(client_seq)
    stored_seq = int(session.get("draft_input_seq") or 0)
    if client_seq <= stored_seq:
        return {
            "draft_input": session.get("draft_input", ""),
            "draft_input_seq": stored_seq,
            "rejected": True,
        }
    draft_images = body.get("draft_images")
    if draft_images is not None and not isinstance(draft_images, list):
        raise HTTPException(status_code=400, detail="draft_images must be an array")
    await asyncio.to_thread(
        session_manager.set_draft,
        session_id,
        draft,
        client_seq,
        images=draft_images,
        client_id=body.get("client_id"),
    )
    result = {"draft_input": draft, "draft_input_seq": client_seq}
    if draft_images is not None:
        result["draft_images"] = draft_images
    return result
