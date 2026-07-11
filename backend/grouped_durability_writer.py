from __future__ import annotations

import os
import tempfile
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import perf


@dataclass(frozen=True)
class DurabilityReceipt:
    generation: int
    future: Future[int]

    def wait(self, timeout: Optional[float] = None) -> int:
        return self.future.result(timeout=timeout)


@dataclass(frozen=True)
class BatchSnapshot:
    generations: tuple[int, ...]
    targets: tuple[Path, ...]
    parent_dirs: tuple[Path, ...]


@dataclass(frozen=True)
class _Intent:
    generation: int
    target: Path
    payload: Optional[bytes]
    future: Future[int]


@dataclass
class _Staged:
    intent: _Intent
    temp_path: Optional[Path]
    fd: Optional[int]


CrashHook = Callable[[str, BatchSnapshot], None]


class GroupedDurabilityWriter:
    def __init__(
        self,
        *,
        max_batch_size: int = 64,
        max_batch_age_s: float = 0.01,
        crash_hook: Optional[CrashHook] = None,
        thread_name: str = "grouped-durability-writer",
    ) -> None:
        if max_batch_size < 1:
            raise ValueError("max_batch_size must be positive")
        if max_batch_age_s < 0:
            raise ValueError("max_batch_age_s must be non-negative")
        self._max_batch_size = max_batch_size
        self._max_batch_age_s = max_batch_age_s
        self._crash_hook = crash_hook
        self._cv = threading.Condition()
        self._pending: list[_Intent] = []
        self._generation = 0
        self._active = False
        self._closing = False
        self._closed = False
        self._metric_prefix = f"durability_writer.{thread_name}"
        self._thread = threading.Thread(target=self._run, name=thread_name, daemon=True)
        perf.register_queue(thread_name, self.pending_count)
        self._thread.start()

    def replace(self, target: Path, payload: bytes) -> DurabilityReceipt:
        if not isinstance(payload, bytes):
            raise TypeError("payload must be bytes")
        return self._enqueue(Path(target), payload)

    def unlink(self, target: Path) -> DurabilityReceipt:
        return self._enqueue(Path(target), None)

    def pending_count(self) -> int:
        with self._cv:
            return len(self._pending) + int(self._active)

    def drain(self, timeout: Optional[float] = None) -> None:
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cv:
            while self._pending or self._active:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise TimeoutError("durability writer drain timed out")
                self._cv.wait(remaining)

    def close(self, timeout: Optional[float] = None) -> None:
        with self._cv:
            if self._closed:
                return
            self._closing = True
            self._cv.notify_all()
        self._thread.join(timeout)
        if self._thread.is_alive():
            raise TimeoutError("durability writer shutdown timed out")
        perf.unregister_queue(self._thread.name)

    def _enqueue(self, target: Path, payload: Optional[bytes]) -> DurabilityReceipt:
        future: Future[int] = Future()
        with self._cv:
            if self._closing:
                raise RuntimeError("durability writer is closing")
            self._generation += 1
            generation = self._generation
            self._pending.append(_Intent(generation, target, payload, future))
            self._cv.notify_all()
        return DurabilityReceipt(generation, future)

    def _run(self) -> None:
        while True:
            with self._cv:
                while not self._pending and not self._closing:
                    self._cv.wait()
                if not self._pending and self._closing:
                    self._closed = True
                    self._cv.notify_all()
                    return
                deadline = time.monotonic() + self._max_batch_age_s
                while len(self._pending) < self._max_batch_size and not self._closing:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self._cv.wait(remaining)
                batch = self._pending[: self._max_batch_size]
                del self._pending[: len(batch)]
                self._active = True
            try:
                self._commit(batch)
            except BaseException as exc:
                for intent in batch:
                    if not intent.future.done():
                        intent.future.set_exception(exc)
                perf.record_count(f"{self._metric_prefix}.failed", len(batch))
            finally:
                with self._cv:
                    self._active = False
                    self._cv.notify_all()

    def _commit(self, batch: list[_Intent]) -> None:
        started = time.perf_counter()
        staged: list[_Staged] = []
        parent_dirs = tuple(sorted({intent.target.parent for intent in batch}, key=os.fspath))
        snapshot = BatchSnapshot(
            tuple(intent.generation for intent in batch),
            tuple(intent.target for intent in batch),
            parent_dirs,
        )
        try:
            phase_started = time.perf_counter()
            for intent in batch:
                intent.target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                if intent.payload is None:
                    staged.append(_Staged(intent, None, None))
                    continue
                fd, name = tempfile.mkstemp(
                    prefix=f".{intent.target.name}.", suffix=".durability.tmp", dir=intent.target.parent
                )
                try:
                    with os.fdopen(fd, "wb", closefd=False) as handle:
                        handle.write(intent.payload)
                        handle.flush()
                except BaseException:
                    os.close(fd)
                    Path(name).unlink(missing_ok=True)
                    raise
                staged.append(_Staged(intent, Path(name), fd))
            self._record_phase("temp_flush", phase_started)
            self._hook("after_temp_flush", snapshot)

            phase_started = time.perf_counter()
            for item in staged:
                if item.fd is not None:
                    os.fsync(item.fd)
                    os.close(item.fd)
                    item.fd = None
            self._record_phase("file_fsync", phase_started)
            self._hook("after_file_fsync", snapshot)

            phase_started = time.perf_counter()
            for item in staged:
                if item.temp_path is None:
                    item.intent.target.unlink(missing_ok=True)
                else:
                    os.replace(item.temp_path, item.intent.target)
                    item.temp_path = None
            self._record_phase("mutation", phase_started)
            self._hook("after_mutation", snapshot)

            phase_started = time.perf_counter()
            for parent in parent_dirs:
                try:
                    directory_fd = os.open(parent, os.O_RDONLY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                except OSError:
                    if os.name != "nt":
                        raise
            self._record_phase("dir_fsync", phase_started)
            self._hook("after_dir_fsync", snapshot)
            self._hook("before_ack", snapshot)

            high_water = max(intent.generation for intent in batch)
            for intent in batch:
                intent.future.set_result(high_water)
            perf.record_count(f"{self._metric_prefix}.batch_size", len(batch))
            perf.record_count(f"{self._metric_prefix}.parent_dirs", len(parent_dirs))
            perf.record_count(f"{self._metric_prefix}.replace", sum(i.payload is not None for i in batch))
            perf.record_count(f"{self._metric_prefix}.unlink", sum(i.payload is None for i in batch))
            perf.record(f"{self._metric_prefix}.batch", (time.perf_counter() - started) * 1000.0)
        finally:
            for item in staged:
                if item.fd is not None:
                    os.close(item.fd)
                if item.temp_path is not None:
                    item.temp_path.unlink(missing_ok=True)

    def _record_phase(self, phase: str, started: float) -> None:
        perf.record(f"{self._metric_prefix}.{phase}", (time.perf_counter() - started) * 1000.0)

    def _hook(self, phase: str, snapshot: BatchSnapshot) -> None:
        if self._crash_hook is not None:
            self._crash_hook(phase, snapshot)
