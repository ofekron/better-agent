"""Manager-mode rewind helpers.

Walks the worker panels of a manager assistant message and rewinds
each worker's claude jsonl back to the anchor uuid that turn used,
so a `rewind_files` on the manager session also undoes everything
its workers did during that turn.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from stores import worker_store
from session_manager import manager as session_manager

if TYPE_CHECKING:
    from orchestrator import Coordinator

logger = logging.getLogger(__name__)


def _safe_delete_forks(fbsids: list[str], log_fmt: str) -> None:
    """Delete each fork Better Agent session, logging per-failure without raising.
    `log_fmt` must include exactly one `%s` slot for `fbsid` so each
    call site keeps its diagnostic phrasing.
    INVARIANT: never raises — fork cleanup must not block the caller's
    happy path.
    """
    for fbsid in fbsids:
        try:
            session_manager.delete(fbsid)
        except Exception:
            logger.exception(log_fmt, fbsid)


def first_user_uuid_in_panel(panel: dict) -> Optional[str]:
    """Walk a worker panel's events looking for the first
    `agent_message` of `data.type == "user"`. Its uuid is the
    delegation's anchor in the worker's claude jsonl — the point we
    rewind to so everything that worker did during this turn is
    undone.
    """
    for ev in panel.get("events") or []:
        if ev.get("type") != "agent_message":
            continue
        data = ev.get("data") or {}
        if data.get("type") == "user" and data.get("uuid"):
            return data["uuid"]
    return None


async def rewind_workers_for_turn(
    coordinator: "Coordinator",
    app_session_id: str,
    assistant_msg: dict,
    target_user_msg: dict,
) -> dict:
    """Rewind every worker that participated in `assistant_msg`.

    Workers born during this turn (worker_store record's
    `created_at >= target_user_msg.timestamp`) are deleted entirely;
    pre-existing workers get `claude --resume <fork_sid>
    --rewind-files <uuid>` so their file edits + jsonl tail are
    undone. Forks pointing at any rewound worker are cleared so the
    next delegation re-forks fresh.

    Returns a summary dict for telemetry.
    """
    panels = assistant_msg.get("workers") or []
    if not panels:
        return {"rewound": 0, "deleted": 0, "skipped": 0}

    target_ts = target_user_msg.get("timestamp") or ""
    # Group panels by worker_session_id, keeping only the FIRST panel
    # per worker (delegation order = list order, earliest first).
    first_panel_by_worker: dict[str, dict] = {}
    for panel in panels:
        wsid = panel.get("worker_session_id")
        if not wsid or wsid in first_panel_by_worker:
            continue
        first_panel_by_worker[wsid] = panel

    rewound = 0
    deleted = 0
    skipped = 0
    for wsid, panel in first_panel_by_worker.items():
        worker_session = session_manager.get(wsid)
        record = worker_store.get_worker("", wsid)
        created_at = (record or {}).get("created_at") or ""

        born_this_turn = False
        if created_at and target_ts:
            try:
                born_this_turn = datetime.fromisoformat(created_at) > datetime.fromisoformat(target_ts)
            except (ValueError, TypeError):
                born_this_turn = bool(created_at > target_ts)

        if born_this_turn:
            # Tear down the worker entirely — it didn't exist before
            # this turn, so "rewind" means "make it never have
            # existed."
            worker_store.remove_worker("", wsid)
            _safe_delete_forks(
                worker_store.clear_forks_for_worker_everywhere(wsid),
                "delete delegate-fork BC %s failed",
            )
            if worker_session is not None:
                session_manager.delete(wsid)
            deleted += 1
            continue

        anchor_uuid = first_user_uuid_in_panel(panel)
        fork_sid = panel.get("fork_agent_sid")
        if not anchor_uuid or not fork_sid:
            # Delegation produced no anchor — nothing to rewind on
            # claude's side. Just clear forks so the next delegation
            # re-forks fresh.
            _safe_delete_forks(
                worker_store.clear_forks_for_worker_everywhere(wsid),
                "delete delegate-fork BC %s failed",
            )
            skipped += 1
            continue

        try:
            await coordinator.rewind_session(wsid, fork_sid, anchor_uuid)
        except RuntimeError as e:
            logger.warning("worker rewind failed for %s: %s", wsid, e)
            _safe_delete_forks(
                worker_store.clear_forks_for_worker_everywhere(wsid),
                "delete fork BC %s on rewind failure failed",
            )
            skipped += 1
            continue
        except Exception:
            logger.exception("worker rewind crashed for %s", wsid)
            _safe_delete_forks(
                worker_store.clear_forks_for_worker_everywhere(wsid),
                "delete fork BC %s on rewind failure failed",
            )
            skipped += 1
            continue

        _safe_delete_forks(
            worker_store.clear_forks_for_worker_everywhere(wsid),
            "delete fork BC %s post-rewind failed",
        )
        rewound += 1

    if rewound or deleted:
        await coordinator.broadcast_workers_changed(None)
    return {"rewound": rewound, "deleted": deleted, "skipped": skipped}
