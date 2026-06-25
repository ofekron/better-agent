"""Regression: concurrent same-client_id sends can't both be admitted.

A prompt re-dispatched with the SAME client_id (offline backlog replay
after a brief reconnect) used to slip past the WS handler's read-only
dedup checks and broadcast a phantom `user_message_queued` bubble before
`submit_prompt` deduped it — the user saw the prompt twice.

The fix adds an atomic admission claim (`try_claim_prompt_client_id`)
taken BEFORE anything is persisted/emitted, and teaches `submit_prompt`
to honor a claim the handler already took (`_client_id_claimed`). This
locks both halves:
  * a second same-client_id claim is detected as a duplicate;
  * the owning send is still admitted (not self-deduped) when the handler
    pre-claimed it, while a non-pre-claimed duplicate is still deduped.

Run:
    cd backend && .venv/bin/python scripts/test_prompt_client_id_claim_race.py
"""

from __future__ import annotations

import os
import shutil
import sys
import asyncio

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-client-id-claim-race-")
os.environ["BETTER_CLAUDE_TEST_AUTH_BYPASS"] = "1"

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import main  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _reset(co) -> None:
    co._active_prompt_client_ids.clear()
    co._prompt_client_id_by_item.clear()
    co._prompt_queues.clear()
    co._queued_ids.clear()
    co._processor_tasks.clear()


def test_claim_detects_duplicate() -> bool:
    co = main.coordinator
    _reset(co)
    sid, cid = "sess-claim", "pending-123"

    first = co.try_claim_prompt_client_id(sid, "item1", cid)
    dup = co.try_claim_prompt_client_id(sid, "item2", cid)
    no_cid = co.try_claim_prompt_client_id(sid, "item3", None)
    co._forget_active_prompt_item("item1")
    after_release = co.try_claim_prompt_client_id(sid, "item4", cid)

    ok = (
        first is None          # first send claims it
        and dup == "item1"     # concurrent duplicate detected → handler echoes + skips
        and no_cid is None     # absent client_id never claims
        and after_release is None  # claim freed at turn end ⇒ reusable
    )
    print(f"{PASS if ok else FAIL} atomic claim detects a concurrent same-client_id duplicate")
    if not ok:
        print({"first": first, "dup": dup, "no_cid": no_cid, "after_release": after_release})
    return ok


async def _submit_path() -> bool:
    co = main.coordinator
    _reset(co)

    async def _noop(*_a, **_k):
        return None

    original_proc = co._run_session_processor
    co._run_session_processor = _noop
    try:
        sid, cid = "sess-submit", "pending-777"

        # Handler pre-claims, then submits with the claim flag.
        pre = co.try_claim_prompt_client_id(sid, "itemA", cid)
        admitted = co.submit_prompt(
            sid,
            {"client_id": cid, "_queued_id": "itemA", "_client_id_claimed": True},
            _adv_sync_checked=True,
        )
        # A concurrent duplicate WITHOUT a pre-claim must still dedup to itemA.
        deduped = co.submit_prompt(
            sid,
            {"client_id": cid, "_queued_id": "itemB"},
            _adv_sync_checked=True,
        )
        return pre is None and admitted == "itemA" and deduped == "itemA"
    finally:
        co._run_session_processor = original_proc


def test_submit_honors_claim_and_dedups_others() -> bool:
    ok = asyncio.run(_submit_path())
    print(f"{PASS if ok else FAIL} submit_prompt admits the claim owner, dedups other duplicates")
    return ok


def test_cancel_releases_claim() -> bool:
    """Cancelling a claimed prompt must release its claim, else a future
    genuine re-send of that client_id is blocked forever."""
    import asyncio as _aio
    co = main.coordinator
    _reset(co)
    sid, cid = "sess-cancel", "pending-555"

    co._prompt_queues[sid] = _aio.Queue()
    claimed = co.try_claim_prompt_client_id(sid, "itemX", cid)
    co._prompt_queues[sid].put_nowait({"_queued_id": "itemX", "client_id": cid})

    co.cancel_queued(sid)
    reusable = co.try_claim_prompt_client_id(sid, "itemY", cid)

    ok = (
        claimed is None
        and reusable is None  # claim released ⇒ client_id reusable
        and "itemX" not in co._prompt_client_id_by_item
    )
    print(f"{PASS if ok else FAIL} cancel releases the client_id claim (no leak)")
    if not ok:
        print({"claimed": claimed, "reusable": reusable})
    return ok


def main_run() -> int:
    try:
        results = [
            test_claim_detects_duplicate(),
            test_submit_honors_claim_and_dedups_others(),
            test_cancel_releases_claim(),
        ]
        return 0 if all(results) else 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main_run())
