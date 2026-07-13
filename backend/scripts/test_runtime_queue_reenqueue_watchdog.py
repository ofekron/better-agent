"""Regression: a prompt admitted to the durable queue must self-heal into a
running turn even when its in-memory ``submit_prompt`` was lost (event-loop
starvation / crashed processor / WS handler interrupted before submit) and no
backend restart ever happens.

Before the fix, ``_re_enqueue_queued_prompts`` ran only at startup, so a
prompt lost mid-runtime sat in the persisted queue forever — the user saw a
sent message with no response. The runtime watchdog now drains the durable
queue periodically, and the ``is_prompt_item_in_flight`` gate makes it
double-run-safe regardless of client_id.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-runtime-reenqueue-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import main  # noqa: E402
import session_queue_projection  # noqa: E402


class _SessionManager:
    def __init__(self, queued: list[dict]) -> None:
        self.session = {
            "id": "sid",
            "model": "m",
            "cwd": "/tmp/runtime-reenqueue",
            "messages": [],
            "queued_prompts": queued,
        }
        self.updated: list[tuple[str, str | None, dict]] = []
        self.removed: list[str] = []
        self.rebuild_calls = 0

    def get(self, sid: str) -> dict | None:
        return self.session if sid == "sid" else None

    def get_lite(self, sid: str) -> dict | None:
        return self.get(sid)

    def update_queued_prompt(self, sid, queued_id, updates):
        self.updated.append((sid, queued_id, updates))

    def remove_queued_prompt(self, sid, queued_id, *_args):
        self.removed.append(queued_id)

    def rebuild_queued_prompt_counts(self) -> None:
        self.rebuild_calls += 1


class _FakeTask:
    def __init__(self, done: bool) -> None:
        self._done = done

    def done(self) -> bool:
        return self._done


class _Coordinator:
    def __init__(
        self,
        in_flight: set[str] | None = None,
        claimed: set[str] | None = None,
        processor_task: _FakeTask | None = None,
    ) -> None:
        self.submitted: list[tuple[str, dict]] = []
        # Simulate the in-memory trackers the real coordinator maintains.
        self._queued_ids = {"sid": list(in_flight or set())}
        self._claimed_queued_ids = {"sid": set(claimed or set())}
        self._processor_tasks = {"sid": processor_task} if processor_task else {}

    def is_prompt_item_in_flight(self, sid: str, item_id: str) -> bool:
        # Mirror the real (task-aware) implementation.
        if item_id in self._queued_ids.get(sid, []):
            return True
        if item_id not in self._claimed_queued_ids.get(sid, set()):
            return False
        task = self._processor_tasks.get(sid)
        return task is not None and not task.done()

    async def submit_prompt_async(self, sid: str, params: dict) -> str:
        self.submitted.append((sid, params))
        return params.get("_queued_id") or "runtime-id"


def _patch(fakes):
    real_sm = main.session_manager
    real_co = main.coordinator
    real_list = session_queue_projection.list_queued_records
    real_ensure = session_queue_projection.ensure_current_or_rebuild
    main.session_manager = fakes["sm"]
    main.coordinator = fakes["co"]
    session_queue_projection.list_queued_records = lambda: [fakes["sm"].session]
    session_queue_projection.ensure_current_or_rebuild = lambda: False
    return real_sm, real_co, real_list, real_ensure


def _restore(real_sm, real_co, real_list, real_ensure):
    session_queue_projection.ensure_current_or_rebuild = real_ensure
    session_queue_projection.list_queued_records = real_list
    main.coordinator = real_co
    main.session_manager = real_sm


def _lost_prompt_case() -> int:
    """A persisted prompt the coordinator has no trace of is re-submitted."""
    sm = _SessionManager([{
        "id": "lost-1",
        "content": "dropped under starvation",
        "client_id": "client-lost",
        "orchestration_mode": "native",
    }])
    co = _Coordinator(in_flight=set())
    restored = _patch({"sm": sm, "co": co})
    try:
        asyncio.run(main._re_enqueue_queued_prompts(runtime=True))
        assert len(co.submitted) == 1, f"expected 1 submit, got {len(co.submitted)}"
        assert co.submitted[0][1]["_queued_id"] == "lost-1"
        # Runtime mode must NOT do the full count rebuild.
        assert sm.rebuild_calls == 0, "runtime pass should skip count rebuild"
        # Lost item is not removed by re-enqueue (processor owns removal).
        assert "lost-1" not in sm.removed
        print("PASS runtime re-enqueue drains a lost prompt without restart")
        return 0
    finally:
        _restore(*restored)


def _inflight_skip_case() -> int:
    """A prompt already queued (in _queued_ids) is NOT re-submitted — no
    double-run, even with an empty client_id (the client_id dedup path alone
    is not enough)."""
    sm = _SessionManager([{
        "id": "running-1",
        "content": "already queued",
        "client_id": "",  # empty: client_id dedup would NOT catch a duplicate
        "lifecycle_msg_id": "life-1",
        "orchestration_mode": "native",
    }])
    co = _Coordinator(in_flight={"running-1"})  # present in _queued_ids
    restored = _patch({"sm": sm, "co": co})
    try:
        asyncio.run(main._re_enqueue_queued_prompts(runtime=True))
        assert co.submitted == [], (
            "queued prompt must not be re-submitted (double-run risk)"
        )
        print("PASS runtime re-enqueue skips queued prompt (no double-run)")
        return 0
    finally:
        _restore(*restored)


def _claimed_live_task_skip_case() -> int:
    """A prompt claimed by a LIVE processor task is in-flight — skipped."""
    sm = _SessionManager([{
        "id": "claimed-live",
        "content": "being processed right now",
        "client_id": "ccl",
        "lifecycle_msg_id": "life-cl",
        "orchestration_mode": "native",
    }])
    co = _Coordinator(
        claimed={"claimed-live"},
        processor_task=_FakeTask(done=False),
    )
    restored = _patch({"sm": sm, "co": co})
    try:
        asyncio.run(main._re_enqueue_queued_prompts(runtime=True))
        assert co.submitted == [], "live-claimed prompt must not be re-submitted"
        print("PASS runtime re-enqueue skips live-claimed prompt")
        return 0
    finally:
        _restore(*restored)


def _dead_processor_stale_claim_case() -> int:
    """The gap the adversarial review found: if the processor task died
    after claiming an item (before its finally cleared the claim), the
    claim is stale. The watchdog MUST re-enqueue — otherwise the prompt
    still drops forever, the exact bug this fix targets."""
    sm = _SessionManager([{
        "id": "stale-claimed",
        "content": "processor crashed mid-turn",
        "client_id": "c-stale",
        "lifecycle_msg_id": "life-stale",
        "orchestration_mode": "native",
    }])
    co = _Coordinator(
        claimed={"stale-claimed"},
        processor_task=_FakeTask(done=True),  # dead task -> stale claim
    )
    restored = _patch({"sm": sm, "co": co})
    try:
        asyncio.run(main._re_enqueue_queued_prompts(runtime=True))
        assert len(co.submitted) == 1, (
            "stale claim from a dead processor must be re-enqueued, not dropped"
        )
        assert co.submitted[0][1]["_queued_id"] == "stale-claimed"
        print("PASS stale claim from dead processor is re-enqueued (no silent drop)")
        return 0
    finally:
        _restore(*restored)


def _watchdog_recovers_case() -> int:
    """End-to-end watchdog loop body: simulate a prompt that becomes lost
    after startup, then one watchdog pass recovers it."""
    sm = _SessionManager([{
        "id": "late-lost",
        "content": "lost after boot",
        "client_id": "client-late",
        "orchestration_mode": "native",
    }])
    co = _Coordinator(in_flight=set())
    restored = _patch({"sm": sm, "co": co})
    try:
        # The watchdog calls _re_enqueue_queued_prompts(runtime=True) per pass.
        asyncio.run(main._re_enqueue_queued_prompts(runtime=True))
        assert len(co.submitted) == 1
        assert co.submitted[0][1]["_queued_id"] == "late-lost"
        print("PASS watchdog pass recovers a prompt lost after startup")
        return 0
    finally:
        _restore(*restored)


def main_test() -> int:
    rc = _lost_prompt_case()
    rc |= _inflight_skip_case()
    rc |= _claimed_live_task_skip_case()
    rc |= _dead_processor_stale_claim_case()
    rc |= _watchdog_recovers_case()
    shutil.rmtree(_TMP_HOME, ignore_errors=True)
    return rc


if __name__ == "__main__":
    raise SystemExit(main_test())
