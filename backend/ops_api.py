"""Ops surface: liveness/readiness probes, build info, supervisor-driven
restart (admin + control-plane), frontend log ingest, and the mobile
web-bundle manifest/download.

Not session domain logic — these routes only need process-level facts, so
the ones main.py still owns (daemon-registry readiness, cold-recovery
backlog, mobile gating, the frontend dist dir) are injected by the
composition root. See `configure`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse

import extension_daemons
import extension_jobs
import extension_store
import installation_profile
import internal_guards
import mobile_bundle_ticket
from env_compat import get_env
from i18n import t
from paths import ba_home
from restart_request import (
    new_restart_request_id,
    valid_restart_request_id,
    write_restart_request,
)
from secret_redaction import redact_secrets
from session_manager import manager as session_manager

router = APIRouter()
logger = logging.getLogger(__name__)
frontend_logger = logging.getLogger("frontend")


_coordinator_ref: Any = None
_supervised_restart_requested = False


def _coordinator() -> Any:
    if _coordinator_ref is None:
        raise HTTPException(status_code=503, detail="ops API is not configured")
    return _coordinator_ref


_extension_daemons_ready: Optional[Callable[[], bool]] = None
_cold_recovery_integration_pending: Optional[Callable[[], bool]] = None
_require_mobile_enabled: Optional[Callable[[], Awaitable[None]]] = None
frontend_dist_dir: Optional[Callable[[], Path]] = None


def configure(
    *,
    coordinator: Any,
    extension_daemons_ready: Callable[[], bool],
    cold_recovery_integration_pending: Callable[[], bool],
    require_mobile_enabled: Callable[[], Awaitable[None]],
    frontend_dist: Callable[[], Path],
) -> None:
    """Bind the process-level facts this router reports on."""
    global _coordinator_ref, _extension_daemons_ready
    global _cold_recovery_integration_pending, _require_mobile_enabled, frontend_dist_dir
    _coordinator_ref = coordinator
    _extension_daemons_ready = extension_daemons_ready
    _cold_recovery_integration_pending = cold_recovery_integration_pending
    _require_mobile_enabled = require_mobile_enabled
    frontend_dist_dir = frontend_dist


# Resolved once at import time — stable for the process lifetime.
_GIT_HASH: str | None = None
try:
    _GIT_HASH = subprocess.check_output(
        ["git", "rev-parse", "--short", "HEAD"], stderr=subprocess.DEVNULL
    ).decode().strip()
except Exception:
    pass


@router.get("/healthz")


async def healthz():
    """Liveness probe used by the frontend to poll for backend availability
    after triggering /api/admin/restart. Intentionally tiny — no I/O, no
    session/provider touches — so it answers the moment the event loop is
    up after a self-restart.
    """
    return {"ok": True}


@router.get("/readyz")


async def readyz():
    if not _extension_daemons_ready():
        raise HTTPException(
            status_code=503,
            detail="Extension daemon registry is not ready",
        )
    if extension_jobs.has_active_jobs() or not extension_daemons.ui_only_quiescent():
        if not installation_profile.integrations_enabled():
            raise HTTPException(status_code=503, detail="UI-only cleanup is incomplete")
    return {"ok": True}


@router.get("/api/build-info")


async def build_info():
    """Returns backend version and the latest supervised refresh result."""
    def _read_refresh_result() -> dict | None:
        result_path = ba_home() / "refresh_result.json"
        try:
            return json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    refresh_result = await asyncio.to_thread(_read_refresh_result)
    return {"git_hash": _GIT_HASH, "refresh_result": refresh_result}


def _valid_refresh_request_id(request_id: str) -> bool:
    return valid_restart_request_id(request_id)


def _refresh_acceptance_path() -> Path:
    return ba_home() / "refresh_request_accepted.json"


def _read_refresh_result_for(request_id: str) -> dict | None:
    result_path = ba_home() / "refresh_result.json"
    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if result.get("request_id") != request_id:
        return None
    return result


def _read_refresh_acceptance_for(request_id: str) -> dict | None:
    try:
        accepted = json.loads(_refresh_acceptance_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if accepted.get("request_id") != request_id:
        return None
    return accepted


@router.get("/api/admin/restart-status/{request_id}")


async def admin_restart_status(request_id: str):
    if not _valid_refresh_request_id(request_id):
        raise HTTPException(status_code=400, detail="Invalid restart request id.")
    accepted = await asyncio.to_thread(_read_refresh_acceptance_for, request_id)
    result = await asyncio.to_thread(_read_refresh_result_for, request_id)
    from daemonhost import switch_control

    switch_result = await asyncio.to_thread(switch_control.request_status, request_id)
    has_switch = switch_result.get("found") is True
    status = switch_result.get("status") if has_switch else (
        result.get("status") if result else "pending"
    )
    error = switch_result.get("error") if has_switch else (
        str(result.get("error") or "") if result else ""
    )
    return {
        "request_id": request_id,
        "accepted": has_switch or accepted is not None or result is not None,
        "status": status,
        "error": error,
        "refresh_result": result,
    }


_FRONTEND_LOG_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warn": logging.WARNING,
    "error": logging.ERROR,
}
_FRONTEND_LOG_MAX = 16384


def _clip(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return value[:limit]


@router.post("/api/logs/frontend")


async def frontend_log(request: Request):
    try:
        body = await request.json()
    except Exception:
        return {"ok": True, "dropped": True}
    if not isinstance(body, dict):
        return {"ok": True, "dropped": True}

    level = body.get("level")
    log_level = _FRONTEND_LOG_LEVELS.get(level if isinstance(level, str) else "", logging.ERROR)
    source = _clip(body.get("source"), 128) or "unknown"
    message = redact_secrets(_clip(body.get("message"), _FRONTEND_LOG_MAX))
    stack = redact_secrets(_clip(body.get("stack"), _FRONTEND_LOG_MAX))
    url = redact_secrets(_clip(body.get("url"), 2048))

    line = f"[{source}] {message}"
    if url:
        line += f" | url={url}"
    if stack:
        line += f"\n{stack}"
    frontend_logger.log(log_level, line)
    return {"ok": True}


@router.get("/api/mobile/bundle/manifest")


async def mobile_bundle_manifest():
    """Current web-bundle version for the Capacitor OTA updater. Gated by
    the normal auth middleware (the JS caller sends the bearer header)."""
    await _require_mobile_enabled()
    import mobile_bundle
    info = await asyncio.to_thread(mobile_bundle.build_bundle, frontend_dist_dir())
    if not info:
        raise HTTPException(status_code=503, detail="web bundle unavailable")
    return {
        "version": info["version"],
        "checksum": info["checksum"],
        "download_path": (
            "/api/mobile/bundle/download?ticket="
            + mobile_bundle_ticket.create_ticket(info["version"], info["checksum"])
        ),
    }


@router.get("/api/mobile/bundle/download")


async def mobile_bundle_download(ticket: str = Query(default="")):
    """Serve the current web bundle as a zip for the Capacitor updater.

    Public-listed because the native GET cannot send Authorization. Access is
    limited by a short-lived capability bound to the exact bundle bytes."""
    await _require_mobile_enabled()
    import mobile_bundle
    info = await asyncio.to_thread(mobile_bundle.build_bundle, frontend_dist_dir())
    if not info:
        raise HTTPException(status_code=503, detail="web bundle unavailable")
    if not ticket or not mobile_bundle_ticket.verify_ticket(
        ticket, info["version"], info["checksum"],
    ):
        raise HTTPException(status_code=401, detail="invalid bundle ticket")
    return FileResponse(
        info["path"],
        media_type="application/zip",
        filename=f"{info['version']}.zip",
    )


@router.post("/api/admin/restart")


async def admin_restart(body: dict | None = None):
    """Ask the run.sh supervisor to rebuild the frontend and restart.

    The request id is persisted in the restart flag. run.sh starts the new
    backend and waits until it is healthy, then builds the frontend atomically
    and records success/failure for the reloaded UI.

    Runner processes survive: SIGTERM (not SIGINT) leaves the
    `_intentional_shutdown` flag false, so `on_shutdown` skips
    `provider.cancel_all` and run_recovery re-attaches the still-alive
    runners on the next boot.
    """
    if get_env("BETTER_CLAUDE_RUN_SH_SUPERVISOR") != "1":
        raise HTTPException(
            status_code=409,
            detail="In-app refresh requires the run.sh supervisor.",
        )

    raw_request_id = (body or {}).get("request_id")
    request_id = str(raw_request_id) if raw_request_id is not None else ""
    if not _valid_refresh_request_id(request_id):
        request_id = new_restart_request_id()

    mode = str((body or {}).get("mode") or "now")
    if mode not in {"now", "idle"}:
        raise HTTPException(status_code=400, detail="Invalid restart mode.")

    if mode == "idle":
        await _wait_for_all_agents_idle()

    restarted_nodes = await _restart_connected_worker_nodes()
    await _trigger_supervisor_restart(request_id)
    return {
        "status": "rebuilding",
        "request_id": request_id,
        "restarted_nodes": restarted_nodes,
    }


@router.post("/api/internal/switch-restart")


async def internal_switch_restart(
    body: dict | None = None,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    """Restart trigger for control-plane extensions (line switching).

    Same supervisor contract as /api/admin/restart, but authenticated with an
    extension internal token. Fail closed: only an active extension that was
    consented as a supervisor-daemon owner may restart the backend."""
    if not internal_guards.authority_is_valid():
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    caller = internal_guards.internal_authority_extension_id() or ""
    if not caller or not extension_store.is_extension_active(caller):
        raise HTTPException(status_code=403, detail="calling extension is not active")
    manifest = (extension_store.get_extension(caller) or {}).get("manifest") or {}
    if (manifest.get("permissions") or {}).get("daemons") != "supervisor":
        raise HTTPException(status_code=403, detail="extension lacks supervisor daemon consent")
    if get_env("BETTER_CLAUDE_RUN_SH_SUPERVISOR") != "1":
        raise HTTPException(
            status_code=409,
            detail="Line switching requires the run.sh supervisor.",
        )
    raw_request_id = (body or {}).get("request_id")
    request_id = str(raw_request_id) if raw_request_id is not None else ""
    if not _valid_refresh_request_id(request_id):
        request_id = new_restart_request_id()
    restarted_nodes = await _restart_connected_worker_nodes()
    await _trigger_supervisor_restart(request_id)
    return {"status": "rebuilding", "request_id": request_id, "restarted_nodes": restarted_nodes}


async def request_supervised_backend_restart() -> bool:
    global _supervised_restart_requested
    if get_env("BETTER_CLAUDE_RUN_SH_SUPERVISOR") != "1":
        return False
    if _supervised_restart_requested:
        return True
    _supervised_restart_requested = True
    try:
        await _trigger_supervisor_restart(new_restart_request_id())
    except Exception:
        _supervised_restart_requested = False
        raise
    return True


async def _trigger_supervisor_restart(request_id: str) -> None:
    """Write the restart flag and SIGTERM uvicorn so the run.sh supervisor
    rebuilds the frontend and restarts the backend. Caller is responsible
    for the supervisor-env guard (`BETTER_CLAUDE_RUN_SH_SUPERVISOR=1`) —
    restarting without the supervisor would just kill the server with
    nothing to respawn it.

    Runner processes survive: SIGTERM (not SIGINT) leaves
    `_intentional_shutdown` false, so `on_shutdown` skips
    `provider.cancel_all` and run_recovery re-attaches the still-alive
    runners on the next boot."""
    accepted_payload = {
        "request_id": request_id,
        "accepted_at": datetime.now().astimezone().isoformat(),
    }
    await asyncio.to_thread(
        _refresh_acceptance_path().write_text,
        json.dumps(accepted_payload),
        "utf-8",
    )

    flag = ba_home() / "restart_requested"
    await asyncio.to_thread(write_restart_request, flag, request_id)
    pid = os.getpid()

    async def _restart():
        # Give uvicorn time to flush the JSON response before terminating.
        await asyncio.sleep(0.3)
        os.kill(pid, signal.SIGTERM)

    asyncio.create_task(_restart())


async def _wait_for_all_agents_idle() -> None:
    while True:
        await asyncio.to_thread(_coordinator().turn_manager._refresh_cache)
        if not _has_restart_blocking_agent_work():
            return
        await asyncio.sleep(1.0)


def _has_restart_blocking_agent_work() -> bool:
    if session_manager.has_any_queued_prompts():
        return True
    if extension_jobs.has_active_jobs():
        return True
    if _cold_recovery_integration_pending():
        return True

    active_sids = set(_coordinator().turn_manager.active_run_ids.keys())
    active_sids.update(getattr(_coordinator(), "_in_flight_prompts", {}).keys())
    active_sids.update(getattr(_coordinator(), "_prompt_queues", {}).keys())
    active_sids.update(_coordinator().turn_manager._run_state.keys())
    return any(_coordinator().turn_manager.has_active_runs(sid) for sid in active_sids)


async def _restart_connected_worker_nodes() -> list[str]:
    import node_link
    import node_store

    restarted: list[str] = []
    for node in node_store.snapshot():
        if node.get("role") != "worker_node" or node.get("state") != "connected":
            continue
        node_id = node.get("id")
        if not isinstance(node_id, str) or not node_id:
            continue
        try:
            await node_link.send_restart(node_id)
        except node_link.NodeOffline:
            continue
        restarted.append(node_id)
    return restarted
