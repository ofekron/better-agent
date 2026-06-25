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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)


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

    def subscribe(
        self,
        pattern: str,
        handler: Handler,
        *,
        priority: int = 50,
        name: str,
    ) -> None:
        """Register `handler` for events whose `type` matches `pattern`
        (fnmatch glob). Lower `priority` fires first. `name` is for
        debugging and bus dumps only.

        Subscribers are not de-duplicated by name — adding the same name
        twice creates two subscriptions. Caller's responsibility.
        """
        self._subs.append(_Sub(priority, pattern, handler, name))
        self._reindex_subscribers()

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

    def describe(self) -> list[dict]:
        """Snapshot of registered subscribers. Used by debug endpoints
        and tests — order matches dispatch order."""
        return [
            {"priority": s.priority, "pattern": s.pattern, "name": s.name}
            for s in self._subs
        ]


bus = EventBus()
