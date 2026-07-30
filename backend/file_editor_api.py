"""File-editing mode: the dedicated review session for a file, its
comments, and its threaded discussions."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Body, Header, HTTPException

import config_store
import file_editor
import internal_guards
import session_detail_api
import working_mode
from i18n import t
from provider_validation import (
    api_reasoning_effort as _api_reasoning_effort,
    provider_reasoning_effort as _provider_reasoning_effort,
    provider_runner as _provider_runner,
)
from session_helpers import session_lite as _session_lite

router = APIRouter()
logger = logging.getLogger(__name__)


_coordinator_ref: Any = None


def _coordinator() -> Any:
    if _coordinator_ref is None:
        raise HTTPException(status_code=503, detail="file editor API is not configured")
    return _coordinator_ref


def configure(*, coordinator: Any) -> None:
    """Bind the coordinator this router submits prompts through."""
    global _coordinator_ref
    _coordinator_ref = coordinator


@router.post("/api/file-editor")


async def start_file_editor(body: dict = Body(default={})):
    """Start (or join) the file-editing session for a project cwd and
    ensure the file is in its set.

    Body: { file_path: str, cwd: str, model?: str }
    """
    file_path = body.get("file_path")
    if not file_path:
        raise HTTPException(status_code=400, detail=t("error.file_path_required"))
    try:
        runner = _provider_runner(body.get("provider_id"), body.get("runner"))
        model = body.get("model") or await asyncio.to_thread(config_store.default_session_model)
        reasoning_effort = await asyncio.to_thread(
            _provider_reasoning_effort,
            body.get("provider_id"),
            _api_reasoning_effort(body.get("reasoning_effort")),
            runner,
            model,
        )
        result = await file_editor.start(
            file_path,
            cwd=body.get("cwd", ""),
            model=model,
            provider_id=body.get("provider_id"),
            runner=runner,
            reasoning_effort=reasoning_effort,
            node_id=body.get("node_id") or "primary",
        )
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    editor_session = await _session_lite(result["session_id"]) or {}
    await asyncio.to_thread(config_store.apply_env_vars)

    if result.get("meta_prompt") is not None:
        await _coordinator().submit_prompt_async(result["session_id"], {
            "prompt": result["meta_prompt"],
            "app_session_id": result["session_id"],
            "model": editor_session.get("model"),
            "cwd": editor_session.get("cwd"),
            "ws_callback": None,
        })

    return {
        "session_id": result["session_id"],
        "file_paths": result["file_paths"],
        "original_contents": result["original_contents"],
        "session": editor_session,
        "resumed": bool(result.get("resumed", False)),
    }


@router.post("/api/file-editor/{session_id}/comment")


async def add_file_editor_comment(session_id: str, body: dict):
    """Send a file-anchored comment to the file-editor session."""
    if not file_editor.is_file_editor_session(session_id):
        raise HTTPException(status_code=404, detail=t("error.not_file_editor_session"))
    body = body or {}
    try:
        file_path = body["file_path"]
        start_line = int(body["start_line"])
        end_line = int(body["end_line"])
        start_col = int(body["start_col"])
        end_col = int(body["end_col"])
        comment = body["comment"]
    except (KeyError, TypeError, ValueError) as e:
        raise HTTPException(status_code=400, detail=t("error.missing_invalid_field", e=str(e)))

    message = working_mode.format_file_comment(
        file_path, start_line, end_line, start_col, end_col, comment,
    )
    editor_session = await _session_lite(session_id) or {}
    await asyncio.to_thread(config_store.apply_env_vars)
    await _coordinator().submit_prompt_async(session_id, {
        "prompt": message,
        "app_session_id": session_id,
        "model": editor_session.get("model"),
        "cwd": editor_session.get("cwd"),
        "ws_callback": None,
        "client_id": body.get("client_id"),
    })
    return {"submitted": True}


@router.post("/api/file-editor/{session_id}/discussions")


async def start_file_editor_discussion(session_id: str, body: dict):
    if not file_editor.is_file_editor_session(session_id):
        raise HTTPException(status_code=404, detail=t("error.not_file_editor_session"))
    try:
        discussion = file_editor.start_discussion(
            session_id,
            file_path=str(body.get("file_path") or "").strip(),
            line=int(body.get("line")),
            title=str(body.get("title") or ""),
            opened_by="user",
            client_id=body.get("client_id"),
        )
    except (TypeError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"discussion": discussion}


@router.post("/api/internal/file-editor/start-discussion")
async def internal_start_file_discussion(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    """Agent-opened counterpart of `start_file_editor_discussion`: same
    `file_editor.start_discussion`, `opened_by="agent"`, and errors returned
    in-band so the MCP tool gets a clean tool_result."""
    if not internal_guards.authority_is_valid():
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    app_session_id = str(body.get("app_session_id") or "").strip()
    if not file_editor.is_file_editor_session(app_session_id):
        return {"success": False, "error": t("error.not_file_editor_session")}
    try:
        discussion = file_editor.start_discussion(
            app_session_id,
            file_path=str(body.get("file_path") or "").strip(),
            line=int(body.get("line")),
            title=str(body.get("title") or ""),
            opened_by="agent",
        )
    except (TypeError, ValueError) as e:
        return {"success": False, "error": str(e)}
    return {"success": True, "discussion": discussion}


@router.patch("/api/file-editor/{session_id}/discussions/{discussion_id}")


async def patch_file_editor_discussion(session_id: str, discussion_id: str, body: dict):
    if not file_editor.is_file_editor_session(session_id):
        raise HTTPException(status_code=404, detail=t("error.not_file_editor_session"))
    try:
        discussion = file_editor.patch_discussion(
            session_id,
            discussion_id,
            body or {},
            client_id=(body or {}).get("client_id"),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"discussion": discussion}


@router.post("/api/file-editor/{session_id}/discussions/{discussion_id}/messages")


async def send_file_editor_discussion_message(session_id: str, discussion_id: str, body: dict):
    if not file_editor.is_file_editor_session(session_id):
        raise HTTPException(status_code=404, detail=t("error.not_file_editor_session"))
    prompt = str((body or {}).get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail=t("error.prompt_required"))
    try:
        discussion = file_editor.get_discussion(session_id, discussion_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    editor_session = await _session_lite(session_id) or {}
    await asyncio.to_thread(config_store.apply_env_vars)
    client_id = (body or {}).get("client_id")
    await _coordinator().submit_prompt_async(session_id, {
        "prompt": prompt,
        "cli_prompt": file_editor.format_discussion_prompt(discussion, prompt),
        "app_session_id": session_id,
        "model": editor_session.get("model"),
        "cwd": editor_session.get("cwd"),
        "ws_callback": None,
        "client_id": client_id,
        "file_discussion_id": discussion_id,
    })
    return {"submitted": True, "client_id": client_id}


@router.delete("/api/file-editor/{session_id}")


async def cleanup_file_editor(session_id: str):
    """Tear down a file-editor session."""
    if not file_editor.is_file_editor_session(session_id):
        raise HTTPException(status_code=404, detail=t("error.not_file_editor_session"))
    await _coordinator().cancel_session(session_id)
    ok = file_editor.cleanup(session_id)
    await session_detail_api._publish_worker_fanout_required(
        session_id,
        op_label="file-editor cleanup",
        caller_scope=True,
        remove_worker=True,
        outer_log_msg="worker fan-out failed during file-editor cleanup",
    )
    return {"deleted": ok}
