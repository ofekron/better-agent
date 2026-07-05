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
HISTORY_MODE_UNREAD = "unread_history"
HISTORY_MODE_CAUGHT_UP = "caught_up"
SENDER_POLICY_OPEN = "open"
SENDER_POLICY_ALLOWLIST = "allowlist"
SENDER_POLICY_DISALLOWLIST = "disallowlist"
SENDER_POLICIES = {
    SENDER_POLICY_OPEN,
    SENDER_POLICY_ALLOWLIST,
    SENDER_POLICY_DISALLOWLIST,
}


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


def _bool_setting(value: Any, field: str, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        clean = value.strip().lower()
        if clean in {"true", "1", "yes", "on"}:
            return True
        if clean in {"false", "0", "no", "off"}:
            return False
    raise ChatStoreError(f"{field} must be a boolean")


def _history_mode(value: Any) -> str:
    clean = str(value or "").strip()
    if not clean:
        return ""
    if clean in {HISTORY_MODE_UNREAD, HISTORY_MODE_CAUGHT_UP}:
        return clean
    raise ChatStoreError(
        f"history_mode must be {HISTORY_MODE_UNREAD!r} or {HISTORY_MODE_CAUGHT_UP!r}"
    )


def _sender_policy(value: Any) -> str:
    clean = str(value or "").strip()
    if not clean:
        return SENDER_POLICY_OPEN
    if clean in SENDER_POLICIES:
        return clean
    raise ChatStoreError(
        "sender_policy must be 'open', 'allowlist', or 'disallowlist'"
    )


def _clean_session_ids(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ChatStoreError(f"{field} must be a list")
    seen: set[str] = set()
    result: list[str] = []
    for item in value:
        clean = _clean_id(item, field)
        if clean not in seen:
            seen.add(clean)
            result.append(clean)
    return result


def _head(messages: list[dict[str, Any]]) -> int:
    return max((int(m.get("seq", 0)) for m in messages), default=0)


def _blank(
    chat_id: str,
    created_by: str,
    name: str,
    *,
    new_readers_see_history: bool,
    sender_policy: str,
    sender_ids: list[str],
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "id": chat_id,
        "name": name,
        "created_by": created_by,
        "created_at": _now(),
        "new_readers_see_history": new_readers_see_history,
        "sender_policy": sender_policy,
        "sender_ids": sender_ids,
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


def create_chat(
    *,
    chat_id: str,
    created_by: str,
    name: str = "",
    new_readers_see_history: bool = True,
    sender_policy: str = SENDER_POLICY_OPEN,
    sender_ids: list[str] | None = None,
) -> dict[str, Any]:
    chat_id = _clean_id(chat_id, "chat_id")
    created_by = _clean_id(created_by, "created_by")
    new_readers_see_history = _bool_setting(
        new_readers_see_history,
        "new_readers_see_history",
        default=True,
    )
    sender_policy = _sender_policy(sender_policy)
    sender_ids = _clean_session_ids(sender_ids, "sender_ids")
    with _locked(chat_id):
        path = _path(chat_id)
        if path.exists():
            raise ChatStoreError("chat_id already exists")
        record = _blank(
            chat_id,
            created_by,
            str(name or "").strip(),
            new_readers_see_history=new_readers_see_history,
            sender_policy=sender_policy,
            sender_ids=sender_ids,
        )
        write_json(path, record)
        return record


def _sender_policy_view(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "sender_policy": _sender_policy(record.get("sender_policy")),
        "sender_ids": _clean_session_ids(record.get("sender_ids"), "sender_ids"),
    }


def _can_post(record: dict[str, Any], sender_id: str) -> bool:
    owner_id = str(record.get("created_by") or "").strip()
    if sender_id == owner_id:
        return True
    policy = _sender_policy(record.get("sender_policy"))
    sender_ids = set(_clean_session_ids(record.get("sender_ids"), "sender_ids"))
    if policy == SENDER_POLICY_OPEN:
        return True
    if policy == SENDER_POLICY_ALLOWLIST:
        return sender_id in sender_ids
    if policy == SENDER_POLICY_DISALLOWLIST:
        return sender_id not in sender_ids
    return False


def set_sender_policy(
    *,
    chat_id: str,
    owner_id: str,
    sender_policy: str,
    sender_ids: list[str] | None = None,
) -> dict[str, Any]:
    chat_id = _clean_id(chat_id, "chat_id")
    owner_id = _clean_id(owner_id, "owner_id")
    sender_policy = _sender_policy(sender_policy)
    sender_ids = _clean_session_ids(sender_ids, "sender_ids")
    with _locked(chat_id):
        path = _path(chat_id)
        if not path.exists():
            raise ChatStoreError("chat_id does not exist; create it first")
        record = read_json(path, {})
        if record.get("schema_version") != SCHEMA_VERSION:
            raise ChatStoreError("Unsupported chat store schema; wipe chats/*.json to start fresh")
        if str(record.get("created_by") or "").strip() != owner_id:
            raise ChatStoreError("only the chat owner can change sender policy")
        record["sender_policy"] = sender_policy
        record["sender_ids"] = sender_ids
        write_json(path, record)
        return {
            "chat_id": chat_id,
            "created_by": owner_id,
            **_sender_policy_view(record),
        }


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
            "new_readers_see_history": _bool_setting(
                record.get("new_readers_see_history"),
                "new_readers_see_history",
                default=True,
            ),
            **_sender_policy_view(record),
            "messages": list(messages),
            "cursors": dict(cursors),
        })
    return chats


def _default_history_mode(record: dict[str, Any]) -> str:
    if _bool_setting(
        record.get("new_readers_see_history"),
        "new_readers_see_history",
        default=True,
    ):
        return HISTORY_MODE_UNREAD
    return HISTORY_MODE_CAUGHT_UP


def post_and_read(
    *,
    chat_id: str,
    reader_id: str,
    message: str,
    history_mode: str = "",
) -> dict[str, Any]:
    """Append a non-empty message (stamped with reader_id) and return every
    message newer than this reader's last-read cursor, then advance the
    cursor to the newest message. An empty/whitespace message is not stored;
    the call is a read-only check in that case."""
    chat_id = _clean_id(chat_id, "chat_id")
    reader_id = str(reader_id or "").strip()
    if not reader_id:
        raise ChatStoreError("reader_id is required")
    text = str(message or "").strip()
    requested_history_mode = _history_mode(history_mode)
    with _locked(chat_id):
        path = _path(chat_id)
        if not path.exists():
            raise ChatStoreError("chat_id does not exist; create it first")
        record = read_json(path, {})
        if record.get("schema_version") != SCHEMA_VERSION:
            raise ChatStoreError("Unsupported chat store schema; wipe chats/*.json to start fresh")
        messages, cursors = _coerce(record)
        current_head = _head(messages)
        if text:
            if not _can_post(record, reader_id):
                raise ChatStoreError("sender is not allowed to post in this chat")
            seq = current_head + 1
            messages.append(
                {"seq": seq, "sender_id": reader_id, "text": text, "ts": _now()}
            )
            record["messages"] = messages
        if reader_id in cursors:
            prev_cursor = int(cursors[reader_id])
        elif (requested_history_mode or _default_history_mode(record)) == HISTORY_MODE_UNREAD:
            prev_cursor = 0
        else:
            prev_cursor = current_head
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


def read_history(
    *,
    chat_id: str,
    limit: int = 50,
    before_seq: int | None = None,
) -> dict[str, Any]:
    chat_id = _clean_id(chat_id, "chat_id")
    clean_limit = max(1, min(int(limit or 50), 200))
    clean_before_seq = int(before_seq) if before_seq is not None else None
    with _locked(chat_id):
        path = _path(chat_id)
        if not path.exists():
            raise ChatStoreError("chat_id does not exist; create it first")
        record = read_json(path, {})
        if record.get("schema_version") != SCHEMA_VERSION:
            raise ChatStoreError("Unsupported chat store schema; wipe chats/*.json to start fresh")
        messages, _cursors = _coerce(record)
        bounded = [
            m for m in messages
            if clean_before_seq is None or int(m.get("seq", 0)) < clean_before_seq
        ]
        page = bounded[-clean_limit:]
        next_before_seq = int(page[0].get("seq", 0)) if len(bounded) > len(page) and page else None
        return {
            "chat_id": chat_id,
            "messages": page,
            "count": len(page),
            "next_before_seq": next_before_seq,
        }
