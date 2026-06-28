"""Repro / regression for: a stray non-session JSON file in the sessions
dir (e.g. a leaked `git-last.json` with no `id`) must not crash
`iter_all_sessions()` — which is walked by the startup adv-sync overlay
recovery task. Before the fix, `_index_tree(root)` did `root["id"]` and
raised KeyError, aborting the walk and failing the startup task."""
from __future__ import annotations

import os
import sys
import json

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-iter-all-sessions-stray-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import session_store  # noqa: E402
from paths import ba_home  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def main() -> int:
    sessions_dir = ba_home() / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    # 1) One real session file (valid shape, has "id").
    real_id = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    session_store.write_session_full({
        "id": real_id,
        "title": "real session",
        "cwd": "/tmp/real",
        "messages": [],
        "forks": [],
    })

    # 2) A stray non-session JSON file that the sidecar filter does NOT
    #    exclude (no .summary.json/.drafts.json/.fork-index.json suffix).
    #    Mirrors the on-disk `git-last.json` that triggered the startup
    #    KeyError: a dict that simply has no "id" key.
    (sessions_dir / "git-last.json").write_text(
        json.dumps({"cwd": "/tmp/x", "messages": []}), encoding="utf-8"
    )

    try:
        yielded = [s for s in session_store.iter_all_sessions()]
    except Exception as exc:  # noqa: BLE001
        print(f"{FAIL}: iter_all_sessions raised {type(exc).__name__}: {exc}")
        return 1

    ids = {s.get("id") for s in yielded}
    if real_id not in ids:
        print(f"{FAIL}: real session not yielded; ids={ids}")
        return 1
    if any(_id is None for _id in ids):
        print(f"{FAIL}: a non-session (no id) was yielded; ids={ids}")
        return 1

    print(f"{PASS}: iter_all_sessions yielded {len(yielded)} session(s), "
          f"skipped stray git-last.json; ids={sorted(ids)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
