"""TCP+secret transport for the mcp_prewarm daemon, used on Windows (no
usable unix domain sockets) or when a test forces it via
`ensure_daemon_ready(transport="tcp")` to exercise the identical code
path on macOS/Linux.

Windows named pipes were considered and rejected: Python's asyncio
Proactor event loop only exposes a low-level pipe transport/protocol
pair for named pipes, not the (StreamReader, StreamWriter)-shaped
object `daemon_process.py`'s per-connection MCP session isolation is
already built around -- reusing named pipes would mean a second,
divergent read/write plumbing path per platform instead of one shared
proxy loop. Loopback TCP gives the same stream shape anyio already
provides for unix sockets, at the cost of needing an explicit
per-daemon-instance connection secret, since a loopback port carries
no filesystem-permission-based peer isolation the way a 0600 unix
socket does.

Stdlib-only (no anyio/mcp import) so `stub.py` can keep importing this
module without paying the interpreter-import cost the whole prewarm
subsystem exists to avoid.
"""

from __future__ import annotations

import hmac
import secrets as _secrets
import struct
from typing import Awaitable, Callable

_SECRET_FRAME_MAX_BYTES = 4096


def generate_connect_secret() -> str:
    return _secrets.token_urlsafe(32)


def encode_secret_frame(secret: str) -> bytes:
    # A fixed-width 4-byte length prefix (vs. newline-delimited) keeps
    # the handshake unambiguous from the JSON-RPC framing that follows
    # on the same stream once the secret is accepted -- the two never
    # need to agree on a shared delimiter convention.
    data = secret.encode("utf-8")
    return struct.pack("!I", len(data)) + data


def secrets_match(expected: str, received: bytes) -> bool:
    # Constant-time compare: a length- or content-dependent short
    # circuit here would leak the secret one byte at a time to a local
    # attacker timing repeated connection attempts.
    return hmac.compare_digest(expected.encode("utf-8"), received)


async def read_secret_frame(receive: Callable[[int], Awaitable[bytes]]) -> bytes:
    """`receive` is an async callable(max_bytes) -> bytes, e.g. an
    anyio SocketStream's bound `.receive` method. Generic over the
    stream type so this same function is exercised directly in tests
    without needing a real anyio stream."""
    header = await _read_exact(receive, 4)
    (length,) = struct.unpack("!I", header)
    if length <= 0 or length > _SECRET_FRAME_MAX_BYTES:
        raise ValueError("invalid mcp_prewarm secret frame length")
    return await _read_exact(receive, length)


async def _read_exact(receive: Callable[[int], Awaitable[bytes]], size: int) -> bytes:
    buf = bytearray()
    while len(buf) < size:
        chunk = await receive(size - len(buf))
        if not chunk:
            raise ConnectionError("mcp_prewarm: connection closed during secret handshake")
        buf += chunk
    return bytes(buf)
