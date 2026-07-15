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
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any, Awaitable, Callable, Mapping

from bff_current_turn_cache import current_turn_cache
from bff_runtime_service import RuntimeServiceError, runtime_service

logger = logging.getLogger(__name__)

_SETTLE_TYPES = frozenset({"turn_complete", "turn_stopped", "turn_detached"})
_SESSION_TAIL_SEQ = 1 << 62

SessionReader = Callable[[str], Awaitable[Mapping[str, Any] | None]]


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
    def __init__(self, *, session_reader: SessionReader | None = None) -> None:
        self._session_reader = session_reader or _default_session_reader
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
        if etype in _SETTLE_TYPES:
            self._settle(root_id)
            return
        if etype == "turn_start":
            self._reset(root_id)
            return
        session = self._sessions.get(root_id)
        turn_id = self._turn_ids.get(root_id)
        if session is None:
            session = await self._session_reader(root_id)
            if not isinstance(session, dict):
                return
            turn_id = _turn_id_from_session(session)
            if not turn_id:
                return
            self._sessions[root_id] = session
            self._turn_ids[root_id] = turn_id
        rows = self._rows.setdefault(root_id, [])
        rows.append(_row_from_frame(frame, root_id))
        await asyncio.to_thread(
            current_turn_cache.update, root_id, turn_id, rows, session,
        )

    def _settle(self, root_id: str) -> None:
        turn_id = self._turn_ids.get(root_id)
        if turn_id:
            current_turn_cache.settle(root_id, turn_id)
        self._reset(root_id)

    def _reset(self, root_id: str) -> None:
        self._rows.pop(root_id, None)
        self._sessions.pop(root_id, None)
        self._turn_ids.pop(root_id, None)


current_turn_feed = CurrentTurnFeed()
