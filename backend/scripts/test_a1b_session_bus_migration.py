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
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Use a tempdir so we don't touch the real session store.
import _test_home
_TEST_HOME = _test_home.TestHome.acquire("bc_a1b_")
_TMP = _TEST_HOME.path
os.environ["BETTER_CLAUDE_API_ONLY"] = "1"


def setup_function(_function) -> None:
    _test_home.engage(_TEST_HOME.path)


def teardown_module() -> None:
    _TEST_HOME.release()


def _check(cond: bool, label: str, failures: list[str]) -> None:
    print(f"  {'OK' if cond else 'FAIL'}  {label}")
    if not cond:
        failures.append(label)


class _ChangeProbe:
    def __init__(self) -> None:
        self.hits: list[tuple[str, dict]] = []
        self._delivered: dict[tuple[str, str], asyncio.Event] = {}

    def __call__(self, sid: str, change: dict) -> None:
        self.hits.append((sid, change))
        key = (sid, str(change.get("kind")))
        self._delivered.setdefault(key, asyncio.Event()).set()

    async def wait_for(self, sid: str, kind: str) -> None:
        key = (sid, kind)
        if any(
            hit_sid == sid and change.get("kind") == kind
            for hit_sid, change in self.hits
        ):
            return
        await asyncio.wait_for(
            self._delivered.setdefault(key, asyncio.Event()).wait(),
            timeout=2.0,
        )


async def _run(failures: list[str]) -> None:
    import main
    import loop_affinity
    from event_bus import bus
    from event_bus_subscribers import bind_session_ws_broadcaster

    sm = main.session_manager
    previous_sm_loop = sm._loop
    previous_main_loop = loop_affinity.main_loop()
    sm.bind_loop(asyncio.get_running_loop())
    orig = main.ws_broadcaster.on_change
    probe = _ChangeProbe()

    def legacy_listener(sid_, change_):
        return None

    live_sids: set[str] = set()
    main.ws_broadcaster.on_change = probe
    bind_session_ws_broadcaster(main.ws_broadcaster)

    try:
        _check(
            len(sm._listeners) == 0,
            f"session_manager._listeners is empty (got {len(sm._listeners)})",
            failures,
        )

        sub_names = {s["name"] for s in bus.describe()}
        _check(
            "session_ws_broadcaster_on_change" in sub_names,
            "bus subscriber `session_ws_broadcaster_on_change` registered",
            failures,
        )

        sess = sm.create(name="a1b-test", cwd=_TMP, orchestration_mode="native")
        sid = sess["id"]
        live_sids.add(sid)
        await probe.wait_for(sid, "created")
        sm.delete(sid)
        await probe.wait_for(sid, "deleted")
        live_sids.remove(sid)

        kinds = [
            change.get("kind") for hit_sid, change in probe.hits if hit_sid == sid
        ]
        _check("created" in kinds, "bus subscriber received `created`", failures)
        _check("deleted" in kinds, "bus subscriber received `deleted`", failures)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            sm.add_listener(legacy_listener)
            deprecated = [
                w for w in caught if issubclass(w.category, DeprecationWarning)
            ]
            _check(
                len(deprecated) >= 1,
                "add_listener emits DeprecationWarning",
                failures,
            )

        sess2 = sm.create(name="a1b-thread", cwd=_TMP, orchestration_mode="native")
        sid2 = sess2["id"]
        live_sids.add(sid2)
        await probe.wait_for(sid2, "created")
        await asyncio.to_thread(sm.set_archived, sid2, True)
        await probe.wait_for(sid2, "archived_set")
        archived_kinds = [
            change.get("kind") for hit_sid, change in probe.hits if hit_sid == sid2
        ]
        _check(
            "archived_set" in archived_kinds,
            f"_fire from worker thread reaches bus subscriber (kinds: {archived_kinds})",
            failures,
        )
        sm.delete(sid2)
        await probe.wait_for(sid2, "deleted")
        live_sids.remove(sid2)
    finally:
        for sid in live_sids:
            sm.delete(sid)
        sm.flush_pending_persists()
        if legacy_listener in sm._listeners:
            sm._listeners.remove(legacy_listener)
        main.ws_broadcaster.on_change = orig
        bind_session_ws_broadcaster(main.ws_broadcaster)
        sm._loop = previous_sm_loop
        if previous_main_loop is None:
            loop_affinity.reset_for_tests()
        else:
            loop_affinity.bind_main_loop(previous_main_loop)


def test_session_bus_migration() -> None:
    failures: list[str] = []
    asyncio.run(_run(failures))
    assert not failures, "\n".join(failures)


def main_entry() -> int:
    failures: list[str] = []
    try:
        asyncio.run(_run(failures))
    finally:
        _TEST_HOME.release()

    if failures:
        print(f"\n{len(failures)} FAILURES")
        return 1
    print("\nall A1b checks OK")
    return 0


if __name__ == "__main__":
    sys.exit(main_entry())
