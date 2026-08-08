from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-detail-cache-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from event_ingester import event_ingester  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402
import main as main_mod  # noqa: E402
import session_detail_api  # noqa: E402
from _test_request import http_request  # noqa: E402


PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _response_json(value: object) -> dict:
    body = getattr(value, "body", None)
    if isinstance(body, bytes):
        return json.loads(body)
    if isinstance(value, dict):
        return value
    raise TypeError(f"unexpected detail response: {type(value).__name__}")


def _fresh_session_with_render_event() -> tuple[str, str]:
    sess = session_manager.create(
        name="detail-cache", model="gpt-test", cwd="/tmp",
        orchestration_mode="native", source="cli",
    )
    sid = sess["id"]
    msg_id = str(uuid.uuid4())
    session_manager.append_assistant_msg(sid, {
        "id": msg_id,
        "role": "assistant",
        "content": "",
        "events": [],
        "isStreaming": True,
    })
    event_ingester.ingest(
        sid,
        sid=sid,
        event_type="agent_message",
        data={"type": "assistant", "uuid": str(uuid.uuid4())},
        source="test",
        msg_id=msg_id,
    )
    return sid, msg_id


def test_non_render_event_keeps_detail_cache_valid() -> None:
    sid, _msg_id = _fresh_session_with_render_event()
    key = session_detail_api._session_detail_response_cache_key_sync(
        sid, msg_limit=50, exchange_count=None,
    )
    assert key is not None, "failed to build detail cache key"

    event_ingester.ingest(
        sid,
        sid=sid,
        event_type="command_received",
        data={"uuid": str(uuid.uuid4()), "method": "PATCH"},
        source="test",
        msg_id=None,
    )

    assert session_detail_api._session_detail_cached_key_still_current(
        key, sid, msg_limit=50, exchange_count=None,
    ), "non-render event invalidated detail cache"


def test_render_event_invalidates_detail_cache() -> None:
    sid, msg_id = _fresh_session_with_render_event()
    key = session_detail_api._session_detail_response_cache_key_sync(
        sid, msg_limit=50, exchange_count=None,
    )
    assert key is not None, "failed to build detail cache key"

    event_ingester.ingest(
        sid,
        sid=sid,
        event_type="agent_message",
        data={"type": "assistant", "uuid": str(uuid.uuid4())},
        source="test",
        msg_id=msg_id,
    )

    assert not session_detail_api._session_detail_cached_key_still_current(
        key, sid, msg_limit=50, exchange_count=None,
    ), "render event did not invalidate detail cache"


def test_route_populates_reusable_semantic_cache_key() -> None:
    sid, _msg_id = _fresh_session_with_render_event()
    session_detail_api._session_detail_response_cache.clear()
    session_detail_api._session_detail_response_cache_latest.clear()

    asyncio.run(session_detail_api.get_session(
            http_request(f"/sessions/{sid}"), sid,
            msg_limit=50, exchange_count=None,
        ))
    simple_key = (sid, 50, None)
    key = session_detail_api._session_detail_response_cache_latest.get(simple_key)
    assert isinstance(key, tuple) and len(key) == 4, f"route stored unexpected cache key: {key!r}"
    assert session_detail_api._session_detail_cache_get(key) is not None, "route did not populate detail response cache"

    event_ingester.ingest(
        sid,
        sid=sid,
        event_type="command_received",
        data={"uuid": str(uuid.uuid4()), "method": "PATCH"},
        source="test",
        msg_id=None,
    )
    assert session_detail_api._session_detail_cached_key_still_current(
        key, sid, msg_limit=50, exchange_count=None,
    ), "route-populated key became stale after non-render event"

    asyncio.run(session_detail_api.get_session(
            http_request(f"/sessions/{sid}"), sid,
            msg_limit=50, exchange_count=None,
        ))
    assert session_detail_api._session_detail_response_cache_latest.get(simple_key) == key, (
        "second route read replaced reusable cache key"
    )


def test_last_opened_invalidates_cached_detail_response() -> None:
    sid, _msg_id = _fresh_session_with_render_event()
    session_detail_api._session_detail_response_cache.clear()
    session_detail_api._session_detail_response_cache_latest.clear()

    before = _response_json(
        asyncio.run(session_detail_api.get_session(
            http_request(f"/sessions/{sid}"), sid,
            msg_limit=50, exchange_count=None,
        )),
    )
    assert before.get("last_opened_at") is None, f"fresh session unexpectedly opened: {before.get('last_opened_at')!r}"

    simple_key = (sid, 50, None)
    old_key = session_detail_api._session_detail_response_cache_latest.get(simple_key)
    assert isinstance(old_key, tuple), f"route stored unexpected cache key: {old_key!r}"

    opened_at = "2026-07-22T08:01:28.643495"
    session_manager.set_last_opened_at(sid, opened_at, return_session=False)
    assert not session_detail_api._session_detail_cached_key_still_current(
        old_key, sid, msg_limit=50, exchange_count=None,
    ), "last-opened mutation did not invalidate detail cache"

    after = _response_json(
        asyncio.run(session_detail_api.get_session(
            http_request(f"/sessions/{sid}"), sid,
            msg_limit=50, exchange_count=None,
        )),
    )
    assert after.get("last_opened_at") == opened_at, (
        f"detail response returned stale last_opened_at: {after.get('last_opened_at')!r}"
    )


def main() -> int:
    tests = [
        ("non-render event keeps detail cache valid", test_non_render_event_keeps_detail_cache_valid),
        ("render event invalidates detail cache", test_render_event_invalidates_detail_cache),
        ("route populates reusable semantic cache key", test_route_populates_reusable_semantic_cache_key),
        ("last-opened invalidates cached detail response", test_last_opened_invalidates_cached_detail_response),
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
    raise SystemExit(main())
