from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from json_store import read_json, write_json
from paths import ba_home


SCHEMA_VERSION = 1


class TeamStoreError(ValueError):
    pass


def _root() -> Path:
    return ba_home() / "teams"


def _path(team_id: str) -> Path:
    clean = _clean_id(team_id, "team_id")
    return _root() / f"{clean}.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_id(value: Any, field: str) -> str:
    clean = str(value or "").strip()
    if not clean:
        raise TeamStoreError(f"{field} is required")
    if any(part in clean for part in ("/", "\\", "..")):
        raise TeamStoreError(f"{field} is invalid")
    return clean


def _blank(
    *,
    team_id: str,
    root_session_id: str,
    definition_ref: str = "",
    profile: str = "",
) -> dict[str, Any]:
    now = _now()
    return {
        "schema_version": SCHEMA_VERSION,
        "id": team_id,
        "definition_ref": definition_ref,
        "profile": profile,
        "root_session_id": root_session_id,
        "created_at": now,
        "updated_at": now,
        "members": {},
    }


def create(
    *,
    root_session_id: str,
    definition_ref: str = "",
    profile: str = "",
    team_id: str | None = None,
) -> dict[str, Any]:
    tid = _clean_id(team_id or f"team-{uuid4().hex}", "team_id")
    root = _clean_id(root_session_id, "root_session_id")
    record = _blank(
        team_id=tid,
        root_session_id=root,
        definition_ref=str(definition_ref or "").strip(),
        profile=str(profile or "").strip(),
    )
    now = _now()
    record["members"]["manager"] = {
        "id": "manager",
        "type": "manager",
        "agent_session_id": root,
        "role": "manager",
        "description": "manager",
        "cwd": "",
        "provider_id": "",
        "model": "",
        "reasoning_effort": "",
        "run_mode": "",
        "parent_member_id": "",
        "status": "active",
        "nested_team_id": "",
        "created_at": now,
        "updated_at": now,
    }
    write_json(_path(tid), record)
    return record


def get(team_id: str) -> dict[str, Any] | None:
    path = _path(team_id)
    if not path.exists():
        return None
    data = read_json(path, {})
    if data.get("schema_version") != SCHEMA_VERSION:
        raise TeamStoreError("Unsupported team store schema; wipe teams/*.json to start fresh")
    if not isinstance(data.get("members"), dict):
        raise TeamStoreError("Malformed team store: members must be an object")
    return data


def upsert_member(
    team_id: str,
    *,
    member_id: str,
    member_type: str,
    agent_session_id: str,
    role: str,
    description: str = "",
    cwd: str = "",
    provider_id: str = "",
    model: str = "",
    reasoning_effort: str = "",
    run_mode: str = "",
    parent_member_id: str = "",
    status: str = "active",
    nested_team_id: str = "",
) -> dict[str, Any]:
    if member_type not in {"manager", "worker", "team_session"}:
        raise TeamStoreError("member_type must be manager, worker, or team_session")
    if member_type == "manager" and member_id != "manager":
        raise TeamStoreError("manager member_id must be manager")
    team = get(team_id)
    if team is None:
        raise TeamStoreError("team_id does not exist")
    mid = _clean_id(member_id, "member_id")
    sid = _clean_id(agent_session_id, "agent_session_id")
    existing = team["members"].get(mid) or {}
    now = _now()
    team["members"][mid] = {
        "id": mid,
        "type": member_type,
        "agent_session_id": sid,
        "role": str(role or member_type).strip(),
        "description": str(description or "").strip(),
        "cwd": str(cwd or "").strip(),
        "provider_id": str(provider_id or "").strip(),
        "model": str(model or "").strip(),
        "reasoning_effort": str(reasoning_effort or "").strip(),
        "run_mode": str(run_mode or "").strip(),
        "parent_member_id": str(parent_member_id or "").strip(),
        "status": str(status or "active").strip(),
        "nested_team_id": str(nested_team_id or "").strip(),
        "created_at": existing.get("created_at") or now,
        "updated_at": now,
    }
    team["updated_at"] = now
    write_json(_path(team_id), team)
    return team["members"][mid]


def find_for_session(session_id: str) -> dict[str, Any] | None:
    sid = str(session_id or "").strip()
    if not sid:
        return None
    if not _root().exists():
        return None
    for path in sorted(_root().glob("*.json")):
        data = read_json(path, {})
        if data.get("schema_version") != SCHEMA_VERSION:
            continue
        members = data.get("members")
        if not isinstance(members, dict):
            continue
        for member in members.values():
            if isinstance(member, dict) and member.get("agent_session_id") == sid:
                return data
    return None


def member_for_session(team: dict[str, Any], session_id: str) -> dict[str, Any] | None:
    sid = str(session_id or "").strip()
    members = team.get("members")
    if not isinstance(members, dict):
        return None
    for member in members.values():
        if isinstance(member, dict) and member.get("agent_session_id") == sid:
            return member
    return None


def ordered_members(team: dict[str, Any]) -> list[dict[str, Any]]:
    members = team.get("members")
    if not isinstance(members, dict):
        return []
    return sorted(
        [item for item in members.values() if isinstance(item, dict)],
        key=lambda item: (0 if item.get("type") == "manager" else 1, str(item.get("id") or "")),
    )
