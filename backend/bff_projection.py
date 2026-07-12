from __future__ import annotations

import re
from typing import Any

import app_chat_draft_store


_SESSION_DETAIL = re.compile(r"^/api/sessions/([A-Za-z0-9_-]+)$")
_SESSION_LIST_PATHS = frozenset({
    "/api/sessions",
    "/api/sessions/topbar-pinned",
    "/api/sessions/summaries",
})

_DRAFT_KEYS = ("draft_input", "draft_input_seq", "draft_images")


def needs_json_projection(path: str) -> bool:
    return _SESSION_DETAIL.fullmatch(path) is not None or path in _SESSION_LIST_PATHS


def _overlay_tree(node: Any) -> None:
    if not isinstance(node, dict):
        return
    session_id = node.get("id")
    if isinstance(session_id, str):
        draft = app_chat_draft_store.get(session_id)
        node.update({key: draft[key] for key in _DRAFT_KEYS})
    for child in node.get("forks") or []:
        _overlay_tree(child)


def _overlay_list(sessions: Any) -> None:
    if not isinstance(sessions, list):
        return
    ids = [item["id"] for item in sessions if isinstance(item, dict) and isinstance(item.get("id"), str)]
    drafts = app_chat_draft_store.get_many(ids)
    for item in sessions:
        if not isinstance(item, dict):
            continue
        session_id = item.get("id")
        draft = drafts.get(session_id) if isinstance(session_id, str) else None
        if draft is not None:
            item.update({key: draft[key] for key in _DRAFT_KEYS})


def project_json(path: str, payload: Any) -> Any:
    if path in _SESSION_LIST_PATHS:
        if isinstance(payload, dict):
            _overlay_list(payload.get("sessions"))
    elif _SESSION_DETAIL.fullmatch(path) is not None:
        _overlay_tree(payload)
    return payload
