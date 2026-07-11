from __future__ import annotations

import os
import json
from pathlib import Path
import threading
import time
from typing import Any

from json_store import read_json, write_json
from paths import ba_home


_lock = threading.RLock()


def _safe_session_id(value: object) -> str:
    session_id = str(value or "")
    if not session_id or len(session_id) > 128 or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for ch in session_id):
        raise ValueError("session_id must be a non-empty safe id")
    return session_id


def _root() -> Path:
    path = ba_home() / "app-state" / "chat-drafts"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != "nt":
        path.chmod(0o700)
    return path


def _path(session_id: str) -> Path:
    return _root() / f"{_safe_session_id(session_id)}.json"


def empty(session_id: str) -> dict[str, Any]:
    return {
        "session_id": _safe_session_id(session_id),
        "draft_input": "",
        "draft_input_seq": 0,
        "draft_images": [],
    }


def get(session_id: str) -> dict[str, Any]:
    session_id = _safe_session_id(session_id)
    with _lock:
        raw = read_json(_path(session_id), {})
    if not isinstance(raw, dict):
        return empty(session_id)
    text = raw.get("draft_input")
    seq = raw.get("draft_input_seq")
    images = raw.get("draft_images")
    return {
        "session_id": session_id,
        "draft_input": text if isinstance(text, str) else "",
        "draft_input_seq": seq if isinstance(seq, int) and not isinstance(seq, bool) else 0,
        "draft_images": images if isinstance(images, list) else [],
    }


def update(
    session_id: str,
    *,
    draft_input: object,
    client_seq: object,
    draft_images: object = None,
) -> dict[str, Any]:
    session_id = _safe_session_id(session_id)
    if not isinstance(draft_input, str):
        raise ValueError("draft_input must be a string")
    if len(draft_input.encode("utf-8")) > 2 * 1024 * 1024:
        raise ValueError("draft_input exceeds 2 MiB")
    if isinstance(client_seq, bool) or not isinstance(client_seq, (int, float)):
        raise ValueError("client_seq must be a number")
    seq = int(client_seq)
    if draft_images is not None and not isinstance(draft_images, list):
        raise ValueError("draft_images must be an array")
    if isinstance(draft_images, list):
        if len(draft_images) > 32:
            raise ValueError("draft_images exceeds 32 items")
        if len(json.dumps(draft_images, separators=(",", ":")).encode("utf-8")) > 16 * 1024 * 1024:
            raise ValueError("draft_images exceeds 16 MiB")
    with _lock:
        current = get(session_id)
        if seq <= current["draft_input_seq"]:
            return {**current, "rejected": True}
        record = {
            "session_id": session_id,
            "draft_input": draft_input,
            "draft_input_seq": seq,
            "draft_images": (
                draft_images
                if draft_images is not None
                else current["draft_images"]
            ),
            "updated_at": time.time(),
        }
        write_json(_path(session_id), record)
    return {key: record[key] for key in ("session_id", "draft_input", "draft_input_seq", "draft_images")}


def delete(session_id: str) -> None:
    with _lock:
        _path(session_id).unlink(missing_ok=True)
