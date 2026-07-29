from __future__ import annotations

from pathlib import Path
from typing import Any

from json_store import read_json, write_json
from paths import ba_home


SCHEMA_VERSION = 2
_TURN_STATES = {"idle", "running", "complete", "stopped", "legacy_reconciling"}
_PROMPT_STATES = {"requested", "queued", "sent", "received"}
_ADMISSION_STATES = {
    "registered",
    "starting",
    "admitted",
    "spawned",
    "deferred",
    "cancelled",
    "failed",
    "cancellation_requested",
}
_STEER_STATES = {"requested", "accepted", "persisted", "failed"}


def _migrate_v1_to_v2(value: dict[str, Any]) -> dict[str, Any]:
    sessions = value.get("sessions")
    if not isinstance(sessions, dict):
        raise RuntimeError("invalid lifecycle state projection")
    migrated_sessions: dict[str, Any] = {}
    for session_id, raw_session in sessions.items():
        if not isinstance(session_id, str) or not isinstance(raw_session, dict):
            raise RuntimeError("invalid lifecycle state session")
        session = dict(raw_session)
        raw_turn = session.get("turn")
        turn = dict(raw_turn) if isinstance(raw_turn, dict) else {}
        if turn.get("state") == "running":
            turn["state"] = "legacy_reconciling"
        turn.setdefault("user_turn_id", None)
        turn.setdefault("lifecycle_message_id", None)
        turn.setdefault("execution_turn_id", None)
        turn.setdefault("assistant_message_id", None)
        turn.setdefault("executions", {})
        turn.setdefault("admissions", {})
        turn.setdefault("steers", {})
        admissions = turn.get("admissions")
        if isinstance(admissions, dict):
            migrated_admissions: dict[str, Any] = {}
            for run_id, raw_admission in admissions.items():
                if not isinstance(run_id, str) or not isinstance(raw_admission, dict):
                    raise RuntimeError("invalid lifecycle admission")
                admission = dict(raw_admission)
                admission["execution_turn_id"] = admission.pop(
                    "turn_run_id",
                    None,
                )
                admission.setdefault("user_turn_id", None)
                admission.setdefault("lifecycle_message_id", None)
                admission.setdefault("assistant_message_id", None)
                migrated_admissions[run_id] = admission
            turn["admissions"] = migrated_admissions
        session["turn"] = turn
        migrated_sessions[session_id] = session
    return {"version": 2, "sessions": migrated_sessions}


_MIGRATIONS = {
    1: _migrate_v1_to_v2,
}


def _optional_id(value: Any) -> bool:
    return value is None or isinstance(value, str) and bool(value)


def _validate_state_map(value: Any, allowed_states: set[str], name: str) -> None:
    if not isinstance(value, dict):
        raise RuntimeError(f"invalid lifecycle {name} map")
    for item_id, item in value.items():
        if not isinstance(item_id, str) or not isinstance(item, dict):
            raise RuntimeError(f"invalid lifecycle {name}")
        if item.get("state") not in allowed_states:
            raise RuntimeError(f"invalid lifecycle {name} state")


def _validate_v2(value: dict[str, Any]) -> None:
    sessions = value.get("sessions")
    if not isinstance(sessions, dict):
        raise RuntimeError("invalid lifecycle state projection")
    for session_id, session in sessions.items():
        if not isinstance(session_id, str) or not isinstance(session, dict):
            raise RuntimeError("invalid lifecycle state session")
        _validate_state_map(
            session.get("prompts", {}),
            _PROMPT_STATES,
            "prompt",
        )
        turn = session.get("turn")
        if not isinstance(turn, dict) or turn.get("state") not in _TURN_STATES:
            raise RuntimeError("invalid lifecycle turn")
        for key in (
            "user_turn_id",
            "lifecycle_message_id",
            "execution_turn_id",
            "assistant_message_id",
        ):
            if not _optional_id(turn.get(key)):
                raise RuntimeError(f"invalid lifecycle turn {key}")
        turn_ids = (
            turn.get("user_turn_id"),
            turn.get("lifecycle_message_id"),
            turn.get("execution_turn_id"),
            turn.get("assistant_message_id"),
        )
        if turn["state"] == "running" and not all(turn_ids):
            raise RuntimeError("running lifecycle turn requires complete identity")
        if turn["state"] == "legacy_reconciling" and any(turn_ids):
            raise RuntimeError("legacy lifecycle turn cannot carry identity")
        executions = turn.get("executions")
        _validate_state_map(executions, {"running", "complete", "stopped"}, "execution")
        for execution_id, execution in executions.items():
            if not _optional_id(execution.get("assistant_message_id")):
                raise RuntimeError("invalid lifecycle execution assistant identity")
            if execution_id != turn.get("execution_turn_id") and (
                execution.get("state") == "running"
            ):
                raise RuntimeError("inactive lifecycle execution cannot be running")
        admissions = turn.get("admissions")
        _validate_state_map(admissions, _ADMISSION_STATES, "admission")
        for admission in admissions.values():
            for key in (
                "execution_turn_id",
                "user_turn_id",
                "lifecycle_message_id",
                "assistant_message_id",
            ):
                if not _optional_id(admission.get(key)):
                    raise RuntimeError(f"invalid lifecycle admission {key}")
            if turn["state"] != "legacy_reconciling" and any(
                admission.get(key) != turn.get(key)
                for key in (
                    "execution_turn_id",
                    "user_turn_id",
                    "lifecycle_message_id",
                    "assistant_message_id",
                )
            ):
                raise RuntimeError("lifecycle admission parent mismatch")
        _validate_state_map(turn.get("steers"), _STEER_STATES, "steer")


def _path() -> Path:
    return ba_home() / "lifecycle-state.json"


def load() -> dict[str, Any]:
    value = read_json(_path(), {})
    if not value:
        return {"version": SCHEMA_VERSION, "sessions": {}}
    version = value.get("version")
    if not isinstance(version, int):
        raise RuntimeError("invalid lifecycle state schema")
    migrated = False
    while version < SCHEMA_VERSION:
        migrate = _MIGRATIONS.get(version)
        if migrate is None:
            raise RuntimeError("unsupported lifecycle state schema")
        value = migrate(value)
        version = value.get("version")
        migrated = True
    if version != SCHEMA_VERSION:
        raise RuntimeError("unsupported lifecycle state schema")
    sessions = value.get("sessions")
    if not isinstance(sessions, dict):
        raise RuntimeError("invalid lifecycle state projection")
    _validate_v2(value)
    if migrated:
        write_json(_path(), value)
    return value


def save(projection: dict[str, Any]) -> None:
    if projection.get("version") != SCHEMA_VERSION:
        raise RuntimeError("invalid lifecycle state schema")
    _validate_v2(projection)
    write_json(_path(), projection)


def merge_sessions(changes: dict[str, dict[str, Any] | None]) -> None:
    projection = load()
    sessions = dict(projection["sessions"])
    for session_id, session in changes.items():
        if session is None:
            sessions.pop(session_id, None)
        else:
            sessions[session_id] = session
    save({"version": SCHEMA_VERSION, "sessions": sessions})
