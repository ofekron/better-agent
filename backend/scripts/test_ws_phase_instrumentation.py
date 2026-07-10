from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path


BACKEND = Path(__file__).parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import ws_serialization


async def _run() -> None:
    original_dumps = json.dumps
    calls = 0

    def counted_dumps(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original_dumps(*args, **kwargs)

    json.dumps = counted_dumps
    try:
        values = [{"type": "messages_delta", "seq": index} for index in range(64)]
        frames = await asyncio.gather(*(ws_serialization.dumps_ws_json(v) for v in values))
    finally:
        json.dumps = original_dumps

    assert calls == len(values)
    assert [json.loads(frame)["seq"] for frame in frames] == list(range(len(values)))
    for frame in frames:
        assert frame.submit_at <= frame.start_at <= frame.done_at
        assert type(frame) is ws_serialization.SerializedWebSocketFrame


if __name__ == "__main__":
    asyncio.run(_run())
    print("PASS websocket phase instrumentation")
