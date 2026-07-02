"""Shared team chat store.

A team-wide chat room persisted as one JSON file per chat under
``ba_home()/chats``. Every team session reads the same chat; each reader
tracks its own last-read cursor so a call returns only the messages new
since that reader last looked. Messages are stamped with the sender id.

Read-modify-write (post + cursor advance) is guarded by an advisory file
lock so concurrent posts from different sessions cannot lose appends;
``write_json``'s atomic rename keeps on-disk state crash-safe.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from json_store import read_json, write_json
from paths import ba_home

try:  # POSIX only; absent on Windows.
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - non-POSIX
    _fcntl = None

SCHEMA_VERSION = 1


class ChatStoreError(ValueError):
    pass


def _root() -> Path:
    return ba_home() / "chats"


def _clean_id(value: Any, field: str) -> str:
    clean = str(value or "").strip()
    if not clean:
        raise ChatStoreError(f"{field} is required")
    if any(part in clean for part in ("/", "\\", "..")):
        raise ChatStoreError(f"{field} is invalid")
    return clean


def _path(chat_id: str) -> Path:
    return _root() / f"{_clean_id(chat_id, 'chat_id')}.json"


def _now() -> float:
    import time

    return time.time()


@contextmanager
def _locked(chat_id: str) -> Iterator[None]:
    """Exclusive advisory lock for this chat's read-modify-write window.
    No-op on Windows (no fcntl); atomic rename still guards partial writes."""
    clean = _clean_id(chat_id, "chat_id")
    if _fcntl is None:
        yield
        return
    _root().mkdir(parents=True, exist_ok=True)
    lock = open(_root() / f".{clean}.lock", "w")
    try:
        _fcntl.flock(lock.fileno(), _fcntl.LOCK_EX)
        yield
    finally:
        _fcntl.flock(lock.fileno(), _fcntl.LOCK_UN)
        lock.close()


def _blank(chat_id: str, created_by: str, name: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "id": chat_id,
        "name": name,
        "created_by": created_by,
        "created_at": _now(),
        "messages": [],
        "cursors": {},
    }


def _coerce(record: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    messages = record.get("messages")
    if not isinstance(messages, list):
        messages = []
    cursors = record.get("cursors")
    if not isinstance(cursors, dict):
        cursors = {}
    return messages, cursors


def create_chat(*, chat_id: str, created_by: str, name: str = "") -> dict[str, Any]:
    chat_id = _clean_id(chat_id, "chat_id")
    with _locked(chat_id):
        path = _path(chat_id)
        if path.exists():
            raise ChatStoreError("chat_id already exists")
        record = _blank(chat_id, created_by, str(name or "").strip())
        write_json(path, record)
        return record


def delete_chat(chat_id: str) -> bool:
    chat_id = _clean_id(chat_id, "chat_id")
    with _locked(chat_id):
        path = _path(chat_id)
        if not path.exists():
            return False
        path.unlink()
        return True


def list_chats() -> list[dict[str, Any]]:
    root = _root()
    if not root.exists():
        return []
    chats: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.json")):
        record = read_json(path, {})
        if not isinstance(record, dict):
            continue
        if record.get("schema_version") != SCHEMA_VERSION:
            continue
        messages, cursors = _coerce(record)
        chats.append({
            "id": str(record.get("id") or path.stem),
            "name": str(record.get("name") or ""),
            "created_by": str(record.get("created_by") or ""),
            "created_at": record.get("created_at"),
            "messages": list(messages),
            "cursors": dict(cursors),
        })
    return chats


def post_and_read(*, chat_id: str, reader_id: str, message: str) -> dict[str, Any]:
    """Append a non-empty message (stamped with reader_id) and return every
    message newer than this reader's last-read cursor, then advance the
    cursor to the newest message. An empty/whitespace message is not stored;
    the call is a read-only check in that case."""
    chat_id = _clean_id(chat_id, "chat_id")
    reader_id = str(reader_id or "").strip()
    if not reader_id:
        raise ChatStoreError("reader_id is required")
    text = str(message or "").strip()
    with _locked(chat_id):
        path = _path(chat_id)
        if not path.exists():
            raise ChatStoreError("chat_id does not exist; create it first")
        record = read_json(path, {})
        if record.get("schema_version") != SCHEMA_VERSION:
            raise ChatStoreError("Unsupported chat store schema; wipe chats/*.json to start fresh")
        messages, cursors = _coerce(record)
        if text:
            seq = max((int(m.get("seq", 0)) for m in messages), default=0) + 1
            messages.append(
                {"seq": seq, "sender_id": reader_id, "text": text, "ts": _now()}
            )
            record["messages"] = messages
        prev_cursor = int(cursors.get(reader_id, 0))
        new_messages = [m for m in messages if int(m.get("seq", 0)) > prev_cursor]
        if messages:
            cursors[reader_id] = max(int(m.get("seq", 0)) for m in messages)
            record["cursors"] = cursors
        write_json(path, record)
        return {
            "chat_id": chat_id,
            "new_messages": new_messages,
            "count": len(new_messages),
            "cursor": cursors.get(reader_id, prev_cursor),
        }
