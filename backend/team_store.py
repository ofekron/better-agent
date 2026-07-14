from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import threading
from typing import Any
from uuid import uuid4

from json_store import read_json, write_json
from paths import ba_home


SCHEMA_VERSION = 1
_revision_lock = threading.Lock()
_revision = 0


def _bump_revision() -> None:
    global _revision
    with _revision_lock:
        _revision += 1


def revision() -> int:
    with _revision_lock:
        return _revision


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
        "pending_members": {},
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
    # An explicit team_id can be re-created idempotently by the SAME root
    # session (retry semantics), but must never be silently overwritten by a
    # different one — that would destroy the existing owner's team record
    # and orphan its workers out from under it.
    existing = get(tid) if team_id else None
    if existing is not None and existing.get("root_session_id") != root:
        raise TeamStoreError(
            f"team_id {tid!r} already exists under a different root_session_id",
        )
    now = _now()
    if existing is not None:
        # Same root re-creating its own team_id: refresh metadata but
        # PRESERVE members/pending_members. A caller retrying after a
        # partial failure relies on previously-registered workers
        # surviving — the manager entry is a placeholder here regardless
        # (every caller immediately follows create() with upsert_member for
        # "manager" with the real details), so it's fine to leave as-is.
        record = existing
        record["definition_ref"] = str(definition_ref or "").strip()
        record["profile"] = str(profile or "").strip()
        record["updated_at"] = now
    else:
        record = _blank(
            team_id=tid,
            root_session_id=root,
            definition_ref=str(definition_ref or "").strip(),
            profile=str(profile or "").strip(),
        )
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
    _bump_revision()
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
    data.setdefault("pending_members", {})
    return data


def list_all() -> list[dict[str, Any]]:
    if not _root().exists():
        return []
    teams: list[dict[str, Any]] = []
    for path in sorted(_root().glob("*.json")):
        data = read_json(path, {})
        if data.get("schema_version") == SCHEMA_VERSION and isinstance(data.get("members"), dict):
            teams.append(data)
    return teams


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
    _bump_revision()
    return team["members"][mid]


def remove_members_by_session_ids(team_id: str, agent_session_ids: list[str]) -> list[str]:
    """Drop member entries whose agent_session_id is in the given set — e.g.
    after a rollback deleted those sessions but the team record itself
    survives (a sibling member's deletion failed). Returns the removed
    member_ids. No-op (returns []) if the team no longer exists."""
    team = get(team_id)
    if team is None:
        return []
    wanted = {str(sid).strip() for sid in agent_session_ids or [] if str(sid).strip()}
    if not wanted:
        return []
    removed: list[str] = []
    for mid, member in list(team["members"].items()):
        if isinstance(member, dict) and str(member.get("agent_session_id") or "") in wanted:
            del team["members"][mid]
            removed.append(mid)
    if removed:
        team["updated_at"] = _now()
        write_json(_path(team_id), team)
        _bump_revision()
    return removed


def restore(team_id: str, snapshot: dict[str, Any]) -> None:
    """Overwrite the team record with an exact prior snapshot — e.g. to undo
    every mutation a failed retry made (manager data, pending_members,
    reused-worker re-registration) in one shot. `snapshot` must be a dict
    previously returned by get() for this same team_id."""
    write_json(_path(team_id), snapshot)
    _bump_revision()


def delete(team_id: str) -> bool:
    path = _path(team_id)
    if not path.exists():
        return False
    path.unlink()
    _bump_revision()
    return True


def set_pending_members(team_id: str, specs: list[dict[str, Any]]) -> dict[str, Any]:
    team = get(team_id)
    if team is None:
        raise TeamStoreError("team_id does not exist")
    pending: dict[str, Any] = {}
    for spec in specs or []:
        if not isinstance(spec, dict):
            continue
        member_id = str(spec.get("member_id") or spec.get("role_key") or "").strip()
        if not member_id:
            continue
        pending[member_id] = dict(spec)
    team["pending_members"] = pending
    team["updated_at"] = _now()
    write_json(_path(team_id), team)
    _bump_revision()
    return team


def pop_pending_member(team_id: str, member_id: str) -> dict[str, Any] | None:
    team = get(team_id)
    if team is None:
        raise TeamStoreError("team_id does not exist")
    pending = team.get("pending_members")
    if not isinstance(pending, dict):
        return None
    mid = str(member_id or "").strip()
    spec = pending.pop(mid, None)
    if spec is None:
        return None
    team["pending_members"] = pending
    team["updated_at"] = _now()
    write_json(_path(team_id), team)
    _bump_revision()
    return spec


def restore_pending_member(team_id: str, member_id: str, spec: dict[str, Any]) -> dict[str, Any] | None:
    """Put a member spec back into pending_members, e.g. after a finalize
    attempt popped it but the actual provisioning failed. Returns None if the
    team no longer exists (caller decides whether that's fatal)."""
    team = get(team_id)
    if team is None:
        return None
    pending = team.get("pending_members")
    if not isinstance(pending, dict):
        pending = {}
    mid = str(member_id or "").strip()
    if mid:
        pending[mid] = dict(spec)
    team["pending_members"] = pending
    team["updated_at"] = _now()
    write_json(_path(team_id), team)
    _bump_revision()
    return team


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
