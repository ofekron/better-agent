from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Mapping


CONTRACT_SCHEMA = 1
SECRET_NAMES = (
    "api_key",
    "apikey",
    "password",
    "secret",
    "token",
    "credential",
    "authorization",
)
ALLOWED_ENVIRONMENT_SELECTORS = {"CODEX_HOME"}
ALLOWED_CONFIG_KEYS = {
    "features.image_generation",
    "features.shell_snapshot",
    "model",
    "model_provider",
    "shell_environment_policy.exclude",
}
SAFE_PROFILE_RE = re.compile(r"^[A-Za-z0-9_.-]{0,64}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ExecutionContractError(RuntimeError):
    pass


def sha256_fd(fd: int) -> str:
    digest = hashlib.sha256()
    os.lseek(fd, 0, os.SEEK_SET)
    while True:
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
    os.lseek(fd, 0, os.SEEK_SET)
    return digest.hexdigest()


def sha256_and_first_line_fd(fd: int) -> tuple[str, bytes]:
    digest = hashlib.sha256()
    first_line = bytearray()
    line_complete = False
    os.lseek(fd, 0, os.SEEK_SET)
    while True:
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
        if not line_complete and len(first_line) < 4096:
            remaining = 4096 - len(first_line)
            prefix = chunk[:remaining]
            newline = prefix.find(b"\n")
            if newline >= 0:
                first_line.extend(prefix[:newline + 1])
                line_complete = True
            else:
                first_line.extend(prefix)
                if len(first_line) == 4096:
                    line_complete = True
    os.lseek(fd, 0, os.SEEK_SET)
    return digest.hexdigest(), bytes(first_line)


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def required_string(raw: Mapping[str, Any], key: str) -> str:
    value = raw.get(key)
    if type(value) is not str:
        raise ExecutionContractError(f"{key} must be a string")
    return value


def required_integer(raw: Mapping[str, Any], key: str) -> int:
    value = raw.get(key)
    if type(value) is not int:
        raise ExecutionContractError(f"{key} must be an integer")
    return value


def required_string_list(
    raw: Mapping[str, Any],
    key: str,
) -> tuple[str, ...]:
    value = raw.get(key)
    if type(value) is not list or any(type(item) is not str for item in value):
        raise ExecutionContractError(f"{key} must be a string list")
    return tuple(value)


def symlink_chain(path: Path) -> tuple[tuple[str, str], ...]:
    current = path
    seen: set[Path] = set()
    chain: list[tuple[str, str]] = []
    for _ in range(64):
        absolute = current.absolute()
        if absolute in seen:
            raise ExecutionContractError("provider executable symlink cycle")
        seen.add(absolute)
        if not current.is_symlink():
            return tuple(chain)
        target = os.readlink(current)
        chain.append((str(absolute), target))
        target_path = Path(target)
        current = (
            target_path
            if target_path.is_absolute()
            else current.parent / target_path
        )
    raise ExecutionContractError("provider executable symlink chain is too deep")
