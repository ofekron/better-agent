from __future__ import annotations

import atexit
import asyncio
import json
import multiprocessing
import os
import shutil
import sys
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home

_TMP_HOME = _test_home.isolate("ba-test-lag-queue-")
atexit.register(lambda: shutil.rmtree(_TMP_HOME, ignore_errors=True))

import lag_incident_queue as queue
import main
import paths


def _multiprocess_overflow_enqueue(ref: str) -> None:
    import lag_incident_queue as child_queue
    child_queue._MAX_PENDING = 0
    child_queue.enqueue(_payload(ref))


def _crash_after_overflow_payload(ref: str) -> None:
    import lag_incident_queue as child_queue
    child_queue._MAX_PENDING = 0
    child_queue._write_overflow_ledger_locked = lambda *_args: os._exit(73)
    child_queue.enqueue(_payload(ref))


def _multiprocess_quota_enqueue(ref: str, results) -> None:
    import lag_incident_queue as child_queue
    try:
        results.put((ref, child_queue.enqueue(_payload(ref))))
    except child_queue.LagIncidentSpoolFull:
        results.put((ref, "full"))


def _crash_after_reserved_payload(ref: str) -> None:
    import lag_incident_queue as child_queue
    child_queue._MAX_TOTAL_ENTRIES = 0
    child_queue._BACKPRESSURE_RESERVE_ENTRIES = 1
    child_queue._write_overflow_ledger_locked = lambda *_args: os._exit(74)
    payload = _payload(ref)
    try:
        child_queue.enqueue(payload)
    except child_queue.LagIncidentSpoolFull:
        child_queue.enqueue_backpressure(payload)


def _reset_spool() -> None:
    shutil.rmtree(paths.ba_home() / "lag-incidents", ignore_errors=True)


def _payload(ref: str = "a" * 16, evidence: str = "evidence") -> bytes:
    return json.dumps(
        {
            "requirement_ref": f"bug:lag-watchdog:{ref}",
            "summary": "lag",
            "source": "lag_watchdog",
            "severity": "high",
            "evidence": evidence,
        },
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


async def _blocked_loop_eventual_exactly_once() -> None:
    _reset_spool()
    calls: list[bytes] = []

    async def dispatch(body: bytes) -> bool:
        calls.append(body)
        return True

    queue.start(dispatch)
    finished = threading.Event()

    def enqueue_during_stall() -> None:
        time.sleep(0.05)
        for _ in range(2):
            main._report_lag_watchdog_issue(
                label="blocking stack candidate",
                heartbeat_age=3.0,
                dump_path=paths.ba_home() / "logs" / "backend-faulthandler.log",
                evidence="event loop lag evidence heartbeat_age=3.0s",
                stack_names=["block", "block", "block"],
            )
        finished.set()

    worker = threading.Thread(target=enqueue_during_stall)
    worker.start()
    started = time.monotonic()
    time.sleep(0.25)
    assert finished.is_set()
    assert time.monotonic() - started < 0.5
    assert calls == [], "dispatcher ran while the event loop was blocked"
    await asyncio.sleep(0.1)
    assert len(calls) == 1
    assert queue.depth() == 0
    worker.join()
    await queue.stop()


async def _restart_and_unavailable_retry() -> None:
    _reset_spool()
    assert queue.enqueue(_payload("b" * 16))
    attempts = 0
    original_base = queue._RETRY_BASE_SECONDS
    original_jitter = queue._RETRY_JITTER_RATIO
    queue._RETRY_BASE_SECONDS = 0.02
    queue._RETRY_JITTER_RATIO = 0.0

    async def dispatch(_body: bytes) -> bool:
        nonlocal attempts
        attempts += 1
        return attempts >= 2

    try:
        queue.start(dispatch)
        for _ in range(50):
            if attempts >= 2 and queue.depth() == 0:
                break
            await asyncio.sleep(0.02)
        assert attempts == 2
        assert queue.depth() == 0
    finally:
        queue._RETRY_BASE_SECONDS = original_base
        queue._RETRY_JITTER_RATIO = original_jitter
        await queue.stop()


async def _corruption_fails_closed() -> None:
    _reset_spool()
    root = paths.ba_home() / "lag-incidents"
    root.mkdir(parents=True)
    corrupt = root / ("c" * 16 + ".json")
    corrupt.write_bytes(b'{"requirement_ref":"bug:lag-watchdog:cccccccccccccccc"')
    calls = 0

    async def dispatch(_body: bytes) -> bool:
        nonlocal calls
        calls += 1
        return True

    queue.start(dispatch)
    await asyncio.sleep(0.05)
    assert calls == 0
    assert corrupt.exists(), "corrupt evidence must not be silently destroyed"
    await queue.stop()


def test_redaction_bounds_and_dedup() -> None:
    _reset_spool()
    main._report_lag_watchdog_issue(
        label="blocking stack candidate",
        heartbeat_age=2.0,
        dump_path=paths.ba_home() / "logs" / "backend-faulthandler.log",
        evidence="Bearer private-value\naccess_token=private-value",
        stack_names=["Bearer private-value"],
    )
    files = list((paths.ba_home() / "lag-incidents").glob("*.json"))
    assert len(files) == 1
    raw = files[0].read_bytes()
    assert len(raw) <= main._LAG_REPORT_BODY_LIMIT_BYTES
    assert b"private-value" not in raw
    assert not queue.enqueue(raw)
    assert len(list((paths.ba_home() / "lag-incidents").glob("*.json"))) == 1

    original_max = queue._MAX_PENDING
    queue._MAX_PENDING = 1
    try:
        assert queue.enqueue(_payload("d" * 16))
        ledger = paths.ba_home() / "lag-incidents" / queue._OVERFLOW_LEDGER_NAME
        assert ledger.exists()
        assert len(queue._load_overflow_ledger_locked(ledger.parent)) == 1
        assert queue.depth() == 2
    finally:
        queue._MAX_PENDING = original_max


async def _saturated_spool_promotes_lossless_fifo() -> None:
    _reset_spool()
    original_max = queue._MAX_PENDING
    queue._MAX_PENDING = 1
    refs = ("1" * 16, "2" * 16, "3" * 16)
    for ref in refs:
        queue.enqueue(_payload(ref))
        time.sleep(0.002)
    received: list[str] = []

    async def dispatch(body: bytes) -> bool:
        received.append(json.loads(body)["requirement_ref"].rsplit(":", 1)[-1])
        return True

    try:
        queue.start(dispatch)
        for _ in range(200):
            if queue.depth() == 0:
                break
            await asyncio.sleep(0.01)
        await queue.stop()
        assert received == list(refs), received
        assert queue.depth() == 0
    finally:
        if queue._task is not None:
            await queue.stop()
        queue._MAX_PENDING = original_max


def test_spool_symlink_escape_is_rejected() -> None:
    _reset_spool()
    outside = paths.ba_home().parent / "lag-queue-outside"
    shutil.rmtree(outside, ignore_errors=True)
    outside.mkdir()
    (paths.ba_home() / "lag-incidents").symlink_to(outside, target_is_directory=True)
    try:
        try:
            queue.enqueue(_payload("e" * 16))
        except RuntimeError as exc:
            assert str(exc) == "lag incident spool must be a real directory"
        else:
            raise AssertionError("spool followed a symlink outside the state root")
        assert list(outside.iterdir()) == []
    finally:
        (paths.ba_home() / "lag-incidents").unlink(missing_ok=True)
        shutil.rmtree(outside, ignore_errors=True)


def test_inside_home_spool_symlink_is_rejected() -> None:
    _reset_spool()
    target = paths.ba_home() / "inside-home-spool-target"
    shutil.rmtree(target, ignore_errors=True)
    target.mkdir()
    link = paths.ba_home() / "lag-incidents"
    link.symlink_to(target, target_is_directory=True)
    try:
        try:
            queue.enqueue(_payload("a" * 16))
        except RuntimeError as exc:
            assert str(exc) == "lag incident spool must be a real directory"
        else:
            raise AssertionError("inside-home spool symlink was accepted")
        assert list(target.iterdir()) == []
    finally:
        link.unlink(missing_ok=True)
        shutil.rmtree(target, ignore_errors=True)


async def _replay_symlink_never_reads_or_deletes_outside() -> None:
    _reset_spool()
    root = paths.ba_home() / "lag-incidents"
    root.mkdir()
    outside = paths.ba_home().parent / "lag-incident-outside.json"
    outside.write_bytes(_payload("f" * 16, evidence="outside-marker"))
    link = root / ("f" * 16 + ".json")
    link.symlink_to(outside)
    calls = 0

    async def dispatch(_body: bytes) -> bool:
        nonlocal calls
        calls += 1
        return True

    try:
        queue.start(dispatch)
        await asyncio.sleep(0.05)
        assert calls == 0
        assert outside.read_bytes() == _payload("f" * 16, evidence="outside-marker")
        assert link.is_symlink()
    finally:
        await queue.stop()
        link.unlink(missing_ok=True)
        outside.unlink(missing_ok=True)


async def _identity_swap_before_ack_never_deletes_outside() -> None:
    _reset_spool()
    ref = "8" * 16
    queue.enqueue(_payload(ref))
    root = paths.ba_home() / "lag-incidents"
    entry = root / f"{ref}.json"
    outside = paths.ba_home().parent / "lag-incident-ack-outside.json"
    outside.write_bytes(b"outside-must-survive")
    dispatched = asyncio.Event()

    async def dispatch(_body: bytes) -> bool:
        entry.unlink()
        entry.symlink_to(outside)
        dispatched.set()
        return True

    try:
        queue.start(dispatch)
        await asyncio.wait_for(dispatched.wait(), timeout=1)
        await asyncio.sleep(0.02)
        await queue.stop()
        assert outside.read_bytes() == b"outside-must-survive"
        assert entry.is_symlink()
    finally:
        if queue._task is not None:
            await queue.stop()
        entry.unlink(missing_ok=True)
        outside.unlink(missing_ok=True)


async def _in_place_rewrite_during_dispatch_survives_ack() -> None:
    _reset_spool()
    ref = "b" * 16
    original = _payload(ref, evidence="original")
    rewritten = _payload(ref, evidence="rewritte")
    assert len(original) == len(rewritten)
    queue.enqueue(original)
    entry = paths.ba_home() / "lag-incidents" / f"{ref}.json"
    original_stat = entry.stat()
    dispatched = asyncio.Event()

    async def dispatch(_body: bytes) -> bool:
        entry.write_bytes(rewritten)
        os.utime(
            entry,
            ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
        )
        dispatched.set()
        return True

    queue.start(dispatch)
    await asyncio.wait_for(dispatched.wait(), timeout=1)
    await asyncio.sleep(0.02)
    await queue.stop()
    assert entry.exists()
    assert entry.read_bytes() == rewritten


async def _recursive_malformed_entry_is_skipped() -> None:
    _reset_spool()
    root = paths.ba_home() / "lag-incidents"
    root.mkdir()
    malformed = root / ("c" * 16 + ".json")
    malformed.write_bytes(b"[" * 2_000 + b"]" * 2_000)
    time.sleep(0.002)
    queue.enqueue(_payload("d" * 16))
    calls = 0
    original_loads = queue.json.loads

    def recursive_loads(raw, *args, **kwargs):
        if isinstance(raw, bytes) and raw.startswith(b"["):
            raise RecursionError("deterministic recursive payload")
        return original_loads(raw, *args, **kwargs)

    async def dispatch(_body: bytes) -> bool:
        nonlocal calls
        calls += 1
        return True

    queue.json.loads = recursive_loads
    try:
        queue.start(dispatch)
        for _ in range(50):
            if calls == 1 and queue.depth() == 1:
                break
            await asyncio.sleep(0.01)
        await queue.stop()
        assert calls == 1
        assert malformed.exists()
    finally:
        if queue._task is not None:
            await queue.stop()
        queue.json.loads = original_loads


async def _directory_fsync_covers_publish_and_ack() -> None:
    _reset_spool()
    directory_fsyncs = 0
    original_fsync = queue.os.fsync

    def tracked_fsync(fd: int) -> None:
        nonlocal directory_fsyncs
        import stat
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            directory_fsyncs += 1
        original_fsync(fd)

    queue.os.fsync = tracked_fsync
    try:
        queue.enqueue(_payload("1" * 16))

        async def dispatch(_body: bytes) -> bool:
            return True

        queue.start(dispatch)
        for _ in range(50):
            if queue.depth() == 0:
                break
            await asyncio.sleep(0.01)
        assert queue.depth() == 0
        assert directory_fsyncs >= 2
    finally:
        await queue.stop()
        queue.os.fsync = original_fsync


async def _ack_before_unlink_replays_idempotently_after_restart() -> None:
    _reset_spool()
    queue.enqueue(_payload("2" * 16))
    calls = 0
    acknowledged = asyncio.Event()
    original_acknowledge = queue._acknowledge

    async def dispatch(_body: bytes) -> bool:
        nonlocal calls
        calls += 1
        acknowledged.set()
        return True

    def crash_before_unlink(*_args) -> None:
        raise OSError("simulated crash boundary")

    queue._acknowledge = crash_before_unlink
    queue.start(dispatch)
    await asyncio.wait_for(acknowledged.wait(), timeout=1)
    await asyncio.sleep(0.02)
    await queue.stop()
    assert calls == 1 and queue.depth() == 1, (calls, queue.depth())

    queue._acknowledge = original_acknowledge
    queue.start(dispatch)
    queue.notify_destination_changed()
    for _ in range(200):
        if calls == 2 and queue.depth() == 0:
            break
        await asyncio.sleep(0.01)
    await queue.stop()
    assert calls == 2 and queue.depth() == 0


async def _transient_failure_opens_ordered_circuit() -> None:
    _reset_spool()
    for index, ref in enumerate(("3" * 16, "4" * 16, "5" * 16)):
        queue.enqueue(_payload(ref, evidence=str(index)))
        time.sleep(0.002)
    calls: list[str] = []
    original_base = queue._RETRY_BASE_SECONDS
    original_jitter = queue._RETRY_JITTER_RATIO
    queue._RETRY_BASE_SECONDS = 0.05
    queue._RETRY_JITTER_RATIO = 0.0

    async def dispatch(body: bytes) -> bool:
        calls.append(json.loads(body)["requirement_ref"])
        return False

    try:
        queue.start(dispatch)
        await asyncio.sleep(0.13)
        assert 1 <= len(calls) <= 3
        assert set(calls) == {"bug:lag-watchdog:" + "3" * 16}
        assert queue.depth() == 3
    finally:
        await queue.stop()
        queue._RETRY_BASE_SECONDS = original_base
        queue._RETRY_JITTER_RATIO = original_jitter


async def _shutdown_joins_inflight_dispatch() -> None:
    _reset_spool()
    queue.enqueue(_payload("6" * 16))
    time.sleep(0.002)
    queue.enqueue(_payload("9" * 16))
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def dispatch(_body: bytes) -> bool:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return True

    queue.start(dispatch)
    await asyncio.wait_for(entered.wait(), timeout=1)
    stopping = asyncio.create_task(queue.stop())
    await asyncio.sleep(0.02)
    assert not stopping.done()
    release.set()
    await asyncio.wait_for(stopping, timeout=1)
    assert calls == 1
    assert queue.depth() == 1


def test_non_finite_numbers_are_rejected() -> None:
    for value in (float("nan"), float("inf"), float("-inf")):
        payload = json.loads(_payload("7" * 16))
        payload["lag_seconds"] = value
        try:
            queue.enqueue(json.dumps(payload, separators=(",", ":")).encode())
        except ValueError as exc:
            assert str(exc) == "invalid lag_seconds"
        else:
            raise AssertionError("non-finite lag_seconds reached durable spool")


async def _portable_identity_fallback_roundtrip() -> None:
    _reset_spool()
    original = queue._DIRFD_SUPPORTED
    queue._DIRFD_SUPPORTED = False
    calls = 0

    async def dispatch(_body: bytes) -> bool:
        nonlocal calls
        calls += 1
        return True

    try:
        queue.enqueue(_payload("0" * 16))
        queue.start(dispatch)
        for _ in range(50):
            if queue.depth() == 0:
                break
            await asyncio.sleep(0.01)
        assert calls == 1 and queue.depth() == 0
    finally:
        await queue.stop()
        queue._DIRFD_SUPPORTED = original


async def _structured_retry_after_and_destination_wake() -> None:
    _reset_spool()
    queue.enqueue(_payload("9" * 16))
    attempts = 0
    original_base = queue._RETRY_BASE_SECONDS
    original_max = queue._RETRY_MAX_SECONDS
    queue._RETRY_BASE_SECONDS = 10.0
    queue._RETRY_MAX_SECONDS = 10.0

    async def dispatch(_body: bytes) -> queue.DispatchOutcome:
        nonlocal attempts
        attempts += 1
        return queue.DispatchOutcome(attempts > 1, retry_after=10.0)

    try:
        queue.start(dispatch)
        for _ in range(100):
            if attempts:
                break
            await asyncio.sleep(0.01)
        assert attempts == 1 and queue.depth() == 1
        queue.notify_destination_changed()
        for _ in range(100):
            if queue.depth() == 0:
                break
            await asyncio.sleep(0.01)
        assert attempts == 2 and queue.depth() == 0, (attempts, queue.depth())
    finally:
        await queue.stop()
        queue._RETRY_BASE_SECONDS = original_base
        queue._RETRY_MAX_SECONDS = original_max


async def _nonretryable_incident_is_durably_parked() -> None:
    _reset_spool()
    queue.enqueue(_payload("a" * 16))
    available = False

    attempts = 0

    async def dispatch(_body: bytes) -> queue.DispatchOutcome:
        nonlocal attempts
        attempts += 1
        return queue.DispatchOutcome(available, retryable=False)

    queue.start(dispatch)
    try:
        for _ in range(100):
            if queue.parked_depth() == 1:
                break
            await asyncio.sleep(0.01)
        assert queue.depth() == 1
        assert queue.parked_depth() == 1
        parked = list((paths.ba_home() / "lag-incidents").glob("*.parked"))
        assert len(parked) == 1
        assert json.loads(parked[0].read_bytes())["requirement_ref"].endswith("a" * 16)
        assert queue.enqueue(_payload("b" * 16))
        await asyncio.sleep(0.05)
        assert attempts == 1, "same-generation arrivals must park without probing"
        assert queue.parked_depth() == 2
        available = True
        queue.notify_destination_changed()
        for _ in range(100):
            if queue.depth() == 0:
                break
            await asyncio.sleep(0.01)
        assert queue.depth() == 0
        assert attempts == 3
    finally:
        await queue.stop()


async def _destination_unavailable_parks_generation_without_probe_storm() -> None:
    _reset_spool()
    original_max = queue._MAX_PENDING
    queue._MAX_PENDING = 2
    refs = ("1" * 16, "2" * 16, "3" * 16)
    try:
        for ref in refs:
            assert queue.enqueue(_payload(ref))
        attempts = 0

        async def dispatch(_body: bytes) -> queue.DispatchOutcome:
            nonlocal attempts
            attempts += 1
            return queue.DispatchOutcome(
                False,
                retryable=False,
                destination_unavailable=True,
            )

        queue.start(dispatch)
        try:
            for _ in range(100):
                pending = list((paths.ba_home() / "lag-incidents").glob("*.json"))
                if attempts == 1 and not pending and queue.parked_depth() == len(refs):
                    break
                await asyncio.sleep(0.01)
            pending = list((paths.ba_home() / "lag-incidents").glob("*.json"))
            overflow = list((paths.ba_home() / "lag-incidents").glob("*.overflow"))
            assert attempts == 1
            assert pending == []
            assert overflow == []
            assert queue.parked_depth() == len(refs)
        finally:
            await queue.stop()
    finally:
        queue._MAX_PENDING = original_max


def test_spool_quota_backpressures_without_silent_loss() -> None:
    _reset_spool()
    original_pending = queue._MAX_PENDING
    original_entries = queue._MAX_TOTAL_ENTRIES
    original_bytes = queue._MAX_TOTAL_BYTES
    queue._MAX_PENDING = 1
    queue._MAX_TOTAL_ENTRIES = 2
    queue._MAX_TOTAL_BYTES = 1_000_000
    try:
        assert queue.enqueue(_payload("1" * 16))
        assert queue.enqueue(_payload("2" * 16))
        try:
            queue.enqueue(_payload("3" * 16))
        except queue.LagIncidentSpoolFull as exc:
            assert "count quota" in str(exc)
        else:
            raise AssertionError("count quota did not apply backpressure")
        assert queue._reconcile_depth_projection() == 2

        queue._MAX_TOTAL_ENTRIES = 10
        queue._MAX_TOTAL_BYTES = sum(
            path.stat().st_size
            for path in (paths.ba_home() / "lag-incidents").glob("*.json")
        ) + (paths.ba_home() / "lag-incidents" / queue._OVERFLOW_LEDGER_NAME).stat().st_size
        try:
            queue.enqueue(_payload("4" * 16))
        except queue.LagIncidentSpoolFull as exc:
            assert "byte quota" in str(exc)
        else:
            raise AssertionError("byte quota did not apply backpressure")
        assert queue._reconcile_depth_projection() == 2
    finally:
        queue._MAX_PENDING = original_pending
        queue._MAX_TOTAL_ENTRIES = original_entries
        queue._MAX_TOTAL_BYTES = original_bytes


def test_overflow_ledger_is_multiprocess_lossless() -> None:
    _reset_spool()
    original_pending = queue._MAX_PENDING
    queue._MAX_PENDING = 0
    refs = [f"{index:016x}" for index in range(1, 9)]
    try:
        context = multiprocessing.get_context("fork")
        processes = [context.Process(target=_multiprocess_overflow_enqueue, args=(ref,)) for ref in refs]
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=5)
            assert process.exitcode == 0
        root = paths.ba_home() / "lag-incidents"
        with queue._depth_process_lock(root):
            entries = queue._load_overflow_ledger_locked(root)
        assert {entry["digest"] for entry in entries} == set(refs)
        assert all(set(entry) == {"digest", "enqueued_ns", "name", "size"} for entry in entries)
        assert len(list(root.glob("*.overflow"))) == len(refs)
        assert queue._reconcile_depth_projection() == len(refs)
    finally:
        queue._MAX_PENDING = original_pending


async def _overflow_publish_crash_replays_after_restart() -> None:
    _reset_spool()
    original_pending = queue._MAX_PENDING
    queue._MAX_PENDING = 0
    queue.enqueue(_payload("d" * 16))
    assert queue._reconcile_depth_projection() == 1
    queue._MAX_PENDING = 1
    received: list[str] = []

    async def dispatch(body: bytes) -> bool:
        received.append(json.loads(body)["requirement_ref"])
        return True

    try:
        queue.start(dispatch)
        for _ in range(100):
            if queue.depth() == 0:
                break
            await asyncio.sleep(0.01)
        assert received == ["bug:lag-watchdog:" + "d" * 16]
        assert queue.depth() == 0
    finally:
        await queue.stop()
        queue._MAX_PENDING = original_pending


def test_overflow_crash_cutpoint_is_reconciled() -> None:
    _reset_spool()
    context = multiprocessing.get_context("fork")
    process = context.Process(target=_crash_after_overflow_payload, args=("e" * 16,))
    process.start()
    process.join(timeout=5)
    assert process.exitcode == 73
    assert queue._reconcile_depth_projection() == 1
    root = paths.ba_home() / "lag-incidents"
    with queue._depth_process_lock(root):
        entries = queue._load_overflow_ledger_locked(root)
    assert [entry["digest"] for entry in entries] == ["e" * 16]


def test_retry_metadata_survives_wall_clock_jumps() -> None:
    _reset_spool()
    original_time = queue.time.time
    now = 10_000.0
    queue.time.time = lambda: now
    try:
        queue._save_retry_state(3, now + 100.0)
        backward = now - 10_000.0
        queue.time.time = lambda: backward
        failures, deadline = queue._load_retry_state()
        assert failures == 3
        assert 99.0 <= deadline - backward <= 100.0
        forward = now + 100_000.0
        queue.time.time = lambda: forward
        failures, deadline = queue._load_retry_state()
        assert failures == 3
        assert deadline == forward
    finally:
        queue.time.time = original_time


def test_synchronize_destination_repairs_stale_metadata_version() -> None:
    _reset_spool()
    root = paths.ba_home() / "lag-incidents"
    root.mkdir(parents=True, exist_ok=True)
    (root / queue._DESTINATION_META_NAME).write_text(
        '{"version":1,"generation":7,"blocked_generation":7,"identity":"stale"}',
        encoding="utf-8",
    )
    queue._destination_generation = 0

    assert queue.synchronize_destination("current")
    assert queue._destination_state() == (1, None)
    assert not queue.synchronize_destination("current")
    assert queue._destination_state() == (1, None)


def test_active_quota_is_atomic_across_processes() -> None:
    _reset_spool()
    original_entries = queue._MAX_TOTAL_ENTRIES
    original_pending = queue._MAX_PENDING
    queue._MAX_TOTAL_ENTRIES = 4
    queue._MAX_PENDING = 4
    context = multiprocessing.get_context("fork")
    results = context.Queue()
    refs = [f"{index:016x}" for index in range(20, 28)]
    processes = [context.Process(target=_multiprocess_quota_enqueue, args=(ref, results)) for ref in refs]
    try:
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=5)
            assert process.exitcode == 0
        outcomes = [results.get(timeout=1)[1] for _ in refs]
        assert outcomes.count(True) == 4
        assert outcomes.count("full") == 4
        assert queue._reconcile_depth_projection() == 4
    finally:
        queue._MAX_TOTAL_ENTRIES = original_entries
        queue._MAX_PENDING = original_pending


def test_enqueue_never_rescans_spool_with_seeded_projection() -> None:
    # The lag watchdog enqueues an incident on every lag event. An enqueue
    # that stats the spool corpus makes each lag event more expensive as
    # incidents accumulate — a self-amplifying spiral (observed at 1,912
    # parked files: 58.7s per enqueue). With the inventory projection seeded,
    # enqueue must be O(1): zero directory scans.
    _reset_spool()
    root = paths.ba_home() / "lag-incidents"
    root.mkdir(parents=True)
    for index in range(800):
        ref = f"{index:016x}"
        (root / f"{ref}.json").write_bytes(_payload(ref))
    queue._reconcile_depth_projection()
    inventory_calls = 0
    pending_calls = 0
    original_inventory = queue._active_inventory
    original_pending = queue._pending_files

    def tracked_inventory(path):
        nonlocal inventory_calls
        inventory_calls += 1
        return original_inventory(path)

    def tracked_pending(*args, **kwargs):
        nonlocal pending_calls
        pending_calls += 1
        return original_pending(*args, **kwargs)

    queue._active_inventory = tracked_inventory
    queue._pending_files = tracked_pending
    try:
        assert queue.enqueue(_payload("f" * 16))
        assert inventory_calls == 0, "enqueue must trust the inventory projection"
        assert pending_calls == 0, "enqueue must not rescan the spool corpus"
        assert queue.depth() == 801
    finally:
        queue._active_inventory = original_inventory
        queue._pending_files = original_pending
    assert queue._reconcile_depth_projection() == 801


def test_parked_backlog_is_bounded_under_blocked_destination() -> None:
    # A permanently unavailable destination (e.g. the Assistant extension
    # disabled) parks every incident. Without a cap the backlog grows without
    # bound and every startup reactivation pays for it.
    _reset_spool()
    root = paths.ba_home() / "lag-incidents"
    root.mkdir(parents=True)
    with queue._depth_process_lock(root):
        queue._write_destination_meta_locked(root, 7, 7)
    original_cap = queue._MAX_PARKED
    original_target = queue._PARKED_PRUNE_TARGET
    queue._MAX_PARKED = 8
    queue._PARKED_PRUNE_TARGET = 6
    try:
        refs = [f"{index:016x}" for index in range(40, 60)]
        for ref in refs:
            assert queue.enqueue(_payload(ref))
            time.sleep(0.002)
        parked = {path.name[: -len(".parked")] for path in root.glob("*.parked")}
        assert len(parked) <= queue._MAX_PARKED, f"parked backlog unbounded: {len(parked)}"
        assert refs[-1] in parked, "newest incident must survive pruning"
        assert refs[0] not in parked, "oldest incident must be pruned first"
        assert queue._reconcile_depth_projection() == len(parked)
    finally:
        queue._MAX_PARKED = original_cap
        queue._PARKED_PRUNE_TARGET = original_target


def test_reactivate_parked_is_single_pass() -> None:
    # Reactivation used to rescan pending and reload the overflow ledger once
    # per parked entry — O(n^2) at every backend startup and destination
    # change. It must do a constant number of spool scans regardless of N.
    _reset_spool()
    root = paths.ba_home() / "lag-incidents"
    root.mkdir(parents=True)
    n = 40
    for index in range(n):
        ref = f"{index + 0x100:016x}"
        (root / f"{ref}.parked").write_bytes(_payload(ref))
        time.sleep(0.001)
    original_max = queue._MAX_PENDING
    queue._MAX_PENDING = 4
    pending_calls = 0
    original_pending = queue._pending_files

    def tracked_pending(*args, **kwargs):
        nonlocal pending_calls
        pending_calls += 1
        return original_pending(*args, **kwargs)

    queue._pending_files = tracked_pending
    try:
        moved = queue._reactivate_parked()
        assert moved == n
        assert pending_calls <= 3, (
            f"reactivation must not rescan per parked entry: {pending_calls} scans for {n} files"
        )
        assert len(list(root.glob("*.json"))) == queue._MAX_PENDING
        assert len(list(root.glob("*.parked"))) == 0
        assert len(list(root.glob("*.overflow"))) == n - queue._MAX_PENDING
        assert queue.depth() == n
    finally:
        queue._pending_files = original_pending
        queue._MAX_PENDING = original_max


async def _blocked_generation_survives_restart_without_probe() -> None:
    _reset_spool()
    queue.enqueue(_payload("f" * 16))
    root = paths.ba_home() / "lag-incidents"
    with queue._depth_process_lock(root):
        queue._write_destination_meta_locked(root, 7, 7)
    attempts = 0

    async def dispatch(_body: bytes) -> bool:
        nonlocal attempts
        attempts += 1
        return True

    queue._destination_generation = 0
    queue.start(dispatch)
    try:
        for _ in range(100):
            if queue.parked_depth() == 1:
                break
            await asyncio.sleep(0.01)
        assert attempts == 0
        assert queue.parked_depth() == 1
        queue.notify_destination_changed()
        for _ in range(100):
            if queue.depth() == 0:
                break
            await asyncio.sleep(0.01)
        assert attempts == 1
        assert queue.depth() == 0
    finally:
        await queue.stop()


async def _blocked_generation_parks_overflow_without_probe() -> None:
    _reset_spool()
    original_max = queue._MAX_PENDING
    queue._MAX_PENDING = 1
    try:
        queue.enqueue(_payload("a" * 16))
        queue.enqueue(_payload("b" * 16))
        root = paths.ba_home() / "lag-incidents"
        assert len(list(root.glob("*.overflow"))) == 1
        with queue._depth_process_lock(root):
            queue._write_destination_meta_locked(root, 7, 7)
        attempts = 0

        async def dispatch(_body: bytes) -> bool:
            nonlocal attempts
            attempts += 1
            return True

        queue._destination_generation = 0
        queue.start(dispatch)
        try:
            for _ in range(100):
                pending = list(root.glob("*.json"))
                overflow = list(root.glob("*.overflow"))
                if not pending and not overflow and queue.parked_depth() == 2:
                    break
                await asyncio.sleep(0.01)
            assert attempts == 0
            assert list(root.glob("*.json")) == []
            assert list(root.glob("*.overflow")) == []
            assert queue.parked_depth() == 2
        finally:
            await queue.stop()
    finally:
        queue._MAX_PENDING = original_max


def test_corrupt_reference_ledger_is_quarantined_and_rebuilt() -> None:
    _reset_spool()
    root = paths.ba_home() / "lag-incidents"
    root.mkdir()
    (root / queue._OVERFLOW_LEDGER_NAME).write_bytes(b"not-json")
    (root / ("a" * 16 + queue._OVERFLOW_SUFFIX)).write_bytes(_payload("a" * 16))
    assert queue._reconcile_depth_projection() == 1
    assert len(list(root.glob(queue._OVERFLOW_LEDGER_NAME + ".corrupt.*"))) == 1
    with queue._depth_process_lock(root):
        entries = queue._load_overflow_ledger_locked(root)
    assert [entry["digest"] for entry in entries] == ["a" * 16]


async def _reserved_quota_crash_restarts_and_drains() -> None:
    _reset_spool()
    context = multiprocessing.get_context("fork")
    process = context.Process(target=_crash_after_reserved_payload, args=("9" * 16,))
    process.start()
    process.join(timeout=5)
    assert process.exitcode == 74
    assert queue._reconcile_depth_projection() == 1
    received: list[str] = []

    async def dispatch(body: bytes) -> bool:
        received.append(json.loads(body)["requirement_ref"])
        return True

    queue.start(dispatch)
    try:
        for _ in range(100):
            if queue.depth() == 0:
                break
            await asyncio.sleep(0.01)
        assert received == ["bug:lag-watchdog:" + "9" * 16]
        assert queue.depth() == 0
    finally:
        await queue.stop()


def test_parked_depth_does_not_stat_per_file() -> None:
    # perf.flush() reads parked_depth() on the event loop every ROLLUP_SECS.
    # It must NOT issue per-file os.stat — at 1669 parked files that blocked
    # the loop ~1.6s (lag-watchdog self-amplification). A count needs names only.
    _reset_spool()
    root = paths.ba_home() / "lag-incidents"
    root.mkdir()
    n = 400
    for i in range(n):
        (root / f"{i:08x}.parked").write_bytes(b"{}")

    real_stat = os.stat
    counts = {"n": 0}

    def counting_stat(*args, **kwargs):
        counts["n"] += 1
        return real_stat(*args, **kwargs)

    queue.os.stat = counting_stat
    try:
        counts["n"] = 0
        depth = queue.parked_depth()
        stat_calls = counts["n"]
    finally:
        queue.os.stat = real_stat

    assert depth == n, f"parked_depth miscounted: expected {n}, got {depth}"
    assert stat_calls <= 16, (
        f"parked_depth must not stat per file: {stat_calls} os.stat calls for {n} files"
    )
    _reset_spool()


def main_test() -> None:
    asyncio.run(_blocked_loop_eventual_exactly_once())
    asyncio.run(_restart_and_unavailable_retry())
    asyncio.run(_corruption_fails_closed())
    test_redaction_bounds_and_dedup()
    asyncio.run(_saturated_spool_promotes_lossless_fifo())
    test_spool_symlink_escape_is_rejected()
    test_inside_home_spool_symlink_is_rejected()
    asyncio.run(_replay_symlink_never_reads_or_deletes_outside())
    asyncio.run(_identity_swap_before_ack_never_deletes_outside())
    asyncio.run(_in_place_rewrite_during_dispatch_survives_ack())
    asyncio.run(_recursive_malformed_entry_is_skipped())
    asyncio.run(_directory_fsync_covers_publish_and_ack())
    asyncio.run(_ack_before_unlink_replays_idempotently_after_restart())
    asyncio.run(_transient_failure_opens_ordered_circuit())
    asyncio.run(_shutdown_joins_inflight_dispatch())
    test_non_finite_numbers_are_rejected()
    asyncio.run(_portable_identity_fallback_roundtrip())
    asyncio.run(_structured_retry_after_and_destination_wake())
    asyncio.run(_nonretryable_incident_is_durably_parked())
    asyncio.run(_destination_unavailable_parks_generation_without_probe_storm())
    test_spool_quota_backpressures_without_silent_loss()
    test_overflow_ledger_is_multiprocess_lossless()
    asyncio.run(_overflow_publish_crash_replays_after_restart())
    test_overflow_crash_cutpoint_is_reconciled()
    test_retry_metadata_survives_wall_clock_jumps()
    test_synchronize_destination_repairs_stale_metadata_version()
    test_active_quota_is_atomic_across_processes()
    test_enqueue_never_rescans_spool_with_seeded_projection()
    test_parked_backlog_is_bounded_under_blocked_destination()
    test_reactivate_parked_is_single_pass()
    asyncio.run(_blocked_generation_survives_restart_without_probe())
    asyncio.run(_blocked_generation_parks_overflow_without_probe())
    test_corrupt_reference_ledger_is_quarantined_and_rebuilt()
    test_parked_depth_does_not_stat_per_file()
    asyncio.run(_reserved_quota_crash_restarts_and_drains())
    print("PASS: durable lag incident queue")


if __name__ == "__main__":
    main_test()
