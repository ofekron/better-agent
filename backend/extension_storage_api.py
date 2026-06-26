from __future__ import annotations

import asyncio
import base64
import os
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Header, HTTPException

import extension_store
from paths import atomic_replace, ba_home

router = APIRouter(prefix="/api/internal/extension-storage", tags=["extension-storage"])

_MAX_VALUE_BYTES = 2 * 1024 * 1024


def _require_storage_extension(internal_token: str) -> str:
    from orchestrator import get_active_coordinator

    coordinator = get_active_coordinator()
    if coordinator is None or not coordinator.is_internal_caller(internal_token):
        raise HTTPException(status_code=403, detail="invalid internal token")
    # Identity is derived from the per-extension token, NOT the self-asserted
    # X-Extension-Id header — otherwise any internal_loopback extension could
    # read/write another extension's storage by spoofing the header.
    clean_id = coordinator.principal_extension_id(internal_token) or ""
    record = extension_store.get_extension(clean_id) if clean_id else None
    if (
        record is None
        or not extension_store.is_extension_active(clean_id)
        or not extension_store.has_permission(record, "storage")
    ):
        raise HTTPException(status_code=403, detail="extension lacks storage permission")
    return clean_id


def _storage_root(extension_id: str) -> Path:
    return ba_home() / "extensions" / "storage" / extension_id


def _resolve_key(extension_id: str, key: Any) -> Path:
    raw = str(key or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="key is required")
    rel = Path(raw)
    if rel.is_absolute() or any(part in {"", ".", ".."} for part in rel.parts):
        raise HTTPException(status_code=400, detail="key must be a safe relative path")
    root = _storage_root(extension_id).resolve()
    path = (root / rel).resolve(strict=False)
    if not path.is_relative_to(root):
        raise HTTPException(status_code=400, detail="key escapes extension storage")
    return path


def _assert_no_symlink_components(root: Path, path: Path) -> None:
    current = root
    for part in path.relative_to(root).parts:
        current = current / part
        if current.exists() and current.is_symlink():
            raise HTTPException(status_code=400, detail="key contains a symlink")


def _open_no_follow(path: Path, flags: int, mode: int = 0o600) -> int:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(str(path), flags | nofollow, mode)
    except OSError as exc:
        raise HTTPException(status_code=400, detail="key does not reference a safe file") from exc


def _read_value(extension_id: str, path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"success": True, "found": False}
    root = _storage_root(extension_id).resolve()
    _assert_no_symlink_components(root, path)
    if path.is_symlink() or not path.is_file():
        raise HTTPException(status_code=400, detail="key does not reference a file")
    fd = _open_no_follow(path, os.O_RDONLY)
    with os.fdopen(fd, "rb") as handle:
        raw = handle.read()
    return {
        "success": True,
        "found": True,
        "value_base64": base64.b64encode(raw).decode("ascii"),
        "size": len(raw),
    }


@router.post("/get")
async def storage_get(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    extension_id = _require_storage_extension(x_internal_token)
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")
    return await asyncio.to_thread(_read_value, extension_id, _resolve_key(extension_id, body.get("key")))


@router.post("/put")
async def storage_put(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    extension_id = _require_storage_extension(x_internal_token)
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")
    path = _resolve_key(extension_id, body.get("key"))
    try:
        value = base64.b64decode(str(body.get("value_base64") or ""), validate=True)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="value_base64 must be valid base64") from exc
    if len(value) > _MAX_VALUE_BYTES:
        raise HTTPException(status_code=400, detail="value is too large")

    def _write() -> None:
        root = _storage_root(extension_id).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        _assert_no_symlink_components(root, path)
        if path.exists() and (path.is_symlink() or not path.is_file()):
            raise HTTPException(status_code=400, detail="key does not reference a file")
        tmp = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
        fd = _open_no_follow(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(value)
                handle.flush()
                os.fsync(handle.fileno())
            atomic_replace(tmp, path)
            if path.is_symlink() or not path.is_file():
                raise HTTPException(status_code=400, detail="key does not reference a file")
        finally:
            tmp.unlink(missing_ok=True)

    await asyncio.to_thread(_write)
    return {"success": True, "size": len(value)}


@router.post("/delete")
async def storage_delete(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    extension_id = _require_storage_extension(x_internal_token)
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")
    path = _resolve_key(extension_id, body.get("key"))

    def _delete() -> bool:
        if not path.exists():
            return False
        root = _storage_root(extension_id).resolve()
        _assert_no_symlink_components(root, path)
        if path.is_symlink() or not path.is_file():
            raise HTTPException(status_code=400, detail="key does not reference a file")
        path.unlink()
        return True

    deleted = await asyncio.to_thread(_delete)
    return {"success": True, "deleted": deleted}
