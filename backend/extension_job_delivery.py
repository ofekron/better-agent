"""Fallback delivery for durable core-MCP jobs whose original synchronous
caller went away before it could read the result.

Mirrors `ask_delivery.py`'s caller-terminal handling, retargeted at the
`extension_jobs` primitive: `session-bridge-delegate` (the `delegate_to_session`
MCP tool) IS the whole synchronous "join a target turn and return its output"
operation — unlike `delegate-task`/`mssg`, which dispatch and return quickly,
with the real deliverable already reaching the caller through a separate
durable channel (`ask_delivery.complete()` in `orchs/manager/_delegation.py`).

`_maybe_run_core_mcp_job` (main.py) runs the actual work under
`asyncio.shield`, so a disconnected/cancelled HTTP request doesn't kill it —
it keeps running to completion, durably recorded via `extension_jobs`. But
if the caller's own turn independently reaches `lifecycle.turn_complete`/
`turn_stopped` while that shielded job is still running, nothing was ever
polling the job again and nothing pushed the result anywhere — it would sit
in the completed job record forever, silently unread. This module closes
that gap by delivering it to the caller's inbox once both the job is known
complete and the caller is known gone, the same way `ask_delivery` already
does for plain `ask`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import extension_jobs
import inbox_store

logger = logging.getLogger(__name__)

_OWNER = "core-mcp"
# Operations whose job IS the caller's only channel to the result — as
# opposed to delegate-task/mssg, whose real output already reaches the
# caller through ask_delivery.complete() regardless of whether this HTTP
# job is ever read again. Including those here would double-deliver.
_FALLBACK_DELIVERY_OPERATIONS = ("session-bridge-delegate",)


def _caller_session_id(record: dict[str, Any]) -> str:
    payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
    return str(payload.get("app_session_id") or "").strip()


def _fallback_message(result: dict[str, Any]) -> str:
    text = str(result.get("final_message") or "").strip()
    if text:
        return text
    error = str(result.get("error") or "").strip()
    if error:
        return error
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"))


async def _deliver_one(operation: str, record: dict[str, Any]) -> None:
    job_id = str(record.get("id") or "")
    if not job_id:
        return
    caller_session_id = _caller_session_id(record)
    if not caller_session_id:
        return
    status = record.get("status")
    if status == "failed":
        # A hard failure (task cancelled, or an exception raised before
        # session_bridge.delegate() ever returned a result dict) has no
        # `result` payload — response_from_record's "result" key is None
        # for this status. Build the fallback message from the job's own
        # `error` field directly instead.
        message = str(record.get("error") or "job failed").strip()
        sender_session_id = caller_session_id
    else:
        response = extension_jobs.response_from_record(record)
        result = response.get("result") if isinstance(response, dict) else None
        if not isinstance(result, dict):
            return
        message = _fallback_message(result)
        sender_session_id = str(result.get("session_id") or caller_session_id)
    await asyncio.to_thread(
        inbox_store.send,
        sender_session_id=sender_session_id,
        recipient_session_id=caller_session_id,
        message=message,
        delivery_id=f"extjob:{_OWNER}:{operation}:{job_id}",
    )
    await asyncio.to_thread(extension_jobs.mark_delivered, _OWNER, operation, job_id)


async def on_caller_terminal(event: Any) -> None:
    payload = event.payload if isinstance(event.payload, dict) else {}
    if payload.get("reason") == "worker_inner":
        return
    caller_session_id = str(event.sid or "").strip()
    if not caller_session_id:
        return
    for operation in _FALLBACK_DELIVERY_OPERATIONS:
        records = await asyncio.to_thread(extension_jobs.list_records, _OWNER, operation)
        for record in records:
            if record.get("status") not in ("complete", "failed") or record.get("delivered"):
                continue
            if _caller_session_id(record) != caller_session_id:
                continue
            try:
                await _deliver_one(operation, record)
            except Exception:
                logger.exception(
                    "extension_job_delivery fallback failed operation=%s job_id=%s",
                    operation, record.get("id"),
                )
