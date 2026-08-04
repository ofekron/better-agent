from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

from filelock import FileLock

from json_store import write_json
from paths import bc_home


_SCHEMA_VERSION = 1
_STATUSES = frozenset({"pending", "synced", "failed"})
_FAILURE_CODES = frozenset({"credential_unavailable", "rpc_failed", "ack_mismatch"})
_RECORD_KEYS = frozenset({
    "node_id",
    "node_authority",
    "provider_id",
    "provider_generation",
    "provider_name",
    "operation_id",
    "status",
    "authorized_at",
    "updated_at",
    "failure_code",
    "acknowledged_execution_revision",
})
_lock = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _path() -> Path:
    return bc_home() / "node_provider_credential_grants.json"


def _file_lock() -> FileLock:
    return FileLock(str(_path()) + ".lock", timeout=60)


def _empty() -> dict:
    return {"schema_version": _SCHEMA_VERSION, "grants": []}


def _clean_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _clean_record(value: object) -> dict | None:
    if not isinstance(value, dict) or not set(value).issubset(_RECORD_KEYS):
        return None
    try:
        status = _clean_text(value.get("status"), "status")
        if status not in _STATUSES:
            return None
        record = {
            "node_id": _clean_text(value.get("node_id"), "node_id"),
            "node_authority": _clean_text(
                value.get("node_authority"),
                "node_authority",
            ),
            "provider_id": _clean_text(value.get("provider_id"), "provider_id"),
            "provider_generation": _clean_text(
                value.get("provider_generation"),
                "provider_generation",
            ),
            "provider_name": _clean_text(
                value.get("provider_name"),
                "provider_name",
            ),
            "operation_id": _clean_text(
                value.get("operation_id"),
                "operation_id",
            ),
            "status": status,
            "authorized_at": _clean_text(
                value.get("authorized_at"),
                "authorized_at",
            ),
            "updated_at": _clean_text(value.get("updated_at"), "updated_at"),
        }
    except ValueError:
        return None
    failure_code = value.get("failure_code")
    if failure_code is not None and failure_code not in _FAILURE_CODES:
        return None
    if failure_code is not None:
        record["failure_code"] = failure_code
    acknowledged_revision = value.get("acknowledged_execution_revision")
    if acknowledged_revision is not None and (
        not isinstance(acknowledged_revision, int)
        or isinstance(acknowledged_revision, bool)
        or acknowledged_revision < 0
    ):
        return None
    if acknowledged_revision is not None:
        record["acknowledged_execution_revision"] = acknowledged_revision
    if (status == "synced") != (acknowledged_revision is not None):
        return None
    if status != "failed" and failure_code is not None:
        return None
    return record


def _read() -> dict:
    path = _path()
    if not path.exists():
        return _empty()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty()
    if (
        not isinstance(raw, dict)
        or set(raw) != {"schema_version", "grants"}
        or raw.get("schema_version") != _SCHEMA_VERSION
    ):
        return _empty()
    values = raw.get("grants")
    if not isinstance(values, list):
        return _empty()
    cleaned = [record for value in values if (record := _clean_record(value))]
    return {"schema_version": _SCHEMA_VERSION, "grants": cleaned}


def _write(state: dict) -> None:
    write_json(_path(), state)


def _key(record: dict) -> tuple[str, str]:
    return record["node_id"], record["provider_id"]


def _copy(record: dict) -> dict:
    return dict(record)


def authorize(
    *,
    node_id: str,
    node_authority: str,
    provider_id: str,
    provider_generation: str,
    provider_name: str,
    operation_id: str,
) -> dict:
    now = _now()
    record = {
        "node_id": _clean_text(node_id, "node_id"),
        "node_authority": _clean_text(node_authority, "node_authority"),
        "provider_id": _clean_text(provider_id, "provider_id"),
        "provider_generation": _clean_text(
            provider_generation,
            "provider_generation",
        ),
        "provider_name": _clean_text(provider_name, "provider_name"),
        "operation_id": _clean_text(operation_id, "operation_id"),
        "status": "pending",
        "authorized_at": now,
        "updated_at": now,
    }
    with _lock, _file_lock():
        state = _read()
        prior = next(
            (item for item in state["grants"] if _key(item) == _key(record)),
            None,
        )
        if (
            prior
            and prior["node_authority"] == record["node_authority"]
            and prior["provider_generation"] == record["provider_generation"]
        ):
            record["authorized_at"] = prior["authorized_at"]
        state["grants"] = [
            item for item in state["grants"] if _key(item) != _key(record)
        ]
        state["grants"].append(record)
        _write(state)
    return _copy(record)


def mark_status(
    *,
    node_id: str,
    node_authority: str,
    provider_id: str,
    provider_generation: str,
    operation_id: str,
    status: str,
    failure_code: str = "",
    acknowledged_execution_revision: int | None = None,
) -> dict | None:
    clean_status = _clean_text(status, "status")
    if clean_status not in _STATUSES:
        raise ValueError(f"unsupported credential grant status {clean_status!r}")
    expected = (
        _clean_text(node_id, "node_id"),
        _clean_text(provider_id, "provider_id"),
    )
    clean_authority = _clean_text(node_authority, "node_authority")
    clean_generation = _clean_text(provider_generation, "provider_generation")
    clean_operation_id = _clean_text(operation_id, "operation_id")
    with _lock, _file_lock():
        state = _read()
        record = next(
            (item for item in state["grants"] if _key(item) == expected),
            None,
        )
        if (
            record is None
            or record["node_authority"] != clean_authority
            or record["provider_generation"] != clean_generation
            or record["operation_id"] != clean_operation_id
        ):
            return None
        record["status"] = clean_status
        record["updated_at"] = _now()
        record.pop("failure_code", None)
        record.pop("acknowledged_execution_revision", None)
        if clean_status == "failed" and failure_code:
            clean_failure = _clean_text(failure_code, "failure_code")
            if clean_failure not in _FAILURE_CODES:
                raise ValueError(f"unsupported failure code {clean_failure!r}")
            record["failure_code"] = clean_failure
        if clean_status == "synced":
            if (
                not isinstance(acknowledged_execution_revision, int)
                or isinstance(acknowledged_execution_revision, bool)
                or acknowledged_execution_revision < 0
            ):
                raise ValueError(
                    "synced credential grants require an acknowledged execution revision"
                )
            record["acknowledged_execution_revision"] = (
                acknowledged_execution_revision
            )
        _write(state)
        return _copy(record)


def begin_sync(
    *,
    node_id: str,
    node_authority: str,
    provider_id: str,
    provider_generation: str,
    operation_id: str,
) -> dict | None:
    expected = (
        _clean_text(node_id, "node_id"),
        _clean_text(provider_id, "provider_id"),
    )
    clean_authority = _clean_text(node_authority, "node_authority")
    clean_generation = _clean_text(provider_generation, "provider_generation")
    clean_operation_id = _clean_text(operation_id, "operation_id")
    with _lock, _file_lock():
        state = _read()
        record = next(
            (item for item in state["grants"] if _key(item) == expected),
            None,
        )
        if (
            record is None
            or record["node_authority"] != clean_authority
            or record["provider_generation"] != clean_generation
        ):
            return None
        record["operation_id"] = clean_operation_id
        record["status"] = "pending"
        record["updated_at"] = _now()
        record.pop("failure_code", None)
        record.pop("acknowledged_execution_revision", None)
        _write(state)
        return _copy(record)


def operations_are_current(records: list[dict]) -> bool:
    expected = {
        (
            _clean_text(record.get("node_id"), "node_id"),
            _clean_text(record.get("provider_id"), "provider_id"),
        ): (
            _clean_text(record.get("node_authority"), "node_authority"),
            _clean_text(record.get("provider_generation"), "provider_generation"),
            _clean_text(record.get("operation_id"), "operation_id"),
        )
        for record in records
    }
    with _lock, _file_lock():
        current = {_key(record): record for record in _read()["grants"]}
    return all(
        key in current
        and current[key]["node_authority"] == authority
        and current[key]["provider_generation"] == generation
        and current[key]["operation_id"] == operation_id
        for key, (authority, generation, operation_id) in expected.items()
    )


def valid_for_node_with_prune(
    node_id: str,
    node_authority: str,
    provider_generations: dict[str, str],
) -> tuple[list[dict], bool]:
    clean_node_id = _clean_text(node_id, "node_id")
    clean_authority = _clean_text(node_authority, "node_authority")
    with _lock, _file_lock():
        state = _read()
        before = len(state["grants"])
        state["grants"] = [
            record
            for record in state["grants"]
            if record["node_id"] != clean_node_id
            or (
                record["node_authority"] == clean_authority
                and provider_generations.get(record["provider_id"])
                == record["provider_generation"]
            )
        ]
        pruned = len(state["grants"]) != before
        if pruned:
            _write(state)
        return [
            _copy(record)
            for record in state["grants"]
            if record["node_id"] == clean_node_id
        ], pruned


def valid_for_node(
    node_id: str,
    node_authority: str,
    provider_generations: dict[str, str],
) -> list[dict]:
    records, _ = valid_for_node_with_prune(
        node_id,
        node_authority,
        provider_generations,
    )
    return records


def public_for_node(node_id: str) -> list[dict]:
    clean_node_id = _clean_text(node_id, "node_id")
    with _lock, _file_lock():
        records = [
            _copy(record)
            for record in _read()["grants"]
            if record["node_id"] == clean_node_id
        ]
    for record in records:
        record.pop("node_authority", None)
        record.pop("provider_generation", None)
        record.pop("acknowledged_execution_revision", None)
        record.pop("operation_id", None)
    return records


def remove_node(node_id: str) -> bool:
    clean_node_id = _clean_text(node_id, "node_id")
    with _lock, _file_lock():
        state = _read()
        kept = [
            record for record in state["grants"] if record["node_id"] != clean_node_id
        ]
        if len(kept) == len(state["grants"]):
            return False
        state["grants"] = kept
        _write(state)
        return True
