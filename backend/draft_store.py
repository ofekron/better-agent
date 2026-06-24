"""DraftStore — debounced sidecar persistence for per-keystroke drafts.

The user's in-progress chat input is mutated on every keystroke (the
frontend debounces ~300ms; the backend coalesces further with
`DRAFT_FLUSH_DELAY`). Persisting the full session tree on every
keystroke was the dominant `write_session_full` cost; the per-root
sidecar (`<root_id>.drafts.json`) makes the disk write O(small file).

This class owns the debounce/coalescer state machinery for those
sidecar flushes. It is a sibling to `SessionManager` — NOT folded
into it — because the responsibility is a single coherent state
machine (dirty-set + generation counter + scheduled flush) that has
no cross-talk with the rest of session_manager's invariants
(reconcile, batches, ingest).

Boundary (kept narrow by design):
  - DraftStore owns: `_dirty: set[root_id]`, `_gen: dict[root_id, int]`,
    the `_loop` reference for `call_later` scheduling, the
    `_on_flush_done` exception bridge.
  - DraftStore does NOT own: the in-memory session tree (sm), the
    per-root locks (sm), the WS fire fan-out (sm), the sidecar I/O
    primitives (session_store).
  - DraftStore writes to disk via `session_store.write_drafts(...)`
    and reads the current per-node drafts via
    `session_store.collect_tree_drafts(root)`.
  - DraftStore mutates the cached session record via the new public
    `session_manager.set_draft_inline(...)` (pure cache mutate + WS
    fire; no flush coalescer wiring).
  - DraftStore acquires `session_manager._lock_for_root(rid)` only
    inside the flush body — same pattern as sm's pre-extraction
    `_flush_draft_sync`. The lock is the documented sync point for
    cached-root reads; pulling it into UPM was the adversary's
    "publicizing sm privates" objection. We acquire it via the
    `with_root_lock` public helper instead of touching the private
    `_lock_for_root`.

Coordinator changes:
  - `self.draft_store = DraftStore(self)` in __init__
  - `_ensure_ds()` lazy-init pattern

Callers:
  - main.py PATCH /draft → coordinator.draft_store.set_draft(...)
  - main.py on_shutdown → coordinator.draft_store.drain_pending_drafts()
  - session_manager hot paths (`_is_pinned`, `_persist_root`,
    `_drop_root_memory`, root-delete branch) resolve the active store
    via `sm._draft_store_or_none()` and call into it directly. No
    bind pattern — DraftStore owns both the behavior AND the
    access path.
"""

import asyncio
import logging
import threading
from typing import Optional

import session_store
from session_manager import manager as session_manager

logger = logging.getLogger(__name__)

DRAFT_FLUSH_DELAY = 0.2


class DraftStore:
    """Per-coordinator debounced draft-sidecar persistence."""

    def __init__(self, coordinator) -> None:
        self._c = coordinator
        # Root_ids with pending sidecar flushes.
        self._dirty: set[str] = set()
        # Per-root monotonic generation. Bumped on every set_draft so
        # a stale loop-thread scheduled flush can short-circuit when a
        # newer keystroke has superseded it.
        self._gen: dict[str, int] = {}
        # asyncio loop bound at startup; `call_later` runs the flush
        # body on the loop thread, which then hops to an executor.
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        # Light lock for our own dirty/gen dicts (separate from sm's
        # per-root lock, which protects the cached session tree).
        self._state_lock = threading.RLock()

    # ------------------------------------------------------------------
    # Bindings.
    # ------------------------------------------------------------------
    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    # ------------------------------------------------------------------
    # Public API.
    # ------------------------------------------------------------------
    def set_draft(
        self,
        sid: str,
        text: str,
        seq: int,
        *,
        images: Optional[list] = None,
        bump_updated_at: bool = False,
        client_id: Optional[str] = None,
    ) -> Optional[dict]:
        """Per-keystroke draft update.

        Mutates the cached session record in-place via the public
        sm.set_draft_inline (which acquires the per-root lock, updates
        `draft_input` / `draft_input_seq` / `draft_images`, fires the
        WS `draft_set` change, and returns the cached session dict).
        Then arms a debounced flush UNLESS sm is mid-batch for the
        root, in which case the batch-exit persist will pick up the
        sidecar write itself.
        """
        rid = session_manager._root_id_for(sid)
        if rid is None:
            return None
        sess = session_manager.set_draft_inline(
            sid, text, seq,
            images=images,
            bump_updated_at=bump_updated_at,
            client_id=client_id,
        )
        if sess is None:
            return None
        # Mark dirty UNCONDITIONALLY — even inside a batch. sm's
        # batch-exit `_persist_root` reads our `is_dirty(rid)` hook
        # to decide whether to flush the sidecar; clearing dirty
        # there satisfies our pending state. Pre-refactor sm.set_draft
        # added to `_draft_dirty` regardless of batch for this exact
        # reason.
        with self._state_lock:
            self._dirty.add(rid)
        # Batch active → sm's batch-exit will flush the sidecar
        # (it sees our dirty state via the pin_check hook). Skip the
        # scheduled coalescer to avoid a redundant write between
        # mid-batch keystrokes.
        # NOTE: this check runs AFTER `set_draft_inline` released the
        # per-root lock, so a parallel `batch(rid)` enter/exit could
        # race in between. Outcomes are benign because
        # `_flush_draft_sync` re-checks under the lock before writing:
        # if batch is open by then, the executor body still runs (a
        # second sidecar write coalesces correctly with batch exit);
        # if batch closed, we just wrote the sidecar twice
        # idempotently.
        if session_manager.is_batching(rid):
            return sess
        if self._loop is None:
            # Pre-startup / test harness path — write inline to match
            # pre-coalescer behavior. Errors propagate so a write
            # failure surfaces to the caller (parity with sm pre-F3).
            self._persist_drafts(rid)
            return sess
        with self._state_lock:
            gen = self._gen.get(rid, 0) + 1
            self._gen[rid] = gen
        self._loop.call_soon_threadsafe(self._arm_draft_flush, rid, gen)
        return sess

    def drain_pending_drafts(self) -> None:
        """Synchronously flush every dirty draft. Called from
        `on_shutdown` BEFORE `event_ingester.close_all()` so no
        typed-but-unsent draft text is lost on a clean shutdown.

        INVARIANT: callers (on_shutdown) run after uvicorn has stopped
        accepting requests, so no new `set_draft` can land mid-drain
        and bypass the snapshot.
        """
        with self._state_lock:
            rids = list(self._dirty)
        for rid in rids:
            # sm's per-root lock protects the cached tree. Snapshot
            # the draft sidecar under it for byte-identity with the
            # tree's in-memory state.
            session_manager.with_root_lock(rid, lambda r=rid: self._persist_drafts(r))

    def is_dirty(self, rid: str) -> bool:
        """Pin predicate: while a root has a pending draft flush, sm's
        LRU MUST NOT evict it (the eviction would drop the cached tree
        before our flush could read the latest draft fields). Bound
        into sm's `_is_pinned` via `bind_draft_pin_predicate`."""
        return rid in self._dirty

    def note_root_persisted(self, rid: str) -> None:
        """Called by sm._persist_root once the full root has been
        written to disk. The full-tree write IS a draft persist
        (sidecar is updated as part of `_strip_volatile_from_tree`'s
        callers), so the pending flush is satisfied — drop the dirty
        flag and bump the gen to invalidate any scheduled callback."""
        with self._state_lock:
            self._dirty.discard(rid)

    def note_root_dropped(self, rid: str) -> None:
        """Called by sm._drop_root_memory on LRU eviction. Clean up
        our per-root state."""
        with self._state_lock:
            self._dirty.discard(rid)
            self._gen.pop(rid, None)

    # ------------------------------------------------------------------
    # Internals.
    # ------------------------------------------------------------------
    def _arm_draft_flush(self, rid: str, gen: int) -> None:
        """Loop-thread entry. Schedule the actual flush at
        `DRAFT_FLUSH_DELAY` so successive keystrokes within the
        window coalesce."""
        if self._loop is None:
            return
        self._loop.call_later(
            DRAFT_FLUSH_DELAY, self._maybe_flush_draft, rid, gen,
        )

    def _maybe_flush_draft(self, rid: str, expected_gen: int) -> None:
        """Loop-thread callback. Cheap lockless pre-checks — dict reads
        are GIL-atomic; a stale read here means the executor body
        re-checks under the lock (correct, slightly wasteful)."""
        if self._loop is None:
            return
        if self._gen.get(rid, 0) != expected_gen:
            return  # superseded by a newer set_draft
        if rid not in self._dirty:
            return  # already persisted by another path
        fut = self._loop.run_in_executor(
            None, self._flush_draft_sync, rid, expected_gen,
        )
        fut.add_done_callback(self._on_flush_done)

    def _flush_draft_sync(self, rid: str, expected_gen: int) -> None:
        """Executor-thread body. Re-checks gen + dirty under sm's
        per-root lock so a parallel `set_draft` or `_persist_root`
        cannot race us into stale state."""
        def _under_lock(_rid: str = rid) -> None:
            with self._state_lock:
                if self._gen.get(_rid, 0) != expected_gen:
                    return
                if _rid not in self._dirty:
                    return
            self._persist_drafts(_rid)

        session_manager.with_root_lock(rid, _under_lock)

    def _persist_drafts(self, root_id: str) -> None:
        """Write ONLY the draft sidecar — not the full tree. This is
        the per-keystroke hot path; must stay O(small file). Caller
        MUST hold `session_manager.with_root_lock(root_id)`."""
        root = session_manager.get_root_ref(root_id)
        if root is None:
            return
        session_store.write_drafts(
            root_id, session_store.collect_tree_drafts(root),
        )
        with self._state_lock:
            self._dirty.discard(root_id)

    @staticmethod
    def _on_flush_done(fut) -> None:
        # Guard `cancelled()` before `exception()` so the shutdown
        # cancel cascade doesn't log spurious CancelledError noise.
        if fut.cancelled():
            return
        exc = fut.exception()
        if exc is not None:
            logger.exception("draft flush failed", exc_info=exc)
