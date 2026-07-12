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
import os
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Optional

from event_journal import FORK_BACKUP_SOURCE
from event_ingester import event_ingester
from event_shape import project_content_snapshot
import hydration_index_store
from orchs.base import (
    _event_uuid,
    _normalize_for_render,
    _unwrap_typed_worker_envelope,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PreparedHydration:
    root_id: str
    tree_sids: tuple[str, ...]
    after_seq: int
    journal_seq: int
    identity: tuple[int, int, int, int, int]
    ownership_generation: str
    ownership: tuple[tuple[int, str], ...]
    offsets_by_sid: tuple[tuple[str, tuple[int, ...]], ...]


def prepare_hydration(
    root_id: str, tree_sids: tuple[str, ...], *, after_seq: int = 0,
    _attempt: int = 0,
) -> PreparedHydration:
    path, index, _ = _hydration_index(root_id)
    journal_seq = event_ingester.current_seq(root_id) or 0
    if _journal_identity(path) != index.identity:
        if _attempt >= 2:
            raise RuntimeError("event journal changed during hydration preparation")
        return prepare_hydration(
            root_id, tree_sids, after_seq=after_seq, _attempt=_attempt + 1,
        )
    wanted = tuple(sorted(set(tree_sids)))
    return PreparedHydration(
        root_id=root_id,
        tree_sids=wanted,
        after_seq=after_seq,
        journal_seq=journal_seq,
        identity=index.identity,
        ownership_generation=index.ownership_generation,
        ownership=index.ownership,
        offsets_by_sid=tuple(
            (sid, index.offsets_by_sid.get(sid, ())) for sid in wanted
        ),
    )


def decode_prepared_hydration(
    prepared: PreparedHydration,
) -> Optional[dict[str, tuple[dict[str, tuple[dict, ...]], tuple[dict, ...]]]]:
    path = event_ingester._events_path(prepared.root_id)
    ownership = dict(prepared.ownership)
    decoded: dict[str, tuple[dict[str, tuple[dict, ...]], tuple[dict, ...]]] = {}
    try:
        if _journal_identity(path) != prepared.identity:
            return None
        with path.open("rb") as file:
            for sid, offsets in prepared.offsets_by_sid:
                by_msg: dict[str, list[dict]] = {}
                orphans: list[dict] = []
                for offset in offsets:
                    file.seek(offset, os.SEEK_SET)
                    row = json.loads(file.readline())
                    if not isinstance(row, dict) or int(row.get("seq") or 0) <= prepared.after_seq:
                        continue
                    if row.get("source") == FORK_BACKUP_SOURCE:
                        continue
                    seq = row.get("seq")
                    if isinstance(seq, int) and seq in ownership:
                        row["msg_id"] = ownership[seq]
                    msg_id = row.get("msg_id")
                    if isinstance(msg_id, str) and msg_id:
                        by_msg.setdefault(msg_id, []).append(row)
                    else:
                        orphans.append(row)
                decoded[sid] = (
                    {msg_id: tuple(rows) for msg_id, rows in by_msg.items()},
                    tuple(orphans),
                )
        if _journal_identity(path) != prepared.identity:
            return None
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return decoded


def apply_prepared_hydration(
    tree: dict,
    prepared: PreparedHydration,
    decoded_rows: dict[str, tuple[dict[str, tuple[dict, ...]], tuple[dict, ...]]],
    *,
    on_historical_change: Optional[Callable[[str, str, dict], None]] = None,
    ownership_validated: bool = False,
) -> bool:
    def ids(node: dict) -> list[str]:
        result = [node.get("id")]
        for child in node.get("forks", []):
            result.extend(ids(child))
        return [value for value in result if isinstance(value, str)]
    if tree.get("id") != prepared.root_id or tuple(sorted(ids(tree))) != prepared.tree_sids:
        return False
    if (event_ingester.current_seq(prepared.root_id) or 0) != prepared.journal_seq:
        return False
    path = event_ingester._events_path(prepared.root_id)
    try:
        if _journal_identity(path) != prepared.identity:
            return False
    except OSError:
        return False
    if not ownership_validated and not validate_prepared_ownership(prepared):
        return False
    _hydrate_msg_events_from_jsonl(
        tree, after_seq=prepared.after_seq, prepared_rows=decoded_rows,
        on_historical_change=on_historical_change,
    )
    return True


@dataclass(frozen=True)
class _HydrationIndex:
    identity: tuple[int, int, int, int, int]
    ownership_generation: str
    ownership: tuple[tuple[int, str], ...]
    offsets_by_sid: dict[str, tuple[int, ...]]
    checkpoint: int = 0


_HYDRATION_INDEX_LIMIT = 16
_hydration_indexes: OrderedDict[str, _HydrationIndex] = OrderedDict()
_hydration_building: dict[str, threading.Condition] = {}
_hydration_index_lock = threading.Lock()
_hydration_apply_slots = threading.BoundedSemaphore(2)


@contextmanager
def hydration_decode_apply_slot():
    with _hydration_apply_slots:
        yield


def validate_prepared_ownership(prepared: PreparedHydration) -> bool:
    _, ownership_digest = _ownership_snapshot(prepared.root_id)
    return prepared.ownership_generation.endswith(f":{ownership_digest}")


def _journal_identity(path) -> tuple[int, int, int, int, int]:
    stat = path.stat()
    return (
        int(stat.st_dev), int(stat.st_ino), int(stat.st_size),
        int(stat.st_mtime_ns), int(stat.st_ctime_ns),
    )


def _ownership_snapshot(root_id: str) -> tuple[dict[int, str], str]:
    resolutions = event_ingester.ownership_resolutions(root_id)
    digest = hashlib.sha256()
    for seq, msg_id in sorted(resolutions.items()):
        digest.update(f"{seq}\0{msg_id}\0".encode("utf-8", errors="surrogatepass"))
    return resolutions, digest.hexdigest()


def _ownership_generation(root_id: str) -> int:
    return int(event_ingester._root_events_version.get(root_id, 0))


def _build_hydration_index(
    path, identity: tuple[int, int, int, int, int], resolutions: dict[int, str],
    ownership_generation: str, prior: Optional[_HydrationIndex] = None,
) -> Optional[_HydrationIndex]:
    offsets, metrics = hydration_index_store.load(
        path.parent.name, path,
        prior.offsets_by_sid if prior is not None else None,
        prior.checkpoint if prior is not None else 0,
    )
    if metrics["cold"] or metrics["scanned_bytes"] >= 1024 * 1024:
        logger.info(
            "hydrate index projection root=%s cold=%d scanned_bytes=%d rows=%d elapsed_ms=%d",
            path.parent.name[:8], metrics["cold"], metrics["scanned_bytes"],
            metrics["rows"], metrics["elapsed_ms"],
        )
    try:
        if _journal_identity(path) != identity:
            return None
    except OSError:
        return None
    return _HydrationIndex(
        identity=identity,
        ownership_generation=ownership_generation,
        ownership=tuple(sorted(resolutions.items())),
        offsets_by_sid=offsets,
        checkpoint=metrics["checkpoint"],
    )


def _hydration_index(root_id: str) -> tuple[object, _HydrationIndex, dict[int, str]]:
    path = event_ingester._events_path(root_id)
    while True:
        identity = _journal_identity(path)
        ownership_version = _ownership_generation(root_id)
        with _hydration_index_lock:
            cached = _hydration_indexes.get(root_id)
            if (
                cached is not None
                and cached.identity == identity
                and cached.ownership_generation.startswith(f"{ownership_version}:")
            ):
                _hydration_indexes.move_to_end(root_id)
                return path, cached, dict(cached.ownership)
            condition = _hydration_building.get(root_id)
            if condition is not None:
                condition.wait()
                continue
            condition = threading.Condition(_hydration_index_lock)
            _hydration_building[root_id] = condition
        built: Optional[_HydrationIndex] = None
        try:
            resolutions, ownership_digest = _ownership_snapshot(root_id)
            ownership_generation = f"{ownership_version}:{ownership_digest}"
            built = _build_hydration_index(
                path, identity, resolutions, ownership_generation, cached,
            )
        finally:
            with _hydration_index_lock:
                condition = _hydration_building.pop(root_id)
                if built is not None:
                    _hydration_indexes[root_id] = built
                    _hydration_indexes.move_to_end(root_id)
                    while len(_hydration_indexes) > _HYDRATION_INDEX_LIMIT:
                        _hydration_indexes.popitem(last=False)
                condition.notify_all()
        if built is not None:
            return path, built, resolutions


def _indexed_rows_for_sid(root_id: str, sid: str) -> list[dict]:
    while True:
        try:
            path, index, resolutions = _hydration_index(root_id)
        except OSError:
            return []
        rows: list[dict] = []
        try:
            with path.open("rb") as file:
                for offset in index.offsets_by_sid.get(sid, ()):
                    file.seek(offset, os.SEEK_SET)
                    try:
                        row = json.loads(file.readline())
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
                    if not isinstance(row, dict):
                        continue
                    seq = row.get("seq")
                    if isinstance(seq, int) and seq in resolutions:
                        row["msg_id"] = resolutions[seq]
                    rows.append(row)
            if (
                _journal_identity(path) == index.identity
                and index.ownership_generation.startswith(
                    f"{_ownership_generation(root_id)}:",
                )
            ):
                return rows
        except OSError:
            pass


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


def _event_rows_for_sid(
    root_id: str, sid: str, *, after_seq: int = 0,
) -> tuple[dict[str, list[dict]], list[dict]]:
    start = time.perf_counter()
    all_raw = [
        row for row in _indexed_rows_for_sid(root_id, sid)
        if int(row.get("seq") or 0) > after_seq
    ]
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


def _add_event_row(
    rows_cache: dict[str, tuple[dict[str, list[dict]], list[dict]]],
    event: dict,
) -> None:
    event_sid = event.get("sid")
    if not event_sid or event.get("source") == FORK_BACKUP_SOURCE:
        return
    by_msg_id, orphan_raw = rows_cache.setdefault(event_sid, ({}, []))
    msg_id = event.get("msg_id")
    if msg_id:
        by_msg_id.setdefault(msg_id, []).append(event)
        return
    orphan_raw.append(event)


def _bracket_orphan_rows(
    assistant_msgs: list[tuple[int, dict]],
    by_msg_id: dict[str, list[dict]],
    orphan_raw: list[dict],
) -> dict[str, list[dict]]:
    # A message owns orphan rows whose seq falls between its own last
    # named row (floor) and the first named row of the NEXT message that
    # actually has named rows (ceil). Scanning forward past not-yet-
    # resolved (empty) messages is required: while a neighbor turn's
    # events are still transient orphans mid-resolution it has zero named
    # rows, and an unbounded ceil there made this message swallow every
    # later turn's orphans (rendering them under the wrong turn).
    n = len(assistant_msgs)
    first_named: list[Optional[int]] = [
        min((row.get("seq", 0) for row in by_msg_id.get(message["id"], [])), default=None)
        for _ai, message in assistant_msgs
    ]
    msg_boundaries: list[tuple[str, int, Optional[int]]] = []
    for idx, (ai, message) in enumerate(assistant_msgs):
        msg_id = message["id"]
        named = by_msg_id.get(msg_id, [])
        floor_seq = max((row.get("seq", 0) for row in named), default=0)
        ceil_seq: Optional[int] = None
        for j in range(idx + 1, n):
            if first_named[j] is not None:
                ceil_seq = first_named[j]
                break
        msg_boundaries.append((msg_id, floor_seq, ceil_seq))

    # Assign each orphan to EXACTLY ONE message — the nearest preceding
    # message (greatest floor below the row) whose window contains it.
    # Single-owner attribution prevents one event rendering under two
    # turns when windows overlap (e.g. an empty message with floor 0).
    out: dict[str, list[dict]] = {}
    for row in orphan_raw:
        raw_seq = row.get("seq", 0)
        best_floor: Optional[int] = None
        best_msg: Optional[str] = None
        for msg_id, floor_seq, ceil_seq in msg_boundaries:
            if raw_seq <= floor_seq:
                continue
            if ceil_seq is not None and raw_seq >= ceil_seq:
                continue
            if best_floor is None or floor_seq > best_floor:
                best_floor = floor_seq
                best_msg = msg_id
        if best_msg is not None:
            out.setdefault(best_msg, []).append(row)
    return out


def _hydrate_msg_events_from_jsonl(
    tree: dict,
    *,
    after_seq: int = 0,
    on_historical_change: Optional[Callable[[str, str, dict], None]] = None,
    prepared_rows=None,
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
        if prepared_rows is not None:
            by_msg, orphans = prepared_rows.get(sid, ({}, ()))
            return {key: list(value) for key, value in by_msg.items()}, list(orphans)
        if len(tree_sids) <= 1:
            return _event_rows_for_sid(root_id, sid, after_seq=after_seq)
        if rows_cache is None:
            start = time.perf_counter()
            all_raw = []
            for tree_sid in tree_sids:
                all_raw.extend(
                    row for row in _indexed_rows_for_sid(root_id, tree_sid)
                    if int(row.get("seq") or 0) > after_seq
                )
            all_raw.sort(key=lambda row: int(row.get("seq") or 0))
            elapsed_ms = (time.perf_counter() - start) * 1000
            if elapsed_ms >= 20 or len(all_raw) >= 1000:
                logger.info(
                    "hydrate event_rows bulk %s: rows=%d %.1fms",
                    root_id[:8], len(all_raw), elapsed_ms,
                )
            rows_cache = {}
            for event in all_raw:
                _add_event_row(rows_cache, event)
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

        live_sess = node if bulk_live_root else (session_manager.get_ref(sid) or {})
        for idx, (ai, m) in enumerate(assistant_msgs):
            msg_id = m["id"]
            if m.get("isStreaming"):
                continue

            # Stub-invalidation detection: every completed assistant msg is
            # stubbed on heavy read paths, so any expanded-timeline change must
            # refresh the frontend stub. Even outside-tail changes need the ping
            # because the frontend keys its full-message fetch cache on
            # `stubVersion`. Only armed on the reconcile path (callback set);
            # cold-load hydrate passes None.
            watch_change = on_historical_change is not None
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

            if changed_for_stub:
                # This path mutates m's events outside of
                # apply_written_journal_event's incremental-revision
                # bookkeeping (bulk merge / worker replay / orphan
                # catch-up) — invalidate so the next live append
                # re-establishes a correct full-hash baseline instead of
                # folding onto one that no longer reflects reality.
                import messages_delta_compaction
                m.pop(messages_delta_compaction.PRECOMPUTED_REVISION_KEY, None)
            if watch_change and changed_for_stub:
                on_historical_change(sid, msg_id, m)

            live_m = next(
                (mm for mm in (live_sess.get("messages") or [])
                 if mm.get("id") == msg_id),
                None,
            )
            if live_m is None or live_m.get("isStreaming") is True:
                continue
            if not (
                named_raw
                or orphan_rows
                or bool(live_m.get("_content_dirty"))
                or not live_m.get("content")
            ):
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


def hydrate_msg_events_from_jsonl(
    tree: dict,
    *,
    after_seq: int = 0,
    on_historical_change: Optional[Callable[[str, str, dict], None]] = None,
) -> None:
    with hydration_decode_apply_slot():
        _hydrate_msg_events_from_jsonl(
            tree,
            after_seq=after_seq,
            on_historical_change=on_historical_change,
        )


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
