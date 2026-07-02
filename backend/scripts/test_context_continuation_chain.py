from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

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
    native_path = Path(_TMP_HOME) / "native" / "old-provider-sid.jsonl"
    run_dir = Path(_TMP_HOME) / "runs" / "run-old-provider-sid"
    run_dir.mkdir(parents=True)
    (run_dir / "backend_state.json").write_text(
        json.dumps({
            "session_id": "old-provider-sid",
            "jsonl_path": str(native_path),
        }),
        encoding="utf-8",
    )
    started = start_continuation_for(
        session_manager=session_manager,
        app_session_id=sid,
        prompt="continue the work",
        old_provider_sid="old-provider-sid",
    )
    fresh = session_manager.get(sid)
    assert fresh is not None
    assert fresh.get("continuation_chain") == ["old-provider-sid"]
    assert started.continuation_chain == ["old-provider-sid"]
    assert started.chain_depth == 1
    assert "Better Agent session id: " + sid in started.prompt
    expected_session_path = Path(_TMP_HOME).resolve() / "sessions" / f"{sid}.json"
    assert f"Better Agent session file path: {expected_session_path}" in started.prompt
    assert "Previous provider session ids: old-provider-sid" in started.prompt
    assert f"- old-provider-sid: {native_path}" in started.prompt
    assert "query_provider_native_transcript_index" in started.prompt
    assert "native_element_fts.sid" in started.prompt
    assert "agent_session_id" in started.prompt
    assert "supervisor_agent_session_id" in started.prompt
    assert "already native ids" in started.prompt
    assert started.prompt.endswith("continue the work")


if __name__ == "__main__":
    try:
        test_session_manager_persists_continuation_chain()
        test_start_continuation_for_same_session()
        print("ALL TESTS PASSED")
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
