"""Report long-running extension work into the user's background work stack.

    with client.background_work("Indexing repo", total=120) as work:
        for n, path in enumerate(paths, 1):
            work.progress(n)
        work.phase("writing index")

The item finishes as succeeded when the block exits cleanly and as failed
with the exception message when it does not, so work cannot be left spinning
by a code path that forgot to close it.

There is no heartbeat. If the extension dies or is disabled, core finalizes
the item from that observable event rather than waiting out a deadline.

Omit `total` when the work has no countable end: the item then renders as
explicitly indeterminate instead of inventing a percentage.
"""

from __future__ import annotations

import uuid
from typing import Any, Optional

_CAPABILITY = "background-work"


class BackgroundWorkHandle:
    """Live handle to one reported item. Every mutation is a capability call,
    so the backend stays the owner of what the user sees."""

    def __init__(
        self,
        client: Any,
        local_id: str,
        *,
        total: Optional[int] = None,
        unit: str = "",
    ) -> None:
        self._client = client
        self._local_id = local_id
        self._total = total
        self._unit = unit
        self._finished = False

    @property
    def local_id(self) -> str:
        return self._local_id

    def progress(self, completed: int) -> None:
        self._update(progress={
            "completed": int(completed),
            "total": self._total,
            "unit": self._unit,
        })

    def phase(self, phase: str) -> None:
        self._update(phase=str(phase))

    def detail(self, detail: str) -> None:
        self._update(detail=str(detail))

    def label(self, label: str) -> None:
        self._update(label=str(label))

    def finish(self, *, status: str = "succeeded", error: Optional[str] = None) -> None:
        if self._finished:
            return
        self._finished = True
        self._client.invoke_capability(_CAPABILITY, "finish", {
            "local_id": self._local_id,
            "status": status,
            "error": error,
        })

    def dismiss(self) -> None:
        self._finished = True
        self._client.invoke_capability(
            _CAPABILITY, "dismiss", {"local_id": self._local_id},
        )

    def _update(self, **fields: Any) -> None:
        if self._finished:
            return
        payload: dict[str, Any] = {"local_id": self._local_id}
        payload.update({k: v for k, v in fields.items() if v is not None})
        self._client.invoke_capability(_CAPABILITY, "update", payload)

    def __enter__(self) -> "BackgroundWorkHandle":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is None:
            self.finish()
        else:
            self.finish(status="failed", error=str(exc) or exc_type.__name__)
        return False

    async def __aenter__(self) -> "BackgroundWorkHandle":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return self.__exit__(exc_type, exc, tb)


def background_work(
    client: Any,
    label: str,
    *,
    total: Optional[int] = None,
    unit: str = "",
    detail: Optional[str] = None,
    local_id: Optional[str] = None,
    session_id: Optional[str] = None,
    dismissible: bool = True,
) -> BackgroundWorkHandle:
    """Report a running item and return its handle.

    `local_id` is the identity core scopes to this extension. Pass a stable
    one to make the report idempotent across a resume; omit it for a fresh
    row per call.
    """
    resolved_id = local_id or uuid.uuid4().hex
    payload: dict[str, Any] = {
        "local_id": resolved_id,
        "label": label,
        "dismissible": dismissible,
    }
    if detail is not None:
        payload["detail"] = detail
    if session_id is not None:
        payload["session_id"] = session_id
    if total is not None:
        payload["progress"] = {"completed": 0, "total": int(total), "unit": unit}
    client.invoke_capability(_CAPABILITY, "report", payload)
    return BackgroundWorkHandle(client, resolved_id, total=total, unit=unit)
