"""Per-machine UI navigation-restore state (persisted to disk).

Better Agent runs locally, so the backend instance IS the machine: this
store is the authoritative "what was the user looking at" memory for that
machine, independent of any single browser/tab. The frontend reflects it
(REST snapshot on mount + `ui_selection_changed` WS push) and keeps only an
offline first-paint cache in localStorage.

Storage:
  ~/.better-claude/ui_selection.json

Shape:
  {
    "selected_project": {"path": str, "node_id": str} | None,
    "remembered_session_by_project": {
        <project_path>: { <node_id>: <session_id> }
    }
  }

`node_id` is the multi-machine filesystem node the project lives on
("primary" is the local-node sentinel). It is a KEY inside the remembered
map, not the machine axis — the machine axis is the backend instance itself.
"""

import logging

from json_store import read_json, write_json
from paths import bc_home

logger = logging.getLogger(__name__)

DEFAULT_NODE_ID = "primary"
_PATH = bc_home() / "ui_selection.json"


def _path():
    return _PATH


def _load() -> dict:
    return read_json(_path(), {})


def _save(data: dict) -> None:
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


def get_remembered_sessions() -> dict:
    return _remembered_sessions_from(_load())


def set_remembered_session(path: str, node_id: str, session_id: str) -> dict:
    """Record the last session the user viewed in (project path × node)."""
    _require_nonempty_str(path, "path")
    _require_nonempty_str(session_id, "session_id")
    node = _clean_node_id(node_id)
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


def _snapshot(data: dict) -> dict:
    return {
        "selected_project": _selected_project_from(data),
        "remembered_session_by_project": _remembered_sessions_from(data),
    }


def get_all() -> dict:
    return _snapshot(_load())
