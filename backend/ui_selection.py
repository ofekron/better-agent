"""Per-machine UI navigation-restore state (persisted to disk).

Better Agent runs locally, so the backend instance IS the machine: this
store is the authoritative "what was the user looking at" memory for that
machine, independent of any single browser/tab. The frontend reflects it
(REST snapshot on mount + `ui_selection_changed` WS push) and keeps only an
offline first-paint cache in localStorage.

Storage:
  <ba_home>/app-state/ui-selection.json

Shape:
  {
    "selected_project": {"path": str, "node_id": str} | None,
    "remembered_session_by_project": {
        <project_path>: { <node_id>: <session_id> }
    },
    "open_session_tab_ids": [<session_id>, ...],
    "open_session_tab_joined_at": {<session_id>: <iso_timestamp>, ...}
  }

`node_id` is the multi-machine filesystem node the project lives on
("primary" is the local-node sentinel). It is a KEY inside the remembered
map, not the machine axis — the machine axis is the backend instance itself.
"""

import logging
from datetime import datetime, timezone
import threading

from json_store import read_json, write_json
from paths import ba_home

logger = logging.getLogger(__name__)

DEFAULT_NODE_ID = "primary"
_PATH = ba_home() / "app-state" / "ui-selection.json"
_LOCK = threading.RLock()


def _path():
    return _PATH


def _load() -> dict:
    with _LOCK:
        return read_json(_path(), {})


def _save(data: dict) -> None:
    with _LOCK:
        write_json(_path(), data)


def _clean_node_id(node_id) -> str:
    if not isinstance(node_id, str) or not node_id.strip():
        return DEFAULT_NODE_ID
    return node_id


def _require_nonempty_str(value, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _selected_project_from(data: dict) -> dict | None:
    sel = data.get("selected_project")
    if not isinstance(sel, dict):
        return None
    path = sel.get("path")
    if not isinstance(path, str) or not path.strip():
        return None
    return {"path": path, "node_id": _clean_node_id(sel.get("node_id"))}


def get_selected_project() -> dict | None:
    return _selected_project_from(_load())


def set_selected_project(path: str, node_id: str = DEFAULT_NODE_ID) -> dict:
    """Record the project the user is currently viewing. Pass an empty
    path to clear it (e.g. user navigated away from any project)."""
    with _LOCK:
        data = _load()
        if isinstance(path, str) and path.strip():
            data["selected_project"] = {"path": path, "node_id": _clean_node_id(node_id)}
        else:
            data["selected_project"] = None
        _save(data)
        return _snapshot(data)


def _remembered_sessions_from(data: dict) -> dict:
    raw = data.get("remembered_session_by_project")
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, str]] = {}
    for path, by_node in raw.items():
        if not isinstance(path, str) or not isinstance(by_node, dict):
            continue
        clean = {
            n: s
            for n, s in by_node.items()
            if isinstance(n, str) and n and isinstance(s, str) and s
        }
        if clean:
            out[path] = clean
    return out


def _open_session_tab_ids_from(data: dict) -> list[str]:
    raw = data.get("open_session_tab_ids")
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for sid in raw:
        if not isinstance(sid, str) or not sid:
            continue
        if sid in seen:
            continue
        seen.add(sid)
        out.append(sid)
    return out


def _open_session_tab_joined_at_from(data: dict, session_ids: list[str]) -> dict[str, str]:
    raw = data.get("open_session_tab_joined_at")
    if not isinstance(raw, dict):
        return {}
    open_ids = set(session_ids)
    out: dict[str, str] = {}
    for sid, joined_at in raw.items():
        if not isinstance(sid, str) or sid not in open_ids:
            continue
        if not isinstance(joined_at, str) or not joined_at:
            continue
        out[sid] = joined_at
    return out


def get_remembered_sessions() -> dict:
    return _remembered_sessions_from(_load())


def set_remembered_session(path: str, node_id: str, session_id: str) -> dict:
    """Record the last session the user viewed in (project path × node)."""
    _require_nonempty_str(path, "path")
    _require_nonempty_str(session_id, "session_id")
    node = _clean_node_id(node_id)
    with _LOCK:
        data = _load()
        by_project = data.get("remembered_session_by_project")
        if not isinstance(by_project, dict):
            by_project = {}
        by_node = by_project.get(path)
        if not isinstance(by_node, dict):
            by_node = {}
        by_node[node] = session_id
        by_project[path] = by_node
        data["remembered_session_by_project"] = by_project
        _save(data)
        return _snapshot(data)


def set_open_session_tab_ids(session_ids: list[str]) -> dict:
    if not isinstance(session_ids, list):
        raise ValueError("open_session_tab_ids must be a list")
    with _LOCK:
        data = _load()
        next_ids = _open_session_tab_ids_from({
            "open_session_tab_ids": session_ids,
        })
        existing_joined_at = _open_session_tab_joined_at_from(data, next_ids)
        now = datetime.now(timezone.utc).isoformat()
        data["open_session_tab_ids"] = next_ids
        data["open_session_tab_joined_at"] = {
            sid: existing_joined_at.get(sid, now)
            for sid in next_ids
        }
        _save(data)
        return _snapshot(data)


def set_open_session_tab_joined_at(joined_at: dict[str, str]) -> dict:
    if not isinstance(joined_at, dict):
        raise ValueError("open_session_tab_joined_at must be an object")
    with _LOCK:
        data = _load()
        open_ids = _open_session_tab_ids_from(data)
        existing_joined_at = _open_session_tab_joined_at_from(data, open_ids)
        provided_joined_at = _open_session_tab_joined_at_from(
            {"open_session_tab_joined_at": joined_at},
            open_ids,
        )
        now = datetime.now(timezone.utc).isoformat()
        data["open_session_tab_joined_at"] = {
            sid: provided_joined_at.get(sid) or existing_joined_at.get(sid) or now
            for sid in open_ids
        }
        _save(data)
        return _snapshot(data)


def _snapshot(data: dict) -> dict:
    open_ids = _open_session_tab_ids_from(data)
    return {
        "selected_project": _selected_project_from(data),
        "remembered_session_by_project": _remembered_sessions_from(data),
        "open_session_tab_ids": open_ids,
        "open_session_tab_joined_at": _open_session_tab_joined_at_from(data, open_ids),
    }


def get_all() -> dict:
    return _snapshot(_load())
