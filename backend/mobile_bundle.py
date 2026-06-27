"""Versioned web-bundle packaging for the Capacitor OTA updater.

Single source of truth = the served `frontend/dist`. The bundle version is
derived from the hashed `index-<hash>.js` filename Vite emits, so it changes
exactly when the web build changes. The zip is built on demand and cached by
version under `bc_home()/mobile_bundle/`.
"""
from __future__ import annotations

import hashlib
import io
import re
import threading
import zipfile
from pathlib import Path

from paths import ba_home

_INDEX_JS_RE = re.compile(r"index-([A-Za-z0-9_-]+)\.js")
_lock = threading.Lock()
_cache: dict[str, dict] = {}


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

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in sorted(dist_dir.rglob("*")):
                if f.is_file():
                    zf.write(f, f.relative_to(dist_dir).as_posix())
        data = buf.getvalue()
        checksum = hashlib.sha256(data).hexdigest()
        zip_path = _bundle_dir() / f"{version}.zip"
        zip_path.write_bytes(data)

        info = {"path": str(zip_path), "checksum": checksum}
        _cache[version] = info
        return {"version": version, **info}
