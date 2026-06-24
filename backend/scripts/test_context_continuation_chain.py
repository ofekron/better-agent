from __future__ import annotations

import os
import shutil
import sys
import tempfile

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-continuation-chain-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from session_manager import manager as session_manager  # noqa: E402
from continuation_flow import start_continuation_for  # noqa: E402


def test_session_manager_persists_continuation_chain() -> None:
    sess = session_manager.create(name="continuation", cwd="/tmp", model="m")
    sid = sess["id"]
    updated = session_manager.set_continuation_chain(
        sid,
        ["old-provider-sid", "", " next-provider-sid "],
    )
    assert updated is not None
    fresh = session_manager.get(sid)
    assert fresh is not None
    assert fresh.get("continuation_chain") == [
        "old-provider-sid",
        "next-provider-sid",
    ]


def test_start_continuation_for_same_session() -> None:
    sess = session_manager.create(name="continuation-flow", cwd="/tmp", model="m")
    sid = sess["id"]
    started = start_continuation_for(
        session_manager=session_manager,
        app_session_id=sid,
        prompt="continue the work",
        provider_kind="codex",
        old_provider_sid="old-provider-sid",
    )
    fresh = session_manager.get(sid)
    assert fresh is not None
    assert fresh.get("continuation_chain") == ["old-provider-sid"]
    assert started.continuation_chain == ["old-provider-sid"]
    assert started.chain_depth == 1
    assert "Better Agent session id: " + sid in started.prompt
    assert "Previous provider session ids: old-provider-sid" in started.prompt
    assert started.prompt.endswith("continue the work")


if __name__ == "__main__":
    try:
        test_session_manager_persists_continuation_chain()
        test_start_continuation_for_same_session()
        print("ALL TESTS PASSED")
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
