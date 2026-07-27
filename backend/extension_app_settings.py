"""App Settings sections contributed by extensions.

An extension declares ``entrypoints.settings_sections`` and binds settings to
one through their ``section``. Those settings hold one app-wide value (never a
per-harness-profile overlay) and render as their own section of the app
Settings page. Values live in the extension settings store, so reads reuse
``extension_store.get_extension_settings`` and writes reuse the existing
``PATCH /api/extensions/{id}/settings`` path — this module is a read
projection only.
"""
from __future__ import annotations

from typing import Any

import extension_store


def _section_items(record: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    manifest = record.get("manifest") or {}
    extension_id = str(manifest.get("id") or "")
    entrypoints = manifest.get("entrypoints") or {}
    declared = {
        str(section.get("id")): section
        for section in entrypoints.get("settings_sections") or []
        if isinstance(section, dict) and section.get("id")
    }
    if not declared or not extension_id:
        return {}
    settings = extension_store.get_extension_settings(extension_id)
    values = settings.get("values") or {}
    by_section: dict[str, list[dict[str, Any]]] = {}
    for spec in settings.get("schema") or []:
        section_id = str(spec.get("section") or "")
        if section_id not in declared:
            continue
        key = str(spec["key"])
        by_section.setdefault(section_id, []).append({
            "extension_id": extension_id,
            "extension_name": str(manifest.get("name") or extension_id),
            "key": key,
            "label": spec.get("label") or key,
            "type": spec.get("type"),
            "enum": list(spec.get("enum") or []),
            "help": spec.get("help") or "",
            "default": spec.get("default"),
            "value": values.get(key),
        })
    return by_section


def sections() -> list[dict[str, Any]]:
    """Every active extension's app Settings sections, with current values.

    Sections with the same id across extensions merge into one page section
    (first declaration wins the label) so related extensions can share a
    section such as notifications.
    """
    merged: dict[str, dict[str, Any]] = {}
    for record in extension_store._active_records():
        manifest = record.get("manifest") or {}
        declared = (manifest.get("entrypoints") or {}).get("settings_sections") or []
        items_by_section = _section_items(record)
        for section in declared:
            if not isinstance(section, dict):
                continue
            section_id = str(section.get("id") or "")
            items = items_by_section.get(section_id) or []
            if not items:
                continue
            entry = merged.setdefault(
                section_id,
                {
                    "id": section_id,
                    "label": str(section.get("label") or section_id),
                    "description": str(section.get("description") or ""),
                    "items": [],
                },
            )
            entry["items"].extend(items)
    return [merged[key] for key in sorted(merged)]
