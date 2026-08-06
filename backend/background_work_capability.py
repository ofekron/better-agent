"""Extension-facing surface for the background work registry.

Extensions never name their own owner: the id is taken from the resolved
capability caller, so an extension cannot report, update, finish or dismiss
another owner's work no matter what it sends. Everything else is validated by
the registry's own funnel.

The call budget is scoped to this capability rather than installed at the
shared `_invoke` chokepoint, so a misbehaving background-work caller cannot
throttle unrelated capabilities.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any, Optional

from fastapi import HTTPException
from pydantic import BaseModel, Field

import background_work
from background_work import BackgroundWorkRejected, background_work_registry

# One extension reporting more than this in a minute is either looping or
# hostile; either way the user's corner is not the place to find out.
_BUDGET_CALLS = 60
_BUDGET_WINDOW_S = 60.0

_budget_lock = threading.Lock()
_budget: dict[str, deque] = {}


def _charge_budget(extension_id: str) -> None:
    now = time.monotonic()
    with _budget_lock:
        calls = _budget.setdefault(extension_id, deque())
        while calls and now - calls[0] > _BUDGET_WINDOW_S:
            calls.popleft()
        if len(calls) >= _BUDGET_CALLS:
            raise HTTPException(
                status_code=429,
                detail="background-work call budget exceeded",
            )
        calls.append(now)


def forget_extension(extension_id: str) -> None:
    with _budget_lock:
        _budget.pop(extension_id, None)


class ReportPayload(BaseModel):
    local_id: str = Field(min_length=1, max_length=128)
    label: str = Field(min_length=1)
    detail: Optional[str] = None
    phase: Optional[str] = None
    progress: Optional[dict] = None
    session_id: Optional[str] = None
    dismissible: bool = True


class UpdatePayload(BaseModel):
    local_id: str = Field(min_length=1, max_length=128)
    label: Optional[str] = None
    detail: Optional[str] = None
    phase: Optional[str] = None
    progress: Optional[dict] = None


class FinishPayload(BaseModel):
    local_id: str = Field(min_length=1, max_length=128)
    status: str = background_work.STATUS_SUCCEEDED
    error: Optional[str] = None


class DismissPayload(BaseModel):
    local_id: str = Field(min_length=1, max_length=128)


def _item_id(extension_id: str, local_id: str) -> str:
    return f"{background_work.OWNER_EXTENSION}:{extension_id}:{local_id}"


def register_background_work(register, caller_var) -> None:
    """`caller_var` is the contextvar holding the resolved extension id for
    the in-flight capability call — the single source of owner identity."""

    def _caller() -> str:
        extension_id = caller_var.get()
        if not extension_id:
            raise HTTPException(status_code=403, detail="unresolved capability caller")
        _charge_budget(extension_id)
        return extension_id

    async def report(payload: BaseModel) -> Any:
        extension_id = _caller()
        body: ReportPayload = payload  # type: ignore[assignment]
        try:
            item_id = background_work_registry.report(
                owner_kind=background_work.OWNER_EXTENSION,
                owner_id=extension_id,
                local_id=body.local_id,
                label=body.label,
                detail=body.detail,
                phase=body.phase,
                progress=body.progress,
                session_id=body.session_id,
                dismissible=body.dismissible,
            )
        except BackgroundWorkRejected as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"id": item_id}

    async def update(payload: BaseModel) -> Any:
        extension_id = _caller()
        body: UpdatePayload = payload  # type: ignore[assignment]
        try:
            applied = background_work_registry.update(
                _item_id(extension_id, body.local_id),
                label=body.label,
                detail=body.detail,
                phase=body.phase,
                progress=body.progress,
            )
        except BackgroundWorkRejected as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"applied": applied}

    async def finish(payload: BaseModel) -> Any:
        extension_id = _caller()
        body: FinishPayload = payload  # type: ignore[assignment]
        try:
            applied = background_work_registry.finish(
                _item_id(extension_id, body.local_id),
                status=body.status,
                error=body.error,
            )
        except BackgroundWorkRejected as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"applied": applied}

    async def dismiss(payload: BaseModel) -> Any:
        extension_id = _caller()
        body: DismissPayload = payload  # type: ignore[assignment]
        return {
            "applied": background_work_registry.dismiss(
                _item_id(extension_id, body.local_id),
            ),
        }

    register("background-work", "report", ReportPayload, report)
    register("background-work", "update", UpdatePayload, update)
    register("background-work", "finish", FinishPayload, finish)
    register("background-work", "dismiss", DismissPayload, dismiss)
