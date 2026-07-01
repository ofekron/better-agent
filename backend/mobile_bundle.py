"""Versioned web-bundle packaging for the Capacitor OTA updater.

Single source of truth = the served `frontend/dist`. The bundle version is
derived from the hashed `index-<hash>.js` filename Vite emits, so it changes
exactly when the web build changes. The zip is built on demand and cached by
version under `bc_home()/mobile_bundle/`.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import threading
import zipfile
from pathlib import Path

from paths import ba_home

_INDEX_JS_RE = re.compile(r"index-([A-Za-z0-9_-]+)\.js")
_lock = threading.Lock()
_cache: dict[str, dict] = {}
_META_SCHEMA_VERSION = 1


def _bundle_dir() -> Path:
    d = ba_home() / "mobile_bundle"
    d.mkdir(parents=True, exist_ok=True)
    return d


def bundle_version(dist_dir: Path) -> str | None:
    """Content-derived version: the hash Vite stamps on the entry chunk."""
    index = dist_dir / "index.html"
    if not index.is_file():
        return None
    m = _INDEX_JS_RE.search(index.read_text("utf-8"))
    return m.group(1) if m else None


def _zip_path(version: str) -> Path:
    return _bundle_dir() / f"{version}.zip"


def _metadata_path(version: str) -> Path:
    return _bundle_dir() / f"{version}.json"


def _persisted_info(version: str) -> dict | None:
    zip_path = _zip_path(version)
    metadata_path = _metadata_path(version)
    if not zip_path.is_file() or not metadata_path.is_file():
        return None
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if metadata.get("schema_version") != _META_SCHEMA_VERSION:
        return None
    if metadata.get("version") != version:
        return None
    checksum = str(metadata.get("checksum") or "")
    size = metadata.get("size")
    if not checksum or not isinstance(size, int):
        return None
    try:
        if zip_path.stat().st_size != size:
            return None
        with zipfile.ZipFile(zip_path, "r") as zf:
            if zf.testzip() is not None:
                return None
        data = zip_path.read_bytes()
    except (OSError, zipfile.BadZipFile):
        return None
    if hashlib.sha256(data).hexdigest() != checksum:
        return None
    return {"path": str(zip_path), "checksum": checksum}


def _write_metadata(version: str, checksum: str, size: int) -> None:
    metadata_path = _metadata_path(version)
    tmp_path = metadata_path.with_suffix(metadata_path.suffix + ".tmp")
    tmp_path.write_text(
        json.dumps(
            {
                "schema_version": _META_SCHEMA_VERSION,
                "version": version,
                "checksum": checksum,
                "size": size,
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    tmp_path.replace(metadata_path)


def build_bundle(dist_dir: Path) -> dict | None:
    """Return {version, path, checksum} for the current dist, building and
    caching the zip if needed. None when dist has no resolvable version."""
    version = bundle_version(dist_dir)
    if not version:
        return None
    with _lock:
        cached = _cache.get(version)
        if cached and Path(cached["path"]).is_file():
            return {"version": version, **cached}
        persisted = _persisted_info(version)
        if persisted is not None:
            _cache[version] = persisted
            return {"version": version, **persisted}

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in sorted(dist_dir.rglob("*")):
                if f.is_file():
                    zf.write(f, f.relative_to(dist_dir).as_posix())
        data = buf.getvalue()
        checksum = hashlib.sha256(data).hexdigest()
        zip_path = _zip_path(version)
        zip_path.write_bytes(data)
        _write_metadata(version, checksum, len(data))

        info = {"path": str(zip_path), "checksum": checksum}
        _cache[version] = info
        return {"version": version, **info}
