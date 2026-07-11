"""Disk-backed status records for long-running operations.

One JSON file per operation id under `<ba_home>/<dirname>/`. This is the
single implementation behind `ask_status_store` ("ask-status", keyed by
`ask_id`) and `delegation_status_store` ("delegate-status", keyed by
`delegation_id`/`client_delegation_id`). Records hold the correlation ids
a client needs to reattach after a runtime restart and, once the
operation resolves, the terminal `result` payload.

`operation_status` is the typed poll contract (plan Phase 1): clients
query a durable operation by (kind, id) instead of only re-issuing the
same blocking call. Unknown kinds and empty/unsafe ids fail closed.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from paths import ba_home
from runs_dir import atomic_write_json


class OperationStatusStore:
    def __init__(self, dirname: str) -> None:
        self._dirname = dirname

    @staticmethod
    def _safe_id(op_id: str) -> str:
        return "".join(ch for ch in op_id if ch.isalnum() or ch in ("-", "_"))

    def status_path(self, op_id: str) -> Path:
        return ba_home() / self._dirname / f"{self._safe_id(op_id)}.json"

    def write_status(self, op_id: str, **fields: Any) -> None:
        path = self.status_path(op_id)
        current = self.read_status(op_id) or {}
        current.update(fields)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, current)

    async def write_status_async(self, op_id: str, **fields: Any) -> None:
        await asyncio.to_thread(self.write_status, op_id, **fields)

    def read_status(self, op_id: str) -> dict[str, Any] | None:
        try:
            data = json.loads(self.status_path(op_id).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def delete_status(self, op_id: str) -> None:
        try:
            self.status_path(op_id).unlink()
        except FileNotFoundError:
            pass


ASK_STATUS = OperationStatusStore("ask-status")
DELEGATION_STATUS = OperationStatusStore("delegate-status")

_KINDS: dict[str, OperationStatusStore] = {
    "ask": ASK_STATUS,
    "delegation": DELEGATION_STATUS,
}


def operation_status(kind: str, operation_id: str) -> dict[str, Any]:
    """Poll a durable operation by id.

    Returns `{kind, operation_id, found, status, record}`. `status` is
    the record's own `status` field when present (delegations:
    resolving/queued/running/complete), else derived from `result`
    presence (asks write no interim status): "complete" once a result
    is stored, "in_flight" before that.
    """
    store = _KINDS.get(str(kind or ""))
    if store is None:
        raise ValueError(f"unknown operation kind: {kind!r}")
    clean_id = OperationStatusStore._safe_id(str(operation_id or ""))
    if not clean_id or clean_id != operation_id:
        raise ValueError("operation_id must be a non-empty safe id")
    record = store.read_status(clean_id)
    if record is None:
        return {
            "kind": kind,
            "operation_id": clean_id,
            "found": False,
            "status": "unknown",
            "record": None,
        }
    status = str(record.get("status") or "").strip()
    if not status:
        status = "complete" if record.get("result") is not None else "in_flight"
    return {
        "kind": kind,
        "operation_id": clean_id,
        "found": True,
        "status": status,
        "record": record,
    }
