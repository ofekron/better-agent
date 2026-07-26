"""Deleting a session closes its open session tab.

The open-tab list is backend-owned durable state (`ui_selection.json`),
so a deleted session must not leave a tab behind for any client. Pins:

  1. `ui_selection.close_session_tabs` prunes only the given ids, keeps
     the surviving tabs' `joined_at`, and reports "nothing changed" so
     callers can skip a no-op broadcast.
  2. A deleted session's id is tombstoned, so a client replaying a
     pre-delete tab list (durable offline write backlog, or a PATCH that
     raced the delete) cannot resurrect the tab.
  3. `session_manager.delete` publishes the whole removed subtree as
     `deleted_sids`, so deleting a root also closes its forks' tabs.
  4. End-to-end: deleting a session prunes its tab and broadcasts the
     new `ui_selection_changed` snapshot to every client.

Run with:
    cd backend && .venv/bin/python scripts/test_session_delete_closes_tab.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _test_home
_TMP = _test_home.isolate("bc_delete_tab_")
os.environ["BETTER_CLAUDE_API_ONLY"] = "1"


def _check(cond: bool, label: str, failures: list[str]) -> None:
    print(f"  {'OK' if cond else 'FAIL'}  {label}")
    if not cond:
        failures.append(label)


def _test_close_session_tabs(failures: list[str]) -> None:
    import ui_selection

    ui_selection.set_open_session_tab_ids(["a", "b", "c"])
    joined_before = ui_selection.get_all()["open_session_tab_joined_at"]

    snapshot = ui_selection.close_session_tabs(["b"])
    _check(
        snapshot is not None and snapshot["open_session_tab_ids"] == ["a", "c"],
        "close_session_tabs prunes only the closed id",
        failures,
    )
    _check(
        snapshot is not None
        and snapshot["open_session_tab_joined_at"] == {
            "a": joined_before["a"], "c": joined_before["c"],
        },
        "close_session_tabs keeps surviving tabs' joined_at and drops the closed one",
        failures,
    )
    _check(
        ui_selection.close_session_tabs(["b"]) is None,
        "close_session_tabs reports no change when no tab was open",
        failures,
    )
    _check(
        ui_selection.close_session_tabs([]) is None,
        "close_session_tabs is a no-op for an empty id list",
        failures,
    )
    _check(
        ui_selection.get_all()["open_session_tab_ids"] == ["a", "c"],
        "pruned tab list is persisted",
        failures,
    )


def _test_stale_writes_cannot_resurrect(failures: list[str]) -> None:
    """A client whose tab list predates the delete replays it on reconnect
    (durable write backlog) — the deleted tab must not come back."""
    import ui_selection

    ui_selection.set_open_session_tab_ids(["keep", "doomed"])
    ui_selection.close_session_tabs(["doomed"])

    ui_selection.set_open_session_tab_ids(["keep", "doomed"])
    _check(
        ui_selection.get_all()["open_session_tab_ids"] == ["keep"],
        "a stale absolute tab list cannot re-open a deleted session's tab",
        failures,
    )

    # Concurrent writers: a PATCH landing around the prune must not leave the
    # deleted tab persisted, whichever order they complete in.
    import threading

    ui_selection.set_open_session_tab_ids(["keep", "racer"])
    barrier = threading.Barrier(2)

    def _patch() -> None:
        barrier.wait()
        ui_selection.set_open_session_tab_ids(["keep", "racer"])

    def _prune() -> None:
        barrier.wait()
        ui_selection.close_session_tabs(["racer"])

    threads = [threading.Thread(target=_patch), threading.Thread(target=_prune)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    _check(
        ui_selection.get_all()["open_session_tab_ids"] == ["keep"],
        "a PATCH racing the delete cannot leave the deleted tab persisted",
        failures,
    )


async def _test_delete_closes_tabs(failures: list[str]) -> None:
    import main
    import ui_selection
    import ui_selection_projection

    sm = main.session_manager
    sm.bind_loop(asyncio.get_running_loop())

    broadcasts: list[tuple[str, dict]] = []
    broadcast_arrived = asyncio.Event()

    async def _capture(event_type: str, payload: dict) -> None:
        broadcasts.append((event_type, payload))
        broadcast_arrived.set()

    ui_selection_projection.bind(_capture)

    async def _await_broadcast() -> None:
        await asyncio.wait_for(broadcast_arrived.wait(), timeout=10)
        broadcast_arrived.clear()

    kept = sm.create(name="kept", cwd=_TMP, orchestration_mode="native")
    doomed = sm.create(name="doomed", cwd=_TMP, orchestration_mode="native")
    # Forking requires the parent to have taken a turn (it needs a native
    # agent session to branch from).
    sm.set_agent_sid(doomed["id"], "native", str(uuid.uuid4()))
    fork = sm.fork(doomed["id"], name="doomed-fork")

    fired: list[dict] = []
    orig_fire = sm._fire

    def _spy_fire(sid, change):
        if change.get("kind") == "deleted":
            fired.append(change)
        return orig_fire(sid, change)

    sm._fire = _spy_fire
    try:
        ui_selection.set_open_session_tab_ids([kept["id"], doomed["id"], fork["id"]])
        broadcasts.clear()

        _check(
            sm.delete(doomed["id"]),
            "session_manager.delete removed the session",
            failures,
        )
        await _await_broadcast()
    finally:
        sm._fire = orig_fire

    _check(
        len(fired) == 1
        and set(fired[0].get("deleted_sids") or []) == {doomed["id"], fork["id"]},
        "deleted event carries the whole removed subtree as deleted_sids",
        failures,
    )
    _check(
        ui_selection.get_all()["open_session_tab_ids"] == [kept["id"]],
        "deleting a session closes its tab and its fork's tab, keeping the others",
        failures,
    )
    ui_selection_changed = [b for b in broadcasts if b[0] == "ui_selection_changed"]
    _check(
        len(ui_selection_changed) == 1
        and ui_selection_changed[0][1]["open_session_tab_ids"] == [kept["id"]],
        "the pruned snapshot is broadcast so every client's tab strip converges",
        failures,
    )

    # A delete for a session with no open tab must not spam a broadcast. The
    # sentinel delete that follows it DOES broadcast, so awaiting the sentinel
    # proves the untabbed delete's projection already ran and stayed silent.
    broadcasts.clear()
    untabbed = sm.create(name="untabbed", cwd=_TMP, orchestration_mode="native")
    sentinel = sm.create(name="sentinel", cwd=_TMP, orchestration_mode="native")
    ui_selection.set_open_session_tab_ids([kept["id"], sentinel["id"]])
    sm.delete(untabbed["id"])
    sm.delete(sentinel["id"])
    await _await_broadcast()
    _check(
        [b[1]["open_session_tab_ids"] for b in broadcasts if b[0] == "ui_selection_changed"]
        == [[kept["id"]]],
        "deleting a session with no open tab broadcasts nothing",
        failures,
    )

    ui_selection_projection.unbind()


def main_() -> int:
    failures: list[str] = []
    print("ui_selection.close_session_tabs")
    _test_close_session_tabs(failures)
    print("stale/racing writes cannot resurrect a closed tab")
    _test_stale_writes_cannot_resurrect(failures)
    print("session delete -> tab closed")
    asyncio.run(_test_delete_closes_tabs(failures))
    print()
    if failures:
        print(f"FAILED ({len(failures)}): " + "; ".join(failures))
        return 1
    print("PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main_())
