from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import chat_store
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
        "body": body,
    }


def _session_entries(sessions: list[dict], names: dict[str, str]) -> list[dict]:
    entries: list[dict] = []
    for session in sessions:
        for message in session.get("messages") or []:
            if isinstance(message, dict):
                entry = _message_entry(
                    session=session,
                    message=message,
                    names=names,
                    status="delivered",
                )
                if entry:
                    entries.append(entry)
        for queued in session.get("queued_prompts") or []:
            if isinstance(queued, dict):
                entry = _message_entry(
                    session=session,
                    message=queued,
                    names=names,
                    status="queued",
                )
                if entry:
                    entries.append(entry)
    return entries


def _chat_entries(names: dict[str, str]) -> list[dict]:
    entries: list[dict] = []
    for chat in chat_store.list_chats():
        chat_id = str(chat.get("id") or "")
        chat_name = str(chat.get("name") or chat_id)
        for message in chat.get("messages") or []:
            if not isinstance(message, dict):
                continue
            sender_id = str(message.get("sender_id") or "")
            if not sender_id:
                continue
            seq = int(message.get("seq") or 0)
            entries.append({
                "id": f"chat:{chat_id}:{seq}",
                "kind": "chat",
                "status": "posted",
                "created_at": _iso(message.get("ts")),
                "from_session_id": sender_id,
                "from_name": _session_name(names, sender_id),
                "to_session_id": None,
                "to_name": chat_name,
                "chat_id": chat_id,
                "chat_name": chat_name,
                "body": str(message.get("text") or ""),
            })
    return entries


def _involves_session(entry: dict, session_id: str, chat_participants: dict[str, set[str]]) -> bool:
    if entry.get("from_session_id") == session_id or entry.get("to_session_id") == session_id:
        return True
    chat_id = str(entry.get("chat_id") or "")
    return bool(chat_id and session_id in chat_participants.get(chat_id, set()))


def list_communications(*, session_id: str = "", limit: int = 200) -> dict:
    clean_session_id = str(session_id or "").strip()
    clean_limit = max(1, min(int(limit or 200), 500))
    sessions = [
        session_manager.get(str(session.get("id") or "")) or session
        for session in session_manager.iter_all()
    ]
    names = _session_names(sessions)
    entries = _session_entries(sessions, names) + _chat_entries(names)
    chat_participants: dict[str, set[str]] = {}
    for entry in entries:
        chat_id = str(entry.get("chat_id") or "")
        sender_id = str(entry.get("from_session_id") or "")
        if chat_id and sender_id:
            chat_participants.setdefault(chat_id, set()).add(sender_id)
    if clean_session_id:
        entries = [
            entry for entry in entries
            if _involves_session(entry, clean_session_id, chat_participants)
        ]
    entries.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return {
        "items": entries[:clean_limit],
        "count": min(len(entries), clean_limit),
        "total": len(entries),
    }
