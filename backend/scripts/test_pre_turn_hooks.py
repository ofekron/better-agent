"""Tests for the extension pre_turn lifecycle hook (mirror of post_turn).

Covers the schema gate (_validate_hooks), the pre_turn_hooks() accessor over
installed extensions, and the bind_pre_turn_hooks() bus subscriber wiring on
lifecycle.turn_start. Runs in an isolated BETTER_AGENT_HOME.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import _test_home
_test_home.isolate("bc_test_pre_turn_hooks_")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import extension_store  # noqa: E402
from event_bus import BusEvent, bus  # noqa: E402
from event_bus_subscribers import bind_pre_turn_hooks  # noqa: E402


def _hooks(value, *, has_backend=True):
    return extension_store._validate_hooks(value, has_backend=has_backend)


async def _run() -> int:
    failures: list[str] = []

    def check(cond: bool, label: str) -> None:
        print(f"  {'OK' if cond else 'FAIL'}  {label}")
        if not cond:
            failures.append(label)

    # Schema: pre_turn validates like post_turn.
    check(_hooks({"pre_turn": "/pre-turn"}) == {"pre_turn": "/pre-turn"},
          "pre_turn accepted with backend")
    check(_hooks({}, has_backend=False) == {},
          "no hooks is fine without backend")
    try:
        _hooks({"pre_turn": "/pre-turn"}, has_backend=False)
        check(False, "pre_turn without backend rejected")
    except extension_store.ExtensionError:
        check(True, "pre_turn without backend rejected")
    try:
        _hooks({"pre_turn": "pre-turn"})
        check(False, "pre_turn must start with /")
    except extension_store.ExtensionError:
        check(True, "pre_turn must start with /")
    try:
        _hooks({"bogus": "/x"})
        check(False, "unknown hook key rejected")
    except extension_store.ExtensionError:
        check(True, "unknown hook key rejected")
    # pre_turn + post_turn can coexist.
    both = _hooks({"pre_turn": "/pre", "post_turn": "/post"})
    check(both == {"pre_turn": "/pre", "post_turn": "/post"},
          "pre_turn and post_turn coexist")

    # Accessor over installed extensions (isolated home → none installed).
    check(extension_store.pre_turn_hooks() == [],
          "pre_turn_hooks() empty when no extensions declare it")

    # Dispatcher subscribes to lifecycle.turn_start, idempotently.
    bind_pre_turn_hooks()
    bind_pre_turn_hooks()  # second bind must replace, not duplicate
    subs = [s for s in bus.describe() if s["name"] == "extension_pre_turn_hooks"]
    check(len(subs) == 1, "exactly one extension_pre_turn_hooks subscriber")
    check(subs and subs[0]["pattern"] == "lifecycle.turn_start",
          "subscriber bound to lifecycle.turn_start")

    # Firing turn_start with no hooks declared is a no-op (early return,
    # isolated errors never reach the bus). Must not raise.
    ev = BusEvent(type="lifecycle.turn_start", root_id="r", sid="s", payload={})
    await bus.publish(ev)
    check(True, "turn_start publish with no hooks does not raise")

    if failures:
        print(f"\n{len(failures)} FAILURES")
        return 1
    print("\npre-turn hook checks OK")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_run()))
