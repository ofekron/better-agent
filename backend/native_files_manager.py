"""NativeFilesManager — sole owner of the native-CLI-jsonl tailing concern.

Single responsibility: own *which provider CLI jsonl files must be tailed*
for the backend, and the live `OwnedClaudeJsonlTailer`s that tail them.

Four duties (mirrors the design contract):
  1. COLLECT targets from other subsystems' bus firings — never by being
     called imperatively. Supply arrives as bus facts:
       • `session.agent_sid_set`        → a session's primary agent_sid.
       • `native_files.fork_target`     → a worker-fork panel's sid+path.
       • `session.processed_lines_advanced` → resume offset for a sid.
  2. Be the ONLY reader/holder of those targets and their tailers. Nothing
     outside this module constructs an `OwnedClaudeJsonlTailer` or reads
     `processed_line_by_sid` for tail purposes.
  3. Know what still needs tailing vs. what is already handled, and emit
     `native_files.tailing_started` / `native_files.tailing_closed` facts
     so observers react without polling.
  4. Reconcile: tail what is demanded-but-not-running, close what is
     running-but-no-longer-demanded.

Demand (who needs a file tailed) also arrives only as a bus fact —
`native_files.demand` — published by the orchestrator on WS
subscribe/unsubscribe. The manager never reaches back into the
orchestrator.

Cold start: bus events do not replay on restart, so a session's targets
are lazily SEEDED once from the session store (owner state) the first
time demand references it; thereafter the projection is event-only. This
is the standard read-model rebuild-from-owner-state pattern.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import perf
from event_bus import BusEvent, bus
from session_manager import manager as session_manager

logger = logging.getLogger(__name__)

_PRIMARY_JSONL_CACHE_TTL_S = 1.0
_PRIMARY_JSONL_POSITIVE_CACHE_TTL_S = 60.0
_RUN_STATE_LOOKUP_TIMEOUT_S = 1.0
_RUN_STATE_LOOKUP_CACHE_TTL_S = 1.0
_RUN_STATE_LOOKUP_POSITIVE_CACHE_TTL_S = 60.0
_RUN_STATE_LOOKUP_CACHE: dict[tuple[str, str], tuple[float, Optional[Path]]] = {}
_RUN_STATE_LOOKUP_CACHE_LOCK = threading.Lock()
_RUN_STATE_INFLIGHT: dict[tuple[str, str], threading.Event] = {}


@dataclass
class _Target:
    """One tailable native jsonl reachable from `owning` session."""

    owning: str          # app_session_id whose subscribers demand this file
    agent_sid: str
    jsonl_path: Path
    start_offset: int
    can_tail: bool = True
    trigger_event_id: Optional[int] = None
    trigger_event_type: Optional[str] = None
    # For worker forks: the FORK's own Better Agent session id. Two writers feed the
    # fork's resume offset — the delegation prep-skip writes to the FORK BC
    # record (`fork_agent_session_id`, agent_sid=fork_agent_sid), and the
    # tailer's own `_on_cursor` writes to the PARENT (`owning`,
    # agent_sid=fork_agent_sid). Both must be consulted; the authoritative
    # offset is `max(parent_cursor, fork_bc_cursor)`. For primaries, None.
    fork_agent_session_id: Optional[str] = None


def _run_state_cache_get(root_key: str, agent_sid: str) -> Optional[Path] | bool:
    now = time.monotonic()
    key = (root_key, agent_sid)
    with _RUN_STATE_LOOKUP_CACHE_LOCK:
        cached = _RUN_STATE_LOOKUP_CACHE.get(key)
        if cached is None:
            return False
        ts, path = cached
        ttl = (
            _RUN_STATE_LOOKUP_POSITIVE_CACHE_TTL_S
            if path is not None
            else _RUN_STATE_LOOKUP_CACHE_TTL_S
        )
        if now - ts < ttl:
            return path
        _RUN_STATE_LOOKUP_CACHE.pop(key, None)
        return False


def _run_state_cache_put(root_key: str, agent_sid: str, path: Optional[Path]) -> None:
    with _RUN_STATE_LOOKUP_CACHE_LOCK:
        _RUN_STATE_LOOKUP_CACHE[(root_key, agent_sid)] = (time.monotonic(), path)


def _claim_run_state_lookup(root_key: str, agent_sid: str) -> tuple[threading.Event, bool]:
    key = (root_key, agent_sid)
    with _RUN_STATE_LOOKUP_CACHE_LOCK:
        event = _RUN_STATE_INFLIGHT.get(key)
        if event is not None:
            return event, False
        event = threading.Event()
        _RUN_STATE_INFLIGHT[key] = event
        return event, True


def _finish_run_state_lookup(root_key: str, agent_sid: str, path: Optional[Path]) -> None:
    key = (root_key, agent_sid)
    with _RUN_STATE_LOOKUP_CACHE_LOCK:
        _RUN_STATE_LOOKUP_CACHE[key] = (time.monotonic(), path)
        event = _RUN_STATE_INFLIGHT.pop(key, None)
    if event is not None:
        event.set()


def _state_files_for_sid(root: Path, agent_sid: str) -> list[Path]:
    from runs_dir import state_files_for_sid
    return state_files_for_sid(root, agent_sid)


def _scan_run_state_for_jsonl(agent_sid: str) -> Optional[Path]:
    """Resolve one sid's native jsonl path from run `state.json` files.

    This lookup is intentionally targeted. Building a full sid->path index
    was measured at multi-second latency in large runs directories.
    """
    try:
        from runs_dir import runs_root
        root = runs_root()
    except Exception:
        return None
    if not root.is_dir():
        return None
    key = str(root)
    cached = _run_state_cache_get(key, agent_sid)
    if cached is not False:
        return cached
    event, owner = _claim_run_state_lookup(key, agent_sid)
    if not owner:
        event.wait(_RUN_STATE_LOOKUP_TIMEOUT_S)
        cached = _run_state_cache_get(key, agent_sid)
        return cached if cached is not False else None
    newest: tuple[float, Path] | None = None
    try:
        with perf.timed("native_files.lookup_run_state"):
            for sp in _state_files_for_sid(root, agent_sid):
                try:
                    st = json.loads(sp.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if str(st.get("session_id") or "") != agent_sid:
                    continue
                jp = st.get("jsonl_path")
                if not jp:
                    continue
                mt = sp.stat().st_mtime
                if newest is None or mt > newest[0]:
                    newest = (mt, Path(jp))
    except Exception:
        logger.exception("native_files: run-state lookup failed sid=%s", agent_sid[:8])
    path = newest[1] if newest is not None else None
    _finish_run_state_lookup(key, agent_sid, path)
    return path


def _resolve_primary_jsonl(sess: dict, sid: str) -> Optional[Path]:
    """Resolve the native session-stream path for a session's primary sid,
    provider-agnostically (Claude `<projects>/<encoded-cwd>/<sid>.jsonl`,
    Gemini `<run-dir>/session_events.jsonl`, remote-node shadow, …).

    Two layers so this manager is the authority for ALL providers AND
    never silently drops a primary whose file lags the sid announcement:
      1. run `state.json` scan — existence-free lookup for the live-turn
         window where the sid is announced (via the SDK/CLI `init` stream)
         before the stream file is flushed to disk. The
         `OwnedClaudeJsonlTailer`'s inner tailer polls until the file
         appears, so handing back a not-yet-existing path is correct.
      2. `compute_jsonl_read_path` — covers remote-node shadow routing and
         older already-flushed local sessions not backed by run state."""
    if _is_local_session(sess):
        path = _scan_run_state_for_jsonl(sid)
        if path is not None:
            return path
    cwd = sess.get("cwd") or ""
    try:
        from orchs.jsonl_helpers import compute_jsonl_read_path
        with perf.timed("native_files.compute_jsonl_read_path"):
            path = compute_jsonl_read_path(cwd, sid, session=sess)
    except Exception:
        logger.exception("native_files: jsonl resolve failed for sid=%s", sid)
        path = None
    return path


def _is_codex_rollout(path: Path) -> bool:
    codex_home = Path.home() / ".codex"
    try:
        path.resolve().relative_to(codex_home.resolve())
        return True
    except (ValueError, OSError):
        return path.name.startswith("rollout-")


def _is_local_session(sess: dict) -> bool:
    node_id = sess.get("node_id") or "primary"
    try:
        from topology import local_node_id
        return node_id == local_node_id()
    except Exception:
        return node_id == "primary"


class NativeFilesManager:
    def __init__(self) -> None:
        # supply: owning app_session_id -> {agent_sid: _Target}
        self._targets: dict[str, dict[str, _Target]] = {}
        # demand: owning app_session_id -> set of subscriber tokens
        self._demand: dict[str, set] = {}
        # live tailers, keyed (root_id, agent_sid) — deduped across owners
        # in the same root.
        self._tailers: dict[tuple[str, str], "object"] = {}
        # sessions already cold-seeded from the store.
        self._seeded: set[str] = set()
        self._seed_locks: dict[str, asyncio.Lock] = {}
        self._native_path_locks: dict[str, threading.Lock] = {}
        self._native_path_locks_guard = threading.Lock()
        self._primary_jsonl_cache: dict[tuple[str, str, str], tuple[float, Optional[Path]]] = {}
        self._primary_jsonl_cache_lock = threading.Lock()
        self._primary_resolution_tasks: dict[tuple[str, str], asyncio.Task] = {}

    # ── wiring ────────────────────────────────────────────────────────
    def bind(self) -> None:
        """Subscribe to the supply + demand facts. Idempotent."""
        for name in (
            "native_files_agent_sid",
            "native_files_fork_target",
            "native_files_processed",
            "native_files_demand",
        ):
            bus.unsubscribe(name)
        bus.subscribe(
            "session.agent_sid_set", self._on_agent_sid, name="native_files_agent_sid",
        )
        bus.subscribe(
            "native_files.fork_target", self._on_fork_target,
            name="native_files_fork_target",
        )
        bus.subscribe(
            "session.processed_lines_advanced", self._on_processed,
            name="native_files_processed",
        )
        bus.subscribe(
            "native_files.demand", self._on_demand, name="native_files_demand",
        )
        logger.info("native_files: bound supply + demand subscribers")

    # ── pin predicate (R2: only holder of tailer liveness) ────────────
    def is_tailing_root(self, root_id: str) -> bool:
        return any(rid == root_id for (rid, _sid) in self._tailers)

    # ── supply folds ──────────────────────────────────────────────────
    async def _on_agent_sid(self, event: BusEvent) -> None:
        """A session's primary agent_sid became known. Upsert its target."""
        sid = event.sid
        agent_sid = (event.payload or {}).get("agent_sid")
        if not sid or not agent_sid:
            return
        sess = await asyncio.to_thread(session_manager.get_lite, sid)
        if sess is None:
            return
        target = await self._resolve_primary_target(
            sid,
            sess,
            agent_sid,
            allow_slow=False,
            trigger_event_id=event.seq,
            trigger_event_type=event.type,
        )
        if target is not None:
            self._targets.setdefault(sid, {})[agent_sid] = target
            await self._append_native_path_target_async(sid, target)
        else:
            self._schedule_primary_resolution(
                sid,
                sess,
                agent_sid,
                trigger_event_id=event.seq,
                trigger_event_type=event.type,
            )
        await self._reconcile()

    async def _on_fork_target(self, event: BusEvent) -> None:
        """A worker-fork panel was assigned a sid + jsonl path. The fork
        is owned (for demand) by the PARENT session whose messages hold
        the panel — its subscribers are what keep the fork tailed."""
        p = event.payload or {}
        owning = p.get("parent_app_session_id")
        agent_sid = p.get("fork_agent_sid")
        jsonl_path = p.get("jsonl_path")
        fork_bc = p.get("fork_agent_session_id")
        if not owning or not agent_sid or not jsonl_path:
            return
        target = _Target(
            owning=owning,
            agent_sid=agent_sid,
            jsonl_path=Path(jsonl_path),
            start_offset=await self._fork_resume_offset(owning, fork_bc, agent_sid),
            trigger_event_id=event.seq,
            trigger_event_type=event.type,
            fork_agent_session_id=fork_bc,
        )
        self._targets.setdefault(owning, {})[agent_sid] = target
        await self._append_native_path_target_async(owning, target)
        await self._reconcile()

    async def _fork_resume_offset(
        self, owning: str, fork_bc: Optional[str], agent_sid: str,
    ) -> int:
        """Authoritative resume offset for a fork tailer: the max of the
        PARENT's cursor (where the tailer's own `_on_cursor` writes) and
        the FORK Better Agent session's cursor (where the delegation prep-skip
        writes, to drop parent-inherited prep lines). Reading only one
        loses the other writer's progress."""
        offset = 0
        parent = await asyncio.to_thread(session_manager.get, owning)
        if parent is not None:
            offset = max(offset, int(
                (parent.get("processed_line_by_sid") or {}).get(agent_sid) or 0,
            ))
        if fork_bc:
            fork = await asyncio.to_thread(session_manager.get, fork_bc)
            if fork is not None:
                offset = max(offset, int(
                    (fork.get("processed_line_by_sid") or {}).get(agent_sid) or 0,
                ))
        return offset

    async def _on_processed(self, event: BusEvent) -> None:
        """A sid's processed-line cursor advanced. Update the matching
        target's resume offset (consumed only at next tailer open).

        For forks the cursor can land on EITHER the parent session record
        (tailer self-writes) OR the fork Better Agent session record (delegation
        prep-skip). Both should bump the same target's offset, so scan
        all targets keyed by agent_sid for either owner identity."""
        sid = event.sid
        p = event.payload or {}
        agent_sid = p.get("agent_sid")
        n = p.get("n")
        if not sid or not agent_sid or not isinstance(n, int):
            return
        for owning, tgts in self._targets.items():
            tgt = tgts.get(agent_sid)
            if tgt is None:
                continue
            if sid == owning or sid == tgt.fork_agent_session_id:
                tgt.start_offset = max(tgt.start_offset, int(n))

    # ── demand fold ───────────────────────────────────────────────────
    async def _on_demand(self, event: BusEvent) -> None:
        p = event.payload or {}
        owning = p.get("owning_session")
        token = p.get("token")
        present = p.get("present")
        if not owning:
            return
        if present:
            # `token=None` is reserved as the "drop ALL demand" sweep
            # sentinel (present=False). A present=True must carry a real
            # per-subscriber token; reject a phantom None member that only
            # a full sweep could ever clear.
            if token is None:
                logger.warning("native_files: demand present with no token, ignoring")
                return
            await self._seed_session(owning)
            self._demand.setdefault(owning, set()).add(token)
        else:
            tokens = self._demand.get(owning)
            if tokens is not None:
                if token is None:
                    tokens.clear()
                else:
                    tokens.discard(token)
                if not tokens:
                    self._demand.pop(owning, None)
        await self._reconcile()

    # ── cold-start seed (owner state → projection) ────────────────────
    async def _seed_session(self, owning: str) -> None:
        if owning in self._seeded:
            return
        lock = self._seed_locks.setdefault(owning, asyncio.Lock())
        async with lock:
            if owning in self._seeded:
                return
            sess = await asyncio.to_thread(session_manager.get, owning)
            if sess is None:
                return
            primary = sess.get("agent_session_id")
            if primary:
                target = await self._resolve_primary_target(
                    owning,
                    sess,
                    primary,
                    allow_slow=False,
                )
                if target is not None:
                    self._targets.setdefault(owning, {})[primary] = target
                    await self._append_native_path_target_async(owning, target)
                else:
                    self._schedule_primary_resolution(owning, sess, primary)
            for m in sess.get("messages") or []:
                for panel in (m.get("workers") or []):
                    fsid = panel.get("fork_agent_sid")
                    jp = panel.get("jsonl_path")
                    if not fsid or not jp:
                        continue
                    fork_bc = panel.get("fork_agent_session_id")
                    target = _Target(
                        owning=owning,
                        agent_sid=fsid,
                        jsonl_path=Path(jp),
                        start_offset=await self._fork_resume_offset(owning, fork_bc, fsid),
                        fork_agent_session_id=fork_bc,
                    )
                    self._targets.setdefault(owning, {})[fsid] = target
                    await self._append_native_path_target_async(owning, target)
            self._seeded.add(owning)

    async def _resolve_primary_target(
        self,
        owning: str,
        sess: dict,
        agent_sid: str,
        *,
        allow_slow: bool = True,
        trigger_event_id: Optional[int] = None,
        trigger_event_type: Optional[str] = None,
    ) -> Optional[_Target]:
        persisted = await asyncio.to_thread(
            self._read_native_path_target,
            owning,
            agent_sid,
        )
        if persisted is not None:
            return persisted
        if allow_slow:
            jp = await asyncio.to_thread(self._resolve_primary_jsonl_cached, dict(sess), agent_sid)
        else:
            jp = self._primary_jsonl_cache_get(dict(sess), agent_sid)
        if jp is None:
            return None
        offset = int((sess.get("processed_line_by_sid") or {}).get(agent_sid) or 0)
        return _Target(
            owning=owning,
            agent_sid=agent_sid,
            jsonl_path=jp,
            start_offset=offset,
            can_tail=not _is_codex_rollout(jp),
            trigger_event_id=trigger_event_id,
            trigger_event_type=trigger_event_type,
        )

    def _schedule_primary_resolution(
        self,
        owning: str,
        sess: dict,
        agent_sid: str,
        *,
        trigger_event_id: Optional[int] = None,
        trigger_event_type: Optional[str] = None,
    ) -> None:
        key = (owning, agent_sid)
        existing = self._primary_resolution_tasks.get(key)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(
            self._resolve_primary_target_background(
                owning,
                dict(sess),
                agent_sid,
                trigger_event_id=trigger_event_id,
                trigger_event_type=trigger_event_type,
            ),
            name=f"native-primary-resolve-{agent_sid[:8]}",
        )
        self._primary_resolution_tasks[key] = task
        task.add_done_callback(lambda done, task_key=key: self._primary_resolution_done(task_key, done))

    def _primary_resolution_done(
        self,
        key: tuple[str, str],
        task: asyncio.Task,
    ) -> None:
        self._primary_resolution_tasks.pop(key, None)
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("native_files: primary resolution failed sid=%s", key[1][:8])

    async def _resolve_primary_target_background(
        self,
        owning: str,
        sess: dict,
        agent_sid: str,
        *,
        trigger_event_id: Optional[int] = None,
        trigger_event_type: Optional[str] = None,
    ) -> None:
        target = await self._resolve_primary_target(
            owning,
            sess,
            agent_sid,
            allow_slow=True,
            trigger_event_id=trigger_event_id,
            trigger_event_type=trigger_event_type,
        )
        if target is None:
            return
        self._targets.setdefault(owning, {})[agent_sid] = target
        await self._append_native_path_target_async(owning, target)
        await self._reconcile()

    def _primary_jsonl_cache_get(self, sess: dict, agent_sid: str) -> Optional[Path]:
        key = self._primary_jsonl_cache_key(sess, agent_sid)
        now = time.monotonic()
        with self._primary_jsonl_cache_lock:
            cached = self._primary_jsonl_cache.get(key)
            if cached is None:
                return None
            ts, path = cached
            ttl = (
                _PRIMARY_JSONL_POSITIVE_CACHE_TTL_S
                if path is not None
                else _PRIMARY_JSONL_CACHE_TTL_S
            )
            if now - ts < ttl:
                return path
            self._primary_jsonl_cache.pop(key, None)
        return None

    def _primary_jsonl_cache_key(self, sess: dict, agent_sid: str) -> tuple[str, str, str]:
        return (
            str(sess.get("id") or ""),
            str(sess.get("cwd") or ""),
            str(agent_sid),
        )

    def _resolve_primary_jsonl_cached(self, sess: dict, agent_sid: str) -> Optional[Path]:
        key = self._primary_jsonl_cache_key(sess, agent_sid)
        now = time.monotonic()
        with self._primary_jsonl_cache_lock:
            cached = self._primary_jsonl_cache.get(key)
            if cached is not None:
                ts, path = cached
                ttl = (
                    _PRIMARY_JSONL_POSITIVE_CACHE_TTL_S
                    if path is not None
                    else _PRIMARY_JSONL_CACHE_TTL_S
                )
                if now - ts < ttl:
                    return path
                self._primary_jsonl_cache.pop(key, None)
        path = _resolve_primary_jsonl(sess, agent_sid)
        with self._primary_jsonl_cache_lock:
            self._primary_jsonl_cache[key] = (now, path)
        return path

    def _native_paths_path(self, root_id: str) -> Path:
        import session_store
        return Path(session_store.session_file_path(root_id)).parent / root_id / "native_paths"

    def _read_native_path_target(
        self,
        owning: str,
        agent_sid: str,
    ) -> Optional[_Target]:
        root_id = session_manager._root_id_for(owning)
        if not root_id:
            return None
        path = self._native_paths_path(root_id)
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return None
        newest: dict | None = None
        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("owning_session") != owning:
                continue
            if row.get("agent_sid") != agent_sid:
                continue
            if not row.get("jsonl_path"):
                continue
            newest = row
            break
        if newest is None:
            return None
        return _Target(
            owning=owning,
            agent_sid=agent_sid,
            jsonl_path=Path(str(newest["jsonl_path"])),
            start_offset=int(newest.get("start_offset") or 0),
            can_tail=bool(newest.get("can_tail", True)),
            trigger_event_id=newest.get("trigger_event_id"),
            trigger_event_type=newest.get("trigger_event_type"),
            fork_agent_session_id=newest.get("fork_agent_session_id"),
        )

    async def _append_native_path_target_async(
        self, owning: str, target: _Target,
    ) -> None:
        await asyncio.to_thread(self._append_native_path_target, owning, target)

    def _native_path_lock(self, path: Path) -> threading.Lock:
        key = str(path)
        with self._native_path_locks_guard:
            lock = self._native_path_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._native_path_locks[key] = lock
            return lock

    def _append_native_path_target(self, owning: str, target: _Target) -> None:
        with perf.timed("native_files.append_native_path"):
            root_id = session_manager._root_id_for(owning)
            if not root_id:
                return
            path = self._native_paths_path(root_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            lock = self._native_path_lock(path)
            row = {
                "owning_session": owning,
                "agent_sid": target.agent_sid,
                "jsonl_path": str(target.jsonl_path),
                "start_offset": int(target.start_offset),
                "can_tail": bool(target.can_tail),
                "fork_agent_session_id": target.fork_agent_session_id,
                "trigger_event_id": target.trigger_event_id,
                "trigger_event_type": target.trigger_event_type,
            }
            signature = (
                row["owning_session"],
                row["agent_sid"],
                row["jsonl_path"],
            )
            with lock:
                if path.exists():
                    try:
                        for line in path.read_text(encoding="utf-8").splitlines():
                            if not line.strip():
                                continue
                            existing = json.loads(line)
                            if (
                                existing.get("owning_session"),
                                existing.get("agent_sid"),
                                existing.get("jsonl_path"),
                            ) == signature:
                                return
                    except (OSError, json.JSONDecodeError):
                        logger.exception("native_files: native_paths read failed")
                        return
                with path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(row, separators=(",", ":")) + "\n")

    # ── reconcile (R3/R4) ─────────────────────────────────────────────
    async def _reconcile(self) -> None:
        desired: dict[tuple[str, str], _Target] = {}
        for owning, tgts in self._targets.items():
            if not self._demand.get(owning):
                continue
            root_id = session_manager._root_id_for(owning)
            if not root_id:
                continue
            for agent_sid, tgt in tgts.items():
                if not tgt.can_tail:
                    continue
                desired[(root_id, agent_sid)] = tgt

        from jsonl_tailer import OwnedClaudeJsonlTailer

        # Open what is demanded but not yet running.
        for key, tgt in desired.items():
            if key in self._tailers:
                continue
            owned = OwnedClaudeJsonlTailer(
                root_id=key[0],
                app_session_id=tgt.owning,
                agent_sid=tgt.agent_sid,
                jsonl_path=tgt.jsonl_path,
                start_offset=tgt.start_offset,
            )
            owned.acquire()
            if not owned.alive:
                continue
            self._tailers[key] = owned
            await bus.publish(BusEvent(
                type="native_files.tailing_started",
                root_id=key[0],
                sid=tgt.owning,
                payload={"agent_sid": key[1]},
                persist=False,
            ))

        # Close what is running but no longer demanded.
        for key in list(self._tailers):
            if key in desired:
                continue
            owned = self._tailers.pop(key)
            stop_task = owned.release()
            await bus.publish(BusEvent(
                type="native_files.tailing_closed",
                root_id=key[0],
                sid=owned.app_session_id,
                payload={"agent_sid": key[1]},
                persist=False,
            ))
            if stop_task is not None:
                asyncio.create_task(
                    self._await_stop(key, stop_task),
                    name=f"native-tailer-stop-{key[1][:8]}",
                )

    async def _await_stop(self, key: tuple[str, str], task: asyncio.Task) -> None:
        try:
            await asyncio.wait_for(task, timeout=3.0)
        except asyncio.TimeoutError:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        except (asyncio.CancelledError, Exception):
            pass
        logger.info("native_files: tailer stopped agent_sid=%s", key[1])


native_files = NativeFilesManager()
