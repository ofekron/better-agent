"""Unit tests for backend/auto_restart_on_idle.py.

The monitor is driven by calling `_tick()` directly with a controllable
busy signal, so the tests are deterministic and need no real agent work.

Run with:
    cd backend && .venv/bin/python scripts/test_auto_restart_on_idle.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

# Per CLAUDE.md: isolate ~/.better-claude state to a tempdir BEFORE
# importing any backend module.
import _test_home

_TMP_HOME = _test_home.isolate("bc-test-auto-restart-on-idle-")

import auto_restart_on_idle  # noqa: E402

_REAL_GET_ENV = auto_restart_on_idle.get_env

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"

_results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, ok, detail))


def _make_monitor(
    *,
    busy_sequence: list[bool],
    enabled: bool = True,
    supervisor: bool = True,
    new_commit: bool = True,
    cooldown_remaining: float = 0.0,
):
    """Build a monitor whose busy signal returns busy_sequence values in
    order, and record every restart trigger."""
    state = {"i": 0}
    triggered: list[str] = []
    fired_records: list[int] = []

    def is_busy() -> bool:
        i = state["i"]
        state["i"] = min(i + 1, len(busy_sequence) - 1)
        return busy_sequence[i]

    async def trigger_restart(request_id: str) -> None:
        triggered.append(request_id)

    def record_restart_fired() -> None:
        fired_records.append(1)

    # The module resolves get_env from its own global at call time, so patch
    # the module global to simulate running under the supervisor or not.
    orig_get_env = auto_restart_on_idle.get_env

    def patched_get_env(key: str):
        if key == auto_restart_on_idle.SUPERVISOR_ENV:
            return "1" if supervisor else None
        return _REAL_GET_ENV(key)

    auto_restart_on_idle.get_env = patched_get_env  # type: ignore[assignment]

    mon = auto_restart_on_idle.AutoRestartOnIdleMonitor(
        is_busy=is_busy,
        trigger_restart=trigger_restart,
        is_enabled=lambda: enabled,
        has_new_commit=lambda: new_commit,
        restart_cooldown_remaining=lambda: cooldown_remaining,
        record_restart_fired=record_restart_fired,
        poll_interval=0,
    )
    return mon, triggered, fired_records


async def _run() -> None:
    # 1. Fires exactly once on a busy→idle transition.
    mon, triggered, _fired = _make_monitor(busy_sequence=[True, False])
    await mon._tick()  # busy=True  -> was_busy becomes True, no fire
    await mon._tick()  # busy=False -> transition -> fire
    await mon._tick()  # already triggered -> no-op
    check(
        "fires once on busy->idle",
        len(triggered) == 1,
        f"triggered {len(triggered)} times",
    )

    # 2. Does NOT fire on the initial idle (no prior busy).
    mon, triggered, _fired = _make_monitor(busy_sequence=[False, False])
    await mon._tick()
    await mon._tick()
    check(
        "no fire on initial idle",
        len(triggered) == 0,
        f"triggered {len(triggered)} times",
    )

    # 3. Does NOT fire when the pref is disabled, even across a transition.
    mon, triggered, _fired = _make_monitor(busy_sequence=[True, False], enabled=False)
    await mon._tick()
    await mon._tick()
    check(
        "no fire when disabled",
        len(triggered) == 0,
        f"triggered {len(triggered)} times",
    )

    # 4. Does NOT fire off-supervisor (no BETTER_CLAUDE_RUN_SH_SUPERVISOR).
    mon, triggered, _fired = _make_monitor(busy_sequence=[True, False], supervisor=False)
    await mon._tick()
    await mon._tick()
    check(
        "no fire off-supervisor",
        len(triggered) == 0,
        f"triggered {len(triggered)} times",
    )

    # 5. Stays busy -> no fire.
    mon, triggered, _fired = _make_monitor(busy_sequence=[True, True])
    await mon._tick()
    await mon._tick()
    check(
        "no fire while still busy",
        len(triggered) == 0,
        f"triggered {len(triggered)} times",
    )

    # 6. Does NOT fire when work completes but the running process already
    #    matches the current repo commit.
    mon, triggered, _fired = _make_monitor(busy_sequence=[True, False], new_commit=False)
    await mon._tick()
    await mon._tick()
    check(
        "no fire without newer commit",
        len(triggered) == 0,
        f"triggered {len(triggered)} times",
    )

    # 6. Re-enabling while idle does not fire on stale busy history: the
    #    disabled branch resets _was_busy, so a subsequent enable requires a
    #    fresh busy period before firing.
    mon, triggered, _fired = _make_monitor(
        busy_sequence=[True, False, False, True, False]
    )
    mon._is_enabled = lambda: False  # type: ignore[assignment]
    await mon._tick()  # disabled + busy -> resets was_busy, no fire
    mon._is_enabled = lambda: True  # type: ignore[assignment]
    await mon._tick()  # now idle, but was_busy was reset -> no fire
    await mon._tick()  # busy again
    await mon._tick()  # idle -> transition -> fire
    check(
        "no stale fire after re-enable",
        len(triggered) == 1,
        f"triggered {len(triggered)} times",
    )

    enabled_thread_ids: list[int] = []
    main_thread_id = threading.get_ident()

    def enabled_off_loop() -> bool:
        enabled_thread_ids.append(threading.get_ident())
        return True

    mon, _triggered, _fired = _make_monitor(busy_sequence=[False], enabled=True)
    mon._is_enabled = enabled_off_loop  # type: ignore[assignment]
    await mon._tick()
    check(
        "pref read runs off loop",
        bool(enabled_thread_ids) and all(t != main_thread_id for t in enabled_thread_ids),
        f"thread ids {enabled_thread_ids}, main {main_thread_id}",
    )

    # 7. Does NOT fire on a busy->idle transition while a prior auto-restart
    #    is still cooling down, even with a newer commit available. This is
    #    the cross-process restart-storm guard: a freshly respawned process
    #    has no in-memory memory of a prior fire, only the persisted
    #    cooldown protects it.
    mon, triggered, fired = _make_monitor(
        busy_sequence=[True, False], cooldown_remaining=120.0
    )
    await mon._tick()
    await mon._tick()
    check(
        "no fire while cooling down",
        len(triggered) == 0 and len(fired) == 0,
        f"triggered {len(triggered)} times, fired records {len(fired)}",
    )

    # 8. Fires and records the fire when cooldown has elapsed.
    mon, triggered, fired = _make_monitor(
        busy_sequence=[True, False], cooldown_remaining=0.0
    )
    await mon._tick()
    await mon._tick()
    check(
        "fires and records when cooldown elapsed",
        len(triggered) == 1 and len(fired) == 1,
        f"triggered {len(triggered)} times, fired records {len(fired)}",
    )

    # 9. A cooldown-skip resets was_busy just like the no-new-commit skip, so
    #    a subsequent idle tick (with no fresh busy period in between) does
    #    not immediately retry and fire.
    mon, triggered, fired = _make_monitor(
        busy_sequence=[True, False, False], cooldown_remaining=120.0
    )
    await mon._tick()  # busy=True
    await mon._tick()  # busy=False -> transition, cooldown blocks, was_busy reset
    await mon._tick()  # busy=False again -> no transition (was_busy already False)
    check(
        "cooldown skip does not retry without a fresh busy period",
        len(triggered) == 0 and len(fired) == 0,
        f"triggered {len(triggered)} times, fired records {len(fired)}",
    )


def main() -> int:
    try:
        asyncio.run(_run())
    finally:
        auto_restart_on_idle.get_env = _REAL_GET_ENV  # type: ignore[assignment]
    all_ok = True
    for name, ok, detail in _results:
        status = PASS if ok else FAIL
        suffix = f" ({detail})" if detail and not ok else ""
        print(f"{status}  {name}{suffix}")
        all_ok = all_ok and ok
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
