"""A1b regression: session mutations fan out via the event bus.

Pins the contract:
  1. `main.py` no longer wires `session_manager.add_listener(...)` for
     production subscribers — the legacy listener list is empty at
     startup.
  2. The `session_ws_broadcaster_on_change` bus subscriber is
     registered on `session.*`.
  3. A `session_manager._fire(sid, change)` call schedules a bus
     publish on the bound loop; the bus subscriber routes it to
     `ws_broadcaster.on_change(sid, change)` with the same payload.
  4. `session_manager.add_listener` still works (deprecated path) but
     emits a `DeprecationWarning`.
  5. `_fire` is safe to call from non-loop threads via
     `run_coroutine_threadsafe` — calling from a worker thread does
     not crash and the event still reaches subscribers.

Run with:
    cd backend && .venv/bin/python scripts/test_a1b_session_bus_migration.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Use a tempdir so we don't touch the real session store.
import _test_home
_TMP = _test_home.isolate("bc_a1b_")
os.environ["BETTER_CLAUDE_API_ONLY"] = "1"


def _check(cond: bool, label: str, failures: list[str]) -> None:
    print(f"  {'OK' if cond else 'FAIL'}  {label}")
    if not cond:
        failures.append(label)


async def _run(failures: list[str]) -> None:
    import main
    from event_bus import bus, BusEvent

    sm = main.session_manager
    # Bind the running loop so `_fire`'s bus path is active for this
    # test (main.py's startup hook does this too, but we're not going
    # through on_startup here).
    sm.bind_loop(asyncio.get_running_loop())

    # 1. Legacy listener list empty at startup
    _check(
        len(sm._listeners) == 0,
        f"session_manager._listeners is empty (got {len(sm._listeners)})",
        failures,
    )

    # 2. session_ws_broadcaster_on_change subscriber registered
    sub_names = {s["name"] for s in bus.describe()}
    _check(
        "session_ws_broadcaster_on_change" in sub_names,
        "bus subscriber `session_ws_broadcaster_on_change` registered",
        failures,
    )

    # 3. _fire → bus → on_change end-to-end
    hits: list[tuple[str, dict]] = []
    orig = main.ws_broadcaster.on_change
    main.ws_broadcaster.on_change = lambda sid, change: hits.append((sid, change))
    # Rebind so the subscriber captures the stubbed on_change.
    from event_bus_subscribers import bind_session_ws_broadcaster
    bind_session_ws_broadcaster(main.ws_broadcaster)

    sess = sm.create(name="a1b-test", cwd=_TMP, orchestration_mode="native")
    sid = sess["id"]
    await asyncio.sleep(0.05)  # let async dispatch land
    sm.delete(sid)
    await asyncio.sleep(0.05)

    kinds = [h[1].get("kind") for h in hits]
    _check("created" in kinds, "bus subscriber received `created`", failures)
    _check("deleted" in kinds, "bus subscriber received `deleted`", failures)

    # 4. add_listener emits DeprecationWarning
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        sm.add_listener(lambda sid_, change_: None)
        deprecated = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        _check(
            len(deprecated) >= 1,
            "add_listener emits DeprecationWarning",
            failures,
        )

    # Restore for downstream tests (the test process might continue).
    main.ws_broadcaster.on_change = orig

    # 5. _fire from a worker thread doesn't crash
    hits.clear()
    bind_session_ws_broadcaster(main.ws_broadcaster)
    main.ws_broadcaster.on_change = lambda sid, change: hits.append((sid, change))
    sess2 = sm.create(name="a1b-thread", cwd=_TMP, orchestration_mode="native")

    def thread_work():
        # `set_archived` is a mutator that calls `_fire` synchronously
        # under the per-root lock. From a thread, `_fire`'s bus path
        # must use `run_coroutine_threadsafe` (NOT `loop.create_task`)
        # to avoid corrupting the asyncio loop.
        sm.set_archived(sess2["id"], True)

    await asyncio.to_thread(thread_work)
    await asyncio.sleep(0.05)
    archived_kinds = [h[1].get("kind") for h in hits]
    _check(
        any(k == "archived_set" for k in archived_kinds),
        f"_fire from worker thread reaches bus subscriber (kinds: {archived_kinds})",
        failures,
    )
    sm.delete(sess2["id"])
    main.ws_broadcaster.on_change = orig


def main_entry() -> int:
    failures: list[str] = []
    try:
        asyncio.run(_run(failures))
    finally:
        import shutil
        shutil.rmtree(_TMP, ignore_errors=True)

    if failures:
        print(f"\n{len(failures)} FAILURES")
        return 1
    print("\nall A1b checks OK")
    return 0


if __name__ == "__main__":
    sys.exit(main_entry())
