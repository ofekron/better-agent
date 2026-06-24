from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional


def normalize_provider_run_config(value: Optional[dict]) -> dict:
    if not value:
        return {}
    if not isinstance(value, dict):
        raise ValueError("provider_run_config must be an object")

    out: dict[str, Any] = {}
    mcp_servers = value.get("mcp_servers", value.get("mcpServers", {}))
    if mcp_servers:
        if not isinstance(mcp_servers, dict):
            raise ValueError("provider_run_config.mcp_servers must be an object")
        out["mcp_servers"] = mcp_servers

    skills = value.get("skills", {})
    if skills:
        if not isinstance(skills, dict):
            raise ValueError("provider_run_config.skills must be an object")
        out["skills"] = skills

    return out


def write_skill_tree(root: Path, skills: dict) -> None:
    for name, value in skills.items():
        if not isinstance(name, str) or not name.strip() or "/" in name or "\\" in name:
            raise ValueError(f"invalid skill name: {name!r}")
        skill_dir = root / name
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(_skill_text(name, value), encoding="utf-8")


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
