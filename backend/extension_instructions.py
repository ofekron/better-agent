"""Instruction-section reading for installed extensions.

Instruction content is injected into sessions through the temporal harness
profile system, resolved per session/turn from each extension's manifest.
There is no longer a provider-config-file reconciliation path.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any


def _local_project_paths() -> list[Path]:
    """Absolute paths of the user's local (primary-node) projects."""
    import project_store

    paths: list[Path] = []
    for project in project_store.list_projects():
        if (project.get("node_id") or "primary") != "primary":
            continue
        raw = project.get("path")
        if raw:
            paths.append(Path(str(raw)).expanduser().resolve())
    return paths


def instruction_items_from_entrypoints(entrypoints: dict) -> Any:
    """Instruction sections from a manifest's entrypoints.

    Single source for reading instruction sections; accepts the legacy
    ``provider_capabilities`` field as an alias for ``instructions`` (legacy
    items had no level, so they are treated as global-scope). Lets already-
    installed extensions authored before the rename keep contributing their
    instruction content.
    """
    items = entrypoints.get("instructions")
    if items is None:
        legacy = entrypoints.get("provider_capabilities") or []
        items = [{**i, "level": "global"} for i in legacy if isinstance(i, dict)]
    return items


def _instruction_items(manifest: dict) -> list[dict]:
    return instruction_items_from_entrypoints(manifest.get("entrypoints") or {}) or []


def runtime_instruction_blocks(record: dict) -> list[str]:
    manifest = record.get("manifest") or {}
    extension_id = str(manifest.get("id") or "")
    if not extension_id:
        return []
    import extension_store

    install_path = extension_store.runtime_package_root_for_record(record)
    if install_path is None:
        return []
    root = install_path.resolve()
    blocks: list[str] = []
    for item in _instruction_items(manifest):
        content_path = (root / item["path"]).resolve()
        if not content_path.is_relative_to(root) or not content_path.is_file():
            continue
        providers = item.get("providers")
        scope = f" Providers: {', '.join(providers)} only." if providers else ""
        content = content_path.read_text(encoding="utf-8").strip()
        if content:
            blocks.append(f"### {item['name']} ({extension_id}).{scope}\n{content}")
    return blocks


def normalize_state(record: dict) -> dict:
    """Instruction enable state with defaults: ``{global: bool, projects: {path: bool}}``."""
    raw = record.get("instructions_enabled") or {}
    projects = raw.get("projects") or {}
    return {
        "global": bool(raw.get("global", True)),
        "projects": {str(k): bool(v) for k, v in projects.items()},
    }
