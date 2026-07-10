"""Tail-F based file tailers — sole I/O substrate for live event streaming.

Two tailers live here, deliberately decoupled:

  - `ClaudeJsonlTailer`: tails one claude CLI session jsonl. For each new
     line: enriches (parent_tool_use_id), forwards to a `dispatch` callback,
     and (recursively) spawns sub-tailers for Agent/Task subagent jsonls.
     Knows nothing about WS, event_ingester, or the orchestrator queue —
     callers wire it to whatever sink they want.

  - `BetterAgentJsonlTailer`: tails one BC root's `events.jsonl`. For each
     new line: parses + invokes a `broadcast` callback. Sole producer of
     live WS frames — so duplicate-push bugs are impossible by construction.

  - `OwnedClaudeJsonlTailer`: refcount-managed ClaudeJsonlTailer for
     a (root_id, agent_sid, jsonl_path) triple. Started by the first
     consumer, stopped when the last releases. The tailer ingests every
     line via the event journal writer as `agent_message` — no
     orchestrator queue, no WS. Lets WS subscribers and active runs
     share one ingest path; coordinator manages the refcount.

Both use `tail -n +K -F <path>` as a subprocess. `-F` follows by name AND
retries on file non-existence, so we no longer need a "wait for the file
to appear" loop. Latency is OS-flush level (sub-ms on local disk). Idle
CPU is zero — `tail` blocks on `read`, the asyncio loop pumps when bytes
arrive.

Why subprocess `tail` instead of polling or a watchdog dep:
  - `tail -F` already handles atomic-rename writes, file truncation,
    file-recreation. We'd reinvent these edge cases with our own polling.
  - No third-party dep. Mac/Linux POSIX `tail` behaves identically for
    our usage.
  - One subprocess per active jsonl is cheap (tail RSS is sub-MB).

Lifecycle: the caller drives `run()` in a Task and calls `stop()` to
terminate. `stop()` SIGTERMs the subprocess; the run loop exits when the
pipe closes. Callers MUST `await` the task after `stop()` to clean up.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import random
import sys
import threading
import time
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional
from weakref import WeakKeyDictionary

import perf
from claude_jsonl_enrich import _SubagentRegistry, enrich_jsonl_line
from session_manager import manager as session_manager

logger = logging.getLogger(__name__)

_CURSOR_EXECUTOR = ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix="jsonl-cursor",
)
_SUBAGENT_SCAN_EXECUTOR = ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix="subagent-scan",
)
_SUBAGENT_SCAN_MAX_PENDING_FUTURES = 2
_SUBAGENT_SCAN_SEMAPHORES: WeakKeyDictionary[
    asyncio.AbstractEventLoop,
    asyncio.Semaphore,
] = WeakKeyDictionary()


def _subagent_scan_semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    sem = _SUBAGENT_SCAN_SEMAPHORES.get(loop)
    if sem is None:
        sem = asyncio.Semaphore(_SUBAGENT_SCAN_MAX_PENDING_FUTURES)
        _SUBAGENT_SCAN_SEMAPHORES[loop] = sem
    return sem


# ============================================================================
# JsonlEventTailer — provider-agnostic base
# ============================================================================
async def _race_readline(
    stream: asyncio.StreamReader, stop_event: asyncio.Event,
) -> Optional[bytes]:
    """Race `stream.readline()` against `stop_event.wait()`. Returns the
    line bytes on a real read, or None if stop won OR the stream
    returned empty (EOF / tail exited).
    INVARIANT: cancels the loser, never leaks pending tasks."""
    read_task = asyncio.create_task(stream.readline())
    stop_task = asyncio.create_task(stop_event.wait())
    try:
        done, _ = await asyncio.wait(
            [read_task, stop_task], return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        for t in (read_task, stop_task):
            if not t.done():
                t.cancel()
    if stop_task in done and read_task not in done:
        return None
    line_bytes = read_task.result()
    return line_bytes if line_bytes else None


class JsonlEventTailer(ABC):
    """Common machinery for tailing a jsonl event source.

    Subclasses pick how lines arrive (`_open_source` + `_next_line`) and
    how to parse each line into an event dict (`_decode_line`). The base
    owns the run loop, the stop event, the cursor counter, and dispatch
    error trapping. Subclasses also get `_start_side_watchers` /
    `_teardown` hooks for source-specific lifecycle work (subprocess
    cleanup, sibling watchers, etc.).

    Cancel contract: `stop()` sets an internal `_stop_event`. Subclass
    `_next_line` MUST race source-IO against the event and return None
    when stop wins. The base finally-block always calls `_teardown` so
    subclasses don't have to.

    Durability contract: `processed_offset` advances ONLY after `dispatch`
    returns without raising. A raising dispatch triggers a bounded
    jittered-exponential retry (`_DISPATCH_RETRY_BACKOFF`); the final
    failure halts the tailer (sets `_stop_event`, logs critical). On
    next start the un-advanced cursor re-reads the failing line — uuid
    dedup in `event_ingester` makes the eventual re-ingest idempotent.
    Blank lines and decode-failures advance the cursor (the line was
    read, there's nothing to dispatch; retrying would loop forever).
    """

    # Jittered exponential backoff for dispatch retries. Designed to
    # ride out APFS/ext4 fsync pressure spikes (often 5–15s under load)
    # without an automatic give-up that loses data. Final attempt is
    # ~30s; after that the tailer halts and requires operator restart.
    _DISPATCH_RETRY_BACKOFF: tuple[float, ...] = (0.1, 1.0, 5.0, 15.0, 30.0)
    _DISPATCH_JITTER = 0.3  # ±30%

    def __init__(
        self,
        *,
        path: Path,
        start_offset: int,
        dispatch: Callable[[dict], Any],
        on_cursor_advance: Optional[Callable[[int], None]] = None,
    ) -> None:
        self.path = path
        self.start_offset = max(0, int(start_offset))
        self.dispatch = dispatch
        self.on_cursor_advance = on_cursor_advance
        self.processed_offset = self.start_offset
        self._stop_event = asyncio.Event()

    async def _call_dispatch(self, event: dict) -> None:
        result = self.dispatch(event)
        if inspect.isawaitable(result):
            await result

    def stop(self) -> None:
        self._stop_event.set()

    async def run(self) -> None:
        try:
            opened = await self._open_source()
        except Exception:
            logger.exception(
                "%s: failed to open source for %s",
                type(self).__name__, self.path,
            )
            return
        if not opened:
            await self._teardown()
            return

        side_tasks = await self._start_side_watchers()
        try:
            while not self._stop_event.is_set():
                raw_line = await self._next_line()
                if raw_line is None:
                    break
                if not raw_line:
                    # blank line: consumed, nothing to dispatch.
                    self.processed_offset = self._advance_cursor(raw_line)
                    await self._notify_cursor()
                    continue
                event = self._decode_line(raw_line)
                if event is None:
                    # malformed source line: consumed, retrying would
                    # loop forever — log and move on.
                    logger.warning(
                        "%s: undecodable line skipped in %s",
                        type(self).__name__, self.path,
                    )
                    self.processed_offset = self._advance_cursor(raw_line)
                    await self._notify_cursor()
                    continue
                if not await self._dispatch_with_retry(event):
                    # All retries exhausted: halt so the un-advanced
                    # cursor preserves this line for the next start.
                    logger.critical(
                        "%s: dispatch FAILED after %d attempts; halting "
                        "tailer for %s (cursor=%d). Restart to retry.",
                        type(self).__name__,
                        len(self._DISPATCH_RETRY_BACKOFF),
                        self.path, self.processed_offset,
                    )
                    self._stop_event.set()
                    break
                self.processed_offset = self._advance_cursor(raw_line)
                await self._notify_cursor()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "%s: run loop crashed for %s",
                type(self).__name__, self.path,
            )
        finally:
            for t in side_tasks:
                t.cancel()
            for t in side_tasks:
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
            await self._teardown()

    async def _dispatch_with_retry(self, event: dict) -> bool:
        """Dispatch with bounded jittered-exponential retry. Returns
        True on success, False on final failure. Cancellable via
        `_stop_event` between retries so a graceful shutdown isn't
        delayed by a stuck line."""
        attempts = self._DISPATCH_RETRY_BACKOFF
        for i, base in enumerate(attempts):
            try:
                await self._call_dispatch(event)
                return True
            except Exception:
                logger.exception(
                    "%s: dispatch attempt %d/%d failed in %s",
                    type(self).__name__, i + 1, len(attempts), self.path,
                )
            if i + 1 == len(attempts):
                break
            jitter = 1.0 + random.uniform(
                -self._DISPATCH_JITTER, self._DISPATCH_JITTER,
            )
            delay = base * jitter
            sleep_task = asyncio.create_task(asyncio.sleep(delay))
            stop_task = asyncio.create_task(self._stop_event.wait())
            try:
                done, _ = await asyncio.wait(
                    [sleep_task, stop_task],
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                for t in (sleep_task, stop_task):
                    if not t.done():
                        t.cancel()
            if stop_task in done:
                return False
        return False

    async def _notify_cursor(self) -> None:
        if self.on_cursor_advance is None:
            return
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                _CURSOR_EXECUTOR,
                self.on_cursor_advance,
                self.processed_offset,
            )
        except Exception:
            logger.exception(
                "%s: on_cursor_advance raised", type(self).__name__,
            )

    def _advance_cursor(self, raw_line: Any) -> int:
        return self.processed_offset + 1

    # ── Abstract hooks ────────────────────────────────────────────────
    @abstractmethod
    async def _open_source(self) -> bool:
        """Open the underlying source (spawn subprocess, validate path,
        etc.). Return True if the source is ready to read from; False
        to abort the run cleanly (teardown still fires)."""

    @abstractmethod
    async def _next_line(self) -> Optional[str]:
        """Return the next raw line (without trailing newline), or None
        when the source is exhausted OR `_stop_event` was set. MUST race
        IO against `self._stop_event.wait()`."""

    @abstractmethod
    def _decode_line(self, raw_line: str) -> Optional[dict]:
        """Parse + optionally enrich one raw line. Return None to skip
        the line (e.g. malformed JSON)."""

    async def _start_side_watchers(self) -> list[asyncio.Task]:
        """Hook for source-specific sibling tasks (subagent watchers, etc.).
        Default: none. Returned tasks are cancelled in the run() finally."""
        return []

    async def _teardown(self) -> None:
        """Source-specific cleanup (subprocess kill, file handle close).
        Default: no-op."""
        return None


class _FileTailFollower:
    """Pure-Python stand-in for `tail -n +K -F <path>`, used where the
    `tail` binary isn't available (Windows).

    Implements just the slice of ``asyncio.subprocess.Process`` the tailers
    and ``_terminate`` touch: ``.stdout`` (a StreamReader fed with file
    bytes), ``.returncode``, ``.terminate()`` / ``.kill()`` and ``.wait()``.
    Follows appends by polling, and — like `tail -F` — restarts from the
    top if the file is truncated or recreated (its size shrinks), and
    keeps waiting if the file is briefly missing."""

    _POLL = 0.1  # seconds

    def __init__(self, path: Path, start_line: int):
        self._path = path
        self._skip = max(0, start_line - 1)  # lines to drop before emitting
        # Match the `tail` spawn's huge limit so readline() never raises
        # LimitOverrunError on the large lines Claude jsonls produce.
        self.stdout: asyncio.StreamReader = asyncio.StreamReader(limit=sys.maxsize)
        self.returncode: Optional[int] = None
        self._stop = asyncio.Event()
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> "_FileTailFollower":
        self._task = asyncio.create_task(
            self._run(), name=f"file-follow-{self._path.name[:12]}"
        )
        return self

    async def _run(self) -> None:
        pos = 0
        skip = self._skip
        try:
            while not self._stop.is_set():
                try:
                    size = await self._stat_size()
                except OSError:
                    await self._wait_tick()
                    continue
                if size < pos:  # truncated or recreated → restart from top
                    pos, skip = 0, self._skip
                if size > pos:
                    try:
                        data, pos = await self._read_from(pos)
                    except OSError:
                        await self._wait_tick()
                        continue
                    # Drop the first `skip` whole lines (tail's `-n +K`).
                    while skip > 0 and data:
                        nl = data.find(b"\n")
                        if nl == -1:
                            data = b""  # partial skipped line; rest comes later
                            break
                        data = data[nl + 1:]
                        skip -= 1
                    if data:
                        self.stdout.feed_data(data)
                await self._wait_tick()
        finally:
            self.stdout.feed_eof()
            self.returncode = 0

    async def _stat_size(self) -> int:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            _CURSOR_EXECUTOR,
            self._stat_size_sync,
        )

    def _stat_size_sync(self) -> int:
        return self._path.stat().st_size

    async def _read_from(self, pos: int) -> tuple[bytes, int]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            _CURSOR_EXECUTOR,
            self._read_from_sync,
            pos,
        )

    def _read_from_sync(self, pos: int) -> tuple[bytes, int]:
        with open(self._path, "rb") as f:
            f.seek(pos)
            data = f.read()
            return data, f.tell()

    async def _wait_tick(self) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=self._POLL)
        except asyncio.TimeoutError:
            pass

    def terminate(self) -> None:
        self._stop.set()

    # SIGKILL has no gentler/forceful distinction here — both just stop.
    kill = terminate

    async def wait(self) -> int:
        if self._task is not None:
            try:
                await self._task
            except Exception:
                pass
        self.returncode = 0
        return self.returncode


class _AppendOnlyByteFollower:
    _POLL = 0.05

    def __init__(
        self,
        path: Path,
        start_byte: int,
        *,
        on_source_reset: Optional[Callable[[], None]] = None,
    ):
        self._path = path
        self._start_byte = max(0, int(start_byte))
        self._on_source_reset = on_source_reset
        self.stdout: asyncio.StreamReader = asyncio.StreamReader(limit=sys.maxsize)
        self.returncode: Optional[int] = None
        self._stop = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self._inode: Optional[int] = None

    async def start(self) -> "_AppendOnlyByteFollower":
        self._task = asyncio.create_task(
            self._run(), name=f"byte-follow-{self._path.name[:12]}"
        )
        return self

    async def _run(self) -> None:
        pos = self._start_byte
        try:
            while not self._stop.is_set():
                try:
                    st = await self._stat()
                except OSError:
                    await self._wait_tick()
                    continue
                inode = getattr(st, "st_ino", None)
                if self._inode is None:
                    self._inode = inode
                if inode != self._inode or st.st_size < pos:
                    logger.warning(
                        "Claude jsonl changed while tailing %s "
                        "(inode %s->%s, size=%d, cursor=%d); rewinding",
                        self._path, self._inode, inode, st.st_size, pos,
                    )
                    self._inode = inode
                    pos = 0
                    if self._on_source_reset is not None:
                        self._on_source_reset()
                if st.st_size > pos:
                    try:
                        data, pos = await self._read_from(pos)
                    except OSError:
                        await self._wait_tick()
                        continue
                    if data:
                        self.stdout.feed_data(data)
                await self._wait_tick()
        finally:
            self.stdout.feed_eof()
            self.returncode = 0

    async def _stat(self) -> os.stat_result:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            _CURSOR_EXECUTOR,
            self._path.stat,
        )

    async def _read_from(self, pos: int) -> tuple[bytes, int]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            _CURSOR_EXECUTOR,
            self._read_from_sync,
            pos,
        )

    def _read_from_sync(self, pos: int) -> tuple[bytes, int]:
        with open(self._path, "rb") as f:
            f.seek(pos)
            data = f.read()
            return data, f.tell()

    async def _wait_tick(self) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=self._POLL)
        except asyncio.TimeoutError:
            pass

    def terminate(self) -> None:
        self._stop.set()

    kill = terminate

    async def wait(self) -> int:
        if self._task is not None:
            try:
                await self._task
            except Exception:
                pass
        self.returncode = 0
        return self.returncode


async def _spawn_byte_follower(
    path: Path,
    *,
    start_byte: int,
    on_source_reset: Optional[Callable[[], None]] = None,
):
    return await _AppendOnlyByteFollower(
        path,
        start_byte,
        on_source_reset=on_source_reset,
    ).start()


async def _spawn_tail(path: Path, *, start_line: int = 1):
    """Start following `path` from `start_line` (1-indexed, `tail -n +K`).

    POSIX uses the `tail -F` binary — it handles truncation, rotation and
    file-recreation for free. Where `tail` isn't available (Windows) a
    pure-Python `_FileTailFollower` provides the same follow semantics.

    `limit=sys.maxsize` raises asyncio's default 64KB StreamReader buffer
    so `readline()` doesn't raise `LimitOverrunError` on the large lines
    Claude jsonls produce (tool_results / attachments / base64 images
    routinely run hundreds of KB). Memory stays bounded by backpressure
    (pause threshold = 2*limit only while an oversized line is in flight)."""
    if os.name == "nt":
        follower = _FileTailFollower(path, start_line)
        return await follower.start()
    return await asyncio.create_subprocess_exec(
        "tail", "-n", f"+{max(1, start_line)}", "-F", str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        limit=sys.maxsize,
    )


async def _terminate(proc: Optional[asyncio.subprocess.Process]) -> None:
    if proc is None or proc.returncode is not None:
        return
    try:
        proc.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=2.0)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            await proc.wait()
        except Exception:
            pass


# ============================================================================
# ClaudeJsonlTailer — concrete: tail -F on a Claude CLI jsonl
# ============================================================================
class ClaudeJsonlTailer(JsonlEventTailer):
    """Tails one claude CLI session jsonl, dispatching each enriched line.

    Concrete subclass of `JsonlEventTailer`:
      - `_open_source`     → seek to the saved byte offset and follow appends
      - `_next_line`       → race readline against stop
      - `_decode_line`     → `enrich_jsonl_line` (parent_tool_use_id, etc.)
      - `_start_side_watchers` → spawn the subagent-meta watcher
      - `_teardown`        → terminate the tail subprocess

    Subagent fan-out: when an Agent/Task `tool_use` is seen, the enclosing
    tool_use_id is registered with the shared `subagent_registry`. A side
    coroutine watches `<jsonl_dir>/<jsonl_stem>/subagents/` for new
    `agent-*.meta.json` files and spawns a child tailer per match,
    sharing the registry so nested Agent calls also nest. Sub-tailers
    set `inject_parent_tool_use_id` so all their events carry the
    enclosing Agent's tool_use_id.

    The enrichment + subagent-registry types are imported lazily from
    `provider_claude` to keep this module dependency-light.
    """

    _SUB_DIR_POLL_INTERVAL = 0.2  # subagent meta files appear once per Agent call
    _SUB_DIR_IDLE_POLL_INTERVAL = 1.5
    _SUB_DIR_IDLE_BACKOFF = 1.6
    _SUB_DIR_PENDING_FAST_SECONDS = 10.0
    _active_sub_tailer_keys: set[tuple[str, str]] = set()
    _active_sub_tailer_lock = threading.Lock()

    def __init__(
        self,
        *,
        path: Path,
        start_offset: int,
        dispatch: Callable[[dict], Any],
        on_cursor_advance: Optional[Callable[[int], None]] = None,
        subagent_registry: Optional[Any] = None,
        inject_parent_tool_use_id: Optional[str] = None,
        is_subagent: bool = False,
    ) -> None:
        super().__init__(
            path=path,
            start_offset=start_offset,
            dispatch=dispatch,
            on_cursor_advance=on_cursor_advance,
        )
        self.inject_parent_tool_use_id = inject_parent_tool_use_id
        self.is_subagent = is_subagent

        self._uuid_to_tool_use_ids: dict[str, list[str]] = {}
        self._uuid_to_parent_uuid: dict[str, str] = {}
        self.subagent_registry = subagent_registry or _SubagentRegistry()
        self._sub_tasks: list[asyncio.Task] = []
        self._known_meta_files: set[str] = set()
        self._known_workflow_dirs: set[str] = set()
        self._subagent_scan_wakeup: Optional[asyncio.Event] = None
        self._subagent_pending_fast_until = 0.0

        self._proc: Optional[asyncio.subprocess.Process] = None

    async def _open_source(self) -> bool:
        try:
            self._proc = await _spawn_byte_follower(
                self.path,
                start_byte=self.start_offset,
                on_source_reset=self._reset_processed_offset,
            )
        except Exception:
            logger.exception(
                "ClaudeJsonlTailer: failed to spawn tail for %s", self.path,
            )
            return False
        return True

    def _reset_processed_offset(self) -> None:
        self.processed_offset = 0
        if self.on_cursor_advance is not None:
            self.on_cursor_advance(0)

    async def _next_line(self) -> Optional[bytes]:
        if self._proc is None or self._proc.stdout is None:
            return None
        line_bytes = await _race_readline(self._proc.stdout, self._stop_event)
        if line_bytes is None:
            return None  # stop OR tail exited (file removed permanently, or signal).
        return line_bytes

    def _decode_line(self, raw_line: bytes) -> Optional[dict]:
        decoded = raw_line.decode("utf-8", errors="replace").rstrip("\n")
        pending_before = self._subagent_pending_count()
        ev = enrich_jsonl_line(
            decoded,
            self._uuid_to_tool_use_ids,
            self._uuid_to_parent_uuid,
            self.subagent_registry,
            parent_tool_use_id=self.inject_parent_tool_use_id,
        )
        if ev is None:
            return None
        if self._subagent_pending_count() > pending_before:
            self._mark_subagent_pending_fast()
        # dispatch expects the inner enriched dict (not the
        # {"type": "agent_message", "data": ...} wrapper).
        return ev["data"]

    def _advance_cursor(self, raw_line: bytes) -> int:
        return self.processed_offset + len(raw_line)

    async def _start_side_watchers(self) -> list[asyncio.Task]:
        task = asyncio.create_task(
            self._watch_subagents(),
            name=f"claude-tailer-subwatch-{self.path.name[:12]}",
        )
        return [task, *self._sub_tasks]

    async def _teardown(self) -> None:
        # Final-poll drain for subagents whose meta file landed within
        # the watcher's last poll window (_SUB_DIR_POLL_INTERVAL). Those
        # never got a sub-tailer; read their jsonl synchronously and
        # dispatch each enriched line so we don't lose the subagent's
        # work on a graceful parent stop.
        try:
            await self._final_drain_subagents()
        except Exception:
            logger.exception(
                "ClaudeJsonlTailer: subagent final-drain failed for %s",
                self.path,
            )
        # Cancel any in-flight sub-tailers that were spawned during the
        # run (added to self._sub_tasks after _start_side_watchers ran).
        for t in self._sub_tasks:
            if not t.done():
                t.cancel()
        for t in self._sub_tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        await _terminate(self._proc)

    def _prune_done_sub_tasks(self) -> None:
        """Drop completed sub-tailer tasks so `_sub_tasks` can't grow
        without bound over a long-lived tailer — one task is spawned per
        Agent/Task subagent call (in `_watch_subagents`) and is otherwise
        only cleared at run end. Retrieving
        `.exception()` on a finished, non-cancelled task marks it retrieved
        so a crashed sub-tailer doesn't emit an 'exception never retrieved'
        warning at GC; sub-tailer crashes are already logged inside `run()`."""
        if not self._sub_tasks:
            return
        live: list[asyncio.Task] = []
        for t in self._sub_tasks:
            if not t.done():
                live.append(t)
            elif not t.cancelled():
                t.exception()
        self._sub_tasks = live

    async def _final_drain_subagents(self) -> None:
        """Dispatch any subagent jsonl whose meta file
        appeared after the watcher's last poll. Idempotent against
        meta files we already spawned sub-tailers for — those are in
        `_known_meta_files`. Uses `enrich_jsonl_line` with the agent's
        bound `parent_tool_use_id` so dispatched lines carry the right
        ancestry. Sub-agent UUIDs deduped downstream by event_ingester.

        Also drains workflow subagents from `subagents/workflows/wf_*/`.
        """
        sub_dir = self._subagents_dir()
        if not sub_dir.exists():
            return
        # Direct Agent/Task subagents
        for meta_path in sub_dir.glob("agent-*.meta.json"):
            key = str(meta_path)
            if key in self._known_meta_files:
                continue
            agent_id = meta_path.name[len("agent-"):-len(".meta.json")]
            jsonl_path = sub_dir / f"agent-{agent_id}.jsonl"
            if not jsonl_path.exists():
                continue
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            parent_tuid = self.subagent_registry.claim(
                meta.get("agentType", "") or "",
                meta.get("description", "") or "",
            )
            if parent_tuid is None:
                continue
            self._known_meta_files.add(key)
            await self._drain_agent_jsonl(jsonl_path, parent_tuid)
        # Workflow subagents
        wf_base = sub_dir / "workflows"
        if wf_base.exists():
            for wf_path in sorted(wf_base.iterdir()):
                if not wf_path.is_dir() or not wf_path.name.startswith("wf_"):
                    continue
                run_id = wf_path.name
                # Bind if not yet bound
                if run_id not in self.subagent_registry._workflow_bindings:
                    self.subagent_registry.claim_workflow(run_id)
                parent_tuid = self.subagent_registry.get_workflow_parent(run_id)
                if not parent_tuid:
                    continue
                for meta_path in wf_path.glob("agent-*.meta.json"):
                    key = str(meta_path)
                    if key in self._known_meta_files:
                        continue
                    agent_id = meta_path.name[len("agent-"):-len(".meta.json")]
                    jsonl_path = wf_path / f"agent-{agent_id}.jsonl"
                    if not jsonl_path.exists():
                        continue
                    self._known_meta_files.add(key)
                    await self._drain_agent_jsonl(jsonl_path, parent_tuid)

    async def _drain_agent_jsonl(self, jsonl_path: Path, parent_tuid: str) -> None:
        """Read an agent jsonl end-to-end and dispatch each enriched line."""
        try:
            with jsonl_path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.rstrip("\n")
                    if not line:
                        continue
                    ev = enrich_jsonl_line(
                        line,
                        {}, {},  # fresh dicts — this is a one-shot drain
                        self.subagent_registry,
                        parent_tool_use_id=parent_tuid,
                    )
                    if ev is None:
                        continue
                    try:
                        await self._call_dispatch(ev["data"])
                    except Exception:
                        logger.exception(
                            "subagent final-drain dispatch failed for %s",
                            jsonl_path,
                        )
        except OSError:
            pass

    # --- subagent fan-out ----------------------------------------------
    def _subagents_dir(self) -> Path:
        return self.path.parent / self.path.stem / "subagents"

    def _subagent_pending_count(self) -> int:
        return len(getattr(self.subagent_registry, "_pending", ()))

    def _wake_subagent_scan(self) -> None:
        wakeup = self._subagent_scan_wakeup
        if wakeup is not None:
            wakeup.set()

    def _mark_subagent_pending_fast(self) -> None:
        self._subagent_pending_fast_until = (
            time.monotonic() + self._SUB_DIR_PENDING_FAST_SECONDS
        )
        self._wake_subagent_scan()

    def _has_fresh_subagent_pending(self) -> bool:
        return (
            self._subagent_pending_count() > 0
            and time.monotonic() <= self._subagent_pending_fast_until
        )

    def _should_scan_subagents(self) -> bool:
        return self._subagent_pending_count() > 0 or bool(self._known_workflow_dirs)

    def _next_subagent_poll_interval(
        self,
        current_interval: float,
        *,
        active: bool,
    ) -> float:
        if active:
            return self._SUB_DIR_POLL_INTERVAL
        return min(
            self._SUB_DIR_IDLE_POLL_INTERVAL,
            max(
                self._SUB_DIR_POLL_INTERVAL,
                current_interval * self._SUB_DIR_IDLE_BACKOFF,
            ),
        )

    async def _watch_subagents(self) -> None:
        """Poll the subagents directory for new `agent-*.meta.json` files,
        match them against pending Agent/Task tool_uses, and spawn a
        sub-tailer per matched file. Also watches `subagents/workflows/`
        for Workflow subagents. Polling here is fine — meta files arrive
        once per Agent/Workflow call, not continuously."""
        sub_dir = self._subagents_dir()
        poll_interval = self._SUB_DIR_POLL_INTERVAL
        wakeup = asyncio.Event()
        self._subagent_scan_wakeup = wakeup
        try:
            while not self._stop_event.is_set():
                self._prune_done_sub_tasks()
                loop = asyncio.get_running_loop()
                if self._should_scan_subagents():
                    known_meta_files = frozenset(self._known_meta_files)
                    async with _subagent_scan_semaphore():
                        with perf.timed("tailer.subagent_scan"):
                            invalid_meta, direct, workflows = await loop.run_in_executor(
                                _SUBAGENT_SCAN_EXECUTOR,
                                self._scan_subagent_files,
                                sub_dir,
                                known_meta_files,
                            )
                else:
                    invalid_meta, direct, workflows = [], [], []
                applied = self._apply_subagent_scan(invalid_meta, direct, workflows)
                if applied > 0:
                    self._mark_subagent_pending_fast()
                active = applied > 0 or self._has_fresh_subagent_pending()
                poll_interval = self._next_subagent_poll_interval(
                    poll_interval,
                    active=active,
                )
                try:
                    await asyncio.wait_for(wakeup.wait(), timeout=poll_interval)
                    wakeup.clear()
                    poll_interval = self._SUB_DIR_POLL_INTERVAL
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "ClaudeJsonlTailer: subagent watcher crashed for %s", self.path,
            )
        finally:
            if self._subagent_scan_wakeup is wakeup:
                self._subagent_scan_wakeup = None

    def _scan_subagent_files(
        self,
        sub_dir: Path,
        known_meta_files: frozenset[str],
    ) -> tuple[
        list[str],
        list[tuple[str, Path, dict]],
        list[tuple[Path, list[tuple[str, Path]]]],
    ]:
        invalid_meta: list[str] = []
        direct: list[tuple[str, Path, dict]] = []
        workflows: list[tuple[Path, list[tuple[str, Path]]]] = []
        if not sub_dir.exists():
            return invalid_meta, direct, workflows

        for meta_path in sub_dir.glob("agent-*.meta.json"):
            key = str(meta_path)
            if key in known_meta_files:
                continue
            agent_id = meta_path.name[
                len("agent-") : -len(".meta.json")
            ]
            jsonl_path = sub_dir / f"agent-{agent_id}.jsonl"
            if not jsonl_path.exists():
                continue
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                invalid_meta.append(key)
                continue
            direct.append((agent_id, jsonl_path, meta))

        wf_base = sub_dir / "workflows"
        if not wf_base.exists():
            return invalid_meta, direct, workflows
        for wf_path in sorted(wf_base.iterdir()):
            if not wf_path.is_dir() or not wf_path.name.startswith("wf_"):
                continue
            agents: list[tuple[str, Path]] = []
            for meta_path in wf_path.glob("agent-*.meta.json"):
                key = str(meta_path)
                if key in known_meta_files:
                    continue
                agent_id = meta_path.name[len("agent-"):-len(".meta.json")]
                jsonl_path = wf_path / f"agent-{agent_id}.jsonl"
                if not jsonl_path.exists():
                    continue
                agents.append((agent_id, jsonl_path))
            workflows.append((wf_path, agents))
        return invalid_meta, direct, workflows

    def _apply_subagent_scan(
        self,
        invalid_meta: list[str],
        direct: list[tuple[str, Path, dict]],
        workflows: list[tuple[Path, list[tuple[str, Path]]]],
    ) -> int:
        applied = 0
        for key in invalid_meta:
            logger.warning(
                "ClaudeJsonlTailer: failed to read subagent meta %s",
                key,
            )
            self._known_meta_files.add(key)
            applied += 1
        for agent_id, jsonl_path, meta in direct:
            key = str(jsonl_path.parent / f"agent-{agent_id}.meta.json")
            if key in self._known_meta_files:
                continue
            parent_tuid = self.subagent_registry.claim(
                meta.get("agentType", "") or "",
                meta.get("description", "") or "",
            )
            if parent_tuid is None:
                continue
            self._spawn_sub_tailer(
                agent_id, jsonl_path, parent_tuid, meta.get("agentType"),
            )
            applied += 1

        for wf_path, agents in workflows:
            wf_key = str(wf_path)
            run_id = wf_path.name
            if wf_key not in self._known_workflow_dirs:
                parent_tuid = self.subagent_registry.claim_workflow(run_id)
                if parent_tuid is None:
                    continue
                self._known_workflow_dirs.add(wf_key)
                logger.info(
                    "ClaudeJsonlTailer: bound workflow %s to tool_use_id=%s",
                    run_id, parent_tuid,
                )
                applied += 1
            parent_tuid = self.subagent_registry.get_workflow_parent(run_id)
            if not parent_tuid:
                continue
            for agent_id, jsonl_path in agents:
                key = str(jsonl_path.parent / f"agent-{agent_id}.meta.json")
                if key in self._known_meta_files:
                    continue
                self._spawn_sub_tailer(
                    agent_id, jsonl_path, parent_tuid, "workflow-subagent",
                )
                applied += 1
        return applied

    def _spawn_sub_tailer(
        self, agent_id: str, jsonl_path: Path, parent_tuid: str, agent_type: str,
    ) -> None:
        """Create, register, and launch a child ClaudeJsonlTailer."""
        meta_key = str(jsonl_path.parent / f"agent-{agent_id}.meta.json")
        self._known_meta_files.add(meta_key)
        self.subagent_registry._bound[agent_id] = parent_tuid
        active_key = (str(jsonl_path), parent_tuid)
        with self._active_sub_tailer_lock:
            if active_key in self._active_sub_tailer_keys:
                logger.debug(
                    "ClaudeJsonlTailer: sub-tailer already active for agent %s "
                    "under tool_use_id=%s",
                    agent_id, parent_tuid,
                )
                return
            self._active_sub_tailer_keys.add(active_key)
        sub_tailer = ClaudeJsonlTailer(
            path=jsonl_path,
            start_offset=0,
            dispatch=self.dispatch,
            on_cursor_advance=None,
            subagent_registry=self.subagent_registry,
            inject_parent_tool_use_id=parent_tuid,
            is_subagent=True,
        )
        task = asyncio.create_task(
            sub_tailer.run(),
            name=f"claude-tailer-sub-{agent_id[:8]}",
        )
        task.add_done_callback(
            lambda _task, key=active_key: self._release_active_sub_tailer_key(key)
        )
        self._sub_tasks.append(task)
        logger.info(
            "ClaudeJsonlTailer: spawned sub-tailer for agent %s "
            "(type=%s) under tool_use_id=%s",
            agent_id, agent_type, parent_tuid,
        )

    @classmethod
    def _release_active_sub_tailer_key(cls, key: tuple[str, str]) -> None:
        with cls._active_sub_tailer_lock:
            cls._active_sub_tailer_keys.discard(key)


# ============================================================================
# GeminiJsonlTailer — concrete: polling read on runner-written events file
# ============================================================================
class GeminiJsonlTailer(JsonlEventTailer):
    """Tails a `session_events.jsonl` file written by the Gemini runner.

    Differs from `ClaudeJsonlTailer`:
      - No `tail -F` subprocess. Polling read (file lives in our own
        run dir, no atomic-rename / truncate races to worry about).
      - No enrichment. Runner writes already-normalized events.
      - No sub-source watchers.

    The polling cadence is set by `_POLL_INTERVAL`. Cancel responsiveness
    is bounded by the poll period — fine for Gemini's coarse-grained
    event cadence; if the file ever needs sub-100ms latency, swap to
    `inotify` / `kqueue`.
    """

    _POLL_INTERVAL = 0.05

    def __init__(
        self,
        *,
        path: Path,
        start_offset: int,
        dispatch: Callable[[dict], Any],
        on_cursor_advance: Optional[Callable[[int], None]] = None,
    ) -> None:
        super().__init__(
            path=path,
            start_offset=start_offset,
            dispatch=dispatch,
            on_cursor_advance=on_cursor_advance,
        )
        # Per-pass line buffer drained into _next_line one at a time.
        self._pending_lines: list[str] = []

    async def _open_source(self) -> bool:
        # Polling read needs no eager open. We just confirm the path
        # makes sense; the file is allowed to not exist yet.
        return True

    async def _next_line(self) -> Optional[str]:
        # Return a buffered line if we have one. Otherwise scan the file
        # for new lines past our cursor, buffer them, and sleep until
        # next poll. Race the sleep against stop so cancel is prompt.
        if self._pending_lines:
            return self._pending_lines.pop(0)
        while not self._stop_event.is_set():
            new_lines = await asyncio.to_thread(self._read_new_lines)
            if new_lines:
                self._pending_lines = new_lines[1:]
                return new_lines[0]
            sleep_task = asyncio.create_task(
                asyncio.sleep(self._POLL_INTERVAL),
            )
            stop_task = asyncio.create_task(self._stop_event.wait())
            try:
                done, _ = await asyncio.wait(
                    [sleep_task, stop_task], return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                for t in (sleep_task, stop_task):
                    if not t.done():
                        t.cancel()
            if stop_task in done:
                return None
        return None

    def _read_new_lines(self) -> list[str]:
        """Read every new line past `processed_line` (1-indexed cursor).
        Returns the list of newly available raw lines; mutates nothing
        except a quick file scan."""
        if not self.path.exists():
            return []
        try:
            with self.path.open("r", encoding="utf-8") as f:
                # Skip already-processed lines. processed_line is the
                # count of lines we've already emitted via _next_line
                # (incremented by the base after each dispatch); the
                # NEXT line to emit is line-index processed_line (0-
                # indexed) so we skip exactly that many.
                for _ in range(self.processed_offset):
                    if f.readline() == "":
                        return []
                return [
                    line.rstrip("\n")
                    for line in f
                    if line and not line.isspace()
                ]
        except OSError:
            return []

    def _decode_line(self, raw_line: str) -> Optional[dict]:
        try:
            parsed = json.loads(raw_line)
        except json.JSONDecodeError:
            return None
        if isinstance(parsed, dict):
            # Stamp `parent_tool_use_id` to match what `enrich_jsonl_line`
            # produces in the claude path. Gemini has no sidechain / Agent
            # nesting so the value is always None, but the FIELD must be
            # present so live-streamed events are byte-identical to events
            # replayed through run_recovery (which DOES run enrich and
            # therefore adds the key). Diverging shapes between live and
            # replay paths break the single-code-path invariant.
            parsed.setdefault("parent_tool_use_id", None)
        return parsed


# ============================================================================
# OwnedClaudeJsonlTailer — refcount-managed wrapper used by coordinator
# ============================================================================
class OwnedClaudeJsonlTailer:
    """One refcount-managed ClaudeJsonlTailer for a single claude jsonl.

    Starts on the first `acquire()`, stops on the last `release()`. The
    tailer's only side-effect is ingesting each enriched line via
    `event_ingester.ingest` — `BetterAgentJsonlTailer` then picks the
    line up from events.jsonl and broadcasts to WS subscribers, so this
    class is decoupled from the WS layer.

    `start_offset` is set once at first-acquire time (typically the
    session's `processed_line_by_sid[agent_sid]`); subsequent acquires
    of an already-running tailer don't rewind. `event_ingester` dedups
    by uuid, so any overlap with another simultaneous tailer (e.g. an
    active-run tailer started by `_bootstrap_run`) is harmless.
    """

    def __init__(
        self,
        *,
        root_id: str,
        app_session_id: str,
        agent_sid: str,
        jsonl_path: Path,
        start_offset: int,
    ) -> None:
        self.root_id = root_id
        self.app_session_id = app_session_id
        self.agent_sid = agent_sid
        self.jsonl_path = jsonl_path
        self.start_offset = max(0, int(start_offset))
        self._refcount = 0
        self._tailer: Optional[ClaudeJsonlTailer] = None
        self._task: Optional[asyncio.Task] = None
        self._cursor_persisted = self.start_offset
        self._cursor_pending = self.start_offset
        self._cursor_persisted_at = time.monotonic()
        self._owner_token = None
        self._unsubscribe_owner_revoked: Optional[Callable[[], None]] = None
        self._owner_retired = False

    def _owner_revoked(self) -> None:
        if self._tailer is not None:
            self._tailer.stop()
        self._owner_token = None
        self._unsubscribe_owner_revoked = None
        self._owner_retired = True

    def _ensure_owner_token(self):
        if self._owner_retired:
            return None
        if self._owner_token is not None:
            return self._owner_token
        token = session_manager.claim_owner(self.app_session_id)
        if token is None:
            return None
        self._owner_token = token
        self._unsubscribe_owner_revoked = session_manager.subscribe_owner_revoked(
            token, self._owner_revoked,
        )
        return token

    @perf.timed_fn("tailer.dispatch")
    async def _dispatch(self, enriched: dict) -> None:
        # Two routing branches keyed on whether `self.agent_sid` is the
        # session's PRIMARY agent (manager / native / supervisor):
        #
        #   • Primary agent → funnel through `apply_event` (streaming
        #     msg present) or `ingest_orphan` (no streaming msg). This
        #     is the single ingestion path for primary-CLI events; live
        #     ingest, recovery replay, and this tail-side fallback all
        #     share `OrchestrationStrategy.apply_event`. Dedup at
        #     `event_ingester` (uid + sha256(data)) keeps overlap with
        #     the orchestrator's live writes idempotent.
        #
        #   • Worker-fork agent → direct journal write stamped with the
        #     FORK's identity (`sid=agent_sid`, no msg_id,
        #     `source=FORK_BACKUP_SOURCE`). See the worker-fork branch
        #     at the bottom of this method for the rationale (the tailer
        #     was constructed with `app_session_id=PARENT_app_session_id`
        #     and cannot safely route through `apply_event(msg=parent_…)`).
        # `get_lite` skips the per-message events deepcopy; _dispatch only
        # reads top-level metadata (*_agent_session_id, orchestration_mode,
        # messages[].isStreaming / .id), none of which live in events lists.
        sess = await asyncio.to_thread(session_manager.get_lite, self.app_session_id) or {}
        primary_sids = {
            sess.get("agent_session_id"),
            sess.get("supervisor_agent_session_id"),
        }
        primary_sids.discard(None)
        is_primary = self.agent_sid in primary_sids

        if is_primary:
            from orchs import ApplyEventCtx, get_strategy
            event = {"type": "agent_message", "data": enriched}
            mode = sess.get("orchestration_mode") or "team"
            strategy = get_strategy(mode)
            ctx = ApplyEventCtx(root_id=self.root_id)
            try:
                # Always use ingest_orphan — never apply_event.
                # The per-RUN ClaudeJsonlTailer (started by
                # provider_claude.py) feeds the orchestrator's queue,
                # which routes through save_ws_callback → apply_event
                # with the correct assistant message.  This tailer is a
                # backup: it must only write to events.jsonl (via
                # ingest_orphan) so the WS wire tailer can broadcast
                # catch-up frames.  Routing through apply_event here
                # would graft stale events from a previous turn onto the
                # current turn's streaming message — the per-message
                # dedup in apply_event only checks the target message,
                # so UUIDs already present in a prior message would pass
                # through undetected.
                token = self._ensure_owner_token()
                if token is None:
                    return
                accepted, _ = await asyncio.to_thread(
                    session_manager.run_if_owner,
                    token,
                    lambda: strategy.ingest_orphan(
                        app_session_id=self.app_session_id,
                        event=event,
                        ctx=ctx,
                        source_is_provider_stream=True,
                    ),
                )
                if not accepted:
                    self._owner_revoked()
            except Exception:
                logger.exception(
                    "OwnedClaudeJsonlTailer: ingest_orphan failed for %s",
                    self.jsonl_path,
                )
            return

        # Worker-fork branch: crash-window backup write. The tailer was
        # constructed with `app_session_id=PARENT_app_session_id`, so
        # routing through `apply_event(msg=parent_streaming_msg)` would
        # graft worker raw SDK lines onto the parent manager's events
        # list. The row carries the FORK's identity (`sid=agent_sid`,
        # no msg_id) and `source=FORK_BACKUP_SOURCE`, which every
        # ownership-resolution / hydrate / message-read path excludes —
        # fork rows can never attach to a parent message. The worker's
        # own `apply_event` (driven by the delegation MCP turn) is the
        # primary producer for fork events; these rows are durable
        # backup only.
        try:
            from event_journal import FORK_BACKUP_SOURCE, publish_event_sync
            token = self._ensure_owner_token()
            if token is None:
                return
            accepted, _ = await asyncio.to_thread(
                session_manager.run_if_owner,
                token,
                lambda: publish_event_sync(
                    session_id=self.root_id,
                    context_id=self.agent_sid,
                    event_type="agent_message",
                    data=enriched,
                    source=FORK_BACKUP_SOURCE,
                ),
            )
            if not accepted:
                self._owner_revoked()
        except Exception:
            logger.exception(
                "OwnedClaudeJsonlTailer: ingest failed for %s",
                self.jsonl_path,
            )

    def _on_cursor(self, line_count: int) -> None:
        """Persist `processed_line_by_sid[agent_sid] = line_count` so a
        subsequent acquire (e.g. after backend restart) starts past the
        already-ingested prefix instead of re-reading the whole file."""
        n = int(line_count)
        from orchs.jsonl_helpers import note_jsonl_append
        note_jsonl_append(self.jsonl_path, n)
        if n < self._cursor_pending:
            self._cursor_pending = n
            self._persist_cursor(n)
            return
        self._cursor_pending = max(self._cursor_pending, n)
        if (
            self._cursor_pending - self._cursor_persisted < 32
            and time.monotonic() - self._cursor_persisted_at < 1.0
        ):
            return
        self._persist_cursor(self._cursor_pending)

    def _persist_cursor(self, line_count: int) -> None:
        try:
            token = self._ensure_owner_token()
            if token is None:
                return
            accepted, _ = session_manager.run_if_owner(
                token,
                lambda: session_manager.advance_processed_lines(
                    self.app_session_id,
                    self.agent_sid,
                    int(line_count),
                    bump_updated_at=False,
                ),
            )
            if not accepted:
                self._owner_revoked()
                return
            self._cursor_persisted = int(line_count)
            self._cursor_persisted_at = time.monotonic()
        except Exception:
            logger.exception(
                "OwnedClaudeJsonlTailer: cursor persist failed for %s",
                self.jsonl_path,
            )

    def acquire(self) -> None:
        self._refcount += 1
        if self._tailer is None:
            token = self._ensure_owner_token()
            if token is None:
                self._refcount = max(0, self._refcount - 1)
                return
            self._tailer = ClaudeJsonlTailer(
                path=self.jsonl_path,
                start_offset=self.start_offset,
                dispatch=self._dispatch,
                on_cursor_advance=self._on_cursor,
            )
            self._task = asyncio.create_task(
                self._tailer.run(),
                name=f"owned-claude-tailer-{self.agent_sid[:8]}",
            )
            logger.info(
                "OwnedClaudeJsonlTailer started agent_sid=%s start_offset=%d",
                self.agent_sid, self.start_offset,
            )

    def release(self) -> Optional[asyncio.Task]:
        self._refcount = max(0, self._refcount - 1)
        if self._refcount == 0 and self._tailer is not None:
            self._tailer.stop()
            if self._cursor_pending > self._cursor_persisted:
                self._persist_cursor(self._cursor_pending)
            t = self._task
            self._tailer = None
            self._task = None
            if self._unsubscribe_owner_revoked is not None:
                self._unsubscribe_owner_revoked()
                self._unsubscribe_owner_revoked = None
            self._owner_token = None
            self._owner_retired = False
            return t
        return None

    @property
    def alive(self) -> bool:
        return self._tailer is not None


# ============================================================================
# BetterAgentJsonlTailer
# ============================================================================
class _Subscriber:
    """One WS subscriber's per-session watermark state.

    Each subscriber knows the highest seq it has already received
    (`next_seq` is the next seq it expects). On every event:

      - seq < next_seq → already delivered, drop.
      - seq == next_seq → forward, advance.
      - seq > next_seq → gap! Fill the gap from events.jsonl, then forward
        this event. Happens on subscribe (next_seq vs current cursor) and
        in transient races where the tailer's broadcast outraces a new
        subscriber's gap-fill.

    Per-subscriber `_lock` serializes pushes so gap-fill and live events
    can't interleave incorrectly when both arrive concurrently.
    """

    def __init__(
        self,
        *,
        app_session_id: str,
        ws_callback: Callable[[dict], Awaitable[None]],
        from_seq: int,
        root_id: str,
    ) -> None:
        self.app_session_id = app_session_id
        self.ws_callback = ws_callback
        self.next_seq = max(0, int(from_seq)) + 1
        self.root_id = root_id
        self._lock = asyncio.Lock()
        # Set False by `remove_subscriber`. `add_subscriber` and
        # `push_entry` skip work for inactive subs so a disconnect that
        # fires WHILE `_subscribe_to_wire_tailer` is still mid-await
        # can't leave an orphan in `_subscribers`.
        self._active = True

    async def push_entry(self, entry: dict, frame: dict) -> None:
        """Forward `frame` to the ws_callback if `entry`'s seq is current
        or future. Fill any gap from events.jsonl on the way."""
        seq = entry.get("seq")
        if not isinstance(seq, int):
            return
        async with self._lock:
            if seq < self.next_seq:
                return
            if seq > self.next_seq:
                if not await self._fill_gap(seq - 1):
                    return
            if await self._send(frame):
                self.next_seq = seq + 1

    async def catch_up_to(self, target_seq: int) -> None:
        """Synchronously send every missing event up to and including
        `target_seq`. Used on subscribe to drain the from_seq..cursor gap
        before live events flow."""
        async with self._lock:
            if target_seq < self.next_seq:
                return
            await self._fill_gap(target_seq)

    async def _fill_gap(self, until_seq: int) -> bool:
        """Read events.jsonl from `next_seq..until_seq` filtered by sid,
        send each. Caller MUST hold `_lock`."""
        from event_journal import event_journal_reader
        replayed = 0
        rejected = 0
        boundary_reached = False
        started = time.perf_counter()
        while self.next_seq <= until_seq:
            events, _, has_more = await asyncio.to_thread(
                event_journal_reader.read_events,
                self.root_id,
                after_seq=self.next_seq - 1,
                limit=10_000,
                sid_filter=self.app_session_id,
            )
            if not events:
                break
            for e in events:
                seq = e.get("seq")
                if not isinstance(seq, int):
                    rejected = 1
                    break
                if seq > until_seq:
                    boundary_reached = True
                    break
                frame = BetterAgentJsonlTailer._entry_to_ws_frame(e)
                if frame is None:
                    self.next_seq = seq + 1
                    continue
                if not await self._send(frame):
                    rejected = 1
                    break
                self.next_seq = seq + 1
                replayed += 1
            if rejected:
                break
            if boundary_reached:
                break
            if not has_more:
                break
        perf.record(
            "ws.replay.gap_fill",
            (time.perf_counter() - started) * 1000.0,
        )
        perf.record_count("ws.replay.frames", replayed)
        if rejected:
            perf.record_count("ws.replay.rejected")
        return not rejected

    async def _send(self, frame: dict) -> bool:
        try:
            accepted = await self.ws_callback(frame)
            return accepted is not False
        except Exception:
            logger.exception(
                "Subscriber: ws_callback raised for sid=%s",
                self.app_session_id,
            )
            return False


class BetterAgentJsonlTailer:
    """Tails one BC root's `events.jsonl`, broadcasting each new event to
    its registered subscribers.

    Sole live-WS producer for events that flow through events.jsonl.
    Each subscriber owns its own watermark (`from_seq`) and reconciles
    gaps lazily — so REST snapshots and WS streams are stitched together
    by seq number, not by uuid dedup.

    Wire format preserved for backward compat with the frontend: events
    that came in as `agent_message` are wrapped into `manager_event` ->
    `event` shape on the way out. Future cleanup: teach the frontend to
    consume the native shape directly and drop this wrap.
    """

    def __init__(self, *, events_jsonl_path: Path, root_id: str) -> None:
        self.events_jsonl_path = events_jsonl_path
        self.root_id = root_id
        # sid -> list of subscribers. Each WS subscribe adds one entry;
        # a single client subscribed to one session has exactly one entry.
        self._subscribers: dict[str, list[_Subscriber]] = {}
        self._stop_event = asyncio.Event()
        self._proc: Optional[asyncio.subprocess.Process] = None

    def stop(self) -> None:
        self._stop_event.set()

    async def add_subscriber(self, sub: _Subscriber) -> None:
        """Register a subscriber, then drain the gap from its from_seq
        up to current cursor.

        Order matters: append-then-drain. Live events arriving during
        the drain race against `catch_up_to` for the subscriber's
        `_lock`. Whichever acquires first wins — the loser sees
        `next_seq` already past its target / past the live event's
        seq, so the duplicate is dropped or the gap-fill becomes a
        no-op. No event is lost.

        Idempotent / abort-safe: if `sub._active` was flipped False
        between scheduling and arriving here (a fast disconnect during
        `_subscribe_to_wire_tailer`'s await), we skip both registration
        and drain so no orphan ends up in `_subscribers`."""
        if not sub._active:
            return
        from event_journal import event_journal_reader
        self._subscribers.setdefault(sub.app_session_id, []).append(sub)
        cursor = await asyncio.to_thread(event_journal_reader.cursor, self.root_id)
        await sub.catch_up_to(cursor)

    def remove_subscriber(self, sub: _Subscriber) -> None:
        # Mark inactive FIRST so a concurrent `add_subscriber` (still
        # mid-await) won't append this sub after we've already
        # decided to remove it.
        sub._active = False
        lst = self._subscribers.get(sub.app_session_id)
        if not lst:
            return
        try:
            lst.remove(sub)
        except ValueError:
            pass
        if not lst:
            self._subscribers.pop(sub.app_session_id, None)

    def has_subscribers(self) -> bool:
        return any(self._subscribers.values())

    async def run(self) -> None:
        from event_journal import event_journal_reader

        async def _on_entry(entry: dict) -> None:
            sid = entry.get("sid")
            if not isinstance(sid, str):
                return
            frame = self._entry_to_ws_frame(entry)
            if frame is None:
                return
            subs = list(self._subscribers.get(sid, []))
            for sub in subs:
                try:
                    await sub.push_entry(entry, frame)
                except Exception:
                    logger.exception(
                        "BetterAgentJsonlTailer: subscriber push raised",
                    )

        try:
            await event_journal_reader.watch_entries(
                self.root_id,
                self._stop_event,
                _on_entry,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "BetterAgentJsonlTailer: run loop crashed for %s",
                self.events_jsonl_path,
            )

    @staticmethod
    def _entry_to_ws_frame(entry: dict) -> Optional[dict]:
        """Reshape an events.jsonl entry into the WS frame the frontend
        expects.

        events.jsonl entry shape:
          {"seq": int, "ts": str, "sid": str, "type": str, "data": dict, ...}

        WS frame shape:
          - `agent_message` events pass through as-is with app_session_id:
              {"type": "agent_message", "data": {..., "app_session_id": sid}, "seq": N}
          - Legacy `manager_event` rows (pre-migration) are unwrapped to
            agent_message for uniform frontend handling.
          - everything else passes through as `{"type": <type>, "data": <data>, "seq": N}`.

        The top-level `seq` lets the frontend track the high-water mark.
        """
        etype = entry.get("type")
        data = entry.get("data") or {}
        seq = entry.get("seq")
        if not isinstance(etype, str):
            return None

        # Legacy backward compat: unwrap old manager_event rows
        if etype == "manager_event":
            inner = data.get("event") if isinstance(data, dict) else None
            if isinstance(inner, dict):
                inner_data = inner.get("data") if isinstance(inner.get("data"), dict) else {}
                if isinstance(data, dict) and "app_session_id" not in inner_data:
                    inner_data = {**inner_data, "app_session_id": data.get("app_session_id") or entry.get("sid")}
                data = inner_data
                etype = "agent_message"

        # Annotate the frame with the entry's session id and owning
        # assistant message id so the frontend can route each frame to the
        # correct pane and message. `msg_id` is what lets a LATE event —
        # one the provider re-emits after its turn already completed and
        # the run was cleared — be applied to its real, already-finalized
        # message instead of spawning a duplicate placeholder bubble.
        if isinstance(data, dict):
            if "app_session_id" not in data and entry.get("sid"):
                data = {**data, "app_session_id": entry["sid"]}
            msg_id = entry.get("msg_id")
            if isinstance(msg_id, str) and msg_id and "msg_id" not in data:
                data = {**data, "msg_id": msg_id}
        return {"type": etype, "data": data, "seq": seq}
