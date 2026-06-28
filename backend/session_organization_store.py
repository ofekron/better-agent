from __future__ import annotations

import copy
import threading
import uuid
from datetime import datetime
from typing import Any, Literal

import json_store
from paths import ba_home

SCHEMA_VERSION = 1

_lock = threading.RLock()
_cache_signature: tuple[int, int] | None = None
_cache_data: dict[str, Any] | None = None


def _path():
    return ba_home() / "session_organization.json"


def _now() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _new_state() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "folders": [],
        "tags": [],
        "assignments": {},
    }


def _validate(data: dict[str, Any]) -> dict[str, Any]:
    if data.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported session_organization schema")
    if not isinstance(data.get("folders"), list):
        raise ValueError("session_organization folders must be a list")
    if not isinstance(data.get("tags"), list):
        raise ValueError("session_organization tags must be a list")
    if not isinstance(data.get("assignments"), dict):
        raise ValueError("session_organization assignments must be an object")
    return data


def _load_shared() -> dict[str, Any]:
    global _cache_data, _cache_signature
    path = _path()
    try:
        st = path.stat()
        signature = (st.st_mtime_ns, st.st_size)
    except OSError:
        signature = None
    if signature is not None and _cache_signature == signature and _cache_data is not None:
        return _cache_data
    data = _validate(json_store.read_json(path, _new_state()))
    if signature is not None:
        _cache_signature = signature
        _cache_data = copy.deepcopy(data)
        return _cache_data
    return data


def _load() -> dict[str, Any]:
    data = _load_shared()
    return copy.deepcopy(data)


def version_token() -> tuple[int, int] | None:
    with _lock:
        _load_shared()
        return _cache_signature


def _save(data: dict[str, Any]) -> None:
    global _cache_data, _cache_signature
    path = _path()
    json_store.write_json(path, data)
    try:
        st = path.stat()
        _cache_signature = (st.st_mtime_ns, st.st_size)
        _cache_data = copy.deepcopy(data)
    except OSError:
        _cache_signature = None
        _cache_data = None


def _clean_text(value: Any, field: str, *, required: bool = True) -> str:
    if not isinstance(value, str):
        if required:
            raise ValueError(f"{field} must be a string")
        return ""
    cleaned = value.strip()
    if required and not cleaned:
        raise ValueError(f"{field} is required")
    return cleaned


def _clean_id(value: Any, field: str, *, required: bool = True) -> str | None:
    if value is None and not required:
        return None
    cleaned = _clean_text(value, field, required=required)
    return cleaned or None


def _folder(data: dict[str, Any], folder_id: str) -> dict[str, Any] | None:
    return next((f for f in data["folders"] if f.get("id") == folder_id), None)


def _folder_subtree_ids(data: dict[str, Any], folder_id: str) -> set[str]:
    ids = {folder_id}
    changed = True
    while changed:
        changed = False
        for folder in data["folders"]:
            fid = folder.get("id")
            if (
                isinstance(fid, str)
                and folder.get("parent_folder_id") in ids
                and fid not in ids
            ):
                ids.add(fid)
                changed = True
    return ids


def _tag(data: dict[str, Any], tag_id: str) -> dict[str, Any] | None:
    return next((t for t in data["tags"] if t.get("id") == tag_id), None)


def _assignment(data: dict[str, Any], session_id: str) -> dict[str, Any]:
    assignments = data["assignments"]
    raw = assignments.get(session_id)
    if not isinstance(raw, dict):
        raw = {}
    folder_id = raw.get("folder_id")
    tag_ids = raw.get("tag_ids")
    if not isinstance(folder_id, str):
        folder_id = None
    if not isinstance(tag_ids, list):
        tag_ids = []
    cleaned = {
        "folder_id": folder_id,
        "tag_ids": [tid for tid in tag_ids if isinstance(tid, str)],
    }
    assignments[session_id] = cleaned
    return cleaned


def snapshot(project_id: str | None = None) -> dict[str, Any]:
    with _lock:
        data = _load()
        if project_id is None:
            return copy.deepcopy(data)
        project_id = _clean_text(project_id, "project_id")
        folders = [f for f in data["folders"] if f.get("project_id") == project_id]
        tags = [
            t for t in data["tags"]
            if t.get("project_id") in (project_id, None, "")
        ]
        return {
            "schema_version": SCHEMA_VERSION,
            "folders": copy.deepcopy(folders),
            "tags": copy.deepcopy(tags),
            "assignments": copy.deepcopy(data["assignments"]),
        }


def organization_for_session(session_id: str) -> dict[str, Any]:
    session_id = _clean_text(session_id, "session_id")
    with _lock:
        data = _load()
        assignment = _assignment(data, session_id)
        tag_by_id = {t.get("id"): t for t in data["tags"]}
        tags = [
            copy.deepcopy(tag_by_id[tid])
            for tid in assignment["tag_ids"]
            if tid in tag_by_id
        ]
        return {
            "folder_id": assignment["folder_id"],
            "tag_ids": list(assignment["tag_ids"]),
            "tags": tags,
        }


def enrich_session_summary(summary: dict[str, Any]) -> dict[str, Any]:
    sid = str(summary.get("id") or "")
    with _lock:
        data = _load_shared()
        assignment = _assignment(data, sid)
        tag_by_id = {t.get("id"): t for t in data["tags"]}
        tags = [
            copy.deepcopy(tag_by_id[tid])
            for tid in assignment["tag_ids"]
            if tid in tag_by_id
        ]
        return {
            **summary,
            "folder_id": assignment["folder_id"],
            "session_tags": tags,
        }


def enrich_session_summaries(summaries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    with _lock:
        data = _load_shared()
        tag_by_id = {t.get("id"): t for t in data["tags"]}
        assignments = data["assignments"]
        enriched: list[dict[str, Any]] = []
        for summary in summaries:
            sid = str(summary.get("id") or "")
            raw_assignment = assignments.get(sid)
            assignment = raw_assignment if isinstance(raw_assignment, dict) else {}
            folder_id = assignment.get("folder_id")
            if not isinstance(folder_id, str):
                folder_id = None
            raw_tag_ids = assignment.get("tag_ids")
            tag_ids = raw_tag_ids if isinstance(raw_tag_ids, list) else []
            tags = [
                copy.deepcopy(tag_by_id[tid])
                for tid in tag_ids
                if isinstance(tid, str) and tid in tag_by_id
            ]
            enriched.append({
                **summary,
                "folder_id": folder_id,
                "session_tags": tags,
            })
        return enriched


def create_folder(
    *,
    project_id: str,
    name: str,
    parent_folder_id: str | None = None,
) -> dict[str, Any]:
    project_id = _clean_text(project_id, "project_id")
    name = _clean_text(name, "name")
    parent_folder_id = _clean_id(parent_folder_id, "parent_folder_id", required=False)
    with _lock:
        data = _load()
        if parent_folder_id:
            parent = _folder(data, parent_folder_id)
            if not parent or parent.get("project_id") != project_id:
                raise ValueError("parent_folder_id is not in this project")
        order = max(
            [
                int(f.get("order") or 0)
                for f in data["folders"]
                if f.get("project_id") == project_id
                and f.get("parent_folder_id") == parent_folder_id
            ],
            default=-1,
        ) + 1
        folder = {
            "id": str(uuid.uuid4()),
            "project_id": project_id,
            "parent_folder_id": parent_folder_id,
            "name": name,
            "order": order,
            "created_at": _now(),
            "updated_at": _now(),
        }
        data["folders"].append(folder)
        _save(data)
        return copy.deepcopy(folder)


def update_folder(folder_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    folder_id = _clean_text(folder_id, "folder_id")
    with _lock:
        data = _load()
        folder = _folder(data, folder_id)
        if not folder:
            raise KeyError(folder_id)
        if "name" in patch:
            folder["name"] = _clean_text(patch["name"], "name")
        if "parent_folder_id" in patch:
            parent_id = _clean_id(patch["parent_folder_id"], "parent_folder_id", required=False)
            if parent_id == folder_id:
                raise ValueError("folder cannot be its own parent")
            if parent_id:
                parent = _folder(data, parent_id)
                if not parent or parent.get("project_id") != folder.get("project_id"):
                    raise ValueError("parent_folder_id is not in this project")
            folder["parent_folder_id"] = parent_id
        folder["updated_at"] = _now()
        _save(data)
        return copy.deepcopy(folder)


def folder_delete_preview(folder_id: str) -> dict[str, Any]:
    folder_id = _clean_text(folder_id, "folder_id")
    with _lock:
        data = _load()
        if not _folder(data, folder_id):
            raise KeyError(folder_id)
        folder_ids = _folder_subtree_ids(data, folder_id)
        session_ids = [
            session_id
            for session_id, assignment in data["assignments"].items()
            if isinstance(session_id, str)
            and isinstance(assignment, dict)
            and assignment.get("folder_id") in folder_ids
        ]
        return {
            "folder_ids": sorted(folder_ids),
            "session_ids": sorted(session_ids),
            "folder_count": len(folder_ids),
            "session_count": len(session_ids),
        }


def delete_folder(
    folder_id: str,
    *,
    mode: Literal["unassign", "delete_sessions"] = "unassign",
) -> bool:
    if mode not in ("unassign", "delete_sessions"):
        raise ValueError("mode must be unassign or delete_sessions")
    folder_id = _clean_text(folder_id, "folder_id")
    with _lock:
        data = _load()
        if not _folder(data, folder_id):
            return False
        child_ids = _folder_subtree_ids(data, folder_id)
        data["folders"] = [
            f for f in data["folders"] if f.get("id") not in child_ids
        ]
        for assignment in data["assignments"].values():
            if isinstance(assignment, dict) and assignment.get("folder_id") in child_ids:
                assignment["folder_id"] = None
        _save(data)
        return True


def create_tag(
    *,
    name: str,
    project_id: str | None = None,
    color: str | None = None,
) -> dict[str, Any]:
    name = _clean_text(name, "name")
    project_id = _clean_id(project_id, "project_id", required=False)
    color = _clean_id(color, "color", required=False)
    with _lock:
        data = _load()
        tag = {
            "id": str(uuid.uuid4()),
            "project_id": project_id,
            "name": name,
            "color": color,
            "created_at": _now(),
            "updated_at": _now(),
        }
        data["tags"].append(tag)
        _save(data)
        return copy.deepcopy(tag)


def update_tag(tag_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    tag_id = _clean_text(tag_id, "tag_id")
    with _lock:
        data = _load()
        tag = _tag(data, tag_id)
        if not tag:
            raise KeyError(tag_id)
        if "name" in patch:
            tag["name"] = _clean_text(patch["name"], "name")
        if "color" in patch:
            tag["color"] = _clean_id(patch["color"], "color", required=False)
        tag["updated_at"] = _now()
        _save(data)
        return copy.deepcopy(tag)


def delete_tag(tag_id: str) -> bool:
    tag_id = _clean_text(tag_id, "tag_id")
    with _lock:
        data = _load()
        before = len(data["tags"])
        data["tags"] = [t for t in data["tags"] if t.get("id") != tag_id]
        if len(data["tags"]) == before:
            return False
        for assignment in data["assignments"].values():
            if isinstance(assignment, dict):
                assignment["tag_ids"] = [
                    tid for tid in assignment.get("tag_ids") or [] if tid != tag_id
                ]
        _save(data)
        return True


def set_session_folder(session_id: str, folder_id: str | None) -> dict[str, Any]:
    session_id = _clean_text(session_id, "session_id")
    folder_id = _clean_id(folder_id, "folder_id", required=False)
    with _lock:
        data = _load()
        if folder_id and not _folder(data, folder_id):
            raise ValueError("folder_id does not exist")
        assignment = _assignment(data, session_id)
        assignment["folder_id"] = folder_id
        _save(data)
        return organization_for_session(session_id)


def set_session_tags(session_id: str, tag_ids: list[Any]) -> dict[str, Any]:
    session_id = _clean_text(session_id, "session_id")
    if not isinstance(tag_ids, list):
        raise ValueError("tag_ids must be a list")
    cleaned: list[str] = []
    for raw in tag_ids:
        tag_id = _clean_text(raw, "tag_id")
        if tag_id not in cleaned:
            cleaned.append(tag_id)
    with _lock:
        data = _load()
        missing = [tag_id for tag_id in cleaned if not _tag(data, tag_id)]
        if missing:
            raise ValueError("unknown tag_id")
        assignment = _assignment(data, session_id)
        assignment["tag_ids"] = cleaned
        _save(data)
        return organization_for_session(session_id)


def patch_session_tags(
    session_id: str,
    *,
    add: list[Any] | None = None,
    remove: list[Any] | None = None,
) -> dict[str, Any]:
    session_id = _clean_text(session_id, "session_id")
    add = add or []
    remove = remove or []
    if not isinstance(add, list) or not isinstance(remove, list):
        raise ValueError("add and remove must be lists")
    add_ids = [_clean_text(tag_id, "tag_id") for tag_id in add]
    remove_ids = {_clean_text(tag_id, "tag_id") for tag_id in remove}
    with _lock:
        data = _load()
        missing = [tag_id for tag_id in add_ids if not _tag(data, tag_id)]
        if missing:
            raise ValueError("unknown tag_id")
        assignment = _assignment(data, session_id)
        next_ids = [tid for tid in assignment["tag_ids"] if tid not in remove_ids]
        for tag_id in add_ids:
            if tag_id not in next_ids:
                next_ids.append(tag_id)
        assignment["tag_ids"] = next_ids
        _save(data)
        return organization_for_session(session_id)


def query_sessions(sessions: list[dict[str, Any]], query: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(query, dict):
        raise ValueError("query must be an object")
    text = query.get("text")
    if text is not None and not isinstance(text, str):
        raise ValueError("text must be a string")
    text = (text or "").strip().lower()
    folder_ids = query.get("folder_ids") or []
    tag_ids = query.get("tag_ids") or []
    project_ids = query.get("project_ids") or []
    providers = query.get("providers") or []
    models = query.get("models") or []
    modes = query.get("modes") or []
    statuses = query.get("status") or []
    for field, values in {
        "folder_ids": folder_ids,
        "tag_ids": tag_ids,
        "project_ids": project_ids,
        "providers": providers,
        "models": models,
        "modes": modes,
        "status": statuses,
    }.items():
        if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
            raise ValueError(f"{field} must be a string list")
    tag_match = query.get("tag_match") or "all"
    if tag_match not in ("all", "any"):
        raise ValueError("tag_match must be all or any")
    project_set = set(project_ids)
    folder_set = set(folder_ids)
    tag_set = set(tag_ids)
    provider_set = set(providers)
    model_set = set(models)
    mode_set = set(modes)
    status_set = set(statuses)
    out: list[dict[str, Any]] = []
    for session in sessions:
        if project_set and session.get("cwd") not in project_set:
            continue
        if folder_set and (session.get("folder_id") or "") not in folder_set:
            continue
        if provider_set and (session.get("provider_id") or "") not in provider_set:
            continue
        if model_set and (session.get("model") or "") not in model_set:
            continue
        if mode_set and (session.get("orchestration_mode") or "") not in mode_set:
            continue
        if tag_set:
            session_tags = {
                tag.get("id")
                for tag in session.get("session_tags") or []
                if isinstance(tag, dict)
            }
            if tag_match == "all" and not tag_set.issubset(session_tags):
                continue
            if tag_match == "any" and not tag_set.intersection(session_tags):
                continue
        if status_set:
            ok = False
            if "running" in status_set and session.get("is_running"):
                ok = True
            if "idle" in status_set and not session.get("is_running") and not session.get("archived"):
                ok = True
            if "archived" in status_set and session.get("archived"):
                ok = True
            if "pinned" in status_set and session.get("pinned"):
                ok = True
            if not ok:
                continue
        if text:
            fields = [
                session.get("name"),
                session.get("cwd"),
                session.get("model"),
                session.get("provider_id"),
                session.get("orchestration_mode"),
                *(tag.get("name") for tag in session.get("session_tags") or [] if isinstance(tag, dict)),
            ]
            if not any(isinstance(value, str) and text in value.lower() for value in fields):
                continue
        out.append(session)
    return out
