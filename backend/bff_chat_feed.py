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
from chat_projection_cache import projection_cache_root
from chat_projection_store import ChatProjectionStoreError
from bff_runtime_contract import BFF_SERVICE_TOKEN_HEADER
from bff_runtime_service import RuntimeServiceError, runtime_service
from bff_runtime_upstream import RuntimeUpstreamUnavailable, runtime_upstream

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
    return projection_cache_root() / "feed"


def _cursors_path() -> Path:
    return _state_dir() / "cursors.json"


def _resets_path() -> Path:
    return _state_dir() / "pending-resets.json"


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
        # Roots whose projection must be dropped before the next admit:
        # upstream rewrote canonical fact content at/behind our consumed
        # cursor. Durable so a crash between the rewrite frame and the
        # re-pull can't strand a stale projection.
        self._pending_resets: set[str] = set()
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
        self._pending_resets = await asyncio.to_thread(self._load_resets)
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
                    # Pending resets ride along — their cursor entry was
                    # already dropped, so they aren't in _cursors.
                    self.mark_dirty(*self._cursors.keys(), *self._pending_resets)
                    async for raw in upstream:
                        await self._handle_frame(raw)
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

    async def _handle_frame(self, raw: str | bytes) -> None:
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
        if frame_type == "canonical_rewrite":
            await self._handle_rewrite_frame(frame)
            return
        if frame_type != "canonical_advance":
            return
        roots = frame.get("roots")
        if not isinstance(roots, list):
            return
        self.mark_dirty(*(r for r in roots if isinstance(r, str) and r))

    async def _handle_rewrite_frame(self, frame: dict[str, Any]) -> None:
        """Upstream rewrote committed canonical fact content in place.
        If the rewrite landed at/behind our consumed cursor, everything
        we admitted for that root is suspect: drop the cursor and flag
        the root for a projection reset on the next pull."""
        rewrites = frame.get("rewrites")
        if not isinstance(rewrites, dict):
            return
        stale: list[str] = []
        for root_id, seq in rewrites.items():
            if not isinstance(root_id, str) or not root_id:
                continue
            if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
                continue
            if self._cursors.get(root_id, 0) < seq and root_id not in self._pending_resets:
                # Rewrite is ahead of what we consumed — the normal
                # pull will pick up the current content; nothing stale.
                continue
            stale.append(root_id)
        if not stale:
            return
        for root_id in stale:
            self._pending_resets.add(root_id)
            self._cursors.pop(root_id, None)
        # Persist reset intent BEFORE the cursor drop: reversed, a crash
        # in between leaves a rewound cursor whose re-pull is skipped by
        # the projection's durable watermarks — silent staleness.
        await asyncio.to_thread(self._persist_resets)
        await asyncio.to_thread(self._persist_cursors)
        self.mark_dirty(*stale)

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
        while True:
            task = self._pull_tasks.get(root_id)
            joined = task is not None
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
            # A joined in-flight pull predates a rewrite reset and never
            # applied it — run a fresh pull so the reset lands now. A
            # pull we started ourselves leaves the reset pending only on
            # failure; the next advance retries, don't spin here.
            if not joined or root_id not in self._pending_resets:
                return

    async def _pull_root(self, root_id: str) -> None:
        cursor = self._cursors.get(root_id, 0)
        while True:
            page = await self._source_reader(
                root_id, after_seq=cursor, limit=_PAGE_LIMIT,
            )
            if not isinstance(page, dict) or page.get("found") is not True:
                self._cursors.pop(root_id, None)
                await asyncio.to_thread(self._persist_cursors)
                if root_id in self._pending_resets:
                    # Root is gone upstream — nothing left to reset.
                    self._pending_resets.discard(root_id)
                    await asyncio.to_thread(self._persist_resets)
                return
            provider = page.get("provider_kind")
            if provider not in _PROVIDER_KINDS:
                logger.error(
                    "chat feed: provider kind unavailable for %s; refusing to admit",
                    root_id,
                )
                return
            if root_id in self._pending_resets:
                logger.warning(
                    "chat feed: canonical rewrite behind consumed cursor for %s; "
                    "dropping projection and rebuilding from seq 0",
                    root_id,
                )
                await asyncio.to_thread(
                    chat_projection_ingestion.reset_root_projection,
                    root_id, provider=provider,
                )
                self._pending_resets.discard(root_id)
                await asyncio.to_thread(self._persist_resets)
                # A pull that was already in flight when the rewrite
                # frame landed may have re-advanced the cursor; the
                # rebuild must re-admit from the very beginning.
                if cursor != 0:
                    cursor = 0
                    self._cursors.pop(root_id, None)
                    await asyncio.to_thread(self._persist_cursors)
                    continue
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

    def _load_resets(self) -> set[str]:
        try:
            raw = json.loads(_resets_path().read_text(encoding="utf-8"))
        except FileNotFoundError:
            return set()
        except (OSError, ValueError):
            logger.warning("chat feed: pending-resets file unreadable; ignoring")
            return set()
        if not isinstance(raw, list):
            logger.warning("chat feed: pending-resets file malformed; ignoring")
            return set()
        return {root for root in raw if isinstance(root, str) and root}

    def _persist_resets(self) -> None:
        directory = _state_dir()
        directory.mkdir(parents=True, exist_ok=True)
        path = _resets_path()
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(sorted(self._pending_resets)), encoding="utf-8")
        os.replace(tmp, path)

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
