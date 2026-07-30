"""Harness profile CRUD, field/override writes, and the per-session
harness-profile selector.

Depends on the coordinator only through the broadcast capability it
actually needs, bound by the composition root (see `configure`).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Optional

from fastapi import APIRouter, Body, HTTPException

import config_store
import extension_api
import extension_store
import harness_field_writer
import harness_fields
import harness_profile_resolver
import harness_profile_store
from i18n import t
from provider_validation import api_disabled_builtin_extensions
from session_manager import manager as session_manager

router = APIRouter()
logger = logging.getLogger(__name__)

_broadcast_global: Optional[Callable[[str, dict], Any]] = None


def configure(broadcast_global: Callable[[str, dict], Any]) -> None:
    """Bind the coordinator capability this router needs."""
    global _broadcast_global
    _broadcast_global = broadcast_global


def _require_configured() -> Callable[[str, dict], Any]:
    if _broadcast_global is None:
        raise HTTPException(status_code=503, detail="harness profiles API is not configured")
    return _broadcast_global


def harness_profile_selection(body: dict) -> str:
    """Validate a request's harness profile selection and return the id.

    A session names a profile, not a revision of it: later edits to the
    profile apply to every session already on it. Blank means the default
    profile."""
    profile_id = str((body or {}).get("harness_profile_id") or "").strip()
    if not profile_id:
        return ""
    try:
        profile = harness_profile_store.get_profile(profile_id)
    except harness_profile_store.HarnessProfileError:
        # "default" is a synthesized profile, never a stored one — same
        # 404 an unknown stored id would get.
        profile = None
    if not profile:
        raise HTTPException(status_code=404, detail="harness profile not found")
    try:
        harness_profile_resolver.resolve_for_session(
            {"harness_profile_id": profile_id},
            profile_id=profile_id,
        )
    except harness_profile_resolver.HarnessProfileResolutionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return profile_id


async def _broadcast_harness_profiles_changed(payload: dict[str, Any] | None = None) -> None:
    harness_profile_resolver.invalidate_cache()
    try:
        await _require_configured()("harness_profiles_changed", payload or {})
    except Exception:
        logger.exception("failed to broadcast harness profile change")
    try:
        import node_config_sync

        node_config_sync.notify_changed("harness")
    except Exception:
        logger.exception("node harness sync notify failed")


_PROFILE_INSTANCE_FIELD_KEYS = (
    "mcp_servers", "skills", "instruction_names", "setting_overlays", "headless",
)


def _resolved_override_response(entry: dict[str, Any]) -> dict[str, Any]:
    """API-facing {resolved, override} shape for a resolver {resolved, override} entry."""
    return {"resolved": entry["resolved"], "override": entry["override"]}


def _profile_instance_fields_response(fields: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in _PROFILE_INSTANCE_FIELD_KEYS:
        entry = fields.get(key)
        if entry is None:
            continue
        if key == "setting_overlays":
            out[key] = {
                overlay_key: _resolved_override_response(overlay)
                for overlay_key, overlay in (entry or {}).items()
            }
        else:
            out[key] = _resolved_override_response(entry)
    return out


def _profile_response_from_resolved(resolved: dict[str, Any]) -> dict[str, Any]:
    fields = {
        "extension_instances": {
            extension_id: _profile_instance_fields_response(instance_fields)
            for extension_id, instance_fields in (resolved.get("extension_instances") or {}).items()
        },
        "disabled_builtin_tools": _resolved_override_response(resolved["disabled_builtin_tools"]),
        "disabled_builtin_extensions": _resolved_override_response(resolved["disabled_builtin_extensions"]),
        "instruction_sources": {
            name: _resolved_override_response(entry)
            for name, entry in (resolved.get("instruction_sources") or {}).items()
        },
    }
    return {
        "id": resolved["id"],
        "name": resolved.get("name"),
        "description": resolved.get("description"),
        "revision": resolved.get("revision"),
        "created_at": resolved.get("created_at"),
        "updated_at": resolved.get("updated_at"),
        "fields": fields,
        "mcp_overrides": resolved.get("mcp_overrides") or {},
        "skill_overrides": resolved.get("skill_overrides") or {},
        "native_harness_overrides": resolved.get("native_harness_overrides") or {},
        "provider_run_config_overlay": resolved.get("provider_run_config_overlay") or {},
    }


def _with_profile_meta(response: dict[str, Any], stored: dict[str, Any] | None) -> dict[str, Any]:
    """Attach the profile's OWN base pointer + pins (what THIS profile sets,
    for the editor). The Default profile has no stored record, so all null."""
    stored = stored or {}
    response["base_profile_id"] = stored.get("base_profile_id")
    response["base_profile_revision"] = stored.get("base_profile_revision")
    response["default_provider_id"] = stored.get("default_provider_id")
    response["default_model"] = stored.get("default_model")
    response["default_reasoning_effort"] = stored.get("default_reasoning_effort")
    response["provisioning_prompt"] = stored.get("provisioning_prompt")
    response["read_only"] = bool(stored.get("read_only"))
    response["source"] = stored.get("source") or ""
    response["extension_id"] = stored.get("extension_id") or ""
    return response


def _profile_response(profile: dict[str, Any], default: dict[str, Any] | None = None) -> dict[str, Any]:
    resolved = harness_profile_resolver.resolve_profile(profile["id"], profile.get("revision"), default=default)
    return _with_profile_meta(_profile_response_from_resolved(resolved), profile)


def _profile_summary_response(profile: dict[str, Any] | None = None) -> dict[str, Any]:
    if profile is None:
        return {
            "id": "default",
            "name": "Default",
            "description": "The live harness state before any profile override is applied.",
            "revision": "",
            "base_profile_id": None,
            "base_profile_revision": None,
            "default_provider_id": None,
            "default_model": None,
            "default_reasoning_effort": None,
            "provisioning_prompt": None,
            "read_only": False,
            "source": "",
            "extension_id": "",
        }
    return _with_profile_meta(
        {
            "id": profile["id"],
            "name": profile["name"],
            "description": profile.get("description"),
            "revision": profile["revision"],
        },
        profile,
    )


def _default_profile_response(default: dict[str, Any] | None = None) -> dict[str, Any]:
    resolved = harness_profile_resolver.resolve_profile(harness_profile_store.DEFAULT_PROFILE_ID, default=default)
    response = _profile_response_from_resolved(resolved)
    response["id"] = "default"
    response["name"] = "Default"
    response["description"] = "The live harness state before any profile override is applied."
    return _with_profile_meta(response, None)


@router.get("/api/harness-profiles")
async def list_harness_profiles():
    stored = await asyncio.to_thread(harness_profile_store.list_profiles)
    return {"profiles": [_profile_summary_response(), *[_profile_summary_response(profile) for profile in stored]]}


@router.get("/api/harness-profiles/descriptor")
async def get_harness_profile_descriptor():
    """What is configurable, independent of the selected profile. The UI
    renders one control tree from this for Default and every named profile;
    per-profile values come from GET /api/harness-profiles/{id}."""
    try:
        return await asyncio.to_thread(harness_fields.descriptor)
    except harness_fields.HarnessFieldError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _profile_field_write_response(
    profile_id: str, revision: str | None, writes: list[dict[str, Any]]
) -> dict[str, Any]:
    stored = harness_field_writer.apply_field_writes(profile_id, revision, writes)
    # Default is a live projection of extension/config state, so it is
    # re-read after a write-through rather than reported from the stale view.
    if stored is None:
        return _default_profile_response()
    return _profile_response(stored)


def _field_writes_change_global(profile_id: str, writes: list[dict[str, Any]]) -> bool:
    if profile_id == harness_profile_store.DEFAULT_PROFILE_ID:
        return True
    for write in writes:
        path = [str(part) for part in (write.get("path") or [])]
        if path and path[0] == harness_fields.GROUP_PROFILE_META:
            continue
        if harness_fields.scope_for(path) == harness_fields.SCOPE_GLOBAL:
            return True
    return False


@router.patch("/api/harness-profiles/{profile_id}/fields")
async def patch_harness_profile_fields(profile_id: str, body: dict = Body(default={})):
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="request body must be an object")
    writes = body.get("writes")
    if not isinstance(writes, list) or not writes:
        raise HTTPException(status_code=400, detail="writes must be a non-empty list")
    for write in writes:
        if not isinstance(write, dict) or not isinstance(write.get("path"), list) or not write["path"]:
            raise HTTPException(status_code=400, detail="each write needs a non-empty path")
    try:
        changes_global_state = _field_writes_change_global(profile_id, writes)
        response = await asyncio.to_thread(
            _profile_field_write_response, profile_id, body.get("revision") or None, writes
        )
    except (
        harness_profile_store.HarnessProfileNotFoundError,
        harness_profile_resolver.HarnessProfileMissingError,
    ) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (harness_fields.HarnessFieldError, harness_profile_store.HarnessProfileError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except extension_store.ExtensionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except harness_profile_resolver.HarnessProfileResolutionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if changes_global_state:
        await extension_api._broadcast_extension_changed("extension.config", "extension.harness.default")
    await _broadcast_harness_profiles_changed({
        "action": "fields_updated",
        "profile_id": response.get("id"),
        "revision": response.get("revision"),
    })
    return response


@router.get("/api/harness-profiles/{profile_id}")
async def get_harness_profile(profile_id: str):
    if profile_id == harness_profile_store.DEFAULT_PROFILE_ID:
        return await asyncio.to_thread(_default_profile_response)
    try:
        profile = await asyncio.to_thread(harness_profile_store.get_profile, profile_id)
    except harness_profile_store.HarnessProfileError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not profile:
        raise HTTPException(status_code=404, detail="harness profile not found")
    return await asyncio.to_thread(_profile_response, profile)


@router.post("/api/harness-profiles")
async def create_harness_profile(body: dict = Body(default={})):
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="request body must be an object")
    try:
        profile = await asyncio.to_thread(harness_profile_store.create_profile, body)
    except harness_profile_store.HarnessProfileError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    response = await asyncio.to_thread(_profile_response, profile)
    await _broadcast_harness_profiles_changed({
        "action": "created",
        "profile_id": response.get("id"),
        "revision": response.get("revision"),
    })
    return response


@router.patch("/api/harness-profiles/{profile_id}/overrides")
async def patch_harness_profile_overrides(profile_id: str, body: dict = Body(default={})):
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="request body must be an object")
    ops = body.get("ops")
    if not isinstance(ops, list):
        raise HTTPException(status_code=400, detail="ops must be a list")
    try:
        profile = await asyncio.to_thread(
            harness_profile_store.apply_override_patch,
            profile_id,
            ops,
            body.get("revision") or None,
        )
    except harness_profile_store.HarnessProfileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except harness_profile_store.HarnessProfileError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    response = await asyncio.to_thread(_profile_response, profile)
    await _broadcast_harness_profiles_changed({
        "action": "overrides_updated",
        "profile_id": response.get("id"),
        "revision": response.get("revision"),
    })
    return response


@router.delete("/api/harness-profiles/{profile_id}")
async def delete_harness_profile(profile_id: str, revision: str = ""):
    try:
        deleted = await asyncio.to_thread(
            harness_profile_store.delete_profile,
            profile_id,
            revision or None,
        )
    except harness_profile_store.HarnessProfileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except harness_profile_store.HarnessProfileError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="harness profile not found")
    await _broadcast_harness_profiles_changed({
        "action": "deleted",
        "profile_id": profile_id,
        "revision": "",
    })
    return {"success": True}


@router.patch("/api/sessions/{session_id}/harness-profile")
async def update_session_harness_profile(session_id: str, body: dict = Body(default={})):
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="request body must be an object")
    profile_id = harness_profile_selection(body)
    session = await asyncio.to_thread(
        session_manager.set_harness_profile,
        session_id,
        profile_id,
    )
    if not session:
        raise HTTPException(status_code=404, detail=t("error.session_not_found_retry"))
    return {
        "id": session_id,
        "harness_profile_id": session.get("harness_profile_id") or "",
    }


# ── Global harness defaults ──────────────────────────────────


@router.get("/api/harness/default/disabled-builtin-tools")
async def get_global_disabled_builtin_tools():
    return {"disabled_builtin_tools": await asyncio.to_thread(config_store.get_disabled_builtin_tools)}


@router.put("/api/harness/default/disabled-builtin-tools")
async def set_global_disabled_builtin_tools(body: dict = Body(default={})):
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="request body must be an object")
    tools = body.get("disabled_builtin_tools")
    if not isinstance(tools, list):
        raise HTTPException(status_code=400, detail="disabled_builtin_tools must be a list")
    updated = await asyncio.to_thread(config_store.set_disabled_builtin_tools, tools)
    harness_profile_resolver.invalidate_cache()
    await extension_api._broadcast_extension_changed("extension.harness.default.disabled_builtin_tools")
    return {"disabled_builtin_tools": updated}


@router.get("/api/harness/default/disabled-builtin-extensions")
async def get_global_disabled_builtin_extensions():
    return {"disabled_builtin_extensions": await asyncio.to_thread(config_store.get_disabled_builtin_extensions)}


@router.put("/api/harness/default/disabled-builtin-extensions")
async def set_global_disabled_builtin_extensions(body: dict = Body(default={})):
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="request body must be an object")
    extension_ids = api_disabled_builtin_extensions(body.get("disabled_builtin_extensions"))
    updated = await asyncio.to_thread(config_store.set_disabled_builtin_extensions, extension_ids)
    harness_profile_resolver.invalidate_cache()
    await extension_api._broadcast_extension_changed(
        "extension.catalog",
        "extension.harness.default.disabled_builtin_extensions",
    )
    return {"disabled_builtin_extensions": updated}
