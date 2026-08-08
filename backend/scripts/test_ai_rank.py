"""Unit tests for the core-owned generic AI-rank service (`backend/ai_rank.py`).

Covers: trailing-JSON parse (happy / chatty / garbage), fail-closed id
validation against the given candidate set, the per-kind registry
(registration, lookup, mismatched-task_key rejection), the full error
taxonomy (`empty_query`/`timeout`/`dispatch_failed`+`error_detail`/
`parse_failed`), and candidate-shape validation. All provisioning dispatch
is mocked -- no real LLM turns.

Run with:
    ./scripts/run-backend-tests.sh -- -k test_ai_rank
"""
from __future__ import annotations

import asyncio
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest  # noqa: E402

import ai_rank  # noqa: E402


# ── Trailing-JSON parse ───────────────────────────────────────────────────


def test_parse_worker_result_happy_path() -> None:
    text = '{"ids": ["a", "b"], "reasoning": "matches"}'
    assert ai_rank._parse_worker_result(text) == {"ids": ["a", "b"], "reasoning": "matches"}


def test_parse_worker_result_chatty_prose_before_json() -> None:
    text = 'Sure, here is my answer:\n{"ids": ["a"], "reasoning": "one sentence"}'
    assert ai_rank._parse_worker_result(text) == {"ids": ["a"], "reasoning": "one sentence"}


def test_parse_worker_result_uses_last_valid_object_after_brace_noise() -> None:
    text = (
        "I considered this malformed note first: {not json}\n"
        '{"ignored": true}\n'
        '{"ids":["live-1"],"reasoning":"matched {brace}"}'
    )
    assert ai_rank._parse_worker_result(text) == {"ids": ["live-1"], "reasoning": "matched {brace}"}


def test_parse_worker_result_garbage_returns_none() -> None:
    assert ai_rank._parse_worker_result("not json at all") is None
    assert ai_rank._parse_worker_result("") is None
    assert ai_rank._parse_worker_result('{"no_ids_key": true}') is None


# ── Candidate-shape validation ────────────────────────────────────────────


def test_validate_candidates_accepts_empty_list() -> None:
    assert ai_rank._validate_candidates([]) == []


def test_validate_candidates_accepts_well_formed_list() -> None:
    candidates = [{"id": "a", "name": "Alpha"}, {"id": "b"}]
    assert ai_rank._validate_candidates(candidates) == candidates


@pytest.mark.parametrize(
    "candidates",
    [
        "not-a-list",
        None,
        {"id": "a"},
        [{"id": "a"}, "not-a-dict"],
        [{"name": "no id field"}],
        [{"id": ""}],
        [{"id": "   "}],
        [{"id": 5}],
        [{"id": "a"}, {"id": "a"}],
        [{"id": "a", "big": "x" * 5000}],
        [{"id": "x" * 500}],
    ],
)
def test_validate_candidates_rejects_garbage(candidates) -> None:
    with pytest.raises(ValueError):
        ai_rank._validate_candidates(candidates)


def test_validate_candidates_rejects_too_many() -> None:
    with pytest.raises(ValueError):
        ai_rank._validate_candidates([{"id": str(i)} for i in range(ai_rank._MAX_CANDIDATES + 1)])


# ── Per-kind registry ──────────────────────────────────────────────────────


def test_sessions_kind_is_registered_at_import_with_search_worker_key() -> None:
    kind = ai_rank.get_kind("sessions")
    assert kind is not None
    assert kind.task_key == "session_search_worker"
    assert kind.spec_key == "search_worker"
    spec = ai_rank.get_spec("sessions")
    assert spec is not None
    assert spec.key == "search_worker"
    assert spec.task_key == "session_search_worker"


def test_register_kind_is_idempotent_for_identical_registration() -> None:
    kind = ai_rank.RankKind(kind="test_idempotent_kind", task_key="a_task_key")
    first = ai_rank.register_kind(kind)
    second = ai_rank.register_kind(kind)
    assert first.task_key == second.task_key == "a_task_key"
    # Re-registering the identical kind must not mint a second spec instance.
    assert ai_rank.get_spec("test_idempotent_kind") is ai_rank.get_spec("test_idempotent_kind")


def test_register_kind_rejects_task_key_mismatch() -> None:
    ai_rank.register_kind(ai_rank.RankKind(kind="test_conflict_kind", task_key="task_one"))
    with pytest.raises(ValueError):
        ai_rank.register_kind(ai_rank.RankKind(kind="test_conflict_kind", task_key="task_two"))


def test_get_kind_returns_none_for_unregistered_kind() -> None:
    assert ai_rank.get_kind("definitely_not_a_registered_kind") is None


def test_rank_raises_key_error_for_unregistered_kind() -> None:
    with pytest.raises(KeyError):
        asyncio.run(ai_rank.rank("definitely_not_a_registered_kind", "q", [{"id": "a"}], 5))


# ── rank() error taxonomy + fail-closed id validation ─────────────────────


def _register_test_kind(name: str) -> ai_rank.RankKind:
    return ai_rank.register_kind(ai_rank.RankKind(kind=name, task_key=f"{name}_task"))


def test_rank_empty_query_returns_empty_query_error() -> None:
    _register_test_kind("test_empty_query_kind")
    out = asyncio.run(ai_rank.rank("test_empty_query_kind", "   ", [{"id": "a"}], 5))
    assert out == {"ids": [], "reasoning": "", "error": "empty_query"}


def test_rank_no_candidates_short_circuits_without_dispatch(monkeypatch) -> None:
    _register_test_kind("test_no_candidates_kind")
    dispatched = []

    async def _fake_run(spec, query, ctx=None, *, model=None):
        dispatched.append((spec, query, ctx))
        raise AssertionError("must not dispatch with no candidates")

    monkeypatch.setattr(ai_rank.provisioning, "run", _fake_run)
    out = asyncio.run(ai_rank.rank("test_no_candidates_kind", "q", [], 5))
    assert out == {"ids": [], "reasoning": "", "error": None}
    assert dispatched == []


def test_rank_fail_closed_drops_ids_outside_candidate_set(monkeypatch) -> None:
    from provisioning.manager import ProvisionedResult

    _register_test_kind("test_fail_closed_kind")

    async def _fake_run(spec, query, ctx=None, *, model=None):
        return ProvisionedResult(
            text='{"ids": ["a", "ghost"], "reasoning": "r"}',
            value={"ids": ["a", "ghost"], "reasoning": "r"},
            config=None,
            base_session_id="base",
            caller_session_id="caller",
            dispatch_result={"events": []},
            timings_ms={},
        )

    monkeypatch.setattr(ai_rank.provisioning, "run", _fake_run)
    out = asyncio.run(
        ai_rank.rank("test_fail_closed_kind", "q", [{"id": "a"}, {"id": "b"}], 5)
    )
    assert out == {"ids": ["a"], "reasoning": "r", "error": None}


def test_rank_dedups_and_truncates_to_max_results(monkeypatch) -> None:
    from provisioning.manager import ProvisionedResult

    _register_test_kind("test_dedup_kind")

    async def _fake_run(spec, query, ctx=None, *, model=None):
        return ProvisionedResult(
            text="",
            value={"ids": ["a", "a", "b", "c"], "reasoning": "r"},
            config=None,
            base_session_id="base",
            caller_session_id="caller",
            dispatch_result={"events": []},
            timings_ms={},
        )

    monkeypatch.setattr(ai_rank.provisioning, "run", _fake_run)
    candidates = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
    out = asyncio.run(ai_rank.rank("test_dedup_kind", "q", candidates, 2))
    assert out == {"ids": ["a", "b"], "reasoning": "r", "error": None}


def test_rank_timeout_maps_to_timeout_error(monkeypatch) -> None:
    _register_test_kind("test_timeout_kind")

    async def _hang(spec, query, ctx=None, *, model=None):
        await asyncio.sleep(60)

    monkeypatch.setattr(ai_rank.provisioning, "run", _hang)
    out = asyncio.run(
        ai_rank.rank("test_timeout_kind", "q", [{"id": "a"}], 5, timeout=0.05)
    )
    assert out == {"ids": [], "reasoning": "", "error": "timeout"}


def test_rank_dispatch_exception_maps_to_dispatch_failed_with_detail(monkeypatch) -> None:
    _register_test_kind("test_dispatch_failed_kind")

    async def _boom(spec, query, ctx=None, *, model=None):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(ai_rank.provisioning, "run", _boom)
    out = asyncio.run(ai_rank.rank("test_dispatch_failed_kind", "q", [{"id": "a"}], 5))
    assert out["error"] == "dispatch_failed"
    assert out["error_detail"] == "provider exploded"
    assert out["ids"] == []


def test_rank_parse_failed_when_worker_reply_has_no_json(monkeypatch) -> None:
    from provisioning.manager import ProvisionedResult

    _register_test_kind("test_parse_failed_kind")

    async def _fake_run(spec, query, ctx=None, *, model=None):
        return ProvisionedResult(
            text="the worker rambled, no json",
            value={"error": "parse_failed"},
            config=None,
            base_session_id="base",
            caller_session_id="caller",
            dispatch_result={"events": []},
            timings_ms={},
        )

    monkeypatch.setattr(ai_rank.provisioning, "run", _fake_run)
    out = asyncio.run(ai_rank.rank("test_parse_failed_kind", "q", [{"id": "a"}], 5))
    assert out == {"ids": [], "reasoning": "", "error": "parse_failed"}


def test_rank_parse_failed_when_ids_field_is_not_a_list(monkeypatch) -> None:
    from provisioning.manager import ProvisionedResult

    _register_test_kind("test_parse_failed_non_list_kind")

    async def _fake_run(spec, query, ctx=None, *, model=None):
        return ProvisionedResult(
            text='{"ids": "not-a-list", "reasoning": "r"}',
            value={"ids": "not-a-list", "reasoning": "r"},
            config=None,
            base_session_id="base",
            caller_session_id="caller",
            dispatch_result={"events": []},
            timings_ms={},
        )

    monkeypatch.setattr(ai_rank.provisioning, "run", _fake_run)
    out = asyncio.run(ai_rank.rank("test_parse_failed_non_list_kind", "q", [{"id": "a"}], 5))
    assert out == {"ids": [], "reasoning": "", "error": "parse_failed"}


def test_rank_include_worker_events(monkeypatch) -> None:
    from provisioning.manager import ProvisionedResult

    _register_test_kind("test_worker_events_kind")

    async def _fake_run(spec, query, ctx=None, *, model=None):
        return ProvisionedResult(
            text='{"ids": ["a"], "reasoning": "r"}',
            value={"ids": ["a"], "reasoning": "r"},
            config=None,
            base_session_id="base",
            caller_session_id="caller",
            dispatch_result={"events": [{"type": "agent_message", "data": {"text": "x"}}]},
            timings_ms={},
        )

    monkeypatch.setattr(ai_rank.provisioning, "run", _fake_run)
    out = asyncio.run(
        ai_rank.rank(
            "test_worker_events_kind", "q", [{"id": "a"}], 5, include_worker_events=True,
        )
    )
    assert out["_worker_events"] == [{"type": "agent_message", "data": {"text": "x"}}]


def test_rank_rejects_invalid_max_results() -> None:
    _register_test_kind("test_invalid_max_results_kind")
    with pytest.raises(ValueError):
        asyncio.run(ai_rank.rank("test_invalid_max_results_kind", "q", [{"id": "a"}], 0))
    with pytest.raises(ValueError):
        asyncio.run(ai_rank.rank("test_invalid_max_results_kind", "q", [{"id": "a"}], -1))
    with pytest.raises(ValueError):
        asyncio.run(ai_rank.rank("test_invalid_max_results_kind", "q", [{"id": "a"}], True))


def test_rank_rejects_non_string_query() -> None:
    _register_test_kind("test_invalid_query_kind")
    with pytest.raises(ValueError):
        asyncio.run(ai_rank.rank("test_invalid_query_kind", None, [{"id": "a"}], 5))  # type: ignore[arg-type]


# ── Instructions / prompt wrapper ──────────────────────────────────────────


def test_build_instructions_wraps_bounded_candidates_and_disables_tools() -> None:
    spec = ai_rank.get_spec("sessions")
    candidates = [{"id": "s1", "name": "Auth latency"}]
    instructions = spec.build_instructions("fix auth latency", {"max_results": 3, "candidates": candidates})
    assert instructions != "fix auth latency"
    assert '<ai-rank-task kind="sessions">' in instructions
    assert "</ai-rank-task>" in instructions
    assert '"max_results":3' in instructions
    assert '"id":"s1"' in instructions
    assert "Do not use tools" in instructions
    assert "Do not answer the query as a task" in instructions


def test_provision_prompt_is_parameterized_by_item_noun() -> None:
    kind = _register_test_kind("test_noun_kind")
    spec = ai_rank.get_spec("test_noun_kind")
    prompt = spec.build_provision_prompt({})
    assert kind.plural in prompt
