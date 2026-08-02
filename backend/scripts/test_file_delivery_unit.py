"""Unit tests for ``file_delivery`` — off-loop local-file reads.

Pure unit tests (no FastAPI/pydantic import chain) so they run identically
under the host ``.venv`` (Python 3.14) and the Docker test image (3.13).
Each test drives its own ``FileDeliveryHost`` and stops it in a finally so
the module-level singleton is never touched.
"""

from __future__ import annotations

import asyncio
import builtins
import tempfile
from pathlib import Path

import file_delivery


def _new_host() -> "file_delivery.FileDeliveryHost":
    return file_delivery.FileDeliveryHost()


def test_stream_range_reads_whole_file() -> None:
    host = _new_host()
    host.start()
    try:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "f.bin"
            payload = bytes(range(256))
            path.write_bytes(payload)

            async def run() -> bytes:
                out = bytearray()
                async for chunk in host.stream_range(path, 0, len(payload)):
                    out += chunk
                return bytes(out)

            assert asyncio.run(run()) == payload
    finally:
        host.stop()


def test_stream_range_honours_start_and_length_window() -> None:
    host = _new_host()
    host.start()
    try:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "f.txt"
            path.write_bytes(b"0123456789")

            async def run() -> bytes:
                out = bytearray()
                async for chunk in host.stream_range(path, 2, 4):
                    out += chunk
                return bytes(out)

            assert asyncio.run(run()) == b"2345"
    finally:
        host.stop()


def test_stream_range_breaks_early_at_eof_when_length_exceeds_file() -> None:
    host = _new_host()
    host.start()
    try:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "small.bin"
            path.write_bytes(b"onlyten0"[:10])

            async def run() -> bytes:
                out = bytearray()
                async for chunk in host.stream_range(path, 0, 10_000):
                    out += chunk
                return bytes(out)

            assert asyncio.run(run()) == b"onlyten0"[:10]
    finally:
        host.stop()


def test_stream_range_chunks_large_file_beyond_default_chunk_size() -> None:
    host = _new_host()
    host.start()
    try:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "big.bin"
            payload = (b"AB" * 100_000)  # 200_000 bytes -> > one 64 KiB chunk
            path.write_bytes(payload)

            async def run() -> tuple[bytes, list[int]]:
                collected = bytearray()
                sizes: list[int] = []
                async for chunk in host.stream_range(path, 0, len(payload)):
                    collected += chunk
                    sizes.append(len(chunk))
                return bytes(collected), sizes

            data, sizes = asyncio.run(run())
            assert data == payload
            # Multiple reads means the chunking loop iterated more than once.
            assert len(sizes) >= 3
            # Every chunk except possibly the last is exactly the chunk size.
            assert all(s == 64 * 1024 for s in sizes[:-1])
    finally:
        host.stop()


def test_stream_range_respects_custom_chunk_bytes() -> None:
    host = _new_host()
    host.start()
    try:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "c.txt"
            path.write_bytes(b"0123456789")

            async def run() -> list[int]:
                return [len(chunk) async for chunk in
                        host.stream_range(path, 0, 10, chunk_bytes=3)]

            assert asyncio.run(run()) == [3, 3, 3, 1]
    finally:
        host.stop()


def test_stream_range_closes_handle_even_on_seek_error() -> None:
    host = _new_host()
    host.start()
    try:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "f.bin"
            path.write_bytes(b"abc")

            closed = {"v": False}
            real_open = builtins.open

            def tracking_open(*args, **kwargs):  # type: ignore[no-untyped-def]
                handle = real_open(*args, **kwargs)
                original_close = handle.close

                def close():  # type: ignore[no-untyped-def]
                    closed["v"] = True
                    return original_close()

                handle.close = close  # type: ignore[method-assign]
                return handle

            builtins.open = tracking_open  # type: ignore[assignment]
            try:
                async def run() -> None:
                    async for _ in host.stream_range(path, -1, 3):
                        pass

                try:
                    asyncio.run(run())
                except (OSError, ValueError):
                    pass
                else:
                    raise AssertionError("expected error for negative seek offset")
            finally:
                builtins.open = real_open  # type: ignore[assignment]
            assert closed["v"] is True, "handle was not closed after seek error"
    finally:
        host.stop()


def test_exists_reports_true_and_false_off_loop() -> None:
    host = _new_host()
    host.start()
    try:
        with tempfile.TemporaryDirectory() as d:
            present = Path(d) / "there"
            present.write_bytes(b"x")
            missing = Path(d) / "nope"

            async def run() -> tuple[bool, bool]:
                return await host.exists(present), await host.exists(missing)

            assert asyncio.run(run()) == (True, False)
    finally:
        host.stop()


def test_host_starts_idempotently_and_stops_cleanly() -> None:
    host = _new_host()
    host.start()
    assert host.running
    host.start()  # idempotent — no second thread
    host.stop()
    assert not host.running
    host.stop()  # idempotent
