"""Unit coverage for session_queue_projection_dispatcher.py.

The ProjectionDispatcher batches session-record upserts onto a single-worker
ThreadPoolExecutor and surfaces drain/close/overlay semantics; the
DispatcherRegistry owns one shared dispatcher per storage context. This file
covers every branch through both, using a FakeRuntime that satisfies the
dispatcher's narrow collaborator contract (``upsert(record, durable=False)``)
and real StorageContext dataclasses built directly (the registry only compares
them via ``equivalent_to``).

Run with:
    cd backend && .venv/bin/python scripts/test_session_queue_projection_dispatcher.py
"""

from __future__ import annotations

import copy
import os
import sys
import threading
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-queue-dispatcher-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from session_queue_projection_dispatcher import (  # noqa: E402
    DispatcherRegistry,
    ProjectionDispatcher,
)
from session_queue_projection_runtime import StorageContext  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _check(cond: bool, name: str, detail: str = "") -> bool:
    print(f"{PASS if cond else FAIL} {name}{'' if cond else ' -- ' + detail}")
    return cond


# --------------------------------------------------------------------------- #
# Collaborators
# --------------------------------------------------------------------------- #
class FakeRuntime:
    """Satisfies the dispatcher's only runtime dependency: upsert(record,
    durable=False). Records every call (deep-copied) and can be made to fail
    on demand to exercise the failure/requeue paths."""

    def __init__(self) -> None:
        self.upserts: list[dict] = []
        self._fail_times = 0          # fail this many leading attempts
        self._fail_always = False
        self._gate: threading.Event | None = None  # block on gate.wait() if set
        self._on_enter: threading.Event | None = None

    def upsert(self, record: dict, *, durable: bool = False) -> None:
        if self._on_enter is not None:
            self._on_enter.set()
        if self._gate is not None:
            self._gate.wait()
        self.upserts.append(copy.deepcopy(record))
        if self._fail_always or self._fail_times > 0:
            self._fail_times -= 1
            raise RuntimeError("fake upsert failure")


def _ctx(tag: str, *, contract: str = "default") -> StorageContext:
    # Direct dataclass construction: the dispatcher only compares contexts via
    # equivalent_to, so we never need a real sessions dir or state root.
    return StorageContext(
        identity=Path(f"/tmp/home-{tag}/sessions"),
        database_path=Path(f"/tmp/home-{tag}/queue/projection.sqlite3"),
        session_files=lambda: (),
        enumerator_contract=contract,
    )


def _dispatcher(tag: str = "d", *, runtime: FakeRuntime | None = None) -> tuple[ProjectionDispatcher, FakeRuntime]:
    rt = runtime or FakeRuntime()
    return ProjectionDispatcher(_ctx(tag), rt), rt


# --------------------------------------------------------------------------- #
# submit: validation + deepcopy isolation + worker dispatch
# --------------------------------------------------------------------------- #
def test_submit_validation_and_deepcopy() -> bool:
    ok = True
    disp, rt = _dispatcher("submit")
    # Non-str / empty ids are silently dropped (no worker, no upsert).
    for bad in ({"id": None}, {"id": 123}, {"id": ""}, {"data": {"x": 1}}):
        disp.submit(bad)
    ok = _check(rt.upserts == [], "invalid ids dropped, no upsert") and ok

    rec = {"id": "s1", "v": 1}
    disp.submit(rec)
    # Mutating the caller's record after submit must not leak into the worker.
    rec["v"] = 999
    disp.drain(timeout=2.0)
    ok = _check(len(rt.upserts) == 1, "one valid record upserted") and ok
    ok = _check(rt.upserts[0].get("v") == 1,
                "worker saw pre-mutation deepcopy", f"{rt.upserts}") and ok
    disp.close(timeout=2.0)
    return ok


def test_submit_while_closing() -> bool:
    ok = True
    # From a NON-producer thread while closing -> RuntimeError.
    disp, _ = _dispatcher("close-raise")
    disp._state = "closing"
    raised = False
    try:
        disp.submit({"id": "s1"})
    except RuntimeError:
        raised = True
    ok = _check(raised, "submit while closing (non-producer) raises") and ok
    ok = _check(disp._pending == {}, "nothing enqueued on the raise") and ok

    # From a PRODUCER thread while closing -> allowed + enqueued. The producer
    # enters while open; a concurrent close() would then flip state to closing
    # while the producer is still registered (which is exactly the allowed case).
    disp2, rt2 = _dispatcher("close-allow")
    with disp2.producer():
        disp2._state = "closing"          # simulate close() flipping state mid-producer
        disp2.submit({"id": "s2"})
        ok = _check("s2" in disp2._pending, "producer submit while closing enqueues") and ok
        disp2._state = "open"             # restore so close() can drain cleanly
    disp2.drain(timeout=2.0)
    ok = _check(len(rt2.upserts) == 1, "enqueued record drained") and ok
    disp2.close(timeout=2.0)
    return ok


# --------------------------------------------------------------------------- #
# producer(): nesting + close-raise + cleanup
# --------------------------------------------------------------------------- #
def test_producer_nesting() -> bool:
    ok = True
    disp, _ = _dispatcher("prod")
    tid = threading.get_ident()
    with disp.producer():
        ok = _check(disp._active_producers == 1, "one active producer") and ok
        ok = _check(disp._producer_threads[tid] == 1, "thread depth 1") and ok
        with disp.producer():
            # Nested entry on the same thread increments depth, not the count.
            ok = _check(disp._active_producers == 2, "two active producers") and ok
            ok = _check(disp._producer_threads[tid] == 2, "thread depth 2") and ok
        ok = _check(disp._producer_threads[tid] == 1,
                    "inner exit keeps thread depth at 1") and ok
    ok = _check(tid not in disp._producer_threads,
                "outer exit pops the thread entry") and ok
    ok = _check(disp._active_producers == 0, "all producers released") and ok

    # Entering producer while not open raises and does not register.
    disp._state = "closing"
    raised = False
    try:
        with disp.producer():
            pass
    except RuntimeError:
        raised = True
    ok = _check(raised, "producer() while closing raises") and ok
    ok = _check(disp._active_producers == 0, "raise left no producer registered") and ok
    disp._state = "closed"
    return ok


# --------------------------------------------------------------------------- #
# overlay(): pending vs inflight vs missing, deepcopy
# --------------------------------------------------------------------------- #
def test_overlay() -> bool:
    ok = True
    disp, _ = _dispatcher("overlay")
    disp._pending["p"] = {"id": "p", "src": "pending"}
    disp._inflight["i"] = {"id": "i", "src": "inflight"}
    out = disp.overlay(["p", "i", "missing"])
    ok = _check(set(out) == {"p", "i"}, "pending+inflight selected, missing skipped",
                f"{list(out)}") and ok
    ok = _check(out["p"]["src"] == "pending" and out["i"]["src"] == "inflight",
                "overlay returns the right source per id") and ok
    # Returned dict is a deepcopy.
    out["p"]["src"] = "mutated"
    ok = _check(disp._pending["p"]["src"] == "pending",
                "overlay deepcopy does not leak into internal state") and ok
    return ok


# --------------------------------------------------------------------------- #
# drain(): empty fast path + failure-restart + final-failure + timeout
# --------------------------------------------------------------------------- #
def test_drain_empty_fast_path() -> bool:
    ok = True
    disp, _ = _dispatcher("drain-empty")
    # Nothing pending -> returns immediately without timing out.
    disp.drain(timeout=2.0)
    ok = _check(True, "drain on empty dispatcher returned cleanly") and ok
    return ok


def test_drain_failure_then_success() -> bool:
    ok = True
    disp, rt = _dispatcher("drain-retry")
    rt._fail_times = 1  # first attempt fails, second succeeds
    disp.submit({"id": "s1", "n": 1})
    disp.drain(timeout=2.0)
    ok = _check(len(rt.upserts) == 2, "record retried after failure -> 2 attempts",
                f"{len(rt.upserts)}") and ok
    ok = _check(disp._pending == {} and disp._inflight == {},
                "pending+inflight cleared after recovery") and ok
    disp.close(timeout=2.0)
    return ok


def test_drain_failure_requeue_skip_when_replaced() -> bool:
    ok = True
    # When a newer submit replaces _pending[sid] before the failed worker
    # requeues, the stale record is dropped (current is not None) and only the
    # failure flag + the newer record remain. The newer record then drains.
    disp, rt = _dispatcher("drain-replace")
    gate = threading.Event()
    entered = threading.Event()
    rt._gate = gate
    rt._on_enter = entered
    rt._fail_times = 1  # the in-flight (stale) attempt will fail after unblocking

    disp.submit({"id": "s1", "n": 1})  # becomes inflight, blocks in upsert
    entered.wait(2.0)
    # While the worker is blocked on the first record, replace the pending slot
    # with a newer record and release the worker so its upsert "fails".
    disp._pending["s1"] = {"id": "s1", "n": 2}
    gate.set()
    # Now drain: failure is cleared, worker retries the NEWER pending record
    # (n=2), which succeeds.
    disp.drain(timeout=2.0)
    ok = _check(rt.upserts[0].get("n") == 1, "first attempt was the stale record") and ok
    ok = _check(any(u.get("n") == 2 for u in rt.upserts),
                "newer record drained after stale failure", f"{rt.upserts}") and ok
    disp.close(timeout=2.0)
    return ok


def test_drain_timeout_on_persistent_failure() -> bool:
    ok = True
    disp, rt = _dispatcher("drain-timeout")
    rt._fail_always = True
    disp.submit({"id": "s1"})
    raised = False
    try:
        disp.drain(timeout=0.2)
    except TimeoutError:
        raised = True
    ok = _check(raised, "persistent failure -> drain TimeoutError") and ok
    ok = _check(bool(disp._pending or disp._inflight),
                "work still outstanding when drain timed out") and ok
    # Clean up: stop retrying by making the runtime succeed, then drain+close.
    rt._fail_always = False
    rt._fail_times = 0
    disp.drain(timeout=2.0)
    disp.close(timeout=2.0)
    return ok


def test_drain_final_failure_raise() -> bool:
    ok = True
    # The post-loop guard (failure set with no pending/inflight/scheduled work)
    # is reachable by injecting a bare failure flag, then draining.
    disp, _ = _dispatcher("drain-final")
    disp._failure = RuntimeError("leftover")
    raised = False
    try:
        disp.drain(timeout=2.0)
    except RuntimeError as exc:
        raised = "leftover" in str(exc) or exc.__cause__ is not None
    ok = _check(raised, "post-loop failure flag -> RuntimeError") and ok
    return ok


# --------------------------------------------------------------------------- #
# close(): producer-quiesce timeout + executor shutdown + state transition
# --------------------------------------------------------------------------- #
def test_close_producer_quiesce_timeout() -> bool:
    ok = True
    disp, _ = _dispatcher("close-timeout")
    # An active producer that never releases blocks close's quiesce wait.
    holder = disp.producer()
    holder.__enter__()
    raised = False
    try:
        disp.close(timeout=0.2)
    except TimeoutError:
        raised = True
    ok = _check(raised, "close with live producer -> TimeoutError") and ok
    ok = _check(disp._state == "closing", "state is 'closing' after timeout") and ok
    # Release the producer so the executor thread can shut down on process exit.
    holder.__exit__(None, None, None)
    disp.close(timeout=2.0)
    return ok


def test_close_drains_and_shuts_down() -> bool:
    ok = True
    disp, rt = _dispatcher("close-ok")
    disp.submit({"id": "s1"})
    disp.close(timeout=2.0)
    ok = _check(len(rt.upserts) == 1, "close drained the queued record") and ok
    ok = _check(disp._state == "closed", "state is 'closed' after close") and ok
    ok = _check(disp._executor is None, "executor cleared after close") and ok
    return ok


# --------------------------------------------------------------------------- #
# _start_worker_locked: already-scheduled skip + executor.submit failure
# --------------------------------------------------------------------------- #
def test_start_worker_locked_guards() -> bool:
    ok = True
    disp, _ = _dispatcher("start-guard")

    class _BrokenExecutor:
        def submit(self, *_a, **_k):
            raise RuntimeError("executor refused")

    # _failure set -> skip (worker never scheduled).
    disp._failure = RuntimeError("x")
    disp._start_worker_locked()
    ok = _check(disp._worker_scheduled is False,
                "failure flag skips worker scheduling") and ok
    disp._failure = None

    # Pre-seed a real worker so the already-scheduled short-circuit fires.
    disp._worker_scheduled = True
    disp._start_worker_locked()
    ok = _check(True, "already-scheduled short-circuit did not raise") and ok
    disp._worker_scheduled = False

    # Broken executor: submit raises -> worker_scheduled reset and propagated.
    disp._executor = _BrokenExecutor()  # type: ignore[assignment]
    raised = False
    try:
        disp._start_worker_locked()
    except RuntimeError:
        raised = True
    ok = _check(raised, "executor.submit failure propagated") and ok
    ok = _check(disp._worker_scheduled is False,
                "worker_scheduled reset after submit failure") and ok
    disp._executor = None
    return ok


# --------------------------------------------------------------------------- #
# _drain_worker: empty-exit + stale-inflight skip
# --------------------------------------------------------------------------- #
def test_drain_worker_stale_inflight() -> bool:
    ok = True
    disp, rt = _dispatcher("stale")
    gate = threading.Event()
    entered = threading.Event()
    rt._gate = gate
    rt._on_enter = entered
    disp.submit({"id": "s1", "n": 1})  # goes inflight, worker blocks in upsert
    entered.wait(2.0)
    ok = _check(disp._inflight.get("s1", {}).get("n") == 1,
                "record is inflight while worker blocked") and ok
    # Replace the inflight entry with a different record while the worker holds
    # the original; on success the equality check fails and the pop is skipped.
    stale = {"id": "s1", "n": 999}
    disp._inflight["s1"] = stale
    gate.set()
    # Give the worker a beat to finish its success branch (no pop).
    for _ in range(100):
        if not disp._worker_scheduled:
            break
        disp._cv.acquire()
        disp._cv.notify_all()
        disp._cv.release()
        threading.Event().wait(0.01)
    ok = _check(disp._inflight.get("s1") is stale,
                "stale inflight entry left in place (equality skip)", f"{disp._inflight}") and ok
    # Restore consistency so close() can drain cleanly.
    disp._inflight.clear()
    disp._pending.clear()
    disp.close(timeout=2.0)
    return ok


# --------------------------------------------------------------------------- #
# DispatcherRegistry: acquire / close / release
# --------------------------------------------------------------------------- #
def test_registry_acquire_create_reuse_mismatch() -> bool:
    ok = True
    reg = DispatcherRegistry()
    ctx_a = _ctx("reg-a")
    ctx_a2 = StorageContext(  # equivalent to ctx_a (same id/db/contract)
        identity=ctx_a.identity,
        database_path=ctx_a.database_path,
        session_files=lambda: (),
        enumerator_contract=ctx_a.enumerator_contract,
    )
    rt = FakeRuntime()
    d1 = reg.acquire(ctx_a, rt)
    ok = _check(reg.acquire(ctx_a2, rt) is d1,
                "equivalent context reuses the same dispatcher") and ok
    ctx_b = _ctx("reg-b")  # different identity/db
    raised = False
    try:
        reg.acquire(ctx_b, rt)
    except RuntimeError:
        raised = True
    ok = _check(raised, "non-equivalent context -> mismatch RuntimeError") and ok
    reg.close(ctx_a, timeout=2.0)
    return ok


def test_registry_close_noop_and_mismatch() -> bool:
    ok = True
    reg = DispatcherRegistry()
    # close on a registry with no dispatcher is a no-op.
    reg.close(_ctx("noop"), timeout=2.0)
    ok = _check(True, "close on empty registry is a no-op") and ok

    ctx_a = _ctx("reg-close-a")
    ctx_b = _ctx("reg-close-b")
    reg.acquire(ctx_a, FakeRuntime())
    raised = False
    try:
        reg.close(ctx_b, timeout=2.0)
    except RuntimeError:
        raised = True
    ok = _check(raised, "close with mismatched context -> RuntimeError") and ok
    ok = _check(reg._dispatcher is not None,
                "dispatcher retained after mismatched close") and ok
    reg.close(ctx_a, timeout=2.0)
    ok = _check(reg._dispatcher is not None and reg._dispatcher._state == "closed",
                "matching close shuts the dispatcher down (closed, retained)",
                f"{reg._dispatcher and reg._dispatcher._state}") and ok
    return ok


def test_registry_release() -> bool:
    ok = True
    reg = DispatcherRegistry()
    ctx_a = _ctx("reg-rel-a")
    ctx_b = _ctx("reg-rel-b")
    reg.acquire(ctx_a, FakeRuntime())
    # Non-matching context -> dispatcher retained.
    reg.release(ctx_b)
    ok = _check(reg._dispatcher is not None,
                "non-matching release keeps dispatcher") and ok
    # Matching context -> cleared.
    reg.release(ctx_a)
    ok = _check(reg._dispatcher is None, "matching release clears dispatcher") and ok
    return ok


TESTS = [
    test_submit_validation_and_deepcopy,
    test_submit_while_closing,
    test_producer_nesting,
    test_overlay,
    test_drain_empty_fast_path,
    test_drain_failure_then_success,
    test_drain_failure_requeue_skip_when_replaced,
    test_drain_timeout_on_persistent_failure,
    test_drain_final_failure_raise,
    test_close_producer_quiesce_timeout,
    test_close_drains_and_shuts_down,
    test_start_worker_locked_guards,
    test_drain_worker_stale_inflight,
    test_registry_acquire_create_reuse_mismatch,
    test_registry_close_noop_and_mismatch,
    test_registry_release,
]


def main() -> int:
    results = []
    for test in TESTS:
        print(f"\n--- {test.__name__} ---")
        try:
            results.append(test())
        except Exception as exc:  # noqa: BLE001
            print(f"{FAIL} {test.__name__} raised: {exc!r}")
            results.append(False)
    passed = sum(1 for r in results if r)
    print(f"\n{passed}/{len(results)} test groups passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
