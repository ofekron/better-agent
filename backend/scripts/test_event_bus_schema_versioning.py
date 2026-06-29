"""Regression tests for A16's BusEvent schema-versioning + monotonic seq
+ EventSchemaError on replay.

Pins the contract:
  1. Fresh publish stamps a monotonic `seq` when the producer left 0.
  2. Fresh publish stamps `schema_version` from the registry when the
     producer left the dataclass default of 1.
  3. A producer-supplied non-zero `seq` is preserved (cluster tests
     that mint synthetic events).
  4. `is_replay=True` validates `schema_version` against the registry
     and raises `EventSchemaError` on mismatch BEFORE any subscriber
     runs.
  5. `is_replay=True` preserves the event's original `seq` and
     fast-forwards the bus counter past it (no duplicate seqs across
     restart-replay-then-fresh-publish).
  6. Subscribers see `event.is_replay == True` on the replay path
     and `False` on the fresh publish path.
  7. `register_event_schema` is idempotent; mismatched re-registration
     raises ValueError.

Run with:
    cd backend && .venv/bin/python scripts/test_event_bus_schema_versioning.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Make backend/ importable when run from anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from event_bus import (  # noqa: E402
    BusEvent,
    EventBus,
    EventSchemaError,
    current_schema_version,
    register_event_schema,
)


def _make_event(event_type: str, *, schema_version: int = 1, seq: int = 0) -> BusEvent:
    return BusEvent(
        type=event_type,
        root_id="root-1",
        sid="sid-1",
        payload={},
        schema_version=schema_version,
        seq=seq,
    )


async def _run():
    failures: list[str] = []

    def check(cond: bool, label: str) -> None:
        print(f"  {'OK' if cond else 'FAIL'}  {label}")
        if not cond:
            failures.append(label)

    # Subscriber that records every event it sees.
    bus = EventBus()
    seen: list[BusEvent] = []

    async def recorder(ev: BusEvent) -> None:
        seen.append(ev)

    bus.subscribe("*", recorder, name="recorder")

    # ── 1. Fresh publish stamps monotonic seq ──────────────────────
    seen.clear()
    e1 = _make_event("test.fresh.a")
    e2 = _make_event("test.fresh.b")
    await bus.publish(e1)
    await bus.publish(e2)
    check(e1.seq == 1 and e2.seq == 2, "fresh publish: monotonic seq stamped (1, 2)")
    check(seen[-1].seq == 2, "subscriber sees stamped seq")

    # ── 2. Fresh publish picks up schema_version from registry ─────
    register_event_schema("test.v3.type", 3)
    e3 = _make_event("test.v3.type")  # producer left default 1
    await bus.publish(e3)
    check(e3.schema_version == 3, "fresh publish: schema_version stamped from registry")
    check(current_schema_version("test.v3.type") == 3, "current_schema_version lookup")
    check(current_schema_version("test.unregistered") == 1, "unregistered default = 1")

    # ── 3. Producer-supplied seq is preserved ──────────────────────
    e4 = _make_event("test.preset", seq=999)
    await bus.publish(e4)
    check(e4.seq == 999, "producer-supplied seq preserved")
    # Counter is still 3 (last auto-stamp); fresh publish after this
    # increments to 4, NOT past 999. That's correct — preset seqs are
    # the caller's responsibility for ordering, not the bus's.
    e5 = _make_event("test.after.preset")
    await bus.publish(e5)
    check(e5.seq == 4, "fresh publish after preset: continues from counter")

    # ── 4. Replay validates schema_version ─────────────────────────
    register_event_schema("test.v2.type", 2)
    bad = _make_event("test.v2.type", schema_version=1)  # registry says 2
    raised = False
    try:
        await bus.publish(bad, is_replay=True)
    except EventSchemaError as exc:
        raised = True
        check("schema_version=1" in str(exc) and "expected=2" in str(exc),
              "EventSchemaError details version mismatch")
    check(raised, "replay: schema mismatch raises EventSchemaError")

    # Replay BEFORE any subscriber fires — confirm the bad event
    # wasn't recorded.
    check(all(ev.type != "test.v2.type" for ev in seen),
          "replay: mismatched event never reaches subscribers")

    # ── 5. Replay preserves seq + fast-forwards counter ────────────
    bus2 = EventBus()  # fresh bus, counter=0
    bus2.subscribe("*", recorder, name="recorder")
    replayed = _make_event("test.replay", schema_version=1, seq=500)
    await bus2.publish(replayed, is_replay=True)
    check(replayed.seq == 500, "replay: original seq preserved")
    # Now publish fresh — should be 501, not 1.
    fresh_after = _make_event("test.after.replay")
    await bus2.publish(fresh_after)
    check(fresh_after.seq == 501,
          "replay: counter fast-forwarded past replayed seq (501, not 1)")

    # ── 6. is_replay flag on subscriber side ────────────────────────
    seen.clear()
    fresh = _make_event("test.flag.fresh")
    repl = _make_event("test.flag.replay", schema_version=1, seq=1000)
    await bus2.publish(fresh)
    await bus2.publish(repl, is_replay=True)
    check(seen[-2].is_replay is False, "subscriber sees is_replay=False on fresh")
    check(seen[-1].is_replay is True, "subscriber sees is_replay=True on replay")

    # ── 7. register_event_schema idempotent / mismatch raises ──────
    register_event_schema("test.idem", 5)
    register_event_schema("test.idem", 5)  # idempotent
    raised = False
    try:
        register_event_schema("test.idem", 6)
    except ValueError as exc:
        raised = True
        check("test.idem" in str(exc), "ValueError details type")
    check(raised, "mismatched re-registration raises ValueError")

    # ── 8. Exact subscribers bypass glob matching ───────────────────
    import event_bus as event_bus_module

    bus3 = EventBus()
    exact_seen: list[str] = []
    glob_seen: list[str] = []

    async def exact_recorder(ev: BusEvent) -> None:
        exact_seen.append(ev.type)

    async def glob_recorder(ev: BusEvent) -> None:
        glob_seen.append(ev.type)

    bus3.subscribe("test.exact", exact_recorder, priority=20, name="exact")
    original_fnmatchcase = event_bus_module.fnmatch.fnmatchcase

    def fail_fnmatchcase(_name: str, _pat: str) -> bool:
        raise AssertionError("exact subscriber used fnmatchcase")

    event_bus_module.fnmatch.fnmatchcase = fail_fnmatchcase
    try:
        await bus3.publish(_make_event("test.exact"))
    finally:
        event_bus_module.fnmatch.fnmatchcase = original_fnmatchcase
    check(exact_seen == ["test.exact"], "exact subscriber dispatch bypasses fnmatchcase")

    bus3.subscribe("test.*", glob_recorder, priority=30, name="glob")
    await bus3.publish(_make_event("test.exact"))
    check(glob_seen == ["test.exact"], "glob subscriber still dispatches")

    if failures:
        print(f"\n{len(failures)} FAILURES")
        return 1
    print("\nall A16 checks OK")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_run()))
