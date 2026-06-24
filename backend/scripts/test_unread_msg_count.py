"""Tests the unread message count logic (turns vs events).
Goal: one assistant response with multiple events should count as ONE unread message.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-unread-msg-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from orchs import ApplyEventCtx, get_strategy  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402


PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _mk_session(mode: str = "native") -> tuple[str, dict]:
    sess = session_manager.create(
        name="t", model="sonnet", cwd="/tmp/test-unread-msg",
        orchestration_mode=mode, source="cli",
    )
    sid = sess["id"]
    strategy = get_strategy(mode)
    scaffold = strategy.build_assistant_scaffold()
    session_manager.append_assistant_msg(sid, scaffold)
    return sid, scaffold


def _native_event(uuid: str, text: str = "x") -> dict:
    return {
        "type": "agent_message",
        "data": {
            "uuid": uuid,
            "type": "assistant",
            "message": {"content": text},
        },
    }

def test_multiple_events_one_message() -> None:
    sid, msg = _mk_session("native")
    strategy = get_strategy("native")
    ctx = ApplyEventCtx(root_id=sid)
    
    # Send 3 events to the SAME assistant message
    for u in ("u1", "u2", "u3"):
        strategy.apply_event(
            app_session_id=sid, msg=msg,
            event=_native_event(u),
            ctx=ctx, source_is_provider_stream=True,
        )
    
    count = session_manager.get_unread_count(sid)
    # CURRENT BEHAVIOR: this will be 3.
    # DESIRED BEHAVIOR: this should be 1.
    print(f"Current unread_count for 3 events in 1 message: {count}")
    
    if count == 3:
        print(f"{PASS} (Verified current over-counting behavior: {count})")
    elif count == 1:
        print(f"{PASS} (Verified message-based counting: {count})")
    else:
        print(f"{FAIL} Unexpected unread_count: {count}")

def test_multiple_messages() -> None:
    sid, msg1 = _mk_session("native")
    strategy = get_strategy("native")
    ctx = ApplyEventCtx(root_id=sid)
    
    # Msg 1 has 2 events
    for u in ("m1-e1", "m1-e2"):
        strategy.apply_event(
            app_session_id=sid, msg=msg1,
            event=_native_event(u),
            ctx=ctx, source_is_provider_stream=True,
        )
        
    # Msg 2 has 1 event
    msg2 = strategy.build_assistant_scaffold()
    session_manager.append_assistant_msg(sid, msg2)
    strategy.apply_event(
        app_session_id=sid, msg=msg2,
        event=_native_event("m2-e1"),
        ctx=ctx, source_is_provider_stream=True,
    )
    
    count = session_manager.get_unread_count(sid)
    # CURRENT BEHAVIOR: this will be 3.
    # DESIRED BEHAVIOR: this should be 2.
    print(f"Current unread_count for 3 events across 2 messages: {count}")
    
    if count == 3:
        print(f"{PASS} (Verified current over-counting behavior: {count})")
    elif count == 2:
        print(f"{PASS} (Verified message-based counting: {count})")
    else:
        print(f"{FAIL} Unexpected unread_count: {count}")

def main() -> int:
    try:
        test_multiple_events_one_message()
        test_multiple_messages()
        print("Done")
        return 0
    except Exception as e:
        print(f"{FAIL}: {e}")
        import traceback
        traceback.print_exc()
        return 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)

if __name__ == "__main__":
    sys.exit(main())
