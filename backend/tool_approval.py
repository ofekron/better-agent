"""In-process registry for interactive tool/command approvals.

A runner subprocess (Claude `can_use_tool` callback or a Codex app-server
approval ServerRequest) needs a human decision mid-turn. It POSTs a request
to the backend (`/api/internal/tool-approvals/request`); that handler creates
a record here, broadcasts a WS event to the session, and AWAITS the paired
Future. The frontend's decision POST resolves the Future, unblocking the
runner's request, which returns the verdict to the CLI.

Fail-closed: if no decision arrives within APPROVAL_TIMEOUT_S the awaited
request resolves `approved=False` so the CLI denies the action rather than
hanging forever. State is intentionally in-memory/ephemeral — a backend
crash mid-approval lets the runner's HTTP call fail, which the runner treats
as a denial (never silent auto-approve)."""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

APPROVAL_TIMEOUT_S = 5 * 60


@dataclass
class ToolApproval:
    approval_id: str
    app_session_id: str
    run_id: str
    provider_kind: str
    tool_name: str
    summary: dict
    created_at: float
    future: "asyncio.Future[bool]" = field(default=None)


class ToolApprovalRegistry:
    def __init__(self) -> None:
        self._pending: dict[str, ToolApproval] = {}
        self._lock = asyncio.Lock()

    def create(
        self,
        *,
        app_session_id: str,
        run_id: str,
        provider_kind: str,
        tool_name: str,
        summary: dict,
    ) -> ToolApproval:
        loop = asyncio.get_running_loop()
        aid = uuid.uuid4().hex
        rec = ToolApproval(
            approval_id=aid,
            app_session_id=app_session_id,
            run_id=run_id,
            provider_kind=provider_kind,
            tool_name=tool_name,
            summary=summary,
            created_at=time.monotonic(),
            future=loop.create_future(),
        )
        self._pending[aid] = rec
        return rec

    async def await_decision(self, rec: ToolApproval) -> bool:
        """Block until a decision arrives or the fail-closed timeout fires.
        Always cleans up the record. Returns False on timeout/error — never
        raises into the caller (the HTTP handler must always return a verdict)."""
        try:
            return await asyncio.wait_for(rec.future, timeout=APPROVAL_TIMEOUT_S)
        except asyncio.TimeoutError:
            return False
        except Exception:
            return False
        finally:
            self._pending.pop(rec.approval_id, None)

    def decide(self, approval_id: str, approved: bool) -> bool:
        rec = self._pending.get(approval_id)
        if rec is None or rec.future is None or rec.future.done():
            return False
        try:
            rec.future.set_result(bool(approved))
        except asyncio.InvalidStateError:
            return False
        return True

    def get(self, approval_id: str) -> Optional[ToolApproval]:
        return self._pending.get(approval_id)

    def list_for_session(self, app_session_id: str) -> list[ToolApproval]:
        return [r for r in self._pending.values() if r.app_session_id == app_session_id]

    def public_view(self, rec: ToolApproval) -> dict:
        return {
            "approval_id": rec.approval_id,
            "app_session_id": rec.app_session_id,
            "run_id": rec.run_id,
            "provider_kind": rec.provider_kind,
            "tool_name": rec.tool_name,
            "summary": rec.summary,
        }


registry = ToolApprovalRegistry()
