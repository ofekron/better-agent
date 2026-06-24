"""Regression tests for A17's subscriber-failure isolation contract on
the event bus.

Pins the contract:
  1. A subscriber raising does NOT break sibling subscribers — every
     other matching subscriber still fires for the same event.
  2. `publish()` always returns successfully from the caller's
     perspective — the publisher never re-raises a subscriber's
     exception. (Required by A3's WAL contract: "publisher succeeds
     whenever fsync succeeded.")
  3. A failure emits a structured `subscriber_failed` meta-event with
     `original_type`, `original_seq`, `subscriber_name`, `error_class`,
     `error_message`.
  4. A subscriber failure on a `subscriber_failed` event does NOT
     recursively emit another meta-event — recursion is broken at
     the publish site by checking `event.type != _SUBSCRIBER_FAILED_TYPE`.
  5. The `subscriber_failed` meta-event has `persist=False` so it
     doesn't enter the WAL / events.jsonl (avoids feedback loops
     when the persistence subscriber is the one failing).

Run with:
    cd backend && .venv/bin/python scripts/test_event_bus_subscriber_isolation.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from event_bus import BusEvent, EventBus  # noqa: E402


def _make_event(event_type: str = "test.work") -> BusEvent:
    return BusEvent(
        type=event_type,
        root_id="root-1",
        sid="sid-1",
        payload={"hello": "world"},
    )


async def _run():
    failures: list[str] = []

    def check(cond: bool, label: str) -> None:
        print(f"  {'OK' if cond else 'FAIL'}  {label}")
        if not cond:
            failures.append(label)

    # ── 1+2. Failing subscriber doesn't break siblings; publish() returns ──
    bus = EventBus()
    sibling_seen: list[BusEvent] = []

    async def bad_sub(_ev: BusEvent) -> None:
        raise RuntimeError("synthetic boom")

    async def good_sub(ev: BusEvent) -> None:
        sibling_seen.append(ev)

    # Register the bad one FIRST (lower-priority numbers fire first;
    # default priority is 50 — set the bad one to 10 so it fires before
    # the good one. This is the worst case for isolation: bad fires
    # first, must not prevent good from firing.).
    bus.subscribe("test.*", bad_sub, priority=10, name="bad_sub")
    bus.subscribe("test.*", good_sub, priority=50, name="good_sub")

    ev = _make_event("test.work")
    try:
        await bus.publish(ev)
        check(True, "publish() returns successfully even when subscriber raises")
    except Exception as e:
        check(False, f"publish() re-raised: {e!r}")

    check(len(sibling_seen) == 1 and sibling_seen[0] is ev,
          "sibling subscriber fires after failing subscriber")

    # ── 3. subscriber_failed meta-event emitted with full payload ──────
    meta_seen: list[BusEvent] = []

    async def meta_recorder(ev: BusEvent) -> None:
        meta_seen.append(ev)

    bus.subscribe("subscriber_failed", meta_recorder, name="meta_recorder")

    sibling_seen.clear()
    ev2 = _make_event("test.work")
    await bus.publish(ev2)
    check(len(meta_seen) == 1, "exactly one subscriber_failed emitted")
    meta = meta_seen[0]
    check(meta.type == "subscriber_failed", "meta event type")
    check(meta.payload.get("original_type") == "test.work",
          "meta carries original_type")
    check(meta.payload.get("subscriber_name") == "bad_sub",
          "meta carries subscriber_name")
    check(meta.payload.get("error_class") == "RuntimeError",
          "meta carries error_class")
    check("synthetic boom" in meta.payload.get("error_message", ""),
          "meta carries error_message")
    check(meta.payload.get("original_seq") == ev2.seq,
          "meta carries original_seq")

    # ── 4. Failure on a subscriber_failed event does NOT recurse ───────
    bus2 = EventBus()
    bad_meta_count = [0]

    async def bad_meta_sub(_ev: BusEvent) -> None:
        bad_meta_count[0] += 1
        raise RuntimeError("meta handler explodes")

    bus2.subscribe("subscriber_failed", bad_meta_sub, name="bad_meta")
    # Also a regular failing subscriber whose failure ⇒ subscriber_failed
    bus2.subscribe("test.*", bad_sub, name="bad_work")

    await bus2.publish(_make_event("test.recurse"))
    check(bad_meta_count[0] == 1,
          "subscriber_failed handler called exactly once (no recursion)")

    # ── 5. subscriber_failed has persist=False ────────────────────────
    check(meta.persist is False,
          "subscriber_failed meta is persist=False (avoids WAL feedback)")

    if failures:
        print(f"\n{len(failures)} FAILURES")
        return 1
    print("\nall A17 isolation checks OK")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_run()))
