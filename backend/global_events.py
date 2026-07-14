from __future__ import annotations

import json
import re
import threading
import uuid
from dataclasses import dataclass
from typing import Mapping

AUTHORITY_EPOCH = uuid.uuid4().hex
_authority_lock = threading.Lock()
_owner_revisions: dict[str, int] = {}


@dataclass(frozen=True)
class GlobalEventSpec:
    owner: str


_EVENT_OWNERS: Mapping[str, tuple[str, ...]] = {
    "provider": (
        "provider_changed", "provider_install_progress",
        "provider_install_finished", "models_catalog_changed",
        "provider_config_sync_changed", "internal_llm_changed",
    ),
    "project": (
        "projects_changed", "project_mappings_changed", "workers_changed",
        "worker_activity_changed",
        "tasks_changed", "project_updates_changed",
    ),
    "extension": ("extensions_changed", "extension_event"),
    "schedule_store": ("schedules_changed",),
    "user_prefs": ("user_prefs_changed", "ui_selection_changed"),
    "session_manager": (
        "session_organization_changed", "session_metadata_updated",
        "todos_snapshot", "session_created", "session_forked",
        "session_deleted", "session_renamed", "session_processing_started",
        "session_processing_finished", "session_reconciled",
        "stub_invalidated", "message_recovering_changed",
        "message_retrying_changed", "message_auto_retry_changed",
        "message_content_updated", "message_continuation_changed",
        "message_run_meta_changed", "messages_delta",
        "message_ask_result_changed", "message_ask_choice_changed",
        "user_input_requested", "user_input_resolved",
        "session_error_changed", "session_user_input_changed",
        "session_monitoring_changed",
        "session_provenance_changed", "session_unread_changed",
        "session_marker_changed",
        "render_delta",
        "resnapshot_required",
    ),
    "startup_tasks": ("startup_task_changed",),
    "machine_nodes": (
        "node_state_changed", "node_registration_requested",
        "node_registration_resolved",
    ),
    "switch_control": ("switch_control_state_changed",),
    "credential_broker": ("credential_consent_changed",),
}

GLOBAL_EVENT_SPECS = {
    event_type: GlobalEventSpec(owner=owner)
    for owner, event_types in _EVENT_OWNERS.items()
    for event_type in event_types
}
GLOBAL_EVENT_TYPES = frozenset(GLOBAL_EVENT_SPECS)


def authority_metadata(owner: str, *, advance: bool = False) -> dict[str, str | int]:
    with _authority_lock:
        revision = _owner_revisions.get(owner, 0)
        if advance:
            revision += 1
            _owner_revisions[owner] = revision
    return {"authority_epoch": AUTHORITY_EPOCH, "revision": revision}

_EXTENSION_TOKEN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")
_MAX_EXTENSION_EVENT_BYTES = 256 * 1024


def validate_global_event(event_type: str, data: dict) -> dict:
    if event_type not in GLOBAL_EVENT_SPECS:
        raise ValueError(
            f"broadcast_global called with non-registered type {event_type!r}; "
            "per-session events must use broadcast_session"
        )
    if not isinstance(data, dict):
        raise ValueError("global event data must be an object")
    try:
        serialized = json.dumps(
            data,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("global event data must be JSON-serializable") from exc
    snapshot = json.loads(serialized)
    if event_type == "extension_event":
        _validate_extension_event(snapshot)
    return snapshot


def extension_event(
    extension_id: str,
    event_name: str,
    data: dict,
) -> tuple[str, dict]:
    payload = {
        "extension_id": extension_id,
        "event_name": event_name,
        "data": data,
    }
    validate_global_event("extension_event", payload)
    return "extension_event", payload


def _validate_extension_event(payload: dict) -> None:
    if set(payload) != {"extension_id", "event_name", "data"}:
        raise ValueError("extension_event has an invalid shape")
    extension_id = payload["extension_id"]
    event_name = payload["event_name"]
    event_data = payload["data"]
    if not isinstance(extension_id, str) or not _EXTENSION_TOKEN.fullmatch(extension_id):
        raise ValueError("extension_event has an invalid extension_id")
    if not isinstance(event_name, str) or not _EXTENSION_TOKEN.fullmatch(event_name):
        raise ValueError("extension_event has an invalid event_name")
    if not isinstance(event_data, dict):
        raise ValueError("extension_event data must be an object")
    try:
        size = len(json.dumps(
            payload,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise ValueError("extension_event data must be JSON-serializable") from exc
    if size > _MAX_EXTENSION_EVENT_BYTES:
        raise ValueError("extension_event payload is too large")
