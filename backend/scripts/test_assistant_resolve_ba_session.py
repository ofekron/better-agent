"""Test assistant_ui.resolve_ba_session: native/agent session id -> BA session id.

search_in_native_sessions returns a provider-native session id; ask/delegate
need the Better Agent session id. resolve_ba_session bridges them via
session_manager.root_id_for. This locks the mapping + the not-found contract.

Run: cd backend && .venv/bin/python scripts/test_assistant_resolve_ba_session.py
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-resolve-ba-")

import assistant_ui  # noqa: E402

OK = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"


def _run() -> int:
    reverse = {"native-abc": "ba-root-1", "ba-root-1": "ba-root-1"}
    orig = assistant_ui.session_manager.root_id_for
    assistant_ui.session_manager.root_id_for = lambda sid: reverse.get(sid)
    results = []
    try:
        mapped = asyncio.run(assistant_ui.resolve_ba_session("native-abc"))
        ok = mapped == {"ba_session_id": "ba-root-1"}
        results.append(ok)
        print(f"{OK if ok else FAIL} native id maps to BA session (got {mapped})")

        selfmap = asyncio.run(assistant_ui.resolve_ba_session("ba-root-1"))
        ok = selfmap == {"ba_session_id": "ba-root-1"}
        results.append(ok)
        print(f"{OK if ok else FAIL} BA id resolves to itself (got {selfmap})")

        missing = asyncio.run(assistant_ui.resolve_ba_session("orphan-native"))
        ok = missing == {"ba_session_id": None}
        results.append(ok)
        print(f"{OK if ok else FAIL} unmapped native transcript -> None (got {missing})")

        empty = asyncio.run(assistant_ui.resolve_ba_session("  "))
        ok = empty == {"ba_session_id": None}
        results.append(ok)
        print(f"{OK if ok else FAIL} empty input -> None without lookup (got {empty})")

        # adopt_native_session: enumerate yields a claude session (native_id ==
        # file stem) and a codex session (native_id == DB thread id != the FTS
        # sid == rollout stem), so path-matching is the only accurate key.
        import native_import
        orig_enum, orig_imp = native_import.enumerate_native_sessions, native_import.import_session
        imported = []

        class _Sess:
            def __init__(self, nid, path):
                self.native_id = nid
                self.jsonl_path = path

        native_import.enumerate_native_sessions = lambda *a, **k: [
            _Sess("claude-abc", "/proj/claude-abc.jsonl"),
            _Sess("codex-thread-99", "/store/rollout-xyz.jsonl"),
        ]
        native_import.import_session = lambda sess, **k: (
            imported.append((sess.native_id, sess.jsonl_path)) or "ba-imported-1"
        )
        try:
            already = asyncio.run(assistant_ui.adopt_native_session("native-abc"))
            ok = already == {"ba_session_id": "ba-root-1"} and imported == []
            results.append(ok)
            print(f"{OK if ok else FAIL} adopt: already-BA returns id, no import (got {already}, imported={imported})")

            # Codex-style: sid (rollout stem) != native_id; match by PATH.
            by_path = asyncio.run(assistant_ui.adopt_native_session(
                "rollout-xyz", "/store/rollout-xyz.jsonl"))
            ok = by_path == {"ba_session_id": "ba-imported-1"} and imported == [("codex-thread-99", "/store/rollout-xyz.jsonl")]
            results.append(ok)
            print(f"{OK if ok else FAIL} adopt: path match handles sid != native_id (got {by_path}, imported={imported})")

            # Claude fallback: no path -> match by native_id.
            imported.clear()
            by_id = asyncio.run(assistant_ui.adopt_native_session("claude-abc"))
            ok = by_id == {"ba_session_id": "ba-imported-1"} and imported == [("claude-abc", "/proj/claude-abc.jsonl")]
            results.append(ok)
            print(f"{OK if ok else FAIL} adopt: native_id fallback when no path (got {by_id})")

            missing = asyncio.run(assistant_ui.adopt_native_session("nope", "/store/none.jsonl"))
            ok = missing.get("ba_session_id") is None and missing.get("error") == "native_session_not_found"
            results.append(ok)
            print(f"{OK if ok else FAIL} adopt: path not found -> error (got {missing})")
        finally:
            native_import.enumerate_native_sessions = orig_enum
            native_import.import_session = orig_imp
    finally:
        assistant_ui.session_manager.root_id_for = orig

    n = sum(1 for r in results if r)
    print(f"\n{n}/{len(results)} resolve-ba-session tests passed")
    shutil.rmtree(_TMP_HOME, ignore_errors=True)
    return 0 if n == len(results) else 1


if __name__ == "__main__":
    sys.exit(_run())
