"""First-paint snapshot of the background work registry.

The manager UI renders from this alone. It carries no WS dependency by
contract, because the surface it replaces has to work while the backend is
still booting and `/ws/chat` is closed — which is why the route joins the
bootstrap admission allowlist.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Body, HTTPException

import extension_jobs
from background_work import OWNER_EXTENSION, STATUS_UNKNOWN, background_work_registry

logger = logging.getLogger("uvicorn")

router = APIRouter()


@router.get("/api/background-work")
async def get_background_work() -> dict:
    """`{epoch, seq, items}`. Clients treat this as authoritative and drop
    any `background_work_changed` delta that predates it, re-fetching
    whenever the epoch changes."""
    return background_work_registry.snapshot()


def _seeded_job_identity(item_id: str) -> tuple[str, str, str] | None:
    """Recover `(owner, operation, job_id)` from an item id shaped exactly
    like the one `seed_background_work_after_recovery` mints
    (`extension:<owner>:job:<operation>:<job_id>`), or None if the id
    doesn't match that shape.

    `item_id` itself is client-supplied (the POST body), so this shape
    check alone is NOT sufficient to prove the row came from the recovery
    seeder — an extension can freely choose its own `local_id` via the
    `background-work/report` capability, including one that happens to
    look like `job:<op>:<id>`. The caller must additionally gate on
    `status == STATUS_UNKNOWN`, which only the seeder ever assigns (every
    real extension report defaults to `STATUS_RUNNING`), before treating a
    shape match as license to mutate a durable `extension_jobs` record."""
    parts = item_id.split(":", 2)
    if len(parts) != 3 or parts[0] != OWNER_EXTENSION:
        return None
    owner, local_id = parts[1], parts[2]
    job_parts = local_id.split(":", 2)
    if len(job_parts) != 3 or job_parts[0] != "job":
        return None
    _, operation, job_id = job_parts
    return owner, operation, job_id


@router.post("/api/background-work/dismiss")
async def dismiss_background_work(body: dict = Body(...)) -> dict:
    """User-initiated dismiss for a card in the manager UI. Removes the
    live registry row and, for a row seeded from a stuck durable
    `extension_jobs` record, stamps that record `background_work_dismissed`
    so a future restart's recovery seed skips it — closing the loop that
    plain in-memory `dismiss()` leaves open."""
    item_id = body.get("id")
    if not isinstance(item_id, str) or not item_id:
        raise HTTPException(status_code=400, detail="id is required")

    item = background_work_registry.get(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="background work item not found")

    removed = background_work_registry.dismiss(item_id)
    if not removed:
        raise HTTPException(status_code=409, detail="background work item is not dismissible")

    # Only a row the recovery seeder itself created carries STATUS_UNKNOWN —
    # a live extension row (always reported STATUS_RUNNING) can never match
    # this gate even if its owner-chosen `local_id` happens to look like the
    # seeder's `job:<operation>:<job_id>` shape. Without this gate, dismissing
    # a live row could stamp `background_work_dismissed` on an unrelated,
    # genuinely-in-progress durable job record.
    if item["owner_kind"] == OWNER_EXTENSION and item["status"] == STATUS_UNKNOWN:
        identity = _seeded_job_identity(item_id)
        if identity is not None:
            owner, operation, job_id = identity
            stamped = await asyncio.to_thread(
                extension_jobs.mark_background_work_dismissed, owner, operation, job_id
            )
            if not stamped:
                logger.warning(
                    "background_work_dismiss_not_stamped owner=%s operation=%s job_id=%s",
                    owner, operation, job_id,
                )

    return {"dismissed": True}
