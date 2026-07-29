"""In-process pub/sub for backend lifecycle events.

Single-process, asyncio-native bus. Producers publish `BusEvent`s; subscribers
register handlers keyed on a glob-style type pattern with an explicit priority.

Ordering invariant: subscribers with lower priority numbers fire FIRST.
This matters because `events.jsonl` is the durable substrate WS catch-up
replay reads from — the persistence subscriber (priority 10) MUST land its
write before the WS broadcaster (priority 50) emits to a live client, or a
client that reconnects mid-publish gets a gap.

Re-entrancy guard: a handler that publishes from inside its own dispatch is
allowed up to `_MAX_DEPTH`. Beyond that we raise — almost always a real
loop, not a legitimate deep chain.

**Schema versioning (A16).** Every BusEvent carries a `schema_version: int`
stamped by the producer (default 1). The `_TYPE_SCHEMA_VERSIONS` registry
maps an event type to its CURRENT expected version; the producer pulls
its stamp from there via `current_schema_version(type)` so call sites
don't have to remember the number. When you change an event's payload
shape, bump the entry in the registry and the new producers
auto-stamp the new version. On replay (`bus.publish(event, is_replay=True)`)
the bus validates the event's `schema_version` against the registry and
raises `EventSchemaError` on mismatch — per CLAUDE.md "schema migrations
are NOT supported", the operator wipes the persisted log to recover.

**Cross-thread delivery.** A subscriber may pin itself to an event loop
(`subscribe(..., bind_current_loop=True)`, or `bind_unpinned_to_current_loop()`
once from the main loop at startup). A publish whose running loop is the
subscriber's loop delivers inline and awaited, exactly as before — the
main-loop-to-main-loop path is unchanged. A publish from any other
thread hands the frame to the subscriber's loop instead.

What that costs, stated plainly:

- Cross-loop delivery is NOT awaited by the publisher. `publish()`
  returns once every remote frame is handed off, not once it is
  handled. A publisher that needs a subscriber's side effect to be
  visible before it continues must be on that subscriber's loop.
- Ordering is per-target FIFO in the order frames were handed off,
  which for one publisher means priority order. There is no global
  total order across targets, so the priority-10-before-priority-50
  guarantee holds only when both subscribers share a delivery target.
  The persistence-before-broadcast pair MUST stay co-located.
- A target whose loop is closed, stopped, or `_TARGET_BUFFER_MAX`
  behind drops frames and logs. Publishers never block on a foreign
  loop.

`publish_threadsafe()` is the entry point for a plain worker thread with
no loop of its own; it skips unpinned subscribers rather than running
loop-affine handlers off-loop.

**Monotonic seq (A16).** The bus stamps a per-instance monotonic `seq`
on every fresh publish so subscribers (and downstream readers of the
persisted events.jsonl) can detect reordering and gaps. On replay, the
event's original `seq` is preserved and the bus's internal counter is
fast-forwarded past it so subsequent fresh publishes stay monotone.
"""

from __future__ import annotations

import asyncio
import contextvars
import fnmatch
import logging
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

# Cap on frames buffered for one delivery target. A target whose loop is
# blocked or gone must never block or unbound-grow the publisher's thread,
# so an over-full target drops its oldest frame and logs. Fail closed.
_TARGET_BUFFER_MAX = 4096


class EventSchemaError(Exception):
    """Raised by `bus.publish(event, is_replay=True)` when an event's
    `schema_version` doesn't match the current expected version for
    its `type`. Per CLAUDE.md the policy is no auto-migration — the
    operator wipes the persisted log (events.jsonl, or A3's WAL) and
    restarts. Producers stamp `schema_version` from
    `current_schema_version(type)` so live publishes always match
    by construction; this exception is only reachable from the
    replay path."""
    pass


# Registry: event type → current expected schema_version. Producers
# pull their stamp from here at publish time; the replay path
# validates against it. Bump the entry when an event's payload
# changes shape.
#
# Unknown types default to 1 (the all-events-pre-versioning baseline).
# This keeps the registry sparse — only enumerate types whose payload
# has actually moved past v1.
_TYPE_SCHEMA_VERSIONS: dict[str, int] = {}


def register_event_schema(event_type: str, version: int) -> None:
    """Declare the current schema_version for `event_type`. Idempotent
    — re-registering the same (type, version) is a no-op; mismatched
    re-registration raises (a programming error)."""
    existing = _TYPE_SCHEMA_VERSIONS.get(event_type)
    if existing is not None and existing != version:
        raise ValueError(
            f"event schema {event_type!r} already registered at "
            f"version {existing}; cannot re-register at {version}"
        )
    _TYPE_SCHEMA_VERSIONS[event_type] = version


def current_schema_version(event_type: str) -> int:
    """Lookup the registered schema_version for `event_type`. Returns
    1 for unregistered types (the implicit baseline)."""
    return _TYPE_SCHEMA_VERSIONS.get(event_type, 1)

# Re-entrancy depth — per-task, not process-wide. Two concurrent
# publishes from different tasks must NOT share the counter (their
# depths would superimpose and falsely trip `_MAX_DEPTH`). A
# ContextVar gives each asyncio task its own counter while still
# correctly inheriting depth across a subscriber's nested publish.
_publish_depth: contextvars.ContextVar[int] = contextvars.ContextVar(
    "event_bus_publish_depth", default=0,
)


@dataclass
class BusEvent:
    """One published event.

    `type` is the routing key (e.g. "user_message_queued", "agent_message").
    Subscribers match it with fnmatch globs.

    `root_id` and `sid` locate the Better Agent session this event belongs to —
    every persistence target and most WS broadcasters need these. Required.

    `payload` is the event-specific body the subscribers consume.

    `msg_id` / `run_id` are correlation handles when relevant. None otherwise.

    `persist` controls whether the events.jsonl persistence subscriber
    writes this event to disk. Default True — every agent transcript
    event and every user_message_* lifecycle event needs to land in
    events.jsonl for WS catch-up replay. Set False for backend-internal
    notifications that have no WS consumer (e.g. session.agent_sid_set
    used by Orchestrator to spin tailers — the session record on disk
    is already the durable state).

    `schema_version` (A16) is the producer-stamped declaration of which
    payload shape this event uses. Producers normally let the bus stamp
    this from `current_schema_version(type)` at publish time — they
    don't need to remember the number. Replay validates against the
    registry and raises `EventSchemaError` on mismatch.

    `seq` (A16) is a per-bus-instance monotonic counter stamped at
    publish time. Subscribers use it to detect reordering / gaps. 0
    means "unstamped" — the bus replaces 0 with the next counter
    value on fresh publish. On replay the original `seq` is preserved
    and the bus counter is fast-forwarded past it.

    `is_replay` (A16) is True iff the event reached the bus via the
    replay path (`bus.publish(event, is_replay=True)`). Subscribers
    that are idempotent on a request-id (A3's WAL replay model)
    branch on this flag if they want to suppress side effects that
    were already executed pre-crash.
    """
    type: str
    root_id: str
    sid: str
    payload: dict
    msg_id: Optional[str] = None
    run_id: Optional[str] = None
    ts: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    persist: bool = True
    schema_version: int = 1
    seq: int = 0
    is_replay: bool = False


Handler = Callable[[BusEvent], Awaitable[None]]


@dataclass
class _Sub:
    priority: int
    pattern: str
    handler: Handler
    name: str
    # Loop this subscriber must run on. None means "run inline on
    # whatever loop published", which is the pre-cross-thread behavior
    # and the right default for handlers with no loop affinity.
    loop: Optional[asyncio.AbstractEventLoop] = None


class _DeliveryTarget:
    """Serialized hand-off of events onto one foreign event loop.

    A single drain task per target pops a plain deque, so frames reach
    the target in the order they were handed off — the ordering
    guarantee is per-target FIFO, not a global total order across
    targets.

    The deque and its lock are plain threading primitives on purpose:
    `asyncio.Queue` is bound to one loop and is not safe to fill from
    another thread. `call_soon_threadsafe` is the only cross-loop call
    made here, which is exactly what it is for.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._buffer: deque[tuple[_Sub, BusEvent]] = deque()
        self._lock = threading.Lock()
        self._draining = False
        self._dropped = 0

    def offer(self, sub: _Sub, event: BusEvent) -> None:
        if self._loop.is_closed():
            logger.warning(
                "event_bus: dropping %s for %r — target loop is closed",
                event.type, sub.name,
            )
            return
        with self._lock:
            while len(self._buffer) >= _TARGET_BUFFER_MAX:
                self._buffer.popleft()
                self._dropped += 1
            self._buffer.append((sub, event))
        try:
            self._loop.call_soon_threadsafe(self._kick)
        except RuntimeError:
            # Loop shut down between the is_closed() check and here.
            logger.warning(
                "event_bus: dropping %s for %r — target loop stopped",
                event.type, sub.name,
            )

    def _kick(self) -> None:
        if self._draining:
            return
        self._draining = True
        self._loop.create_task(self._drain(), name="event-bus-target-drain")

    async def _drain(self) -> None:
        try:
            while True:
                with self._lock:
                    if not self._buffer:
                        return
                    sub, event = self._buffer.popleft()
                    dropped, self._dropped = self._dropped, 0
                if dropped:
                    logger.warning(
                        "event_bus: target dropped %d frame(s) before %s",
                        dropped, event.type,
                    )
                try:
                    await sub.handler(event)
                except Exception:
                    logger.exception(
                        "event_bus subscriber %r raised on %s "
                        "(cross-thread delivery)",
                        sub.name, event.type,
                    )
        finally:
            self._draining = False
            with self._lock:
                pending = bool(self._buffer)
            if pending:
                self._kick()

    def pending(self) -> int:
        with self._lock:
            return len(self._buffer)


class EventBus:
    """Single in-process bus instance. Use the module-level `bus`."""

    _MAX_DEPTH = 8

    def __init__(self) -> None:
        self._subs: list[_Sub] = []
        self._exact_subs: dict[str, list[_Sub]] = {}
        self._glob_subs: list[_Sub] = []
        self._lock = asyncio.Lock()  # Protects _subs sort during concurrent subscribe.
        # A16 monotonic seq stamper. Plain int + `+= 1` is atomic
        # under the GIL (single bytecode op, no await between read
        # and write), so no asyncio.Lock needed. Reset to 0 on every
        # bus instantiation; survives nothing (per-process). Replays
        # fast-forward this past their original seq so subsequent
        # fresh publishes stay monotone across restarts.
        self._seq_counter: int = 0
        # Stamping now happens from arbitrary threads via
        # `publish_threadsafe`, so `+= 1` is no longer the only writer
        # and the GIL-atomicity argument above no longer covers it.
        self._seq_lock = threading.Lock()
        self._targets: dict[int, _DeliveryTarget] = {}
        self._targets_lock = threading.Lock()

    def subscribe(
        self,
        pattern: str,
        handler: Handler,
        *,
        priority: int = 50,
        name: str,
        loop: Optional[asyncio.AbstractEventLoop] = None,
        bind_current_loop: bool = False,
    ) -> None:
        """Register `handler` for events whose `type` matches `pattern`
        (fnmatch glob). Lower `priority` fires first. `name` is for
        debugging and bus dumps only.

        Subscribers are not de-duplicated by name — adding the same name
        twice creates two subscriptions. Caller's responsibility.

        `loop` pins delivery to a specific event loop; `bind_current_loop`
        pins it to the loop running at subscribe time. A handler with
        neither runs inline on whichever loop published, which is the
        right default only when the handler is loop-agnostic. Any
        handler that touches loop-bound state (asyncio primitives, a
        WebSocket, a store guarded by main-loop-only assumptions) MUST
        pin itself, or a publish from another thread will run it on the
        wrong loop.
        """
        if loop is None and bind_current_loop:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                raise RuntimeError(
                    f"subscribe({name!r}, bind_current_loop=True) needs a "
                    f"running loop",
                ) from None
        self._subs.append(_Sub(priority, pattern, handler, name, loop))
        self._reindex_subscribers()

    def bind_unpinned_to_current_loop(self) -> int:
        """Pin every not-yet-pinned subscriber to the running loop.

        Most subscriptions are registered at import time, where there is
        no running loop to capture. Call this once from the main loop
        during startup — after that, a publish from any other thread
        routes those handlers back to main instead of running them on
        the publisher's loop. Returns the number pinned.
        """
        loop = asyncio.get_running_loop()
        pinned = 0
        for sub in self._subs:
            if sub.loop is None:
                sub.loop = loop
                pinned += 1
        return pinned

    def unsubscribe(self, name: str) -> int:
        """Remove every subscription with the given `name`. Returns the
        number removed. Use for test cleanup."""
        before = len(self._subs)
        self._subs = [s for s in self._subs if s.name != name]
        self._reindex_subscribers()
        return before - len(self._subs)

    def _reindex_subscribers(self) -> None:
        self._subs.sort(key=lambda s: (s.priority, s.name))
        exact: dict[str, list[_Sub]] = {}
        glob_subs: list[_Sub] = []
        for sub in self._subs:
            if any(char in sub.pattern for char in "*?["):
                glob_subs.append(sub)
            else:
                exact.setdefault(sub.pattern, []).append(sub)
        self._exact_subs = exact
        self._glob_subs = glob_subs

    def _matching_subscribers(self, event_type: str) -> list[_Sub]:
        exact = self._exact_subs.get(event_type) or []
        globbed = [
            sub
            for sub in self._glob_subs
            if fnmatch.fnmatchcase(event_type, sub.pattern)
        ]
        if not exact:
            return globbed
        if not globbed:
            return exact
        return sorted([*exact, *globbed], key=lambda s: (s.priority, s.name))

    # A17: meta-event emitted when a subscriber raises. Surfaced as a
    # bus event so downstream observability subscribers (metrics, the
    # frontend's diagnostic banner, future tracing) can react to
    # failures without scraping logs. NOT persisted (avoid feedback
    # loop if the persistence subscriber is the one failing) and NOT
    # re-emitted recursively (the publisher checks event.type before
    # emitting a meta to break the cycle).
    _SUBSCRIBER_FAILED_TYPE: str = "subscriber_failed"

    async def publish(self, event: BusEvent, *, is_replay: bool = False) -> None:
        """Dispatch `event` to every matching subscriber in priority order.

        One subscriber raising doesn't stop the rest — exceptions are
        logged and swallowed. Re-entrancy beyond `_MAX_DEPTH` raises.

        Depth tracking is per-task via a `ContextVar`: two unrelated
        publishes running concurrently on different tasks don't share
        a counter, but a subscriber that publishes a nested event still
        sees the parent's depth (ContextVar inheritance).

        **A16 stamping.** On fresh publish:
          - `schema_version` is stamped from `current_schema_version(type)`
            if the event came in with the dataclass default of 1 AND the
            registry says otherwise. Producers that already set a
            specific version (e.g. for a forward-compat trial) keep it.
          - `seq` is stamped from the next monotonic counter if the
            event came in with 0 (unstamped).

        **A16 replay.** When `is_replay=True`:
          - The event's `schema_version` is validated against
            `current_schema_version(type)`. Mismatch raises
            `EventSchemaError` BEFORE any subscriber fires — no
            partial-replay state.
          - The event's `is_replay` flag is set to True so subscribers
            that branch on it (idempotent re-application of a WAL'd
            operation) can see the signal.
          - The bus's internal seq counter is fast-forwarded past the
            event's original seq so subsequent fresh publishes after
            replay completes stay monotone w.r.t. the replayed history.
            The replayed event's seq is NOT changed.
        """
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if is_replay:
            expected = current_schema_version(event.type)
            if event.schema_version != expected:
                raise EventSchemaError(
                    f"event {event.type!r} on replay has "
                    f"schema_version={event.schema_version}, "
                    f"expected={expected}. No auto-migration — wipe "
                    f"the persisted log and restart."
                )
            event.is_replay = True
            if event.seq > self._seq_counter:
                self._seq_counter = event.seq
        else:
            # Fresh publish — stamp schema_version (if producer left
            # the default and the registry has a different current),
            # stamp seq (if producer left 0), and force `is_replay`
            # to False so a producer that accidentally set it (or
            # re-used a deserialized event without resetting) can't
            # mis-signal idempotent-replay branches in subscribers.
            registered = current_schema_version(event.type)
            if event.schema_version == 1 and registered != 1:
                event.schema_version = registered
            if event.seq == 0:
                with self._seq_lock:
                    self._seq_counter += 1
                    event.seq = self._seq_counter
            event.is_replay = False

        depth = _publish_depth.get()
        if depth >= self._MAX_DEPTH:
            raise RuntimeError(
                f"event_bus re-entrancy depth {depth} exceeded "
                f"for event type {event.type!r}"
            )
        token = _publish_depth.set(depth + 1)
        try:
            for sub in self._matching_subscribers(event.type):
                if sub.loop is not None and sub.loop is not running:
                    self._target_for(sub.loop).offer(sub, event)
                    continue
                try:
                    await sub.handler(event)
                except Exception as exc:
                    # A17: per-subscriber isolation contract.
                    # Sibling subscribers MUST still fire — never let
                    # one bad handler poison the chain. The publisher
                    # always succeeds from the caller's perspective
                    # (this `except` swallows the raise) so A3's WAL
                    # commit guarantee (`fsync envelope → publish →
                    # subscribers mutate`) holds even when a
                    # downstream mutator throws.
                    logger.exception(
                        "event_bus subscriber %r raised on %s",
                        sub.name, event.type,
                    )
                    # Emit a structured `subscriber_failed` meta-event
                    # so observability subscribers can react. Guarded
                    # against recursion: a subscriber_failed handler
                    # raising would otherwise loop. `persist=False`
                    # so the meta doesn't enter the WAL/events.jsonl
                    # (the persistence subscriber itself might be the
                    # one failing).
                    if event.type != self._SUBSCRIBER_FAILED_TYPE:
                        meta = BusEvent(
                            type=self._SUBSCRIBER_FAILED_TYPE,
                            root_id=event.root_id,
                            sid=event.sid,
                            payload={
                                "original_type": event.type,
                                "original_seq": event.seq,
                                "subscriber_name": sub.name,
                                "error_class": type(exc).__name__,
                                "error_message": str(exc),
                            },
                            persist=False,
                        )
                        try:
                            await self.publish(meta)
                        except Exception:
                            logger.exception(
                                "event_bus: subscriber_failed meta "
                                "emit raised (suppressed)",
                            )
        finally:
            _publish_depth.reset(token)

    def _target_for(self, loop: asyncio.AbstractEventLoop) -> _DeliveryTarget:
        key = id(loop)
        with self._targets_lock:
            target = self._targets.get(key)
            if target is None:
                target = _DeliveryTarget(loop)
                self._targets[key] = target
            return target

    def publish_threadsafe(self, event: BusEvent) -> None:
        """Publish from a thread with no running event loop.

        Fire-and-forget: the caller's thread never blocks on a foreign
        loop and never learns whether subscribers succeeded. Handlers
        pinned to a loop are handed off to it; unpinned handlers are
        skipped and logged, because running them here would execute
        loop-affine code on a plain worker thread.
        """
        registered = current_schema_version(event.type)
        if event.schema_version == 1 and registered != 1:
            event.schema_version = registered
        if event.seq == 0:
            with self._seq_lock:
                self._seq_counter += 1
                event.seq = self._seq_counter
        event.is_replay = False
        unpinned: list[str] = []
        for sub in self._matching_subscribers(event.type):
            if sub.loop is None:
                unpinned.append(sub.name)
                continue
            self._target_for(sub.loop).offer(sub, event)
        if unpinned:
            logger.warning(
                "event_bus: publish_threadsafe(%s) skipped unpinned "
                "subscriber(s) %s — pin them with bind_current_loop or "
                "bus.bind_unpinned_to_current_loop()",
                event.type, ", ".join(sorted(unpinned)),
            )

    def pending_cross_thread(self) -> int:
        """Frames handed off but not yet delivered. For shutdown checks
        and tests."""
        with self._targets_lock:
            targets = list(self._targets.values())
        return sum(target.pending() for target in targets)

    def drop_delivery_targets(self) -> None:
        """Forget every cross-thread target. Buffered frames are
        discarded — call only during shutdown or test teardown."""
        with self._targets_lock:
            self._targets.clear()

    def describe(self) -> list[dict]:
        """Snapshot of registered subscribers. Used by debug endpoints
        and tests — order matches dispatch order."""
        return [
            {
                "priority": s.priority,
                "pattern": s.pattern,
                "name": s.name,
                "pinned": s.loop is not None,
            }
            for s in self._subs
        ]


bus = EventBus()
