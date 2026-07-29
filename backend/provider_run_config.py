from __future__ import annotations

import copy
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Optional

_PORTABLE_SKILL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


def normalize_provider_run_config(value: Optional[dict]) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("provider_run_config must be an object")
    if not value:
        return {}

    out = {
        key: copy.deepcopy(item)
        for key, item in value.items()
        if key not in ("mcp_servers", "mcpServers", "skills")
    }
    if "mcp_servers" in value and "mcpServers" in value:
        raise ValueError("provider_run_config MCP server keys are ambiguous")
    mcp_key = (
        "mcp_servers"
        if "mcp_servers" in value
        else "mcpServers"
        if "mcpServers" in value
        else ""
    )
    if mcp_key:
        mcp_servers = value[mcp_key]
        if not isinstance(mcp_servers, dict):
            raise ValueError("provider_run_config.mcp_servers must be an object")
        for name, config in mcp_servers.items():
            if (
                not isinstance(name, str)
                or not name
                or name != name.strip()
            ):
                raise ValueError(
                    "provider_run_config.mcp_servers names must be non-empty strings"
                )
            if not isinstance(config, dict):
                raise ValueError(
                    f"provider_run_config.mcp_servers.{name} must be an object"
                )
        if mcp_servers:
            out["mcp_servers"] = copy.deepcopy(mcp_servers)

    if "skills" in value:
        skills = value["skills"]
        if not isinstance(skills, dict):
            raise ValueError("provider_run_config.skills must be an object")
        if skills:
            out["skills"] = copy.deepcopy(skills)

    return out


def merge_provider_run_configs(base: Optional[dict], override: Optional[dict]) -> dict:
    merged = normalize_provider_run_config(base)
    incoming = normalize_provider_run_config(override)
    for key, value in incoming.items():
        if key in ("mcp_servers", "skills") and isinstance(value, dict):
            merged[key] = {
                **copy.deepcopy(merged.get(key) or {}),
                **copy.deepcopy(value),
            }
            continue
        merged[key] = copy.deepcopy(value)
    return merged


def write_skill_tree(root: Path, skills: dict) -> None:
    for name, value in skills.items():
        if (
            not isinstance(name, str)
            or _PORTABLE_SKILL_NAME.fullmatch(name) is None
            or name in (".", "..")
            or name.endswith(".")
            or name.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES
        ):
            raise ValueError(f"invalid skill name: {name!r}")
        skill_dir = root / name
        skill_dir.mkdir(parents=True, exist_ok=True)
        target = skill_dir / "SKILL.md"
        descriptor, temporary_name = tempfile.mkstemp(
            dir=skill_dir,
            prefix=".SKILL.md.",
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(_skill_text(name, value))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)


def toml_literal(value: Any) -> str:
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, list):
        return "[" + ", ".join(toml_literal(item) for item in value) + "]"
    if isinstance(value, dict):
        items = []
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("TOML object keys must be strings")
            items.append(f"{_toml_key(key)} = {toml_literal(item)}")
        return "{ " + ", ".join(items) + " }"
    if value is None:
        raise ValueError("TOML does not support null values")
    raise ValueError(f"unsupported TOML value type: {type(value).__name__}")


def symlink_home_overlay(source_home: Path, target_home: Path, *, skip: set[str]) -> None:
    target_home.mkdir(parents=True, exist_ok=True)
    if not source_home.is_dir():
        return
    for child in source_home.iterdir():
        if child.name in skip:
            continue
        target = target_home / child.name
        if target.exists() or target.is_symlink():
            continue
        os.symlink(child, target, target_is_directory=child.is_dir())


def _skill_text(name: str, value: Any) -> str:
    if isinstance(value, str):
        return value if value.endswith("\n") else value + "\n"
    if not isinstance(value, dict):
        raise ValueError(f"skill {name!r} must be a string or object")
    instructions = value.get("instructions", "")
    if not isinstance(instructions, str):
        raise ValueError(f"skill {name!r}.instructions must be a string")
    metadata = value.get("metadata", {})
    if metadata and not isinstance(metadata, dict):
        raise ValueError(f"skill {name!r}.metadata must be an object")
    frontmatter = {"name": value.get("name") or name}
    if value.get("description"):
        frontmatter["description"] = value["description"]
    frontmatter.update(metadata or {})
    lines = ["---"]
    for key, item in frontmatter.items():
        lines.append(f"{key}: {_yaml_scalar(item)}")
    lines.append("---")
    lines.append(instructions.rstrip("\n"))
    return "\n".join(lines).rstrip() + "\n"


def _toml_key(key: str) -> str:
    if key.replace("_", "").replace("-", "").isalnum() and key[0].isalpha():
        return key
    return json.dumps(key)


def _yaml_scalar(value: Any) -> str:
    if isinstance(value, str):
        return value if value and all(ch not in value for ch in "\n:#{}[]") else json.dumps(value)
    return json.dumps(value)
