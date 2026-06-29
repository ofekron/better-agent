from __future__ import annotations

import inspect
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from orchs.supervisor import replay_pending_verdict  # noqa: E402


def test_pending_verdict_replay_uses_field_snapshot() -> None:
    source = inspect.getsource(replay_pending_verdict)
    assert "session_manager.get_fields(" in source
    assert "session_manager.get(app_session_id)" not in source
    assert "pending_supervisor_verdict" in source
    assert "supervisor_enabled" in source


if __name__ == "__main__":
    test_pending_verdict_replay_uses_field_snapshot()
    print("ok")
