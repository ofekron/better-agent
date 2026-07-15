"""BFF chat rendering cache feed.

Consumes the runtime's canonical feed: a server-to-server WebSocket
announces "canonical journal advanced for root X" facts, and this
client pulls `projection-source` pages for dirty roots and admits
every fact into the canonical chat projection store
(`chat_projection_ingestion.admit_canonical_fact`).

The cache (and its pull cursors) is a disposable, rebuildable
projection of runtime-owned state: a lost or corrupt cursor file just
means re-pulling from seq 0, which admission dedup collapses to a
no-op. The runtime is always authoritative.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from pathlib import Path
from typing import Any, Awaitable, Callable

import websockets

import chat_projection_ingestion
from bff_current_turn_feed import CurrentTurnFeed
from chat_projection_store import ChatProjectionStoreError
from bff_runtime_contract import BFF_SERVICE_TOKEN_HEADER
from bff_runtime_service import RuntimeServiceError, runtime_service
from bff_runtime_upstream import RuntimeUpstreamUnavailable, runtime_upstream
from paths import ba_home

logger = logging.getLogger(__name__)

_PROVIDER_KINDS = {"claude", "codex", "gemini"}
_PAGE_LIMIT = 500
_RECONNECT_MAX_SECONDS = 30.0
# Admission error codes that reject a single fact (conflict / bad shape) rather
# than indicating a store-level failure. Such a fact is un-admittable by
# construction, so we drop it and keep advancing the cursor; aborting would
# stall the whole root's pull on every advance and re-log the same error.
_SOFT_ADMISSION_ERROR_CODES = frozenset({"source_catalog_rejected"})

SourceReader = Callable[..., Awaitable[dict[str, Any]]]


def _state_dir() -> Path:
    return ba_home() / "app-state" / "chat-feed-cache"


def _cursors_path() -> Path:
    return _state_dir() / "cursors.json"


class ChatFeedClient:
    def __init__(
        self,
        *,
        source_reader: SourceReader | None = None,
        current_turn_feed: CurrentTurnFeed | None = None,
    ) -> None:
        self._source_reader = source_reader or runtime_service.projection_source
        self._current_turn_feed = current_turn_feed or CurrentTurnFeed()
        self._cursors: dict[str, int] = {}
        self._dirty: set[str] = set()
        self._wake = asyncio.Event()
        self._connected = False
        self._stopping = False
        self._runner: asyncio.Task | None = None
        self._pull_tasks: dict[str, asyncio.Task] = {}

    # ── lifecycle ─────────────────────────────────────────────────

    async def start(self) -> None:
        self._stopping = False
        self._cursors = await asyncio.to_thread(self._load_cursors)
        self._current_turn_feed.start()
        self._runner = asyncio.create_task(self._run(), name="bff-chat-feed")

    async def stop(self) -> None:
        self._stopping = True
        runner = self._runner
        self._runner = None
        if runner is not None:
            runner.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await runner
        await self._current_turn_feed.stop()
        await asyncio.to_thread(chat_projection_ingestion.close)

    async def _run(self) -> None:
        puller = asyncio.create_task(self._pull_loop(), name="bff-chat-feed-pull")
        try:
            await self._subscribe_loop()
        finally:
            puller.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await puller

    # ── advance subscription ──────────────────────────────────────

    async def _subscribe_loop(self) -> None:
        backoff = 1.0
        while not self._stopping:
            try:
                async with await self._connect() as upstream:
                    self._connected = True
                    backoff = 1.0
                    # Catch up every known root on (re)connect: advances
                    # broadcast while disconnected were never queued.
                    self.mark_dirty(*self._cursors.keys())
                    async for raw in upstream:
                        self._handle_frame(raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._stopping:
                    return
                logger.warning("chat feed subscription lost (%s); reconnecting", exc)
            finally:
                self._connected = False
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _RECONNECT_MAX_SECONDS)

    async def _connect(self):
        lease = await runtime_upstream.acquire()
        descriptor = lease.descriptor
        token = lease.service_token
        await lease.release()
        headers = [(BFF_SERVICE_TOKEN_HEADER, token)]
        if descriptor["kind"] == "uds":
            return websockets.unix_connect(
                descriptor["path"],
                uri="ws://better-agent-runtime/api/bff-runtime/feed",
                additional_headers=headers,
            )
        return websockets.connect(
            f"ws://{descriptor['host']}:{descriptor['port']}/api/bff-runtime/feed",
            additional_headers=headers,
        )

    def _handle_frame(self, raw: str | bytes) -> None:
        try:
            frame = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            logger.warning("chat feed: dropping malformed frame")
            return
        if not isinstance(frame, dict):
            return
        frame_type = frame.get("type")
        if frame_type == "raw_event":
            self._current_turn_feed.submit(frame)
            return
        if frame_type != "canonical_advance":
            return
        roots = frame.get("roots")
        if not isinstance(roots, list):
            return
        self.mark_dirty(*(r for r in roots if isinstance(r, str) and r))

    def mark_dirty(self, *roots: str) -> None:
        if not roots:
            return
        self._dirty.update(roots)
        self._wake.set()

    # ── pulling ───────────────────────────────────────────────────

    async def _pull_loop(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            while self._dirty:
                root_id = self._dirty.pop()
                try:
                    await self.pull_now(root_id)
                except asyncio.CancelledError:
                    raise
                except (RuntimeServiceError, RuntimeUpstreamUnavailable) as exc:
                    logger.warning(
                        "chat feed pull unavailable for %s (%s); will retry on next advance",
                        root_id, exc,
                    )
                except Exception:
                    logger.exception("chat feed pull failed for %s", root_id)

    async def pull_now(self, root_id: str) -> None:
        task = self._pull_tasks.get(root_id)
        if task is None:
            task = asyncio.create_task(
                self._pull_root(root_id),
                name=f"bff-chat-feed-pull-{root_id[:8]}",
            )
            self._pull_tasks[root_id] = task
        try:
            await task
        finally:
            if self._pull_tasks.get(root_id) is task:
                self._pull_tasks.pop(root_id, None)

    async def _pull_root(self, root_id: str) -> None:
        cursor = self._cursors.get(root_id, 0)
        while True:
            page = await self._source_reader(
                root_id, after_seq=cursor, limit=_PAGE_LIMIT,
            )
            if not isinstance(page, dict) or page.get("found") is not True:
                self._cursors.pop(root_id, None)
                await asyncio.to_thread(self._persist_cursors)
                return
            provider = page.get("provider_kind")
            if provider not in _PROVIDER_KINDS:
                logger.error(
                    "chat feed: provider kind unavailable for %s; refusing to admit",
                    root_id,
                )
                return
            facts = page.get("facts")
            for fact in facts if isinstance(facts, list) else []:
                try:
                    await asyncio.to_thread(
                        chat_projection_ingestion.admit_canonical_fact,
                        fact, provider=provider,
                    )
                except ChatProjectionStoreError as exc:
                    if exc.code not in _SOFT_ADMISSION_ERROR_CODES:
                        raise
                    logger.warning(
                        "chat feed: skipping un-admittable fact for %s (%s: %s)",
                        root_id, exc.code, exc.detail,
                    )
            next_seq = page.get("next_seq")
            if isinstance(next_seq, int) and not isinstance(next_seq, bool) and next_seq > cursor:
                cursor = next_seq
                self._cursors[root_id] = cursor
                await asyncio.to_thread(self._persist_cursors)
            if page.get("has_more") is not True:
                return

    # ── cursor persistence ────────────────────────────────────────

    def _load_cursors(self) -> dict[str, int]:
        try:
            raw = json.loads(_cursors_path().read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            logger.warning("chat feed: cursor file unreadable; rebuilding cache from seq 0")
            return {}
        if not isinstance(raw, dict):
            logger.warning("chat feed: cursor file malformed; rebuilding cache from seq 0")
            return {}
        return {
            root: seq for root, seq in raw.items()
            if isinstance(root, str) and root
            and isinstance(seq, int) and not isinstance(seq, bool) and seq >= 0
        }

    def _persist_cursors(self) -> None:
        directory = _state_dir()
        directory.mkdir(parents=True, exist_ok=True)
        path = _cursors_path()
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._cursors, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)

    # ── status ────────────────────────────────────────────────────

    def status(self, root_id: str) -> dict[str, Any]:
        return {
            "root_id": root_id,
            "connected": self._connected,
            "cursor": self._cursors.get(root_id, 0),
            "pending_pull": root_id in self._dirty,
        }


feed_client = ChatFeedClient()
