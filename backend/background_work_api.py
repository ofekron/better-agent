"""First-paint snapshot of the background work registry.

The manager UI renders from this alone. It carries no WS dependency by
contract, because the surface it replaces has to work while the backend is
still booting and `/ws/chat` is closed — which is why the route joins the
bootstrap admission allowlist.
"""

from __future__ import annotations

from fastapi import APIRouter

from background_work import background_work_registry

router = APIRouter()


@router.get("/api/background-work")
async def get_background_work() -> dict:
    """`{epoch, seq, items}`. Clients treat this as authoritative and drop
    any `background_work_changed` delta that predates it, re-fetching
    whenever the epoch changes."""
    return background_work_registry.snapshot()
