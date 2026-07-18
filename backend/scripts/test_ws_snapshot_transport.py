from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import struct
import sys
import threading
from pathlib import Path
from unittest import mock


BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from ws_snapshot_transport import (  # noqa: E402
    MAX_WS_FRAME_BYTES,
    SNAPSHOT_CHUNK_BYTES,
    SNAPSHOT_THRESHOLD_BYTES,
    Snapshot,
    SnapshotCache,
    SnapshotTransport,
)
import ws_snapshot_transport  # noqa: E402


class Sender:
    def __init__(self) -> None:
        self.frames: list[dict] = []
        self.serialized = []

    async def __call__(self, frame: dict, serialized) -> bool:
        self.frames.append(frame)
        self.serialized.append(serialized)
        return True


class BinarySender:
    def __init__(self) -> None:
        self.frames: list[dict] = []
        self.binary_frames: list[bytes] = []

    async def send(self, frame: dict, _serialized) -> bool:
        self.frames.append(frame)
        return True

    async def send_binary(self, frame: bytes) -> bool:
        self.binary_frames.append(frame)
        return True


def event(size: int, *, event_type: str = "messages_replay") -> dict:
    data = {"app_session_id": "sid", "messages": [{"content": "x" * size}]}
    if event_type == "rewind_complete":
        data = {"session_id": "sid", "messages": [{"content": "x" * size}]}
    if event_type == "stub_invalidated":
        data = {"changes": [{
            "app_session_id": "sid",
            "msg_id": "msg",
            "stub": {"value": "x" * size},
        }]}
    return {"type": event_type, "data": data}


def ack(begin: dict, next_chunk: int) -> dict:
    data = begin["data"]
    return {
        "type": "snapshot_ack",
        "data": {
            "snapshot_id": data["snapshot_id"],
            "revision": data["revision"],
            "next_chunk": next_chunk,
        },
    }


async def drain(sender: Sender, transport: SnapshotTransport) -> bytes:
    begin = sender.frames[0]
    total = begin["data"]["total_chunks"]
    next_chunk = 0
    while next_chunk < total:
        assert await transport.acknowledge(ack(begin, next_chunk))
        chunks = [frame for frame in sender.frames if frame["type"] == "snapshot_chunk"]
        next_chunk = max(frame["data"]["index"] for frame in chunks) + 1
    assert await transport.acknowledge(ack(begin, total))
    chunks = sorted(
        (frame for frame in sender.frames if frame["type"] == "snapshot_chunk"),
        key=lambda frame: frame["data"]["index"],
    )
    return b"".join(base64.b64decode(frame["data"]["payload"]) for frame in chunks)


def test_small_frame_is_unchanged_and_prepared_once() -> None:
    async def run() -> None:
        sender = Sender()
        transport = SnapshotTransport(principal="user-a", send=sender)
        original = event(16)
        assert await transport.send_event(original)
        assert sender.frames == [original]
        assert sender.serialized[0] is not None

    asyncio.run(run())


def test_legacy_client_keeps_text_base64_transport() -> None:
    async def run() -> None:
        sender = Sender()
        transport = SnapshotTransport(principal="legacy-user", send=sender)
        assert await transport.send_event(event(SNAPSHOT_THRESHOLD_BYTES + 10))
        begin = sender.frames[0]
        assert "encoding" not in begin["data"]
        assert await transport.acknowledge(ack(begin, 0))
        chunks = [frame for frame in sender.frames if frame["type"] == "snapshot_chunk"]
        assert chunks
        assert all(isinstance(frame["data"]["payload"], str) for frame in chunks)

    asyncio.run(run())


def test_snapshot_is_immutable_and_digest_verified() -> None:
    async def run() -> None:
        sender = Sender()
        transport = SnapshotTransport(principal="user-a", send=sender)
        original = event(SNAPSHOT_THRESHOLD_BYTES + 10)
        assert await transport.send_event(original)
        original["data"]["messages"][0]["content"] = "mutated"
        payload = await drain(sender, transport)
        begin = sender.frames[0]["data"]
        assert hashlib.sha256(payload).hexdigest() == begin["digest"]
        parsed = json.loads(payload)
        assert parsed["data"]["messages"][0]["content"] != "mutated"
        assert sender.frames[-1]["type"] == "snapshot_end"

    asyncio.run(run())


def test_every_encoded_chunk_frame_is_bounded() -> None:
    async def run() -> None:
        sender = Sender()
        transport = SnapshotTransport(principal="user-a", send=sender)
        assert await transport.send_event(event(SNAPSHOT_CHUNK_BYTES * 3 + 17))
        await drain(sender, transport)
        for frame in sender.frames:
            encoded = json.dumps(
                frame, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
            ).encode("utf-8")
            assert len(encoded) <= MAX_WS_FRAME_BYTES

    asyncio.run(run())


def test_large_snapshot_emits_binary_v1_chunks_without_base64() -> None:
    async def run() -> None:
        sender = BinarySender()
        transport = SnapshotTransport(
            principal="user-a",
            send=sender.send,
            send_binary=sender.send_binary,
            binary=True,
        )
        original = event(SNAPSHOT_CHUNK_BYTES * 2 + 17)
        assert await transport.send_event(original)
        begin = sender.frames[0]
        assert begin["type"] == "snapshot_begin"
        assert begin["data"]["encoding"] == "binary-v1"
        total = begin["data"]["total_chunks"]
        next_chunk = 0
        while next_chunk < total:
            assert await transport.acknowledge(ack(begin, next_chunk))
            next_chunk = len(sender.binary_frames)
        assert await transport.acknowledge(ack(begin, total))
        assert all(frame["type"] != "snapshot_chunk" for frame in sender.frames)
        payload = b"".join(frame[32:] for frame in sender.binary_frames)
        assert json.loads(payload) == original

    asyncio.run(run())


def test_binary_header_is_exact_bounded_and_big_endian() -> None:
    async def run() -> None:
        sender = BinarySender()
        transport = SnapshotTransport(
            principal="user-a",
            send=sender.send,
            send_binary=sender.send_binary,
            binary=True,
        )
        assert await transport.send_event(event(SNAPSHOT_THRESHOLD_BYTES + 10))
        begin = sender.frames[0]
        assert await transport.acknowledge(ack(begin, 0))
        frame = sender.binary_frames[0]
        magic, version, kind, flags, raw_id, index, payload_length = struct.unpack(
            "!4sBBH16sII", frame[:32],
        )
        assert magic == b"BASN"
        assert (version, kind, flags, index) == (1, 1, 0, 0)
        assert raw_id.hex() == begin["data"]["snapshot_id"]
        assert payload_length == len(frame) - 32
        assert len(frame) <= MAX_WS_FRAME_BYTES

    asyncio.run(run())


def test_binary_cache_budget_counts_raw_bytes() -> None:
    async def run() -> None:
        cache = SnapshotCache(max_bytes=SNAPSHOT_THRESHOLD_BYTES * 2)
        sender = BinarySender()
        transport = SnapshotTransport(
            principal="user-a",
            send=sender.send,
            send_binary=sender.send_binary,
            binary=True,
            cache=cache,
        )
        assert await transport.send_event(event(SNAPSHOT_THRESHOLD_BYTES + 10))
        snapshot = cache.get(sender.frames[0]["data"]["snapshot_id"])
        assert snapshot is not None
        assert snapshot.retained_bytes == snapshot.payload_bytes
        assert all(isinstance(chunk, bytes) for chunk in snapshot.chunks)

    asyncio.run(run())


def test_ack_rejects_forward_gap_stale_and_malformed_values() -> None:
    async def run() -> None:
        sender = Sender()
        transport = SnapshotTransport(principal="user-a", send=sender)
        assert await transport.send_event(event(SNAPSHOT_THRESHOLD_BYTES + 10))
        begin = sender.frames[0]
        assert not await transport.acknowledge(ack(begin, 1))
        assert await transport.acknowledge(ack(begin, 0))
        assert not await transport.acknowledge(ack(begin, 3))
        malformed = ack(begin, 0)
        malformed["data"]["next_chunk"] = True
        assert not await transport.acknowledge(malformed)

    asyncio.run(run())


def test_resume_is_principal_bound_and_continues_cached_bytes() -> None:
    async def run() -> None:
        first_sender = Sender()
        first = SnapshotTransport(principal="user-a", send=first_sender)
        assert await first.send_event(event(SNAPSHOT_CHUNK_BYTES * 3))
        begin = first_sender.frames[0]
        assert await first.acknowledge(ack(begin, 0))
        resume = {
            "type": "snapshot_resume",
            "data": {
                "snapshot_id": begin["data"]["snapshot_id"],
                "revision": begin["data"]["revision"],
                "digest": begin["data"]["digest"],
                "next_chunk": 2,
            },
        }
        attacker_sender = Sender()
        attacker = SnapshotTransport(principal="user-b", send=attacker_sender)
        assert not await attacker.resume(resume)
        assert attacker_sender.frames == []

        resumed_sender = Sender()
        resumed = SnapshotTransport(principal="user-a", send=resumed_sender)
        assert await resumed.resume(resume)
        assert resumed_sender.frames[0]["type"] == "snapshot_begin"
        assert resumed_sender.frames[0]["data"]["resume_from"] == 2
        chunks = [f for f in resumed_sender.frames if f["type"] == "snapshot_chunk"]
        assert chunks[0]["data"]["index"] == 2

    asyncio.run(run())


def test_same_key_supersession_is_ordered_and_terminal() -> None:
    async def run() -> None:
        sender = Sender()
        transport = SnapshotTransport(principal="user-a", send=sender)
        assert await transport.send_event(event(SNAPSHOT_THRESHOLD_BYTES + 1))
        old_id = sender.frames[0]["data"]["snapshot_id"]
        assert await transport.send_event(event(SNAPSHOT_THRESHOLD_BYTES + 2))
        assert [frame["type"] for frame in sender.frames] == [
            "snapshot_begin", "snapshot_cancelled", "snapshot_begin",
        ]
        assert sender.frames[1]["data"] == {
            "snapshot_id": old_id,
            "revision": sender.frames[0]["data"]["revision"],
            "reason": "superseded",
        }

    asyncio.run(run())


def test_identical_snapshot_reuses_cached_chunks_and_authority_revision() -> None:
    async def run() -> None:
        original = event(SNAPSHOT_THRESHOLD_BYTES + 1)
        original["data"]["next_seq"] = 42
        first_sender = Sender()
        second_sender = Sender()
        first = SnapshotTransport(principal="same-user", send=first_sender)
        second = SnapshotTransport(principal="same-user", send=second_sender)
        assert await first.send_event(original)
        assert await second.send_event(original)
        first_begin = first_sender.frames[0]["data"]
        second_begin = second_sender.frames[0]["data"]
        assert first_begin["snapshot_id"] == second_begin["snapshot_id"]
        assert first_begin["revision"] == "seq:42"
        assert first_begin["digest"] == second_begin["digest"]

    asyncio.run(run())


def test_large_payload_preparation_runs_off_event_loop() -> None:
    async def run() -> None:
        event_loop_thread = threading.get_ident()
        observed: list[int] = []
        original_encode = ws_snapshot_transport._encode_and_digest
        original_chunks = ws_snapshot_transport._split_chunks

        def encode(serialized):
            observed.append(threading.get_ident())
            return original_encode(serialized)

        def chunks(payload):
            observed.append(threading.get_ident())
            return original_chunks(payload)

        sender = Sender()
        transport = SnapshotTransport(principal="off-loop-user", send=sender)
        with (
            mock.patch.object(ws_snapshot_transport, "_encode_and_digest", encode),
            mock.patch.object(ws_snapshot_transport, "_split_chunks", chunks),
        ):
            assert await transport.send_event(event(SNAPSHOT_THRESHOLD_BYTES + 1))
        assert len(observed) == 2
        assert all(thread_id != event_loop_thread for thread_id in observed)

    asyncio.run(run())


def test_cache_ttl_and_lru_are_byte_bounded() -> None:
    payload = b"12345"

    def snapshot(snapshot_id: str, created_at: float) -> Snapshot:
        digest = hashlib.sha256(payload).hexdigest()
        return Snapshot(
            snapshot_id=snapshot_id,
            principal="p",
            key=snapshot_id,
            event_type="messages_replay",
            revision=digest,
            digest=digest,
            payload_bytes=len(payload),
            chunks=(payload,),
            scope=(("sid", None),),
            refresh_id="c" * 32,
            created_at=created_at,
        )

    cache = SnapshotCache(ttl_seconds=10, max_bytes=9)
    first = snapshot("a" * 32, 0)
    second = snapshot("b" * 32, 1)
    cache.put(first, now=1)
    cache.put(second, now=1)
    assert cache.get(first.snapshot_id, now=1) is None
    assert cache.get(second.snapshot_id, now=12) is None


def test_global_budget_counts_unique_active_bytes_across_connections() -> None:
    async def run() -> None:
        original = event(SNAPSHOT_THRESHOLD_BYTES + 100)
        payload = json.dumps(
            original, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
        ).encode("utf-8")
        retained = len(payload)
        different = event(SNAPSHOT_THRESHOLD_BYTES + 101)
        different_payload = json.dumps(
            different, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
        ).encode("utf-8")
        different_retained = len(different_payload)
        budget = max(retained, different_retained)
        cache = SnapshotCache(max_bytes=budget)
        first_sender = Sender()
        shared_sender = Sender()
        blocked_sender = Sender()
        first = SnapshotTransport(
            principal="budget-user", send=first_sender, cache=cache,
        )
        shared = SnapshotTransport(
            principal="budget-user", send=shared_sender, cache=cache,
        )
        blocked = SnapshotTransport(
            principal="budget-user", send=blocked_sender, cache=cache,
        )
        assert await first.send_event(original)
        assert await shared.send_event(original)
        assert cache._bytes == retained
        with mock.patch.object(
            ws_snapshot_transport,
            "_split_chunks",
            side_effect=AssertionError("unadmittable snapshot must not encode chunks"),
        ):
            assert await blocked.send_event(different)
        assert blocked_sender.frames[-1]["type"] == "snapshot_refresh_required"
        assert blocked_sender.frames[-1]["data"]["reason"] == "overflow"
        assert cache._bytes <= budget
        assert cache._reserved_bytes == 0
        await first.close()
        await shared.close()
        retry_sender = Sender()
        retry = SnapshotTransport(
            principal="budget-user", send=retry_sender, cache=cache,
        )
        assert await retry.send_event(different)
        assert retry_sender.frames[0]["type"] == "snapshot_begin"
        assert cache._bytes <= budget
        await retry.close()
        await blocked.close()

    asyncio.run(run())


def test_binary_resume_starts_at_exact_cumulative_index() -> None:
    async def run() -> None:
        cache = SnapshotCache()
        first_sender = BinarySender()
        first = SnapshotTransport(
            principal="resume-user",
            send=first_sender.send,
            send_binary=first_sender.send_binary,
            binary=True,
            cache=cache,
        )
        assert await first.send_event(event(SNAPSHOT_CHUNK_BYTES * 3))
        begin = first_sender.frames[0]
        assert await first.acknowledge(ack(begin, 0))
        assert len(first_sender.binary_frames) == 2
        await first.close()

        resumed_sender = BinarySender()
        resumed = SnapshotTransport(
            principal="resume-user",
            send=resumed_sender.send,
            send_binary=resumed_sender.send_binary,
            binary=True,
            cache=cache,
        )
        assert await resumed.resume({
            "type": "snapshot_resume",
            "data": {
                "snapshot_id": begin["data"]["snapshot_id"],
                "revision": begin["data"]["revision"],
                "digest": begin["data"]["digest"],
                "next_chunk": 2,
            },
        })
        resumed_begin = resumed_sender.frames[0]
        assert resumed_begin["data"]["resume_from"] == 2
        assert resumed_begin["data"]["encoding"] == "binary-v1"
        assert resumed_sender.binary_frames
        _, _, _, _, _, first_index, _ = struct.unpack(
            "!4sBBH16sII", resumed_sender.binary_frames[0][:32],
        )
        assert first_index == 2

    asyncio.run(run())


def test_refresh_cancellation_is_terminal_against_concurrent_ack_pump() -> None:
    class BlockingChunkSender(Sender):
        def __init__(self) -> None:
            super().__init__()
            self.chunk_started = asyncio.Event()
            self.release_chunk = asyncio.Event()
            self.block_once = True

        async def __call__(self, frame: dict, serialized) -> bool:
            self.frames.append(frame)
            self.serialized.append(serialized)
            if frame["type"] == "snapshot_chunk" and self.block_once:
                self.block_once = False
                self.chunk_started.set()
                await self.release_chunk.wait()
            return True

    async def run() -> None:
        sender = BlockingChunkSender()

        async def refresh(_scope, _refresh_id) -> bool:
            return True

        transport = SnapshotTransport(
            principal="race-user",
            send=sender,
            refresh=refresh,
            cache=SnapshotCache(max_bytes=4 * 1024 * 1024),
        )
        assert await transport.send_event(event(SNAPSHOT_CHUNK_BYTES * 3))
        begin = sender.frames[0]["data"]
        ack_task = asyncio.create_task(transport.acknowledge(ack(sender.frames[0], 0)))
        await sender.chunk_started.wait()
        refresh_task = asyncio.create_task(transport.refresh({
            "type": "snapshot_refresh",
            "data": {
                "key": begin["key"],
                "event_type": begin["event_type"],
                "failed_revision": begin["revision"],
                "reason": "corrupt",
                "refresh_id": begin["refresh_id"],
            },
        }))
        await asyncio.sleep(0)
        assert not refresh_task.done()
        sender.release_chunk.set()
        assert await ack_task
        assert await refresh_task
        cancelled_index = next(
            index for index, frame in enumerate(sender.frames)
            if frame["type"] == "snapshot_cancelled"
        )
        assert all(
            frame["type"] not in {"snapshot_chunk", "snapshot_end"}
            for frame in sender.frames[cancelled_index + 1:]
        )
        assert not await transport.acknowledge(ack(sender.frames[0], 2))
        await transport.close()

    asyncio.run(run())


def test_stale_refresh_cannot_cancel_concurrent_new_same_key_transfer() -> None:
    class BlockingCancelSender(Sender):
        def __init__(self) -> None:
            super().__init__()
            self.cancel_started = asyncio.Event()
            self.release_cancel = asyncio.Event()
            self.block_once = True

        async def __call__(self, frame: dict, serialized) -> bool:
            self.frames.append(frame)
            self.serialized.append(serialized)
            if frame["type"] == "snapshot_cancelled" and self.block_once:
                self.block_once = False
                self.cancel_started.set()
                await self.release_cancel.wait()
            return True

    async def run() -> None:
        sender = BlockingCancelSender()
        refreshed = []

        async def refresh(scope, refresh_id) -> bool:
            refreshed.append((scope, refresh_id))
            return True

        transport = SnapshotTransport(
            principal="stale-refresh-user",
            send=sender,
            refresh=refresh,
            cache=SnapshotCache(max_bytes=4 * 1024 * 1024),
        )
        assert await transport.send_event(event(SNAPSHOT_THRESHOLD_BYTES + 1))
        old = sender.frames[0]["data"]
        stale_request = {
            "type": "snapshot_refresh",
            "data": {
                "key": old["key"],
                "event_type": old["event_type"],
                "failed_revision": old["revision"],
                "reason": "corrupt",
                "refresh_id": old["refresh_id"],
            },
        }
        replacement = event(SNAPSHOT_THRESHOLD_BYTES + 2)
        replacement["data"]["next_seq"] = 2
        new_send = asyncio.create_task(transport.send_event(replacement))
        await sender.cancel_started.wait()
        stale_refresh = asyncio.create_task(transport.refresh(stale_request))
        await asyncio.sleep(0)
        assert not stale_refresh.done()
        sender.release_cancel.set()
        assert await new_send
        assert not await stale_refresh
        new_begin = next(
            frame for frame in reversed(sender.frames)
            if frame["type"] == "snapshot_begin"
        )
        assert new_begin["data"]["revision"] == "seq:2"
        assert new_begin["data"]["snapshot_id"] in transport._active_by_id
        assert refreshed == []
        await transport.close()

    asyncio.run(run())


def test_stub_invalidations_use_independent_authoritative_identity_keys() -> None:
    async def run() -> None:
        sender = Sender()
        cache = SnapshotCache(max_bytes=4 * 1024 * 1024)
        transport = SnapshotTransport(
            principal="stub-user", send=sender, cache=cache,
        )
        first = event(SNAPSHOT_THRESHOLD_BYTES + 1, event_type="stub_invalidated")
        second = event(SNAPSHOT_THRESHOLD_BYTES + 1, event_type="stub_invalidated")
        second["data"]["changes"][0]["msg_id"] = "other-msg"
        assert await transport.send_event(first)
        assert await transport.send_event(second)
        begins = [frame for frame in sender.frames if frame["type"] == "snapshot_begin"]
        assert len(begins) == 2
        assert begins[0]["data"]["key"] != begins[1]["data"]["key"]
        assert not any(frame["type"] == "snapshot_cancelled" for frame in sender.frames)
        assert await transport.acknowledge(ack(begins[1], 0))
        assert await transport.acknowledge(ack(begins[0], 0))
        chunks = [frame for frame in sender.frames if frame["type"] == "snapshot_chunk"]
        assert {frame["data"]["snapshot_id"] for frame in chunks} == {
            begins[0]["data"]["snapshot_id"],
            begins[1]["data"]["snapshot_id"],
        }
        await transport.close()

    asyncio.run(run())


def test_refresh_is_server_owned_and_emits_current_authority() -> None:
    async def run() -> None:
        sender = Sender()
        refreshed = []

        async def refresh(scope, refresh_id) -> bool:
            refreshed.append((scope, refresh_id))
            return True

        transport = SnapshotTransport(
            principal="refresh-user",
            send=sender,
            refresh=refresh,
            cache=SnapshotCache(max_bytes=4 * 1024 * 1024),
        )
        assert await transport.send_event(event(SNAPSHOT_THRESHOLD_BYTES + 1))
        begin = sender.frames[0]["data"]
        request = {
            "type": "snapshot_refresh",
            "data": {
                "key": begin["key"],
                "event_type": begin["event_type"],
                "failed_revision": begin["revision"],
                "reason": "corrupt",
                "refresh_id": begin["refresh_id"],
            },
        }
        forged = json.loads(json.dumps(request))
        forged["data"]["failed_revision"] = "seq:999999"
        assert not await transport.refresh(forged)
        assert refreshed == []
        assert await transport.refresh(request)
        assert sender.frames[-1]["type"] == "snapshot_cancelled"
        assert refreshed == [((('sid', None),), begin["refresh_id"])]
        await transport.close()

    asyncio.run(run())


def test_over_16_mib_uses_bounded_refresh_required_not_begin() -> None:
    async def run() -> None:
        sender = Sender()
        refreshed = []

        async def refresh(scope, refresh_id) -> bool:
            refreshed.append((scope, refresh_id))
            return True

        transport = SnapshotTransport(
            principal="oversize-user", send=sender, refresh=refresh,
        )
        with mock.patch.object(
            ws_snapshot_transport,
            "SNAPSHOT_MAX_PAYLOAD_BYTES",
            SNAPSHOT_THRESHOLD_BYTES,
        ):
            assert await transport.send_event(event(SNAPSHOT_THRESHOLD_BYTES + 1))
        required = sender.frames[0]
        assert required["type"] == "snapshot_refresh_required"
        assert required["data"]["reason"] == "too_large"
        assert await transport.refresh({
            "type": "snapshot_refresh",
            "data": {
                "key": required["data"]["key"],
                "event_type": required["data"]["event_type"],
                "failed_revision": required["data"]["revision"],
                "reason": "too_large",
                "refresh_id": required["data"]["refresh_id"],
            },
        })
        assert refreshed == [(
            (('sid', None),),
            required["data"]["refresh_id"],
        )]

    asyncio.run(run())


if __name__ == "__main__":
    tests = [value for name, value in globals().items() if name.startswith("test_")]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
