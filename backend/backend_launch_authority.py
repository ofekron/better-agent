from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from env_compat import get_env
from paths import (
    assert_state_root_safe,
    ba_home,
    is_test_mode,
    make_private_file,
)

_AUTHORITY_FILE = "backend_launch_authority.json"
_TOKEN_ENV = "BETTER_AGENT_BACKEND_LAUNCH_TOKEN"
_GENERATION_ENV = "BETTER_AGENT_BACKEND_LAUNCH_GENERATION"
_ACTIVE_CHECKOUT_ENV = "BETTER_AGENT_ACTIVE_CHECKOUT"
_VERSION = 1
_POINTER_STATES = frozenset({"active", "switching", "reverted"})
_RECORD_KEYS = frozenset({
    "version",
    "generation",
    "token_sha256",
    "checkout",
    "state_root",
    "role",
    "issuer_pid",
    "issued_at",
})
_HEX_32_RE = re.compile(r"[0-9a-f]{32}")
_HEX_64_RE = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class LaunchAuthority:
    generation: str
    token: str
    checkout: Path
    state_root: Path
    test_mode: bool


def launch_env_keys() -> tuple[str, ...]:
    return (
        _TOKEN_ENV,
        _GENERATION_ENV,
        _ACTIVE_CHECKOUT_ENV,
        "BETTER_CLAUDE_ACTIVE_CHECKOUT",
    )


def _canonical(path: Path | str) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute() or ".." in candidate.parts:
        raise RuntimeError("backend launch authority paths must be absolute")
    return candidate.resolve()


def _authority_path(root: Path) -> Path:
    return root / _AUTHORITY_FILE


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _write_private_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    fd = -1
    try:
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        data = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        offset = 0
        while offset < len(data):
            offset += os.write(fd, data[offset:])
        os.fsync(fd)
        os.close(fd)
        fd = -1
        make_private_file(temporary)
        os.replace(temporary, path)
        make_private_file(path)
        if os.name != "nt":  # pragma: no branch - posix-only dir fsync; nt skips it
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def issue_primary_backend_launch(
    *,
    checkout: Path | str,
    state_root: Path | str | None = None,
    generation: str | None = None,
) -> dict[str, str]:
    root = _canonical(state_root or ba_home())
    selected_checkout = _canonical(checkout)
    generation = generation or uuid.uuid4().hex
    if _HEX_32_RE.fullmatch(generation) is None:
        raise ValueError("backend launch generation must be 32 lowercase hex characters")
    token = secrets.token_urlsafe(32)
    _write_private_json(
        _authority_path(root),
        {
            "version": _VERSION,
            "generation": generation,
            "token_sha256": _token_digest(token),
            "checkout": str(selected_checkout),
            "state_root": str(root),
            "role": "primary",
            "issuer_pid": os.getpid(),
            "issued_at": time.time(),
        },
    )
    return {
        _TOKEN_ENV: token,
        _GENERATION_ENV: generation,
        _ACTIVE_CHECKOUT_ENV: str(selected_checkout),
        "BETTER_CLAUDE_ACTIVE_CHECKOUT": str(selected_checkout),
    }


def _read_object(path: Path, label: str) -> dict[str, Any]:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):  # pragma: no branch - absent only on platforms lacking the flag
        flags |= os.O_NOFOLLOW
    try:
        before = path.lstat()
        if (
            stat.S_ISLNK(before.st_mode)
            or (
                getattr(before, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            )
            or not stat.S_ISREG(before.st_mode)
        ):
            raise RuntimeError(f"backend launch authority {label} is invalid")
        fd = os.open(path, flags)
        try:
            opened = os.fstat(fd)
            raw = bytearray()
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                raw.extend(chunk)
        finally:
            os.close(fd)
        after = path.lstat()
    except FileNotFoundError as exc:
        raise RuntimeError(f"backend launch authority {label} is missing") from exc
    except OSError as exc:
        raise RuntimeError(f"backend launch authority {label} is invalid") from exc
    identities = {
        (before.st_dev, before.st_ino),
        (opened.st_dev, opened.st_ino),
        (after.st_dev, after.st_ino),
    }
    if (  # pragma: no branch - TOCTOU swap race / windows reparse point only
        len(identities) != 1
        or (
            getattr(after, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        )
    ):
        raise RuntimeError(  # pragma: no cover - TOCTOU swap race / windows reparse point only
            f"backend launch authority {label} changed during validation"
        )
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"backend launch authority {label} is invalid") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"backend launch authority {label} is invalid")
    return value


def _validate_pointer(root: Path, checkout: Path) -> None:
    path = root / "active_checkout.json"
    try:
        path.lstat()
    except FileNotFoundError:
        return
    pointer = _read_object(path, "active checkout pointer")
    status = pointer.get("status")
    active = pointer.get("active")
    if status == "failed":
        return
    if status not in _POINTER_STATES or not isinstance(active, str) or not active:
        raise RuntimeError("backend launch authority active checkout pointer is invalid")
    if _canonical(active) != checkout:
        raise RuntimeError("backend launch authority does not match active checkout pointer")


def _record_path(record: dict[str, Any], key: str) -> Path:
    value = record.get(key)
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"backend launch authority record has invalid {key}")
    return _canonical(value)


def assert_primary_backend_launch_authorized(
    *,
    executing_checkout: Path | str | None = None,
) -> LaunchAuthority:
    root = _canonical(ba_home())
    checkout = _canonical(
        executing_checkout or Path(__file__).resolve().parents[1]
    )
    if is_test_mode():
        assert_state_root_safe(root)
        return LaunchAuthority("", "", checkout, root, True)

    token = os.environ.get(_TOKEN_ENV, "")
    generation = os.environ.get(_GENERATION_ENV, "")
    active_checkout = get_env("BETTER_CLAUDE_ACTIVE_CHECKOUT") or ""
    if not token or not generation or not active_checkout:
        raise RuntimeError("backend launch authority environment is missing")
    if _canonical(active_checkout) != checkout:
        raise RuntimeError("backend launch authority checkout does not match executable")

    record = _read_object(_authority_path(root), "record")
    if set(record) != _RECORD_KEYS:
        raise RuntimeError("backend launch authority record has unexpected fields")
    if (
        record.get("version") != _VERSION
        or record.get("role") != "primary"
        or not isinstance(record.get("issuer_pid"), int)
        or isinstance(record.get("issuer_pid"), bool)
        or record["issuer_pid"] <= 0
        or not isinstance(record.get("issued_at"), (int, float))
        or isinstance(record.get("issued_at"), bool)
        or not isinstance(record.get("generation"), str)
        or _HEX_32_RE.fullmatch(record["generation"]) is None
        or not isinstance(record.get("token_sha256"), str)
        or _HEX_64_RE.fullmatch(record["token_sha256"]) is None
    ):
        raise RuntimeError("backend launch authority record is invalid")
    if record.get("generation") != generation:
        raise RuntimeError("backend launch authority generation is stale")
    digest = record.get("token_sha256")
    if not isinstance(digest, str) or not hmac.compare_digest(
        digest, _token_digest(token)
    ):
        raise RuntimeError("backend launch authority token is stale")
    if _record_path(record, "checkout") != checkout:
        raise RuntimeError("backend launch authority record checkout does not match executable")
    if _record_path(record, "state_root") != root:
        raise RuntimeError("backend launch authority record state root does not match")
    _validate_pointer(root, checkout)
    return LaunchAuthority(generation, token, checkout, root, False)


def clear_primary_backend_launch_token(authority: LaunchAuthority) -> None:
    if authority.test_mode:
        return
    current = os.environ.get(_TOKEN_ENV, "")
    if not hmac.compare_digest(current, authority.token):
        raise RuntimeError("backend launch authority token changed before sanitization")
    os.environ.pop(_TOKEN_ENV, None)
