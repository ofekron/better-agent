"""Owning entity for local file I/O served over HTTP.

Reading bytes off local disk is blocking work. Done inline on the
request loop it stalls every other in-flight request for the duration
of the read — and media streaming reads a whole file in 64 KiB steps,
so the stall is the length of the download, not of one syscall.

This host is that work's home: a dedicated thread with its own bounded
executor, so a slow disk or a large video starves neither the request
loop nor the process-wide default executor.

It owns execution, not policy. Path resolution, the extension
allowlist, and remote-node access stay with `node_op` and the preview
token; this host only moves already-authorised reads off the loop.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import AsyncIterator

from thread_loop_host import ThreadLoopHost

logger = logging.getLogger(__name__)

_CHUNK_BYTES = 64 * 1024


class FileDeliveryHost(ThreadLoopHost):
    """Runs blocking local-file reads on a thread of its own."""

    def __init__(self) -> None:
        super().__init__(name="file-delivery", executor_workers=4)

    async def stream_range(
        self,
        file_path: Path,
        start: int,
        length: int,
        chunk_bytes: int = _CHUNK_BYTES,
    ) -> AsyncIterator[bytes]:
        """Yield `length` bytes from `start`, reading off the loop.

        Every syscall — open, seek, read, close — is dispatched to this
        host's executor, so the request loop only ever awaits.
        """
        handle = await self.run_blocking(open, file_path, "rb")
        try:
            await self.run_blocking(handle.seek, start)
            remaining = length
            while remaining > 0:
                chunk = await self.run_blocking(
                    handle.read, min(chunk_bytes, remaining),
                )
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk
        finally:
            await self.run_blocking(handle.close)

    async def exists(self, file_path: Path) -> bool:
        return await self.run_blocking(file_path.exists)


host = FileDeliveryHost()
