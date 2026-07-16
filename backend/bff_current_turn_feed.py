"""BFF consumer for runtime's ordered raw-event stream.

Runtime forwards in-scope raw event frames over the shared
`/api/bff-runtime/feed` socket (see `runtime_feed_channel`). This module
turns that ordered frame stream into `current_turn_cache` updates: it
accumulates a root's in-flight rows, renders them through the shared
funnel on every frame (last-write-wins), and settles the entry when the
turn's lifecycle frame arrives.

Rows are keyed by root only — there is one in-flight turn per root at a
time. The session scaffold (fetched once per turn) supplies the turn's
prompt identity; the accumulated rows supply the in-flight content.

Every render also publishes a `chat_tree_delta` WS frame to the BFF's
browser-facing hub (`bff_event_hub.hub.publish_session`), keyed by
root_id — the same id the browser subscribes with when viewing that
root directly. This is the live counterpart to the durable
`/api/chat-tree` REST read: same node/lookup wire shape
(`bff_chat_lookup.build_lookup`), so the frontend's `chatTreeToMessages`
consumes both through one function.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any, Awaitable, Callable, Mapping

from bff_current_turn_cache import TurnDelta, current_turn_cache
from bff_runtime_service import RuntimeServiceError, runtime_service

logger = logging.getLogger(__name__)

# turn_complete -> "settled" (durable projection now authoritative);
# turn_stopped -> "stopped" (user/system interrupted); turn_detached ->
# "detached" (backend lost the turn, runner may still be alive). Mirrors
# the phases `useWebSocket.ts` used to derive from raw turn_* frames.
_SETTLE_PHASES = {
    "turn_complete": "settled",
    "turn_stopped": "stopped",
    "turn_detached": "detached",
}
_SESSION_TAIL_SEQ = 1 << 62

# Frame types whose runtime-side effect mutates the in-flight assistant
# message's `workers` snapshot field (panel creation, richer metadata on
# completion, or ownership of the worker's own sub-events). The cached
# session scaffold below is fetched ONCE per turn as a low-latency
# optimization (avoids a runtime round-trip on every streaming token),
# which is correct for pure chat content (agent_message/manager_event/
# todos_snapshot don't touch `workers`) — but a worker frame's effect on
# the snapshot happens on the RUNTIME side, out of band from this cache,
# so a stale cached scaffold would render the panel with no
# description/token_usage/events, or omit it while it exists. Forcing a
# refetch on exactly these types keeps the common case cheap while
# keeping worker panels live-accurate.
_SNAPSHOT_MUTATING_TYPES = frozenset({
    "worker_start", "worker_event", "worker_complete",
    "worker_prep_start", "worker_prep_event", "worker_prep_complete",
    "worker_prep_cancelled",
})

SessionReader = Callable[[str], Awaitable[Mapping[str, Any] | None]]
DeltaPublisher = Callable[[str, str, str, TurnDelta], Awaitable[None]]


async def _default_delta_publisher(
    root_id: str, turn_id: str, phase: str, delta: TurnDelta,
) -> None:
    from bff_event_hub import hub
    # Payload nested under `data`, matching every other WS frame's shape
    # (`resolveLiveFrameSessionId` and the rest of `useWebSocket.ts` read
    # `event.data.app_session_id`, not a top-level field).
    await hub.publish_session(root_id, {
        "type": "chat_tree_delta",
        "data": {
            "app_session_id": root_id,
            "turn_id": turn_id,
            "phase": phase,
            "items": delta.items,
            "lookup": delta.lookup,
        },
    })


async def _default_session_reader(root_id: str) -> Mapping[str, Any] | None:
    """Fetch the root's session scaffold (messages + provider identity).

    `after_seq` past the journal head keeps the payload to the session
    dict only — the current-turn content comes from the raw frames, not
    this call.
    """
    page = await runtime_service.projection_source(
        root_id, after_seq=_SESSION_TAIL_SEQ, limit=1,
    )
    if not isinstance(page, dict) or page.get("found") is not True:
        return None
    session = page.get("session")
    return session if isinstance(session, dict) else None


def _turn_id_from_session(session: Mapping[str, Any]) -> str | None:
    messages = session.get("messages")
    if not isinstance(messages, list):
        return None
    for message in reversed(messages):
        if (
            isinstance(message, dict)
            and message.get("role") == "user"
            and message.get("id")
        ):
            return str(message["id"])
    return None


def _row_from_frame(frame: Mapping[str, Any], root_id: str) -> dict[str, Any]:
    data = frame.get("data")
    return {
        "seq": frame.get("seq"),
        "sid": frame.get("sid") or root_id,
        "type": frame.get("event_type"),
        "source": frame.get("source"),
        "msg_id": frame.get("msg_id"),
        "data": data if isinstance(data, dict) else {},
    }


class CurrentTurnFeed:
    def __init__(
        self,
        *,
        session_reader: SessionReader | None = None,
        delta_publisher: DeltaPublisher | None = None,
    ) -> None:
        self._session_reader = session_reader or _default_session_reader
        self._delta_publisher = delta_publisher or _default_delta_publisher
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._rows: dict[str, list[dict[str, Any]]] = {}
        self._sessions: dict[str, Mapping[str, Any]] = {}
        self._turn_ids: dict[str, str] = {}
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(
                self._run(), name="bff-current-turn-feed",
            )

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def submit(self, frame: Mapping[str, Any]) -> None:
        self._queue.put_nowait(dict(frame))

    async def _run(self) -> None:
        while True:
            frame = await self._queue.get()
            try:
                await self._handle(frame)
            except asyncio.CancelledError:
                raise
            except (RuntimeServiceError, ValueError) as exc:
                logger.warning("current-turn feed: dropping frame (%s)", exc)
            except Exception:
                logger.exception("current-turn feed: frame handling failed")

    async def _handle(self, frame: Mapping[str, Any]) -> None:
        root_id = frame.get("root_id")
        etype = frame.get("event_type")
        if not isinstance(root_id, str) or not root_id or not isinstance(etype, str):
            return
        settle_phase = _SETTLE_PHASES.get(etype)
        if settle_phase is not None:
            await self._settle(root_id, settle_phase)
            return
        if etype == "turn_start":
            self._reset(root_id)
            return
        session = self._sessions.get(root_id)
        turn_id = self._turn_ids.get(root_id)
        if session is None or etype in _SNAPSHOT_MUTATING_TYPES:
            refetched = await self._session_reader(root_id)
            if not isinstance(refetched, dict):
                if session is None:
                    return
            else:
                session = refetched
                refetched_turn_id = _turn_id_from_session(session)
                if refetched_turn_id:
                    turn_id = refetched_turn_id
            if turn_id is None:
                return
            self._sessions[root_id] = session
            self._turn_ids[root_id] = turn_id
        rows = self._rows.setdefault(root_id, [])
        rows.append(_row_from_frame(frame, root_id))
        delta = await asyncio.to_thread(
            current_turn_cache.render_with_lookup, root_id, turn_id, rows, session,
        )
        if delta is not None:
            await self._delta_publisher(root_id, turn_id, "streaming", delta)

    async def _settle(self, root_id: str, phase: str) -> None:
        turn_id = self._turn_ids.get(root_id)
        if turn_id:
            rows = self._rows.get(root_id, [])
            session = self._sessions.get(root_id)
            if session is not None:
                # Publish the final snapshot before clearing the cache — the
                # durable projection becomes authoritative for this turn the
                # moment the browser has this last render (content is the
                # same either way; only the durability backing changes).
                delta = await asyncio.to_thread(
                    current_turn_cache.render_with_lookup,
                    root_id, turn_id, rows, session,
                )
                if delta is not None:
                    await self._delta_publisher(root_id, turn_id, phase, delta)
            current_turn_cache.settle(root_id, turn_id)
        self._reset(root_id)

    def _reset(self, root_id: str) -> None:
        self._rows.pop(root_id, None)
        self._sessions.pop(root_id, None)
        self._turn_ids.pop(root_id, None)


current_turn_feed = CurrentTurnFeed()
