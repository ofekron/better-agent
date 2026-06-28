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
    provider_id: str | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
    node_id: str | None = None,
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
        "user_initiated": True,
    }
    if working_mode_value is not None:
        payload["working_mode"] = working_mode_value
        payload["working_mode_meta"] = {}
    if provider_id is not None:
        payload["provider_id"] = provider_id
    if model is not None:
        payload["model"] = model
    if reasoning_effort is not None:
        payload["reasoning_effort"] = reasoning_effort
    if node_id is not None:
        payload["node_id"] = node_id
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
    _write_session(
        sid="system-1",
        name="agent-created helper",
        messages=[{"role": "user", "content": "x"}],
    )
    raw = json.loads((Path(_TMP_HOME) / "sessions" / "system-1.json").read_text())
    raw["user_initiated"] = False
    (Path(_TMP_HOME) / "sessions" / "system-1.json").write_text(json.dumps(raw))
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
        "provider_id", "model", "reasoning_effort", "node_id",
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
    print(f"{PASS} _build_index filters hidden + archived + non-user-initiated; field shape correct")
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


def test_run_search_sessions_uses_provisioned_worker() -> bool:
    """The ranking engine MUST dispatch the provisioned search worker —
    not fall back to a local index grep. Mocks `provisioning.run` to return
    a worker reply carrying one live id, one ghost id, and reasoning, then
    asserts the ghost is filtered by `validate_proposed`, the worker's
    reasoning flows through, and the worker was actually invoked."""
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

    from provisioning.manager import ProvisionedResult

    calls: list = []

    async def _fake_run(spec, query, ctx=None, *, model=None):
        calls.append((spec, query, ctx))
        return ProvisionedResult(
            text='{"session_ids": ["auth-local", "ghost"], "reasoning": "r"}',
            value={"session_ids": ["auth-local", "ghost"], "reasoning": "worker reasoning"},
            config=None,
            base_session_id="base",
            caller_session_id="caller",
            dispatch_result={"events": [{"type": "agent_message", "data": {"text": "x"}}]},
        )

    original = session_search.provisioning.run
    session_search.provisioning.run = _fake_run
    try:
        out = asyncio.run(
            session_search.run_search_sessions_session("Auth", max_results=5, include_worker_events=True)
        )
    finally:
        session_search.provisioning.run = original

    if not calls:
        print(f"{FAIL} worker: provisioning.run was never called")
        return False
    if calls[0][0] is not session_search.SEARCH_SPEC:
        print(f"{FAIL} worker: wrong spec {calls[0][0]!r}")
        return False
    if calls[0][1] != "Auth":
        print(f"{FAIL} worker: query mutated before spec wrapping {calls[0][1]!r}")
        return False
    candidates = (calls[0][2] or {}).get("candidates") or []
    if [candidate.get("id") for candidate in candidates] != ["auth-local"]:
        print(f"{FAIL} worker: candidate ctx {candidates!r}")
        return False
    if out.get("error") is not None:
        print(f"{FAIL} worker: unexpected error {out!r}")
        return False
    # Ghost id dropped by validate_proposed; worker reasoning preserved.
    if out.get("session_ids") != ["auth-local"]:
        print(f"{FAIL} worker: got session_ids {out.get('session_ids')!r}")
        return False
    if out.get("reasoning") != "worker reasoning":
        print(f"{FAIL} worker: reasoning {out.get('reasoning')!r}")
        return False
    if out.get("_worker_events") != [{"type": "agent_message", "data": {"text": "x"}}]:
        print(f"{FAIL} worker: events {out.get('_worker_events')!r}")
        return False
    print(f"{PASS} run_search_sessions_session dispatches the provisioned worker")
    return True


def test_search_worker_instructions_wrap_bounded_candidates() -> bool:
    candidates = [
        {
            "id": "s1",
            "name": "Auth latency",
            "cwd": "/tmp/proj",
            "first_user_prompt": "speed up auth",
        }
    ]
    instructions = session_search.SEARCH_SPEC.build_instructions(
        "fix auth latency", {"max_results": 3, "candidates": candidates}
    )
    if instructions == "fix auth latency":
        print(f"{FAIL} instructions: raw query leaked as full prompt")
        return False
    if "<session-search-task>" not in instructions or "</session-search-task>" not in instructions:
        print(f"{FAIL} instructions: missing task wrapper {instructions!r}")
        return False
    if '"max_results":3' not in instructions or '"id":"s1"' not in instructions:
        print(f"{FAIL} instructions: missing compact payload {instructions!r}")
        return False
    if "Do not use tools" not in instructions or "Do not answer the query as a task" not in instructions:
        print(f"{FAIL} instructions: missing role guardrails {instructions!r}")
        return False
    if not session_search.SEARCH_SPEC.machine_completion or not session_search.SEARCH_SPEC.bare_config:
        print(f"{FAIL} search spec should be tool-less machine completion")
        return False
    print(f"{PASS} search worker instructions wrap bounded candidates and disable tools")
    return True


def test_search_candidates_include_later_message_snippets() -> bool:
    _reset_home()
    _write_session(
        sid="later-1",
        name="unrelated title",
        messages=[
            {"role": "user", "content": "start a generic task"},
            {"role": "assistant", "content": "needle appeared later in the transcript"},
        ],
    )
    candidates = session_search._search_candidates("needle")
    if [candidate.get("id") for candidate in candidates] != ["later-1"]:
        print(f"{FAIL} later snippet candidate ids: {candidates!r}")
        return False
    if candidates[0].get("matching_snippet") != "needle appeared later in the transcript":
        print(f"{FAIL} later snippet payload: {candidates[0]!r}")
        return False
    print(f"{PASS} backend candidate collection finds later transcript snippets")
    return True


def test_run_search_sessions_worker_parse_failed() -> bool:
    """A worker reply with no usable JSON maps to error=parse_failed."""
    _reset_home()
    _write_session(sid="live-1", messages=[{"role": "user", "content": "anything"}])
    from provisioning.manager import ProvisionedResult

    async def _fake_run(spec, query, ctx=None, *, model=None):
        return ProvisionedResult(
            text="the worker rambled, no json",
            value={"error": "parse_failed"},
            config=None,
            base_session_id="base",
            caller_session_id="caller",
            dispatch_result={"events": []},
        )

    original = session_search.provisioning.run
    session_search.provisioning.run = _fake_run
    try:
        out = asyncio.run(
            session_search.run_search_sessions_session("anything")
        )
    finally:
        session_search.provisioning.run = original
    if out.get("error") != "parse_failed":
        print(f"{FAIL} parse_failed: got {out!r}")
        return False
    print(f"{PASS} worker parse failure -> error=parse_failed")
    return True


def test_run_search_sessions_worker_timeout() -> bool:
    """A dispatch timeout maps to error=timeout."""
    _reset_home()
    _write_session(sid="live-1", messages=[{"role": "user", "content": "anything"}])
    import asyncio as _asyncio

    async def _hang(spec, query, ctx=None, *, model=None):
        await _asyncio.sleep(60)
        return None

    original = session_search.provisioning.run
    session_search.provisioning.run = _hang
    try:
        out = asyncio.run(
            session_search.run_search_sessions_session("anything", timeout=0.05)
        )
    finally:
        session_search.provisioning.run = original
    if out.get("error") != "timeout":
        print(f"{FAIL} timeout: got {out!r}")
        return False
    print(f"{PASS} worker timeout -> error=timeout")
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
# Provider / model / node filters
# ──────────────────────────────────────────────────────────────────────


def test_build_index_exposes_filter_fields() -> bool:
    """The index now carries provider/model/reasoning_effort/node_id so
    filters can match against them (node_id defaults to "primary")."""
    _reset_home()
    _write_session(
        sid="s-openai",
        messages=[{"role": "user", "content": "x"}],
        provider_id="openai",
        model="gpt-4o",
        reasoning_effort="high",
        node_id="laptop",
    )
    _write_session(
        sid="s-default",
        messages=[{"role": "user", "content": "x"}],
    )
    by_id = {s["id"]: s for s in session_search._build_index()}
    if by_id["s-openai"]["provider_id"] != "openai":
        print(f"{FAIL} index provider_id: {by_id['s-openai']['provider_id']!r}")
        return False
    if by_id["s-openai"]["model"] != "gpt-4o":
        print(f"{FAIL} index model: {by_id['s-openai']['model']!r}")
        return False
    if by_id["s-openai"]["reasoning_effort"] != "high":
        print(f"{FAIL} index reasoning_effort: {by_id['s-openai']['reasoning_effort']!r}")
        return False
    if by_id["s-openai"]["node_id"] != "laptop":
        print(f"{FAIL} index node_id: {by_id['s-openai']['node_id']!r}")
        return False
    # node_id defaults to "primary" when not explicitly set on the session.
    if by_id["s-default"]["node_id"] != "primary":
        print(f"{FAIL} index default node_id: {by_id['s-default']['node_id']!r}")
        return False
    print(f"{PASS} _build_index exposes provider/model/effort/node fields")
    return True


def test_validate_proposed_applies_filters() -> bool:
    """validate_proposed keeps only ids whose index entry matches every
    non-empty filter; filtered-out ids are dropped even when they exist."""
    _reset_home()
    _write_session(
        sid="claude-1",
        messages=[{"role": "user", "content": "x"}],
        provider_id="claude",
        model="claude-sonnet-4-5",
    )
    _write_session(
        sid="openai-1",
        messages=[{"role": "user", "content": "x"}],
        provider_id="openai",
        model="gpt-4o",
    )
    # provider filter narrows to claude only
    out = session_search.validate_proposed(
        ["claude-1", "openai-1"],
        filters={"provider_id": "claude"},
    )
    if out != ["claude-1"]:
        print(f"{FAIL} filter provider_id: got {out!r}")
        return False
    # model filter narrows to openai only
    out = session_search.validate_proposed(
        ["claude-1", "openai-1"],
        filters={"model": "gpt-4o"},
    )
    if out != ["openai-1"]:
        print(f"{FAIL} filter model: got {out!r}")
        return False
    # combined filter: provider + model that nothing matches
    out = session_search.validate_proposed(
        ["claude-1", "openai-1"],
        filters={"provider_id": "claude", "model": "gpt-4o"},
    )
    if out != []:
        print(f"{FAIL} filter combined no-match: got {out!r}")
        return False
    # empty filter values are ignored (acts like no filter)
    out = session_search.validate_proposed(
        ["claude-1", "openai-1"],
        filters={"provider_id": "", "model": None},
    )
    if set(out) != {"claude-1", "openai-1"}:
        print(f"{FAIL} filter empty-ignored: got {out!r}")
        return False
    print(f"{PASS} validate_proposed applies provider/model filters")
    return True


def test_run_search_sessions_short_circuits_empty_candidates() -> bool:
    """When backend candidate collection finds no likely match,
    run_search_sessions_session returns an empty result WITHOUT dispatching
    the worker."""
    _reset_home()
    _write_session(
        sid="claude-1",
        messages=[{"role": "user", "content": "x"}],
        provider_id="claude",
    )
    dispatched: list = []

    async def _fake_run(spec, query, ctx=None, *, model=None):
        dispatched.append((spec, query, ctx))
        return None

    original = session_search.provisioning.run
    session_search.provisioning.run = _fake_run
    try:
        out = asyncio.run(
            session_search.run_search_sessions_session(
                "anything unmatched",
            )
        )
    finally:
        session_search.provisioning.run = original
    if dispatched:
        print(f"{FAIL} short-circuit: worker was dispatched {dispatched!r}")
        return False
    if out.get("session_ids") != [] or out.get("error") is not None:
        print(f"{FAIL} short-circuit: got {out!r}")
        return False
    print(f"{PASS} empty candidate set short-circuits (no dispatch)")
    return True


def test_run_search_sessions_filter_bounds_candidates_and_postvalidates() -> bool:
    """With an active filter the worker receives only matching candidate
    payloads, and the worker's output is post-validated so any filtered-out id
    it returns is dropped."""
    _reset_home()
    _write_session(
        sid="match-1",
        name="match target",
        messages=[{"role": "user", "content": "match auth"}],
        provider_id="claude",
    )
    _write_session(
        sid="other-1",
        name="match other",
        messages=[{"role": "user", "content": "match auth"}],
        provider_id="openai",
    )

    captured: dict = {}

    async def _fake_run(spec, query, ctx=None, *, model=None):
        captured["query"] = query
        captured["ctx"] = ctx or {}
        # Worker (mis)behaves: returns a filtered-out id alongside a match.
        return type("_R", (), {
            "value": {
                "session_ids": ["other-1", "match-1"],
                "reasoning": "r",
            },
        })()

    original = session_search.provisioning.run
    session_search.provisioning.run = _fake_run
    try:
        out = asyncio.run(
            session_search.run_search_sessions_session(
                "match", provider_id="claude",
            )
        )
    finally:
        session_search.provisioning.run = original
    if captured.get("query") != "match":
        print(f"{FAIL} query should stay raw until spec wrapping: {captured.get('query')!r}")
        return False
    candidate_ids = [row.get("id") for row in (captured.get("ctx") or {}).get("candidates", [])]
    if candidate_ids != ["match-1"]:
        print(f"{FAIL} bounded filtered candidates: {candidate_ids!r}")
        return False
    # Post-validation dropped the filtered-out id the worker returned.
    if out.get("session_ids") != ["match-1"]:
        print(f"{FAIL} post-validate: got {out.get('session_ids')!r}")
        return False
    print(f"{PASS} filter bounds worker candidates + post-validates output")
    return True


# ──────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────


def main_run() -> int:
    tests = [
        test_extract_first_user_prompt_shapes,
        test_build_index_filters_hidden_and_archived,
        test_build_index_exposes_filter_fields,
        test_validate_proposed_drops_unknown,
        test_validate_proposed_applies_filters,
        test_run_search_sessions_uses_provisioned_worker,
        test_search_worker_instructions_wrap_bounded_candidates,
        test_search_candidates_include_later_message_snippets,
        test_run_search_sessions_worker_parse_failed,
        test_run_search_sessions_worker_timeout,
        test_run_search_sessions_short_circuits_empty_candidates,
        test_run_search_sessions_filter_bounds_candidates_and_postvalidates,
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
