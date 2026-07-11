from __future__ import annotations

import copy
import threading
from typing import Any

from json_store import read_json, write_json
from paths import ba_home
from bff_runtime_contract import PROJECT_CATALOG_SCHEMA_VERSION


SCHEMA_VERSION = PROJECT_CATALOG_SCHEMA_VERSION
_LOCK = threading.RLock()


def _path():
    return ba_home() / "runtime" / "project-catalog.json"


def _validate_project(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("project catalog entries must be objects")
    path = raw.get("path")
    node_id = raw.get("node_id") or "primary"
    if not isinstance(path, str) or not path:
        raise ValueError("project path must be a non-empty string")
    if not isinstance(node_id, str) or not node_id:
        raise ValueError("project node_id must be a non-empty string")
    return {
        key: copy.deepcopy(value)
        for key, value in raw.items()
        if isinstance(key, str)
    } | {"path": path, "node_id": node_id}


def replace(projects: object) -> list[dict[str, Any]]:
    if not isinstance(projects, list):
        raise ValueError("projects must be a list")
    validated = [_validate_project(project) for project in projects]
    keys = [(project["node_id"], project["path"]) for project in validated]
    if len(keys) != len(set(keys)):
        raise ValueError("project catalog contains duplicate node/path entries")
    payload = {"schema_version": SCHEMA_VERSION, "projects": validated}
    with _LOCK:
        write_json(_path(), payload)
    return copy.deepcopy(validated)


def list_projects() -> list[dict[str, Any]]:
    with _LOCK:
        payload = read_json(
            _path(), {"schema_version": SCHEMA_VERSION, "projects": []}
        )
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported runtime project catalog schema")
    projects = payload.get("projects")
    if not isinstance(projects, list):
        raise ValueError("runtime project catalog projects must be a list")
    return [_validate_project(project) for project in projects]
