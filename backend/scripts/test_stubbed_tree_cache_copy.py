"""Regression: stubbed tree cache hits return isolated response copies."""

from __future__ import annotations

import os
import shutil
import sys

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-stub-tree-copy-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from session_manager import manager as session_manager  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def test_stubbed_tree_cache_hit_is_isolated() -> bool:
    sess = session_manager.create(
        name="cache-copy", model="sonnet", cwd="/tmp",
        orchestration_mode="manager", source="cli",
    )
    sid = sess["id"]
    session_manager.append_user_msg(sid, {
        "id": "u1",
        "role": "user",
        "content": "hello",
        "events": [],
        "isStreaming": False,
    })
    first = session_manager.get_root_tree_stubbed(sid)
    if not first or not first.get("messages"):
        return False
    first["messages"][0]["content"] = "caller mutation"
    first["messages"].append({"id": "caller-added", "role": "user"})
    second = session_manager.get_root_tree_stubbed(sid)
    if any(m.get("id") == "caller-added" for m in second.get("messages") or []):
        print("cached tree kept caller-added message")
        return False
    if second["messages"][0].get("content") == "caller mutation":
        print("cached tree kept caller-mutated content")
        return False
    return True


def main() -> int:
    try:
        ok = test_stubbed_tree_cache_hit_is_isolated()
        print(f"{PASS if ok else FAIL} stubbed tree cache hit is isolated")
        return 0 if ok else 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
