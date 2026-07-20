from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from json_store import read_json, write_json
from paths import bc_home
from portable_lock import lock_ex, unlock
import session_store


SCHEMA_VERSION = 1
MAX_MESSAGE_CHARS = 10_000


class InboxStoreError(ValueError):
    pass


def _root() -> Path:
    return bc_home() / "inboxes"


def _clean_session_id(value: Any, field: str) -> str:
    clean = str(value or "").strip()
    if not clean:
        raise InboxStoreError(f"{field} is required")
    if any(part in clean for part in ("/", "\\", "..")):
        raise InboxStoreError(f"{field} is invalid")
    return clean


def _path(recipient_session_id: str) -> Path:
    clean = _clean_session_id(recipient_session_id, "recipient_session_id")
    return _root() / f"{clean}.json"


def _require_session(session_id: str) -> None:
    if session_store.get_session(session_id) is None:
        raise InboxStoreError("recipient session does not exist")


def _assert_regular_or_missing(path: Path) -> None:
    if path.is_symlink():
        raise InboxStoreError("inbox path must not be a symlink")
    if path.exists() and not path.is_file():
        raise InboxStoreError("inbox path must be a regular file")


@contextmanager
def _locked(recipient_session_id: str) -> Iterator[None]:
    clean = _clean_session_id(recipient_session_id, "recipient_session_id")
    root = _root()
    if root.is_symlink():
        raise InboxStoreError("inbox root must not be a symlink")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not root.is_dir():
        raise InboxStoreError("inbox root must be a directory")
    lock_path = root / f".{clean}.lock"
    _assert_regular_or_missing(lock_path)
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(lock_path, flags, 0o600)
    try:
        lock_ex(fd)
        _assert_regular_or_missing(_path(clean))
        yield
    finally:
        unlock(fd)
        os.close(fd)


def _load(recipient_session_id: str) -> dict[str, Any]:
    path = _path(recipient_session_id)
    if not path.exists():
        return {
            "schema_version": SCHEMA_VERSION,
            "recipient_session_id": recipient_session_id,
            "messages": [],
            "cursor": 0,
        }
    record = read_json(path, {})
    if record.get("schema_version") != SCHEMA_VERSION:
        raise InboxStoreError(
            "Unsupported inbox store schema; wipe inboxes/*.json to start fresh"
        )
    if record.get("recipient_session_id") != recipient_session_id:
        raise InboxStoreError("inbox recipient does not match its storage key")
    if not isinstance(record.get("messages"), list):
        raise InboxStoreError("inbox messages must be a list")
    if not isinstance(record.get("cursor"), int):
        raise InboxStoreError("inbox cursor must be an integer")
    return record


def send(
    *,
    sender_session_id: str,
    recipient_session_id: str,
    message: str,
) -> dict[str, Any]:
    sender_session_id = _clean_session_id(sender_session_id, "sender_session_id")
    recipient_session_id = _clean_session_id(
        recipient_session_id,
        "recipient_session_id",
    )
    text = str(message or "").strip()
    if not text:
        raise InboxStoreError("message is required when recipient_session_id is set")
    if len(text) > MAX_MESSAGE_CHARS:
        raise InboxStoreError(f"message exceeds {MAX_MESSAGE_CHARS} characters")
    _require_session(sender_session_id)
    _require_session(recipient_session_id)
    with _locked(recipient_session_id):
        record = _load(recipient_session_id)
        messages = record["messages"]
        seq = max((int(item.get("seq", 0)) for item in messages), default=0) + 1
        messages.append({
            "seq": seq,
            "sender_session_id": sender_session_id,
            "text": text,
            "ts": time.time(),
        })
        write_json(_path(recipient_session_id), record)
    return {
        "recipient_session_id": recipient_session_id,
        "sent": True,
        "seq": seq,
    }


def post_or_read(
    *,
    caller_session_id: str,
    recipient_session_id: str = "",
    message: str = "",
) -> dict[str, Any]:
    caller_session_id = _clean_session_id(caller_session_id, "caller_session_id")
    recipient_session_id = str(recipient_session_id or "").strip()
    text = str(message or "").strip()
    if recipient_session_id or text:
        if not recipient_session_id or not text:
            raise InboxStoreError(
                "recipient_session_id and message are both required when sending"
            )
        return send(
            sender_session_id=caller_session_id,
            recipient_session_id=recipient_session_id,
            message=text,
        )
    return read_new(recipient_session_id=caller_session_id)


def read_new(*, recipient_session_id: str) -> dict[str, Any]:
    recipient_session_id = _clean_session_id(
        recipient_session_id,
        "recipient_session_id",
    )
    _require_session(recipient_session_id)
    with _locked(recipient_session_id):
        record = _load(recipient_session_id)
        cursor = int(record["cursor"])
        messages = record["messages"]
        new_messages = [
            item for item in messages if int(item.get("seq", 0)) > cursor
        ]
        if messages:
            cursor = max(int(item.get("seq", 0)) for item in messages)
            record["cursor"] = cursor
        write_json(_path(recipient_session_id), record)
    return {
        "recipient_session_id": recipient_session_id,
        "new_messages": new_messages,
        "count": len(new_messages),
        "cursor": cursor,
    }


def read_history(
    *,
    recipient_session_id: str,
    limit: int = 50,
    before_seq: int | None = None,
) -> dict[str, Any]:
    recipient_session_id = _clean_session_id(
        recipient_session_id,
        "recipient_session_id",
    )
    _require_session(recipient_session_id)
    clean_limit = max(1, min(int(limit or 50), 200))
    clean_before_seq = int(before_seq) if before_seq is not None else None
    with _locked(recipient_session_id):
        messages = _load(recipient_session_id)["messages"]
        bounded = [
            item
            for item in messages
            if clean_before_seq is None
            or int(item.get("seq", 0)) < clean_before_seq
        ]
        page = bounded[-clean_limit:]
        next_before_seq = (
            int(page[0].get("seq", 0))
            if len(bounded) > len(page) and page
            else None
        )
    return {
        "recipient_session_id": recipient_session_id,
        "messages": page,
        "count": len(page),
        "next_before_seq": next_before_seq,
    }
