"""Per-session recall: chunking, cache-by-count, cosine ordering, scoping.
Stubs the embedder (no 1.2GB model load) and session_manager.

Run: python backend/scripts/test_session_recall.py
"""

import os
import sys
import tempfile
from pathlib import Path

import _test_home
_TMP = _test_home.isolate("bc-recalltest-")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
import session_recall  # noqa: E402

_FAILURES: list[str] = []


def check(cond: bool, msg: str):
    if not cond:
        _FAILURES.append(msg)
        print(f"  FAIL: {msg}")
    else:
        print(f"  ok:   {msg}")


# Deterministic fake embedder: map known texts to fixed unit vectors so we
# can assert cosine ordering without the real model.
_VECS = {
    "apple": np.array([1.0, 0.0, 0.0], dtype=np.float32),
    "apple pie recipe": np.array([0.9, 0.1, 0.0], dtype=np.float32),
    "banana": np.array([0.0, 1.0, 0.0], dtype=np.float32),
    "carrot": np.array([0.0, 0.0, 1.0], dtype=np.float32),
}


def _fake_embed(texts):
    rows = []
    for t in texts:
        v = _VECS.get(t.strip(), np.array([0.3, 0.3, 0.3], dtype=np.float32))
        rows.append(v / (np.linalg.norm(v) + 1e-9))
    return np.asarray(rows, dtype=np.float32)


_BUILD_CALLS = {"n": 0}
_REAL_EMBED = session_recall.embed


def _counting_embed(texts):
    _BUILD_CALLS["n"] += 1
    return _fake_embed(texts)


def main():
    session_recall.embed = _counting_embed  # type: ignore

    msgs = [
        {"role": "user", "content": "apple"},
        {"role": "assistant", "content": "banana"},
        {"role": "user", "content": "carrot"},
        {"role": "system", "content": "ignored"},   # non-user/assistant → skipped
        {"role": "assistant", "content": ""},         # empty → skipped
    ]
    fake_sessions = {"s1": {"messages": msgs}}
    session_recall.session_manager.manager.get = (  # type: ignore
        lambda sid: fake_sessions.get(sid)
    )

    # build_index chunks only user/assistant non-empty messages.
    n = session_recall.build_index("s1")
    check(n == 3, "build_index indexes 3 chunks (skips system + empty)")

    # cache by message_count: rebuild with same count is a no-op (no re-embed).
    before = _BUILD_CALLS["n"]
    session_recall.build_index("s1")
    check(_BUILD_CALLS["n"] == before, "rebuild with same count reuses cache")

    # growth triggers re-embed.
    fake_sessions["s1"]["messages"].append({"role": "user", "content": "apple pie recipe"})
    session_recall.build_index("s1")
    check(_BUILD_CALLS["n"] == before + 1, "rebuild after growth re-embeds")

    # recall ranks by cosine: query 'apple' → 'apple' then 'apple pie recipe'.
    res = session_recall.recall("s1", "apple", k=2)
    check([r["text"] for r in res] == ["apple", "apple pie recipe"],
          "recall ranks by cosine similarity")
    check(res[0]["score"] >= res[1]["score"], "recall scores descending")
    check(res[0]["role"] == "user" and "message_index" in res[0],
          "recall results carry role + message_index")

    # scoping: unknown session / no index → empty (opt-in per delegation).
    check(session_recall.recall("nope", "apple") == [], "recall on unindexed session is empty")
    check(session_recall.recall("s1", "") == [], "empty query → empty")

    # drop clears the index.
    session_recall.drop("s1")
    check(session_recall.recall("s1", "apple") == [], "drop clears the index")

    session_recall.embed = _REAL_EMBED  # type: ignore
    from shutil import rmtree
    rmtree(_TMP, ignore_errors=True)
    if _FAILURES:
        print(f"\n{len(_FAILURES)} FAILURE(S)")
        sys.exit(1)
    print("\nALL PASS")


if __name__ == "__main__":
    main()
