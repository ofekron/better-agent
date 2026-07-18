from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from typing import Awaitable, Callable

import perf
from ws_snapshot_binary import (
    SNAPSHOT_BINARY_ENCODING,
    SNAPSHOT_CHUNK_BYTES,
    encode_snapshot_chunk,
)
from ws_serialization import SerializedWebSocketFrame, dumps_ws_json


MAX_WS_FRAME_BYTES = 256 * 1024
SNAPSHOT_THRESHOLD_BYTES = 240 * 1024
SNAPSHOT_CACHE_TTL_SECONDS = 120.0
SNAPSHOT_CACHE_MAX_BYTES = 64 * 1024 * 1024
SNAPSHOT_MAX_PAYLOAD_BYTES = 16 * 1024 * 1024
SNAPSHOT_WINDOW_CHUNKS = 2
SNAPSHOT_EVENT_TYPES = frozenset({
    "messages_replay",
    "rewind_complete",
    "stub_invalidated",
})


@dataclass(frozen=True)
class Snapshot:
    snapshot_id: str
    principal: str
    key: str
    event_type: str
    revision: str
    digest: str
    payload_bytes: int
    chunks: tuple[bytes, ...]
    scope: tuple[tuple[str, str | None], ...]
    refresh_id: str
    created_at: float

    @property
    def total_bytes(self) -> int:
        return self.payload_bytes

    @property
    def retained_bytes(self) -> int:
        return sum(len(chunk) for chunk in self.chunks)


class SnapshotCache:
    def __init__(
        self,
        *,
        ttl_seconds: float = SNAPSHOT_CACHE_TTL_SECONDS,
        max_bytes: int = SNAPSHOT_CACHE_MAX_BYTES,
    ) -> None:
        self._ttl_seconds = ttl_seconds
        self._max_bytes = max_bytes
        self._entries: OrderedDict[str, Snapshot] = OrderedDict()
        self._active: dict[str, int] = {}
        self._bytes = 0
        self._reserved_bytes = 0

    def reserve(self, retained_bytes: int, *, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        self._purge(current)
        if retained_bytes > self._max_bytes:
            return False
        self._evict_to_fit(retained_bytes)
        if self._bytes + self._reserved_bytes + retained_bytes > self._max_bytes:
            return False
        self._reserved_bytes += retained_bytes
        return True

    def cancel_reservation(self, retained_bytes: int) -> None:
        if retained_bytes < 0 or retained_bytes > self._reserved_bytes:
            raise RuntimeError("invalid snapshot cache reservation release")
        self._reserved_bytes -= retained_bytes

    def put(
        self,
        snapshot: Snapshot,
        *,
        now: float | None = None,
        reserved_bytes: int = 0,
    ) -> bool:
        current = time.monotonic() if now is None else now
        self._purge(current)
        if reserved_bytes:
            if snapshot.retained_bytes > reserved_bytes:
                self.cancel_reservation(reserved_bytes)
                return False
            self.cancel_reservation(reserved_bytes)
        elif not self.reserve(snapshot.retained_bytes, now=current):
            return False
        if not reserved_bytes:
            self.cancel_reservation(snapshot.retained_bytes)
        replaced = self._entries.pop(snapshot.snapshot_id, None)
        if replaced is not None:
            self._bytes -= replaced.retained_bytes
        if self._bytes + self._reserved_bytes + snapshot.retained_bytes > self._max_bytes:
            return False
        self._entries[snapshot.snapshot_id] = snapshot
        self._bytes += snapshot.retained_bytes
        return True

    def get(self, snapshot_id: str, *, now: float | None = None) -> Snapshot | None:
        current = time.monotonic() if now is None else now
        self._purge(current)
        snapshot = self._entries.get(snapshot_id)
        if snapshot is not None:
            self._entries.move_to_end(snapshot_id)
        return snapshot

    def acquire(self, snapshot_id: str) -> Snapshot | None:
        snapshot = self.get(snapshot_id)
        if snapshot is None:
            return None
        self._active[snapshot_id] = self._active.get(snapshot_id, 0) + 1
        return snapshot

    def release(self, snapshot_id: str) -> None:
        count = self._active.get(snapshot_id, 0)
        if count <= 1:
            self._active.pop(snapshot_id, None)
        else:
            self._active[snapshot_id] = count - 1

    def latest_for_key(
        self, *, principal: str, key: str, event_type: str,
    ) -> Snapshot | None:
        self._purge(time.monotonic())
        for snapshot in reversed(self._entries.values()):
            if (
                snapshot.principal == principal
                and snapshot.key == key
                and snapshot.event_type == event_type
            ):
                return snapshot
        return None

    def find(
        self,
        *,
        principal: str,
        key: str,
        digest: str,
        now: float | None = None,
    ) -> Snapshot | None:
        current = time.monotonic() if now is None else now
        self._purge(current)
        for snapshot_id, snapshot in reversed(self._entries.items()):
            if (
                snapshot.principal == principal
                and snapshot.key == key
                and snapshot.digest == digest
            ):
                self._entries.move_to_end(snapshot_id)
                return snapshot
        return None

    def _purge(self, now: float) -> None:
        expired = [
            snapshot_id
            for snapshot_id, snapshot in self._entries.items()
            if (
                now - snapshot.created_at > self._ttl_seconds
                and self._active.get(snapshot_id, 0) == 0
            )
        ]
        for snapshot_id in expired:
            snapshot = self._entries.pop(snapshot_id)
            self._bytes -= snapshot.retained_bytes

    def _evict_to_fit(self, incoming_bytes: int) -> None:
        for snapshot_id in tuple(self._entries):
            if self._bytes + self._reserved_bytes + incoming_bytes <= self._max_bytes:
                return
            if self._active.get(snapshot_id, 0) > 0:
                continue
            snapshot = self._entries.pop(snapshot_id)
            self._bytes -= snapshot.retained_bytes


_SNAPSHOT_CACHE = SnapshotCache()


@dataclass
class _Transfer:
    snapshot: Snapshot
    acknowledged: int = 0
    sent_until: int = 0
    acquired: bool = True


PreparedSender = Callable[[dict, SerializedWebSocketFrame | None], Awaitable[bool]]
BinarySender = Callable[[bytes], Awaitable[bool]]
RefreshSender = Callable[[tuple[tuple[str, str | None], ...], str], Awaitable[bool]]


class SnapshotTransport:
    def __init__(
        self,
        *,
        principal: str,
        send: PreparedSender,
        send_binary: BinarySender | None = None,
        binary: bool = False,
        refresh: RefreshSender | None = None,
        cache: SnapshotCache | None = None,
    ) -> None:
        if binary and send_binary is None:
            raise ValueError("binary snapshot transport requires a binary sender")
        self._principal = hashlib.sha256(principal.encode("utf-8")).hexdigest()
        self._send = send
        self._send_binary = send_binary
        self._binary = binary
        self._refresh = refresh
        self._cache = cache or _SNAPSHOT_CACHE
        self._active_by_key: dict[str, _Transfer] = {}
        self._active_by_id: dict[str, _Transfer] = {}
        self._lock = asyncio.Lock()
        self._refresh_by_key: dict[
            str,
            tuple[str, str, tuple[tuple[str, str | None], ...], str],
        ] = {}

    async def send_event(self, event: dict) -> bool:
        serialized_task = getattr(event, "_bc_serialized_json_task", None)
        serialized = (
            await serialized_task
            if serialized_task is not None
            else await dumps_ws_json(event)
        )
        event_type = event.get("type") if isinstance(event, dict) else None
        if event_type not in SNAPSHOT_EVENT_TYPES:
            return await self._send(event, serialized)
        prepare_started = time.perf_counter()
        payload, digest = await asyncio.to_thread(_encode_and_digest, serialized)
        perf.record(
            "ws.snapshot.encode_digest",
            (time.perf_counter() - prepare_started) * 1000.0,
        )
        if len(payload) <= SNAPSHOT_THRESHOLD_BYTES:
            return await self._send(event, serialized)
        if len(payload) > SNAPSHOT_MAX_PAYLOAD_BYTES:
            perf.record_count("ws.snapshot.rejected_too_large")
            key = _snapshot_key(event_type, event)
            revision = _snapshot_revision(event, digest)
            scope = _snapshot_scope(event_type, event)
            refresh_id = uuid.uuid4().hex
            async with self._lock:
                self._refresh_by_key[key] = (event_type, revision, scope, refresh_id)
                return await self._send({
                    "type": "snapshot_refresh_required",
                    "data": {
                        "key": key,
                        "event_type": event_type,
                        "revision": revision,
                        "reason": "too_large",
                        "refresh_id": refresh_id,
                    },
                }, None)

        key = _snapshot_key(event_type, event)
        snapshot = self._cache.find(
            principal=self._principal,
            key=key,
            digest=digest,
        )
        if snapshot is None:
            reserved_bytes = len(payload)
            if not self._cache.reserve(reserved_bytes):
                perf.record_count("ws.snapshot.rejected_capacity")
                revision = _snapshot_revision(event, digest)
                scope = _snapshot_scope(event_type, event)
                refresh_id = uuid.uuid4().hex
                async with self._lock:
                    self._refresh_by_key[key] = (
                        event_type,
                        revision,
                        scope,
                        refresh_id,
                    )
                    return await self._send({
                        "type": "snapshot_refresh_required",
                        "data": {
                            "key": key,
                            "event_type": event_type,
                            "revision": revision,
                            "reason": "overflow",
                            "refresh_id": refresh_id,
                        },
                    }, None)
            chunk_started = time.perf_counter()
            try:
                chunks = await asyncio.to_thread(_split_chunks, payload)
            except BaseException:
                self._cache.cancel_reservation(reserved_bytes)
                raise
            perf.record(
                "ws.snapshot.chunk_prepare",
                (time.perf_counter() - chunk_started) * 1000.0,
            )
            candidate = Snapshot(
                snapshot_id=uuid.uuid4().hex,
                principal=self._principal,
                key=key,
                event_type=event_type,
                revision=_snapshot_revision(event, digest),
                digest=digest,
                payload_bytes=len(payload),
                chunks=chunks,
                scope=_snapshot_scope(event_type, event),
                refresh_id=uuid.uuid4().hex,
                created_at=time.monotonic(),
            )
            if not self._cache.put(candidate, reserved_bytes=reserved_bytes):
                perf.record_count("ws.snapshot.rejected_capacity")
                async with self._lock:
                    self._refresh_by_key[key] = (
                        event_type,
                        candidate.revision,
                        candidate.scope,
                        candidate.refresh_id,
                    )
                    return await self._send({
                        "type": "snapshot_refresh_required",
                        "data": {
                            "key": key,
                            "event_type": event_type,
                            "revision": candidate.revision,
                            "reason": "overflow",
                            "refresh_id": candidate.refresh_id,
                        },
                    }, None)
            snapshot = candidate
        if self._cache.acquire(snapshot.snapshot_id) is None:
            return False
        transfer = _Transfer(snapshot=snapshot)
        async with self._lock:
            self._refresh_by_key[key] = (
                event_type,
                snapshot.revision,
                snapshot.scope,
                snapshot.refresh_id,
            )
            prior = self._active_by_key.get(key)
            if prior is not None:
                self._active_by_id.pop(prior.snapshot.snapshot_id, None)
                self._release_transfer(prior)
                if not await self._send({
                    "type": "snapshot_cancelled",
                    "data": {
                        "snapshot_id": prior.snapshot.snapshot_id,
                        "revision": prior.snapshot.revision,
                        "reason": "superseded",
                    },
                }, None):
                    self._release_transfer(transfer)
                    return False
            self._active_by_key[key] = transfer
            self._active_by_id[snapshot.snapshot_id] = transfer
            sent = await self._send(
                _begin_frame(snapshot, resume_from=0, binary=self._binary),
                None,
            )
            if not sent:
                self._active_by_key.pop(key, None)
                self._active_by_id.pop(snapshot.snapshot_id, None)
                self._release_transfer(transfer)
        perf.record_count("ws.snapshot.payload_bytes", len(payload))
        return sent

    async def acknowledge(self, message: dict) -> bool:
        data = _message_data(message)
        parsed = _parse_progress(data)
        if parsed is None:
            return False
        snapshot_id, revision, next_chunk = parsed
        async with self._lock:
            transfer = self._active_by_id.get(snapshot_id)
            if transfer is None or transfer.snapshot.revision != revision:
                return False
            if next_chunk < transfer.acknowledged or next_chunk > transfer.sent_until:
                return False
            transfer.acknowledged = next_chunk
            return await self._pump_locked(transfer)

    async def resume(self, message: dict) -> bool:
        data = _message_data(message)
        parsed = _parse_progress(data)
        digest = data.get("digest") if isinstance(data, dict) else None
        if parsed is None or not _is_digest(digest):
            return False
        snapshot_id, revision, next_chunk = parsed
        snapshot = self._cache.acquire(snapshot_id)
        if snapshot is None:
            return await self._restart_required(snapshot_id, revision, "not_found")
        if snapshot.principal != self._principal:
            self._cache.release(snapshot_id)
            return False
        if snapshot.revision != revision or snapshot.digest != digest:
            self._cache.release(snapshot_id)
            return await self._restart_required(snapshot_id, revision, "revision_mismatch")
        if next_chunk > len(snapshot.chunks):
            self._cache.release(snapshot_id)
            return await self._restart_required(snapshot_id, revision, "invalid_offset")

        transfer = _Transfer(
            snapshot=snapshot,
            acknowledged=next_chunk,
            sent_until=next_chunk,
        )
        async with self._lock:
            prior = self._active_by_key.get(snapshot.key)
            if prior is not None:
                self._active_by_id.pop(prior.snapshot.snapshot_id, None)
                self._release_transfer(prior)
            self._active_by_key[snapshot.key] = transfer
            self._active_by_id[snapshot_id] = transfer
            if not await self._send(
                _begin_frame(snapshot, resume_from=next_chunk, binary=self._binary),
                None,
            ):
                self._active_by_key.pop(snapshot.key, None)
                self._active_by_id.pop(snapshot_id, None)
                self._release_transfer(transfer)
                return False
            return await self._pump_locked(transfer)

    async def _pump_locked(self, transfer: _Transfer) -> bool:
        snapshot = transfer.snapshot
        target = min(
            transfer.acknowledged + SNAPSHOT_WINDOW_CHUNKS,
            len(snapshot.chunks),
        )
        while transfer.sent_until < target:
            index = transfer.sent_until
            chunk = snapshot.chunks[index]
            if self._binary:
                if self._send_binary is None:
                    raise RuntimeError("binary snapshot sender is unavailable")
                frame = encode_snapshot_chunk(snapshot.snapshot_id, index, chunk)
                if len(frame) > MAX_WS_FRAME_BYTES:
                    raise RuntimeError("snapshot chunk exceeds WebSocket frame bound")
                if not await self._send_binary(frame):
                    return False
                transfer.sent_until += 1
                continue
            encode_started = time.perf_counter()
            payload = await asyncio.to_thread(_encode_legacy_chunk, chunk)
            perf.record(
                "ws.snapshot.legacy_base64_encode",
                (time.perf_counter() - encode_started) * 1000.0,
            )
            frame = {
                "type": "snapshot_chunk",
                "data": {
                    "snapshot_id": snapshot.snapshot_id,
                    "revision": snapshot.revision,
                    "index": index,
                    "payload": payload,
                },
            }
            if _encoded_size(frame) > MAX_WS_FRAME_BYTES:
                raise RuntimeError("snapshot chunk exceeds WebSocket frame bound")
            if not await self._send(frame, None):
                return False
            transfer.sent_until += 1
        if transfer.acknowledged != len(snapshot.chunks):
            return True
        sent = await self._send(_end_frame(snapshot), None)
        self._active_by_id.pop(snapshot.snapshot_id, None)
        if self._active_by_key.get(snapshot.key) is transfer:
            self._active_by_key.pop(snapshot.key, None)
        self._release_transfer(transfer)
        return sent

    async def refresh(self, message: dict) -> bool:
        data = _message_data(message)
        if not isinstance(data, dict):
            return False
        key = data.get("key")
        event_type = data.get("event_type")
        revision = data.get("failed_revision")
        reason = data.get("reason")
        refresh_id = data.get("refresh_id")
        if (
            not isinstance(key, str)
            or not 1 <= len(key) <= 1024
            or event_type not in SNAPSHOT_EVENT_TYPES
            or not _is_revision(revision)
            or reason not in {"restart_required", "corrupt", "overflow", "too_large"}
            or not _is_snapshot_id(refresh_id)
        ):
            return False
        async with self._lock:
            owned = self._refresh_by_key.get(key)
            if owned is None:
                snapshot = self._cache.latest_for_key(
                    principal=self._principal,
                    key=key,
                    event_type=event_type,
                )
                if snapshot is None:
                    return False
                owned = (
                    snapshot.event_type,
                    snapshot.revision,
                    snapshot.scope,
                    snapshot.refresh_id,
                )
            if owned[0] != event_type or owned[1] != revision or owned[3] != refresh_id:
                return False
            transfer = self._active_by_key.get(key)
            if transfer is not None:
                self._active_by_key.pop(key, None)
                self._active_by_id.pop(transfer.snapshot.snapshot_id, None)
                self._release_transfer(transfer)
                if not await self._send({
                    "type": "snapshot_cancelled",
                    "data": {
                        "snapshot_id": transfer.snapshot.snapshot_id,
                        "revision": transfer.snapshot.revision,
                        "reason": "refresh",
                    },
                }, None):
                    return False
        return self._refresh is not None and await self._refresh(owned[2], owned[3])

    async def close(self) -> None:
        async with self._lock:
            for transfer in tuple(self._active_by_id.values()):
                self._release_transfer(transfer)
            self._active_by_id.clear()
            self._active_by_key.clear()
            self._refresh_by_key.clear()

    def _release_transfer(self, transfer: _Transfer) -> None:
        if not transfer.acquired:
            return
        transfer.acquired = False
        self._cache.release(transfer.snapshot.snapshot_id)

    async def _restart_required(
        self, snapshot_id: str, revision: str, reason: str,
    ) -> bool:
        return await self._send({
            "type": "snapshot_restart_required",
            "data": {
                "snapshot_id": snapshot_id,
                "revision": revision,
                "reason": reason,
            },
        }, None)


def _snapshot_key(event_type: str, event: dict) -> str:
    data = event.get("data")
    if not isinstance(data, dict):
        raise ValueError("snapshot event data must be an object")
    if event_type == "stub_invalidated":
        scope = _snapshot_scope(event_type, event)
        encoded = json.dumps(scope, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        return f"stub_invalidated:{hashlib.sha256(encoded).hexdigest()}"
    session_id = data.get("app_session_id") or data.get("session_id")
    if not isinstance(session_id, str) or not session_id or len(session_id) > 256:
        raise ValueError("snapshot session id is invalid")
    return f"{event_type}:{session_id}"


def _snapshot_scope(
    event_type: str, event: dict,
) -> tuple[tuple[str, str | None], ...]:
    data = event.get("data")
    if not isinstance(data, dict):
        raise ValueError("snapshot event data must be an object")
    if event_type != "stub_invalidated":
        session_id = data.get("app_session_id") or data.get("session_id")
        if not isinstance(session_id, str) or not session_id or len(session_id) > 256:
            raise ValueError("snapshot session id is invalid")
        return ((session_id, None),)
    changes = data.get("changes")
    if not isinstance(changes, list) or not changes:
        raise ValueError("stub invalidation scope is invalid")
    identities: set[tuple[str, str | None]] = set()
    for change in changes:
        if not isinstance(change, dict):
            raise ValueError("stub invalidation change is invalid")
        session_id = change.get("app_session_id")
        message_id = change.get("msg_id")
        if (
            not isinstance(session_id, str)
            or not session_id
            or len(session_id) > 256
            or not isinstance(message_id, str)
            or not message_id
            or len(message_id) > 256
        ):
            raise ValueError("stub invalidation identity is invalid")
        identities.add((session_id, message_id))
    return tuple(sorted(identities))


def _message_data(message: dict) -> dict | None:
    data = message.get("data")
    return data if isinstance(data, dict) else message


def _parse_progress(data: dict | None) -> tuple[str, str, int] | None:
    if not isinstance(data, dict):
        return None
    snapshot_id = data.get("snapshot_id")
    revision = data.get("revision")
    next_chunk = data.get("next_chunk")
    if (
        not _is_snapshot_id(snapshot_id)
        or not _is_revision(revision)
        or isinstance(next_chunk, bool)
        or not isinstance(next_chunk, int)
        or next_chunk < 0
    ):
        return None
    return snapshot_id, revision, next_chunk


def _is_snapshot_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 32
        and all(ch in "0123456789abcdef" for ch in value)
    )


def _is_digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(ch in "0123456789abcdef" for ch in value)
    )


def _is_revision(value: object) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 80
        and all(ch.isascii() and (ch.isalnum() or ch in ":_-") for ch in value)
    )


def _snapshot_revision(event: dict, digest: str) -> str:
    data = event.get("data")
    if isinstance(data, dict):
        next_seq = data.get("next_seq")
        if isinstance(next_seq, int) and not isinstance(next_seq, bool) and next_seq >= 0:
            return f"seq:{next_seq}"
    event_seq = event.get("seq")
    if isinstance(event_seq, int) and not isinstance(event_seq, bool) and event_seq >= 0:
        return f"event:{event_seq}"
    return f"sha256:{digest}"


def _begin_frame(snapshot: Snapshot, *, resume_from: int, binary: bool = False) -> dict:
    frame = {
        "type": "snapshot_begin",
        "data": {
            "snapshot_id": snapshot.snapshot_id,
            "key": snapshot.key,
            "event_type": snapshot.event_type,
            "revision": snapshot.revision,
            "digest": snapshot.digest,
            "total_bytes": snapshot.total_bytes,
            "total_chunks": len(snapshot.chunks),
            "chunk_bytes": SNAPSHOT_CHUNK_BYTES,
            "resume_from": resume_from,
            "refresh_id": snapshot.refresh_id,
        },
    }
    if binary:
        frame["data"]["encoding"] = SNAPSHOT_BINARY_ENCODING
    return frame


def _end_frame(snapshot: Snapshot) -> dict:
    return {
        "type": "snapshot_end",
        "data": {
            "snapshot_id": snapshot.snapshot_id,
            "revision": snapshot.revision,
            "digest": snapshot.digest,
            "total_bytes": snapshot.total_bytes,
            "total_chunks": len(snapshot.chunks),
        },
    }


def _encoded_size(frame: dict) -> int:
    return len(json.dumps(
        frame,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8"))


def _encode_and_digest(serialized: str) -> tuple[bytes, str]:
    payload = serialized.encode("utf-8")
    return payload, hashlib.sha256(payload).hexdigest()


def _split_chunks(payload: bytes) -> tuple[bytes, ...]:
    return tuple(
        payload[offset:offset + SNAPSHOT_CHUNK_BYTES]
        for offset in range(0, len(payload), SNAPSHOT_CHUNK_BYTES)
    )


def _encode_legacy_chunk(payload: bytes) -> str:
    return base64.b64encode(payload).decode("ascii")
