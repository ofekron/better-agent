"""Durable prompt handoff and the mobile offline-action batch endpoint.

A prompt admitted here survives the HTTP response: the submit task is
tracked so shutdown can drain it, and the caller only waits for the
durable admission, not the turn.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Body, HTTPException

import config_store
import extension_store
import session_detail_api
from capability_contexts import normalize_capability_contexts
from i18n import t
from session_helpers import session_lite as _session_lite
from session_manager import manager as session_manager
from user_msg_lifecycle import new_lifecycle_msg_id

router = APIRouter()
logger = logging.getLogger(__name__)


_coordinator_ref: Any = None


def _coordinator() -> Any:
    if _coordinator_ref is None:
        raise HTTPException(status_code=503, detail="offline actions API is not configured")
    return _coordinator_ref


def configure(*, coordinator: Any) -> None:
    """Bind the coordinator prompts are submitted through."""
    global _coordinator_ref
    _coordinator_ref = coordinator


_PROMPT_HANDOFF_TASKS: set[asyncio.Task] = set()
_PROMPT_HANDOFFS_OPEN = True


def open_prompt_handoffs() -> None:
    global _PROMPT_HANDOFFS_OPEN
    _PROMPT_HANDOFFS_OPEN = True


def close_prompt_handoffs() -> None:
    global _PROMPT_HANDOFFS_OPEN
    _PROMPT_HANDOFFS_OPEN = False


async def _durably_admit_and_submit_prompt(
    session_id: str,
    queued_prompt: dict,
    params: dict,
    admitted: asyncio.Future,
) -> None:
    admission = await asyncio.to_thread(
        session_manager.admit_queued_prompt_durable,
        session_id,
        queued_prompt,
    )
    admitted.set_result(admission)
    if admission.get("admitted"):
        await _coordinator().submit_prompt_async(session_id, params)


def _observe_prompt_handoff(task: asyncio.Task, admitted: asyncio.Future) -> None:
    _PROMPT_HANDOFF_TASKS.discard(task)
    if task.cancelled():
        if not admitted.done():
            admitted.cancel()
        return
    error = task.exception()
    if error is not None:
        if not admitted.done():
            admitted.set_exception(error)
        logger.error(
            "durable prompt handoff failed",
            exc_info=(type(error), error, error.__traceback__),
        )


async def _start_prompt_handoff(
    session_id: str,
    queued_prompt: dict,
    params: dict,
) -> dict:
    if not _PROMPT_HANDOFFS_OPEN:
        raise RuntimeError("prompt handoff is closed")
    admitted = asyncio.get_running_loop().create_future()
    task = asyncio.create_task(
        _durably_admit_and_submit_prompt(
            session_id,
            queued_prompt,
            params,
            admitted,
        ),
        name=f"prompt-handoff-{str(queued_prompt.get('id') or '')[:8]}",
    )
    _PROMPT_HANDOFF_TASKS.add(task)
    task.add_done_callback(lambda done: _observe_prompt_handoff(done, admitted))
    return await asyncio.shield(admitted)


def _validate_offline_batch_attachments(
    images: object,
    files: object,
) -> tuple[list[dict], list[dict]]:
    import base64
    import binascii

    normalized_images = images or []
    normalized_files = files or []
    if not isinstance(normalized_images, list) or not isinstance(normalized_files, list):
        raise HTTPException(status_code=400, detail="attachments must be arrays")
    if len(normalized_images) > 20 or len(normalized_files) > 20:
        raise HTTPException(status_code=400, detail="at most 20 images and 20 files are allowed")
    for image in normalized_images:
        if (
            not isinstance(image, dict)
            or set(image) != {"data", "media_type"}
            or not isinstance(image.get("data"), str)
            or not image["data"]
            or len(image["data"]) > 14 * 1024 * 1024
            or not isinstance(image.get("media_type"), str)
            or not image["media_type"].startswith("image/")
        ):
            raise HTTPException(status_code=400, detail="malformed image attachment")
        try:
            image_bytes = base64.b64decode(image["data"], validate=True)
        except (binascii.Error, ValueError) as exc:
            raise HTTPException(status_code=400, detail="malformed image attachment") from exc
        if len(image_bytes) > 10 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="malformed image attachment")
    for file_payload in normalized_files:
        if (
            not isinstance(file_payload, dict)
            or set(file_payload) != {"name", "data", "media_type", "size"}
            or not isinstance(file_payload.get("name"), str)
            or not file_payload["name"]
            or "/" in file_payload["name"]
            or "\\" in file_payload["name"]
            or not isinstance(file_payload.get("data"), str)
            or not isinstance(file_payload.get("media_type"), str)
            or not isinstance(file_payload.get("size"), int)
            or isinstance(file_payload.get("size"), bool)
            or file_payload["size"] < 0
            or file_payload["size"] > 10 * 1024 * 1024
            or len(file_payload["data"]) > 14 * 1024 * 1024
        ):
            raise HTTPException(status_code=400, detail="malformed file attachment")
        try:
            file_bytes = base64.b64decode(file_payload["data"], validate=True)
        except (binascii.Error, ValueError) as exc:
            raise HTTPException(status_code=400, detail="malformed file attachment") from exc
        if len(file_bytes) != file_payload["size"]:
            raise HTTPException(status_code=400, detail="malformed file attachment")
    return normalized_images, normalized_files


async def _admit_offline_batch_message(action: dict) -> dict:
    session_id = action["sessionId"]
    session = await _session_lite(session_id)
    if not session:
        raise HTTPException(status_code=404, detail=t("error.session_not_found_retry"))
    offline_error = await session_detail_api._node_offline_error(session)
    if offline_error:
        raise HTTPException(status_code=503, detail=offline_error)
    provider_id = session.get("provider_id")
    if provider_id and config_store.provider_suspended(provider_id):
        raise HTTPException(
            status_code=409,
            detail=t("error.provider_suspended", action="run turns"),
        )
    orchestration_mode = session.get("orchestration_mode") or action.get("orchestrationMode")
    if orchestration_mode == "manager":
        orchestration_mode = "team"
    if orchestration_mode == "team":
        team_not_ready = extension_store.runtime_not_ready_message(
            extension_store.extension_id_for_role("team-orchestration")
        )
        if team_not_ready is not None:
            raise HTTPException(status_code=409, detail=team_not_ready)
    if action.get("sendMode") not in (None, "queue"):
        raise HTTPException(
            status_code=409,
            detail="background replay supports only queue send mode",
        )
    if action.get("sendTarget") is not None:
        raise HTTPException(
            status_code=409,
            detail="background replay does not support targeted sends",
        )
    prompt = action["prompt"].strip()
    images, files = _validate_offline_batch_attachments(
        action.get("images"),
        action.get("files"),
    )
    if not prompt and not images and not files:
        raise HTTPException(status_code=400, detail=t("error.ws_empty_prompt"))
    try:
        capability_contexts = normalize_capability_contexts(
            action.get("capabilityContexts")
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    harness_profile_id = str(action.get("harnessProfileId") or "").strip()
    await asyncio.to_thread(config_store.apply_env_vars)
    lifecycle_msg_id = new_lifecycle_msg_id()
    queued_id = str(uuid.uuid4())
    is_queued = (
        _coordinator().turn_manager.has_active_turn(session_id)
        or _coordinator().turn_manager.has_active_runs(session_id)
        or _coordinator().has_queued_prompts(session_id)
    )
    queued_prompt = {
        "id": queued_id,
        "lifecycle_msg_id": lifecycle_msg_id,
        "content": prompt,
        "kind": "queued_behind" if is_queued else "send",
        "queue_position": _coordinator().get_queued_count(session_id),
        "images_count": len(images),
        "files_count": len(files),
        "images": images or None,
        "files": files or None,
        "orchestration_mode": orchestration_mode,
        "send_target": None,
        "cli_prompt": None,
        "disallowed_tools": None,
        "disabled_builtin_extensions": None,
        "client_id": action["clientId"],
        "alter_rewind_latest": False,
        "capability_contexts": capability_contexts,
        "harness_profile_id": harness_profile_id,
        "created_at": datetime.now().isoformat(),
    }
    params = {
        "prompt": prompt,
        "app_session_id": session_id,
        "model": session.get("model"),
        "cwd": session.get("cwd"),
        "ws_callback": None,
        "images": images or None,
        "files": files or None,
        "orchestration_mode": orchestration_mode,
        "send_target": None,
        "client_id": action["clientId"],
        "lifecycle_msg_id": lifecycle_msg_id,
        "cli_prompt": None,
        "disallowed_tools": None,
        "disabled_builtin_extensions": None,
        "capability_contexts": capability_contexts,
        "harness_profile_id": harness_profile_id,
        "_queued_id": queued_id,
    }
    admission = await _start_prompt_handoff(session_id, queued_prompt, params)
    if not admission.get("session"):
        raise HTTPException(status_code=404, detail=t("error.session_not_found_retry"))
    existing = (
        admission.get("existing_queued_prompt")
        or admission.get("existing_user_message")
        or {}
    )
    duplicate = bool(existing)
    return {
        "duplicate": duplicate,
        "enqueued": bool(admission.get("admitted")),
        "lifecycle_msg_id": existing.get("lifecycle_msg_id") or lifecycle_msg_id,
    }


async def _admit_offline_batch_create(action: dict) -> dict:
    queued = action["session"]
    session_id = queued["id"]
    _validate_offline_batch_attachments(action.get("images"), action.get("files"))
    try:
        normalize_capability_contexts(action.get("capabilityContexts"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    existed = await asyncio.to_thread(session_manager.get_lite, session_id)
    session = await session_detail_api.create_session({
        "name": queued.get("name", ""),
        "model": queued.get("model"),
        "cwd": queued.get("cwd", ""),
        "orchestration_mode": queued.get("orchestration_mode"),
        "provider_id": queued.get("provider_id"),
        "node_id": queued.get("node_id"),
        "reasoning_effort": queued.get("reasoning_effort"),
        "runner": queued.get("runner"),
        "permission": queued.get("permission"),
        "client_session_id": session_id,
        "capability_contexts": (
            action.get("capabilityContexts")
            if action.get("capabilityContexts") is not None
            else queued.get("capability_contexts")
        ),
        "harness_profile_id": (
            action.get("harnessProfileId")
            or queued.get("harness_profile_id")
        ),
        "folder_id": queued.get("folder_id"),
    })
    result = {"duplicate": existed is not None, "created": existed is None}
    if action["prompt"].strip() or action.get("images") or action.get("files"):
        send_result = await _admit_offline_batch_message({
            "type": "send_message",
            "sessionId": session["id"],
            "clientId": action["clientId"],
            "prompt": action["prompt"],
            "images": action.get("images"),
            "files": action.get("files"),
            "capabilityContexts": action.get("capabilityContexts"),
            "harnessProfileId": action.get("harnessProfileId"),
            "sendMode": "queue",
            "sendTarget": None,
        })
        result.update(send_result)
        result["duplicate"] = result["duplicate"] or send_result["duplicate"]
    return result


@router.post("/api/offline-actions/batch")


async def ingest_mobile_offline_actions(body: Any = Body(...)):
    import offline_action_batch

    return await offline_action_batch.process_batch(
        body,
        create_action=_admit_offline_batch_create,
        send_action=_admit_offline_batch_message,
    )


async def _drain_prompt_handoffs() -> None:
    tasks = tuple(_PROMPT_HANDOFF_TASKS)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
