from __future__ import annotations

import struct

import pytest

from ws_snapshot_binary import (
    SNAPSHOT_BINARY_ENCODING,
    SNAPSHOT_BINARY_SUBPROTOCOL,
    SNAPSHOT_CHUNK_BYTES,
    _HEADER,
    encode_snapshot_chunk,
)

_VALID_ID = "0123456789abcdef0123456789abcdef"


class TestConstants:
    def test_subprotocol_value(self) -> None:
        assert SNAPSHOT_BINARY_SUBPROTOCOL == "better-agent.snapshot.binary-v1"

    def test_encoding_value(self) -> None:
        assert SNAPSHOT_BINARY_ENCODING == "binary-v1"

    def test_chunk_bytes_value(self) -> None:
        assert SNAPSHOT_CHUNK_BYTES == 180 * 1024


class TestEncodeSnapshotChunkValid:
    def _decode(self, frame: bytes) -> dict:
        magic, ver, enc, flags, sid_bytes, index, length = _HEADER.unpack(frame[: _HEADER.size])
        payload = frame[_HEADER.size :]
        return {
            "magic": magic,
            "version": ver,
            "encoding": enc,
            "flags": flags,
            "snapshot_id": sid_bytes.hex(),
            "index": index,
            "length": length,
            "payload": payload,
        }

    def test_round_trip_minimal(self) -> None:
        payload = b"hello"
        frame = encode_snapshot_chunk(_VALID_ID, 0, payload)
        decoded = self._decode(frame)
        assert decoded["magic"] == b"BASN"
        assert decoded["version"] == 1
        assert decoded["encoding"] == 1
        assert decoded["flags"] == 0
        assert decoded["snapshot_id"] == _VALID_ID
        assert decoded["index"] == 0
        assert decoded["length"] == 5
        assert decoded["payload"] == payload

    def test_max_index(self) -> None:
        frame = encode_snapshot_chunk(_VALID_ID, 0xFFFFFFFF, b"x")
        assert self._decode(frame)["index"] == 0xFFFFFFFF

    def test_max_payload_size(self) -> None:
        payload = b"x" * SNAPSHOT_CHUNK_BYTES
        frame = encode_snapshot_chunk(_VALID_ID, 1, payload)
        assert self._decode(frame)["length"] == SNAPSHOT_CHUNK_BYTES


class TestEncodeSnapshotChunkInvalid:
    def test_id_wrong_length(self) -> None:
        with pytest.raises(ValueError, match="snapshot id"):
            encode_snapshot_chunk("ab", 0, b"x")

    def test_id_non_hex(self) -> None:
        bad = "g" * 32
        with pytest.raises(ValueError, match="snapshot id"):
            encode_snapshot_chunk(bad, 0, b"x")

    def test_index_bool_rejected(self) -> None:
        with pytest.raises(ValueError, match="index"):
            encode_snapshot_chunk(_VALID_ID, True, b"x")  # type: ignore[arg-type]

    def test_index_non_int_rejected(self) -> None:
        with pytest.raises(ValueError, match="index"):
            encode_snapshot_chunk(_VALID_ID, "5", b"x")  # type: ignore[arg-type]

    def test_index_negative_rejected(self) -> None:
        with pytest.raises(ValueError, match="index"):
            encode_snapshot_chunk(_VALID_ID, -1, b"x")

    def test_index_overflow_rejected(self) -> None:
        with pytest.raises(ValueError, match="index"):
            encode_snapshot_chunk(_VALID_ID, 0x100000000, b"x")

    def test_empty_payload_rejected(self) -> None:
        with pytest.raises(ValueError, match="payload"):
            encode_snapshot_chunk(_VALID_ID, 0, b"")

    def test_oversized_payload_rejected(self) -> None:
        with pytest.raises(ValueError, match="payload"):
            encode_snapshot_chunk(_VALID_ID, 0, b"x" * (SNAPSHOT_CHUNK_BYTES + 1))


def test_header_layout_invariants() -> None:
    # 4 + 1 + 1 + 2 + 16 + 4 + 4 = 32 (network order, no padding)
    assert _HEADER.format == "!4sBBH16sII"
    assert _HEADER.size == 32
    assert struct.calcsize("!4sBBH16sII") == 32
