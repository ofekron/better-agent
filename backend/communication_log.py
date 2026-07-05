from __future__ import annotations

import copy
import heapq
import threading
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import chat_store
import perf
import session_manager as session_manager_module
import session_store
from session_manager import manager as session_manager
import team_messaging


def _iso(value: Any) -> str:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), timezone.utc).isoformat()
    if isinstance(value, str) and value.strip():
        return value.strip()
    return datetime.now(timezone.utc).isoformat()


def _session_names(sessions: list[dict]) -> dict[str, str]:
    names: dict[str, str] = {}
    for session in sessions:
        sid = str(session.get("id") or "")
        if sid:
            names[sid] = str(session.get("name") or sid)
    return names


def _session_name(names: dict[str, str], sid: str) -> str:
    return names.get(sid) or sid


_CACHE_MAX = 16
_cache_lock = threading.Lock()
_response_cache: OrderedDict[tuple, dict] = OrderedDict()


def _chat_files_fingerprint() -> tuple[int, int, int]:
    root = chat_store._root()
    if not root.exists():
        return (0, 0, 0)
    count = 0
    newest_mtime_ns = 0
    total_size = 0
    for path in root.glob("*.json"):
        if not isinstance(path, Path):
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        count += 1
        newest_mtime_ns = max(newest_mtime_ns, stat.st_mtime_ns)
        total_size += stat.st_size
    return (count, newest_mtime_ns, total_size)


def _queued_prompt_fingerprint() -> tuple[tuple[str, int], ...]:
    counts = getattr(session_manager, "_queued_prompt_counts_by_sid", {})
    if not isinstance(counts, dict):
        return ()
    return tuple(
        sorted(
            (str(sid), int(count))
            for sid, count in counts.items()
            if int(count) > 0
        )
    )


def _queued_prompt_session_ids() -> set[str]:
    return {sid for sid, count in _queued_prompt_fingerprint() if count > 0}


def _pending_persist_root_ids() -> set[str]:
    lock = getattr(session_manager_module, "_persist_state_lock", None)
    pending = getattr(session_manager_module, "_persist_pending", {})
    if lock is None or not isinstance(pending, dict):
        return set()
    with lock:
        return {str(root_id) for root_id in pending}


def _flatten_sessions(root: dict) -> dict[str, dict]:
    sessions: dict[str, dict] = {}
    stack = [root]
    while stack:
        node = stack.pop()
        sid = str(node.get("id") or "")
        if sid:
            sessions[sid] = node
        stack.extend(child for child in node.get("forks") or [] if isinstance(child, dict))
    return sessions


def _cache_get(key: tuple) -> dict | None:
    with _cache_lock:
        cached = _response_cache.get(key)
        if cached is None:
            return None
        _response_cache.move_to_end(key)
        return copy.deepcopy(cached)


def _cache_put(key: tuple, value: dict) -> dict:
    with _cache_lock:
        _response_cache[key] = copy.deepcopy(value)
        _response_cache.move_to_end(key)
        while len(_response_cache) > _CACHE_MAX:
            _response_cache.popitem(last=False)
    return value


def _participant(names: dict[str, str], sid: str) -> dict:
    return {
        "session_id": sid,
        "name": _session_name(names, sid),
    }


def _participants(names: dict[str, str], session_ids: list[str]) -> list[dict]:
    seen: set[str] = set()
    result: list[dict] = []
    for sid in session_ids:
        clean = str(sid or "").strip()
        if not clean or clean in seen:
            continue
        seen.add(clean)
        result.append(_participant(names, clean))
    return result


def _addressed_target(metadata: dict) -> dict | None:
    selector = metadata.get("target_selector")
    if not isinstance(selector, dict):
        return None
    kind = str(selector.get("kind") or "").strip()
    value = str(selector.get("value") or "").strip()
    if not kind or not value:
        return None
    target = {"kind": kind, "value": value}
    pool_affinity_key = str(selector.get("pool_affinity_key") or "").strip()
    if pool_affinity_key:
        target["pool_affinity_key"] = pool_affinity_key
    return target


class _EntryLimiter:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.total = 0
        self._sequence = 0
        self._entries: list[tuple[tuple[str, int], dict]] = []

    def add(self, entry: dict) -> None:
        self.total += 1
        rank = (str(entry.get("created_at") or ""), -self._sequence)
        self._sequence += 1
        item = (rank, entry)
        if len(self._entries) < self.limit:
            heapq.heappush(self._entries, item)
            return
        if rank > self._entries[0][0]:
            heapq.heapreplace(self._entries, item)

    def items(self) -> list[dict]:
        return [
            entry
            for _rank, entry in sorted(
                self._entries,
                key=lambda item: item[0],
                reverse=True,
            )
        ]


def _message_entry(
    *,
    session: dict,
    message: dict,
    names: dict[str, str],
    status: str,
) -> dict | None:
    source = str(message.get("source") or "")
    if source not in team_messaging.MESSAGE_SOURCES:
        return None
    team_message = message.get("team_message")
    metadata = dict(message.get("metadata") or {})
    if isinstance(team_message, dict):
        metadata.update(dict(team_message.get("metadata") or {}))
    sender_id = str(metadata.get("sender_session_id") or message.get("sender_session_id") or "")
    if not sender_id:
        return None
    target_id = str(session.get("id") or "")
    body = str(
        (team_message.get("message") if isinstance(team_message, dict) else None)
        or message.get("content")
        or message.get("message")
        or message.get("prompt")
        or ""
    )
    return {
        "id": f"{status}:{target_id}:{message.get('id') or message.get('lifecycle_msg_id') or body[:24]}",
        "kind": source,
        "status": status,
        "created_at": _iso(message.get("timestamp") or message.get("created_at")),
        "from_session_id": sender_id,
        "from_name": str(metadata.get("sender_name") or _session_name(names, sender_id)),
        "to_session_id": target_id,
        "to_name": _session_name(names, target_id),
        "chat_id": None,
        "chat_name": "",
        "participants": _participants(names, [sender_id, target_id]),
        "addressed_target": _addressed_target(metadata),
        "body": body,
    }


def _session_involves(entry: dict, session_id: str) -> bool:
    return entry.get("from_session_id") == session_id or entry.get("to_session_id") == session_id


def _collect_session_entries(
    sessions: list[dict],
    names: dict[str, str],
    *,
    session_id: str,
    entries: _EntryLimiter,
) -> None:
    for session in sessions:
        for message in session.get("messages") or []:
            if isinstance(message, dict):
                entry = _message_entry(
                    session=session,
                    message=message,
                    names=names,
                    status="delivered",
                )
                if entry and (not session_id or _session_involves(entry, session_id)):
                    entries.add(entry)
        for queued in session.get("queued_prompts") or []:
            if isinstance(queued, dict):
                entry = _message_entry(
                    session=session,
                    message=queued,
                    names=names,
                    status="queued",
                )
                if entry and (not session_id or _session_involves(entry, session_id)):
                    entries.add(entry)


def _chat_participant_ids(chat: dict) -> list[str]:
    ids = [str(chat.get("created_by") or "")]
    cursors = chat.get("cursors") if isinstance(chat.get("cursors"), dict) else {}
    ids.extend(str(sid or "") for sid in cursors.keys())
    for message in chat.get("messages") or []:
        if isinstance(message, dict):
            ids.append(str(message.get("sender_id") or ""))
    seen: set[str] = set()
    result: list[str] = []
    for sid in ids:
        clean = sid.strip()
        if clean and clean not in seen:
            seen.add(clean)
            result.append(clean)
    return result


def _collect_chat_entries(
    names: dict[str, str],
    *,
    session_id: str,
    entries: _EntryLimiter,
) -> None:
    for chat in chat_store.list_chats():
        chat_id = str(chat.get("id") or "")
        chat_name = str(chat.get("name") or chat_id)
        participant_ids = _chat_participant_ids(chat)
        if session_id and session_id not in set(participant_ids):
            continue
        participants = _participants(names, participant_ids)
        chat_messages = []
        for message in chat.get("messages") or []:
            if not isinstance(message, dict):
                continue
            sender_id = str(message.get("sender_id") or "")
            if not sender_id:
                continue
            seq = int(message.get("seq") or 0)
            chat_messages.append({
                "id": f"chat:{chat_id}:{seq}",
                "seq": seq,
                "created_at": _iso(message.get("ts")),
                "from_session_id": sender_id,
                "from_name": _session_name(names, sender_id),
                "body": str(message.get("text") or ""),
            })
        chat_messages.sort(key=lambda item: int(item.get("seq") or 0))
        if not chat_messages:
            creator_id = str(chat.get("created_by") or "").strip()
            entries.add({
                "id": f"chat:{chat_id}",
                "kind": "chat",
                "status": "open",
                "created_at": _iso(chat.get("created_at")),
                "from_session_id": creator_id,
                "from_name": _session_name(names, creator_id) if creator_id else "",
                "to_session_id": None,
                "to_name": chat_name,
                "chat_id": chat_id,
                "chat_name": chat_name,
                "participants": participants,
                "body": "",
                "messages": [],
            })
            continue
        latest = chat_messages[-1]
        entries.add({
            "id": f"chat:{chat_id}",
            "kind": "chat",
            "status": "posted",
            "created_at": latest["created_at"],
            "from_session_id": latest["from_session_id"],
            "from_name": latest["from_name"],
            "to_session_id": None,
            "to_name": chat_name,
            "chat_id": chat_id,
            "chat_name": chat_name,
            "participants": participants,
            "body": latest["body"],
            "messages": chat_messages,
        })


def list_communications(*, session_id: str = "", limit: int = 200) -> dict:
    clean_session_id = str(session_id or "").strip()
    clean_limit = max(1, min(int(limit or 200), 500))
    pending_root_ids = _pending_persist_root_ids()
    cache_key = (
        clean_session_id,
        clean_limit,
        session_store.summary_version(),
        _queued_prompt_fingerprint(),
        _chat_files_fingerprint(),
    )
    if not pending_root_ids:
        cached = _cache_get(cache_key)
        if cached is not None:
            perf.record("communications.response_cache.hit", 1.0)
            return cached
    cache_metric = (
        "communications.response_cache.pending_bypass"
        if pending_root_ids
        else "communications.response_cache.miss"
    )
    perf.record(cache_metric, 1.0)
    with perf.timed("communications.sessions.iter_all"):
        sessions = list(session_manager.iter_all())
    queued_session_ids = _queued_prompt_session_ids()
    if queued_session_ids or pending_root_ids:
        with perf.timed("communications.sessions.refresh_live"):
            fresh_sessions: dict[str, dict] = {}
            for root_id in pending_root_ids:
                root = session_manager.get_lite(root_id)
                if isinstance(root, dict):
                    fresh_sessions.update(_flatten_sessions(root))
            refreshed_sessions = []
            seen_session_ids: set[str] = set()
            for session in sessions:
                sid = str(session.get("id") or "")
                if sid:
                    seen_session_ids.add(sid)
                if sid in fresh_sessions:
                    refreshed_sessions.append(fresh_sessions[sid])
                elif sid in queued_session_ids:
                    refreshed_sessions.append(session_manager.get_lite(sid) or session)
                else:
                    refreshed_sessions.append(session)
            for sid, session in fresh_sessions.items():
                if sid not in seen_session_ids:
                    refreshed_sessions.append(session)
            sessions = refreshed_sessions
    names = _session_names(sessions)
    entries = _EntryLimiter(clean_limit)
    with perf.timed("communications.sessions.project"):
        _collect_session_entries(
            sessions,
            names,
            session_id=clean_session_id,
            entries=entries,
        )
    with perf.timed("communications.chats.project"):
        _collect_chat_entries(names, session_id=clean_session_id, entries=entries)
    items = entries.items()
    perf.record("communications.items.total", float(entries.total))
    result = {
        "items": items,
        "count": len(items),
        "total": entries.total,
    }
    return result if pending_root_ids else _cache_put(cache_key, result)
