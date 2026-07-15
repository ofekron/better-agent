"""Ephemeral per-(root, turn) render cache for the in-flight turn.

Non-durable counterpart to the durable chat projection: an in-memory
dict, no fsync, no `ProjectionCommit`. It renders the CURRENT turn's raw
events through the same `bff_chat_render.render_chat` funnel the durable
read path uses, so live typing latency does not depend on the durable
double-fsync-per-fact commit pipeline.

`update` is last-write-wins; `settle` drops the entry once the turn is
durably committed (the durable projection becomes authoritative for it);
`rehydrate` reconstructs the cache on demand after a restart by
tail-reading `events.jsonl` past the last settled canonical boundary —
the same incremental gap-catch-up pattern as
`event_journal._ensure_canonical_authority`.
"""
from __future__ import annotations

import threading
from typing import Any, Mapping, Sequence

from bff_chat_render import render_chat
from canonical_event_adapter import (
    canonical_facts_from_rows,
    canonical_message_facts,
    fact_to_wire,
)
from canonical_runtime_journal import canonical_runtime_journal
from event_ingester import event_ingester

_READ_PAGE = 2_000


def _wire_facts(
    root_id: str,
    rows: Sequence[Mapping[str, Any]],
    session: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Turn raw journal rows + session scaffold into ordered wire facts.

    Message scaffolds (`user_prompt`/`message_ownership_declared`) are
    cheap and required for turn structure and provider identity; only the
    turn's raw event rows carry the in-flight content. Canonical seqs are
    assigned monotonically in production order so the adapter/projector
    order events identically to the durable path.
    """
    message_facts = canonical_message_facts(
        root_id, {**dict(session), "generation": 0},
    )
    row_facts = canonical_facts_from_rows(
        {**dict(row), "root_id": root_id, "root_generation": 0} for row in rows
    )
    wire: list[dict[str, Any]] = []
    seq = 0
    for fact in (*message_facts, *row_facts):
        seq += 1
        wire.append(fact_to_wire(fact, seq))
    return wire


def _turn_items(
    items: Sequence[dict[str, Any]], turn_id: str,
) -> list[dict[str, Any]] | None:
    index = next(
        (i for i, item in enumerate(items)
         if item["type"] == "Turn" and item["id"] == turn_id),
        None,
    )
    if index is None:
        return None
    start = index
    while start > 0 and items[start - 1]["type"] == "ModelChange":
        start -= 1
    return list(items[start:index + 1])


class CurrentTurnCache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[tuple[str, str], list[dict[str, Any]]] = {}

    def _render_turn(
        self,
        root_id: str,
        turn_id: str,
        rows: Sequence[Mapping[str, Any]],
        session: Mapping[str, Any],
    ) -> list[dict[str, Any]] | None:
        rendered = render_chat(_wire_facts(root_id, rows, session), session)
        return _turn_items(rendered.items, turn_id)

    def update(
        self,
        root_id: str,
        turn_id: str,
        rows: Sequence[Mapping[str, Any]],
        session: Mapping[str, Any],
    ) -> list[dict[str, Any]] | None:
        items = self._render_turn(root_id, turn_id, rows, session)
        with self._lock:
            if items is None:
                self._entries.pop((root_id, turn_id), None)
            else:
                self._entries[(root_id, turn_id)] = items
        return items

    def get(self, root_id: str, turn_id: str) -> list[dict[str, Any]] | None:
        with self._lock:
            return self._entries.get((root_id, turn_id))

    def settle(self, root_id: str, turn_id: str) -> None:
        with self._lock:
            self._entries.pop((root_id, turn_id), None)

    def rehydrate(
        self, root_id: str, turn_id: str, session: Mapping[str, Any],
    ) -> list[dict[str, Any]] | None:
        authority = canonical_runtime_journal().current_authority(root_id)
        after_seq = authority.journal_through_seq if authority is not None else -1
        rows: list[dict[str, Any]] = []
        while True:
            page, _, has_more = event_ingester.read_events(
                root_id, after_seq=after_seq, limit=_READ_PAGE,
            )
            rows.extend(page)
            next_seq = max(
                (int(row.get("seq") or 0) for row in page), default=after_seq,
            )
            if not has_more:
                break
            if next_seq <= after_seq:
                raise RuntimeError("current-turn rehydrate made no read progress")
            after_seq = next_seq
        return self.update(root_id, turn_id, rows, session)


current_turn_cache = CurrentTurnCache()
