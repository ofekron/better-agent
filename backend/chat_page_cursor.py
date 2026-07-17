"""Signed load-more page cursors for the BFF chat tree.

The chat-tree GET issues an opaque HMAC-signed cursor binding
{root id, pane, source-catalog generation, projection revision head,
window-start turn id, and that turn's prompt-fact anchor}. Load-more
echoes it; any signature/shape/binding mismatch maps to the endpoint's
typed 409 `stale_turn_cursor` so a stale page is never served
undetected after a projection rebuild.

Signing follows the /children cursor convention
(`historical_children_projection._cursor_encode`): canonical JSON +
HMAC-SHA256, base64url without padding. The secret is BFF-local under
`ba_home()/chat/` (0600, created atomically once).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import threading
from pathlib import Path
from typing import Any, Mapping

from paths import ba_home

CURSOR_VERSION = 1
MAX_CURSOR_CHARS = 2048
_HEX = set("0123456789abcdef")
_FIELDS = {"v", "root", "pane", "gen", "rev", "turn", "turn_seq", "turn_hash"}
_lock = threading.Lock()
_secret_cache: tuple[Path, str] | None = None


class PageCursorError(ValueError):
    pass


def _secret_path() -> Path:
    return Path(os.path.abspath(ba_home().expanduser())) / "chat" / "page_cursor_secret"


def _load_secret() -> str:
    global _secret_cache
    path = _secret_path()
    with _lock:
        if _secret_cache is not None and _secret_cache[0] == path:
            return _secret_cache[1]
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            temp = path.parent / f".page_cursor_secret.{os.getpid()}"
            fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            try:
                os.write(fd, os.urandom(32).hex().encode("ascii"))
                os.fsync(fd)
            finally:
                os.close(fd)
            try:
                os.link(temp, path)
            except FileExistsError:
                pass  # concurrent creator won; use theirs
            finally:
                os.unlink(temp)
        raw = path.read_text(encoding="ascii").strip()
        if len(raw) != 64 or not set(raw) <= _HEX:
            raise PageCursorError("page cursor secret is invalid")
        _secret_cache = (path, raw)
        return raw


def encode_page_cursor(
    *, root_id: str, pane_id: str, generation: int, revision: int,
    turn_id: str, turn_seq: int, turn_hash: str,
) -> str:
    payload = {
        "v": CURSOR_VERSION, "root": root_id, "pane": pane_id,
        "gen": generation, "rev": revision, "turn": turn_id,
        "turn_seq": turn_seq, "turn_hash": turn_hash,
    }
    _validate_payload(payload)
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    signature = hmac.new(bytes.fromhex(_load_secret()), raw, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(raw + signature).decode("ascii").rstrip("=")


def decode_page_cursor(token: str) -> dict[str, Any]:
    """Verify + parse a page cursor. Raises PageCursorError on ANY problem
    (fail closed); callers map that to the typed 409 stale_turn_cursor."""
    if not isinstance(token, str) or not 1 <= len(token) <= MAX_CURSOR_CHARS:
        raise PageCursorError("page cursor token is invalid")
    try:
        packed = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    except (ValueError, UnicodeError) as exc:
        raise PageCursorError("page cursor token is invalid") from exc
    if base64.urlsafe_b64encode(packed).decode("ascii").rstrip("=") != token:
        raise PageCursorError("page cursor token is invalid")
    if len(packed) <= 32:
        raise PageCursorError("page cursor token is invalid")
    raw, signature = packed[:-32], packed[-32:]
    expected = hmac.new(bytes.fromhex(_load_secret()), raw, hashlib.sha256).digest()
    if not hmac.compare_digest(signature, expected):
        raise PageCursorError("page cursor signature is invalid")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise PageCursorError("page cursor payload is invalid") from exc
    _validate_payload(payload)
    return dict(payload)


def _validate_payload(payload: Any) -> None:
    if not isinstance(payload, Mapping) or set(payload) != _FIELDS:
        raise PageCursorError("page cursor payload is invalid")
    if payload["v"] != CURSOR_VERSION:
        raise PageCursorError("page cursor version is unsupported")
    for key in ("root", "pane", "turn"):
        value = payload[key]
        if not isinstance(value, str) or not value or len(value) > 512:
            raise PageCursorError("page cursor payload is invalid")
    for key in ("gen", "rev", "turn_seq"):
        value = payload[key]
        if type(value) is not int or not 0 <= value <= 9_223_372_036_854_775_807:
            raise PageCursorError("page cursor payload is invalid")
    turn_hash = payload["turn_hash"]
    if not isinstance(turn_hash, str) or len(turn_hash) != 64 or not set(turn_hash) <= _HEX:
        raise PageCursorError("page cursor payload is invalid")
