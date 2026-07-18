from __future__ import annotations

import struct


SNAPSHOT_BINARY_SUBPROTOCOL = "better-agent.snapshot.binary-v1"
SNAPSHOT_BINARY_ENCODING = "binary-v1"
SNAPSHOT_CHUNK_BYTES = 180 * 1024
_HEADER = struct.Struct("!4sBBH16sII")


def encode_snapshot_chunk(snapshot_id: str, index: int, payload: bytes) -> bytes:
    if (
        len(snapshot_id) != 32
        or any(ch not in "0123456789abcdef" for ch in snapshot_id)
    ):
        raise ValueError("invalid snapshot id")
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index <= 0xFFFFFFFF:
        raise ValueError("invalid snapshot chunk index")
    if not payload or len(payload) > SNAPSHOT_CHUNK_BYTES:
        raise ValueError("invalid snapshot chunk payload")
    return _HEADER.pack(
        b"BASN",
        1,
        1,
        0,
        bytes.fromhex(snapshot_id),
        index,
        len(payload),
    ) + payload
