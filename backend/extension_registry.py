from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import extension_store


@dataclass(frozen=True)
class BuiltinMcpExtension:
    extension_id: str
    name: str
    mcp_server: str
    user_facing: bool
    bare_allowed: bool
    requires_backend_auth: bool
    predicate: Callable[[dict], bool]


BUILTIN_MCP_EXTENSIONS: tuple[BuiltinMcpExtension, ...] = ()


def _disabled_extension_ids(inputs: dict) -> set[str]:
    raw = inputs.get("disabled_builtin_extensions")
    if not isinstance(raw, list):
        return set()
    known = {item.extension_id for item in BUILTIN_MCP_EXTENSIONS}
    return {
        extension_id
        for extension_id in (str(item or "").strip() for item in raw)
        if extension_id in known
    }


def active_builtin_mcp_extensions(inputs: dict, *, user_facing: bool, bare: bool) -> list[BuiltinMcpExtension]:
    disabled = _disabled_extension_ids(inputs)
    active: list[BuiltinMcpExtension] = []
    for extension in BUILTIN_MCP_EXTENSIONS:
        if not extension_store.is_extension_runtime_ready(extension.extension_id):
            continue
        if extension.extension_id in disabled:
            continue
        if bare and not extension.bare_allowed:
            continue
        if extension.user_facing and not user_facing:
            continue
        if not extension.user_facing and bare and not extension.bare_allowed:
            continue
        if extension.predicate(inputs):
            active.append(extension)
    return active
