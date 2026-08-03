from __future__ import annotations

import os
import pickle
import sys
import tempfile
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import codex_execution_common as cec  # noqa: E402
from codex_execution_common import (  # noqa: E402
    AttestOnceCache,
    ExecutionContractError,
    _attest_result_ok,
    _identity_executor,
    binary_open_flags,
    cached_sha256_fd,
    canonical_json,
    parallel_map,
    parallel_map_processes,
    record_step_since,
    required_integer,
    required_string,
    sha256_and_first_line_fd,
    store_cached_sha256,
    symlink_chain,
    timed_contract_step,
)


@pytest.fixture(autouse=True)
def _clear_hash_cache():
    cec._hash_cache.clear()
    yield
    cec._hash_cache.clear()


# ---------------------------------------------------------------------------
# _identity_executor double-checked locking (inner not-None branch)
# ---------------------------------------------------------------------------


def test_identity_executor_inner_branch_under_concurrency() -> None:
    # The double-checked lock has an inner `if _identity_pool is None` whose
    # False branch (62->67) only fires when a caller observes None at the
    # unlocked outer check, then finds the pool already initialized once it
    # acquires the lock. We force that exact ordering deterministically:
    # the main thread holds the lock, a racer passes the outer None-check
    # and blocks on the lock, the main thread then initializes the pool and
    # releases so the racer enters the locked region observing a set pool.
    from concurrent.futures import ThreadPoolExecutor

    original_pool = cec._identity_pool
    original_lock = cec._identity_pool_lock
    preset_pool = ThreadPoolExecutor(max_workers=1)

    class _SignalingLock:
        def __init__(self) -> None:
            self._real = threading.Lock()
            self.racer_at_lock = threading.Event()

        def __enter__(self):
            # Reached only after the caller's outer None-check passed.
            self.racer_at_lock.set()
            self._real.acquire()
            return self

        def __exit__(self, *_exc) -> None:
            self._real.release()

        def acquire_raw(self) -> None:
            self._real.acquire()

        def release_raw(self) -> None:
            self._real.release()

    siglock = _SignalingLock()
    cec._identity_pool = None
    cec._identity_pool_lock = siglock
    siglock.acquire_raw()  # main holds the lock before the racer starts

    racer_result: list = []

    def _racer():
        racer_result.append(_identity_executor())

    racer = threading.Thread(target=_racer)
    racer.start()
    assert siglock.racer_at_lock.wait(5.0), "racer never reached the lock"
    # Racer passed the outer None-check and is blocked; initialize the pool
    # underneath the lock, then let the racer in.
    cec._identity_pool = preset_pool
    siglock.release_raw()
    racer.join(5.0)
    assert not racer.is_alive()

    try:
        assert racer_result == [preset_pool]  # inner check was False -> 67
    finally:
        cec._identity_pool = original_pool
        cec._identity_pool_lock = original_lock
        if original_pool is not preset_pool:
            preset_pool.shutdown(wait=True)


# ---------------------------------------------------------------------------
# parallel_map
# ---------------------------------------------------------------------------


def test_parallel_map_preserves_order_and_handles_single() -> None:
    assert parallel_map(lambda x: x * 2, []) == []
    # len <= 1 fast path.
    assert parallel_map(lambda x: x + 1, [10]) == [11]
    assert parallel_map(str, range(5)) == ["0", "1", "2", "3", "4"]


def test_parallel_map_propagates_first_failure_in_order() -> None:
    def _fn(x):
        if x == 2:
            raise ValueError("boom")
        return x

    with pytest.raises(ValueError, match="boom"):
        parallel_map(_fn, range(5))


# ---------------------------------------------------------------------------
# parallel_map_processes
# ---------------------------------------------------------------------------


def test_parallel_map_processes_small_input_uses_thread_pool() -> None:
    # < _PROCESS_MAP_MIN_ITEMS -> parallel_map fallback (no subprocess spawn).
    assert parallel_map_processes(str, range(10)) == [
        str(i) for i in range(10)
    ]


def test_parallel_map_processes_frozen_runtime_falls_back() -> None:
    # `sys.frozen` truthy with a large input still takes the fallback branch.
    original = getattr(sys, "frozen", False)
    sys.frozen = True
    try:
        result = parallel_map_processes(str, range(70))
    finally:
        if original is False:
            del sys.frozen
        else:
            sys.frozen = original
    assert result == [str(i) for i in range(70)]


def test_parallel_map_processes_happy_path_preserves_order() -> None:
    # Real subprocess workers; `str` is a picklable builtin.
    result = parallel_map_processes(str, range(70))
    assert result == [str(i) for i in range(70)]


def test_parallel_map_processes_propagates_unpicklable_worker_fn() -> None:
    # A test-module-local function pickles in the parent but cannot be
    # imported inside the worker (`-c` process has only backend_root on
    # sys.path), so the worker reports (False, exc) and we re-raise it.
    with pytest.raises(Exception):
        parallel_map_processes(_local_identity, range(70))


def _local_identity(x):  # module-level, deliberately not importable in worker
    return x


class _FakeReader:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


class _FakePipe:
    def write(self, _data) -> None:
        return None

    def close(self) -> None:
        return None


class _FakePopen:
    def __init__(self, *, stdout: bytes, returncode: int, running: bool) -> None:
        self.stdin = _FakePipe()
        self.stdout = _FakeReader(stdout)
        self._returncode = returncode
        self._running = running
        self.killed = False

    def wait(self) -> int:
        return self._returncode

    def poll(self):
        return None if self._running else self._returncode

    def kill(self) -> None:
        self.killed = True
        self._running = False


class _PopenFactory:
    """Returns a fixed fake process, optionally raising OSError after N calls."""

    def __init__(self, fake: _FakePopen, fail_on_call: int | None = None) -> None:
        self.fake = fake
        self.fail_on_call = fail_on_call
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        if self.fail_on_call is not None and self.calls >= self.fail_on_call:
            raise OSError("injected spawn failure")
        return self.fake


def test_parallel_map_processes_rejects_nonzero_worker_exit() -> None:
    fake = _FakePopen(stdout=b"", returncode=1, running=False)
    factory = _PopenFactory(fake)
    original = cec.subprocess.Popen
    cec.subprocess.Popen = factory  # type: ignore[assignment]
    try:
        with pytest.raises(ExecutionContractError, match="worker failed"):
            parallel_map_processes(str, range(70))
    finally:
        cec.subprocess.Popen = original  # type: ignore[assignment]


def test_parallel_map_processes_rejects_invalid_worker_output() -> None:
    fake = _FakePopen(stdout=b"not-valid-pickle", returncode=0, running=False)
    factory = _PopenFactory(fake)
    original = cec.subprocess.Popen
    cec.subprocess.Popen = factory  # type: ignore[assignment]
    try:
        with pytest.raises(ExecutionContractError, match="invalid data"):
            parallel_map_processes(str, range(70))
    finally:
        cec.subprocess.Popen = original  # type: ignore[assignment]


def test_parallel_map_processes_wraps_spawn_oserror_and_kills_running() -> None:
    # First spawn succeeds (a still-running fake), the second raises OSError;
    # the OSError is wrapped and the running worker is killed in the finally.
    fake = _FakePopen(stdout=b"", returncode=0, running=True)
    factory = _PopenFactory(fake, fail_on_call=2)
    original = cec.subprocess.Popen
    cec.subprocess.Popen = factory  # type: ignore[assignment]
    try:
        with pytest.raises(ExecutionContractError, match="worker failed"):
            parallel_map_processes(str, range(70))
        # The first (running) worker was killed during cleanup.
        assert fake.killed is True
    finally:
        cec.subprocess.Popen = original  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# record_step_since / timed_contract_step
# ---------------------------------------------------------------------------


def test_record_step_since_swallows_missing_perf() -> None:
    # `import perf` inside the function fails -> except branch must swallow.
    held = sys.modules.pop("perf", None)
    # Force `import perf` to raise ImportError.
    sys.modules["perf"] = None  # type: ignore[assignment]
    try:
        record_step_since("x", 0.0)  # must not raise
    finally:
        del sys.modules["perf"]
        if held is not None:
            sys.modules["perf"] = held


def test_timed_contract_step_records_on_success_and_failure() -> None:
    # Happy path: perf is importable in the backend, so record runs.
    with timed_contract_step("step-ok"):
        pass

    # The body's own exception must propagate; the finally still records.
    with pytest.raises(RuntimeError, match="body"):
        with timed_contract_step("step-fail"):
            raise RuntimeError("body")


# ---------------------------------------------------------------------------
# hash cache LRU eviction
# ---------------------------------------------------------------------------


def test_cached_sha256_fd_evicts_oldest_past_limit() -> None:
    # Line 296: the cache evicts the oldest entry once over the size limit.
    cec._hash_cache.clear()
    for i in range(cec._HASH_CACHE_LIMIT):
        key = (f"/p/{i}", (i,))
        cec._hash_cache[key] = "deadbeef"
    assert len(cec._hash_cache) == cec._HASH_CACHE_LIMIT
    oldest_key = next(iter(cec._hash_cache))

    store_cached_sha256(("/new", (0,)), "cafe")
    assert len(cec._hash_cache) == cec._HASH_CACHE_LIMIT
    assert oldest_key not in cec._hash_cache  # evicted
    assert cec._hash_cache[("/new", (0,))] == "cafe"


# ---------------------------------------------------------------------------
# _attest_result_ok bare-bool branch
# ---------------------------------------------------------------------------


def test_attest_result_ok_handles_bare_bool_and_tuple() -> None:
    assert _attest_result_ok(True) is True
    assert _attest_result_ok(False) is False            # bare-bool branch
    assert _attest_result_ok((True, "ok")) is True
    assert _attest_result_ok((False, "no")) is False


def test_attest_once_cache_caches_after_success_only() -> None:
    cache = AttestOnceCache()
    calls = {"full": 0, "meta": 0}

    def full():
        calls["full"] += 1
        return (True, "ok")

    def meta():
        calls["meta"] += 1
        return (True, "ok")

    assert cache.resolve(full, meta) == (True, "ok")
    assert cache.resolve(full, meta) == (True, "ok")
    assert calls == {"full": 1, "meta": 1}  # second call used metadata path

    # A failing full_check is not cached -> retried next time.
    cache2 = AttestOnceCache()
    attempts = {"n": 0}

    def failing_full():
        attempts["n"] += 1
        return (False, "no")

    assert cache2.resolve(failing_full, meta) == (False, "no")
    assert cache2.resolve(failing_full, meta) == (False, "no")
    assert attempts["n"] == 2  # full_check ran both times


# ---------------------------------------------------------------------------
# sha256_and_first_line_fd branches
# ---------------------------------------------------------------------------


def _open_ro(path: Path) -> int:
    return os.open(path, binary_open_flags(os.O_RDONLY))


def test_sha256_and_first_line_fd_short_line() -> None:
    # Newline within the first 4096 bytes -> line_complete on first hit. The
    # file is > 1 MiB so a second read chunk arrives after line_complete is
    # already set, exercising the short-circuit branch back to the loop top.
    import hashlib

    with tempfile.TemporaryDirectory() as raw:
        p = Path(raw) / "f"
        p.write_bytes(b"#!/bin/sh\n" + b"x" * (1024 * 1024 + 100))
        fd = _open_ro(p)
        try:
            digest, first_line = sha256_and_first_line_fd(fd)
        finally:
            os.close(fd)
        assert first_line == b"#!/bin/sh\n"
        assert digest == hashlib.sha256(p.read_bytes()).hexdigest()


def test_sha256_and_first_line_fd_no_newline_under_limit() -> None:
    # No newline anywhere -> first_line accumulates to EOF (the extend branch).
    with tempfile.TemporaryDirectory() as raw:
        p = Path(raw) / "f"
        p.write_bytes(b"abcdef")
        fd = _open_ro(p)
        try:
            digest, first_line = sha256_and_first_line_fd(fd)
        finally:
            os.close(fd)
    assert first_line == b"abcdef"
    assert len(first_line) < 4096


def test_sha256_and_first_line_fd_long_line_hits_limit() -> None:
    # No newline and longer than 4096 -> first_line capped at exactly 4096.
    with tempfile.TemporaryDirectory() as raw:
        p = Path(raw) / "f"
        p.write_bytes(b"A" * 5000)
        fd = _open_ro(p)
        try:
            digest, first_line = sha256_and_first_line_fd(fd)
        finally:
            os.close(fd)
    assert len(first_line) == 4096


# ---------------------------------------------------------------------------
# required_string / required_integer defensive raises
# ---------------------------------------------------------------------------


def test_required_string_rejects_non_string() -> None:
    with pytest.raises(ExecutionContractError, match="requested must be a string"):
        required_string({"requested": 5}, "requested")


def test_required_integer_rejects_non_integer() -> None:
    with pytest.raises(ExecutionContractError, match="size must be an integer"):
        required_integer({"size": "big"}, "size")


# ---------------------------------------------------------------------------
# symlink_chain: missing path, cycle, too-deep
# ---------------------------------------------------------------------------


def test_symlink_chain_missing_path_returns_empty() -> None:
    # os.lstat on a missing path raises OSError -> the swallow-and-return-()
    # fast path.
    assert symlink_chain(Path("/nonexistent-path-for-cec-test")) == ()


def test_symlink_chain_regular_file_fast_path() -> None:
    # A non-symlink (regular file) takes the one-lstat fast path -> empty chain.
    with tempfile.TemporaryDirectory() as raw:
        p = Path(raw) / "f"
        p.write_bytes(b"x")
        assert symlink_chain(p) == ()


def test_symlink_chain_detects_cycle() -> None:
    with tempfile.TemporaryDirectory() as raw:
        a = Path(raw) / "a"
        b = Path(raw) / "b"
        a.symlink_to(b)
        b.symlink_to(a)
        with pytest.raises(ExecutionContractError, match="cycle"):
            symlink_chain(a)


def test_symlink_chain_rejects_chain_too_deep() -> None:
    with tempfile.TemporaryDirectory() as raw:
        base = Path(raw)
        # Build a chain longer than the 64-hop limit, every hop a symlink, so
        # the walker never reaches a non-symlink and exhausts the loop.
        links = [base / f"l{i}" for i in range(70)]
        for i in range(len(links) - 1):
            links[i].symlink_to(links[i + 1])
        links[-1].symlink_to(base / "dead-end")
        with pytest.raises(ExecutionContractError, match="too deep"):
            symlink_chain(links[0])


def test_symlink_chain_follows_to_real_target() -> None:
    # Happy path: a symlink chain resolving to a regular file records hops.
    with tempfile.TemporaryDirectory() as raw:
        target = Path(raw) / "real"
        target.write_bytes(b"x")
        link = Path(raw) / "link"
        link.symlink_to(target)
        chain = symlink_chain(link)
        assert len(chain) == 1
        assert chain[0][1] == str(target)


# ---------------------------------------------------------------------------
# read_first_line_fd / stable_stat_identity / required_* happy paths
# ---------------------------------------------------------------------------


def test_read_first_line_fd_stops_at_newline_and_eof() -> None:
    with tempfile.TemporaryDirectory() as raw:
        nl = Path(raw) / "nl"
        nl.write_bytes(b"first\nrest")
        fd = _open_ro(nl)
        try:
            assert cec.read_first_line_fd(fd) == b"first\n"
        finally:
            os.close(fd)

        no_nl = Path(raw) / "no_nl"
        no_nl.write_bytes(b"only")
        fd = _open_ro(no_nl)
        try:
            assert cec.read_first_line_fd(fd) == b"only"
        finally:
            os.close(fd)

        # No newline, longer than the limit -> first_line caps at the limit
        # and the while loop exits via its condition (not via break).
        long_no_nl = Path(raw) / "long"
        long_no_nl.write_bytes(b"A" * 6000)
        fd = _open_ro(long_no_nl)
        try:
            assert len(cec.read_first_line_fd(fd)) == 4096
        finally:
            os.close(fd)


def test_stable_stat_identity_is_a_six_tuple() -> None:
    with tempfile.TemporaryDirectory() as raw:
        p = Path(raw) / "f"
        p.write_bytes(b"x")
        s = p.stat()
        ident = cec.stable_stat_identity(s)
        assert ident == (
            s.st_mode,
            s.st_size,
            s.st_mtime_ns,
            s.st_ctime_ns,
            s.st_dev,
            s.st_ino,
        )


def test_required_helpers_return_valid_values() -> None:
    assert required_string({"k": "v"}, "k") == "v"
    assert required_integer({"n": 7}, "n") == 7
    assert cec.required_string_list({"xs": ["a", "b"]}, "xs") == ("a", "b")


def test_required_string_list_rejects_non_list_and_non_strings() -> None:
    with pytest.raises(ExecutionContractError, match="must be a string list"):
        cec.required_string_list({"xs": "not-a-list"}, "xs")
    with pytest.raises(ExecutionContractError, match="must be a string list"):
        cec.required_string_list({"xs": ["ok", 5]}, "xs")


# ---------------------------------------------------------------------------
# misc pure helpers (cheap, deterministic)
# ---------------------------------------------------------------------------


def test_cached_sha256_fd_dedupes_on_warm_key() -> None:
    with tempfile.TemporaryDirectory() as raw:
        p = Path(raw) / "f"
        p.write_bytes(b"data")
        key = (str(p), (0, 0, 0, 0, 0, 0))
        fd = _open_ro(p)
        try:
            first = cached_sha256_fd(fd, key)
        finally:
            os.close(fd)
        fd = _open_ro(p)
        try:
            second = cached_sha256_fd(fd, key)  # warm -> no re-read
        finally:
            os.close(fd)
        assert first == second


def test_canonical_json_is_sorted_and_compact() -> None:
    assert canonical_json({"b": 2, "a": 1}) == b'{"a":1,"b":2}'
