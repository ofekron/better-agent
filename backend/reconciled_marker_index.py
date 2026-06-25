from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

import perf
import portable_lock


IndexKey = tuple[str, str, int, int, int, int]
Signature = tuple[int, int, int, int, int]


def row_key(row: dict[str, Any]) -> IndexKey:
    run_id = row.get("run_id")
    provider_kind = row.get("provider_kind")
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("invalid reconciled-marker run_id")
    if not isinstance(provider_kind, str) or not provider_kind:
        raise ValueError("invalid reconciled-marker provider_kind")

    def bounded_int(name: str) -> int:
        value = row.get(name)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"invalid reconciled-marker {name}")
        if value < 0 or value > (2**63 - 1):
            raise ValueError(f"out-of-range reconciled-marker {name}")
        return value

    return (
        run_id,
        provider_kind,
        bounded_int("ingestion_version"),
        bounded_int("marker_size"),
        bounded_int("marker_mtime_ns"),
        bounded_int("marker_inode"),
    )


def _signature(path: Path) -> Signature | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


class ReconciledMarkerIndex:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock_path = path.with_suffix(path.suffix + ".lock")
        self._lock = threading.RLock()
        self._signature: Signature | None = None
        self._offset = 0
        self._partial = b""
        self._anchor = b""
        self._keys: set[IndexKey] = set()
        self._latest: dict[str, dict[str, Any]] = {}

    def load_latest(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            self._with_process_lock(self._refresh_locked)
            return {run_id: dict(row) for run_id, row in self._latest.items()}

    def load_keys(self) -> set[IndexKey]:
        with self._lock:
            self._with_process_lock(self._refresh_locked)
            return set(self._keys)

    def append(self, row: dict[str, Any]) -> bool:
        return self.append_many([row]) == 1

    def append_many(self, rows: list[dict[str, Any]]) -> int:
        owned = [dict(row) for row in rows]
        if not owned:
            return 0
        started = time.perf_counter()
        with self._lock:
            appended = self._with_process_lock(
                lambda: self._append_many_locked(owned),
            )
        perf.record(
            "reconciled_marker_index.append",
            (time.perf_counter() - started) * 1000.0,
        )
        perf.record_count(
            "reconciled_marker_index.appended", appended,
        )
        perf.record_count(
            "reconciled_marker_index.duplicate", len(owned) - appended,
        )
        return appended

    def _with_process_lock(self, fn):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+b") as lock_file:
            wait_started = time.perf_counter()
            portable_lock.lock_ex(lock_file.fileno())
            perf.record(
                "reconciled_marker_index.lock_wait",
                (time.perf_counter() - wait_started) * 1000.0,
            )
            try:
                return fn()
            finally:
                portable_lock.unlock(lock_file.fileno())

    def _append_many_locked(self, rows: list[dict[str, Any]]) -> int:
        self._refresh_locked()
        pending: list[dict[str, Any]] = []
        pending_keys: set[IndexKey] = set()
        for row in rows:
            key = row_key(row)
            if key in self._keys or key in pending_keys:
                continue
            pending.append(row)
            pending_keys.add(key)
        if not pending:
            return 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        separator = b"\n" if self._partial else b""
        payload = separator + b"".join(
            json.dumps(row, separators=(",", ":")).encode("utf-8") + b"\n"
            for row in pending
        )
        fd = os.open(self.path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("short reconciled-marker index append")
                view = view[written:]
        finally:
            os.close(fd)
        self._refresh_locked()
        return len(pending)

    def _refresh_locked(self) -> None:
        started = time.perf_counter()
        signature = _signature(self.path)
        if signature is None:
            self._reset_locked()
            return
        rebuild = self._must_rebuild(signature)
        try:
            with self.path.open("rb") as stream:
                if not rebuild and self._anchor:
                    stream.seek(self._offset - len(self._anchor))
                    rebuild = stream.read(len(self._anchor)) != self._anchor
                start = 0 if rebuild else self._offset
                stream.seek(start)
                chunk = stream.read()
        except OSError:
            self._reset_locked()
            return
        if rebuild:
            self._keys.clear()
            self._latest.clear()
            self._partial = b""
            self._anchor = b""
            perf.record_count("reconciled_marker_index.rebuild", 1)
        self._consume_locked(chunk)
        self._offset = start + len(chunk)
        self._anchor = (self._anchor + chunk)[-64:]
        self._signature = signature
        perf.record_count("reconciled_marker_index.bytes_read", len(chunk))
        perf.record(
            "reconciled_marker_index.refresh",
            (time.perf_counter() - started) * 1000.0,
        )

    def _must_rebuild(self, signature: Signature) -> bool:
        previous = self._signature
        if previous is None:
            return True
        if signature[0:2] != previous[0:2] or signature[2] < self._offset:
            return True
        if signature[2] == self._offset and signature != previous:
            return True
        return False

    def _consume_locked(self, chunk: bytes) -> None:
        data = self._partial + chunk
        lines = data.split(b"\n")
        self._partial = lines.pop()
        malformed = 0
        for raw in lines:
            if not raw:
                continue
            try:
                row = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError):
                malformed += 1
                continue
            if not isinstance(row, dict):
                malformed += 1
                continue
            try:
                key = row_key(row)
            except (TypeError, ValueError):
                malformed += 1
                continue
            if not key[0]:
                malformed += 1
                continue
            self._keys.add(key)
            self._latest[key[0]] = row
        if malformed:
            perf.record_count("reconciled_marker_index.malformed", malformed)

    def _reset_locked(self) -> None:
        self._signature = None
        self._offset = 0
        self._partial = b""
        self._anchor = b""
        self._keys.clear()
        self._latest.clear()


_REGISTRY_LOCK = threading.Lock()
_REGISTRY: dict[str, ReconciledMarkerIndex] = {}


def for_path(path: Path) -> ReconciledMarkerIndex:
    key = str(path.absolute())
    with _REGISTRY_LOCK:
        found = _REGISTRY.get(key)
        if found is None:
            found = ReconciledMarkerIndex(path)
            _REGISTRY[key] = found
        return found
