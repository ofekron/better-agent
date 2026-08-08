from __future__ import annotations

import asyncio
import json
import os
import sys

import _test_home
_TMP_HOME = _test_home.isolate_installed("bc-test-command-received-middleware-", provider="claude")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from starlette.responses import Response  # noqa: E402

import event_journal  # noqa: E402
import main  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402
from _test_request import http_request  # noqa: E402


PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


async def _run_middleware(sid: str, payload: dict) -> tuple[dict, bytes]:
    """Drive `ingest_command_received` for a session-mutating PATCH and
    return (published event data, body bytes seen by the downstream
    handler)."""
    body_bytes = json.dumps(payload).encode("utf-8")
    request = http_request(
        f"/api/sessions/{sid}/seen", method="PATCH", body=body_bytes,
    )

    published: list[dict] = []
    original_publish_event = event_journal.publish_event

    async def fake_publish_event(**kwargs):
        published.append(kwargs)

    downstream_seen: list[bytes] = []

    async def call_next(req):
        downstream_seen.append(await req.body())
        return Response(status_code=200)

    event_journal.publish_event = fake_publish_event
    try:
        response = await main.ingest_command_received(request, call_next)
        assert response.status_code == 200
    finally:
        event_journal.publish_event = original_publish_event

    assert len(published) == 1, f"expected exactly one publish_event call, got {len(published)}"
    assert len(downstream_seen) == 1
    return published[0]["data"], downstream_seen[0]


def _run_with_to_thread_spy(sid: str, payload: dict) -> tuple[dict, bytes, list]:
    original_to_thread = asyncio.to_thread
    to_thread_calls: list[tuple] = []

    async def spy_to_thread(fn, *args, **kwargs):
        to_thread_calls.append((fn, args, kwargs))
        return await original_to_thread(fn, *args, **kwargs)

    asyncio.to_thread = spy_to_thread
    try:
        event_data, downstream_body = asyncio.run(_run_middleware(sid, payload))
    finally:
        asyncio.to_thread = original_to_thread
    return event_data, downstream_body, to_thread_calls


def test_command_received_parses_small_body_inline() -> None:
    sess = session_manager.create(
        name="command-received-mw-small", model="gpt-test", cwd="/tmp",
        orchestration_mode="native", source="cli",
    )
    sid = sess["id"]
    payload = {"seen_at": "2026-08-08T00:00:00Z", "nested": {"a": [1, 2, 3]}}
    body_size = len(json.dumps(payload).encode("utf-8"))
    assert body_size <= main._COMMAND_BODY_INLINE_PARSE_LIMIT_BYTES, (
        f"test fixture body ({body_size}B) must stay under the inline-parse threshold"
    )

    event_data, downstream_body, to_thread_calls = _run_with_to_thread_spy(sid, payload)

    # Parity: the durable event carries the exact parsed payload, and the
    # downstream handler still sees the original raw bytes (re-injection).
    assert event_data["payload"] == payload
    assert event_data["method"] == "PATCH"
    assert event_data["sid"] == sid
    assert downstream_body == json.dumps(payload).encode("utf-8")

    json_loads_calls = [c for c in to_thread_calls if c[0] is json.loads]
    assert not json_loads_calls, (
        "a body under the inline-parse threshold must be parsed on the loop, "
        "not hopped through asyncio.to_thread"
    )


def test_command_received_offloads_large_body_parse() -> None:
    sess = session_manager.create(
        name="command-received-mw-large", model="gpt-test", cwd="/tmp",
        orchestration_mode="native", source="cli",
    )
    sid = sess["id"]
    # Pad well past the threshold so the size check is unambiguous regardless
    # of JSON encoding overhead.
    padding = "x" * (main._COMMAND_BODY_INLINE_PARSE_LIMIT_BYTES + 4096)
    payload = {"seen_at": "2026-08-08T00:00:00Z", "padding": padding}
    body_size = len(json.dumps(payload).encode("utf-8"))
    assert body_size > main._COMMAND_BODY_INLINE_PARSE_LIMIT_BYTES, (
        f"test fixture body ({body_size}B) must exceed the inline-parse threshold"
    )

    event_data, downstream_body, to_thread_calls = _run_with_to_thread_spy(sid, payload)

    assert event_data["payload"] == payload
    assert event_data["method"] == "PATCH"
    assert event_data["sid"] == sid
    assert downstream_body == json.dumps(payload).encode("utf-8")

    json_loads_calls = [c for c in to_thread_calls if c[0] is json.loads]
    assert json_loads_calls, (
        "a body over the inline-parse threshold must be offloaded via asyncio.to_thread"
    )
    assert json_loads_calls[0][1][0] == json.dumps(payload).encode("utf-8")


def test_command_received_falls_back_on_malformed_json() -> None:
    sess = session_manager.create(
        name="command-received-mw-malformed", model="gpt-test", cwd="/tmp",
        orchestration_mode="native", source="cli",
    )
    sid = sess["id"]
    body_bytes = b"{not valid json"
    request = http_request(f"/api/sessions/{sid}/seen", method="PATCH", body=body_bytes)

    published: list[dict] = []
    original_publish_event = event_journal.publish_event

    async def fake_publish_event(**kwargs):
        published.append(kwargs)

    async def call_next(_req):
        return Response(status_code=200)

    event_journal.publish_event = fake_publish_event
    try:
        response = asyncio.run(main.ingest_command_received(request, call_next))
    finally:
        event_journal.publish_event = original_publish_event

    assert response.status_code == 200
    assert published[0]["data"]["payload"] == {"_raw": body_bytes.decode("utf-8")}


def test_command_received_skips_non_command_methods() -> None:
    request = http_request("/api/sessions/some-sid/seen", method="GET")
    calls: list[str] = []

    async def call_next(_req):
        calls.append("called")
        return Response(status_code=200)

    response = asyncio.run(main.ingest_command_received(request, call_next))
    assert response.status_code == 200
    assert calls == ["called"]


def test_middleware_source_gates_json_parse_by_body_size() -> None:
    source = (main.__file__ and open(main.__file__, encoding="utf-8").read())
    assert "_COMMAND_BODY_INLINE_PARSE_LIMIT_BYTES = 64 * 1024" in source
    start = source.index("async def ingest_command_received(")
    end = source.index("\ncoordinator = Coordinator()", start)
    body = source[start:end]
    assert "if len(body_bytes) <= _COMMAND_BODY_INLINE_PARSE_LIMIT_BYTES:" in body
    assert "payload = json.loads(body_bytes)" in body
    assert "payload = await asyncio.to_thread(json.loads, body_bytes)" in body


def main_() -> int:
    tests = [
        ("command_received parses small body inline", test_command_received_parses_small_body_inline),
        ("command_received offloads large body parse", test_command_received_offloads_large_body_parse),
        ("command_received falls back on malformed JSON", test_command_received_falls_back_on_malformed_json),
        ("command_received skips non-command methods", test_command_received_skips_non_command_methods),
        ("middleware source gates json parse by body size", test_middleware_source_gates_json_parse_by_body_size),
    ]
    failures = 0
    for name, fn in tests:
        try:
            fn()
        except Exception as exc:
            print(f"  exception: {exc!r}")
            print(f"{FAIL} {name}")
            failures += 1
        else:
            print(f"{PASS} {name}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main_())
