from __future__ import annotations

import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)

MilestoneCallback = Callable[[str, dict[str, Any]], None]

_DELEGATION_STAGE_MILESTONES = frozenset({
    "delegation_run_state_registering",
    "delegation_lock_waiting",
    "delegation_lock_acquired",
    "delegation_fork_resolving",
    "delegation_provider_resolving",
    "delegation_team_context_resolving",
    "delegation_recovery_waiting",
    "delegation_root_persist_flushing",
    "delegation_runner_starting",
})


def emit_milestone(
    callback: MilestoneCallback | None,
    name: str,
    fields: dict[str, Any] | None = None,
) -> None:
    if callback is None:
        return
    try:
        callback(name, dict(fields or {}))
    except Exception:
        logger.exception("provisioning milestone callback failed: %s", name)


def delegation_milestone(status: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    fields: dict[str, Any] = {
        key: status[key]
        for key in (
            "provider_id",
            "provider_run_id",
            "worker_pid",
            "worker_agent_session_id",
        )
        if status.get(key) is not None
    }
    native_session_id = status.get("fork_agent_sid")
    if isinstance(native_session_id, str) and native_session_id:
        fields["native_session_id"] = native_session_id
        native_path = status.get("jsonl_path")
        if isinstance(native_path, str) and native_path:
            fields["native_session_file_path"] = native_path
        return "native_session_started", fields
    worker_pid = status.get("worker_pid")
    if isinstance(worker_pid, int) and not isinstance(worker_pid, bool) and worker_pid > 0:
        return "runner_started", fields
    stage = status.get("stage")
    if isinstance(stage, str) and stage in _DELEGATION_STAGE_MILESTONES:
        return stage, fields
    phase = status.get("status")
    if phase == "queued":
        return "delegation_queued", fields
    if phase == "resolving":
        return "delegation_resolving", fields
    return None
