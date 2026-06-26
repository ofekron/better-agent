from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path
from typing import Any

from paths import atomic_replace, ba_home


_TTL_SECONDS = 10 * 60
_FILE_MODE = 0o600


def _path() -> Path:
    return ba_home() / "auth_setup_nonce.json"


def mint() -> str:
    nonce = secrets.token_urlsafe(32)
    record = {"nonce": nonce, "expires_at": time.time() + _TTL_SECONDS}
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _FILE_MODE)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(record, handle, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    atomic_replace(tmp, path)
    os.chmod(path, _FILE_MODE)
    return nonce


def _read() -> dict[str, Any] | None:
    path = _path()
    try:
        st = path.stat()
        if st.st_mode & 0o077:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def consume(candidate: str | None) -> bool:
    token = str(candidate or "").strip()
    if not token:
        return False
    data = _read()
    if not data:
        return False
    if time.time() > float(data.get("expires_at") or 0):
        _path().unlink(missing_ok=True)
        return False
    if not secrets.compare_digest(str(data.get("nonce") or ""), token):
        return False
    _path().unlink(missing_ok=True)
    return True
