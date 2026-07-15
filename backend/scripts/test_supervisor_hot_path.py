from __future__ import annotations

import inspect
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from orchs.supervisor import replay_pending_verdict  # noqa: E402
from orchs.supervisor import maybe_run_verdict_loop  # noqa: E402
from orchs.supervisor._verdict import request_verdict, request_review  # noqa: E402


def test_pending_verdict_replay_uses_field_snapshot() -> None:
    source = inspect.getsource(replay_pending_verdict)
    assert "session_manager.get_fields(" in source
    assert "session_manager.get(app_session_id)" not in source
    assert "pending_supervisor_verdict" in source
    assert "supervisor_enabled" in source


def test_verdict_loop_read_sites_use_lite_reads() -> None:
    # The verdict loop runs on the event loop; its read-only sites extract
    # message text/role + metadata and never touch msg.events, so they must
    # use get_lite (same data minus events, ~100x cheaper) instead of full
    # get() which deepcopies the whole events-laden tree per iteration and
    # blocks the loop on large sessions.
    verdict_src = inspect.getsource(request_verdict)
    assert "session_manager.get_lite(" in verdict_src
    assert "session_manager.get(app_session_id)" not in verdict_src

    # request_review's verdict-text extraction read is also event-free.
    review_src = inspect.getsource(request_review)
    assert "session_manager.get_lite(app_session_id)" in review_src

    loop_src = inspect.getsource(maybe_run_verdict_loop)
    # The supervisor_enabled re-check gate must be a lite read.
    assert "session_manager.get_lite(app_session_id)" in loop_src


if __name__ == "__main__":
    test_pending_verdict_replay_uses_field_snapshot()
    test_verdict_loop_read_sites_use_lite_reads()
    print("ok")
