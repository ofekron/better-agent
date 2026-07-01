"""Render-tree hydration from events.jsonl.

In schema v8 the on-disk session snapshot is METADATA-ONLY: every
`msg.events` and `msg.workers[*].events` list
is empty on disk. The authoritative event stream lives in
`<ba_home>/sessions/<root_id>/events.jsonl` (written append-only by
the event journal). This module rebuilds the per-msg events lists from
events.jsonl whenever the in-memory cache is cold (first access after
backend start) or lagging (orphan-event recovery).

Two entry points:

  - `hydrate_msg_events_from_jsonl(tree)` — synchronous, called from
    `session_manager._load_root` after disk read. Populates every
    `msg.events` list inline before any reader sees the tree.

  - `reconcile_msg_events_from_jsonl(tree)` — same body, exposed for
    the async reconcile path (`session_manager._sync_reconcile`).
    Idempotent — `apply_event` dedups by event uuid, so re-running on
    an already-hydrated tree is a no-op.

The body was extracted from `main.py:_reconcile_msg_events_from_jsonl`
in commit "v8 snapshot: drop msg.events from disk" so both
`session_manager` and `main` can call into it without a `main →
session_manager → main` import cycle.
"""

import hashlib
import json
import logging
import time
from typing import Callable, Optional

from event_journal import FORK_BACKUP_SOURCE, event_journal_reader
from event_shape import project_content_snapshot
from orchs.base import (
    _event_uuid,
    _normalize_for_render,
    _unwrap_typed_worker_envelope,
)

logger = logging.getLogger(__name__)


def _row_event(raw: dict) -> dict:
    return _unwrap_typed_worker_envelope({
        "type": raw.get("type"),
        "data": raw.get("data"),
    })


def _is_worker_row(raw: dict) -> bool:
    return _row_event(raw).get("type") in {
        "worker_start",
        "worker_event",
        "worker_complete",
    }


def _message_timeline_fingerprint(msg: dict) -> str:
    from render_stub import timeline_events

    try:
        payload = json.dumps(
            timeline_events(msg),
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    except (TypeError, ValueError):
        payload = repr(timeline_events(msg)).encode("utf-8", errors="replace")
    return hashlib.sha256(payload).hexdigest()


def event_rows_by_msg_id_with_orphans(tree: dict, sid: str) -> dict[str, list[dict]]:
    root_id = tree.get("id")
    if not root_id or not sid:
        return {}
    messages = tree.get("messages") or []
    assistant_msgs = [
        (i, m) for i, m in enumerate(messages)
        if isinstance(m, dict) and m.get("role") == "assistant" and m.get("id")
    ]
    if not assistant_msgs:
        return {}
    by_msg_id, orphan_raw = _event_rows_for_sid(root_id, sid)
    orphan_by_msg_id = _bracket_orphan_rows(assistant_msgs, by_msg_id, orphan_raw)
    out = {msg_id: list(rows) for msg_id, rows in by_msg_id.items()}
    for msg_id, rows in orphan_by_msg_id.items():
        out.setdefault(msg_id, []).extend(rows)
    return out


def _event_rows_for_sid(root_id: str, sid: str) -> tuple[dict[str, list[dict]], list[dict]]:
    start = time.perf_counter()
    all_raw, _, _ = event_journal_reader.read_events(
        root_id, limit=20_000, sid_filter=sid,
    )
    by_msg_id: dict[str, list[dict]] = {}
    orphan_raw: list[dict] = []
    for event in all_raw:
        if event.get("source") == FORK_BACKUP_SOURCE:
            continue
        msg_id = event.get("msg_id")
        if msg_id:
            by_msg_id.setdefault(msg_id, []).append(event)
        else:
            orphan_raw.append(event)
    elapsed_ms = (time.perf_counter() - start) * 1000
    if elapsed_ms >= 20 or len(all_raw) >= 1000:
        logger.info(
            "hydrate event_rows %s/%s: rows=%d msg_ids=%d orphans=%d %.1fms",
            root_id[:8], sid[:8], len(all_raw), len(by_msg_id),
            len(orphan_raw), elapsed_ms,
        )
    return by_msg_id, orphan_raw


def _bracket_orphan_rows(
    assistant_msgs: list[tuple[int, dict]],
    by_msg_id: dict[str, list[dict]],
    orphan_raw: list[dict],
) -> dict[str, list[dict]]:
    msg_boundaries: list[tuple[str, int, Optional[int]]] = []
    for idx, (ai, message) in enumerate(assistant_msgs):
        msg_id = message["id"]
        named = by_msg_id.get(msg_id, [])
        floor_seq = max(row.get("seq", 0) for row in named) if named else 0
        if idx + 1 < len(assistant_msgs):
            next_id = assistant_msgs[idx + 1][1]["id"]
            next_named = by_msg_id.get(next_id, [])
            ceil_seq = next_named[0].get("seq") if next_named else None
        else:
            ceil_seq = None
        msg_boundaries.append((msg_id, floor_seq, ceil_seq))

    out: dict[str, list[dict]] = {}
    for msg_id, floor_seq, ceil_seq in msg_boundaries:
        for row in orphan_raw:
            raw_seq = row.get("seq", 0)
            if raw_seq <= floor_seq:
                continue
            if ceil_seq is not None and raw_seq >= ceil_seq:
                continue
            out.setdefault(msg_id, []).append(row)
    return out


def hydrate_msg_events_from_jsonl(
    tree: dict,
    *,
    on_historical_change: Optional[Callable[[str, str, dict], None]] = None,
) -> None:
    """For each assistant message whose persisted events lag events.jsonl,
    apply the missing entries via strategy.apply_event(source_is_provider_stream=False).
    Idempotent: apply_event dedups by event uuid.

    Also recovers "orphaned" events — entries in events.jsonl with
    msg_id=None that were written by the ClaudeJsonlTailer after the
    orchestrator had already finalized the turn (isStreaming=False).
    These are assigned to the closest preceding finalized assistant
    message by seq ordering.

    After re-applying, re-derive `msg.content` from the rendered events
    and update it if it changed AND the message is no longer streaming.
    """
    from orchs import ApplyEventCtx, get_strategy
    from session_manager import manager as session_manager

    root_id = tree.get("id")
    if not root_id:
        return
    bulk_live_root = session_manager.get_ref(root_id) is tree
    rows_cache: Optional[dict[str, tuple[dict[str, list[dict]], list[dict]]]] = None
    tree_sids: set[str] = set()

    def _collect_tree_sids(node: dict) -> None:
        node_sid = node.get("id")
        if node_sid:
            tree_sids.add(node_sid)
        for child in node.get("forks", []):
            if isinstance(child, dict):
                _collect_tree_sids(child)

    _collect_tree_sids(tree)

    def _event_rows_for_tree_sid(sid: str) -> tuple[dict[str, list[dict]], list[dict]]:
        nonlocal rows_cache
        if len(tree_sids) <= 1:
            return _event_rows_for_sid(root_id, sid)
        if rows_cache is None:
            start = time.perf_counter()
            all_raw, _, _ = event_journal_reader.read_events(
                root_id, limit=200_000,
            )
            elapsed_ms = (time.perf_counter() - start) * 1000
            if elapsed_ms >= 20 or len(all_raw) >= 1000:
                logger.info(
                    "hydrate event_rows bulk %s: rows=%d %.1fms",
                    root_id[:8], len(all_raw), elapsed_ms,
                )
            rows_cache = {}
            for event in all_raw:
                event_sid = event.get("sid")
                if not event_sid or event.get("source") == FORK_BACKUP_SOURCE:
                    continue
                by_msg_id, orphan_raw = rows_cache.setdefault(event_sid, ({}, []))
                msg_id = event.get("msg_id")
                if msg_id:
                    by_msg_id.setdefault(msg_id, []).append(event)
                else:
                    orphan_raw.append(event)
        return rows_cache.get(sid, ({}, []))

    def _visit(node: dict, parent_sid: Optional[str] = None) -> None:
        sid = node.get("id")
        if not sid:
            return

        lookup_sid = sid
        mode = node.get("orchestration_mode") or "team"
        strategy = get_strategy(mode)
        msgs = node.get("messages") or []
        ctx = ApplyEventCtx(root_id=root_id)

        assistant_msgs = [
            (i, m) for i, m in enumerate(msgs)
            if m.get("role") == "assistant" and m.get("id")
        ]

        if not assistant_msgs:
            for f in node.get("forks", []):
                _visit(f, parent_sid=sid)
            return

        # `all_finalized` gates the count-match skip below. There is
        # deliberately NO "finalized msg already has events → skip"
        # fast path: a finalized msg can hold a PARTIAL event list —
        # the live stream applies events up to the last apply_event,
        # then the turn's tail arrives via the orphan ingest path
        # (events.jsonl only, never the cache msg) and the msg is
        # finalized. Skipping on "non-empty" alone froze such a msg at
        # its last streamed event (the render tree ending at e.g. the
        # final tool_use, with the closing assistant text missing). The
        # journal read below is the only sound way to detect the lag;
        # the count-match guard then skips the expensive re-apply when
        # the cache already matches the journal.
        all_finalized = all(not m.get("isStreaming") for _, m in assistant_msgs)

        by_msg_id, orphan_raw = _event_rows_for_tree_sid(lookup_sid)

        # No orphans + every msg already matches jsonl count → skip.
        if not orphan_raw and all_finalized:
            all_match = True
            for _, m in assistant_msgs:
                if m.get("isStreaming"):
                    all_match = False
                    break
                if (
                    (bool(m.get("_content_dirty")) or not m.get("content"))
                    and strategy._events_list(m)
                ):
                    all_match = False
                    break
                mid = m["id"]
                jsonl_count = len(by_msg_id.get(mid, []))
                msg_count = len(strategy._events_list(m))
                if jsonl_count > msg_count:
                    all_match = False
                    break
            if all_match:
                for f in node.get("forks", []):
                    _visit(f, parent_sid=sid)
                return

        # Build a set of uuids already present so we skip orphans already applied.
        known_uuids: set[str] = set()
        for _, am in assistant_msgs:
            for ev in strategy._events_list(am):
                d = ev.get("data") if isinstance(ev, dict) else None
                if isinstance(d, dict) and d.get("uuid"):
                    known_uuids.add(d["uuid"])

        orphan_by_msg_id = _bracket_orphan_rows(assistant_msgs, by_msg_id, orphan_raw)

        last_idx = len(assistant_msgs) - 1
        for idx, (ai, m) in enumerate(assistant_msgs):
            msg_id = m["id"]
            if m.get("isStreaming"):
                continue

            # Stub-invalidation detection: a NON-latest (historical,
            # frontend-collapsed) msg whose expanded timeline changes
            # must refresh the frontend stub. Even outside-tail changes
            # need the ping because the frontend keys its full-message
            # fetch cache on `stubVersion`. The latest msg is sent FULL
            # to the frontend (no stub), so it's skipped. Only armed on
            # the reconcile path (callback set); cold-load hydrate passes
            # None.
            watch_change = on_historical_change is not None and idx != last_idx
            changed_for_stub = False

            # Named events for this msg. These rows are already canonical
            # events.jsonl facts. Bulk-append ordinary render events instead
            # of replaying every row through apply_event; heavy sessions can
            # carry hundreds of thousands of rows and apply_event holds the
            # root lock for the whole hydrate. Worker envelopes still go
            # through apply_event because they route to worker panels, not
            # msg.events.
            named_raw = by_msg_id.get(msg_id, [])
            worker_raw = []
            render_raw = []
            for raw in named_raw:
                if _is_worker_row(raw):
                    worker_raw.append(raw)
                else:
                    render_raw.append(raw)
            orphan_rows = orphan_by_msg_id.get(msg_id, [])
            pre_worker_fingerprint = (
                _message_timeline_fingerprint(m)
                if watch_change and worker_raw
                else None
            )
            if render_raw and bulk_live_root:
                changed_for_stub = _merge_render_rows(strategy, m, render_raw)
            else:
                worker_raw = named_raw
            for raw in worker_raw:
                ev = _row_event(raw)
                strategy.apply_event(
                    app_session_id=sid, msg=m, event=ev,
                    ctx=ctx, source_is_provider_stream=False,
                )
            if (
                pre_worker_fingerprint is not None
                and _message_timeline_fingerprint(m) != pre_worker_fingerprint
            ):
                changed_for_stub = True

            for raw in orphan_rows:
                data = raw.get("data")
                raw_uuid = (data or {}).get("uuid") if isinstance(data, dict) else None
                if raw_uuid and raw_uuid in known_uuids:
                    continue
                ev = {"type": raw.get("type"), "data": data}
                strategy.apply_event(
                    app_session_id=sid, msg=m, event=ev,
                    ctx=ctx, source_is_provider_stream=False,
                )
                if raw_uuid:
                    known_uuids.add(raw_uuid)
                changed_for_stub = True

            if watch_change and changed_for_stub:
                on_historical_change(sid, msg_id, m)

            # Re-derive content for finalized messages whose content didn't
            # get set or is stale.
            live_sess = session_manager.get_ref(sid) or {}
            live_m = next(
                (mm for mm in (live_sess.get("messages") or [])
                 if mm.get("id") == msg_id),
                None,
            )
            if live_m is None or live_m.get("isStreaming") is True:
                continue
            extracted = project_content_snapshot(
                strategy._events_list(live_m), live_m.get("content"),
            )
            if extracted != (live_m.get("content") or ""):
                session_manager.update_running_content(sid, msg_id, extracted)

        for f in node.get("forks", []):
            _visit(f, parent_sid=sid)

    # bump_updated_at=False: reconcile re-projects durable journal facts
    # into the render tree (apply_event + content re-derivation). It is
    # not user activity, so it must not bump `updated_at` and reorder the
    # session in the sidebar. Re-entrant batch: a no-op when cold-load's
    # phantom batch (or any outer batch) already owns this root.
    with session_manager.batch(root_id, bump_updated_at=False):
        _visit(tree)


# Alias for the async-reconcile call site — same body, idempotent.
reconcile_msg_events_from_jsonl = hydrate_msg_events_from_jsonl


def _merge_render_rows(strategy, msg: dict, rows: list[dict]) -> bool:
    evs = strategy._events_list(msg)
    owner = strategy._events_owner(msg)
    uid_idx = owner.get("_uid_idx")
    if uid_idx is None:
        uid_idx = {}
        for i, existing in enumerate(evs):
            uid = _event_uuid(existing)
            if uid:
                uid_idx[uid] = i
        owner["_uid_idx"] = uid_idx

    coalesced: dict[str, dict] = {}
    for raw in rows:
        etype = raw.get("type")
        if etype not in strategy._RENDER_TREE_ETYPES:
            continue
        normalized = _normalize_for_render({
            "type": etype,
            "data": raw.get("data"),
        })
        ev_uuid = _event_uuid(normalized)
        if not ev_uuid:
            continue
        coalesced[ev_uuid] = normalized

    changed = False
    for ev_uuid, normalized in coalesced.items():
        existing_idx = uid_idx.get(ev_uuid)
        if existing_idx is None:
            uid_idx[ev_uuid] = len(evs)
            evs.append(normalized)
            changed = True
        elif evs[existing_idx] != normalized:
            evs[existing_idx] = normalized
            changed = True
    if changed:
        from render_stub import invalidate_panel_anchor_cache
        invalidate_panel_anchor_cache(msg)
    return changed
