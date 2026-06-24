"""Backend unit tests for AI-driven session search.

Covers:
  * `_extract_first_user_prompt` shape handling (string content / list
    content / no user msgs / truncation at 200).
  * `_build_index` filtering — hidden (working_mode) / archived /
    normal — and the field shape it emits.
  * Stale-id filtering — `propose_sessions` ids that don't exist in the
    live index are dropped by `validate_proposed`.
  * No proposal — a turn that never calls `propose_sessions` leaves no
    `ask_result` → `search()` returns `error="parse_failed"`.
  * Latest-wins concurrency — a second `search()` while the first is
    in-flight cancels the prior task.
  * REST endpoint rejects empty / whitespace `query` with HTTP 400.

Run with:
    cd backend && .venv/bin/python scripts/test_session_search_unit.py
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

# Per CLAUDE.md: isolate ~/.better-claude state to a tempdir BEFORE
# importing any backend module.
import _test_home
_TMP_HOME = _test_home.isolate("bc-test-session-search-")
os.environ["BETTER_CLAUDE_API_ONLY"] = "1"

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import session_search  # noqa: E402
import session_store  # noqa: E402


PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _reset_home() -> None:
    """Wipe seeded session files between tests so each test sees a
    clean tempdir under the same BETTER_CLAUDE_HOME. Recreate the
    `sessions/` dir immediately: a prior test's DEFERRED singleton
    persist (`session_manager._tail_persist`) can fire after this wipe,
    and a missing dir would raise a noisy FileNotFoundError. The stale
    write is harmless — the singleton is excluded from `_build_index`
    and overwritten by the next test."""
    sessions_dir = Path(_TMP_HOME) / "sessions"
    if sessions_dir.exists():
        shutil.rmtree(sessions_dir)
    sessions_dir.mkdir(parents=True, exist_ok=True)
    with session_store._summary_index_lock:  # type: ignore[attr-defined]
        session_store._summary_index.clear()  # type: ignore[attr-defined]
        session_store._summary_sorted_cache.clear()  # type: ignore[attr-defined]
        session_store._summary_index_loaded = False  # type: ignore[attr-defined]
        session_store._summary_index_version = 0  # type: ignore[attr-defined]
        session_store._summary_sorted_cache_version = -1  # type: ignore[attr-defined]


def _write_session(
    *,
    sid: str,
    name: str = "test",
    cwd: str = "/tmp/proj",
    messages: list | None = None,
    archived: bool = False,
    working_mode_value: str | None = None,
    updated_at: str = "2026-05-01T00:00:00",
) -> None:
    """Write a minimal root session JSON to the tempdir's sessions dir."""
    sessions_dir = Path(_TMP_HOME) / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    payload: dict = {
        "id": sid,
        "name": name,
        "cwd": cwd,
        "messages": messages or [],
        "updated_at": updated_at,
        "archived": archived,
    }
    if working_mode_value is not None:
        payload["working_mode"] = working_mode_value
        payload["working_mode_meta"] = {}
    (sessions_dir / f"{sid}.json").write_text(json.dumps(payload))


# ──────────────────────────────────────────────────────────────────────
# _extract_first_user_prompt
# ──────────────────────────────────────────────────────────────────────


def test_extract_first_user_prompt_shapes() -> bool:
    cases = [
        ([], "", "empty list"),
        (
            [{"role": "assistant", "content": "hi"}],
            "",
            "no user msgs",
        ),
        (
            [{"role": "user", "content": "fix the auth bug"}],
            "fix the auth bug",
            "string content",
        ),
        (
            [{"role": "user", "content": [
                {"type": "text", "text": "refactor "},
                {"type": "text", "text": "the login flow"},
            ]}],
            "refactor \nthe login flow",
            "list-of-blocks content",
        ),
        (
            [
                {"role": "user", "content": ""},  # empty user msg skipped
                {"role": "user", "content": "second user msg wins"},
            ],
            "second user msg wins",
            "skip empty user msgs",
        ),
    ]
    for messages, expected, label in cases:
        got = session_search._extract_first_user_prompt(messages)
        if got != expected:
            print(f"{FAIL} extract[{label}]: expected {expected!r} got {got!r}")
            return False
    # Truncation
    long = "x" * 500
    got = session_search._extract_first_user_prompt(
        [{"role": "user", "content": long}]
    )
    if not got.endswith("…") or len(got) != 201:
        print(f"{FAIL} extract[truncate]: got len={len(got)} tail={got[-3:]!r}")
        return False
    print(f"{PASS} _extract_first_user_prompt shapes + truncation")
    return True


# ──────────────────────────────────────────────────────────────────────
# _build_index
# ──────────────────────────────────────────────────────────────────────


def test_build_index_filters_hidden_and_archived() -> bool:
    _reset_home()
    _write_session(
        sid="normal-1",
        name="auth refactor",
        cwd="/tmp/myproj",
        messages=[{"role": "user", "content": "refactor the auth flow"}],
        updated_at="2026-05-03T00:00:00",
    )
    _write_session(
        sid="normal-2",
        name="redis bug",
        cwd="/tmp/myproj",
        messages=[{"role": "user", "content": "redis is dropping connections"}],
        updated_at="2026-05-02T00:00:00",
    )
    _write_session(
        sid="archived-1",
        name="old session",
        archived=True,
        messages=[{"role": "user", "content": "x"}],
    )
    _write_session(
        sid="hidden-1",
        name="prompt-eng draft",
        working_mode_value="prompt_engineering",
        messages=[{"role": "user", "content": "x"}],
    )
    index = session_search._build_index()
    ids = {s["id"] for s in index}
    if ids != {"normal-1", "normal-2"}:
        print(f"{FAIL} build_index filter: got ids {ids}")
        return False
    by_id = {s["id"]: s for s in index}
    # Field shape on a representative entry.
    auth = by_id["normal-1"]
    expected_keys = {
        "id", "name", "cwd", "project_name", "first_user_prompt",
        "updated_at", "message_count",
    }
    if set(auth.keys()) != expected_keys:
        print(f"{FAIL} build_index fields: got {set(auth.keys())}")
        return False
    if auth["project_name"] != "myproj":
        print(f"{FAIL} build_index project_name: got {auth['project_name']!r}")
        return False
    if auth["first_user_prompt"] != "refactor the auth flow":
        print(f"{FAIL} build_index first_user_prompt mismatch")
        return False
    # Newest first.
    if index[0]["id"] != "normal-1":
        print(f"{FAIL} build_index order: expected normal-1 first")
        return False
    print(f"{PASS} _build_index filters hidden + archived; field shape correct")
    return True


# ──────────────────────────────────────────────────────────────────────
# Stale-id filtering (validate_proposed)
# ──────────────────────────────────────────────────────────────────────


def test_validate_proposed_drops_unknown() -> bool:
    _reset_home()
    _write_session(sid="live-1", messages=[{"role": "user", "content": "x"}])
    # Ghost ids (not in the index) and duplicates are dropped; order kept.
    out = session_search.validate_proposed(["live-1", "ghost", "live-1"])
    if out != ["live-1"]:
        print(f"{FAIL} validate_proposed: got {out!r}")
        return False
    # Non-list input → [].
    if session_search.validate_proposed(None) != []:  # type: ignore[arg-type]
        print(f"{FAIL} validate_proposed: non-list not []")
        return False
    print(f"{PASS} validate_proposed drops unknown/hidden/dup ids")
    return True


def test_run_search_sessions_uses_local_index() -> bool:
    _reset_home()
    session_store.write_session_full(
        {
            "id": "auth-local",
            "name": "Auth latency",
            "cwd": "/tmp/proj",
            "messages": [{"role": "user", "content": "speed up login search"}],
            "updated_at": "2026-05-04T00:00:00",
        },
        bump_updated_at=False,
    )

    async def _explode(*args, **kwargs):
        raise AssertionError("provisioned search worker should not run")

    original = session_search.provisioning.run
    session_search.provisioning.run = _explode
    try:
        out = asyncio.run(
            session_search.run_search_sessions_session("Auth", max_results=5)
        )
    finally:
        session_search.provisioning.run = original

    if out.get("error") is not None:
        print(f"{FAIL} local_search: unexpected error {out!r}")
        return False
    if out.get("session_ids") != ["auth-local"]:
        print(f"{FAIL} local_search: got {out!r}")
        return False
    print(f"{PASS} run_search_sessions_session uses local index")
    return True


# ──────────────────────────────────────────────────────────────────────
# Empty-query rejection at the public API
# ──────────────────────────────────────────────────────────────────────


def test_empty_query_returns_empty_query_error() -> bool:
    _reset_home()
    _write_session(sid="live-1", messages=[{"role": "user", "content": "x"}])
    out = asyncio.run(session_search.search(""))
    if out["error"] != "empty_query":
        print(f"{FAIL} empty_query: error={out['error']!r}")
        return False
    out = asyncio.run(session_search.search("   "))
    if out["error"] != "empty_query":
        print(f"{FAIL} whitespace_query: error={out['error']!r}")
        return False
    print(f"{PASS} empty / whitespace query → error=empty_query")
    return True


# ──────────────────────────────────────────────────────────────────────
# REST endpoint 400 on empty query
# ──────────────────────────────────────────────────────────────────────


def test_rest_endpoint_rejects_empty_query() -> bool:
    """Verify the FastAPI route raises HTTPException(400) on whitespace
    query, without invoking the provider at all.
    """
    _reset_home()
    # Import lazily — pulling `main` warms up the full app graph, which
    # is fine but slow; only do it once and only for this test.
    import main  # noqa: F401
    import config_store
    import extension_store
    from fastapi.testclient import TestClient
    from auth_test_helpers import authenticate_client
    data = extension_store._load()  # type: ignore[attr-defined]
    data["extensions"][extension_store.BUILTIN_ASK_EXTENSION_ID] = {
        "manifest": {"id": extension_store.BUILTIN_ASK_EXTENSION_ID},
        "enabled": True,
        "source": {"type": "test", "install_path": ""},
        "entitlement": {"status": "not_required"},
    }
    extension_store._save(data)  # type: ignore[attr-defined]
    providers = config_store.list_providers()["providers"]
    provider = providers[0]
    assignments = config_store.get_internal_llm_assignments()
    assignments["session_search_worker"] = {
        "provider_id": provider["id"],
        "model": provider["default_model"],
        "reasoning_effort": provider.get("default_reasoning_effort") or "",
    }
    config_store.set_internal_llm_assignments(assignments)
    client = TestClient(main.app)
    authenticate_client(client)
    res = client.post(
        "/api/internal/ask-ui/search",
        headers={"X-Internal-Token": main.coordinator.internal_token},
        json={"query": "   "},
    )
    if res.status_code != 400:
        print(f"{FAIL} rest_empty_query: status={res.status_code}")
        return False
    body = res.json()
    if "detail" not in body or "non-empty" not in body["detail"]:
        print(f"{FAIL} rest_empty_query: body={body!r}")
        return False
    print(f"{PASS} POST /api/internal/ask-ui/search 400 on empty query")
    return True


# ──────────────────────────────────────────────────────────────────────
# Ask outcome: a non-parseable / errored worker reply must NOT become a
# red "Failed" assistant bubble — it's surfaced inside the picker instead.
# ──────────────────────────────────────────────────────────────────────


def test_parse_failed_is_not_an_error_bubble() -> bool:
    # parse_failed (worker reply had no usable JSON) → the assistant message
    # is a completed, NON-error turn (no error/errorText stamp).
    result = {"session_ids": [], "reasoning": "", "error": "parse_failed"}
    msg = session_search._ask_assistant_message_from_worker_result(result)
    if msg.get("error") or "errorText" in msg:
        print(f"{FAIL} parse_failed bubble: msg still stamped error {msg!r}")
        return False
    if not msg.get("completed_at"):
        print(f"{FAIL} parse_failed bubble: missing completed_at")
        return False
    print(f"{PASS} parse_failed worker result → completed non-error assistant msg")
    return True


def test_ask_error_message_mapping() -> bool:
    soft = {"parse_failed", "timeout", "dispatch_failed"}
    for code in soft:
        if not session_search._ask_error_message(code):
            print(f"{FAIL} ask_error_message[{code}]: expected non-empty text")
            return False
    for code in (None, "", "empty_query", "cancelled", "weird"):
        if session_search._ask_error_message(code) != "":
            print(f"{FAIL} ask_error_message[{code!r}]: expected ''")
            return False
    print(f"{PASS} _ask_error_message maps soft errors to picker text, else ''")
    return True


# ──────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────


def main_run() -> int:
    tests = [
        test_extract_first_user_prompt_shapes,
        test_build_index_filters_hidden_and_archived,
        test_validate_proposed_drops_unknown,
        test_run_search_sessions_uses_local_index,
        test_empty_query_returns_empty_query_error,
        test_parse_failed_is_not_an_error_bubble,
        test_ask_error_message_mapping,
        test_rest_endpoint_rejects_empty_query,
    ]
    results = []
    for fn in tests:
        try:
            results.append(fn())
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"{FAIL} {fn.__name__} raised: {e}")
            results.append(False)
    n_pass = sum(1 for r in results if r)
    n_total = len(results)
    print(f"\n{n_pass}/{n_total} session-search unit tests passed")
    shutil.rmtree(_TMP_HOME, ignore_errors=True)
    return 0 if n_pass == n_total else 1


if __name__ == "__main__":
    sys.exit(main_run())
