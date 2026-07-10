"""Better Agent adapter for the standalone provider-config-sync backend."""

from __future__ import annotations

import logging
import os
import json
import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query

import config_store
import extension_store
import project_store
from paths import ba_home, encode_cwd

from provider_config_sync_backend import api as _standalone_api

from provider_config_sync_backend.api import *  # noqa: F403
from provider_config_sync_backend.api import configure

logger = logging.getLogger(__name__)


def _require_provider_config_sync_runtime() -> None:
    not_ready = extension_store.runtime_not_ready_message(
        extension_store.BUILTIN_PROVIDER_CONFIG_SYNC_EXTENSION_ID
    )
    if not_ready is not None:
        raise HTTPException(status_code=404, detail=not_ready)


router = APIRouter(
    prefix="/api/internal/provider-config-sync",
    tags=["provider-config-sync"],
    dependencies=[Depends(_require_provider_config_sync_runtime)],
)

_SECRET_FIELD_RE = re.compile(r"(?i)(api[_-]?key|token|secret|password|authorization)(\s*[:=]\s*)([^\s,;}]+)")
def _redact_llm_text(value: str) -> str:
    return _SECRET_FIELD_RE.sub(r"\1\2[redacted]", value)


def _redact_llm_payload(value):
    if isinstance(value, str):
        return _redact_llm_text(value)
    if isinstance(value, list):
        return [_redact_llm_payload(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact_llm_payload(item) for key, item in value.items()}
    return value


def _json_from_llm_text(text: str) -> dict:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    data = json.loads(stripped)
    if not isinstance(data, dict):
        raise ValueError("LLM review response must be a JSON object")
    return data


def _review_provider_config_sync_hunks_with_zai(context: dict) -> list[str]:
    import extension_package_loader

    extension_package_loader.ensure_package_importable(
        extension_store.BUILTIN_REQUIREMENTS_EXTENSION_ID,
        "requirement_analysis",
    )
    from requirement_analysis.llm import complete

    payload = _redact_llm_payload(context)
    system = (
        "You review deterministic provider config sync hunks. "
        "Return only JSON shaped as {\"approve_hunk_ids\":[\"...\"]}. "
        "Approve only hunks that should be applied from source to target. "
        "Do not invent hunk ids."
    )
    user = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    review_model = config_store.resolve_internal_llm("provider_config_sync_review")["model"]
    data = _json_from_llm_text(complete(system, user, model=review_model))
    approved = data.get("approve_hunk_ids")
    if not isinstance(approved, list) or not all(isinstance(item, str) for item in approved):
        raise ValueError("LLM review response approve_hunk_ids must be a string list")
    return approved


async def _broadcast_better_agent_changed(
    scope: str,
    category: str,
    capability_id: str,
    path: str,
    cwd: str,
) -> None:
    from orchestrator import get_active_coordinator

    coordinator = get_active_coordinator()
    if coordinator is None:
        logger.warning("provider_config_sync_changed not broadcast: no coordinator yet")
        return
    await coordinator.broadcast_global(
        "provider_config_sync_changed",
        {"scope": scope, "category": category, "capability_id": capability_id, "path": path, "cwd": cwd},
    )


configure(
    provider_records=lambda: config_store.list_provider_metadata(),
    project_records=lambda: project_store.list_projects(),
    sync_home=ba_home,
    encode_project_cwd=encode_cwd,
    broadcast_changed=_broadcast_better_agent_changed,
    llm_review=_review_provider_config_sync_hunks_with_zai,
)


def better_agent_config_path() -> Path:
    return ba_home() / "provider-config-sync" / "better-agent-config.json"


def write_better_agent_config() -> Path:
    path = better_agent_config_path()
    providers = config_store.list_provider_metadata()
    projects = [
        {
            "path": project["path"],
            "node_id": project.get("node_id") or "primary",
            "name": project.get("name") or Path(project["path"]).name,
            "git_remote": project.get("git_remote") or "",
        }
        for project in project_store.list_projects()
        if (project.get("node_id") or "primary") == "primary" and project.get("path")
    ]
    payload = {
        "sync_home": str(ba_home()),
        "providers": providers,
        "projects": projects,
    }
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or path.read_text(encoding="utf-8") != text:
        path.write_text(text, encoding="utf-8")
    os.environ["PROVIDER_CONFIG_SYNC_CONFIG"] = str(path)
    return path


write_better_agent_config()


def provider_config_sync_mcp_env(*, backend_url: str, internal_token: str) -> dict[str, str]:
    write_better_agent_config()
    return {
        "PROVIDER_CONFIG_SYNC_CONFIG": str(better_agent_config_path()),
        "PROVIDER_CONFIG_SYNC_CHANGE_WEBHOOK_URL": (
            backend_url.rstrip("/") + "/api/internal/capabilities/invoke"
        ),
        "PROVIDER_CONFIG_SYNC_BROADCAST_TOKEN": internal_token,
        "PROVIDER_CONFIG_SYNC_CHANGE_WEBHOOK_CAPABILITY": "provider-config-sync",
        "PROVIDER_CONFIG_SYNC_CHANGE_WEBHOOK_ACTION": "change.broadcast",
    }


def _discover(cwd: str) -> dict:
    write_better_agent_config()
    return _standalone_api._discover(cwd)


def _capability_picker_sources(cwd: str = "") -> list[dict]:
    write_better_agent_config()
    return _standalone_api._capability_picker_sources(cwd)


_capability_for_tool = _standalone_api._capability_for_tool
_local_project_root = _standalone_api._local_project_root


@router.get("")
async def get_provider_config_sync(cwd: str = Query("", description="Project cwd for project-scope native files")):
    return _discover(cwd)


@router.get("/capability-picker")
async def get_provider_config_sync_capability_picker(cwd: str = Query("", description="Project cwd for project-scope capabilities")):
    return {"sources": _capability_picker_sources(cwd)}


@router.get("/settings")
async def get_provider_config_sync_settings(
    cwd: str = Query("", description="Project cwd for project-scope overrides"),
    capability_id: str = Query("", description="Capability id for effective policy"),
):
    write_better_agent_config()
    return _standalone_api.get_auto_sync_settings(cwd, capability_id)


@router.patch("/settings")
async def patch_provider_config_sync_settings(req: _standalone_api.AutoSyncSettingsPatch):
    write_better_agent_config()
    return _standalone_api.update_auto_sync_settings(req)


@router.get("/repository")
async def get_provider_config_sync_repository_status():
    write_better_agent_config()
    return await _standalone_api.get_repository_status_route()


@router.post("/repository/init")
async def init_provider_config_sync_repository(req: _standalone_api.RepositoryConfigRequest):
    write_better_agent_config()
    return await _standalone_api.init_repository_route(req)


@router.post("/repository/load")
async def load_provider_config_sync_repository(req: _standalone_api.RepositoryConfigRequest):
    write_better_agent_config()
    return await _standalone_api.load_repository_route(req)


@router.post("/repository/sync")
async def sync_provider_config_sync_repository():
    write_better_agent_config()
    return await _standalone_api.sync_repository_route()


@router.put("/file")
async def write_native_file_route(req: WriteNativeFileRequest):  # noqa: F405
    return await write_native_file(req)  # noqa: F405


@router.post("/file/restore")
async def restore_native_file_route(req: RestoreNativeFileRequest):  # noqa: F405
    return await restore_native_file(req)  # noqa: F405


@router.delete("/capability")
async def delete_capability_route(req: DeleteCapabilityRequest):  # noqa: F405
    return await delete_capability(req)  # noqa: F405


@router.post("/capability")
async def create_capability_route(req: CreateCapabilityRequest):  # noqa: F405
    return await create_capability(req)  # noqa: F405


@router.post("/capability/transfer")
async def transfer_capability_route(req: TransferCapabilityRequest):  # noqa: F405
    return await transfer_capability(req)  # noqa: F405


@router.post("/apply")
async def apply_native_file_route(req: ApplyNativeFileRequest):  # noqa: F405
    return await apply_native_file(req)  # noqa: F405


@router.post("/auto-sync")
async def auto_sync_route(req: AutoSyncRequest):  # noqa: F405
    return await auto_sync(req)  # noqa: F405


@router.post("/unified-capability-item")
async def upsert_unified_capability_item_route(req: UpsertUnifiedCapabilityItemRequest):  # noqa: F405
    return await upsert_unified_capability_item(req)  # noqa: F405


@router.delete("/unified-capability-item")
async def remove_unified_capability_item_route(req: RemoveUnifiedCapabilityItemRequest):  # noqa: F405
    return await remove_unified_capability_item(req)  # noqa: F405
