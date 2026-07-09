"""Regression test: Claude orphan-line ingest must NOT read
`orchestration_mode` via `session_manager.get_lite`.

RCA (lag-watchdog loop-block, escalating to a 17s main-loop freeze on
backend startup): `provider_claude.ClaudeProvider._ingest_orphan_line`
read `orchestration_mode` via `session_manager.get_lite(app_sid)`.
`get_lite` takes the per-root lock (and rehydrates a cold root under
it); during startup run-recovery that lock is held for many seconds,
and because this runs on the synchronous tailer dispatch path ON the
main event loop, it froze every concurrent WS / request / session.
The faulthandler dumps pinned the block at `session_manager.get_lite`
(line 2164, the `with _lock_for_root(rid)` line) for four escalating
stalls (1.5 -> 6.6 -> 11.8 -> 17.0s).

Fix: pass the spawn-time `rs.mode` through instead of re-reading it.
`get_strategy` returns one cached strategy for all modes (the mode arg
only validates/normalizes), so the lookup was both blocking and
unnecessary. Codex/Gemini providers already used `rs.mode` here — this
restores parity.

This test locks the behavioral gate: `_ingest_orphan_line` must NOT
call `session_manager.get_lite`. Before the fix it did (calls=1 →
FAIL); after, it does not (calls=0 → PASS).

NOTE: a SECOND synchronous per-root-lock acquisition remains on this
path — `publish_event_sync` (event_journal.py:264) resolves `cwd` via
`session_manager.get_file_ref_context`, which also takes the per-root
lock. That site was NOT the one captured by the dumps (get_lite
blocked first) and is left open: fixing it needs either a lock-free
cwd source or moving the journal submit off-loop, both of which touch
concurrency/recovery-sensitive code. See monitor_routine_notes.log.

Run with:
    cd backend && PYTHONPATH=. .venv/bin/python scripts/test_orphan_ingest_no_session_lock.py
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys

# State-isolation rule: isolate BETTER_AGENT_HOME BEFORE importing any
# backend module so every store, runs root, traces dir lands in a
# throwaway tempdir.
import _test_home
_TMP_HOME = _test_home.isolate("bc-test-orphan-nolock-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import provider_claude  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"


def _enriched(uid: str, text: str) -> dict:
    return {
        "type": "assistant",
        "message": {"id": uid, "content": [{"type": "text", "text": text}]},
    }


async def test_a_orphan_ingest_does_not_call_get_lite() -> bool:
    sess = session_manager.create(
        name="orphan-nolock", model="claude-fable-5", cwd="/tmp",
        orchestration_mode="native", source="cli",
    )
    sid = sess["id"]

    calls = {"n": 0}
    original = session_manager.get_lite

    def spy(_sid: str):
        calls["n"] += 1
        return original(_sid)

    session_manager.get_lite = spy  # type: ignore[assignment]
    try:
        # The method never references `self`, so a bare instance is fine.
        provider_claude.ClaudeProvider._ingest_orphan_line(
            object(), sid, "run-no-lock", _enriched("uuid-A", "payload-A"),
            mode="native",
        )
    finally:
        session_manager.get_lite = original  # type: ignore[assignment]

    ok = calls["n"] == 0
    print(f"{PASS if ok else FAIL} A: orphan ingest skips get_lite "
          f"(calls={calls['n']})")
    return ok


async def _run() -> int:
    results = [
        await test_a_orphan_ingest_does_not_call_get_lite(),
    ]
    total = len(results)
    passed = sum(1 for r in results if r)
    print(f"\n{passed}/{total} subtests passed")
    return 0 if passed == total else 1


def main() -> int:
    try:
        return asyncio.run(_run())
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
