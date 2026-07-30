from __future__ import annotations

import hashlib
import json
import os
import pickle
import re
import stat as stat_module
import subprocess
import sys
import tempfile
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, TypeVar


CONTRACT_SCHEMA = 2
SECRET_NAMES = (
    "api_key",
    "apikey",
    "password",
    "secret",
    "token",
    "credential",
    "authorization",
)
ALLOWED_ENVIRONMENT_SELECTORS = {"CODEX_HOME"}
ALLOWED_CONFIG_KEYS = {
    "features.image_generation",
    "features.shell_snapshot",
    "model",
    "model_provider",
    "shell_environment_policy.exclude",
}
SAFE_PROFILE_RE = re.compile(r"^[A-Za-z0-9_.-]{0,64}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ExecutionContractError(RuntimeError):
    pass


_T = TypeVar("_T")
_S = TypeVar("_S")

# Sized for hash/stat identity capture: sha256 and file I/O release the
# GIL, so per-file capture parallelizes well up to a small multiple of
# the core count before GIL-held path bookkeeping dominates.
_IDENTITY_POOL_WORKERS = min(32, (os.cpu_count() or 4) * 2)
_identity_pool: ThreadPoolExecutor | None = None
_identity_pool_lock = threading.Lock()


def _identity_executor() -> ThreadPoolExecutor:
    global _identity_pool
    if _identity_pool is None:
        with _identity_pool_lock:
            if _identity_pool is None:
                _identity_pool = ThreadPoolExecutor(
                    max_workers=_IDENTITY_POOL_WORKERS,
                    thread_name_prefix="identity-capture",
                )
    return _identity_pool


def parallel_map(fn: Callable[[_S], _T], items: Iterable[_S]) -> list[_T]:
    """Map `fn` over `items` on the shared identity thread pool.

    Results preserve input order. The earliest-in-order exception is
    re-raised in the caller, matching a sequential loop's first-failure
    semantics; `fn` must therefore be side-effect free.

    Items are dispatched in contiguous chunks so per-task executor
    overhead stays negligible for thousands of small captures.

    Only leaf work may run here: `fn` must never call `parallel_map`
    itself, or pool exhaustion can deadlock nested waits.
    """
    values = list(items)
    if len(values) <= 1:
        return [fn(value) for value in values]
    chunk_size = max(1, len(values) // (_IDENTITY_POOL_WORKERS * 4))
    chunks = [
        values[index:index + chunk_size]
        for index in range(0, len(values), chunk_size)
    ]

    def run_chunk(chunk: list[_S]) -> list[_T]:
        return [fn(value) for value in chunk]

    return [
        result
        for chunk_results in _identity_executor().map(run_chunk, chunks)
        for result in chunk_results
    ]


# Per-file identity capture is dominated by GIL-held path bookkeeping, so
# threads cannot speed up a large bundle scan inside a busy process.
# Dedicated `python -I -c` worker subprocesses sidestep the GIL entirely
# and keep scan cost independent of event-loop load. They are used over
# multiprocessing spawn because spawn re-imports the parent's __main__
# (unsafe inside the backend/runner); these workers run a fixed,
# side-effect-free loop instead. Sized to leave headroom for the parent.
_IDENTITY_PROCESS_WORKERS = max(2, min(8, (os.cpu_count() or 4) - 2))
# Below this size the fork/exec + import cost outweighs the parallelism.
_PROCESS_MAP_MIN_ITEMS = 64

_PROCESS_WORKER_SOURCE = """
import pickle, sys
sys.path.insert(0, sys.argv[1])
try:
    fn, values = pickle.loads(sys.stdin.buffer.read())
    result = (True, [fn(value) for value in values])
except BaseException as exc:
    try:
        pickle.dumps(exc)
    except BaseException:
        from codex_execution_common import ExecutionContractError
        exc = ExecutionContractError(f"{type(exc).__name__}: {exc}")
    result = (False, exc)
sys.stdout.buffer.write(pickle.dumps(result))
sys.stdout.buffer.flush()
"""


def parallel_map_processes(
    fn: Callable[[_S], _T],
    items: Iterable[_S],
) -> list[_T]:
    """Map `fn` over `items` on short-lived capture worker processes.

    Same order and first-failure semantics as `parallel_map`; `fn` must
    be a picklable module-level callable and side-effect free. Worker
    failures fail closed as `ExecutionContractError`. Falls back to the
    thread pool for small inputs and frozen runtimes (whose executable
    cannot run `-c`).
    """
    values = list(items)
    if len(values) < _PROCESS_MAP_MIN_ITEMS or getattr(sys, "frozen", False):
        return parallel_map(fn, values)
    backend_root = str(Path(__file__).resolve().parent)
    workers = min(_IDENTITY_PROCESS_WORKERS, len(values))
    chunk_size = -(-len(values) // workers)
    chunks = [
        values[index:index + chunk_size]
        for index in range(0, len(values), chunk_size)
    ]
    processes: list[subprocess.Popen] = []
    stderr_files: list[Any] = []
    outputs: list[tuple[bytes, bytes, int]] = []
    try:
        payloads = [pickle.dumps((fn, chunk)) for chunk in chunks]
        for payload in payloads:
            # stderr goes to an unlinked temp file so a crashing worker
            # can never interlock pipes with the stdout read below.
            stderr_file = tempfile.TemporaryFile()
            stderr_files.append(stderr_file)
            process = subprocess.Popen(
                [sys.executable, "-I", "-c", _PROCESS_WORKER_SOURCE,
                 backend_root],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=stderr_file,
            )
            processes.append(process)
            # Feed every worker up front so they all run concurrently;
            # each worker drains stdin fully before starting its chunk.
            assert process.stdin is not None
            process.stdin.write(payload)
            process.stdin.close()
        for process, stderr_file in zip(processes, stderr_files):
            assert process.stdout is not None
            stdout = process.stdout.read()
            returncode = process.wait()
            stderr_file.seek(0)
            outputs.append((stdout, stderr_file.read(), returncode))
    except (OSError, ValueError, pickle.PicklingError) as exc:
        raise ExecutionContractError(
            "identity capture worker failed",
        ) from exc
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait()
        for stderr_file in stderr_files:
            stderr_file.close()
    results: list[_T] = []
    for stdout, stderr, returncode in outputs:
        if returncode != 0 or not stdout:
            raise ExecutionContractError(
                "identity capture worker failed: "
                + stderr.decode("utf-8", "replace")[:512],
            )
        try:
            ok, value = pickle.loads(stdout)
        except Exception as exc:
            raise ExecutionContractError(
                "identity capture worker returned invalid data",
            ) from exc
        if not ok:
            raise value
        results.extend(value)
    return results


def record_step_since(name: str, started: float) -> None:
    """Record wall-clock ms since `started` (perf_counter) via perf.record.

    perf is import-guarded: these contract modules also run inside spawned
    runner processes where the backend perf logger may be unavailable.
    """
    try:
        import perf

        perf.record(name, (time.perf_counter() - started) * 1000.0)
    except Exception:
        pass


@contextmanager
def timed_contract_step(name: str) -> Iterator[None]:
    """Record wall-clock ms for an attestation/launch step via perf.record."""
    started = time.perf_counter()
    try:
        yield
    finally:
        record_step_since(name, started)


def binary_open_flags(flags: int) -> int:
    return flags | getattr(os, "O_BINARY", 0)


def sha256_fd(fd: int) -> str:
    digest = hashlib.sha256()
    os.lseek(fd, 0, os.SEEK_SET)
    while True:
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
    os.lseek(fd, 0, os.SEEK_SET)
    return digest.hexdigest()


def stable_stat_identity(stat_result: os.stat_result) -> tuple[int, ...]:
    return (
        stat_result.st_mode,
        stat_result.st_size,
        stat_result.st_mtime_ns,
        stat_result.st_ctime_ns,
        stat_result.st_dev,
        stat_result.st_ino,
    )


# Bounds the process-wide hash-reuse cache below: sized generously above the
# handful of large components (CLI binary, interpreter, a few dozen config/
# SDK files) any single launch family touches, so warm reuse never evicts a
# live entry mid-launch.
_HASH_CACHE_LIMIT = 512
_hash_cache: "OrderedDict[tuple[str, tuple[int, ...]], str]" = OrderedDict()
_hash_cache_lock = threading.Lock()


def cached_sha256_fd(fd: int, cache_key: tuple[str, tuple[int, ...]]) -> str:
    """sha256 of `fd`'s full content, reused across calls for an identical
    (resolved path, stable stat tuple).

    This is the single hash-verification primitive full-file attestation/
    capture funnels through. A stat-tuple change is a different cache key
    by construction, so drift always misses the cache and falls through
    to a real read - the cache can only short-circuit hashing a file that
    has provably not changed since the last time it was hashed (same
    device/inode/size/mtime/ctime), never paper over a mismatch.
    """
    with _hash_cache_lock:
        cached = _hash_cache.get(cache_key)
        if cached is not None:
            _hash_cache.move_to_end(cache_key)
            return cached
    digest = sha256_fd(fd)
    with _hash_cache_lock:
        _hash_cache[cache_key] = digest
        _hash_cache.move_to_end(cache_key)
        while len(_hash_cache) > _HASH_CACHE_LIMIT:
            _hash_cache.popitem(last=False)
    return digest


class AttestOnceCache:
    """Per-instance cache collapsing repeated full-hash attestation calls.

    The first `resolve()` call runs `full_check` (a real, hash-verifying
    attest). Once that succeeds, every later call on the same instance
    runs the cheaper `metadata_check` (stat-tuple only, no re-hash)
    instead. A failed check never poisons the cache as verified, so a
    later legitimate capture/attest cycle can still retry the full
    check from scratch.
    """

    __slots__ = ("_verified", "_lock")

    def __init__(self) -> None:
        self._verified = False
        self._lock = threading.Lock()

    def resolve(
        self,
        full_check: Callable[[], bool],
        metadata_check: Callable[[], bool],
    ) -> bool:
        with self._lock:
            verified = self._verified
        if verified:
            return metadata_check()
        if not full_check():
            return False
        with self._lock:
            self._verified = True
        return True


def sha256_and_first_line_fd(fd: int) -> tuple[str, bytes]:
    digest = hashlib.sha256()
    first_line = bytearray()
    line_complete = False
    os.lseek(fd, 0, os.SEEK_SET)
    while True:
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
        if not line_complete and len(first_line) < 4096:
            remaining = 4096 - len(first_line)
            prefix = chunk[:remaining]
            newline = prefix.find(b"\n")
            if newline >= 0:
                first_line.extend(prefix[:newline + 1])
                line_complete = True
            else:
                first_line.extend(prefix)
                if len(first_line) == 4096:
                    line_complete = True
    os.lseek(fd, 0, os.SEEK_SET)
    return digest.hexdigest(), bytes(first_line)


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def required_string(raw: Mapping[str, Any], key: str) -> str:
    value = raw.get(key)
    if type(value) is not str:
        raise ExecutionContractError(f"{key} must be a string")
    return value


def required_integer(raw: Mapping[str, Any], key: str) -> int:
    value = raw.get(key)
    if type(value) is not int:
        raise ExecutionContractError(f"{key} must be an integer")
    return value


def required_string_list(
    raw: Mapping[str, Any],
    key: str,
) -> tuple[str, ...]:
    value = raw.get(key)
    if type(value) is not list or any(type(item) is not str for item in value):
        raise ExecutionContractError(f"{key} must be a string list")
    return tuple(value)


def symlink_chain(path: Path) -> tuple[tuple[str, str], ...]:
    # Fast path for the overwhelmingly common non-symlink case: one
    # lstat, no Path bookkeeping. Path.is_symlink treats OSError as
    # "not a symlink", so an unreadable path yields an empty chain in
    # both branches.
    try:
        if not stat_module.S_ISLNK(os.lstat(path).st_mode):
            return ()
    except OSError:
        return ()
    current = path
    seen: set[Path] = set()
    chain: list[tuple[str, str]] = []
    for _ in range(64):
        absolute = current.absolute()
        if absolute in seen:
            raise ExecutionContractError("provider executable symlink cycle")
        seen.add(absolute)
        if not current.is_symlink():
            return tuple(chain)
        target = os.readlink(current)
        chain.append((str(absolute), target))
        target_path = Path(target)
        current = (
            target_path
            if target_path.is_absolute()
            else current.parent / target_path
        )
    raise ExecutionContractError("provider executable symlink chain is too deep")
