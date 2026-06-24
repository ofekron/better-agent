"""Backend tests for adversarial-sync.

Covers:
  * Convergence parser: whitespace normalization, code-fence wrap,
    missing FINAL, anti-collusion (FINAL == original), multiple FINAL
    blocks (last wins).
  * State machine: mocked `_run_turn` drives the driver to a converged
    state; status, agreed_text, rounds_completed populated.
  * Round cap: non-converging replies hit MAX_ADV_SYNC_ROUNDS → failed.
  * WS broadcaster: `adv_sync_updated` change kind → `session_metadata_updated`
    frame with `adv_sync_overlays` patch (NOT inline_tags).
  * Queue gate: `submit_prompt` on an adv_sync_fork whose parent has a
    running overlay raises RuntimeError.
  * Cancellation: cancel_adv_sync stops the driver and flips status.
  * Recovery: overlays in `status=running` flip to `interrupted` at
    startup recovery time.

Run with:
    cd backend && .venv/bin/python scripts/test_adv_sync.py
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

# Per CLAUDE.md: isolate ~/.better-claude state to a tempdir BEFORE
# importing any backend module.
import _test_home
_TMP_HOME = _test_home.isolate("bc-test-adv-sync-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import session_store  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402
from orchs import adv_sync  # noqa: E402


PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _reset_home() -> None:
    sessions_dir = Path(_TMP_HOME) / "sessions"
    if sessions_dir.exists():
        shutil.rmtree(sessions_dir)
    session_store._fork_index.clear()
    session_store._index_loaded = False
    session_manager._roots.clear()
    session_manager._node_root_id.clear()
    session_manager._root_locks.clear()
    session_manager._batches.clear()


def _make_root_with_message() -> tuple[dict, str]:
    """Create a root with a fake claude_sid + one assistant message.
    Returns (root, assistant_message_id)."""
    root = session_manager.create(name="adv-sync-test", cwd="/tmp")
    session_manager.set_agent_sid(
        root["id"], "manager", f"fake-claude-{root['id'][:8]}",
    )
    session_manager.append_user_msg(root["id"], {
        "id": "u1", "role": "user", "content": "hi", "events": [],
        "timestamp": "2026-05-01T00:00:00", "isStreaming": False,
    })
    session_manager.append_user_msg(root["id"], {
        "id": "a1", "role": "assistant",
        "content": "the answer is X",
        "events": [],
        "timestamp": "2026-05-01T00:00:01", "isStreaming": False,
    })
    return session_manager.get(root["id"]), "a1"


# ──────────────────────────────────────────────────────────────────────
# Convergence parser
# ──────────────────────────────────────────────────────────────────────


def test_parser_table() -> bool:
    _reset_home()
    cases = [
        # (supportive, adversarial, original, expected_converged, label)
        (
            "reasoning\n<FINAL>Hello world</FINAL>",
            "reasoning\n<FINAL>Hello world</FINAL>",
            "Goodbye world",
            True,
            "exact-match converges",
        ),
        (
            "<FINAL>\n  Hello world  \n</FINAL>",
            "<FINAL>Hello world</FINAL>",
            "Goodbye world",
            True,
            "whitespace normalized",
        ),
        (
            "<FINAL>```Hello world```</FINAL>",
            "<FINAL>Hello world</FINAL>",
            "Goodbye world",
            True,
            "code-fence stripped",
        ),
        (
            "no final at all",
            "<FINAL>Hello</FINAL>",
            "Goodbye",
            False,
            "missing FINAL on one side",
        ),
        (
            "<FINAL>Hello</FINAL>",
            "<FINAL>World</FINAL>",
            "Hello",
            False,
            "FINALs differ",
        ),
        (
            "<FINAL>Goodbye world</FINAL>",
            "<FINAL>Goodbye world</FINAL>",
            "Goodbye world",
            False,
            "anti-collusion: FINAL == original",
        ),
        (
            "<FINAL>draft1</FINAL>\nactually <FINAL>final2</FINAL>",
            "<FINAL>final2</FINAL>",
            "original",
            True,
            "last FINAL wins",
        ),
    ]
    ok_all = True
    for sup, adv, orig, expected, label in cases:
        converged, agreed = adv_sync.is_converged(sup, adv, orig)
        if converged != expected:
            print(f"{FAIL} parser case '{label}': expected {expected}, got {converged}")
            ok_all = False
        elif converged and not agreed:
            print(f"{FAIL} parser case '{label}': converged but no agreed_text")
            ok_all = False
    if ok_all:
        print(f"{PASS} convergence parser table ({len(cases)} cases)")
    return ok_all


# ──────────────────────────────────────────────────────────────────────
# State machine (mocked _run_turn)
# ──────────────────────────────────────────────────────────────────────


class _StubCoordinator:
    """Bare-bones stub providing `_run_turn`, `_dispatch_raw`,
    `cancel_session`, and a dict `_adv_sync_drivers`. The stub's
    `_run_turn` appends a canned assistant message to the target fork's
    session so `_last_assistant_text` returns it on the next read."""

    def __init__(self, reply_provider) -> None:
        # reply_provider(fork_id, round_idx) -> str
        self._reply_provider = reply_provider
        self._round_by_fork: dict[str, int] = {}
        self.cancelled_sessions: list[str] = []
        self._adv_sync_drivers: dict = {}

    async def _run_turn(self, **kw) -> None:
        fork_id = kw["app_session_id"]
        round_idx = self._round_by_fork.get(fork_id, 0) + 1
        self._round_by_fork[fork_id] = round_idx
        reply = self._reply_provider(fork_id, round_idx)
        session_manager.append_user_msg(fork_id, {
            "id": f"{fork_id[:6]}-r{round_idx}u",
            "role": "user", "content": kw["prompt"], "events": [],
            "timestamp": "2026-05-01T00:00:00", "isStreaming": False,
        })
        session_manager.append_user_msg(fork_id, {
            "id": f"{fork_id[:6]}-r{round_idx}a",
            "role": "assistant", "content": reply, "events": [],
            "timestamp": "2026-05-01T00:00:01", "isStreaming": False,
        })

    async def _dispatch_raw(self, app_session_id, event_dict) -> None:
        return

    async def cancel_session(self, sid: str) -> int:
        self.cancelled_sessions.append(sid)
        return 0


def test_state_machine_converges() -> bool:
    _reset_home()
    root, msg_id = _make_root_with_message()

    def reply(fork_id, round_idx):
        # Both forks emit the same FINAL on round 2 → converges.
        if round_idx == 1:
            return f"thinking about it... <FINAL>draft round 1 {fork_id[:4]}</FINAL>"
        return "we agree now. <FINAL>the agreed version</FINAL>"

    coord = _StubCoordinator(reply)
    overlay = asyncio.run(adv_sync.start_adv_sync(
        coord,
        parent_session_id=root["id"],
        message_id=msg_id,
        selected_text="the answer is X",
    ))
    # Drain the background task.
    drivers = coord._adv_sync_drivers
    asyncio.run(_drain(drivers.get(overlay["id"])))

    fresh = session_manager.get(root["id"])
    overlays = fresh.get("adv_sync_overlays") or []
    if len(overlays) != 1:
        print(f"{FAIL} state_machine: expected 1 overlay, got {len(overlays)}")
        return False
    ov = overlays[0]
    ok = (
        ov.get("status") == "converged"
        and ov.get("agreed_text") == "the agreed version"
        and ov.get("rounds_completed") == 2
        and ov.get("supportive_fork_id")
        and ov.get("adversarial_fork_id")
    )
    if not ok:
        print(f"{FAIL} state_machine: overlay={ov}")
        return False
    # Both forks should be tagged adv_sync_fork.
    sup = session_manager.get(ov["supportive_fork_id"])
    adv = session_manager.get(ov["adversarial_fork_id"])
    if not (sup and sup.get("kind") == "adv_sync_fork"):
        print(f"{FAIL} state_machine: supportive fork kind={sup and sup.get('kind')}")
        return False
    if not (adv and adv.get("kind") == "adv_sync_fork"):
        print(f"{FAIL} state_machine: adversarial fork kind={adv and adv.get('kind')}")
        return False
    print(f"{PASS} state machine: running → converged, agreed_text set")
    return True


def test_round_cap_fails() -> bool:
    _reset_home()
    root, msg_id = _make_root_with_message()
    # Each fork emits a DIFFERENT non-converging FINAL every round.
    def reply(fork_id, round_idx):
        return f"<FINAL>{fork_id[:6]}-{round_idx}</FINAL>"
    coord = _StubCoordinator(reply)
    overlay = asyncio.run(adv_sync.start_adv_sync(
        coord,
        parent_session_id=root["id"],
        message_id=msg_id,
        selected_text="anything",
    ))
    asyncio.run(_drain(coord._adv_sync_drivers.get(overlay["id"])))
    fresh = session_manager.get(root["id"])
    ov = (fresh.get("adv_sync_overlays") or [])[0]
    ok = (
        ov.get("status") == "failed"
        and ov.get("rounds_completed") == adv_sync.MAX_ADV_SYNC_ROUNDS
        and "max rounds" in (ov.get("error") or "")
    )
    if not ok:
        print(f"{FAIL} round_cap: overlay={ov}")
        return False
    print(f"{PASS} round cap → status=failed after MAX_ADV_SYNC_ROUNDS")
    return True


# ──────────────────────────────────────────────────────────────────────
# WS broadcaster mapping
# ──────────────────────────────────────────────────────────────────────


def test_ws_broadcaster_emits_overlay_patch() -> bool:
    _reset_home()
    root, msg_id = _make_root_with_message()

    # Stand up the broadcaster against a fake coordinator that captures
    # outgoing frames.
    captured: list[dict] = []

    class _CapCoord:
        async def broadcast_global(self, type_, data):
            captured.append({"type": type_, "data": data})

    from session_ws_broadcaster import SessionWSBroadcaster
    broadcaster = SessionWSBroadcaster(_CapCoord())

    # Subscribe the broadcaster to the manager.
    session_manager.add_listener(broadcaster.on_change)
    try:
        overlay = {
            "id": "ovl-1",
            "message_id": msg_id,
            "original_text": "x",
            "agreed_text": None,
            "status": "running",
            "supportive_fork_id": "sup-1",
            "adversarial_fork_id": "adv-1",
            "rounds_completed": 0,
            "max_rounds": 6,
            "created_at": "t",
            "updated_at": "t",
            "error": None,
        }
        # Listeners fire synchronously; broadcaster.on_change schedules
        # dispatch on the running loop. We don't have one in this sync
        # test, so we wrap in asyncio.run to give it a loop.
        async def _do():
            session_manager.add_adv_sync_overlay(root["id"], overlay)
            session_manager.update_adv_sync_overlay(
                root["id"], "ovl-1", {"status": "converged", "agreed_text": "y"},
            )
            # Yield to let the create_task'd broadcasts run.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
        asyncio.run(_do())
    finally:
        session_manager._listeners.remove(broadcaster.on_change)

    # Find the two session_metadata_updated frames carrying
    # adv_sync_overlays.
    relevant = [
        c for c in captured
        if c.get("type") == "session_metadata_updated"
        and "adv_sync_overlays" in (c.get("data", {}).get("patch") or {})
    ]
    if len(relevant) < 2:
        print(f"{FAIL} ws_broadcaster: expected 2+ overlay frames, got {len(relevant)} of {len(captured)}")
        return False
    # The patch must NOT carry inline_tags (the previous buggy fall-through).
    for c in relevant:
        patch = c["data"]["patch"]
        if "inline_tags" in patch:
            print(f"{FAIL} ws_broadcaster: leaked inline_tags into adv_sync patch")
            return False
    # Last frame should show the converged overlay.
    last_overlays = relevant[-1]["data"]["patch"]["adv_sync_overlays"]
    if not (last_overlays and last_overlays[0].get("status") == "converged"):
        print(f"{FAIL} ws_broadcaster: last frame missing converged status")
        return False
    print(f"{PASS} ws_broadcaster: adv_sync_updated → session_metadata_updated patch carries adv_sync_overlays")
    return True


# ──────────────────────────────────────────────────────────────────────
# Queue gate
# ──────────────────────────────────────────────────────────────────────


def test_submit_prompt_rejects_locked_fork() -> bool:
    _reset_home()
    root, msg_id = _make_root_with_message()
    # Manually create a fork tagged adv_sync_fork and a running overlay
    # on the parent referencing it.
    fork = session_manager.fork(root["id"], kind="adv_sync_fork")
    session_manager.add_adv_sync_overlay(root["id"], {
        "id": "ovl-gate",
        "message_id": msg_id,
        "original_text": "x",
        "agreed_text": None,
        "status": "running",
        "supportive_fork_id": fork["id"],
        "adversarial_fork_id": "other",
        "rounds_completed": 0,
        "max_rounds": 6,
        "created_at": "t",
        "updated_at": "t",
        "error": None,
    })
    # Now exercise the gate via a real coordinator instance.
    from orchestrator import Coordinator
    coord = Coordinator()
    try:
        coord.submit_prompt(fork["id"], {"prompt": "hi", "app_session_id": fork["id"]})
        print(f"{FAIL} queue_gate: submit_prompt did not raise")
        return False
    except RuntimeError as e:
        if "adv_sync_fork locked" not in str(e):
            print(f"{FAIL} queue_gate: unexpected error: {e}")
            return False
    print(f"{PASS} queue gate: submit_prompt rejects locked adv_sync_fork")
    return True


# ──────────────────────────────────────────────────────────────────────
# Cancellation
# ──────────────────────────────────────────────────────────────────────


def test_cancel_stops_driver() -> bool:
    _reset_home()
    root, msg_id = _make_root_with_message()

    # _run_turn that blocks forever so we can observe cancel.
    started = asyncio.Event()
    cancelled = asyncio.Event()

    class _BlockingCoord(_StubCoordinator):
        async def _run_turn(self, **kw):
            started.set()
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                cancelled.set()
                raise

    coord = _BlockingCoord(lambda *a, **k: "")

    async def run_test():
        overlay = await adv_sync.start_adv_sync(
            coord,
            parent_session_id=root["id"],
            message_id=msg_id,
            selected_text="x",
        )
        await started.wait()
        await adv_sync.cancel_adv_sync(
            coord,
            parent_session_id=root["id"],
            overlay_id=overlay["id"],
        )
        # Driver task should have been cancelled.
        return overlay

    overlay = asyncio.run(run_test())
    fresh = session_manager.get(root["id"])
    ov = next(
        (o for o in fresh.get("adv_sync_overlays") or [] if o.get("id") == overlay["id"]),
        None,
    )
    if not ov or ov.get("status") != "stopped":
        print(f"{FAIL} cancel: overlay status={ov and ov.get('status')}")
        return False
    if not coord.cancelled_sessions:
        print(f"{FAIL} cancel: cancel_session was not invoked on forks")
        return False
    print(f"{PASS} cancel: driver cancelled + forks cancel_session'd + status=stopped")
    return True


# ──────────────────────────────────────────────────────────────────────
# Recovery
# ──────────────────────────────────────────────────────────────────────


def test_recovery_flips_running_to_interrupted() -> bool:
    _reset_home()
    root, msg_id = _make_root_with_message()
    overlay = {
        "id": "ovl-rec",
        "message_id": msg_id,
        "original_text": "x",
        "agreed_text": None,
        "status": "running",  # zombie on disk
        "supportive_fork_id": "fake-sup",
        "adversarial_fork_id": "fake-adv",
        "rounds_completed": 1,
        "max_rounds": 6,
        "created_at": "t",
        "updated_at": "t",
        "error": None,
    }
    session_manager.add_adv_sync_overlay(root["id"], overlay)
    # Simulate restart by clearing caches; on-disk state is what matters.
    session_manager._roots.clear()
    session_manager._node_root_id.clear()
    session_store._fork_index.clear()
    session_store._index_loaded = False

    flipped = adv_sync.recover_running_overlays_on_startup()
    if flipped != 1:
        print(f"{FAIL} recovery: expected 1 flip, got {flipped}")
        return False
    on_disk = json.loads(
        (Path(_TMP_HOME) / "sessions" / f"{root['id']}.json").read_text()
    )
    ov = (on_disk.get("adv_sync_overlays") or [])[0]
    if ov.get("status") != "interrupted":
        print(f"{FAIL} recovery: on-disk status={ov.get('status')}")
        return False
    print(f"{PASS} recovery: running → interrupted on startup scan")
    return True


# ──────────────────────────────────────────────────────────────────────
# Driver helpers
# ──────────────────────────────────────────────────────────────────────


async def _drain(task) -> None:
    """Await `task` to completion. Tolerates None (task already
    drained or never created)."""
    if task is None:
        return
    try:
        await task
    except asyncio.CancelledError:
        pass


# ──────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────


def main() -> int:
    tests = [
        test_parser_table,
        test_state_machine_converges,
        test_round_cap_fails,
        test_ws_broadcaster_emits_overlay_patch,
        test_submit_prompt_rejects_locked_fork,
        test_cancel_stops_driver,
        test_recovery_flips_running_to_interrupted,
    ]
    results = []
    for fn in tests:
        try:
            results.append(fn())
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"{FAIL} {fn.__name__} raised: {e}")
            results.append(False)
    n_pass = sum(1 for r in results if r)
    n_total = len(results)
    print(f"\n{n_pass}/{n_total} adv-sync tests passed")
    shutil.rmtree(_TMP_HOME, ignore_errors=True)
    return 0 if n_pass == n_total else 1


if __name__ == "__main__":
    sys.exit(main())
