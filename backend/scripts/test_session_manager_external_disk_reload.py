from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-external-reload-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import session_store  # noqa: E402
import session_manager as session_manager_mod  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402


def main() -> int:
    try:
        sess = session_manager.create(
            name="external reload",
            model="sonnet",
            cwd="/tmp/external-reload",
            orchestration_mode="native",
            source="test",
        )
        sid = sess["id"]

        cached = session_manager.get(sid)
        assert cached is not None
        assert cached["messages"] == []

        path = session_store._session_path(sid)
        real_fingerprint = session_store.session_file_fingerprint
        calls = {"n": 0}

        def counting_fingerprint(root_id: str):
            calls["n"] += 1
            return real_fingerprint(root_id)

        session_store.session_file_fingerprint = counting_fingerprint
        try:
            for _ in range(20):
                assert session_manager.get(sid) is not None
            assert calls["n"] <= 1, calls["n"]
        finally:
            session_store.session_file_fingerprint = real_fingerprint

        external = json.loads(path.read_text(encoding="utf-8"))
        external["messages"] = [
            {
                "id": "user-1",
                "role": "user",
                "content": "external user",
                "seq": 1,
                "timestamp": "2026-06-14T00:00:00",
            },
            {
                "id": "assistant-1",
                "role": "assistant",
                "content": "external assistant",
                "seq": 2,
                "timestamp": "2026-06-14T00:00:01",
            },
        ]
        external["next_seq"] = 3
        external["updated_at"] = "2026-06-14T00:00:02"
        time.sleep(0.01)
        path.write_text(json.dumps(external), encoding="utf-8")
        session_manager._root_file_checked_at[
            sid
        ] = time.monotonic() - session_manager_mod.EXTERNAL_RELOAD_POLL_INTERVAL_S

        fresh = session_manager.get(sid)
        assert fresh is not None
        assert [m["content"] for m in fresh["messages"]] == [
            "external user",
            "external assistant",
        ]
        print("PASS external session file update invalidates cached root")
        return 0
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
