from __future__ import annotations

import atexit
import asyncio
import json
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
        try:
            queue.enqueue(_payload("d" * 16))
        except RuntimeError as exc:
            assert str(exc) == "lag incident spool is full"
        else:
            raise AssertionError("bounded spool accepted an excess incident")
    finally:
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
    for _ in range(50):
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


def main_test() -> None:
    asyncio.run(_blocked_loop_eventual_exactly_once())
    asyncio.run(_restart_and_unavailable_retry())
    asyncio.run(_corruption_fails_closed())
    test_redaction_bounds_and_dedup()
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
    print("PASS: durable lag incident queue")


if __name__ == "__main__":
    main_test()
