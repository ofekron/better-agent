from __future__ import annotations

import os
import sys
import uuid
import asyncio

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-detail-cache-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from event_ingester import event_ingester  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402
import main as main_mod  # noqa: E402


PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


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


def test_non_render_event_keeps_detail_cache_valid() -> bool:
    sid, _msg_id = _fresh_session_with_render_event()
    key = main_mod._session_detail_response_cache_key_sync(
        sid, msg_limit=50, exchange_count=None,
    )
    if key is None:
        print("  failed to build detail cache key")
        return False

    event_ingester.ingest(
        sid,
        sid=sid,
        event_type="command_received",
        data={"uuid": str(uuid.uuid4()), "method": "PATCH"},
        source="test",
        msg_id=None,
    )

    if not main_mod._session_detail_cached_key_still_current(
        key, sid, msg_limit=50, exchange_count=None,
    ):
        print("  non-render event invalidated detail cache")
        return False
    return True


def test_render_event_invalidates_detail_cache() -> bool:
    sid, msg_id = _fresh_session_with_render_event()
    key = main_mod._session_detail_response_cache_key_sync(
        sid, msg_limit=50, exchange_count=None,
    )
    if key is None:
        print("  failed to build detail cache key")
        return False

    event_ingester.ingest(
        sid,
        sid=sid,
        event_type="agent_message",
        data={"type": "assistant", "uuid": str(uuid.uuid4())},
        source="test",
        msg_id=msg_id,
    )

    if main_mod._session_detail_cached_key_still_current(
        key, sid, msg_limit=50, exchange_count=None,
    ):
        print("  render event did not invalidate detail cache")
        return False
    return True


def test_route_populates_reusable_semantic_cache_key() -> bool:
    sid, _msg_id = _fresh_session_with_render_event()
    main_mod._session_detail_response_cache.clear()
    main_mod._session_detail_response_cache_latest.clear()

    asyncio.run(main_mod.get_session(sid, msg_limit=50, exchange_count=None))
    simple_key = (sid, 50, None)
    key = main_mod._session_detail_response_cache_latest.get(simple_key)
    if not isinstance(key, tuple) or len(key) != 4:
        print(f"  route stored unexpected cache key: {key!r}")
        return False
    if not main_mod._session_detail_cache_has(key):
        print("  route did not populate detail response cache")
        return False

    event_ingester.ingest(
        sid,
        sid=sid,
        event_type="command_received",
        data={"uuid": str(uuid.uuid4()), "method": "PATCH"},
        source="test",
        msg_id=None,
    )
    if not main_mod._session_detail_cached_key_still_current(
        key, sid, msg_limit=50, exchange_count=None,
    ):
        print("  route-populated key became stale after non-render event")
        return False

    asyncio.run(main_mod.get_session(sid, msg_limit=50, exchange_count=None))
    if main_mod._session_detail_response_cache_latest.get(simple_key) != key:
        print("  second route read replaced reusable cache key")
        return False
    return True


def main() -> int:
    tests = [
        ("non-render event keeps detail cache valid", test_non_render_event_keeps_detail_cache_valid),
        ("render event invalidates detail cache", test_render_event_invalidates_detail_cache),
        ("route populates reusable semantic cache key", test_route_populates_reusable_semantic_cache_key),
    ]
    ok = True
    for name, fn in tests:
        try:
            passed = fn()
        except Exception as exc:
            passed = False
            print(f"  exception: {exc!r}")
        print(f"{PASS if passed else FAIL} {name}")
        ok = ok and passed
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
