"""Declarative applied-config (tag render rules) for installed extensions.

An extension's ``entrypoints.applied_config.tag_rules`` declare tags the
agent wraps around user-visible prose (e.g. ``NEEDS_USER_DECISION``). The
core strips the wrapper from rendered text, optionally styles the inner
text, and may set a per-session attention marker that auto-reverts on view.

This module is a stateless translator off each record's ``enabled`` flag.
The authoritative state lives in ``extensions.json`` (owned by
:mod:`extension_store`); the live registry inside :mod:`file_ref_resolver`
and the per-session markers in :mod:`session_manager` are disposable
projections, rebuilt here on every enable/disable/uninstall and on startup.
"""
from __future__ import annotations

from typing import Any


def _tag_rules_from_record(record: dict) -> list[dict]:
    """Flat ``file_ref_resolver`` rule dicts for one record. Empty when the
    record is disabled. Stamps ``_extension_id`` for marker purge attribution."""
    if not record.get("enabled"):
        return []
    manifest = record.get("manifest") or {}
    extension_id = manifest.get("id", "")
    if not extension_id:
        return []
    applied = (manifest.get("entrypoints") or {}).get("applied_config") or {}
    rules: list[dict] = []
    for rule in applied.get("tag_rules") or []:
        if not isinstance(rule, dict):
            continue
        flat: dict[str, Any] = {
            "tag": rule["tag"],
            "strip_wrapper": bool(rule.get("strip_wrapper", True)),
            "_extension_id": extension_id,
        }
        if rule.get("bold"):
            flat["bold"] = True
        if rule.get("font_scale"):
            flat["font_scale"] = rule["font_scale"]
        if rule.get("highlight"):
            flat["highlight"] = rule["highlight"]
        if rule.get("marker"):
            flat["marker"] = rule["marker"]
        if rule.get("clear_on"):
            flat["clear_on"] = rule["clear_on"]
        rules.append(flat)
    return rules


def _all_enabled_records() -> list[dict]:
    import extension_store

    data = extension_store._load()
    return list(data["extensions"].values())


def _rebuild_registry() -> None:
    """Rebuild the global tag-rule registry from every enabled record."""
    import file_ref_resolver

    merged: list[dict] = []
    for record in _all_enabled_records():
        merged.extend(_tag_rules_from_record(record))
    file_ref_resolver.set_tag_rules(merged)


def reconcile(record: dict) -> None:
    """Rebuild the global registry, then purge markers if this record is now
    disabled (its tags no longer style or mark anything)."""
    _rebuild_registry()
    if not record.get("enabled"):
        manifest = record.get("manifest") or {}
        extension_id = manifest.get("id", "")
        if extension_id:
            from session_manager import manager

            manager.clear_markers_for_extension(extension_id)


def reconcile_all() -> None:
    """Rebuild the global registry from all enabled records. Startup entry."""
    _rebuild_registry()


def clear_for_uninstall(record: dict) -> None:
    """Rebuild the registry (record already removed) and purge its markers."""
    _rebuild_registry()
    manifest = record.get("manifest") or {}
    extension_id = manifest.get("id", "")
    if extension_id:
        from session_manager import manager

        manager.clear_markers_for_extension(extension_id)


def tag_watch_rules() -> dict[str, dict]:
    """Map ``tag -> {extension_id, marker, clear_on}`` for every enabled rule
    that declares a marker. Used by the turn-complete watcher."""
    out: dict[str, dict] = {}
    for record in _all_enabled_records():
        for rule in _tag_rules_from_record(record):
            marker = rule.get("marker")
            if not marker:
                continue
            out[rule["tag"]] = {
                "extension_id": rule["_extension_id"],
                "marker": marker,
                "clear_on": rule.get("clear_on"),
            }
    return out
