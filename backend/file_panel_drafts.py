import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

from paths import ba_home


def _draft_root() -> Path:
    root = ba_home() / "file-panel-drafts"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _draft_key(path: str, node_id: str) -> str:
    if not isinstance(path, str) or not path:
        raise ValueError("path is required")
    if not isinstance(node_id, str) or not node_id:
        raise ValueError("node_id is required")
    raw = json.dumps({"node_id": node_id, "path": path}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _draft_path(path: str, node_id: str) -> Path:
    return _draft_root() / f"{_draft_key(path, node_id)}.json"


def _normalize_identity(value: Any) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    mtime_ns = value.get("mtime_ns")
    size = value.get("size")
    if not isinstance(mtime_ns, int) or not isinstance(size, int):
        return None
    return {"mtime_ns": mtime_ns, "size": size}


def read_draft(path: str, node_id: str) -> dict[str, Any]:
    draft_path = _draft_path(path, node_id)
    if not draft_path.exists():
        return {"exists": False}
    with draft_path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if data.get("path") != path or data.get("node_id") != node_id:
        return {"exists": False}
    content = data.get("content")
    if not isinstance(content, str):
        return {"exists": False}
    return {
        "exists": True,
        "path": path,
        "node_id": node_id,
        "content": content,
        "base_identity": _normalize_identity(data.get("base_identity")),
        "updated_at": data.get("updated_at"),
    }


def write_draft(
    *,
    path: str,
    node_id: str,
    content: str,
    base_identity: Any,
) -> dict[str, Any]:
    if not isinstance(content, str):
        raise ValueError("content must be a string")
    draft_path = _draft_path(path, node_id)
    data = {
        "path": path,
        "node_id": node_id,
        "content": content,
        "base_identity": _normalize_identity(base_identity),
        "updated_at": time.time(),
    }
    tmp_path = draft_path.with_suffix(f".{os.getpid()}.{time.time_ns()}.tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp_path, draft_path)
    return read_draft(path, node_id)


def delete_draft(path: str, node_id: str) -> dict[str, Any]:
    draft_path = _draft_path(path, node_id)
    try:
        draft_path.unlink()
    except FileNotFoundError:
        pass
    return {"exists": False}
