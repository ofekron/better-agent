from __future__ import annotations

import os
from pathlib import Path
import re
import stat

from json_store import write_private_text

_MAX_TOKEN_BYTES = 256
_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{32,256}")


def read_private_token(path: Path) -> str | None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        path_identity = os.lstat(path)
        if (
            not stat.S_ISREG(path_identity.st_mode)
            or _is_windows_reparse_point(path_identity)
        ):
            return None
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        identity = os.fstat(fd)
        if not stat.S_ISREG(identity.st_mode):
            return None
        if (identity.st_dev, identity.st_ino) != (
            path_identity.st_dev,
            path_identity.st_ino,
        ):
            return None
        if not _is_private_identity(path, identity):
            return None
        if not 0 < identity.st_size <= _MAX_TOKEN_BYTES:
            return None
        chunks = bytearray()
        while len(chunks) <= _MAX_TOKEN_BYTES:
            chunk = os.read(
                fd,
                _MAX_TOKEN_BYTES + 1 - len(chunks),
            )
            if not chunk:
                break
            chunks.extend(chunk)
    except OSError:
        return None
    finally:
        os.close(fd)
    if len(chunks) > _MAX_TOKEN_BYTES:
        return None
    try:
        token = bytes(chunks).decode("ascii")
    except UnicodeDecodeError:
        return None
    return token if _TOKEN_PATTERN.fullmatch(token) else None


def write_private_token(path: Path, token: str) -> None:
    if not _TOKEN_PATTERN.fullmatch(token):
        raise ValueError("internal token has invalid shape")
    write_private_text(path, token)


def _is_windows_reparse_point(identity: os.stat_result) -> bool:
    attributes = int(getattr(identity, "st_file_attributes", 0) or 0)
    marker = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return bool(attributes & marker)


def _is_private_identity(path: Path, identity: os.stat_result) -> bool:
    if os.name == "nt":
        from paths import windows_path_has_private_acl

        return windows_path_has_private_acl(path)
    return (
        hasattr(os, "getuid")
        and identity.st_uid == os.getuid()
        and identity.st_mode & 0o077 == 0
    )
