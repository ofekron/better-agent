from __future__ import annotations

import copy
import json
import mimetypes
import shutil
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from json_store import write_json
from paths import ba_home

SCHEMA_VERSION = 1
MAX_TITLE_LEN = 200
MAX_KIND_LEN = 80
MAX_PER_TASK = 50
MAX_OUTPUT_BYTES = 5 * 1024 * 1024
ALLOWED_CONTENT_TYPES = {
    "text/html",
    "text/plain",
    "application/json",
    "text/markdown",
}

_lock = threading.RLock()
_data_cache: tuple[tuple[int, int], dict] | None = None


def _path() -> Path:
    return ba_home() / "task_outputs.json"


def _files_root() -> Path:
    return ba_home() / "routine-outputs"


def _empty() -> dict:
    return {"version": SCHEMA_VERSION, "outputs": []}


def _fingerprint() -> tuple[int, int]:
    try:
        st = _path().stat()
    except OSError:
        return (0, 0)
    return (st.st_mtime_ns, st.st_size)


def _read() -> dict:
    global _data_cache
    path = _path()
    if not path.exists():
        return _empty()
    fingerprint = _fingerprint()
    cached = _data_cache
    if cached is not None and cached[0] == fingerprint:
        return copy.deepcopy(cached[1])
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty()
    if not isinstance(raw, dict) or raw.get("version") != SCHEMA_VERSION:
        return _empty()
    raw.setdefault("outputs", [])
    if not isinstance(raw["outputs"], list):
        return _empty()
    raw["outputs"] = [o for o in raw["outputs"] if isinstance(o, dict)]
    _data_cache = (fingerprint, copy.deepcopy(raw))
    return raw


def _write(data: dict) -> None:
    global _data_cache
    write_json(_path(), data)
    _data_cache = (_fingerprint(), copy.deepcopy(data))


def _clean_text(value, *, field: str, max_len: int, required: bool) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    value = value.strip()
    if required and not value:
        raise ValueError(f"{field} is required")
    if len(value) > max_len:
        raise ValueError(f"{field} exceeds {max_len} chars")
    return value


def _clean_task_id(value: str) -> str:
    task_id = _clean_text(value, field="task_id", max_len=64, required=True)
    if not all(ch.isalnum() or ch in ("-", "_") for ch in task_id):
        raise ValueError("task_id contains invalid characters")
    return task_id


def _safe_content_type(value: str) -> str:
    content_type = (value or "text/html").split(";", 1)[0].strip().lower()
    if content_type not in ALLOWED_CONTENT_TYPES:
        raise ValueError("content_type is not supported")
    return content_type


def _extension_for(content_type: str, title: str, source: Optional[Path] = None) -> str:
    if source is not None and source.suffix:
        return source.suffix.lower()
    if content_type == "text/html":
        return ".html"
    if content_type == "application/json":
        return ".json"
    if content_type == "text/markdown":
        return ".md"
    return ".txt"


def _assert_under(path: Path, root: Path, *, label: str) -> None:
    resolved = path.resolve()
    root_resolved = root.resolve()
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise ValueError(f"{label} must stay under {root_resolved}")


def _source_path(raw: str, *, task_cwd: str) -> Path:
    if not raw or not raw.strip():
        raise ValueError("file_path is required when content is empty")
    source = Path(raw.strip()).expanduser()
    if not source.is_absolute():
        source = Path(task_cwd) / source
    source = source.resolve()
    if not source.is_file():
        raise ValueError("file_path must point to a file")
    roots = [Path(task_cwd).resolve(), _files_root().resolve()]
    if not any(source == root or root in source.parents for root in roots):
        raise ValueError("file_path must be inside the routine cwd or routine output root")
    size = source.stat().st_size
    if size > MAX_OUTPUT_BYTES:
        raise ValueError(f"output file exceeds {MAX_OUTPUT_BYTES} bytes")
    return source


def _destination(task_id: str, output_id: str, content_type: str, title: str, source: Optional[Path]) -> Path:
    dest_dir = _files_root() / task_id
    dest_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    suffix = _extension_for(content_type, title, source)
    dest = dest_dir / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{output_id}{suffix}"
    _assert_under(dest, _files_root(), label="output path")
    return dest


def publish(
    *,
    task_id: str,
    task_cwd: str,
    title: str,
    kind: str = "artifact",
    content_type: str = "text/html",
    content: str = "",
    file_path: str = "",
    session_id: str = "",
) -> dict:
    task_id = _clean_task_id(task_id)
    task_cwd = _clean_text(task_cwd, field="task_cwd", max_len=4096, required=True)
    title = _clean_text(title, field="title", max_len=MAX_TITLE_LEN, required=True)
    kind = _clean_text(kind or "artifact", field="kind", max_len=MAX_KIND_LEN, required=True)
    session_id = _clean_text(session_id, field="session_id", max_len=128, required=False)
    content_type = _safe_content_type(content_type)
    output_id = uuid.uuid4().hex[:12]
    source: Optional[Path] = None
    if content:
        raw = content.encode("utf-8")
        if len(raw) > MAX_OUTPUT_BYTES:
            raise ValueError(f"content exceeds {MAX_OUTPUT_BYTES} bytes")
        dest = _destination(task_id, output_id, content_type, title, None)
        dest.write_bytes(raw)
    else:
        source = _source_path(file_path, task_cwd=task_cwd)
        guessed = mimetypes.guess_type(str(source))[0]
        if guessed and not content_type:
            content_type = _safe_content_type(guessed)
        dest = _destination(task_id, output_id, content_type, title, source)
        shutil.copyfile(source, dest)
    size = dest.stat().st_size
    now = datetime.now().isoformat()
    rec = {
        "id": output_id,
        "task_id": task_id,
        "title": title,
        "kind": kind,
        "content_type": content_type,
        "path": str(dest),
        "size_bytes": size,
        "session_id": session_id,
        "created_at": now,
    }
    with _lock:
        data = _read()
        rows = [o for o in data["outputs"] if o.get("task_id") == task_id]
        keep_ids = {o.get("id") for o in rows[: MAX_PER_TASK - 1]}
        dropped = [
            o for o in data["outputs"]
            if o.get("task_id") == task_id and o.get("id") not in keep_ids
        ]
        data["outputs"] = [
            o for o in data["outputs"]
            if o.get("task_id") != task_id or o.get("id") in keep_ids
        ]
        data["outputs"].insert(0, rec)
        _write(data)
    for old in dropped:
        try:
            old_path = Path(str(old.get("path") or "")).resolve()
            _assert_under(old_path, _files_root(), label="output path")
            old_path.unlink(missing_ok=True)
        except (OSError, ValueError):
            pass
    return public_record(rec)


def public_record(rec: dict) -> dict:
    return {
        "id": rec.get("id"),
        "task_id": rec.get("task_id"),
        "title": rec.get("title"),
        "kind": rec.get("kind"),
        "content_type": rec.get("content_type"),
        "size_bytes": rec.get("size_bytes"),
        "session_id": rec.get("session_id") or "",
        "created_at": rec.get("created_at"),
    }


def list_for_task(task_id: str, *, limit: int = MAX_PER_TASK) -> list[dict]:
    task_id = (task_id or "").strip()
    with _lock:
        data = _read()
    rows = [
        o for o in data["outputs"]
        if isinstance(o, dict) and o.get("task_id") == task_id
    ]
    rows.sort(key=lambda o: str(o.get("created_at") or ""), reverse=True)
    return [public_record(o) for o in rows[: max(1, min(limit, MAX_PER_TASK))]]


def get(task_id: str, output_id: str) -> Optional[dict]:
    task_id = (task_id or "").strip()
    output_id = (output_id or "").strip()
    with _lock:
        data = _read()
    for rec in data["outputs"]:
        if rec.get("task_id") == task_id and rec.get("id") == output_id:
            return dict(rec)
    return None


def content_path(task_id: str, output_id: str) -> tuple[Path, str]:
    task_id = _clean_task_id(task_id)
    rec = get(task_id, output_id)
    if rec is None:
        raise FileNotFoundError("unknown output")
    path = Path(str(rec.get("path") or "")).resolve()
    _assert_under(path, _files_root(), label="output path")
    if not path.is_file():
        raise FileNotFoundError("output file is missing")
    return path, _safe_content_type(str(rec.get("content_type") or "text/plain"))


def delete_for_task(task_id: str) -> None:
    if not (task_id or "").strip():
        return
    task_id = _clean_task_id(task_id)
    with _lock:
        data = _read()
        data["outputs"] = [o for o in data["outputs"] if o.get("task_id") != task_id]
        _write(data)
    root = _files_root() / task_id
    if root.exists():
        shutil.rmtree(root)
