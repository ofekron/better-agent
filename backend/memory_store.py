"""Durable, provider-agnostic agent memory store.

One memory = one markdown file with YAML-ish frontmatter, grouped under a
scope directory (`global`, or a hashed `project`/`folder` path via
`paths.encode_cwd`, mirroring how claude CLI keys `~/.claude/projects/`).
Each scope directory owns a single `MEMORY.md` index that is always
REBUILT from the per-file frontmatter on write/delete -- it is a
projection, never a second source of truth (frontmatter in the `.md`
files is authoritative).

All paths resolve through `paths.ba_home()` per the repo's state-directory
isolation rule; never hardcode `~/.better-claude`.
"""

from __future__ import annotations

import re
import threading
import time
from pathlib import Path
from typing import Any

from paths import ba_home, encode_cwd

_LOCK = threading.RLock()
_MEMORY_TYPES = ("user", "feedback", "project", "reference")
_SCOPE_TYPES = ("global", "project", "folder")
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")


class MemoryStoreError(ValueError):
    pass


def _root() -> Path:
    root = ba_home() / "memory"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _validate_slug(slug: str) -> str:
    if not _SLUG_RE.match(slug or ""):
        raise MemoryStoreError("slug must be lowercase kebab-case, 1-80 chars")
    return slug


def scope_dir(scope_type: str, scope_path: str) -> Path:
    if scope_type not in _SCOPE_TYPES:
        raise MemoryStoreError(f"scope_type must be one of {_SCOPE_TYPES}")
    if scope_type == "global":
        return _root() / "global"
    if not scope_path:
        raise MemoryStoreError("scope_path is required for project/folder scope")
    resolved = str(Path(scope_path).expanduser())
    key = encode_cwd(resolved) if Path(resolved).is_absolute() else encode_cwd(str(Path(resolved).resolve()))
    return _root() / "scoped" / key


def _scope_meta_path(directory: Path) -> Path:
    return directory / "_scope.json"


def _write_scope_meta(directory: Path, scope_type: str, scope_path: str) -> None:
    import json

    if scope_type == "global":
        return
    meta = {"type": scope_type, "path": scope_path}
    tmp = _scope_meta_path(directory).with_suffix(".tmp")
    tmp.write_text(json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(_scope_meta_path(directory))


def _read_scope_meta(directory: Path) -> dict[str, Any] | None:
    import json

    meta_path = _scope_meta_path(directory)
    if not meta_path.exists():
        return None
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _frontmatter_block(
    *,
    slug: str,
    description: str,
    mem_type: str,
    scope_type: str,
    scope_path: str,
    created_at: str,
    updated_at: str,
) -> str:
    return (
        "---\n"
        f"name: {slug}\n"
        f"description: {description}\n"
        "metadata:\n"
        f"  type: {mem_type}\n"
        f"  scope_type: {scope_type}\n"
        f"  scope_path: {scope_path}\n"
        f"  created_at: {created_at}\n"
        f"  updated_at: {updated_at}\n"
        "---\n"
    )


_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.DOTALL)
_FIELD_RE = re.compile(r"^([a-zA-Z_]+):\s*(.*)$")


def _parse_memory_file(text: str) -> dict[str, Any] | None:
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return None
    header, body = match.group(1), match.group(2)
    fields: dict[str, str] = {}
    metadata: dict[str, str] = {}
    in_metadata = False
    for line in header.splitlines():
        if line.startswith("metadata:"):
            in_metadata = True
            continue
        if in_metadata and line.startswith("  "):
            field_match = _FIELD_RE.match(line.strip())
            if field_match:
                metadata[field_match.group(1)] = field_match.group(2)
            continue
        in_metadata = False
        field_match = _FIELD_RE.match(line)
        if field_match:
            fields[field_match.group(1)] = field_match.group(2)
    return {
        "name": fields.get("name", ""),
        "description": fields.get("description", ""),
        "type": metadata.get("type", ""),
        "scope_type": metadata.get("scope_type", ""),
        "scope_path": metadata.get("scope_path", ""),
        "created_at": metadata.get("created_at", ""),
        "updated_at": metadata.get("updated_at", ""),
        "content": body.lstrip("\n"),
    }


def _rebuild_index_locked(directory: Path) -> None:
    entries = []
    for md_path in sorted(directory.glob("*.md")):
        if md_path.name == "MEMORY.md":
            continue
        parsed = _parse_memory_file(md_path.read_text(encoding="utf-8"))
        if parsed:
            entries.append(parsed)
    lines = ["# Memory index"]
    for entry in entries:
        lines.append(f"- [{entry['name']}]({entry['name']}.md) — {entry['description']}")
    (directory / "MEMORY.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_memory(
    *,
    scope_type: str,
    scope_path: str,
    slug: str,
    description: str,
    mem_type: str,
    content: str,
) -> dict[str, Any]:
    slug = _validate_slug(slug)
    if mem_type not in _MEMORY_TYPES:
        raise MemoryStoreError(f"type must be one of {_MEMORY_TYPES}")
    if not description.strip():
        raise MemoryStoreError("description is required")
    if not content.strip():
        raise MemoryStoreError("content is required")
    directory = scope_dir(scope_type, scope_path)
    with _LOCK:
        directory.mkdir(parents=True, exist_ok=True)
        _write_scope_meta(directory, scope_type, scope_path)
        target = directory / f"{slug}.md"
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        created_at = now
        if target.exists():
            existing = _parse_memory_file(target.read_text(encoding="utf-8"))
            if existing and existing.get("created_at"):
                created_at = existing["created_at"]
        block = _frontmatter_block(
            slug=slug,
            description=description.strip(),
            mem_type=mem_type,
            scope_type=scope_type,
            scope_path=scope_path,
            created_at=created_at,
            updated_at=now,
        )
        tmp = target.with_suffix(".tmp")
        tmp.write_text(block + "\n" + content.strip() + "\n", encoding="utf-8")
        tmp.replace(target)
        _rebuild_index_locked(directory)
    return read_memory(scope_type=scope_type, scope_path=scope_path, slug=slug)


def read_memory(*, scope_type: str, scope_path: str, slug: str) -> dict[str, Any] | None:
    slug = _validate_slug(slug)
    directory = scope_dir(scope_type, scope_path)
    target = directory / f"{slug}.md"
    if not target.exists():
        return None
    with _LOCK:
        parsed = _parse_memory_file(target.read_text(encoding="utf-8"))
    return parsed


def delete_memory(*, scope_type: str, scope_path: str, slug: str) -> bool:
    slug = _validate_slug(slug)
    directory = scope_dir(scope_type, scope_path)
    target = directory / f"{slug}.md"
    with _LOCK:
        if not target.exists():
            return False
        target.unlink()
        _rebuild_index_locked(directory)
    return True


def list_memories(*, scope_type: str, scope_path: str) -> list[dict[str, Any]]:
    directory = scope_dir(scope_type, scope_path)
    if not directory.exists():
        return []
    with _LOCK:
        return [
            parsed
            for md_path in sorted(directory.glob("*.md"))
            if md_path.name != "MEMORY.md"
            for parsed in [_parse_memory_file(md_path.read_text(encoding="utf-8"))]
            if parsed
        ]


def list_scopes() -> list[dict[str, str]]:
    """All known non-global scopes, for merging against a cwd."""
    scoped_root = _root() / "scoped"
    if not scoped_root.exists():
        return []
    scopes = []
    for directory in sorted(scoped_root.iterdir()):
        if not directory.is_dir():
            continue
        meta = _read_scope_meta(directory)
        if meta and meta.get("type") in ("project", "folder") and meta.get("path"):
            scopes.append({"type": meta["type"], "path": meta["path"]})
    return scopes


def _is_ancestor_or_equal(ancestor: str, path: str) -> bool:
    try:
        return Path(path).resolve().is_relative_to(Path(ancestor).resolve())
    except (OSError, ValueError):
        return False


def memories_for_cwd(cwd: str) -> dict[str, list[dict[str, Any]]]:
    """Global memories plus every project/folder scope that is an ancestor
    of (or equal to) `cwd` -- the merge order `get_memories` returns to agents."""
    result: dict[str, list[dict[str, Any]]] = {
        "global": list_memories(scope_type="global", scope_path=""),
    }
    for scope in list_scopes():
        if _is_ancestor_or_equal(scope["path"], cwd):
            key = f"{scope['type']}:{scope['path']}"
            result[key] = list_memories(scope_type=scope["type"], scope_path=scope["path"])
    return result
