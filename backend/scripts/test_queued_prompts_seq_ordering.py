"""Regression: `queued_prompts_updated` broadcasts must carry a monotonic
`queued_prompts_seq` so the frontend can drop a stale/out-of-order snapshot.

Bug: "queue is being served but still renders" — a `session_metadata_updated`
WS frame carrying a `queued_prompts` snapshot travels an independent,
fire-and-forget path (SessionManager mutation -> EventBus.publish
(schedule_on_bound_loop, not awaited) -> SessionWSBroadcaster.on_change ->
coordinator.schedule_global -> per-connection send), while the
`user_message_persisted` ack for the same prompt is sent via a direct
awaited per-session ws_callback in the turn coroutine, bypassing the
bus/broadcaster entirely. With no shared ordering primitive, a stale
snapshot (captured before a prompt was dequeued) could be delivered to the
frontend AFTER the ack, resurrecting an already-served prompt in the
queue banner.

Fix: every mutator of `queued_prompts` (`admit_queued_prompt`,
`update_queued_prompt`, `reserve_queued_prompt_delivery_attempt`,
`remove_queued_prompt`, `remove_queued_prompt_by_client_id`) bumps a
per-session monotonic `queued_prompts_seq` via
`SessionManager._bump_queued_prompts_seq`, which is threaded onto the
fired change dict and onto the `session_metadata_updated` WS patch
(`session_ws_broadcaster.py`). The frontend (see
`frontend/tests/queuedPrompts.test.ts`) drops any `queued_prompts` patch
whose `queued_prompts_seq` is not strictly newer than the one already
applied.

This test proves the backend half: the seq strictly increases across
every queue mutation and is present on every `queued_prompts_updated`
change/broadcast, AND that the directly-awaited `user_message_persisted`
ack (`UserPromptManager.notify_user_msg_persisted`) carries the
post-dequeue seq — closing the first-observation gap where a stale
admission broadcast, delayed past the ack, would otherwise resurrect an
already-served prompt because the frontend had never seen a prior queue
snapshot to compare against.

Run with:
    cd backend && .venv/bin/python scripts/test_queued_prompts_seq_ordering.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _test_home
_TMP = _test_home.isolate("bc_queue_seq_")
os.environ["BETTER_CLAUDE_API_ONLY"] = "1"


def _check(cond: bool, label: str, failures: list[str]) -> None:
    print(f"  {'OK' if cond else 'FAIL'}  {label}")
    if not cond:
        failures.append(label)


async def _run(failures: list[str]) -> None:
    import main

    sm = main.session_manager
    sm.bind_loop(asyncio.get_running_loop())

    hits: list[tuple[str, dict]] = []
    sm.add_listener(lambda sid, change: hits.append((sid, change)))

    sess = sm.create(name="queue-seq-test", cwd=_TMP, orchestration_mode="native")
    sid = sess["id"]

    def queue_hits() -> list[dict]:
        return [c for s, c in hits if s == sid and c.get("kind") == "queued_prompts_updated"]

    # 1. Admission bumps the seq and stamps it on the change dict.
    sm.admit_queued_prompt(sid, {"id": "p1", "text": "hello", "client_id": "c1"})
    seq_after_admit = sm.get(sid).get("queued_prompts_seq")
    _check(
        isinstance(seq_after_admit, int) and seq_after_admit >= 1,
        f"admit bumps queued_prompts_seq to a positive int (got {seq_after_admit!r})",
        failures,
    )
    _check(
        queue_hits()[-1].get("queued_prompts_seq") == seq_after_admit,
        "admit's change dict carries the bumped seq",
        failures,
    )

    # 2. A second admission (different prompt) bumps it again, strictly.
    sm.admit_queued_prompt(sid, {"id": "p2", "text": "world", "client_id": "c2"})
    seq_after_second_admit = sm.get(sid).get("queued_prompts_seq")
    _check(
        seq_after_second_admit > seq_after_admit,
        f"second admit strictly increases seq ({seq_after_admit!r} -> {seq_after_second_admit!r})",
        failures,
    )

    # 3. Reserving a delivery attempt bumps it again.
    sm.reserve_queued_prompt_delivery_attempt(sid, "p1")
    seq_after_reserve = sm.get(sid).get("queued_prompts_seq")
    _check(
        seq_after_reserve > seq_after_second_admit,
        f"delivery-attempt reservation strictly increases seq ({seq_after_second_admit!r} -> {seq_after_reserve!r})",
        failures,
    )
    _check(
        queue_hits()[-1].get("queued_prompts_seq") == seq_after_reserve,
        "reserve's change dict carries the bumped seq",
        failures,
    )

    # 4. Removing (serving) p1 bumps it again — this is the mutation that
    #    corresponds to the item being served in the real bug scenario.
    sm.remove_queued_prompt(sid, "p1")
    seq_after_remove = sm.get(sid).get("queued_prompts_seq")
    _check(
        seq_after_remove > seq_after_reserve,
        f"remove (serve) strictly increases seq ({seq_after_reserve!r} -> {seq_after_remove!r})",
        failures,
    )
    last_remove_hit = queue_hits()[-1]
    _check(
        last_remove_hit.get("queued_prompts_seq") == seq_after_remove,
        "remove's change dict carries the bumped seq",
        failures,
    )
    remaining_ids = [p.get("id") for p in (last_remove_hit.get("queued_prompts") or [])]
    _check(
        remaining_ids == ["p2"],
        f"remove's broadcast snapshot no longer contains the served prompt (got {remaining_ids})",
        failures,
    )

    # 5. remove_queued_prompt_by_client_id also bumps it.
    sm.remove_queued_prompt_by_client_id(sid, "c2")
    seq_after_remove_by_cid = sm.get(sid).get("queued_prompts_seq")
    _check(
        seq_after_remove_by_cid > seq_after_remove,
        f"remove_by_client_id strictly increases seq ({seq_after_remove!r} -> {seq_after_remove_by_cid!r})",
        failures,
    )

    # 6. The direct `user_message_persisted` ack must carry the
    #    post-dequeue `queued_prompts_seq` so the frontend can establish a
    #    staleness floor even on the very first queue snapshot it ever
    #    sees (ADV-found gap: without this, a stale admission broadcast
    #    delayed past the ack would still resurrect the served prompt,
    #    since the frontend would have no prior stored seq to compare
    #    against). `notify_user_msg_persisted` only reads session_manager
    #    state — no coordinator collaboration is needed for this method.
    import user_prompt_manager as upm_mod

    upm = upm_mod.UserPromptManager(None)
    sm.admit_queued_prompt(sid, {"id": "p3", "text": "third", "client_id": "c3"})
    seq_after_third_admit = sm.get(sid).get("queued_prompts_seq")
    sm.remove_queued_prompt(sid, "p3")
    seq_after_third_remove = sm.get(sid).get("queued_prompts_seq")
    _check(
        seq_after_third_remove > seq_after_third_admit,
        "serving the third prompt bumps the seq past its admission",
        failures,
    )

    acked_payloads: list[dict] = []

    async def _fake_ws_callback(frame: dict) -> None:
        acked_payloads.append(frame)

    await upm.notify_user_msg_persisted(_fake_ws_callback, sid, {"id": "m3", "client_id": "c3"})
    _check(
        len(acked_payloads) == 1 and acked_payloads[0].get("type") == "user_message_persisted",
        "ack fires exactly one user_message_persisted frame",
        failures,
    )
    acked_seq = (acked_payloads[0].get("data") or {}).get("queued_prompts_seq")
    _check(
        acked_seq == seq_after_third_remove,
        f"ack payload carries the post-dequeue queued_prompts_seq (got {acked_seq!r}, expected {seq_after_third_remove!r})",
        failures,
    )
    _check(
        acked_seq is not None and acked_seq > seq_after_third_admit,
        "ack's seq is newer than the admission that queued the served prompt "
        "(this is the floor the frontend uses to reject a stale admission "
        "broadcast even before it has ever seen a queue snapshot)",
        failures,
    )

    sm.delete(sid)


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
    print("\nqueued_prompts_seq ordering check OK")
    return 0


if __name__ == "__main__":
    sys.exit(main_entry())
