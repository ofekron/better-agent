"""Regression: retrying a stopped/failed turn DISCARDS it (no duplicate prompt).

A stopped/failed turn must be rewound before the retry re-sends, otherwise
the failed user+assistant pair stays in history AND the re-sent prompt is
appended again — producing two consecutive identical user prompts (the
exact bug this locks down).

Covers both rewind paths in `rewind_and_retry`:
  1. failed user message HAS a provider anchor (`agent_message_uuid`) ⇒
     the provider CLI is rewound and the pair is truncated;
  2. failed user message has NO anchor ⇒ full rewind raises, we fall back
     to render-tree-only truncation (`provider_rewind=False`), still
     removing the pair without touching the provider CLI.

Run:
    cd backend && .venv/bin/python scripts/test_retry_discards_failed_turn.py
"""

from __future__ import annotations

import os
import shutil
import sys
import asyncio

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-retry-discards-failed-turn-")
os.environ["BETTER_CLAUDE_TEST_AUTH_BYPASS"] = "1"

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import main  # noqa: E402
import session_store  # noqa: E402
import session_manager as _sm_mod  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402

_sm_mod.PERSIST_DEBOUNCE_S = 0.0

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _reset_home() -> None:
    from pathlib import Path
    sessions_dir = Path(_TMP_HOME) / "sessions"
    if sessions_dir.exists():
        shutil.rmtree(sessions_dir)
    session_store._fork_index.clear()
    session_store._index_loaded = False
    session_store._summary_index.clear()
    session_store._summary_index_loaded = False
    session_manager._roots.clear()
    session_manager._node_root_id.clear()
    session_manager._root_locks.clear()
    session_manager._batches.clear()


class _FakeProvider:
    """Minimal provider stub: records `rewind` calls, never spawns a CLI."""

    def __init__(self, *, rewind_requires_agent_identity: bool) -> None:
        self.supports_rewind = True
        self.supports_semantic_alter = False
        self.rewind_requires_agent_identity = rewind_requires_agent_identity
        self.defunct = False
        self.rewind_calls: list[tuple[str, str]] = []

    async def rewind(self, session_id: str, anchor: str) -> None:
        self.rewind_calls.append((session_id, anchor))


def _seed(user_uuid: str | None) -> tuple[str, dict]:
    root = session_manager.create(name="retry", cwd="/tmp")
    sid = root["id"]
    user = {
        "id": "u1",
        "role": "user",
        "content": "the rendering is better but still not perfect",
        "events": [],
        "timestamp": "2026-06-16T20:31:53",
        "isStreaming": False,
    }
    if user_uuid:
        user["agent_message_uuid"] = user_uuid
    assistant = {
        "id": "a1",
        "role": "assistant",
        "content": "API Error: The operation timed out.",
        "events": [],
        "timestamp": "2026-06-16T20:32:00",
        "isStreaming": False,
        "stopped_at": "2026-06-16T20:32:00",
    }
    session_manager.append_user_msg(sid, user)
    session_manager.append_assistant_msg(sid, assistant)
    return sid, user


def _run_retry(sid: str, provider: _FakeProvider) -> dict:
    original = main.coordinator.provider_for_session
    main.coordinator.provider_for_session = lambda _sid: provider
    try:
        return asyncio.run(
            main.rewind_and_retry(sid, {"assistant_message_id": "a1"})
        )
    finally:
        main.coordinator.provider_for_session = original


def test_retry_rewinds_provider_when_anchor_present() -> bool:
    _reset_home()
    sid, user = _seed(user_uuid="uuid-8f9c6852")
    provider = _FakeProvider(rewind_requires_agent_identity=False)
    body = _run_retry(sid, provider)

    messages = (session_manager.get(sid) or {}).get("messages") or []
    ok = (
        body.get("retry_prompt") == user["content"]
        and messages == []  # failed user+assistant pair discarded
        and provider.rewind_calls == [(sid, "uuid-8f9c6852")]
    )
    print(f"{PASS if ok else FAIL} retry discards failed turn + rewinds provider (anchor present)")
    if not ok:
        print({"messages": [m.get("id") for m in messages], "rewind_calls": provider.rewind_calls})
    return ok


def test_retry_truncates_without_anchor() -> bool:
    sid, user = _seed(user_uuid=None)
    # Requires an agent anchor it can't find ⇒ full rewind raises ⇒ the
    # endpoint falls back to render-tree-only truncation.
    provider = _FakeProvider(rewind_requires_agent_identity=True)
    body = _run_retry(sid, provider)

    messages = (session_manager.get(sid) or {}).get("messages") or []
    ok = (
        body.get("retry_prompt") == user["content"]
        and messages == []  # failed pair still discarded
        and provider.rewind_calls == []  # no provider CLI rewind
    )
    print(f"{PASS if ok else FAIL} retry discards failed turn via truncation (no anchor)")
    if not ok:
        print({"messages": [m.get("id") for m in messages], "rewind_calls": provider.rewind_calls})
    return ok


def main_run() -> int:
    try:
        results = [
            test_retry_rewinds_provider_when_anchor_present(),
            test_retry_truncates_without_anchor(),
        ]
        return 0 if all(results) else 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main_run())
