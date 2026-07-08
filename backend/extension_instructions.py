"""Managed instruction blocks for installed extensions.

An extension's ``entrypoints.instructions`` sections are materialized as managed
blocks (see ``provider_config_sync_backend.managed_blocks``) inside the provider
instruction files. This module is the single funnel that reconciles the on-disk
blocks to an extension record's enable state: ``global`` sections into the
provider home files (``~/.claude/CLAUDE.md`` …), ``project`` sections into the
project-root files.

State ownership (record read/write, ``extensions.json``) lives in
:mod:`extension_store`; this module only translates a record into block
operations against provider config files via provider-config-sync.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pcs_paths

pcs_paths.ensure_on_path()
from provider_config_sync_backend import api as _pcs  # noqa: E402


def _owner(extension_id: str) -> str:
    return f"extension:{extension_id}"


def _configured_providers() -> list[dict]:
    import config_store

    return config_store.list_provider_metadata()


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


def _sections_for_level(
    manifest: dict, install_path: Path, level: str, *, provider_kind: str
) -> list[tuple[str, str]]:
    """Read ``(section_name, content)`` for sections at ``level`` that apply to ``provider_kind``.

    An item with no ``providers`` field applies to every provider; an item that
    declares ``providers`` only materializes into that subset's instruction files.
    """
    sections: list[tuple[str, str]] = []
    root = install_path.resolve()
    for item in _instruction_items(manifest):
        if item.get("level") != level:
            continue
        allowed = item.get("providers")
        if allowed is not None and provider_kind not in allowed:
            continue
        content_path = (root / item["path"]).resolve()
        if not content_path.is_relative_to(root) or not content_path.is_file():
            continue
        sections.append((item["name"], content_path.read_text(encoding="utf-8")))
    return sections


def normalize_state(record: dict) -> dict:
    """Instruction enable state with defaults: ``{global: bool, projects: {path: bool}}``."""
    raw = record.get("instructions_enabled") or {}
    projects = raw.get("projects") or {}
    return {
        "global": bool(raw.get("global", True)),
        "projects": {str(k): bool(v) for k, v in projects.items()},
    }


def _has_any_instructions(record: dict) -> bool:
    manifest = record.get("manifest") or {}
    return bool(_instruction_items(manifest)) or bool(normalize_state(record)["projects"])


def reconcile_blocks(record: dict) -> None:
    """Make managed instruction blocks match the record's enable state. Idempotent.

    Better Agent owns the desired extension state; Provider Config Sync owns
    making the corresponding instruction blocks exist or not exist on disk.

    Sections carrying a ``providers`` filter (see ``_sections_for_level``) only
    reach that subset's instruction files. Each configured provider is reconciled
    with its own call so filtering one provider's block set never touches another
    provider's files.
    """
    manifest = record.get("manifest") or {}
    extension_id = manifest.get("id", "")
    if not extension_id or not _has_any_instructions(record):
        return

    owner = _owner(extension_id)
    providers = _configured_providers()
    project_roots = _local_project_paths()
    enabled = bool(record.get("enabled"))

    install_path = None
    state = None
    if enabled:
        import extension_store

        install_path = extension_store.runtime_package_root_for_record(record)
        state = normalize_state(record)

    for provider in providers:
        desired: list[dict[str, Any]] = []
        if install_path is not None and state is not None:
            kind = provider.get("kind", "")
            if state["global"]:
                sections = _sections_for_level(manifest, install_path, "global", provider_kind=kind)
                if sections:
                    desired.append({"scope": "global", "project_root": None, "sections": sections})
            for project_root in project_roots:
                if not state["projects"].get(str(project_root), False):
                    continue
                sections = _sections_for_level(manifest, install_path, "project", provider_kind=kind)
                if sections:
                    desired.append({"scope": "project", "project_root": project_root, "sections": sections})

        _pcs.reconcile_managed_instruction_blocks(
            owner=owner,
            desired=desired,
            providers=[provider],
            project_roots=project_roots,
        )


def clear_all_blocks(record: dict) -> None:
    """Remove every managed block owned by the extension, everywhere. Uninstall cleanup.

    Skips extensions that never declared instructions and have no per-project
    state, so uninstalling an instruction-less extension never touches files.
    """
    manifest = record.get("manifest") or {}
    extension_id = manifest.get("id", "")
    if not extension_id or not _has_any_instructions(record):
        return
    _pcs.reconcile_managed_instruction_blocks(
        owner=_owner(extension_id),
        desired=[],
        providers=_configured_providers(),
        project_roots=_local_project_paths(),
    )


def sweep_orphan_blocks(installed_ids: set[str]) -> int:
    """Remove blocks owned by extensions no longer installed. Returns count removed."""
    installed_owners = {_owner(eid) for eid in installed_ids}
    return _pcs.sweep_orphan_managed_instruction_blocks(
        owners=installed_owners,
        providers=_configured_providers(),
        project_roots=_local_project_paths(),
    )
