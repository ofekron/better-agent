from __future__ import annotations

import re
from typing import Any

import app_chat_draft_store


_SESSION_DETAIL = re.compile(r"^/api/sessions/([A-Za-z0-9_-]+)$")


def needs_json_projection(path: str) -> bool:
    return _SESSION_DETAIL.fullmatch(path) is not None


def _overlay_tree(node: Any) -> None:
    if not isinstance(node, dict):
        return
    session_id = node.get("id")
    if isinstance(session_id, str):
        draft = app_chat_draft_store.get(session_id)
        node.update({
            "draft_input": draft["draft_input"],
            "draft_input_seq": draft["draft_input_seq"],
            "draft_images": draft["draft_images"],
        })
    for child in node.get("forks") or []:
        _overlay_tree(child)


def project_json(path: str, payload: Any) -> Any:
    if needs_json_projection(path):
        _overlay_tree(payload)
    return payload
